"""Unit tests for the #87 condition-code repair. No live stack."""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bootstrap_radiology_concept as dictionary  # noqa: E402
import manifest as M  # noqa: E402
import repair_condition_codes as R  # noqa: E402

SAMPLE = str(HERE.parent / "sample_cohort.json")


def test_desired_problems_dedupes_per_subject_and_keeps_display():
    studies = [
        M.CohortStudy(study_id="s1", subject_id="19000002",
                      problems=[M.Problem(code="J95.811", display="Postprocedural pneumothorax")]),
        M.CohortStudy(study_id="s2", subject_id="19000002",
                      problems=[M.Problem(code="J95.811", display=""),
                                M.Problem(code="I10", display="Essential hypertension")]),
        M.CohortStudy(study_id="s3", subject_id="19000001", problems=[]),
    ]
    by_subject = R.desired_problems(studies)
    assert by_subject["19000002"] == {"J95.811": "Postprocedural pneumothorax",
                                      "I10": "Essential hypertension"}
    # a subject with no problems still appears, so their empty rows still get voided
    assert by_subject["19000001"] == {}


class _FakeRepairClient:
    """Just the surface repair_subject touches."""

    def __init__(self, conditions):
        self.conditions = conditions
        self.voided = []
        self.created = []
        self.ensured = []

    def find_patient_by_subject_id(self, subject):
        return f"pat-{subject}"

    def _fget(self, path, params=None):
        return {"entry": [{"resource": r} for r in self.conditions]}

    def _rdelete(self, res):
        self.voided.append(res)

    def ensure_diagnosis_concept(self, codes, display="", fallback_prefix="Diagnosis "):
        self.ensured.append(tuple(codes))
        return dictionary.reason_concept_uuid(list(codes))

    def create_condition(self, patient, concept, onset):
        self.created.append(concept)
        return "cond-new"


def _empty_condition(cid):
    return {"id": cid, "clinicalStatus": {"coding": [{"code": "active"}]}}


def _coded_condition(cid, concept_uuid):
    return {"id": cid, "code": {"coding": [{"code": concept_uuid}]}}


def test_repair_voids_empties_and_creates_missing():
    concept = dictionary.reason_concept_uuid(["J95.811"])
    c = _FakeRepairClient([_empty_condition("e1"), _empty_condition("e2")])
    stats = {"subjects_missing": 0, "voided": 0, "created": 0, "already_ok": 0}
    R.repair_subject(c, "19000002", {"J95.811": "Postprocedural pneumothorax"}, apply=True, stats=stats)
    assert c.voided == ["condition/e1", "condition/e2"]
    assert c.created == [concept]
    assert stats == {"subjects_missing": 0, "voided": 2, "created": 1, "already_ok": 0}


def test_repair_is_idempotent_when_condition_already_coded():
    concept = dictionary.reason_concept_uuid(["J95.811"])
    c = _FakeRepairClient([_coded_condition("c1", concept)])
    stats = {"subjects_missing": 0, "voided": 0, "created": 0, "already_ok": 0}
    R.repair_subject(c, "19000002", {"J95.811": ""}, apply=True, stats=stats)
    assert c.voided == [] and c.created == []
    assert stats["already_ok"] == 1


def test_dry_run_writes_nothing():
    c = _FakeRepairClient([_empty_condition("e1")])
    stats = {"subjects_missing": 0, "voided": 0, "created": 0, "already_ok": 0}
    R.repair_subject(c, "19000002", {"J95.811": ""}, apply=False, stats=stats)
    assert c.voided == [] and c.created == [] and c.ensured == []
    assert stats["voided"] == 1 and stats["created"] == 1  # counted, not written
