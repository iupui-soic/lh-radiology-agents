"""#139 step 2: after a critical result is ACKNOWLEDGED, check that someone acted on it.

Acknowledgement is not action. When FOLLOWUP_CHECK_HOURS is set, an acknowledged critical result
schedules one FollowUpCheckWorkflow: it waits the window, looks in fhir2 for a follow-up order,
encounter or procedure, and reminds the ordering physician once if there is none. Off (the
default), nothing changes. Driven on a real Temporal (time-skipping), because the timer and the
child workflow outliving the study are the things under test.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio import activity  # noqa: E402
from temporalio.client import WorkflowExecutionStatus  # noqa: E402
from temporalio.exceptions import ApplicationError  # noqa: E402
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

import orchestrator.activities as activities  # noqa: E402
from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import (  # noqa: E402
    FOLLOWUP_WORKFLOW_SUFFIX,
    FollowUpCheckWorkflow,
    StudyWorkflow,
)

STUDY_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_fu",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/p-1"},
    "order": {"fhirServiceRequestId": "ServiceRequest/sr-own"},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}
# A window of under two seconds. The time-skipping server does not reliably fast-forward a timer in
# a workflow the test did not start once the study has finished, so these tests wait in real time
# instead; the production window is whatever FOLLOWUP_CHECK_HOURS says.
WINDOW_HOURS = 0.0005
ON = {"windowHours": WINDOW_HOURS, "evidenceTypes": ["ServiceRequest", "Encounter", "Procedure"],
      "reminderChannels": ["in-app"]}
_STATE: dict = {}


def _reset(script: dict) -> None:
    _STATE.clear()
    _STATE.update(script=script, policy_loads=0, checks=[], pages=[])


def _deadline_in(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


@activity.defn(name="call_agent_skill_activity")
async def _mock_call(agent: str, skill_id: str, payload: dict) -> dict:
    script = _STATE["script"]
    if skill_id == "report.verify":
        return {"verificationStatus": "PASS", "requiresHumanReview": False, "issues": []}
    if skill_id == "triage.score":
        return {"priorityTier": "ROUTINE", "priorityScore": 50}
    if skill_id == "comms.dispatch":
        if not script.get("critical", True):
            return {"dispatchStatus": "SENT", "channelResults": []}
        return {"dispatchStatus": "SENT", "acrCategory": "Cat1", "communicationId": "c1",
                "taskId": "t1", "deadline": _deadline_in(60), "recipient": "Practitioner/ordering"}
    if skill_id == "comms.checkAck":
        if script.get("acked", True):
            return {"taskId": payload["taskId"], "ackStatus": "COMPLETED",
                    "deadline": _deadline_in(0), "overdue": False}
        return {"taskId": payload["taskId"], "ackStatus": "OVERDUE",
                "deadline": _deadline_in(0), "overdue": True}
    if skill_id == "comms.escalate":
        return {"escalated": False, "reason": "nobody is on call"}
    return {"ok": True}


@activity.defn(name="publish_priority_activity")
async def _mock_publish(workflow_id: str, study_instance_uid: str, triage: dict) -> None:
    return None


@activity.defn(name="publish_findings_activity")
async def _mock_publish_findings(workflow_id: str, study_instance_uid: str, ai_result: dict) -> None:
    return None


@activity.defn(name="publish_state_activity")
async def _mock_publish_state(workflow_id: str, study_instance_uid: str, read_state: str,
                              changed_at: str) -> None:
    return None


@activity.defn(name="escalate_activity")
async def _mock_escalate(workflow_id: str, reason: str, escalation: dict | None = None) -> None:
    _STATE["pages"].append({"workflowId": workflow_id, "reason": reason, "escalation": escalation})


@activity.defn(name="load_escalation_policy_activity")
async def _mock_load_policy(tier: str | None) -> list[dict]:
    return [{"level": 1, "afterMinutes": 240, "targetRole": "reading-radiologist",
             "channels": ["in-app"], "urgency": "routine"}]


@activity.defn(name="load_followup_policy_activity")
async def _mock_followup_policy() -> dict:
    _STATE["policy_loads"] += 1
    return _STATE["script"].get("policy", {})


@activity.defn(name="check_followup_activity")
async def _mock_check(patient_ref: str, own_order_ref: str | None, since_iso: str,
                      evidence_types: list[str]) -> dict:
    _STATE["checks"].append({"patient": patient_ref, "order": own_order_ref, "since": since_iso,
                             "types": evidence_types})
    if _STATE["script"].get("check_fails"):
        raise ApplicationError("fhir2 down", non_retryable=True)
    return {"evidence": _STATE["script"].get("evidence", []), "searched": evidence_types}


_ACTIVITIES = [_mock_call, _mock_publish, _mock_publish_findings, _mock_publish_state,
               _mock_escalate, _mock_load_policy, _mock_followup_policy, _mock_check]


async def _run(script: dict, wf_id: str, context: dict = STUDY_CONTEXT):
    """(study result, follow-up result or None)."""
    _reset(script)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=TASK_QUEUE,
                          workflows=[StudyWorkflow, FollowUpCheckWorkflow], activities=_ACTIVITIES):
            handle = await env.client.start_workflow(
                StudyWorkflow.run, {**context, "workflowId": wf_id}, id=wf_id, task_queue=TASK_QUEUE)
            await handle.signal(StudyWorkflow.report_finalized,
                                {"diagnosticReportId": "DiagnosticReport/1"})
            result = await handle.result()
            follow = None
            if (result.get("followUp") or {}).get("status") == "SCHEDULED":
                # The study has already returned; the check runs on in its own workflow. Wait for it
                # to close, then read its result off its history: handle.result() on a handle this
                # test did not start intermittently fails on the time-skipping server ("No
                # completion event found") even after the workflow has completed.
                child = env.client.get_workflow_handle(result["followUp"]["workflowId"])
                for _ in range(300):
                    if (await child.describe()).status != WorkflowExecutionStatus.RUNNING:
                        break
                    await asyncio.sleep(0.05)
                history = await child.fetch_history()
                done = [e.workflow_execution_completed_event_attributes for e in history.events
                        if e.HasField("workflow_execution_completed_event_attributes")]
                assert done, "the follow-up check did not complete"
                follow = (await env.client.data_converter.decode(done[-1].result.payloads))[0]
            return result, follow


def _follow_up_pages() -> list[dict]:
    return [p for p in _STATE["pages"] if (p["escalation"] or {}).get("kind") == "follow-up"]


# --- off by default -----------------------------------------------------------------------------


def test_off_by_default_nothing_is_scheduled():
    result, follow = asyncio.run(_run({"policy": {}}, "wf-fu-off"))
    assert result["finalState"] == "ARCHIVED"
    assert result["ack"]["ackStatus"] == "COMPLETED"
    assert result["followUp"] == {}
    assert follow is None
    assert _STATE["checks"] == []
    assert _follow_up_pages() == []


# --- on: the three outcomes ------------------------------------------------------------------


def test_follow_up_found_sends_no_reminder():
    result, follow = asyncio.run(_run({"policy": ON, "evidence": ["ServiceRequest/ct-1"]}, "wf-fu-found"))
    assert result["followUp"] == {"status": "SCHEDULED", "workflowId": f"wf-fu-found{FOLLOWUP_WORKFLOW_SUFFIX}",
                                  "windowHours": WINDOW_HOURS}
    assert follow["status"] == "FOUND"
    assert follow["evidence"] == ["ServiceRequest/ct-1"]
    assert _follow_up_pages() == []


def test_nothing_found_reminds_the_ordering_physician_once():
    _, follow = asyncio.run(_run({"policy": ON, "evidence": []}, "wf-fu-none"))
    assert follow["status"] == "NOT_FOUND"
    assert follow["reminded"] is True
    pages = _follow_up_pages()
    assert len(pages) == 1, "one reminder, never a chase"
    page = pages[0]
    assert page["workflowId"] == "wf-fu-none"  # about the study, not the check
    assert page["escalation"]["targetRole"] == "ordering-physician"
    assert page["escalation"]["channels"] == ["in-app"]
    assert f"within {WINDOW_HOURS:g} h" in page["reason"] and "#139" in page["reason"]


def test_a_check_that_cannot_reach_fhir2_sends_no_reminder():
    """"Could not look" is not "nothing found": a reminder on an outage would teach physicians
    that the reminders are noise."""
    _, follow = asyncio.run(_run({"policy": ON, "check_fails": True}, "wf-fu-fail"))
    assert follow["status"] == "CHECK_FAILED"
    assert _follow_up_pages() == []


# --- what it looks at, and when ----------------------------------------------------------------


def test_the_check_waits_the_whole_window_and_looks_from_the_communication():
    _, follow = asyncio.run(_run({"policy": ON, "evidence": []}, "wf-fu-window"))
    since = datetime.fromisoformat(follow["since"])
    started = datetime.fromisoformat(follow["startedAt"])
    checked = datetime.fromisoformat(follow["checkedAt"])
    # Measured from the check's OWN start: the ack loop already waited past the Cat1 deadline, so
    # "checked - since" would hold even if the window were never waited.
    assert checked - started >= timedelta(hours=WINDOW_HOURS)
    assert since <= started, "the window looks back to when the result was communicated"
    check = _STATE["checks"][0]
    assert check == {"patient": "Patient/p-1", "order": "ServiceRequest/sr-own",
                     "since": follow["since"], "types": ON["evidenceTypes"]}


def test_an_unacknowledged_result_is_not_followed_up():
    """The ack loop has already escalated it; a follow-up check on top would page twice."""
    result, follow = asyncio.run(_run({"policy": ON, "acked": False}, "wf-fu-unacked"))
    assert result["ack"]["ackStatus"] == "UNACKNOWLEDGED"
    assert result["followUp"] == {}
    assert _STATE["policy_loads"] == 0
    assert follow is None


def test_a_routine_result_is_not_followed_up():
    result, follow = asyncio.run(_run({"policy": ON, "critical": False}, "wf-fu-routine"))
    assert result["followUp"] == {}
    assert _STATE["policy_loads"] == 0


def test_no_patient_reference_skips_the_check_honestly():
    context = {k: v for k, v in STUDY_CONTEXT.items() if k != "patient"}
    result, follow = asyncio.run(_run({"policy": ON}, "wf-fu-nopatient", context))
    assert result["followUp"] == {"status": "SKIPPED", "reason": "no patient reference to check"}
    assert follow is None


# --- the activities ----------------------------------------------------------------------------


def _policy(monkeypatch, **env) -> dict:
    for name in ("FOLLOWUP_CHECK_HOURS", "FOLLOWUP_EVIDENCE_TYPES", "FOLLOWUP_REMINDER_CHANNELS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return asyncio.run(ActivityEnvironment().run(activities.load_followup_policy_activity))


def test_policy_is_off_unless_a_sane_window_is_set(monkeypatch):
    for value in (None, "", "abc", "0", "-3", "nan", "inf", str(24 * 30 + 1)):
        env = {} if value is None else {"FOLLOWUP_CHECK_HOURS": value}
        assert _policy(monkeypatch, **env) == {}, value


def test_policy_defaults_to_all_three_types_and_in_app(monkeypatch):
    assert _policy(monkeypatch, FOLLOWUP_CHECK_HOURS="24") == {
        "windowHours": 24.0, "evidenceTypes": ["ServiceRequest", "Encounter", "Procedure"],
        "reminderChannels": ["in-app"]}


def test_policy_keeps_only_known_types_and_channels(monkeypatch):
    out = _policy(monkeypatch, FOLLOWUP_CHECK_HOURS="48",
                  FOLLOWUP_EVIDENCE_TYPES="ServiceRequest, Observation,Encounter",
                  FOLLOWUP_REMINDER_CHANNELS="email,fax")
    assert out["evidenceTypes"] == ["ServiceRequest", "Encounter"]
    assert out["reminderChannels"] == ["email"]
    assert _policy(monkeypatch, FOLLOWUP_CHECK_HOURS="48", FOLLOWUP_EVIDENCE_TYPES="Observation") == {}
    assert _policy(monkeypatch, FOLLOWUP_CHECK_HOURS="48",
                   FOLLOWUP_REMINDER_CHANNELS="fax")["reminderChannels"] == ["in-app"]


def test_the_check_excludes_the_study_own_order_whatever_its_form(monkeypatch):
    seen = []

    class _Fhir:
        def __init__(self, *a, **kw):
            pass

        async def search_follow_up_since(self, patient, since, types, exclude):
            seen.append(exclude)
            return []

    monkeypatch.setattr(activities, "Fhir2Client", _Fhir)
    env = ActivityEnvironment()
    for order in ("ServiceRequest/sr-own", "sr-own"):
        asyncio.run(env.run(activities.check_followup_activity, "Patient/p-1", order,
                            "2026-06-27T10:00:00+00:00", ["ServiceRequest"]))
    asyncio.run(env.run(activities.check_followup_activity, "Patient/p-1", None,
                        "2026-06-27T10:00:00+00:00", ["ServiceRequest"]))
    assert seen == [("ServiceRequest/sr-own",), ("ServiceRequest/sr-own",), ()]


def test_a_reminder_never_carries_the_signoff_override_link(monkeypatch):
    """The override link releases a sign-off gate. A follow-up reminder is about an archived study
    with no gate, so the link would point the physician at the wrong action. The sign-off ladder's
    own pages keep it."""
    sent = []

    async def _capture(base_url, skill_id, payload):
        sent.append(payload)
        return {"dispatchStatus": "SENT"}

    monkeypatch.setattr(activities, "call_agent_skill", _capture)
    monkeypatch.setattr(activities, "SIGNOFF_OVERRIDE_URL", "https://h/ingress/signoff/{workflowId}/override")
    rung = {"level": 1, "targetRole": "ordering-physician", "channels": ["in-app"], "urgency": "urgent",
            "attempt": 1}
    env = ActivityEnvironment()
    asyncio.run(env.run(activities.escalate_activity, "wf-1", "reminder", {**rung, "kind": "follow-up"}))
    asyncio.run(env.run(activities.escalate_activity, "wf-1", "ladder", rung))
    assert "overrideUrl" not in sent[0]["escalation"]
    assert "kind" not in sent[0]["escalation"], "the contract's slice has no such field"
    assert sent[1]["escalation"]["overrideUrl"] == "https://h/ingress/signoff/wf-1/override"
