"""Tests for showcase_draft_audit.py (#76 / #77).

Every case here is one this tool got WRONG in its scratch form during the 2026-08-20 rehearsal.
The join and the normality heuristic are the whole tool; the fetching is a thin shell around them.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import showcase_draft_audit as da  # noqa: E402

PTX = "pneumothorax-detect"
EFF = "effusion-detect"


def _row(acc, ptx=None, eff=None):
    findings = []
    if ptx:
        findings.append({"toolId": PTX, "status": ptx})
    if eff:
        findings.append({"toolId": EFF, "status": eff})
    return {"accessionNumber": acc, "aiFindings": {"findings": findings}}


def _draft(order_uuid, conclusion):
    return {"basedOn": [{"reference": f"ServiceRequest/{order_uuid}"}], "conclusion": conclusion}


# --- the negation-aware assertion check ---------------------------------------------------

def test_a_draft_naming_its_finding_agrees():
    assert da.draft_problems("Pleural effusion is present.", [EFF]) == []


def test_a_draft_that_never_mentions_the_finding_omits_it():
    assert da.draft_problems("No acute cardiopulmonary abnormality.", [EFF]) == \
        ["omits-effusion", "asserts-normal"]


def test_a_draft_that_negates_its_finding_is_worse_than_silence():
    """The reassuring-prose case: it names the pathology only to deny it."""
    assert "negates-effusion" in da.draft_problems(
        "No pleural effusion or pneumothorax is identified.", [EFF])


def test_every_confirmed_finding_must_be_asserted():
    problems = da.draft_problems("Pneumothorax is present.", [PTX, EFF])
    assert problems == ["omits-effusion"]


# --- the false positive that cost a wrong headline number ---------------------------------

def test_a_qualified_normality_claim_beside_an_asserted_finding_is_correct_prose():
    """"X is present. No acute cardiopulmonary abnormalities are OTHERWISE identified." is how a
    radiologist writes a one-finding report. The scratch version flagged it and reported a
    contradiction that did not exist."""
    assert da.draft_problems(
        "Pleural effusion is present. No acute cardiopulmonary abnormalities are otherwise "
        "identified.", [EFF]) == []


def test_a_normality_claim_IS_reported_when_a_finding_was_also_missed():
    """It still carries signal; it just cannot stand alone."""
    problems = da.draft_problems("No acute cardiopulmonary abnormality.", [PTX])
    assert problems == ["omits-pneumothorax", "asserts-normal"]


# --- the join, which is the reason this tool exists in the repo ----------------------------

def test_the_draft_is_chosen_by_basedOn_not_by_position():
    """A cohort patient with a prior carries several pre-sign drafts. Taking the first one
    attributes a sibling study's draft to this study; on the demo host 31 patients are like this,
    and it manufactured a contradiction that was not real."""
    sibling = _draft("order-A", "A pneumothorax is present on the current examination.")
    mine = _draft("order-B", "Chest radiograph demonstrates a pneumothorax and a pleural effusion.")
    assert da.draft_for_order([sibling, mine], "ServiceRequest/order-B") is mine


def test_no_draft_for_this_order_is_not_someone_elses_draft():
    sibling = _draft("order-A", "whatever")
    assert da.draft_for_order([sibling], "ServiceRequest/order-B") is None


def test_a_patient_with_a_prior_does_not_produce_a_false_contradiction():
    rows = [_row("s2", ptx="COMPLETE", eff="COMPLETE")]
    drafts = {"s2": [
        _draft("order-A", "A pneumothorax is present on the current examination."),   # the prior
        _draft("order-B", "Chest radiograph demonstrates a pneumothorax and a pleural effusion."),
    ]}
    out = da.audit(rows, drafts, {"s2": "ServiceRequest/order-B"})
    assert out["tally"].get("agrees") == 1
    assert out["contradictions"] == []
    assert out["patientsWithMultipleDrafts"] == 1


def test_a_real_contradiction_still_surfaces():
    rows = [_row("s3", eff="COMPLETE")]
    drafts = {"s3": [_draft("order-C", "No acute cardiopulmonary abnormality.")]}
    out = da.audit(rows, drafts, {"s3": "ServiceRequest/order-C"})
    assert out["tally"].get("contradicts") == 1
    assert out["contradictions"][0]["accession"] == "s3"
    assert "omits-effusion" in out["contradictions"][0]["problems"]


# --- accounting: nothing is silently dropped ----------------------------------------------

def test_a_study_with_no_complete_finding_expects_no_draft():
    out = da.audit([_row("s4", ptx="STUBBED")], {}, {})
    assert out["tally"] == {"no COMPLETE finding (no draft expected)": 1}


def test_a_missing_draft_is_counted_not_ignored():
    """A COMPLETE finding with no draft for its order is its own defect (the write failed or
    never ran), and must not be silently skipped."""
    out = da.audit([_row("s5", eff="COMPLETE")], {"s5": []}, {"s5": "ServiceRequest/order-D"})
    assert out["tally"] == {"COMPLETE finding but no draft for this order": 1}


def test_render_says_so_when_everything_agrees():
    out = da.audit([_row("s6", eff="COMPLETE")],
                   {"s6": [_draft("order-E", "Pleural effusion is present.")]},
                   {"s6": "ServiceRequest/order-E"})
    assert "no contradicting drafts" in da.render(out)
