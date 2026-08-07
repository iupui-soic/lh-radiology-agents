"""Ingress patient/order resolution via the radiology module REST (issues #11, #70).

`_build_study_context` resolves the real patient + order from the accession via the module's
`radiologyorder` REST search (fhir2 cannot search ServiceRequest by accession), with a graceful
fallback to `Patient/UNRESOLVED` so ingestion never fails the PACS. A stable-event re-fire
repairs an UNRESOLVED first pass by re-indexing the (now-resolved) ServiceRequest join, with no
restart.

Skipped when the orchestrator's deps (temporalio) aren't installed.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")
from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

ingress = pytest.importorskip("orchestrator.ingress", reason="orchestrator deps not installed")
from radagent_common import paths, validate_against  # noqa: E402

from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402


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
    """Stand-in for the OpenMRS-REST order resolver: `result` is returned by
    resolve_radiology_order_by_accession; `error`, if set, is raised (simulates OpenMRS being down)."""
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
    """Stand-in for OrthancClient: `description` is returned by get_study_description; `error`, if
    set, is raised (simulates Orthanc being down)."""
    def __init__(self, description: str = "", error=None):
        self.description = description
        self.error = error
        self.calls: list[str] = []

    async def get_study_description(self, orthanc_study_id: str) -> str:
        self.calls.append(orthanc_study_id)
        if self.error is not None:
            raise self.error
        return self.description


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
RESOLVED = {"fhirPatientId": "Patient/pat-1", "fhirServiceRequestId": "ServiceRequest/sr-dup"}


@pytest.fixture(autouse=True)
def _reset_ingress_globals():
    yield
    if ingress._STORE is not None:
        try:
            ingress._STORE.close()
        except Exception:  # noqa: BLE001
            pass
        ingress._STORE = None
    ingress._client = None
    ingress._OPENMRS_REST = None
    ingress._ORTHANC = None


async def _await_state(handle, target: str, tries: int = 400) -> None:
    for _ in range(tries):
        if await handle.query(StudyWorkflow.current_state) == target:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow never reached {target}")


# --- unit: resolution and its fallbacks ------------------------------------
def test_build_context_populates_real_patient_and_order():
    ingress._OPENMRS_REST = _FakeResolver(result=RESOLVED)
    ingress._ORTHANC = _FakeOrthanc()
    ctx = asyncio.run(ingress._build_study_context(dict(EVENT)))
    assert ctx["patient"] == {"fhirPatientId": "Patient/pat-1"}
    assert ctx["order"] == {"fhirServiceRequestId": "ServiceRequest/sr-dup"}


def test_build_context_falls_back_when_fhir2_down():
    ingress._OPENMRS_REST = _FakeResolver(error=RuntimeError("fhir2 unreachable"))
    ingress._ORTHANC = _FakeOrthanc()
    ctx = asyncio.run(ingress._build_study_context(dict(EVENT)))
    # Never fail the PACS: still a valid StudyContext, just the UNRESOLVED placeholder.
    assert ctx["patient"] == {"fhirPatientId": "Patient/UNRESOLVED"}
    assert ctx["order"] == {}


def test_a_resolver_failure_log_masks_credentials_embedded_in_urls(caplog):
    """httpx exception text embeds the request URL. A deployment that configured Basic
    credentials INTO the base URL (http://user:pass@host/...) must not have them echoed by the
    resolve warning -- which fires on every DICOM arrival when the resolver is down (#67)."""
    ingress._OPENMRS_REST = _FakeResolver(error=RuntimeError(
        "Connect failed for url "
        "'http://admin:Admin123@openmrs:8080/openmrs/ws/rest/v1/radiologyorder'"))
    ingress._ORTHANC = _FakeOrthanc()
    with caplog.at_level(logging.WARNING, logger="orchestrator.ingress"):
        ctx = asyncio.run(ingress._build_study_context(dict(EVENT)))
    assert ctx["patient"] == {"fhirPatientId": "Patient/UNRESOLVED"}
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "order resolution failed" in joined     # the reason still surfaces...
    assert "Admin123" not in joined                # ...the credentials never do
    assert "://***@openmrs" in joined
    # a raw @ inside the password must mask whole (greedy to the last @ before the path)
    assert ingress._redacted(RuntimeError(
        "boom at 'http://admin:p@ssword@openmrs:8080/x'")) == "boom at 'http://***@openmrs:8080/x'"
    # a query @ on a pathless URL must not swallow the hostname (over-mask direction)
    assert ingress._redacted(RuntimeError(
        "404 for http://openmrs:8080?email=jdoe@example.com")) == (
        "404 for http://openmrs:8080?email=jdoe@example.com")


def test_build_context_falls_back_on_miss():
    ingress._OPENMRS_REST = _FakeResolver(result=None)
    ingress._ORTHANC = _FakeOrthanc()
    ctx = asyncio.run(ingress._build_study_context(dict(EVENT)))
    assert ctx["patient"] == {"fhirPatientId": "Patient/UNRESOLVED"}
    assert ctx["order"] == {}


# --- the seam: the context ingress BUILDS must carry what triage SCORES on (issue #61) ------
#
# The bug this guards was invisible because it lived in the seam. Triage has 38 tests and they all
# pass; every one of them scores a hand-written fixture whose `order` already carries `priority` and
# `reasonCode`. Ingress -- the only thing that builds an `order` in production -- carried neither, so
# every real study reached triage with no urgency, scored the base 50, and came out ROUTINE. Both
# sides were green and the join between them was broken.
RESOLVED_STAT = {
    "fhirPatientId": "Patient/pat-1",
    "fhirServiceRequestId": "ServiceRequest/sr-dup",
    "priority": "stat",
    "reasonCode": ["J93.1"],  # pneumothorax
}


def test_build_context_carries_the_signals_triage_scores_on():
    ingress._OPENMRS_REST = _FakeResolver(result=RESOLVED_STAT)
    ctx = asyncio.run(ingress._build_study_context(dict(EVENT)))
    assert ctx["order"] == {
        "fhirServiceRequestId": "ServiceRequest/sr-dup",
        "priority": "stat",
        "reasonCode": ["J93.1"],
    }
    # And the envelope carrying them is still schema-valid: `order` is additionalProperties:false,
    # so a field that reaches it must be one the contract already declares.
    validate_against(ctx, paths.contracts_dir() / "studycontext.schema.json")


def test_build_context_omits_signals_an_order_does_not_have():
    ingress._OPENMRS_REST = _FakeResolver(result={k: RESOLVED_STAT[k]
                                      for k in ("fhirPatientId", "fhirServiceRequestId")})
    ctx = asyncio.run(ingress._build_study_context(dict(EVENT)))
    assert ctx["order"] == {"fhirServiceRequestId": "ServiceRequest/sr-dup"}
    validate_against(ctx, paths.contracts_dir() / "studycontext.schema.json")


# --- integration: a re-fire repairs the ServiceRequest join without a restart ---
def test_refire_repairs_serviceRequest_join(tmp_path):
    db = str(tmp_path / "ingress.db")

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[mock_call_agent, mock_publish, mock_publish_findings,
                                          mock_publish_state, mock_escalate]):
                ingress._STORE = ingress.IngressStore(db)
                ingress._client = env.client

                # Event 1: fhir2 is DOWN -> UNRESOLVED, accession-only index.
                ingress._OPENMRS_REST = _FakeResolver(error=RuntimeError("fhir2 down"))
                ingress._ORTHANC = _FakeOrthanc()
                assert await ingress.orthanc_webhook(dict(EVENT)) == {"started": WF_ID}
                # The robust ServiceRequest join does NOT exist yet...
                assert ingress._workflow_id_for_report(
                    {"serviceRequestRef": "ServiceRequest/sr-dup"}) is None
                # ...only the fragile accession join.
                assert ingress._workflow_id_for_report({"accessionNumber": "ACC-DUP"}) == WF_ID

                handle = env.client.get_workflow_handle(WF_ID)
                await _await_state(handle, "AWAITING_RADIOLOGIST")

                # Event 2 (re-fire): fhir2 is BACK -> duplicate 200, and the SR join is repaired
                # in place (no restart of the already-running workflow).
                ingress._OPENMRS_REST = _FakeResolver(result=RESOLVED)
                assert await ingress.orthanc_webhook(dict(EVENT)) == {"started": WF_ID, "duplicate": True}
                assert ingress._workflow_id_for_report(
                    {"serviceRequestRef": "ServiceRequest/sr-dup"}) == WF_ID

    asyncio.run(scenario())


# --- the seam: the context ingress BUILDS must carry the field the registry SELECTS on (#62) ---
#
# Same shape of bug as #61, one field over. `select_tools(modality, studyDescription)` picks the
# body-region tools almost entirely on the description -- `ich-detect`/`stroke-detect` for a CT HEAD,
# `cxr-screen`/`pneumothorax-detect` for a chest film -- and falls through to the modality's generic
# screen without one. The Orthanc stable event does not carry the description, ingress never read it
# back, and so every live study reached the registry with "" and got the generic tool. The
# interpretation agent's tests were green throughout: every fixture hand-writes a studyDescription.
def test_build_context_carries_the_description_the_registry_selects_on():
    ingress._OPENMRS_REST = _FakeResolver(result=RESOLVED)
    ingress._ORTHANC = _FakeOrthanc(description="CT HEAD WITHOUT CONTRAST")
    ctx = asyncio.run(ingress._build_study_context(dict(EVENT)))
    assert ctx["study"]["studyDescription"] == "CT HEAD WITHOUT CONTRAST"
    assert ingress._ORTHANC.calls == ["dup1"]  # looked it up by the event's orthancStudyId
    validate_against(ctx, paths.contracts_dir() / "studycontext.schema.json")


def test_build_context_omits_the_description_when_orthanc_has_none():
    ingress._OPENMRS_REST = _FakeResolver(result=RESOLVED)
    ingress._ORTHANC = _FakeOrthanc(description="")
    ctx = asyncio.run(ingress._build_study_context(dict(EVENT)))
    # Absent, not "": the key says "unknown", an empty string would assert "no description".
    assert "studyDescription" not in ctx["study"]
    validate_against(ctx, paths.contracts_dir() / "studycontext.schema.json")


def test_build_context_still_starts_when_orthanc_is_down():
    # Orthanc down costs the study its body-region tools. Failing the webhook would cost it the
    # whole workflow -- ingestion must never fail the PACS (#11).
    ingress._OPENMRS_REST = _FakeResolver(result=RESOLVED)
    ingress._ORTHANC = _FakeOrthanc(error=RuntimeError("orthanc unreachable"))
    ctx = asyncio.run(ingress._build_study_context(dict(EVENT)))
    assert "studyDescription" not in ctx["study"]
    assert ctx["patient"] == {"fhirPatientId": "Patient/pat-1"}  # the rest of the context survives
    validate_against(ctx, paths.contracts_dir() / "studycontext.schema.json")
