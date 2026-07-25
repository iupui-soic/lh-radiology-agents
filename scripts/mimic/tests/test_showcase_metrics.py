"""Tests for showcase_metrics.py (#76).

Synthetic workflow-result payloads shaped exactly like orchestrator/workflow.py:run() returns --
the fast path, the sloppy-dictation WARN+override arc, a hard FAIL, and a critical-ack study --
so the roll-up numbers are checked against a hand-countable cohort.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import showcase_metrics as sm  # noqa: E402


def _result(wf, final="ARCHIVED", tier="ROUTINE", score=10.0, vstatus="PASS",
            issues=None, requires=False, signoff=None, ack=None):
    return {
        "workflowId": wf,
        "finalState": final,
        "triage": {"priorityTier": tier, "priorityScore": score},
        "verification": {
            "verificationStatus": vstatus,
            "requiresHumanReview": requires,
            "issues": issues or [],
        },
        "comms": {},
        "ack": ack or {},
        "signoff": signoff or {},
    }


# Arc 1: clean fast path. Arc 3: FINDINGS-no-IMPRESSION WARN, released by an authenticated
# #57 override (the workflow records that as status ACKNOWLEDGED + the ingress ack fields).
# Plus a FAIL whose ladder ran out (ABANDONED, no acknowledgedBy), carrying a critical-result
# ack completed after one escalation (escalations is the workflow's int counter, not a list).
COHORT = [
    _result("wf_a", tier="ROUTINE", score=8.0, vstatus="PASS"),
    _result("wf_b", tier="ROUTINE", score=9.0, vstatus="PASS"),
    _result(
        "wf_c", tier="URGENT", score=40.0, vstatus="WARN", requires=True,
        issues=[{"ruleId": "impression_section_present", "severity": "WARN",
                 "message": "FINDINGS present without IMPRESSION"}],
        signoff={"status": "ACKNOWLEDGED", "acknowledgedBy": "Dr. Reader",
                 "reason": "reviewed", "acknowledgedAt": "2026-07-20T10:00:00Z"},
    ),
    _result(
        "wf_d", tier="STAT", score=90.0, vstatus="FAIL", requires=True,
        issues=[{"ruleId": "laterality_agreement", "severity": "FAIL", "message": "mismatch"},
                {"ruleId": "impression_section_present", "severity": "WARN", "message": "no impression"}],
        signoff={"status": "ABANDONED"},
        ack={"ackStatus": "COMPLETED", "taskId": "t1", "escalations": 1},
    ),
]


def _write_cohort(tmp_path, results):
    for r in results:
        (tmp_path / f"{r['workflowId']}.json").write_text(json.dumps(r))
    return str(tmp_path)


def test_load_skips_non_results(tmp_path):
    _write_cohort(tmp_path, COHORT)
    (tmp_path / "manifest.json").write_text(json.dumps({"studies": []}))
    (tmp_path / "broken.json").write_text("{not json")
    loaded = sm.load_results([str(tmp_path)])
    assert len(loaded) == 4  # the 4 results, not the manifest or the broken file


def test_final_state_and_counts():
    s = sm.summarize(COHORT)
    assert s["studies"] == 4
    assert s["byFinalState"] == {"ARCHIVED": 4}


def test_triage_rollup():
    s = sm.summarize(COHORT)
    t = s["triage"]
    assert t["triaged"] == 4
    assert t["byTier"] == {"ROUTINE": 2, "URGENT": 1, "STAT": 1}
    assert t["meanPriorityScore"] == round((8 + 9 + 40 + 90) / 4, 3)


def test_verification_hit_rate_and_rules():
    s = sm.summarize(COHORT)
    v = s["verification"]
    assert v["verified"] == 4
    assert v["byStatus"] == {"PASS": 2, "WARN": 1, "FAIL": 1}
    # 2 of 4 did not pass clean.
    assert v["hitRate"] == 0.5
    assert v["requiresHumanReviewRate"] == 0.5
    # impression rule fires on both wf_c and wf_d.
    assert v["topRules"]["impression_section_present"] == 2
    assert v["topRules"]["laterality_agreement"] == 1
    assert v["issuesBySeverity"] == {"WARN": 2, "FAIL": 1}


def test_signoff_gate():
    s = sm.summarize(COHORT)
    g = s["signoffGate"]
    assert g["recordedReleases"] == 2
    # only wf_c carries the ingress override ack (acknowledgedBy); ABANDONED has none
    assert g["authenticatedOverrides"] == 1
    assert g["byStatus"] == {"ACKNOWLEDGED": 1, "ABANDONED": 1}


def test_critical_ack():
    s = sm.summarize(COHORT)
    a = s["criticalAck"]
    assert a["withAckClock"] == 1
    assert a["byStatus"] == {"COMPLETED": 1}
    assert a["meanEscalations"] == 1.0


def test_partial_payload_degrades():
    # A study captured before triage/verification ran: only workflowId + finalState.
    s = sm.summarize([{"workflowId": "wf_x", "finalState": "TRIAGE"}])
    assert s["studies"] == 1
    assert s["triage"]["triaged"] == 0
    assert s["verification"]["verified"] == 0
    assert s["verification"]["hitRate"] is None


def test_richer_capture_is_flagged():
    s = sm.summarize(COHORT)
    assert any("concordance" in m for m in s["requiresRicherCapture"])
    assert any("time-in-state" in m for m in s["requiresRicherCapture"])
    # the gate metric's blind spot must be named: addendum releases leave signoff empty
    assert any("addendum" in m for m in s["requiresRicherCapture"])
