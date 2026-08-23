"""Custom rule: a bleeding-class finding on an anticoagulated patient that the report never
addresses.

Post-sign safety net for the #80 medication path. The EHR Assistant derives
`medicationFlags.onAnticoagulant` from `MedicationRequest?patient=` and the orchestrator passes the
packet forward into `report.verify`, so verification can cross-check the finding against the drug
the patient is actually on. Anticoagulation is what turns a bleeding finding from a description
into a management decision (hold or reverse the agent, check coags, escalate sooner), and the
signed report is where that decision is meant to be visible.

The rule fires only when all three hold: the patient is anticoagulated, the narrative ASSERTS a
bleeding-class finding, and neither the impression nor the recommendations mention anticoagulation.
The third clause is what keeps it quiet on a real service: a radiologist who addressed the
anticoagulant gets no WARN, so the rule surfaces gaps rather than restating work already done.

WARN, not FAIL. Verification is a read-only safety net and this rule rests on text matching, so it
asks for a human look instead of holding a gate. Owner: Saptarshi.
"""
from __future__ import annotations

from radagent_common.negation import find_asserted_terms, scannable_text

# Bleeding-class findings, kept local to verification for the same reason _CRITICAL_TERMS is
# (golden rule 4: no cross-agent imports; only the negation LOGIC is shared). Both spellings,
# because MIMIC narratives carry the US forms and the o3 deployment may not.
_BLEED_TERMS = (
    "hemorrhage", "haemorrhage", "hemorrhagic", "haemorrhagic",
    "hematoma", "haematoma", "hemothorax", "haemothorax",
    "bleed", "bleeding",
)

# Substrings, NOT word-boundary terms: "anticoagulated" and "anticoagulation" must both match the
# one stem, and a brand name inside a dose string ("Coumadin 5mg") should still count as addressed.
# INR and coagulopathy are here because a report that names either is discussing the same thing by
# its lab and its diagnosis rather than by the drug.
_ADDRESSED_MARKERS = (
    "anticoagul", "coagulopath", "inr",
    "warfarin", "coumadin", "apixaban", "eliquis", "rivaroxaban", "xarelto",
    "dabigatran", "pradaxa", "edoxaban", "savaysa",
    "heparin", "enoxaparin", "lovenox", "bivalirudin", "argatroban",
)


def _addressed(impression: dict) -> bool:
    """True when the impression prose or any recommendation already raises anticoagulation.

    Recommendations count, and are the more likely place: "Hold anticoagulation and recheck" is the
    action a reader is meant to take, and it lives there rather than in the prose.
    """
    parts = [impression.get("impressionText") or ""]
    for rec in impression.get("recommendations") or []:
        # recommendations are {"text": ...} objects per the impression schema, but a bare string
        # costs nothing to tolerate and a TypeError here would abort the whole verification run.
        parts.append(rec.get("text") or "" if isinstance(rec, dict) else str(rec))
    blob = " ".join(parts).lower()
    return any(marker in blob for marker in _ADDRESSED_MARKERS)


def check(ctx: dict) -> dict | None:
    flags = (ctx.get("ehrContext") or {}).get("medicationFlags") or {}
    if not flags.get("onAnticoagulant"):
        return None
    body = (ctx.get("report") or {}).get("body") or {}
    if not body.get("present"):
        return None
    # Same scoping as critical-finding-unflagged: scannable_text drops the provably non-finding
    # sections, so an INDICATION of "on warfarin, rule out bleed" cannot fire the rule by itself,
    # and find_asserted_terms suppresses a negated term ("no hemothorax") while keeping a real one.
    hits = find_asserted_terms(scannable_text(body.get("text") or ""), _BLEED_TERMS)
    if not hits:
        return None
    if _addressed(ctx.get("impression") or {}):
        return None
    return {
        "ruleId": "anticoagulant-bleed-unaddressed",
        "severity": "WARN",
        "message": (
            f"Report describes '{hits[0]}' and the patient is on an anticoagulant, "
            f"but neither the impression nor the recommendations address anticoagulation."
        ),
        "location": "impression.impressionText",
    }
