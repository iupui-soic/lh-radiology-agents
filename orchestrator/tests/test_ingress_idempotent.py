"""Orthanc webhook idempotency (issue #11, per Saptarshi's review note on the MR).

Orthanc re-fires OnStableStudy for the same study (a late instance reopens the study, which then
goes stable again) — normal PACS behaviour. The workflow id is deterministic (wf_<orthancStudyId>),
so a duplicate event used to hit WorkflowAlreadyStartedError and surface as a 500 to the plugin;
once the plugin gains webhook retries (#47) a retry would keep hitting that 500. The webhook must
now treat a re-fire as a no-op: 200 with the existing workflow id.

Skipped when the orchestrator's deps (temporalio) aren't installed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

REPO_ROOT = str(Path(__file__).resolve().parents[2])

pytest.importorskip("temporalio", reason="temporalio not installed")
from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

ingress = pytest.importorskip("orchestrator.ingress", reason="orchestrator deps not installed")
from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402


# --- mock activities: drive the pre-read fan-out with no I/O, then park at the gate ---
@activity.defn(name="call_agent_skill_activity")
async def mock_call_agent(agent: str, skill_id: str, payload: dict) -> dict:
    if skill_id == "report.verify":
        return {"verificationStatus": "PASS", "requiresHumanReview": False, "issues": []}
    if skill_id == "triage.score":
        return {"priorityTier": "ROUTINE", "priorityScore": 50}
    return {"ok": True}


@activity.defn(name="publish_priority_activity")
async def mock_publish(workflow_id: str, study_instance_uid: str, triage: dict) -> None:
    return None

@activity.defn(name="publish_findings_activity")
async def mock_publish_findings(workflow_id: str, study_instance_uid: str, ai_result: dict) -> None:
    """Mock for #74 publish_findings_activity — never-raises like the production version."""
    return None


@activity.defn(name="publish_state_activity")
async def mock_publish_state(
    workflow_id: str, study_instance_uid: str, read_state: str, changed_at: str,
) -> None:
    """Mock for #108 publish_state_activity -- never-raises like the production version."""
    return None


@activity.defn(name="escalate_activity")
async def mock_escalate(workflow_id: str, reason: str) -> None:
    return None


class _FakeResolver:
    """Stand-in for the OpenMRS-REST order resolver in ingress tests (no live server). `result` is
    returned by resolve_radiology_order_by_accession; `error`, if set, is raised (simulates OpenMRS down)."""
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls: list[str] = []

    async def resolve_radiology_order_by_accession(self, accession: str):
        self.calls.append(accession)
        if self.error is not None:
            raise self.error
        return self.result


class _FakeOrthanc:
    """Stand-in for OrthancClient in ingress tests (no live Orthanc). Ingress reads the study's
    description back from Orthanc (#62); without this stand-in these tests would reach for a real
    server."""
    def __init__(self, description: str = "", error=None):
        self.description = description
        self.error = error

    async def get_study_description(self, orthanc_study_id: str) -> str:
        if self.error is not None:
            raise self.error
        return self.description


# A schema-valid OrthancStableStudyEvent (contracts/events/orthanc-stable.schema.json).
EVENT = {
    "schemaVersion": "1.0.0",
    "eventType": "orthanc.study.stable",
    "orthancStudyId": "dup1",
    "studyInstanceUID": "1.2.999",
    "modality": "CT",
    "accessionNumber": "ACC-DUP",
    "occurredAt": "2026-06-26T00:00:00Z",
}
WF_ID = "wf_dup1"


@pytest.fixture(autouse=True)
def _reset_ingress_globals():
    yield
    if ingress._STORE is not None:
        try:
            ingress._STORE.close()
        except Exception:  # noqa: BLE001
            pass
        ingress._STORE = None
    ingress._client = None  # don't leak the test env's client into another test
    ingress._OPENMRS_REST = None    # ditto the injected fhir2 client
    ingress._ORTHANC = None  # ditto the injected Orthanc client (#62)


async def _await_state(handle, target: str, tries: int = 400) -> None:
    for _ in range(tries):
        if await handle.query(StudyWorkflow.current_state) == target:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow never reached {target}")


def test_duplicate_stable_event_is_idempotent(tmp_path):
    """A re-fired stable-study event for a still-running study returns 200 (duplicate), not a 500."""
    db = str(tmp_path / "ingress.db")

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[mock_call_agent, mock_publish, mock_publish_findings,
                                          mock_publish_state, mock_escalate]):
                ingress._STORE = ingress.IngressStore(db)
                ingress._client = env.client  # so _temporal() returns the test client, no real connect
                ingress._OPENMRS_REST = _FakeResolver(result=None)  # unresolved: keep this test purely about idempotency
                ingress._ORTHANC = _FakeOrthanc()

                # First event: starts the workflow normally.
                first = await ingress.orthanc_webhook(dict(EVENT))
                assert first == {"started": WF_ID}

                # Let it park at the human gate so the study is unambiguously still RUNNING.
                handle = env.client.get_workflow_handle(WF_ID)
                await _await_state(handle, "AWAITING_RADIOLOGIST")

                # Orthanc re-fires the SAME event: must be a no-op 200, NOT WorkflowAlreadyStartedError.
                second = await ingress.orthanc_webhook(dict(EVENT))
                assert second == {"started": WF_ID, "duplicate": True}

    asyncio.run(scenario())


def test_duplicate_after_completion_starts_fresh(tmp_path):
    """Sanity: if the first run has CLOSED, a later same-study event is a normal fresh start (no
    duplicate flag). Guards against the catch masking a legitimate re-acquisition of the study."""
    db = str(tmp_path / "ingress.db")

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[mock_call_agent, mock_publish, mock_publish_findings,
                                          mock_publish_state, mock_escalate]):
                ingress._STORE = ingress.IngressStore(db)
                ingress._client = env.client
                ingress._OPENMRS_REST = _FakeResolver(result=None)
                ingress._ORTHANC = _FakeOrthanc()

                first = await ingress.orthanc_webhook(dict(EVENT))
                assert first == {"started": WF_ID}

                # Drive the first run to completion (signal a finalized report -> ARCHIVED).
                handle = env.client.get_workflow_handle(WF_ID)
                await _await_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/dup"})
                await handle.result()

                # Same study re-acquired after the prior run closed: normal start, not a duplicate.
                second = await ingress.orthanc_webhook(dict(EVENT))
                assert second == {"started": WF_ID}

    asyncio.run(scenario())
