"""Unit tests for the MIMIC ETL pure logic (#68). No live stack, no MIMIC data."""
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import manifest as M  # noqa: E402
import fetch  # noqa: E402
import referrers  # noqa: E402
from dicom_fixup import PORTABLE_DESCRIPTION  # noqa: E402

SAMPLE = str(HERE.parent / "sample_cohort.json")


# --- manifest ------------------------------------------------------------
def test_manifest_parses_sample():
    studies = M.load_manifest(SAMPLE)
    assert len(studies) == 3
    s = {x.study_id: x for x in studies}
    assert s["s90000001"].subject_id == "19000001"
    assert isinstance(s["s90000002"].problems[0], M.Problem)
    assert s["s90000002"].problems[0].code == "J95.811"
    assert isinstance(s["s90000001"].labs[0], M.Lab)
    assert s["s90000002"].meds[0].display == "warfarin"


def test_portable_study_description():
    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    assert studies["s90000002"].study_description == PORTABLE_DESCRIPTION  # portable -> AP
    assert studies["s90000001"].study_description == "CHEST (PA AND LAT)"


# --- fetch key derivation (no network) -----------------------------------
def test_fetch_prefix_derivation():
    s = M.CohortStudy(study_id="s56699142", subject_id="10000032")
    assert fetch.study_prefix(s) == "files/p10/p10000032/s56699142/"


def test_fetch_prefix_strips_optional_prefixes():
    s = M.CohortStudy(study_id="56699142", subject_id="p10000032")
    assert fetch.study_prefix(s) == "files/p10/p10000032/s56699142/"


# --- load_cohort orchestration with a fake client ------------------------
class _FakeClient:
    def __init__(self):
        self.calls = []

    def create_patient(self, subject_id, gender="U"):
        self.calls.append(("patient", subject_id)); return f"pat-{subject_id}"

    def create_encounter(self, patient, when):
        self.calls.append(("encounter", patient)); return f"enc-{patient}"

    def ensure_referring_provider(self, username, given, family, gender="U", password=None):
        self.calls.append(("referrer", username)); return f"prov-{username}"

    def insert_radiology_order(self, patient, enc, accession, concept, priority="routine",
                               reason_concept_uuid=None, orderer_provider_uuid=None):
        # Order tuple kept 4-wide (existing assertions); the orderer is a separate record so a study
        # loaded without referrer seeding still matches the unchanged order tuple.
        self.calls.append(("order", accession, priority, reason_concept_uuid))
        self.calls.append(("order-orderer", accession, orderer_provider_uuid))
        return f"ord-{accession}"

    def ensure_order_reason(self, codes, display=""):
        self.calls.append(("reason", tuple(codes), display)); return "reason-" + "+".join(codes)

    def ensure_diagnosis_concept(self, codes, display="", fallback_prefix="Diagnosis "):
        self.calls.append(("diag-concept", tuple(codes), display)); return "diag-" + "+".join(codes)

    def ensure_drug(self, name):
        self.calls.append(("drug", name)); return f"drug-{name}"

    def insert_drug_order(self, patient, enc, drug):
        self.calls.append(("drugorder", drug)); return f"rx-{drug}"

    def create_observation(self, patient, code, value, unit, when):
        self.calls.append(("obs", code)); return "obs-x"

    def create_condition(self, patient, code, when):
        self.calls.append(("cond", code)); return "cond-x"

    def seed_diagnostic_report(self, patient, order, concept, text, status="preliminary"):
        self.calls.append(("report", order, status)); return "rep-x"


def test_load_study_wires_the_join_and_ehr():
    import load_cohort
    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _FakeClient()
    r = load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    assert r["accession"] == "s90000002"          # study_id verbatim
    assert r["order"] == "ord-s90000002"
    assert r["report"] == "rep-x"
    assert r["ehr"]["problems"] == 1              # J95.811 loaded
    assert r["ehr"]["meds"] == 1                  # warfarin loads as a presence-only drug order
    # the order carries the stat priority (drives triage URGENT downstream)
    assert ("order", "s90000002", "stat", "reason-J95.811") in c.calls
    # a preliminary report is seeded basedOn the order
    assert ("report", "ord-s90000002", "preliminary") in c.calls


def test_load_study_ehr_is_best_effort():
    import load_cohort

    class _BadEhr(_FakeClient):
        def create_condition(self, *a, **k):
            raise RuntimeError("no concept mapping")

    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _BadEhr()
    r = load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    # the study still loads (order + report) despite the EHR failure
    assert r["order"] == "ord-s90000002" and r["report"] == "rep-x"
    assert any("problem" in w for w in r.get("warnings", []))


def test_problem_loads_as_provisioned_concept_not_raw_icd_code():
    # #87: the raw ICD code went straight to /condition and persisted as an all-NULL row.
    # The fix provisions the ICD-10-mapped Concept and passes ITS uuid to create_condition.
    import load_cohort
    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _FakeClient()
    r = load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    assert r["ehr"]["problems"] == 1
    assert ("diag-concept", ("J95.811",), "Postprocedural pneumothorax") in c.calls
    assert ("cond", "diag-J95.811") in c.calls
    assert not any(call == ("cond", "J95.811") for call in c.calls)  # never the raw code


def test_problem_concept_failure_degrades_to_warning():
    import load_cohort

    class _BadDiag(_FakeClient):
        def ensure_diagnosis_concept(self, *a, **k):
            raise RuntimeError("no ICD-10 source")

    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _BadDiag()
    r = load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    assert r["order"] == "ord-s90000002" and r["report"] == "rep-x"
    assert r["ehr"]["problems"] == 0
    assert any("problem J95.811" in w for w in r.get("warnings", []))


def test_order_reason_reaches_the_order_with_problem_display():
    import load_cohort
    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _FakeClient()
    load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    # the matching problem's display names the reason concept
    assert ("reason", ("J95.811",), "Postprocedural pneumothorax") in c.calls


def test_reason_failure_never_costs_the_order():
    import load_cohort

    class _BadReason(_FakeClient):
        def ensure_order_reason(self, *a, **k):
            raise RuntimeError("no ICD-10 source")

    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _BadReason()
    r = load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    # order still created, reason-less, with the warning recorded (mirrors the #81 resolver)
    assert r["order"] == "ord-s90000002"
    assert ("order", "s90000002", "stat", None) in c.calls
    assert any("order reason" in w for w in r.get("warnings", []))


def test_a_study_without_reason_codes_never_calls_ensure_order_reason():
    import load_cohort
    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _FakeClient()
    load_cohort.load_study(c, studies["s90000001"], concept_uuid="concept-uuid")
    assert not any(call[0] == "reason" for call in c.calls)
    assert ("order", "s90000001", "routine", None) in c.calls


def test_med_failure_is_best_effort():
    import load_cohort

    class _BadMeds(_FakeClient):
        def ensure_drug(self, *a, **k):
            raise RuntimeError("drug order path refused")

    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _BadMeds()
    r = load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    assert r["order"] == "ord-s90000002" and r["report"] == "rep-x"
    assert r["ehr"]["meds"] == 0
    assert any("warfarin" in w for w in r.get("warnings", []))


# --- referring-physician seeding (#76 build item 1) ----------------------
def test_load_study_assigns_and_wires_a_real_orderer():
    import load_cohort
    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    s = studies["s90000002"]
    expected = referrers.assign(s.subject_id)["username"]
    c = _FakeClient()
    r = load_cohort.load_study(c, s, concept_uuid="concept-uuid")
    # the study's ordering provider is seeded and its uuid reaches insert_radiology_order as orderer
    assert r["referrer"] == expected
    assert ("referrer", expected) in c.calls
    assert ("order-orderer", "s90000002", f"prov-{expected}") in c.calls


def test_two_studies_of_one_patient_share_one_ordering_physician():
    # the clinically honest property: a patient followed by one referrer. Two studies with the SAME
    # subject_id (the sample has none) must resolve to the same seeded orderer uuid on their orders.
    import load_cohort
    a = M.CohortStudy(study_id="s90000010", subject_id="19000010")
    b = M.CohortStudy(study_id="s90000011", subject_id="19000010")  # same patient, second study
    c = _FakeClient()
    ra = load_cohort.load_study(c, a, concept_uuid="concept-uuid")
    rb = load_cohort.load_study(c, b, concept_uuid="concept-uuid")
    assert ra["referrer"] == rb["referrer"]
    orderers = {call[1]: call[2] for call in c.calls if call[0] == "order-orderer"}
    assert orderers["s90000010"] == orderers["s90000011"]        # same orderer on both orders
    assert orderers["s90000010"] == f"prov-{ra['referrer']}"


def test_referrer_seeding_failure_falls_back_to_default_orderer():
    import load_cohort

    class _BadReferrer(_FakeClient):
        def ensure_referring_provider(self, *a, **k):
            raise RuntimeError("provider create refused")

    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _BadReferrer()
    r = load_cohort.load_study(c, studies["s90000001"], concept_uuid="concept-uuid")
    # the study still loads; the order falls back to the ETL default orderer (None -> admin)
    assert r["order"] == "ord-s90000001"
    assert ("order-orderer", "s90000001", None) in c.calls
    assert "referrer" not in r
    assert any("referrer" in w for w in r.get("warnings", []))


def test_seed_referrer_false_leaves_the_orderer_default():
    import load_cohort
    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _FakeClient()
    r = load_cohort.load_study(c, studies["s90000001"], concept_uuid="concept-uuid", seed_referrer=False)
    assert not any(call[0] == "referrer" for call in c.calls)
    assert ("order-orderer", "s90000001", None) in c.calls
    assert "referrer" not in r


# --- stable dictionary UUIDs (no DB) --------------------------------------
def test_reason_concept_uuid_is_stable_and_order_insensitive():
    import bootstrap_radiology_concept as B
    a = B.reason_concept_uuid(["J93.0", "J95.811"])
    b = B.reason_concept_uuid(["J95.811", "J93.0"])
    assert a == b                                  # sorted seed: manifest order never forks the concept
    assert a != B.reason_concept_uuid(["J95.811"])  # different code set, different concept


def test_drug_uuids_normalise_name():
    import bootstrap_radiology_concept as B
    assert B.drug_uuid(" Warfarin ") == B.drug_uuid("warfarin")
    assert B.drug_concept_uuid("warfarin") != B.drug_uuid("warfarin")


# --- link_radiology_studies: the third ETL phase (#76 A.2) -----------------
class _FakeLinkClient:
    def __init__(self, known_accessions=()):
        self.calls = []
        self.known = set(known_accessions)

    def ensure_radiology_study(self, accession, uid, performed_status="COMPLETED"):
        self.calls.append(("study", accession, uid, performed_status))
        return f"study-{accession}" if accession in self.known else None


def _uid_lookup(mapping):
    def find_uid(accession, base_url, http):
        return mapping.get(accession)
    return find_uid


def test_link_studies_writes_real_uids_from_orthanc():
    import link_radiology_studies as L
    studies = list(M.load_manifest(SAMPLE))
    accs = [x.study_id for x in studies]  # study_id verbatim is the accession
    c = _FakeLinkClient(known_accessions=accs)
    uids = {a: f"1.2.3.{i}" for i, a in enumerate(accs)}
    r = L.link_studies(studies, c, "http://orthanc:8042", http=None,
                       find_uid=_uid_lookup(uids))
    assert r["linked"] == len(studies)
    assert r["warnings"] == []
    # every row carries the REAL Orthanc uid and COMPLETED status
    assert all(("study", a, uids[a], "COMPLETED") in c.calls for a in accs)


def test_link_studies_missing_dicom_is_a_warning_not_a_failure():
    import link_radiology_studies as L
    studies = list(M.load_manifest(SAMPLE))
    accs = [x.study_id for x in studies]
    c = _FakeLinkClient(known_accessions=accs)
    uids = {a: f"1.2.3.{i}" for i, a in enumerate(accs[1:], 1)}  # first study not pushed
    r = L.link_studies(studies, c, "http://orthanc:8042", http=None,
                       find_uid=_uid_lookup(uids))
    assert r["linked"] == len(studies) - 1
    assert len(r["warnings"]) == 1 and "not pushed" in r["warnings"][0]
    # the unpushed study never reaches the DB layer: no fabricated uid, ever
    assert not any(call[1] == accs[0] for call in c.calls)


def test_link_studies_missing_order_is_a_warning():
    import link_radiology_studies as L
    studies = list(M.load_manifest(SAMPLE))
    accs = [x.study_id for x in studies]
    c = _FakeLinkClient(known_accessions=accs[1:])  # first study has no order
    r = L.link_studies(studies, c, "http://orthanc:8042", http=None,
                       find_uid=_uid_lookup({a: "1.2.3" for a in accs}))
    assert r["linked"] == len(studies) - 1
    assert len(r["warnings"]) == 1 and "load_cohort" in r["warnings"][0]


# --- conclusion clamp (fhir2 1,024-char column) --------------------------
def test_clamp_conclusion_short_text_untouched():
    from load_cohort import clamp_conclusion
    assert clamp_conclusion("FINDINGS: ok. IMPRESSION: ok.") == "FINDINGS: ok. IMPRESSION: ok."


def test_clamp_conclusion_drops_preamble_keeps_findings_and_impression():
    from load_cohort import clamp_conclusion, FHIR2_CONCLUSION_MAX
    preamble = "WET READ: " + "x" * 900
    body = "FINDINGS:\n " + "f" * 500 + "\nIMPRESSION:\n 1. Pneumothorax."
    out = clamp_conclusion(preamble + "\n" + body)
    assert out == body
    assert len(out) <= FHIR2_CONCLUSION_MAX


def test_clamp_conclusion_keeps_tail_when_findings_alone_too_long():
    from load_cohort import clamp_conclusion, FHIR2_CONCLUSION_MAX
    text = "FINDINGS:\n " + "f" * 2000 + "\nIMPRESSION:\n 1. Effusion."
    out = clamp_conclusion(text)
    assert len(out) == FHIR2_CONCLUSION_MAX
    assert out.endswith("IMPRESSION:\n 1. Effusion.")
