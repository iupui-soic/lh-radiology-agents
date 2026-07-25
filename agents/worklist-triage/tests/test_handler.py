"""Contract + signal tests for the Worklist Triage handler.

The contract test (unchanged from v1) guards the schema; the signal tests
document each rule so a clinical reviewer can grep-audit what moves a study
up the reading list. Fixture-corpus tests pin end-to-end tier assignments
against the shared mocks/fixtures so a scoring change never quietly reorders
the demo studies.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from handler import (
    BASE_SCORE,
    handle,
    _description_signals,
    _instance_count_signal,
    _modality_signal,
    _priority_signal,
    _reason_code_signals,
    _score_to_tier,
)
from radagent_common.validation import validate_skill_output

FIXTURES = Path(__file__).resolve().parents[3] / "mocks" / "fixtures"

SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_test",
    "study": {
        "studyInstanceUID": "1.2.3",
        "orthancStudyId": "abc123",
        "modality": "CT",
        "studyDescription": "CT CHEST W/O",
    },
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"priority": "routine", "reasonCode": ["R91.8"]},
    "meta": {"traceId": "trc_x", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


# --- Contract (unchanged v1 guarantee) ---------------------------------------

async def test_output_conforms_to_contract():
    out = await handle("triage.score", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("triage.score", out)  # raises ContractError on violation
    assert out["workflowId"] == "wf_test"


async def test_unexpected_skill_rejected():
    with pytest.raises(ValueError):
        await handle("triage.notAScore", {"studyContext": SAMPLE_CONTEXT})


async def test_rationale_is_never_empty():
    """CLAUDE.md: rationale[] is how radiologists trust the ordering. Even the
    lightest-signal study must return at least the base-score line."""
    minimal_ctx = {
        **SAMPLE_CONTEXT,
        "study": {"studyInstanceUID": "x", "orthancStudyId": "x", "modality": ""},
        "order": {},
    }
    out = await handle("triage.score", {"studyContext": minimal_ctx})
    assert len(out["rationale"]) >= 1
    assert any("base score" in line for line in out["rationale"])


# --- Individual signals ------------------------------------------------------


class TestPrioritySignal:
    def test_stat_bumps(self):
        w, note = _priority_signal({"priority": "stat"})
        assert w == 30 and "stat" in note

    def test_routine_penalised(self):
        w, note = _priority_signal({"priority": "routine"})
        assert w == -5 and "routine" in note

    def test_missing_is_neutral_with_rationale(self):
        w, note = _priority_signal({})
        assert w == 0 and "neutral" in note

    def test_unknown_priority_is_neutral(self):
        w, _ = _priority_signal({"priority": "whenever"})
        assert w == 0


class TestReasonCodeSignals:
    def test_stat_category_hit(self):
        signals = _reason_code_signals({"reasonCode": ["I21.9"]})
        assert len(signals) == 1
        w, note = signals[0]
        assert w == 25 and "STAT" in note and "myocardial infarction" in note

    def test_urgent_category_hit(self):
        signals = _reason_code_signals({"reasonCode": ["S72.001A"]})
        assert len(signals) == 1
        assert signals[0][0] == 15 and "URGENT" in signals[0][1]

    def test_unknown_code_ignored(self):
        assert _reason_code_signals({"reasonCode": ["Z99.999"]}) == []

    def test_exact_stat_code_hit(self):
        """J95.811 (postprocedural pneumothorax) promotes exactly, not J95-wide."""
        signals = _reason_code_signals({"reasonCode": ["J95.811"]})
        assert len(signals) == 1
        assert signals[0][0] == 25 and "postprocedural pneumothorax" in signals[0][1]

    def test_exact_stat_code_siblings_not_promoted(self):
        """Other J95.* codes stay unscored -- the category is too broad to promote."""
        assert _reason_code_signals({"reasonCode": ["J95.1"]}) == []

    def test_exact_stat_code_survives_category_sibling(self):
        """A sibling J95.* earlier in the list must not swallow the J95.811 promotion,
        and the pair must not stack twice."""
        signals = _reason_code_signals({"reasonCode": ["J95.1", "J95.811", "J95.811"]})
        assert len(signals) == 1
        assert signals[0][0] == 25

    def test_multiple_categories_stack(self):
        """MI + shock is more urgent than either alone."""
        signals = _reason_code_signals({"reasonCode": ["I21.9", "R57.0"]})
        assert sum(w for w, _ in signals) == 50  # two STAT categories

    def test_same_category_deduped(self):
        """Five leg-fracture codes should not stack five URGENT bumps."""
        signals = _reason_code_signals({"reasonCode": ["S72.0", "S72.1", "S72.2", "S72.3", "S72.4"]})
        assert len(signals) == 1
        assert signals[0][0] == 15

    def test_non_string_code_tolerated(self):
        """Malformed inputs must not crash the handler (ingress runs on every study)."""
        assert _reason_code_signals({"reasonCode": [None, 123, "I21.9"]}) == \
               [(25, s) for _, s in _reason_code_signals({"reasonCode": ["I21.9"]})]


class TestModalitySignal:
    def test_ct_gets_bump(self):
        w, note = _modality_signal({"modality": "CT"})
        assert w == 5 and "CT" in note

    def test_angio_higher_than_baseline(self):
        w, _ = _modality_signal({"modality": "CTA"})
        assert w == 10

    def test_multi_modality_takes_first_token(self):
        """`ModalitiesInStudy` may arrive as DICOM VR CS backslash-joined."""
        w, note = _modality_signal({"modality": "CT\\MR"})
        assert w == 5 and "CT" in note

    def test_missing_modality_neutral(self):
        w, note = _modality_signal({})
        assert w == 0 and "unknown" in note


class TestDescriptionSignals:
    def test_stat_keyword(self):
        signals = _description_signals({"studyDescription": "CT HEAD STAT"})
        assert any(w == 15 for w, _ in signals)

    def test_stroke_workup(self):
        signals = _description_signals({"studyDescription": "MR BRAIN STROKE PROTOCOL"})
        assert any("stroke" in note for _, note in signals)

    def test_rule_out(self):
        assert any("rule-out" in note for _, note in _description_signals(
            {"studyDescription": "CT CHEST R/O PE"}))
        assert any("rule-out" in note for _, note in _description_signals(
            {"studyDescription": "CT chest rule out dissection"}))

    def test_word_boundary_prevents_false_positive(self):
        """'stat' inside 'static' must not fire the STAT rule."""
        assert _description_signals({"studyDescription": "STATIC KUB IMAGING"}) == []

    def test_missing_description_no_signals(self):
        assert _description_signals({}) == []


class TestInstanceCountSignal:
    def test_high_count_nudges(self):
        assert _instance_count_signal({"numberOfInstances": 800})[0] == 3

    def test_low_count_no_signal(self):
        assert _instance_count_signal({"numberOfInstances": 40}) is None

    def test_missing_count_no_signal(self):
        assert _instance_count_signal({}) is None


class TestScoreToTier:
    @pytest.mark.parametrize("score,tier", [
        (0, "ROUTINE"), (49, "ROUTINE"), (64, "ROUTINE"),
        (65, "URGENT"), (84, "URGENT"),
        (85, "STAT"), (100, "STAT"),
    ])
    def test_boundaries(self, score, tier):
        assert _score_to_tier(score) == tier


# --- End-to-end scoring across the shared demo fixtures ----------------------


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


async def _score(fixture_name: str) -> dict:
    ctx = _load_fixture(fixture_name)
    return await handle("triage.score", {"studyContext": ctx})


async def test_ct_aortic_dissection_scores_stat():
    """STAT priority + I71 (dissection) STAT category + CT bump: expected STAT."""
    out = await _score("studycontext.ct_aortic_dissection.json")
    assert out["priorityTier"] == "STAT"
    assert out["priorityScore"] == 100  # 50 + 30 + 5 + 25 (capped)
    assert any("I71" in line for line in out["rationale"])


async def test_cxr_pneumothorax_scores_stat():
    """Pneumothorax (J93) belongs in the STAT category — this pinned test
    catches a future edit that quietly demotes it."""
    out = await _score("studycontext.cxr_pneumothorax.json")
    assert out["priorityTier"] == "STAT"
    assert any("J93" in line for line in out["rationale"])


async def test_mammo_routine_scores_routine():
    """Screening mammography must never route above ED cross-sectional imaging."""
    out = await _score("studycontext.mammo_routine.json")
    assert out["priorityTier"] == "ROUTINE"
    assert out["priorityScore"] < _routine_cutoff()


async def test_mr_brain_routine_ms_workup_scores_routine():
    """G35 (MS) with routine priority: the study is important but not time-critical."""
    out = await _score("studycontext.mr_brain.json")
    assert out["priorityTier"] == "ROUTINE"


async def test_sample_ct_chest_routine_stays_routine():
    """The walking-skeleton demo fixture must not silently change tier — otherwise
    the pipeline demo suddenly reports a different tier and confuses reviewers."""
    out = await _score("studycontext.sample.json")
    assert out["priorityTier"] == "ROUTINE"


def _routine_cutoff() -> int:
    from handler import _URGENT_CUTOFF
    return _URGENT_CUTOFF


async def test_score_is_bounded():
    """Any combination of signals must respect the schema's 0-100 bound (defense
    in depth on top of the cap in `handle`)."""
    # Stack every STAT signal we can from context alone.
    ctx = {
        **SAMPLE_CONTEXT,
        "study": {
            "studyInstanceUID": "x", "orthancStudyId": "x",
            "modality": "CTA",
            "studyDescription": "CT CHEST STAT CODE TRAUMA STROKE ACUTE R/O PE",
            "numberOfInstances": 1200,
        },
        "order": {"priority": "stat", "reasonCode": ["I21.9", "R57.0", "I26.99", "S06.5"]},
    }
    out = await handle("triage.score", {"studyContext": ctx})
    assert 0 <= out["priorityScore"] <= 100
    assert out["priorityTier"] == "STAT"
