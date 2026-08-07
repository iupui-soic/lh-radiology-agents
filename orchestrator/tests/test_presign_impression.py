"""Pre-sign impression assist (#26): impression.generate runs BEFORE the radiologist signs,
using aiFindings only, and the draft is offered into the RIS via write_presign_impression_activity.

Covers: the pre-sign call+write happen ahead of the AWAITING_RADIOLOGIST gate; a missing
fhirServiceRequestId skips the RIS write (nowhere to attach it); and a failure in either activity
is advisory/best-effort -- it must never strand the human-gated read that follows.
Skipped unless temporalio is installed.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402

STUDY_CONTEXT_WITH_ORDER = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_presign_test",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc123", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"fhirServiceRequestId": "ServiceRequest/sr-1"},
    "meta": {"traceId": "trc_x", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}

STUDY_CONTEXT_NO_ORDER = {
    **STUDY_CONTEXT_WITH_ORDER,
    "workflowId": "wf_presign_no_order",
    "order": {},
}

# Shared control/record state for the mock activities (reset per scenario).
_STATE: dict = {}


def _reset() -> None:
    _STATE.clear()
    _STATE["impression_calls"] = []
    _STATE["write_calls"] = []
    _STATE["write_should_fail"] = False
    _STATE["write_should_refuse"] = False   # transport guard refusal (config error, not outage)
    _STATE["impression_should_fail"] = False
    # A real (M3) tool ran. The pre-sign write is GATED on this (#26): with the v1 registry every
    # finding is STUBBED, the impression falls through to its constant "no acute findings" text,
    # and writing that into a chart ahead of the read is what the gate exists to prevent. Tests
    # that want the v1 reality flip this to STUBBED.
    _STATE["finding_status"] = "COMPLETE"


@activity.defn(name="call_agent_skill_activity")
async def mock_call_agent(agent: str, skill_id: str, payload: dict) -> dict:
    if skill_id == "interpretation.runTools":
        return {"schemaVersion": "1.0.0", "workflowId": payload["studyContext"]["workflowId"],
                "findings": [{"toolId": "cxr-detect", "label": "pneumothorax",
                              "status": _STATE["finding_status"]}],
                "overallStatus": _STATE["finding_status"], "agentVersion": "mock",
                "ranAt": "2026-06-26T00:00:00Z"}
    if skill_id == "impression.generate":
        _STATE["impression_calls"].append(payload)
        if _STATE["impression_should_fail"]:
            raise RuntimeError("impression-generation down")
        return {
            "schemaVersion": "1.0.0", "workflowId": payload["studyContext"]["workflowId"],
            "impressionText": "Pre-sign draft: no acute findings.",
            "agentVersion": "mock", "generatedAt": "2026-06-26T00:00:00Z",
        }
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


@activity.defn(name="write_presign_impression_activity")
async def mock_write_presign(service_request_ref: str, patient_ref: str, impression_text: str) -> str:
    _STATE["write_calls"].append((service_request_ref, patient_ref, impression_text))
    if _STATE["write_should_refuse"]:
        from radagent_common.fhir_client import InsecureWriteTransportError
        raise InsecureWriteTransportError("refusing a fhir2 write over plaintext HTTP")
    if _STATE["write_should_fail"]:
        raise RuntimeError("fhir2 write down")
    return "presign-draft-1"


@activity.defn(name="escalate_activity")
async def mock_escalate(workflow_id: str, reason: str) -> None:
    pass


def _worker(env: WorkflowEnvironment) -> Worker:
    return Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[StudyWorkflow],
        activities=[mock_call_agent, mock_publish, mock_publish_findings, mock_publish_state,
                    mock_write_presign, mock_escalate],
        max_cached_workflows=0,
    )


async def _wait_state(handle, target: str, tries: int = 200) -> None:
    for _ in range(tries):
        if await handle.query(StudyWorkflow.current_state) == target:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow never reached {target}")


def test_presign_draft_generated_and_written_before_the_radiologist_gate():
    """The pre-sign draft is generated from aiFindings only (no report) and written to the RIS
    BEFORE the workflow ever parks at AWAITING_RADIOLOGIST -- i.e. while the radiologist is still
    reading, not after sign-off."""
    async def scenario():
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT_WITH_ORDER,
                    id="wf-presign-happy", task_queue=TASK_QUEUE,
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                # The pre-sign call/write already ran to get here -- the gate wait is next.
                assert len(_STATE["impression_calls"]) == 1
                presign_payload = _STATE["impression_calls"][0]
                assert "report" not in presign_payload  # pre-sign: no report exists yet
                assert _STATE["write_calls"] == [
                    ("ServiceRequest/sr-1", "Patient/1", "Pre-sign draft: no acute findings.")
                ]
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"
        # Post-sign impression.generate runs too (the v1 safety-net) -- one more call, with report.
        assert len(_STATE["impression_calls"]) == 2
        assert "report" in _STATE["impression_calls"][1]
    asyncio.run(scenario())


def test_presign_write_skipped_without_a_service_request_ref():
    """No fhirServiceRequestId yet (order not resolved) -> nowhere in the RIS to attach the draft,
    so the write is skipped -- but the pre-sign draft is still generated and the read proceeds."""
    async def scenario():
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT_NO_ORDER,
                    id="wf-presign-no-order", task_queue=TASK_QUEUE,
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                assert len(_STATE["impression_calls"]) == 1
                assert _STATE["write_calls"] == []
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"
    asyncio.run(scenario())


def test_presign_write_failure_does_not_strand_the_read():
    """The RIS write is advisory/best-effort: even after retries it keeps failing, the workflow
    must still reach the radiologist gate and complete -- not hang or fail the study."""
    async def scenario():
        _reset()
        _STATE["write_should_fail"] = True
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT_WITH_ORDER,
                    id="wf-presign-write-fail", task_queue=TASK_QUEUE,
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                # Transient failure keeps its bounded retries: attempted, retried, gave up.
                assert len(_STATE["write_calls"]) == 3
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"  # not stranded despite the persistent write failure
    asyncio.run(scenario())


def test_a_transport_guard_refusal_fails_fast_with_no_retries():
    """InsecureWriteTransportError is a CONFIG error (#30): plaintext http to a non-loopback
    fhir2 without the opt-in refuses identically on every attempt. The write policy declares it
    non-retryable, so the draft is skipped after ONE attempt -- no retry budget burned re-asking
    a question with a fixed answer -- and the human read is never stranded."""
    async def scenario():
        _reset()
        _STATE["write_should_refuse"] = True
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT_WITH_ORDER,
                    id="wf-presign-guard-refusal", task_queue=TASK_QUEUE,
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                assert len(_STATE["write_calls"]) == 1   # ONE attempt: refused, not retried
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"        # best-effort: the read went on

    asyncio.run(scenario())


def test_presign_generate_failure_does_not_strand_the_read():
    """impression.generate itself fails pre-sign (agent down) -- no draft, no write attempt, but
    the read still proceeds (the post-sign safety-net covers the study regardless)."""
    async def scenario():
        _reset()
        _STATE["impression_should_fail"] = True
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT_WITH_ORDER,
                    id="wf-presign-generate-fail", task_queue=TASK_QUEUE,
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                assert _STATE["write_calls"] == []  # never reached: impression.generate failed first
                # The agent recovers before sign-off: post-sign impression.generate (unbounded
                # retry, by design -- it's the safety-net the verify step depends on) must succeed
                # here or it retries forever against a mock that never comes back up.
                _STATE["impression_should_fail"] = False
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"
    asyncio.run(scenario())


# --- #26: the write is gated on the draft actually knowing something -----------------

def test_stubbed_findings_write_nothing_into_the_chart():
    """THE gate (#26, a hard condition of the amended locked decision).

    In v1 the Interpretation registry returns every finding STUBBED with an empty label. The
    impression then has nothing to work from and falls through to its constant fallback -- so
    writing the draft would put a fixed NEGATIVE impression ("No acute findings identified..."),
    authored by nobody, into EVERY patient's chart before the radiologist has read anything. That
    is exactly the automation bias the post-sign rule existed to prevent.

    The feature therefore stays inert until the real tools land in M3, and lights up on its own
    when they do -- no flag to remember to flip.
    """
    async def scenario():
        _reset()
        _STATE["finding_status"] = "STUBBED"          # the v1 reality
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT_WITH_ORDER,
                    id="wf-presign-stubbed", task_queue=TASK_QUEUE)
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                await handle.result()

        # Nothing was drafted and nothing was written. The post-sign impression still runs.
        assert _STATE["write_calls"] == []
        presign_calls = [p for p in _STATE["impression_calls"] if "report" not in p]
        assert presign_calls == [], "impression.generate must not even be asked pre-sign"
    asyncio.run(scenario())


def test_a_complete_finding_lets_the_draft_through():
    """The flip side: once a real tool produces a COMPLETE finding, the draft is offered."""
    async def scenario():
        _reset()
        _STATE["finding_status"] = "COMPLETE"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT_WITH_ORDER,
                    id="wf-presign-complete", task_queue=TASK_QUEUE)
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                await handle.result()

        assert len(_STATE["write_calls"]) == 1
    asyncio.run(scenario())
