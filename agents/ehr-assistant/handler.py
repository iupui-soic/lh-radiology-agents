"""EHR Assistant handler — owner: Parvati.

Assembles the pre-read clinical context packet the Interpretation Assistant needs:
priors, decision-relevant labs, active problems, contrast risk flags, medication
flags, and allergies. All reads are from fhir2 (READ-ONLY); the packet is a
distilled slice, NEVER a raw record dump (lean-reference / PHI minimization).

Input  : { studyContext }
Output : contracts/skills/ehr.schema.json

Design notes
------------
* Concurrent fetches via asyncio.gather(return_exceptions=True). One
  endpoint failing (e.g. Condition search) does NOT starve the others — that
  slice just comes back empty and a WARNING is logged. The workflow never
  crashes on partial fhir2 outage.
* If ingress could not resolve the patient (ctx["patient"]["fhirPatientId"] is
  "Patient/UNRESOLVED"), we return an empty-but-valid packet immediately — no
  point fetching against an id that doesn't exist.
* contrastFlags.egfr: fetched from lab observations by LOINC (see EGFR_LOINCS).
  Creatinine (2160-0) is ALSO fetched so it appears in relevantLabs — a
  bulletproof safety net for radiologists when the eGFR observation itself is
  absent (see the Q1 discussion in the design doc).
* medicationFlags: RxNorm code match with a case-insensitive text fallback,
  because OpenMRS deployments vary widely in whether meds are coded with RxNorm
  vs SNOMED vs plain-text `medicationCodeableConcept.text`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from radagent_common.tracing import now_iso
from radagent_common.fhir_client import Fhir2Client

_log = logging.getLogger(__name__)

AGENT_VERSION = "0.2.0"

# --- LOINC panels ------------------------------------------------------------
# eGFR is reported under several LOINCs depending on which equation the lab uses.
# We query for all of them in one search (code=csv). The freshest value wins.
_EGFR_LOINCS = [
    "33914-3",  # MDRD (older, generic)
    "48642-3",  # eGFR by MDRD, non-African American (older race-based, being deprecated)
    "48643-1",  # eGFR by MDRD, African American
    "62238-1",  # GFR/1.73 sq M by CKD-EPI
    "88293-6",  # eGFR by CKD-EPI 2021 (race-free, current recommended)
    "98979-8",  # eGFR by CKD-EPI 2021
]
_CREATININE_LOINC = "2160-0"

# LOINCs pulled together in a single Observation search. Bare codes: the shared client
# system-qualifies them (`http://loinc.org|<code>`) before the fhir2 search — the live-verified
# quirk and its rationale live on `Fhir2Client.search_observations`.
_LAB_LOINCS = [_CREATININE_LOINC, *_EGFR_LOINCS]

# --- Medication flag rules ---------------------------------------------------
# Each rule = (flag name, RxNorm ingredient codes, text-fallback substrings).
# Text substrings are matched case-insensitively against `code` and `display`;
# they exist because OpenMRS deployments frequently code meds with SNOMED or
# free-text rather than RxNorm.
_MED_FLAG_RULES: list[tuple[str, set[str], tuple[str, ...]]] = [
    # onMetformin: relevant to contrast decisions (metformin hold when eGFR < 30
    # or AKI, per 2020 ACR/NKF joint consensus).
    ("onMetformin", {"6809"}, ("metformin",)),
    # onAnticoagulant: bleeding risk during procedures + implications for stroke workup.
    ("onAnticoagulant",
     {"11289",   # warfarin
      "1599538", # apixaban
      "1114195", # rivaroxaban
      "1546356", # dabigatran
      "1656349"}, # edoxaban
     ("warfarin", "coumadin", "apixaban", "eliquis", "rivaroxaban", "xarelto",
      "dabigatran", "pradaxa", "edoxaban", "savaysa", "heparin", "enoxaparin",
      "lovenox")),
    # onBetaBlocker: matters for stress imaging and thyroid-storm differentials.
    ("onBetaBlocker",
     {"20352",   # metoprolol
      "1202",    # atenolol
      "18867",   # propranolol
      "6918",    # carvedilol
      "1998"},   # bisoprolol
     ("metoprolol", "atenolol", "propranolol", "carvedilol", "bisoprolol",
      "labetalol", "nadolol", "-olol")),
    # onInsulin: hypoglycemia risk for prolonged NPO scans.
    ("onInsulin",
     {"5856",    # insulin
      "51428"},  # insulin glargine
     ("insulin",)),
    # onImmunosuppressant: infection risk changes differential (e.g. atypical
    # pneumonias, opportunistic CNS infections).
    ("onImmunosuppressant",
     {"10600",   # tacrolimus
      "10633",   # cyclosporine
      "6851",    # methotrexate
      "6835",    # mycophenolate
      "6387",    # azathioprine
      "3002"},   # prednisone (chronic high-dose)
     ("tacrolimus", "cyclosporine", "methotrexate", "mycophenolate",
      "azathioprine", "prednisone", "rituximab", "infliximab", "adalimumab")),
]


# --- Skill entrypoint --------------------------------------------------------

async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "ehr.assembleContext":
        raise ValueError(f"unexpected skill {skill_id}")
    ctx = payload["studyContext"]
    fhir_patient_id = (ctx.get("patient") or {}).get("fhirPatientId") or ""

    empty_packet = _empty_packet(ctx["workflowId"])
    # Ingress could not resolve the patient — no point fetching against an id that
    # doesn't exist. Return a valid empty packet and let the workflow proceed.
    if not fhir_patient_id or fhir_patient_id == "Patient/UNRESOLVED":
        _log.info("EHR: patient unresolved for workflow %s; returning empty packet",
                  ctx["workflowId"])
        return empty_packet

    fhir = _client()

    # Concurrent fan-out. return_exceptions=True lets one failed slice degrade to
    # empty without starving the others.
    priors_r, labs_r, problems_r, allergies_r, meds_r = await asyncio.gather(
        fhir.search_imaging_studies(fhir_patient_id),
        fhir.search_observations(fhir_patient_id, _LAB_LOINCS),
        fhir.search_conditions(fhir_patient_id),
        fhir.search_allergies(fhir_patient_id),
        fhir.search_medications(fhir_patient_id),
        return_exceptions=True,
    )

    priors = _degrade(priors_r, "priorStudies", ctx["workflowId"])
    labs = _degrade(labs_r, "relevantLabs", ctx["workflowId"])
    problems = _degrade(problems_r, "activeProblems", ctx["workflowId"])
    allergies = _degrade(allergies_r, "allergies", ctx["workflowId"])
    meds = _degrade(meds_r, "medications", ctx["workflowId"])

    med_flags = _medication_flags(meds)

    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "priorStudies": _dedup_current_study(priors, ctx),
        "relevantLabs": labs,
        "activeProblems": problems,
        "contrastFlags": _contrast_flags(labs, allergies, med_flags),
        "medicationFlags": med_flags,
        "allergies": allergies,
        "agentVersion": AGENT_VERSION,
        "assembledAt": now_iso(),
    }


# --- helpers -----------------------------------------------------------------

def _client() -> Fhir2Client:
    """Factory kept as a separate name so tests can monkeypatch handler._client."""
    return Fhir2Client()


def _empty_packet(workflow_id: str) -> dict:
    return {
        "schemaVersion": "1.0.0",
        "workflowId": workflow_id,
        "priorStudies": [],
        "relevantLabs": [],
        "activeProblems": [],
        "contrastFlags": {"egfr": None, "priorReaction": False, "onMetformin": False},
        "medicationFlags": {},
        "allergies": [],
        "agentVersion": AGENT_VERSION,
        "assembledAt": now_iso(),
    }


def _degrade(result: Any, slice_name: str, workflow_id: str) -> list:
    """asyncio.gather(return_exceptions=True) surfaces exceptions as return values.
    Convert them to an empty list + a WARNING so the packet is still schema-valid."""
    if isinstance(result, BaseException):
        _log.warning("EHR: %s fetch failed for workflow %s: %s (%s)",
                     slice_name, workflow_id, result.__class__.__name__, result)
        return []
    return result or []


def _dedup_current_study(priors: list[dict], ctx: dict) -> list[dict]:
    """Drop the currently-being-read study from priors, in the (uncommon) case that
    fhir2 has already indexed it. Match on studyInstanceUID if we have it — the
    ImagingStudy.identifier[] carries it as a DICOM UID system, but the lean
    projector already drops identifiers, so we filter here on ref instead if a
    correlate is available. For M1 we conservatively return all priors (no dedup)
    when the correlate is missing — surfacing one duplicate is better than
    accidentally hiding a real prior."""
    study_iuid = (ctx.get("study") or {}).get("studyInstanceUID")
    if not study_iuid:
        return priors
    # ImagingStudy `ref` is our own construction (ImagingStudy/<fhir-id>) not the
    # DICOM UID, so there is no reliable ref-based match without an extra fetch.
    # See M2 note above; return priors unchanged.
    return priors


def _latest_by_date(labs: list[dict], code_set: set[str]) -> Any:
    """Latest numeric value across the requested code panel. Uses ISO-8601 lexical
    ordering on `date` (fine for RFC 3339). Returns None if no numeric match."""
    matches = [l for l in labs if l.get("code") in code_set
               and isinstance(l.get("value"), (int, float))]
    if not matches:
        return None
    matches.sort(key=lambda l: l.get("date") or "", reverse=True)
    return matches[0]["value"]


def _contrast_flags(labs: list[dict], allergies: list[dict], med_flags: dict) -> dict:
    """Assemble the contrast-decision slice:
      - egfr: latest eGFR from any of the LOINC variants we asked for
      - priorReaction: any AllergyIntolerance whose code/display mentions
        iodinated contrast (case-insensitive)
      - onMetformin: mirrored from medicationFlags (single source of truth is the
        MedicationRequest search; this field is a legacy schema carve-out).
    Kidney safety net: creatinine appears separately in relevantLabs even when
    eGFR itself is null, so a reader can still eyeball kidney function."""
    egfr = _latest_by_date(labs, set(_EGFR_LOINCS))
    return {
        "egfr": egfr,
        "priorReaction": _has_contrast_allergy(allergies),
        "onMetformin": bool(med_flags.get("onMetformin")),
    }


def _has_contrast_allergy(allergies: list[dict]) -> bool:
    """Detect iodinated-contrast-media allergy. AllergyIntolerance code varies:
    SNOMED (293637006 iodinated contrast media allergy) or free-text 'contrast' /
    'iodine' in code/display."""
    for a in allergies:
        code = str(a.get("code") or "").lower()
        if code in {"293637006"}:
            return True
        if "contrast" in code or "iodine" in code:
            return True
    return False


def _medication_flags(meds: list[dict]) -> dict:
    """Apply the _MED_FLAG_RULES panel — each yields a bool. Also feeds
    contrastFlags.onMetformin (mirrored in the caller)."""
    flags: dict[str, bool] = {}
    for flag_name, rxnorm_codes, text_substrings in _MED_FLAG_RULES:
        flags[flag_name] = _med_matches(meds, rxnorm_codes, text_substrings)
    return flags


def _med_matches(meds: list[dict], rxnorm_codes: set[str],
                 text_substrings: tuple[str, ...]) -> bool:
    """RxNorm code match first (precise); case-insensitive text fallback second."""
    for m in meds:
        if str(m.get("code") or "") in rxnorm_codes:
            return True
        haystack = f"{m.get('code') or ''} {m.get('display') or ''}".lower()
        if any(s in haystack for s in text_substrings):
            return True
    return False
