"""Human-gate durability tests for StudyWorkflow (issue #6; gate ladder-wired by #29).

Uses Temporal's time-skipping test environment to prove the two risk items in #6:
  * ESCALATION — the sign-off gate holds while the report sits unsigned, climbing this ROUTINE
    study's escalation ladder (rung 1 at 4h, rung 2 at 8h, then repeats) until the ack releases it;
  * RESTART-DURING-WAIT — a workflow blocked at AWAITING_RADIOLOGIST survives a worker restart
    and completes once signalled.
Plus the happy path: report_finalized releases the radiologist gate and current_state tracks it.

Activities are mocked (registered under the same names the workflow calls); the multi-hour rung
timers are advanced with env.sleep(). Skipped unless temporalio is installed.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402

# Compact ROUTINE-shaped ladder (mirrors the real policy's structure: widening audience,
# repeating final rung) served by the mocked policy activity.
_LADDER = [
    {"level": 1, "afterMinutes": 240, "targetRole": "reading-radiologist",
     "channels": ["in-app", "email"], "urgency": "routine"},
    {"level": 2, "afterMinutes": 480, "targetRole": "on-call-radiologist",
     "channels": ["email", "sms"], "urgency": "urgent", "repeat": True, "repeatEveryMinutes": 120},
]

STUDY_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_gate_test",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc123", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {},
    "meta": {"traceId": "trc_x", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}

# Shared control/record state for the mock activities (reset per scenario).
_STATE: dict = {}


def _reset(verify_plan: list[tuple[str, bool]]) -> None:
    _STATE.clear()
    _STATE["verify_plan"] = list(verify_plan)   # per-call (verificationStatus, requiresHumanReview)
    _STATE["verify_i"] = 0
    _STATE["escalations"] = []
    _STATE["read_states"] = []          # #108 terminal publishes seen this scenario


@activity.defn(name="call_agent_skill_activity")
async def mock_call_agent(agent: str, skill_id: str, payload: dict) -> dict:
    if skill_id == "report.verify":
        # Record which report each verify saw, so the addendum tests can prove the RE-verify ran
        # against the CORRECTED report and not the original (setdefault: harmless for other tests).
        _STATE.setdefault("verify_reports", []).append(payload.get("report"))
        plan = _STATE["verify_plan"]
        status, human = plan[min(_STATE["verify_i"], len(plan) - 1)]
        _STATE["verify_i"] += 1
        return {"verificationStatus": status, "requiresHumanReview": human, "issues": []}
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
    """Mock for #108 publish_state_activity -- never-raises like the production version.

    Records rather than no-ops: the whole point of #108 is that the worklist LEARNS the read
    finished, so a test that only proves the workflow archived would pass just as happily with
    the publish deleted."""
    _STATE["read_states"].append((workflow_id, study_instance_uid, read_state, changed_at))
    return None


@activity.defn(name="escalate_activity")
async def mock_escalate(workflow_id: str, reason: str, escalation: dict | None = None) -> None:
    _STATE["escalations"].append((workflow_id, reason, escalation))


@activity.defn(name="record_signoff_abandoned_activity")
async def mock_record_signoff_abandoned(workflow_id: str, tier: str | None, pages: int) -> None:
    _STATE.setdefault("abandoned", []).append((workflow_id, tier, pages))


@activity.defn(name="load_escalation_policy_activity")
async def mock_load_policy(tier: str | None) -> list[dict]:
    return _LADDER


def _worker(env: WorkflowEnvironment) -> Worker:
    # max_cached_workflows=0 disables the sticky-queue cache: every workflow task replays from
    # history (what a real worker restart does), and it avoids the sticky schedule-to-start
    # timeout that the time-skipping server won't auto-advance across a worker restart.
    return Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[StudyWorkflow],
        activities=[mock_call_agent, mock_publish, mock_publish_findings, mock_publish_state,
                    mock_escalate, mock_load_policy],
        max_cached_workflows=0,
    )


async def _wait_state(handle, target: str, tries: int = 200) -> None:
    for _ in range(tries):
        if await handle.query(StudyWorkflow.current_state) == target:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow never reached {target}")


def test_report_finalized_releases_radiologist_gate_and_archives():
    """Happy path: the report_finalized signal releases AWAITING_RADIOLOGIST -> ARCHIVED, no escalation."""
    async def scenario():
        _reset([("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-signal", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")  # genuinely blocked at the gate
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"
        assert _STATE["escalations"] == []
        # #108: archiving must also TELL the worklist, or the row keeps rendering as unread.
        assert len(_STATE["read_states"]) == 1, "the terminal read-state publish did not fire"
        wf_id, study_uid, read_state, changed_at = _STATE["read_states"][0]
        # The StudyContext's workflowId, not Temporal's execution id -- same key publish_priority
        # already uses, so the worklist joins both publishes on one identifier.
        assert wf_id == STUDY_CONTEXT["workflowId"]
        assert study_uid == STUDY_CONTEXT["study"]["studyInstanceUID"]
        assert read_state == "ARCHIVED"
        assert changed_at, "the publish must carry when the read finished"
    asyncio.run(scenario())


async def _wait_escalations(count: int, tries: int = 600) -> None:
    """Real-time poll until `count` escalations landed (activity runs shortly after its timer)."""
    for _ in range(tries):
        if len(_STATE["escalations"]) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"never saw {count} escalations (got {len(_STATE['escalations'])})")


def test_unsigned_report_climbs_ladder_until_ack_releases_the_gate():
    """Escalation (#29): the sign-off gate holds while unsigned, climbing the ladder — rung 1,
    rung 2, then the repeating final rung — until a radiologist acknowledges (#57's override
    endpoint is what sends that signal in production; here it is sent directly).

    #57: the ack releases the study to COMMUNICATE and it does NOT re-verify. Re-running
    report.verify against the unchanged report would just re-derive the same FAIL and drop the
    study back into the gate — the loop that made this state inescapable (#56). The FAIL and the
    acknowledgement both ride out on the record.

    Time skipping is locked except inside env.sleep()/result-await, so each advance fires
    exactly the rung it targets and the escalation sequence is deterministic.
    """
    async def scenario():
        # A second (PASS) result is scripted but must NOT be consumed: #57 does not re-verify.
        _reset([("FAIL", True), ("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-escalate", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                await _wait_state(handle, "AWAITING_SIGNOFF")
                await env.sleep(timedelta(minutes=241))   # past rung 1 (240m)
                await _wait_escalations(1)
                await env.sleep(timedelta(minutes=241))   # past rung 2 (480m from entry)
                await _wait_escalations(2)
                await env.sleep(timedelta(minutes=121))   # past the repeat cadence (120m)
                await _wait_escalations(3)
                await handle.signal(
                    StudyWorkflow.signoff_acknowledged,
                    {"acknowledgedBy": "Practitioner/9", "reason": "reviewed; finding is known",
                     "acknowledgedAt": "2026-07-13T04:00:00Z"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"
        rungs = [esc for (_, _, esc) in _STATE["escalations"]]
        assert [r["level"] for r in rungs] == [1, 2, 2]                 # ladder, then the repeat
        assert [r["targetRole"] for r in rungs] == [
            "reading-radiologist", "on-call-radiologist", "on-call-radiologist"]  # widening audience
        assert rungs[2]["attempt"] == 2                                 # the re-fire is marked

        # #57: WHO released the safety gate, and WHY, ride out in the workflow record -- that is the
        # entire audit trail for a human waiving a verification FAIL.
        assert result["signoff"] == {
            "status": "ACKNOWLEDGED",
            "acknowledgedBy": "Practitioner/9",
            "reason": "reviewed; finding is known",
            "acknowledgedAt": "2026-07-13T04:00:00Z",
        }
        # The FAIL is not erased by the acknowledgement -- it is carried forward beside it.
        assert result["verification"]["verificationStatus"] == "FAIL"
        # ...and the unchanged report was never re-verified.
        assert _STATE["verify_i"] == 1, "the gate re-verified an unchanged report (#56's loop)"
    asyncio.run(scenario())


# A single fast-repeating rung so one time-skip can drive the whole repeat cadence to the cap.
_REPEAT_LADDER = [
    {"level": 1, "afterMinutes": 1, "targetRole": "reading-radiologist",
     "channels": ["in-app"], "urgency": "routine", "repeat": True, "repeatEveryMinutes": 1},
]


@activity.defn(name="load_escalation_policy_activity")
async def mock_load_repeat_policy(tier: str | None) -> list[dict]:
    return _REPEAT_LADDER


def test_repeating_rung_stops_at_the_cap_and_then_RELEASES_the_study():
    """Backstop (#29) + the escape hatch (#57): a repeating final rung re-fires exactly
    ESCALATION_REPEAT_CAP times (a history-growth guard) -- and then the gate ENDS.

    This test used to assert the opposite: that after the cap the workflow stayed parked at
    AWAITING_SIGNOFF until an ack arrived. That was the bug (#56). Nothing in production could send
    that ack, so "parked until acked" meant parked forever: the study never reached COMMUNICATE, so
    the critical finding that made verification FAIL was never dispatched, and it never archived --
    silently, because the cap had already stopped the paging.

    Now the chase is bounded and its ending is recorded: the gate releases the study to COMMUNICATE
    with the verification FAIL and the non-acknowledgement both on the record, and dead-letters it.
    """
    from orchestrator.workflow import ESCALATION_REPEAT_CAP

    async def scenario():
        # Two verify results are scripted, but only ONE may be consumed: #57 must not re-verify.
        _reset([("FAIL", True), ("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[mock_call_agent, mock_publish, mock_publish_findings, mock_publish_state, mock_escalate,
                                          mock_load_repeat_policy, mock_record_signoff_abandoned],
                              max_cached_workflows=0):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-cap", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                await _wait_state(handle, "AWAITING_SIGNOFF")
                # One skip past the last re-fire (~cap minutes from entry) fires every rung.
                await env.sleep(timedelta(minutes=ESCALATION_REPEAT_CAP + 30))
                # No ack is ever sent -- and the study must still finish. Before #57 this hung.
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"
        rungs = [esc for (_, _, esc) in _STATE["escalations"]]
        assert len(rungs) == ESCALATION_REPEAT_CAP            # exactly the cap, not one more
        assert rungs[-1]["attempt"] == ESCALATION_REPEAT_CAP  # the final re-fire is the cap-th

        # The ending is honest, not a pass: FAIL stands, nobody acknowledged, and it is dead-lettered.
        assert result["signoff"] == {"status": "ABANDONED"}
        assert result["verification"]["verificationStatus"] == "FAIL"
        assert _STATE["abandoned"] == [("wf_gate_test", "ROUTINE", ESCALATION_REPEAT_CAP)]
        # ...and the unchanged report was NOT re-verified: the scripted PASS is still unconsumed.
        assert _STATE["verify_i"] == 1, "the gate re-verified an unchanged report (#56's loop)"
    asyncio.run(scenario())


# A ladder whose last rung does NOT repeat: the gate reaches its end after exactly one page, which
# isolates what happens in the instant AFTER the final page goes out.
_SINGLE_RUNG_LADDER = [
    {"level": 1, "afterMinutes": 1, "targetRole": "reading-radiologist",
     "channels": ["in-app"], "urgency": "routine"},
]


@activity.defn(name="load_escalation_policy_activity")
async def mock_load_single_rung_policy(tier: str | None) -> list[dict]:
    return _SINGLE_RUNG_LADDER


def test_the_last_page_gets_a_window_to_be_answered_before_the_study_is_abandoned():
    """Bounded must not mean abrupt.

    The gate fires its final page and then ends. If it ended in the SAME instant, that page would be
    unanswerable by construction: we would wake someone and give up on the study before they could
    reach for a phone -- and then dead-letter it as though nobody had cared.

    So after the last page the gate waits one more window before abandoning, and an ack landing in
    that window still releases the study. Without the wait this test fails with signoff=ABANDONED and
    a dead letter, having paged a human it had already stopped listening to.
    """
    async def scenario():
        _reset([("FAIL", True), ("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[mock_call_agent, mock_publish, mock_publish_findings, mock_publish_state, mock_escalate,
                                          mock_load_single_rung_policy,
                                          mock_record_signoff_abandoned],
                              max_cached_workflows=0):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-grace", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                await _wait_state(handle, "AWAITING_SIGNOFF")

                await env.sleep(timedelta(minutes=2))     # past the only rung (1m): it pages
                await _wait_escalations(1)                # ...and the ladder is now exhausted

                # The person it just woke answers. Pre-fix the study was already gone.
                await handle.signal(StudyWorkflow.signoff_acknowledged, {
                    "acknowledgedBy": "Practitioner/dept-lead",
                    "reason": "picked up the page; spoke to the referrer directly",
                    "acknowledgedAt": "2026-07-13T11:00:00Z",
                })
                result = await handle.result()

        assert result["finalState"] == "ARCHIVED"
        assert result["signoff"]["status"] == "ACKNOWLEDGED", (
            "the study was abandoned in the same instant as its last page -- a page nobody could "
            "have answered in time"
        )
        assert result["signoff"]["acknowledgedBy"] == "Practitioner/dept-lead"
        assert _STATE.get("abandoned", []) == [], "an answered gate must not be dead-lettered"
    asyncio.run(scenario())


def test_an_override_that_beats_the_gate_open_is_still_honoured():
    """The signal can land before AWAITING_SIGNOFF is reached -- ingress does not (and cannot
    race-freely) check the workflow's state before signalling, so an override sent while the report
    is still being verified arrives early.

    The ack used to be cleared on the way INTO the gate, which ate exactly that signal: ingress had
    already told the radiologist the study was released (202), and the study then paged its entire
    ladder at people and abandoned itself. A gate must honour an acknowledgement that beat it to the
    door.
    """
    async def scenario():
        _reset([("FAIL", True), ("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-early-ack", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                # Signal the ack BEFORE the report is even finalized: it is buffered, and the gate
                # opens well after the handler has run.
                await handle.signal(StudyWorkflow.signoff_acknowledged, {
                    "acknowledgedBy": "Practitioner/dr-early",
                    "reason": "called the referrer before the report came back",
                    "acknowledgedAt": "2026-07-13T09:00:00Z",
                })
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()

        assert result["finalState"] == "ARCHIVED"
        assert result["signoff"]["status"] == "ACKNOWLEDGED"
        assert result["signoff"]["acknowledgedBy"] == "Practitioner/dr-early", (
            "the override was accepted by ingress (202) and then silently dropped by the gate"
        )
        assert _STATE["escalations"] == [], "an already-acknowledged gate must not page anyone"
        assert _STATE.get("abandoned", []) == []
    asyncio.run(scenario())


def test_workflow_survives_worker_restart_during_gate():
    """Restart-during-wait: kill the worker while the workflow waits at the gate; a fresh worker
    resumes it from durable history and it completes once signalled."""
    async def scenario():
        _reset([("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):  # worker #1
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-restart", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
            # worker #1 is now STOPPED (context exited) — the workflow is still parked at the gate.
            async with _worker(env):  # worker #2 (the "restart")
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"  # resumed on the new worker
    asyncio.run(scenario())


# --- #56 (a) / #66: the RIS addendum flow -- a CORRECTED report re-verifies ------------------


def test_an_addendum_reverifies_the_corrected_report_and_archives():
    """#56 (a) / #66: a corrected report re-enters verification and can PASS.

    The #57 override WAIVES an unchanged report; the addendum REPLACES it. A study parked at
    AWAITING_SIGNOFF on a FAIL receives report_addended (the RIS poller's signal for an
    amended/corrected DiagnosticReport), adopts the corrected report, re-verifies against it, and --
    now that the content actually changed -- PASSes and archives through COMMUNICATE. This is exactly
    the path #56 filed and #57 deliberately left open: a genuinely corrected report getting back
    through the gate, instead of being stuck re-deriving the original FAIL.
    """
    FINAL = {"diagnosticReportId": "DiagnosticReport/1", "status": "final"}
    CORRECTED = {"diagnosticReportId": "DiagnosticReport/1", "status": "corrected"}

    async def scenario():
        # First verify FAILs (-> gate); after the addendum the re-verify PASSes.
        _reset([("FAIL", True), ("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-addendum", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized, FINAL)
                await _wait_state(handle, "AWAITING_SIGNOFF")   # the FAIL parked it at the gate
                await handle.signal(StudyWorkflow.report_addended, CORRECTED)
                result = await handle.result()

        assert result["finalState"] == "ARCHIVED"
        # It DID re-verify (unlike the #57 override, which never does): both verify calls ran...
        assert _STATE["verify_i"] == 2, "the addendum did not trigger a re-verify"
        # ...and the re-verify saw the CORRECTED report, not the original final one.
        assert _STATE["verify_reports"][0] == FINAL
        assert _STATE["verify_reports"][1] == CORRECTED
        # The corrected report PASSed, so the study archived clean -- no escalation, no gate ack,
        # no abandon: it left through the front door, not the escape hatch.
        assert result["verification"]["verificationStatus"] == "PASS"
        assert result["signoff"] == {}
        assert _STATE["escalations"] == []
        assert _STATE.get("abandoned", []) == []
    asyncio.run(scenario())


def test_an_addendum_that_arrives_before_any_finalized_report_satisfies_the_report_gate():
    """A signed-then-corrected report whose FIRST poller sighting is already amended -- sign and
    correct within one poll interval, a poller/fhir2 outage spanning both events, or a sign-off
    predating a fresh-start cursor -- never produces report_finalized: the resource's status is
    already amended. The addendum must satisfy the report gate itself, or the workflow sits at
    AWAITING_RADIOLOGIST forever with the corrected report buffered (a reproduced strand: the
    report IS signed in the RIS, and nothing was ever going to arrive)."""
    CORRECTED = {"diagnosticReportId": "DiagnosticReport/1", "status": "amended"}

    async def scenario():
        _reset([("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-addendum-first",
                    task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_addended, CORRECTED)
                result = await handle.result()

        assert result["finalState"] == "ARCHIVED"
        # Verified ONCE, against the corrected report -- the addendum served as the report event.
        assert _STATE["verify_i"] == 1
        assert _STATE["verify_reports"] == [CORRECTED]
    asyncio.run(scenario())


def test_an_addendum_that_still_fails_returns_to_the_gate_not_to_communicate():
    """The addendum re-verifies HONESTLY: if the correction still FAILs, the study returns to the
    gate rather than archiving. Only a report that genuinely passes leaves through verification; the
    #57 override remains the escape hatch for a FAIL a human chooses to waive.

    Here the corrected report still FAILs, so the study re-parks at AWAITING_SIGNOFF; a subsequent
    override then releases it -- carrying the FAIL and the acknowledgement out on the record, and
    NOT re-verifying again (that terminal property is #57's).
    """
    CORRECTED = {"diagnosticReportId": "DiagnosticReport/1", "status": "amended"}

    async def scenario():
        # initial FAIL -> gate; addendum re-verify still FAILs -> re-park; override is terminal.
        _reset([("FAIL", True), ("FAIL", True)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-addendum-refail", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1", "status": "final"})
                await _wait_state(handle, "AWAITING_SIGNOFF")
                await handle.signal(StudyWorkflow.report_addended, CORRECTED)
                # the addendum's re-verify runs and still FAILs -> the study must return to the gate,
                # not archive. Wait for that second verify, then confirm it is parked again.
                for _ in range(200):
                    if _STATE["verify_i"] >= 2:
                        break
                    await asyncio.sleep(0.02)
                assert _STATE["verify_i"] == 2, "the addendum did not re-verify"
                await _wait_state(handle, "AWAITING_SIGNOFF")   # re-parked on the still-FAIL
                # a human then waives it (the escape hatch): terminal, no third verify.
                await handle.signal(StudyWorkflow.signoff_acknowledged, {
                    "acknowledgedBy": "Practitioner/dr-rao",
                    "reason": "correction still flags; discussed with referrer, releasing",
                    "acknowledgedAt": "2026-07-15T02:00:00Z"})
                result = await handle.result()

        assert result["finalState"] == "ARCHIVED"
        assert _STATE["verify_i"] == 2, "the override re-verified (it must not)"
        assert result["verification"]["verificationStatus"] == "FAIL"
        assert result["signoff"]["status"] == "ACKNOWLEDGED"
        assert result["signoff"]["acknowledgedBy"] == "Practitioner/dr-rao"
    asyncio.run(scenario())
