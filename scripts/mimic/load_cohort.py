"""FHIR-first cohort load (#68): for each manifest study, get-or-create the Patient, Encounter and
RadiologyOrder, seed the preliminary report, and best-effort load the EHR packet (labs/meds/
problems). DICOM is pushed SEPARATELY afterwards (dicom_fixup -> Orthanc) so a study's workflow
starts with its patient/order already resolvable (#68 ordering: FHIR first, then DICOM).

Prerequisites on the target stack:
  - a real order/report concept: the demo dictionary has NO chest-x-ray concept, so run
    bootstrap_radiology_concept.py and set MIMIC_ORDER_CONCEPT_UUID before loading.
  - EHR labs/problems need coded numeric/diagnosis concepts; those loads are best-effort so a
    missing concept mapping degrades that packet slice rather than failing the whole study.

Idempotent: patients (by subject_id) and orders (by accession) are get-or-create, so a re-run tops
up rather than duplicates.
"""
from __future__ import annotations
import argparse
import os

from manifest import load_manifest, CohortStudy
from dicom_fixup import study_id_to_accession
from omrs_client import OmrsClient, ORDER_CONCEPT_UUID
from bootstrap_radiology_concept import LAB_LOINC_TO_CONCEPT
import referrers

# A FHIR-valid instant (fhir2 rejects the +0000 offset form). Real loads should stamp the MIMIC
# study date; the manifest can carry it later.
DEFAULT_WHEN = os.environ.get("MIMIC_DEFAULT_WHEN", "2026-01-01T09:00:00+00:00")

# fhir2 validates DiagnosticReport.conclusion against a 1,024-char column; anything longer is a
# 422 and the whole seed is lost. 18/100 real MIMIC reports exceed it (wet-read preambles).
# The direction logic (keep FINDINGS onward, else the IMPRESSION tail) moved to report_text so
# the ris-sign-bridge clamps the SAME way; this shim keeps the seed path's plain-string
# signature so its callers and tests are untouched.
from report_text import FHIR2_CONCLUSION_MAX, clamp_conclusion as _clamp_conclusion


def clamp_conclusion(text: str, limit: int = FHIR2_CONCLUSION_MAX) -> str:
    return _clamp_conclusion(text, limit)[0]


def load_study(c: OmrsClient, s: CohortStudy, concept_uuid: str, when_iso: str = DEFAULT_WHEN,
               seed_referrer: bool = True) -> dict:
    accession = study_id_to_accession(s.study_id)
    summary = {"study_id": s.study_id, "accession": accession, "ehr": {"labs": 0, "problems": 0, "meds": 0}}

    patient = c.create_patient(s.subject_id, gender=s.labels.get("gender", "U"))
    encounter = c.create_encounter(patient, when_iso)
    # Referring physician (#76 build item 1): the study's ORDERER is a real demo Provider, so fhir2
    # surfaces ServiceRequest.requester and the critical-result notification names/reaches the
    # ordering physician (comms.resolve_ordering_provider). Deterministic per subject_id so a
    # patient's studies share one referrer. Best-effort: a seeding failure degrades to the ETL's
    # default orderer (admin) with a warning -- the study still loads and still workflows.
    orderer_uuid = None
    if seed_referrer:
        ref = referrers.assign(s.subject_id)
        try:
            orderer_uuid = c.ensure_referring_provider(
                ref["username"], ref["given"], ref["family"], gender=ref.get("gender", "U"))
            summary["referrer"] = ref["username"]
        except Exception as e:  # noqa: BLE001
            summary.setdefault("warnings", []).append(f"referrer {ref['username']}: {e}")
    # Order reason (#68 gap 4): an ICD-10-mapped Concept, read back by the #81 resolver into
    # StudyContext order.reasonCode. Best-effort like the resolver itself: a reason failure
    # degrades to a reason-less order and must never cost the order or the join.
    reason_uuid = None
    if s.reason_codes:
        display = next((p.display for p in s.problems if p.code in s.reason_codes and p.display), "")
        try:
            reason_uuid = c.ensure_order_reason(list(s.reason_codes), display)
        except Exception as e:  # noqa: BLE001
            summary.setdefault("warnings", []).append(f"order reason {s.reason_codes}: {e}")
    order = c.insert_radiology_order(patient, encounter, accession, concept_uuid,
                                     priority=s.priority, reason_concept_uuid=reason_uuid,
                                     orderer_provider_uuid=orderer_uuid)
    summary.update(patient=patient, encounter=encounter, order=order)

    # EHR packet -- best-effort: a missing concept mapping must not strand the study.
    for lab in s.labs:
        concept = LAB_LOINC_TO_CONCEPT.get(lab.code, lab.code)  # map manifest LOINC -> provisioned concept
        try:
            c.create_observation(patient, concept, lab.value, lab.unit, lab.date or when_iso)
            summary["ehr"]["labs"] += 1
        except Exception as e:  # noqa: BLE001
            summary.setdefault("warnings", []).append(f"lab {lab.code}: {e}")
    for prob in s.problems:
        try:
            if not prob.code:
                raise ValueError("problem with no ICD-10 code")
            # A raw ICD code is NOT a concept uuid: passing it straight to /condition is what
            # blanked the whole cohort's problem list (#87). Provision the ICD-10-mapped Concept
            # first (same family as the order reason above; one code, one shared Concept).
            concept = c.ensure_diagnosis_concept([prob.code], prob.display)
            c.create_condition(patient, concept, when_iso)
            summary["ehr"]["problems"] += 1
        except Exception as e:  # noqa: BLE001
            summary.setdefault("warnings", []).append(f"problem {prob.code}: {e}")
    # meds (#68 gap 3): presence-only drug orders via SQL, so fhir2 surfaces them as
    # MedicationRequest and the anticoagulant med-flag rules see them. Best-effort like labs.
    for med in s.meds:
        name = med.display or med.code
        if not name:
            summary.setdefault("warnings", []).append("med with no display/code skipped")
            continue
        try:
            drug = c.ensure_drug(name)
            c.insert_drug_order(patient, encounter, drug)
            summary["ehr"]["meds"] += 1
        except Exception as e:  # noqa: BLE001
            summary.setdefault("warnings", []).append(f"med {name}: {e}")

    report = c.seed_diagnostic_report(
        patient, order, concept_uuid,
        clamp_conclusion(s.report_text or "FINDINGS: [seed]. IMPRESSION: [seed]."),
        status="preliminary")
    summary["report"] = report
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="FHIR-first load of a MIMIC cohort manifest (#68).")
    p.add_argument("manifest", help="cohort manifest JSON")
    p.add_argument("--concept", default=ORDER_CONCEPT_UUID,
                   help="order/report concept uuid (or set MIMIC_ORDER_CONCEPT_UUID)")
    args = p.parse_args(argv)
    if not args.concept:
        p.error("no order concept: run bootstrap_radiology_concept.py and pass --concept / set MIMIC_ORDER_CONCEPT_UUID")
    c = OmrsClient()
    studies = load_manifest(args.manifest)
    ok = 0
    for s in studies:
        try:
            r = load_study(c, s, args.concept)
            ok += 1
            print(f"loaded {r['study_id']} acc={r['accession']} order={r['order']} "
                  f"req={r.get('referrer', '-')} "
                  f"labs={r['ehr']['labs']} problems={r['ehr']['problems']}")
        except Exception as e:  # noqa: BLE001
            print(f"FAILED {s.study_id}: {e}")
    c.close()
    print(f"\n{ok}/{len(studies)} studies loaded (FHIR side). Now push DICOM (dicom_fixup + "
          f"Orthanc), then run link_radiology_studies.py so the RIS report surface exists.")
    return 0 if ok == len(studies) else 1


if __name__ == "__main__":
    raise SystemExit(main())
