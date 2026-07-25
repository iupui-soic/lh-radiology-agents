"""Worklist Triage handler — owner: Parvati.

v1.1 = signal-based scoring from StudyContext-only inputs.

Every scoring decision is transparent: each signal appends a `rationale[]` line
naming the field and the weight it contributed. Radiologists grep-audit this
list to trust the ordering; keep it human-readable.

Non-goals (deferred, deliberately):
  * No fhir2 / Orthanc reads — the contract is `{ studyContext }` only, and EHR
    slice is the EHR Assistant's job (lean-reference; one skill = one input).
  * No dwell-time / assignment-age bumping — assignment is OWNED BY LH-Radiology
    (see CLAUDE.md locked decisions); we never write it, and re-ordering by
    assignment age would fight the RIS's own routing.
  * No ML acuity model — that is M2 per this agent's CLAUDE.md. This handler
    stays a transparent rule-of-thumb; it just uses MORE signals than v1.

Input  : { studyContext }
Output : contracts/skills/triage.schema.json
"""
from __future__ import annotations

import re
from typing import Iterable

from radagent_common.tracing import now_iso

AGENT_VERSION = "0.2.0"

# ==============================================================================
# Signal weight tables — kept explicit and short so a clinical reviewer can grep
# the handler and audit every rule. Weights are additive; the final score is
# clamped to [0, 100]. A radiology fellow should sanity-check these tables at
# each milestone; they are the source of truth for the ordering.
# ==============================================================================

BASE_SCORE = 50
_STAT_CUTOFF = 85
_URGENT_CUTOFF = 65

# FHIR ServiceRequest.priority -> weight. "routine" costs a small penalty so a
# routine outpatient study cannot tie with a study whose priority is unknown.
_PRIORITY_WEIGHTS = {"stat": 30, "asap": 25, "urgent": 15, "routine": -5}

# Exact ICD-10 codes that map to a STAT-worthy read where the 3-char category is too
# broad to promote wholesale. J95 is "intraoperative and postprocedural complications of
# the respiratory system" — mostly chronic (tracheostomy complications, post-procedural
# respiratory failure histories), so J95-wide would over-triage; J95.811 specifically is
# postprocedural pneumothorax, clinically the same emergency as J93* (MIMIC codes
# chest-tube/post-VATS pneumothorax this way — PI-requested, M4 dry-run finding).
_STAT_REASON_CODES: dict[str, str] = {
    "J95.811": "postprocedural pneumothorax",
}

# ICD-10 prefixes (first 3 chars, uppercased) that map to a STAT-worthy read.
# Life- or limb-threatening; the radiologist wants these at the top of the list.
_STAT_REASON_PREFIXES: dict[str, str] = {
    "I21": "acute myocardial infarction",
    "I26": "pulmonary embolism",
    "I60": "subarachnoid hemorrhage",
    "I61": "intracerebral hemorrhage",
    "I62": "other nontraumatic intracranial hemorrhage",
    "I63": "cerebral infarction (stroke)",
    "I71": "aortic aneurysm / dissection",
    "J93": "pneumothorax",
    "J96": "respiratory failure",
    "K35": "acute appendicitis",
    "S06": "intracranial injury",
    "R57": "shock",
    "R65": "SIRS / sepsis criteria",
    "A41": "sepsis",
}

# Time-sensitive but not immediately life-threatening -> URGENT weight.
_URGENT_REASON_PREFIXES: dict[str, str] = {
    "R10": "abdominal / pelvic pain (rule-out acute abdomen)",
    "S02": "skull / facial fracture",
    "S22": "rib / thoracic spine fracture",
    "S32": "lumbar / pelvic fracture",
    "S42": "shoulder / humerus fracture",
    "S52": "forearm fracture",
    "S62": "wrist / hand fracture",
    "S72": "femur fracture",
    "S82": "lower leg fracture",
    "S92": "foot fracture",
    "I82": "venous embolism / thrombosis",
    "N20": "urinary calculus",
}

_STAT_REASON_WEIGHT = 25
_URGENT_REASON_WEIGHT = 15

# Modality baseline weights. Advanced cross-sectional imaging usually implies a
# time-pressured question (ED workup, oncology restage); plain film is neutral.
# Angio codes bump higher — CTA head/chest is almost always time-critical.
_MODALITY_WEIGHTS = {
    "CT":  5, "MR":  5,
    "CTA": 10, "MRA": 10,   # angiography variants
    "NM":  0, "PT":  0,     # scheduled nuc-med / PET
    "CR":  0, "DX":  0,     # plain film
    "US":  0, "MG":  0,
}

# High-precision keyword hits in `studyDescription`. \b word boundaries prevent
# false matches ("stat" in "static"). Weights are modest; the description is
# free-text and less reliable than structured priority / reason code.
_DESC_KEYWORD_WEIGHTS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"\bstat\b",              re.IGNORECASE), 15, "description tagged STAT"),
    (re.compile(r"\bcode\b",              re.IGNORECASE), 15, "description tagged CODE (activation)"),
    (re.compile(r"\btrauma\b",            re.IGNORECASE), 15, "description mentions trauma"),
    (re.compile(r"\bstroke\b",            re.IGNORECASE), 15, "description references stroke workup"),
    (re.compile(r"\bacute\b",             re.IGNORECASE), 10, "description mentions acute"),
    (re.compile(r"\br/?o\b|\brule\s*out", re.IGNORECASE),  5, "description is a rule-out request"),
]

# Complexity nudge — very high instance counts often mean multiphase or
# high-resolution studies that take longer to READ (not just to acquire).
# Small weight: this is a nudge, not a bump.
_HIGH_INSTANCE_THRESHOLD = 500
_HIGH_INSTANCE_WEIGHT = 3

# ==============================================================================
# Individual signal functions. Each returns (delta, rationale_line[]) so the
# aggregation logic in `handle` is a simple sum + concat. Keeping signals
# functionally isolated makes them unit-testable in isolation.
# ==============================================================================


def _priority_signal(order: dict) -> tuple[int, str]:
    priority = (order.get("priority") or "").lower()
    if not priority:
        return 0, "no explicit order priority (neutral)"
    weight = _PRIORITY_WEIGHTS.get(priority, 0)
    return weight, f"order priority={priority} ({weight:+d})"


def _reason_code_signals(order: dict) -> list[tuple[int, str]]:
    """Emit one signal per UNIQUE ICD-10 category (deduped by 3-char prefix), so a
    combination like MI + shock scores higher than either alone but a poly-fracture
    order does not stack five copies of the same URGENT category."""
    seen: set[str] = set()
    hits: list[tuple[int, str]] = []
    for code in order.get("reasonCode") or []:
        if not isinstance(code, str):
            continue
        prefix = code.split(".", 1)[0][:3].upper()
        exact = code.strip().upper()
        # Exact-code hits dedupe on the exact code, not the category, so a sibling code
        # from the same (unpromoted) category — e.g. J95.1 before J95.811 — can never
        # swallow the promotion; a repeated J95.811 still scores once.
        if exact in _STAT_REASON_CODES:
            if exact not in seen:
                seen.add(exact)
                hits.append((_STAT_REASON_WEIGHT,
                             f"reason {code} -> {_STAT_REASON_CODES[exact]} (STAT code, "
                             f"+{_STAT_REASON_WEIGHT})"))
            continue
        if prefix in seen:
            continue
        seen.add(prefix)
        if prefix in _STAT_REASON_PREFIXES:
            hits.append((_STAT_REASON_WEIGHT,
                         f"reason {code} -> {_STAT_REASON_PREFIXES[prefix]} (STAT category, "
                         f"+{_STAT_REASON_WEIGHT})"))
        elif prefix in _URGENT_REASON_PREFIXES:
            hits.append((_URGENT_REASON_WEIGHT,
                         f"reason {code} -> {_URGENT_REASON_PREFIXES[prefix]} (URGENT category, "
                         f"+{_URGENT_REASON_WEIGHT})"))
    return hits


def _modality_signal(study: dict) -> tuple[int, str]:
    """`modality` may arrive as `ModalitiesInStudy` (e.g. 'CT\\MR' backslash-joined,
    per DICOM VR CS) or as the single-modality `Modality` tag. Split on both
    DICOM ('\\\\') and CSV separators and take the first non-empty token."""
    raw = (study.get("modality") or "").upper()
    tokens = re.split(r"[\\,]", raw)
    modality = next((t.strip() for t in tokens if t.strip()), "")
    if not modality:
        return 0, "modality unknown"
    weight = _MODALITY_WEIGHTS.get(modality, 0)
    return weight, f"modality={modality} ({weight:+d})" if weight else f"modality={modality}"


def _description_signals(study: dict) -> list[tuple[int, str]]:
    desc = study.get("studyDescription") or ""
    if not desc:
        return []
    return [(w, note) for (pattern, w, note) in _DESC_KEYWORD_WEIGHTS if pattern.search(desc)]


def _instance_count_signal(study: dict) -> tuple[int, str] | None:
    n = study.get("numberOfInstances")
    if isinstance(n, int) and n >= _HIGH_INSTANCE_THRESHOLD:
        return _HIGH_INSTANCE_WEIGHT, (
            f"high instance count ({n} >= {_HIGH_INSTANCE_THRESHOLD}) — complex study"
        )
    return None


def _score_to_tier(score: int) -> str:
    if score >= _STAT_CUTOFF:
        return "STAT"
    if score >= _URGENT_CUTOFF:
        return "URGENT"
    return "ROUTINE"


def _collect_signals(study: dict, order: dict) -> Iterable[tuple[int, str]]:
    yield _priority_signal(order)
    yield _modality_signal(study)
    yield from _reason_code_signals(order)
    yield from _description_signals(study)
    ic = _instance_count_signal(study)
    if ic:
        yield ic


# ==============================================================================
# Skill entrypoint
# ==============================================================================


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "triage.score":
        raise ValueError(f"unexpected skill {skill_id}")
    ctx = payload["studyContext"]
    study = ctx.get("study") or {}
    order = ctx.get("order") or {}

    score = BASE_SCORE
    rationale: list[str] = [f"base score={BASE_SCORE}"]

    for weight, note in _collect_signals(study, order):
        score += weight
        rationale.append(note)

    score = max(0, min(100, score))
    tier = _score_to_tier(score)

    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "priorityScore": score,
        "priorityTier": tier,
        "rationale": rationale,
        "agentVersion": AGENT_VERSION,
        "computedAt": now_iso(),
    }
