"""Tests for showcase_feedback.py (#76).

Hand-countable responses: two full ones that disagree, one that only saw arcs 1 and 2, and one
carrying an off-scale entry. The point of each assertion is that a thin or malformed cell stays
visible instead of quietly moving a mean.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import showcase_feedback as sf  # noqa: E402


def _response(pid, role="radiologist", scores=None, comment="", overall=None):
    doc = sf.template()
    doc["participantId"] = pid
    doc["role"] = role
    for stage, axes in (scores or {}).items():
        doc["stages"][stage].update(axes)
    if comment:
        doc["stages"]["viewer"]["comment"] = comment
    doc["overall"].update(overall or {})
    return doc


FULL_A = _response("P01", scores={
    "worklist": {"usefulness": 5, "trust": 4, "workflow_fit": 5},
    "viewer": {"usefulness": 5, "trust": 3, "workflow_fit": 4},
    "draft": {"usefulness": 4, "trust": 2, "workflow_fit": 3},
    "verification": {"usefulness": 4, "trust": 4, "workflow_fit": 4},
    "critical_comms": {"usefulness": 5, "trust": 5, "workflow_fit": 4},
    "ehr_packet": {"usefulness": 4, "trust": 4, "workflow_fit": 4},
}, comment="banner was the first thing I looked at",
   overall={"wouldUseInPractice": True, "biggestConcern": "over-reliance on the draft"})

FULL_B = _response("P02", scores={
    "worklist": {"usefulness": 3, "trust": 4, "workflow_fit": 3},
    "viewer": {"usefulness": 3, "trust": 1, "workflow_fit": 2},
    "draft": {"usefulness": 2, "trust": 2, "workflow_fit": 1},
    "verification": {"usefulness": 4, "trust": 4, "workflow_fit": 4},
    "critical_comms": {"usefulness": 5, "trust": 3, "workflow_fit": 4},
    "ehr_packet": {"usefulness": 4, "trust": 4, "workflow_fit": 4},
}, overall={"wouldUseInPractice": False, "safetyConcerns": "a marginal score reads as certainty"})

# Saw arcs 1 and 2 only: the last four stages are blank, which is a legitimate answer.
PARTIAL = _response("P03", role="referring_physician", scores={
    "worklist": {"usefulness": 4, "trust": 4, "workflow_fit": 4},
    "viewer": {"usefulness": 4, "trust": 4, "workflow_fit": 4},
})

OFF_SCALE = _response("P04", scores={"worklist": {"usefulness": 9, "trust": 4, "workflow_fit": 4}})


def _summary(docs):
    return sf.summarize(docs)


def test_a_blank_stage_is_dropped_from_the_mean_not_counted_as_zero():
    """P03 left `draft` blank. The draft mean must be P01+P02 only, and n must say so."""
    s = _summary([FULL_A, FULL_B, PARTIAL])
    cell = s["stages"]["draft"]["axes"]["usefulness"]
    assert cell["answered"] == 2
    assert cell["mean"] == 3.0            # (4 + 2) / 2, P03 excluded entirely


def test_usefulness_and_trust_can_diverge_and_both_are_reported():
    """The instrument exists to surface this gap, so it must not be averaged away."""
    s = _summary([FULL_A, FULL_B])
    viewer = s["stages"]["viewer"]["axes"]
    assert viewer["usefulness"]["mean"] == 4.0   # (5 + 3) / 2
    assert viewer["trust"]["mean"] == 2.0        # (3 + 1) / 2


def test_an_off_scale_score_is_named_and_never_counted():
    s = _summary([FULL_A, OFF_SCALE])
    cell = s["stages"]["worklist"]["axes"]["usefulness"]
    assert cell["answered"] == 1          # only P01's 5
    assert cell["mean"] == 5.0
    assert any("P04" in m and "worklist.usefulness" in m for m in s["invalidScores"])


def test_unanswered_stages_are_listed_per_participant():
    s = _summary([FULL_A, PARTIAL])
    entry = next(i for i in s["incompleteResponses"] if i["participantId"] == "P03")
    assert set(entry["stagesUnanswered"]) == {"draft", "verification", "critical_comms", "ehr_packet"}
    assert not any(i["participantId"] == "P01" for i in s["incompleteResponses"])


def test_distribution_is_kept_so_a_split_room_is_visible():
    """Two people at 5 and 1 average to 3, which is the one number that describes neither."""
    s = _summary([FULL_A, FULL_B])
    assert s["stages"]["viewer"]["axes"]["trust"]["distribution"] == {1: 1, 3: 1}


def test_free_text_and_comments_carry_their_participant():
    s = _summary([FULL_A, FULL_B])
    assert {"participantId": "P01", "text": "over-reliance on the draft"} in \
        s["overall"]["freeText"]["biggestConcern"]
    assert s["stages"]["viewer"]["comments"] == [
        {"participantId": "P01", "comment": "banner was the first thing I looked at"}]


def test_roles_and_would_use_are_counted():
    s = _summary([FULL_A, FULL_B, PARTIAL])
    assert s["byRole"] == {"radiologist": 2, "referring_physician": 1}
    assert s["overall"]["wouldUseInPractice"]["True"] == 1
    assert s["overall"]["wouldUseInPractice"]["False"] == 1


def test_booleans_are_not_mistaken_for_scores():
    """`True` is an int in Python; a stray boolean must not score as a 1."""
    doc = _response("P05", scores={"worklist": {"usefulness": True, "trust": 3, "workflow_fit": 3}})
    s = _summary([doc])
    assert s["stages"]["worklist"]["axes"]["usefulness"]["answered"] == 0
    assert any("P05" in m for m in s["invalidScores"])


def test_the_template_exposes_every_stage_and_axis():
    """The template IS the instrument for whoever fills it, so it must not drift from STAGES."""
    t = sf.template()
    assert set(t["stages"]) == {k for k, _ in sf.STAGES}
    for key, _ in sf.STAGES:
        assert set(t["stages"][key]) == {a for a, _ in sf.AXES} | {"comment"}


def test_loader_skips_things_that_are_not_responses(tmp_path):
    good = tmp_path / "P01.json"
    good.write_text(json.dumps(FULL_A))
    (tmp_path / "metrics.json").write_text(json.dumps({"workflowId": "wf_x"}))
    (tmp_path / "broken.json").write_text("{not json")
    loaded = sf.load_responses([str(tmp_path)])
    assert [d["participantId"] for d in loaded] == ["P01"]


def test_the_table_renders_without_responses_for_a_stage():
    """A stage nobody scored must render as '--', not crash the operator's tally."""
    out = sf.render_table(_summary([PARTIAL]))
    assert "-- (n=0)" in out
    assert "worklist" in out
