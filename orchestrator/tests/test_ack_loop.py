"""The critical-results ack loop (#52): the orchestrator watches the clock CritCom opened.

The agent records that we told someone and by when they must answer -- and then stops. It has no
self-firing timer. So without this loop a Cat1 finding carries a 60-minute deadline that nothing is
waiting on: the Task sits `requested` in the ledger forever, and the on-call provider is never told
that the ordering physician never answered. The durable wait lives here because Temporal timers
survive a worker restart; a timer inside the agent would not.

Driven on a real Temporal (time-skipping), because the thing under test IS the timer: these tests
assert that the workflow does not check the ack BEFORE the deadline, and does after -- which a
mocked clock cannot show. Skipped unless temporalio is installed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import (  # noqa: E402
    ACK_ESCALATION_CAP,
    ACK_GRACE,
    ACK_LOOP_CAP,
    StudyWorkflow,
)

STUDY_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_ack",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"}, "order": {},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}

ACK_MINUTES = 60           # a Cat1 window
_STATE: dict = {}


def _reset(script: dict) -> None:
    """`script` decides what the fake CritCom does; _STATE records what the workflow asked it."""
    _STATE.clear()
    _STATE["calls"] = []           # (skill_id, taskId) in order
    _STATE["script"] = script
    _STATE["acked"] = script.get("acked_after_escalations")


def _deadline_in(minutes: int) -> str:
    """An ISO deadline `minutes` from now, exactly as the agent reports one."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


@activity.defn(name="call_agent_skill_activity")
async def _mock_call(agent: str, skill_id: str, payload: dict) -> dict:
    if skill_id == "report.verify":
        return {"verificationStatus": "PASS", "requiresHumanReview": False, "issues": []}
    if skill_id == "triage.score":
        return {"priorityTier": "ROUTINE", "priorityScore": 50}

    script = _STATE["script"]

    if skill_id == "comms.dispatch":
        # A routine result opens no clock -- no taskId, no deadline. That is the `critical: False`
        # case, and the loop must not run at all.
        if not script.get("critical"):
            return {"dispatchStatus": "SENT", "channelResults": []}
        _STATE["dispatch_deadline"] = _deadline_in(ACK_MINUTES)
        return {"dispatchStatus": "SENT", "acrCategory": "Cat1", "communicationId": "c1",
                "taskId": "t1", "deadline": _STATE["dispatch_deadline"],
                "recipient": "Practitioner/ordering"}

    if skill_id == "comms.checkAck":
        task_id = payload["taskId"]
        _STATE["calls"].append(("comms.checkAck", task_id))
        if script.get("never_overdue"):
            # A ledger whose clock trails ours far enough that the Task never reads overdue, and
            # whose re-read reports the SAME deadline every time.
            return {"taskId": task_id, "ackStatus": "REQUESTED",
                    "deadline": _STATE["dispatch_deadline"], "overdue": False}
        escalations = sum(1 for s, _ in _STATE["calls"] if s == "comms.escalate")
        if _STATE["acked"] is not None and escalations >= _STATE["acked"]:
            return {"taskId": task_id, "ackStatus": "COMPLETED",
                    "deadline": _deadline_in(0), "overdue": False}
        return {"taskId": task_id, "ackStatus": "OVERDUE",
                "deadline": _deadline_in(0), "overdue": True}

    if skill_id == "comms.escalate":
        task_id = payload["taskId"]
        _STATE["calls"].append(("comms.escalate", task_id))
        n = sum(1 for s, _ in _STATE["calls"] if s == "comms.escalate")
        if not script.get("escalatable", True):
            return {"escalated": False, "reason": "nobody is on call"}
        return {"escalated": True, "newCommunicationId": f"c{n + 1}", "newTaskId": f"t{n + 1}",
                "newDeadline": _deadline_in(ACK_MINUTES)}

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
    return None


@activity.defn(name="load_escalation_policy_activity")
async def _mock_load_policy(tier: str | None) -> list[dict]:
    return [{"level": 1, "afterMinutes": 240, "targetRole": "reading-radiologist",
             "channels": ["in-app"], "urgency": "routine"}]


async def _run(script: dict, wf_id: str, want_history: bool = False):
    _reset(script)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                          activities=[_mock_call, _mock_publish, _mock_publish_findings,
                                      _mock_publish_state, _mock_escalate,
                                      _mock_load_policy]):
            handle = await env.client.start_workflow(
                StudyWorkflow.run, STUDY_CONTEXT, id=wf_id, task_queue=TASK_QUEUE)
            await handle.signal(StudyWorkflow.report_finalized,
                                {"diagnosticReportId": "DiagnosticReport/1"})
            result = await handle.result()
            if want_history:
                return result, await handle.fetch_history()
            return result


def _skills() -> list[str]:
    return [s for s, _ in _STATE["calls"]]


# --- the ordering physician answers -------------------------------------------------


def test_an_acknowledged_result_escalates_nobody():
    """The whole point of the ack loop is that it usually does nothing: the physician answers, the
    Task closes, and no one is woken. One check, zero escalations."""
    result = asyncio.run(_run({"critical": True, "acked_after_escalations": 0}, "wf-ack-ok"))

    assert result["finalState"] == "ARCHIVED"
    assert result["ack"] == {"ackStatus": "COMPLETED", "taskId": "t1", "escalations": 0}
    assert _skills() == ["comms.checkAck"]


# --- nobody answers -> escalate to on-call -------------------------------------------


def test_an_unacknowledged_result_escalates_and_then_watches_the_NEW_clock():
    """Nobody answered by the deadline, so the result escalates to on-call -- and the loop follows
    the FRESH Task the escalation opened. Watching the old (now FAILED) Task would mean the
    escalation is never itself checked: we would page on-call and then stop caring."""
    result = asyncio.run(_run({"critical": True, "acked_after_escalations": 1}, "wf-ack-esc"))

    assert result["ack"] == {"ackStatus": "COMPLETED", "taskId": "t2", "escalations": 1}
    assert _STATE["calls"] == [
        ("comms.checkAck", "t1"),      # the ordering physician's clock ran out
        ("comms.escalate", "t1"),      # -> escalate that task
        ("comms.checkAck", "t2"),      # -> and now watch on-call's NEW task
    ]


def test_the_chase_is_bounded_and_the_study_still_archives():
    """Nobody ever answers. The loop must NOT page forever (each escalation opens a fresh loop, so
    an uncapped chase never terminates and grows history without bound) and must NOT hang the study.
    It escalates up to the cap, records the result as unacknowledged, and archives."""
    result = asyncio.run(_run({"critical": True, "acked_after_escalations": None}, "wf-ack-cap"))

    assert result["finalState"] == "ARCHIVED"            # archived, not stranded
    assert result["ack"]["ackStatus"] == "UNACKNOWLEDGED"
    assert result["ack"]["escalations"] == ACK_ESCALATION_CAP
    assert _skills().count("comms.escalate") == ACK_ESCALATION_CAP


def test_an_unescalatable_result_is_reported_not_spun_on():
    """Nobody is on call. The agent says so honestly (escalated=False) rather than claiming a page
    it never sent -- so the loop stops immediately instead of re-escalating into an empty directory,
    and the study archives carrying the reason."""
    result = asyncio.run(
        _run({"critical": True, "acked_after_escalations": None, "escalatable": False},
             "wf-ack-noone"))

    assert result["ack"]["ackStatus"] == "UNACKNOWLEDGED"
    assert result["ack"]["escalations"] == 1             # tried once, told nobody's there, stopped
    assert "on call" in result["ack"]["reason"]
    assert _skills().count("comms.escalate") == 1


# --- the loop must stay out of the way ------------------------------------------------


def test_the_wait_is_a_durable_temporal_timer_not_a_poll():
    """The clock must survive a worker restart, which is the entire reason it lives in the
    orchestrator and not in the agent. So assert the mechanism, not just the outcome: the workflow
    records a real TIMER for the ack window. A polling loop would pass every other test in this
    file and lose the deadline on the next deploy.

    Time-skipping fires the timer instantly, so without this the suite could not tell a 60-minute
    durable wait from no wait at all.
    """
    _, history = asyncio.run(
        _run({"critical": True, "acked_after_escalations": 0}, "wf-ack-timer", want_history=True))

    timers = [e.timer_started_event_attributes.start_to_fire_timeout.seconds
              for e in history.events if e.HasField("timer_started_event_attributes")]
    assert timers, "the ack window is not a Temporal timer: it would not survive a restart"
    # One timer, the ack window (+ the grace that absorbs ledger clock skew). No sign-off gate ran
    # here -- verification PASSes first time -- so this timer is ours.
    assert len(timers) == 1
    # A window, not an equality: the agent stamps the deadline inside the activity, so a little wall
    # time passes before the workflow subtracts workflow.now() from it. The point of the assertion
    # is that the wait is the ACK WINDOW and not ~0 (a poll) -- so pin it just under the full
    # window+grace, and comfortably above the window itself.
    window = timedelta(minutes=ACK_MINUTES)
    assert window < timedelta(seconds=timers[0]) <= window + ACK_GRACE


def test_a_ledger_stuck_on_a_stale_deadline_still_gets_a_timer_between_rechecks():
    """The skew branch trusts the ledger's re-read deadline -- but a ledger whose clock trails ours
    can report the SAME stale deadline forever, making every computed wait non-positive. Without the
    grace floor the loop would burn ACK_LOOP_CAP re-checks back-to-back with no sleep and bound out
    within a second. So assert the mechanism again: the loop still archives honestly, and EVERY
    re-check is separated by a real Temporal timer of at least the grace period."""
    result, history = asyncio.run(
        _run({"critical": True, "never_overdue": True}, "wf-ack-stale", want_history=True))

    assert result["ack"]["ackStatus"] == "UNACKNOWLEDGED"
    assert result["ack"]["escalations"] == 0              # never overdue, so never escalated
    assert _skills().count("comms.checkAck") == ACK_LOOP_CAP
    assert _skills().count("comms.escalate") == 0

    timers = [e.timer_started_event_attributes.start_to_fire_timeout.seconds
              for e in history.events if e.HasField("timer_started_event_attributes")]
    assert len(timers) == ACK_LOOP_CAP, "a re-check ran without a timer: the sleepless spin is back"
    grace = ACK_GRACE.total_seconds()
    assert all(t >= grace for t in timers[1:]), "re-check waits shrank below the grace floor"


def test_a_routine_result_opens_no_clock_and_is_never_checked():
    """A routine study gets no ack Task, so there is nothing to watch. Polling one anyway would be
    the start of alert fatigue -- and would break, since there is no taskId to poll."""
    result = asyncio.run(_run({"critical": False}, "wf-ack-routine"))

    assert result["finalState"] == "ARCHIVED"
    assert result["ack"] == {}
    assert _STATE["calls"] == []                          # not one check, not one escalation
