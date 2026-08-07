"""Escalation-ladder wiring (#29): the policy loader, the rung dispatch slice, and the gate's
climb/hold/fallback behavior around it.

The ladder-climb sequence itself is covered in test_workflow_gates.py (with a mocked ladder)
and rung-1 tier parity in test_signoff_timeouts.py (with the real policy); this file covers
the loader's tier resolution, the escalation slice escalate_activity passes forward, the
ack-before-any-rung path, and the legacy fallback when the policy cannot be loaded.
Skipped unless temporalio is installed.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment, ActivityEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

import orchestrator.activities as activities  # noqa: E402
from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402

# Validator for the pass-forward escalation slice. $defs/dispatchEscalation $refs sibling $defs, so
# we validate against a wrapper that keeps the whole $defs block in scope.
_ESCALATION_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts" / "escalation-policy.schema.json").read_text()
)
_DISPATCH_ESCALATION_VALIDATOR = Draft202012Validator(
    {"$schema": _ESCALATION_SCHEMA["$schema"], "$defs": _ESCALATION_SCHEMA["$defs"],
     "$ref": "#/$defs/dispatchEscalation"}
)


# --- unit: load_escalation_policy_activity resolves tiers against the real policy ---

def test_loader_returns_the_tiers_ladder():
    ladder = asyncio.run(activities.load_escalation_policy_activity("STAT"))
    assert [r["level"] for r in ladder] == [1, 2, 3]
    assert ladder[0]["afterMinutes"] == 60          # rung 1 mirrors the pre-#29 STAT timeout
    assert ladder[-1]["repeat"] is True


def test_loader_unknown_or_missing_tier_gets_the_default_ladder():
    routine = asyncio.run(activities.load_escalation_policy_activity("ROUTINE"))
    for tier in (None, "", "WEIRD"):
        assert asyncio.run(activities.load_escalation_policy_activity(tier)) == routine
    assert routine[0]["afterMinutes"] == 240        # rung 1 mirrors the pre-#29 lenient default


def test_loader_honors_env_override_per_call(tmp_path, monkeypatch):
    """ESCALATION_POLICY_PATH re-points the policy without a worker restart (read per gate entry)."""
    alt = tmp_path / "policy.yaml"
    alt.write_text(
        "schemaVersion: '1.0.0'\n"
        "defaultTier: ONLY\n"
        "tiers:\n"
        "  ONLY:\n"
        "    levels:\n"
        "      - level: 1\n"
        "        afterMinutes: 5\n"
        "        targetRole: department-lead\n"
        "        channels: [phone]\n"
        "        urgency: critical\n"
    )
    monkeypatch.setenv("ESCALATION_POLICY_PATH", str(alt))
    ladder = asyncio.run(activities.load_escalation_policy_activity("STAT"))
    assert ladder == [{"level": 1, "afterMinutes": 5, "targetRole": "department-lead",
                       "channels": ["phone"], "urgency": "critical"}]


def test_loader_missing_file_raises(tmp_path, monkeypatch):
    """A broken deploy surfaces as an activity failure -> the workflow falls back to the
    legacy single-timeout gate (integration below) instead of escalating silently wrong."""
    monkeypatch.setenv("ESCALATION_POLICY_PATH", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError):
        asyncio.run(activities.load_escalation_policy_activity("STAT"))


def _write_policy(tmp_path, rung_yaml: str) -> Path:
    p = tmp_path / "policy.yaml"
    p.write_text(
        "schemaVersion: '1.0.0'\n"
        "defaultTier: ONLY\n"
        "tiers:\n"
        "  ONLY:\n"
        "    levels:\n" + rung_yaml
    )
    return p


def test_loader_rejects_a_rung_missing_afterMinutes(tmp_path, monkeypatch):
    """A parseable-but-malformed policy surfaces as an activity failure -> the gate falls back,
    rather than the workflow raising a bare KeyError on rung["afterMinutes"] and hot-retry-wedging
    the study. Guards the live-edit / ESCALATION_POLICY_PATH override paths, which bypass CI."""
    bad = _write_policy(tmp_path,
        "      - level: 1\n"
        "        targetRole: department-lead\n"
        "        channels: [phone]\n"
        "        urgency: critical\n")
    monkeypatch.setenv("ESCALATION_POLICY_PATH", str(bad))
    with pytest.raises(ValueError):
        asyncio.run(activities.load_escalation_policy_activity("ONLY"))


def test_loader_rejects_a_repeating_rung_missing_cadence(tmp_path, monkeypatch):
    """A repeating final rung without repeatEveryMinutes would KeyError inside the workflow's
    repeat loop; the loader rejects it up front so the gate falls back instead of wedging."""
    bad = _write_policy(tmp_path,
        "      - level: 1\n"
        "        afterMinutes: 5\n"
        "        targetRole: department-lead\n"
        "        channels: [phone]\n"
        "        urgency: critical\n"
        "        repeat: true\n")
    monkeypatch.setenv("ESCALATION_POLICY_PATH", str(bad))
    with pytest.raises(ValueError):
        asyncio.run(activities.load_escalation_policy_activity("ONLY"))


# --- unit: escalate_activity passes the fired rung forward as the escalation slice ---

def test_escalate_activity_passes_the_rung_slice(monkeypatch):
    """The dispatch carries the rung's who/how/how-loudly (dispatchEscalation slice) and drops
    its scheduling fields; the legacy critical marker is NOT faked alongside it."""
    captured: dict = {}

    async def _fake_dispatch(base_url, skill_id, payload):
        captured.update(skill_id=skill_id, payload=payload)
        return {"schemaVersion": "1.0.0", "workflowId": "wf_rung", "dispatchStatus": "SENT",
                "agentVersion": "0.1.0", "dispatchedAt": "2026-07-11T00:00:00Z"}

    monkeypatch.setattr(activities, "call_agent_skill", _fake_dispatch)
    rung = {"level": 3, "afterMinutes": 120, "targetRole": "department-lead",
            "channels": ["pager", "phone"], "urgency": "critical",
            "repeat": True, "repeatEveryMinutes": 30, "attempt": 4}

    asyncio.run(ActivityEnvironment().run(
        activities.escalate_activity, "wf_rung", "sign-off gate timed out awaiting radiologist", rung))

    assert captured["skill_id"] == "comms.dispatch"
    assert captured["payload"]["escalation"] == {
        "level": 3, "targetRole": "department-lead", "channels": ["pager", "phone"],
        "urgency": "critical", "attempt": 4,
        "reason": "sign-off gate timed out awaiting radiologist",
    }
    assert "verification" not in captured["payload"]    # no faked FAIL when the rung speaks
    assert "afterMinutes" not in captured["payload"]["escalation"]
    # And it must satisfy its contract: the exact-dict above pins today's shape, but validating
    # against $defs/dispatchEscalation catches producer/contract drift ($def is otherwise
    # unreferenced, so validate_contracts.py never exercises it against a real emitted slice).
    _DISPATCH_ESCALATION_VALIDATOR.validate(captured["payload"]["escalation"])


def test_every_page_carries_the_way_out_of_the_gate(monkeypatch):
    """#57: a page tells a human to act on a stuck study, so it must also tell them HOW to release
    it. Without the pointer, the escalation ladder wakes people who then have nowhere to go -- which
    is how a study sat at this gate until its ladder ran out (#56).

    The URL is read in the ACTIVITY, not the workflow: the workflow may not read env (golden rule 5).
    """
    captured: dict = {}

    async def _fake_dispatch(base_url, skill_id, payload):
        captured.update(payload=payload)
        return {"schemaVersion": "1.0.0", "workflowId": "wf_o", "dispatchStatus": "SENT",
                "agentVersion": "0.1.0", "dispatchedAt": "2026-07-13T00:00:00Z"}

    monkeypatch.setattr(activities, "call_agent_skill", _fake_dispatch)
    monkeypatch.setattr(activities, "SIGNOFF_OVERRIDE_URL",
                        "https://ris.example/signoff/{workflowId}/override")
    rung = {"level": 2, "afterMinutes": 120, "targetRole": "on-call-radiologist",
            "channels": ["pager"], "urgency": "critical"}

    asyncio.run(ActivityEnvironment().run(
        activities.escalate_activity, "wf_o", "sign-off gate timed out awaiting radiologist", rung))

    esc = captured["payload"]["escalation"]
    assert esc["overrideUrl"] == "https://ris.example/signoff/wf_o/override"   # id substituted
    _DISPATCH_ESCALATION_VALIDATOR.validate(esc)          # and it still satisfies the contract


def test_an_unconfigured_override_url_simply_omits_the_pointer(monkeypatch):
    """A deployment that has not set SIGNOFF_OVERRIDE_URL still pages -- it just cannot say where to
    go. The page is the safety-critical part; the pointer is an improvement on it, not a gate on it.
    """
    captured: dict = {}

    async def _fake_dispatch(base_url, skill_id, payload):
        captured.update(payload=payload)
        return {"schemaVersion": "1.0.0", "workflowId": "wf_o", "dispatchStatus": "SENT",
                "agentVersion": "0.1.0", "dispatchedAt": "2026-07-13T00:00:00Z"}

    monkeypatch.setattr(activities, "call_agent_skill", _fake_dispatch)
    monkeypatch.setattr(activities, "SIGNOFF_OVERRIDE_URL", "")
    rung = {"level": 1, "afterMinutes": 60, "targetRole": "reading-radiologist",
            "channels": ["in-app"], "urgency": "routine"}

    asyncio.run(ActivityEnvironment().run(activities.escalate_activity, "wf_o", "reason", rung))

    esc = captured["payload"]["escalation"]
    assert "overrideUrl" not in esc
    _DISPATCH_ESCALATION_VALIDATOR.validate(esc)


# --- integration plumbing -----------------------------------------------------------

STUDY_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_ladder",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"}, "order": {},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}

_STATE: dict = {}


def _reset() -> None:
    _STATE.clear()
    _STATE["verify_i"] = 0
    _STATE["escalations"] = []


@activity.defn(name="call_agent_skill_activity")
async def _mock_call(agent: str, skill_id: str, payload: dict) -> dict:
    if skill_id == "report.verify":
        _STATE["verify_i"] += 1
        first = _STATE["verify_i"] == 1
        return {"verificationStatus": "FAIL" if first else "PASS",
                "requiresHumanReview": first, "issues": []}
    if skill_id == "triage.score":
        return {"priorityTier": "ROUTINE", "priorityScore": 50}
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
    _STATE["escalations"].append((workflow_id, reason, escalation))


@activity.defn(name="load_escalation_policy_activity")
async def _mock_load_policy(tier: str | None) -> list[dict]:
    return [{"level": 1, "afterMinutes": 240, "targetRole": "reading-radiologist",
             "channels": ["in-app"], "urgency": "routine"}]


@activity.defn(name="load_escalation_policy_activity")
async def _boom_load_policy(tier: str | None) -> list[dict]:
    raise RuntimeError("policy store down")


@activity.defn(name="record_policy_failure_activity")
async def _mock_record_policy_failure(workflow_id: str, tier: str | None, reason: str,
                                      attempts: int) -> None:
    _STATE.setdefault("policy_failures", []).append((workflow_id, tier, reason, attempts))


@activity.defn(name="record_policy_failure_activity")
async def _boom_record_policy_failure(workflow_id: str, tier: str | None, reason: str,
                                      attempts: int) -> None:
    raise RuntimeError("dead-letter store unwritable")


@activity.defn(name="record_signoff_abandoned_activity")
async def _mock_record_signoff_abandoned(workflow_id: str, tier: str | None, pages: int) -> None:
    _STATE.setdefault("abandoned", []).append((workflow_id, tier, pages))


async def _wait_state(handle, target: str, tries: int = 200) -> None:
    for _ in range(tries):
        if await handle.query(StudyWorkflow.current_state) == target:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow never reached {target}")


# --- integration: ack before any rung -> no paging at all ---------------------------

def test_ack_before_first_rung_pages_nobody():
    """The gate is a human gate first: an ack before rung 1 (240m) elapses means zero
    escalations. Time skipping is locked while we query/signal, so rung 1 cannot fire early."""
    async def scenario():
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[_mock_call, _mock_publish, _mock_publish_findings,
                                      _mock_publish_state, _mock_escalate,
                                          _mock_load_policy]):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-ladder-ack", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                await _wait_state(handle, "AWAITING_SIGNOFF")
                await handle.signal(StudyWorkflow.signoff_acknowledged, {"ackBy": "Practitioner/9"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"
        assert _STATE["escalations"] == []
    asyncio.run(scenario())


# --- integration: policy unavailable -> legacy single-timeout gate ------------------

def test_policy_load_failure_falls_back_to_legacy_gate():
    """A config disaster must not silence escalation OR strand the gate: the ladder loader fails
    (bounded retries) and the gate falls back to flat pages on the legacy tier timeout
    (escalation=None, no rung slice).

    #54: the fallback is LOUD — a dead letter records that escalation is degraded.

    #57: the fallback is also BOUNDED and TERMINAL. It used to page once and drop back into the
    verify loop, which re-verified the unchanged report, re-FAILed, and re-entered the gate — so
    the degraded path paged forever and the study never archived. Now it pages up to the same cap
    as the ladder's repeating rung and then releases the study with the FAIL unacknowledged, which
    is the only ending that gets the finding to COMMUNICATE at all."""
    async def scenario():
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[_mock_call, _mock_publish, _mock_publish_findings,
                                      _mock_publish_state, _mock_escalate,
                                          _boom_load_policy, _mock_record_policy_failure,
                                          _mock_record_signoff_abandoned]):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-ladder-fallback", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()  # env time-skips the legacy 4h ROUTINE timeout
        from orchestrator.workflow import ESCALATION_REPEAT_CAP

        assert result["finalState"] == "ARCHIVED"
        # Bounded (#57): the degraded path pages up to the cap, not once and not forever.
        assert len(_STATE["escalations"]) == ESCALATION_REPEAT_CAP
        wf, reason, esc = _STATE["escalations"][0]
        assert (wf, esc) == ("wf_ladder", None)    # the legacy flat page, no rung slice
        assert "sign-off" in reason
        # Terminal (#57): nobody acknowledged, so the gate says so and releases rather than holding
        # for a signal that -- on this path -- no one was ever told how to send.
        assert result["signoff"] == {"status": "ABANDONED"}
        assert _STATE["abandoned"] == [("wf_ladder", "ROUTINE", ESCALATION_REPEAT_CAP)]
        # The report still carries its FAIL: releasing the gate is NOT the same as passing.
        assert result["verification"]["verificationStatus"] == "FAIL"
        # ...and the collapse was announced exactly once, carrying the tier that lost its ladder.
        assert len(_STATE["policy_failures"]) == 1
        dl_wf, dl_tier, dl_reason, dl_attempts = _STATE["policy_failures"][0]
        assert (dl_wf, dl_tier) == ("wf_ladder", "ROUTINE")
        assert "policy" in dl_reason and dl_attempts >= 1
    asyncio.run(scenario())


def test_policy_dead_letter_failure_never_costs_the_page():
    """The alert is observability, the page is safety. If the dead-letter store is unwritable
    too, the gate must STILL fall back and page — never fail the workflow over a failed alert."""
    async def scenario():
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[_mock_call, _mock_publish, _mock_publish_findings,
                                      _mock_publish_state, _mock_escalate,
                                          _boom_load_policy, _boom_record_policy_failure,
                                          _mock_record_signoff_abandoned]):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-ladder-dl-boom",
                    task_queue=TASK_QUEUE,
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"    # not a workflow failure
        # The radiologist still got paged even though BOTH the policy and the dead-letter store
        # were down -- the page is the safety-critical part, the alert is only observability.
        assert _STATE["escalations"], "a config disaster silenced escalation entirely"
        # ...and the gate still terminated (#57) rather than holding forever on a signal nobody
        # sends. An unwritable dead-letter store must not turn a released gate into a stranded one.
        assert result["signoff"]["status"] == "ABANDONED"
    asyncio.run(scenario())
