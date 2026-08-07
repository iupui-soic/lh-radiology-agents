"""Repair #87: cohort problem-list Conditions that persisted with no coded concept.

The pre-fix ETL passed the manifest's raw ICD-10 code where OpenMRS REST /condition needs a
Concept uuid, and this build's converter answers 201 for an unresolvable coded value while
persisting the row with every content column NULL. Result: every cohort problem existed as an
empty Condition, so fhir2 served `code: null` and the EHR packet's activeProblems carried blank
codes.

For each manifest subject this script:
  1. voids the subject's code-less Conditions (they carry no clinical content at all, so the
     void loses nothing),
  2. re-creates the subject's manifest problems through the fixed path
     (ensure_diagnosis_concept + the read-back-verified create_condition).

Idempotent: a problem whose coded Condition already exists is left alone, and a re-run after a
full repair changes nothing. Dry-run by default; pass --apply to write.

Run it where the ETL runs (the bridge/toolbox container has the env):
    python repair_condition_codes.py /path/to/manifest.json [--apply]
"""
from __future__ import annotations

import argparse
import logging
import sys

import bootstrap_radiology_concept as dictionary
import manifest as M
from omrs_client import OmrsClient

_log = logging.getLogger("repair_condition_codes")


def desired_problems(studies: list[M.CohortStudy]) -> dict[str, dict[str, str]]:
    """subject_id -> {icd10 code: display}, deduped across the subject's studies."""
    by_subject: dict[str, dict[str, str]] = {}
    for s in studies:
        probs = by_subject.setdefault(str(s.subject_id), {})
        for p in s.problems:
            if p.code and p.code.strip():
                probs.setdefault(p.code.strip(), p.display or "")
    return by_subject


def fetch_conditions(c: OmrsClient, patient_uuid: str) -> list[dict]:
    """All of the patient's Conditions as fhir2 resources. fhir2 is the read surface the packet
    uses, so 'has a code' here means exactly what #87 means by it."""
    bundle = c._fget("Condition", {"patient": patient_uuid, "_count": 100})
    return [e["resource"] for e in bundle.get("entry", []) or []]


def _codes_on(resource: dict) -> set[str]:
    return {co.get("code") for co in (resource.get("code") or {}).get("coding", []) or [] if co.get("code")}


def repair_subject(c: OmrsClient, subject: str, problems: dict[str, str],
                   apply: bool, stats: dict) -> None:
    patient = c.find_patient_by_subject_id(subject)
    if not patient:
        _log.warning("subject %s: no patient in OpenMRS, skipped", subject)
        stats["subjects_missing"] += 1
        return
    existing = fetch_conditions(c, patient)
    empty = [r for r in existing if not (r.get("code") or {}).get("coding")]
    have_concepts = set().union(*(_codes_on(r) for r in existing)) if existing else set()

    for r in empty:
        stats["voided"] += 1
        if apply:
            c._rdelete(f"condition/{r['id']}")
        else:
            _log.info("subject %s: would void empty condition %s", subject, r["id"])

    onset = "2026-01-01T09:00:00"  # cohort convention; the empty rows carried the same stamp
    for code, display in sorted(problems.items()):
        concept = dictionary.reason_concept_uuid([code])
        if concept in have_concepts:
            stats["already_ok"] += 1
            continue
        stats["created"] += 1
        if apply:
            c.ensure_diagnosis_concept([code], display)
            c.create_condition(patient, concept, onset)
        else:
            _log.info("subject %s: would create condition %s (%s)", subject, code, display or "no display")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("manifest", help="cohort manifest json (the one the ETL loaded)")
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = ap.parse_args(argv)

    studies = M.load_manifest(args.manifest)
    by_subject = desired_problems(studies)
    c = OmrsClient()
    stats = {"subjects": 0, "subjects_missing": 0, "voided": 0, "created": 0, "already_ok": 0}
    for subject, problems in sorted(by_subject.items()):
        stats["subjects"] += 1
        repair_subject(c, subject, problems, args.apply, stats)
    mode = "APPLIED" if args.apply else "DRY RUN"
    _log.info("%s: %d subjects (%d missing), %d empty conditions voided, "
              "%d problems created, %d already coded",
              mode, stats["subjects"], stats["subjects_missing"],
              stats["voided"], stats["created"], stats["already_ok"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
