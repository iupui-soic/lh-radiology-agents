"""Tier-dependent sign-off gate timing + on-call paging (#23, ladder-wired by #29).

Covers:
- the legacy tier -> timeout mapping (unit) — still the gate's fallback when the escalation
  policy cannot be loaded;
- that the gate uses the REAL escalation policy (integration): a STAT study reaches the
  sign-off gate, rung 1 fires at the tier's 60m (asserted from history — the same value the
  pre-#29 hardcoded timeout used), the fired rung reaches escalate_activity, and the ack
  releases the gate; a failed page does not strand it;
- that escalate_activity's legacy flat page (escalation=None) still dispatches comms.dispatch
  marked critical, which the Communications Agent routes to the on-call pager (unit).
Skipped unless temporalio is installed.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment, ActivityEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

import orchestrator.activities as activities  # noqa: E402
from orchestrator.activities import load_escalation_policy_activity  # noqa: E402
from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import (  # noqa: E402
    StudyWorkflow,
    signoff_timeout_for,
    SIGNOFF_GATE_TIMEOUT_DEFAULT,
)


# --- unit: the legacy tier -> timeout map (the gate's policy-unavailable fallback) --

def test_timeout_is_tier_dependent_and_ordered():
    stat = signoff_timeout_for("STAT")
    urgent = signoff_timeout_for("URGENT")
    routine = signoff_timeout_for("ROUTINE")
    assert (stat, urgent, routine) == (timedelta(hours=1), timedelta(hours=2), timedelta(hours=4))
    # STAT reads escalate fastest, ROUTINE slowest.
    assert stat < urgent < routine


def test_unknown_or_missing_tier_falls_back_to_default():
    assert SIGNOFF_GATE_TIMEOUT_DEFAULT == timedelta(hours=4)
    for tier in (None, "", "WEIRD"):
        assert signoff_timeout_for(tier) == SIGNOFF_GATE_TIMEOUT_DEFAULT


# --- integration: the gate climbs the REAL policy's STAT ladder --------------------

_ESCALATIONS: list = []


@activity.defn(name="call_agent_skill_activity")
async def _mock_call(agent: str, skill_id: str, payload: dict) -> dict:
    if skill_id == "report.verify":
        # First read needs human review (-> sign-off gate); after the ack, re-verify PASSes.
        _mock_call.n += 1  # type: ignore[attr-defined]
        return {"verificationStatus": "FAIL" if _mock_call.n == 1 else "PASS",  # type: ignore[attr-defined]
                "requiresHumanReview": _mock_call.n == 1, "issues": []}  # type: ignore[attr-defined]
    if skill_id == "triage.score":
        return {"priorityTier": "STAT", "priorityScore": 95}
    return {"ok": True}


@activity.defn(name="publish_priority_activity")
async def _mock_publish(workflow_id: str, study_instance_uid: str, triage: dict) -> None:
    return None

@activity.defn(name="publish_findings_activity")
async def _mock_publish_findings(workflow_id: str, study_instance_uid: str, ai_result: dict) -> None:
    """Mock for #74 publish_findings_activity — never-raises like the production version."""
    return None


@activity.defn(name="publish_state_activity")
async def _mock_publish_state(
    workflow_id: str, study_instance_uid: str, read_state: str, changed_at: str,
) -> None:
    """Mock for #108 publish_state_activity -- never-raises like the production version."""
    return None


@activity.defn(name="escalate_activity")
async def _mock_escalate(workflow_id: str, reason: str, escalation: dict | None = None) -> None:
    _ESCALATIONS.append((workflow_id, reason, escalation))


STUDY_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_tier",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"}, "order": {},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


async def _wait_state(handle, target: str, tries: int = 200) -> None:
    for _ in range(tries):
        if await handle.query(StudyWorkflow.current_state) == target:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow never reached {target}")


async def _wait_for(pred, tries: int = 600) -> None:
    """Real-time poll until pred() — activity completions land shortly after a fired timer."""
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


def test_stat_study_pages_rung1_at_the_tier_hour_and_ack_releases_the_gate():
    """A STAT study reaches the sign-off gate; the REAL escalation policy (repo yaml) is loaded,
    and rung 1 fires at the STAT 60m — the same value the pre-#29 hardcoded timeout used, so
    the ladder wiring is behaviour-preserving at rung 1. We assert the 1h timer from history
    (direct proof of the tier), the fired rung's shape, and that the ack releases the gate.

    Time skipping is locked except inside env.sleep()/result-await, so advancing exactly 61m
    fires rung 1 and nothing else — the escalation count is deterministic.
    """
    async def scenario():
        _mock_call.n = 0  # type: ignore[attr-defined]
        _ESCALATIONS.clear()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[_mock_call, _mock_publish, _mock_publish_findings,
                                      _mock_publish_state, _mock_escalate,
                                          load_escalation_policy_activity]):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-tier-stat", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                await _wait_state(handle, "AWAITING_SIGNOFF")
                await env.sleep(timedelta(minutes=61))          # past rung 1 (60m), before rung 2 (90m)
                await _wait_for(lambda: len(_ESCALATIONS) >= 1)
                await handle.signal(StudyWorkflow.signoff_acknowledged, {"ackBy": "Practitioner/9"})
                result = await handle.result()
                timers = [
                    e.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
                    async for e in handle.fetch_history_events()
                    if e.HasField("timer_started_event_attributes")
                ]
        assert result["finalState"] == "ARCHIVED"
        assert len(_ESCALATIONS) == 1                       # rung 1 fired; rung 2 (90m) never reached
        wf, reason, rung = _ESCALATIONS[0]
        assert wf == "wf_tier"
        assert "sign-off" in reason
        assert rung["level"] == 1
        assert rung["targetRole"] == "reading-radiologist"  # the real policy's STAT rung 1
        assert timers[0] == timedelta(hours=1)              # STAT tier, not the ROUTINE default
    asyncio.run(scenario())


_FAILED_ESCALATE_ATTEMPTS: list = []


@activity.defn(name="escalate_activity")
async def _boom_escalate(workflow_id: str, reason: str, escalation: dict | None = None) -> dict:
    _FAILED_ESCALATE_ATTEMPTS.append(workflow_id)
    raise RuntimeError("comms down")


def test_escalation_failure_does_not_strand_the_gate():
    """Best-effort paging: if escalate_activity keeps failing (3 bounded attempts), the gate
    must NOT be stranded — it keeps holding for the ack, which still releases it."""
    async def scenario():
        _mock_call.n = 0  # type: ignore[attr-defined]
        _FAILED_ESCALATE_ATTEMPTS.clear()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[_mock_call, _mock_publish, _mock_publish_findings, _mock_publish_state, _boom_escalate,
                                          load_escalation_policy_activity]):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-tier-boom", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                await _wait_state(handle, "AWAITING_SIGNOFF")
                await env.sleep(timedelta(minutes=61))               # rung 1 fires...
                await _wait_for(lambda: len(_FAILED_ESCALATE_ATTEMPTS) >= 3)  # ...and fails its retries
                await handle.signal(StudyWorkflow.signoff_acknowledged, {"ackBy": "Practitioner/9"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"      # gate not stranded despite the failed paging
        assert len(_FAILED_ESCALATE_ATTEMPTS) == 3     # bounded retries, then the ladder moved on
    asyncio.run(scenario())


# --- unit: the legacy flat page still trips the on-call pager route (#23) -----------

def test_escalate_activity_legacy_flat_page_pages_oncall_via_comms(monkeypatch):
    """escalation=None is the policy-unavailable fallback: the dispatch is marked critical
    (verificationStatus=FAIL), which the Communications Agent routes to the on-call pager
    (agents/communications/handler._is_critical) — so even a config disaster reaches a human."""
    captured: dict = {}

    async def _fake_dispatch(base_url, skill_id, payload):
        captured.update(base_url=base_url, skill_id=skill_id, payload=payload)
        return {
            "schemaVersion": "1.0.0",
            "workflowId": payload["studyContext"]["workflowId"],
            "dispatchStatus": "SENT",
            "channelResults": [{"channel": "ehr-inbox", "status": "SENT"},
                               {"channel": "oncall-pager", "status": "SENT"}],
            "agentVersion": "0.1.0",
            "dispatchedAt": "2026-07-02T00:00:00Z",
        }

    # escalate_activity resolves call_agent_skill as a module global -> patch it there.
    monkeypatch.setattr(activities, "call_agent_skill", _fake_dispatch)

    result = asyncio.run(ActivityEnvironment().run(
        activities.escalate_activity, "wf_esc", "sign-off gate timed out awaiting radiologist"))

    assert captured["skill_id"] == "comms.dispatch"
    assert captured["payload"]["studyContext"]["workflowId"] == "wf_esc"
    assert captured["payload"]["verification"]["verificationStatus"] == "FAIL"  # -> pager route
    assert "escalation" not in captured["payload"]
    assert "communications" in captured["base_url"]
    assert result["dispatchStatus"] == "SENT"
    assert any(c["channel"] == "oncall-pager" for c in result["channelResults"])
