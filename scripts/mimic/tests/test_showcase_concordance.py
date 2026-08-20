"""Tests for showcase_concordance.py (#76).

Hand-countable rows. Each test pins one of the judgement calls the metric rests on: absence of a
label is not a negative, a model that never ran is not a miss, and a model that ran and stayed
under its operating point is a true negative rather than a no-op.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import showcase_concordance as sc  # noqa: E402


def _finding(tool, status, raw=0.5):
    return {"toolId": tool, "status": status, "rawScore": raw, "opThreshold": 0.01}


def _row(acc, ptx=None, eff=None, extra=None):
    findings = []
    if ptx:
        findings.append(_finding("pneumothorax-detect", *ptx))
    if eff:
        findings.append(_finding("effusion-detect", *eff))
    findings.extend(extra or [])
    return {"accessionNumber": acc, "aiFindings": {"findings": findings}}


FIRED = ("COMPLETE",)
QUIET = ("STUBBED",)          # ran, stayed under the operating point


def test_sensitivity_counts_only_label_positive_studies():
    rows = [_row("s1", ptx=FIRED), _row("s2", ptx=QUIET), _row("s3", ptx=FIRED)]
    labels = {"s1": {"Pneumothorax": 1}, "s2": {"Pneumothorax": 1}, "s3": {"No Finding": 1}}
    out = sc.concordance(rows, labels)["tools"]["pneumothorax-detect"]
    assert out["positives"] == {"n": 2, "fired": 1, "sensitivity": 0.5}


def test_an_unlabelled_study_is_indeterminate_never_a_negative():
    """The manifest holds concordant positives only, so absence cannot be scored as a negative."""
    rows = [_row("s1", ptx=FIRED), _row("s2", ptx=FIRED)]
    labels = {"s1": {"Pneumothorax": 1}, "s2": {"Pleural Effusion": 1}}   # s2: no ptx, no No Finding
    out = sc.concordance(rows, labels)["tools"]["pneumothorax-detect"]
    assert out["indeterminate"] == {"n": 1, "fired": 1,
                                    "note": "not scored: absence of a concordant label is not a negative"}
    assert out["concordantNormals"]["n"] == 0
    assert out["concordantNormals"]["falsePositiveRate"] is None      # nothing to divide by


def test_specificity_comes_from_concordant_normals_only():
    rows = [_row("s1", ptx=QUIET), _row("s2", ptx=QUIET), _row("s3", ptx=FIRED), _row("s4", ptx=FIRED)]
    labels = {"s1": {"No Finding": 1}, "s2": {"No Finding": 1},
              "s3": {"No Finding": 1}, "s4": {"Pneumothorax": 1}}
    out = sc.concordance(rows, labels)["tools"]["pneumothorax-detect"]
    assert out["concordantNormals"] == {"n": 3, "fired": 1,
                                        "falsePositiveRate": 0.333, "specificity": 0.667}


def test_a_model_that_never_ran_is_excluded_from_every_denominator():
    """A tool stubbed by design has no rawScore; scoring it as a miss would libel the registry."""
    stub = {"toolId": "pneumothorax-detect", "status": "STUBBED", "rawScore": None,
            "opThreshold": None}
    rows = [{"accessionNumber": "s1", "aiFindings": {"findings": [stub]}}, _row("s2", ptx=FIRED)]
    labels = {"s1": {"Pneumothorax": 1}, "s2": {"Pneumothorax": 1}}
    out = sc.concordance(rows, labels)["tools"]["pneumothorax-detect"]
    assert out["modelDidNotRun"] == 1
    assert out["positives"] == {"n": 1, "fired": 1, "sensitivity": 1.0}


def test_effusion_scores_against_pleural_effusion_not_the_cohort_bucket():
    """Consolidation and edema share curate_cohort's EFFUSION_GROUP bucket but are different
    pathologies; the head was never asked about them."""
    rows = [_row("s1", eff=FIRED), _row("s2", eff=FIRED)]
    labels = {"s1": {"Pleural Effusion": 1}, "s2": {"Consolidation": 1}}
    out = sc.concordance(rows, labels)["tools"]["effusion-detect"]
    assert out["positives"]["n"] == 1
    assert out["indeterminate"]["n"] == 1          # the consolidation study is not scored


def test_studies_missing_from_the_manifest_are_reported_not_dropped_silently():
    rows = [_row("s1", ptx=FIRED), _row("sX", ptx=FIRED)]
    out = sc.concordance(rows, {"s1": {"Pneumothorax": 1}})
    assert out["joinedToManifest"] == 1
    assert out["unmatchedAccessions"] == ["sX"]


def test_the_two_tools_are_scored_independently_on_one_study():
    rows = [_row("s1", ptx=QUIET, eff=FIRED)]
    labels = {"s1": {"Pleural Effusion": 1}}
    tools = sc.concordance(rows, labels)["tools"]
    assert tools["effusion-detect"]["positives"] == {"n": 1, "fired": 1, "sensitivity": 1.0}
    # for the ptx head the same study is indeterminate: no ptx label, no No Finding
    assert tools["pneumothorax-detect"]["indeterminate"]["n"] == 1


def test_worklist_accepts_the_envelope_and_a_bare_list():
    row = _row("s1", ptx=FIRED)
    assert sc.worklist_rows({"items": [row]}) == [row]
    assert sc.worklist_rows([row]) == [row]
    assert sc.worklist_rows({}) == []


def test_manifest_accepts_both_shapes():
    entries = [{"study_id": "s1", "labels": {"Pneumothorax": 1}}]
    assert sc.manifest_labels({"studies": entries}) == {"s1": {"Pneumothorax": 1}}
    assert sc.manifest_labels(entries) == {"s1": {"Pneumothorax": 1}}


def test_the_table_names_the_label_policy_and_renders_empty_cells():
    out = sc.render_table(sc.concordance([_row("s1", ptx=FIRED)], {"s1": {"Pneumothorax": 1}}))
    assert "sensitivity=1.0" in out
    assert "specificity=None" in out          # no normals in this slice, and it says so
    assert "chexpert AND negbio" in out
