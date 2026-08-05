"""Report-body parsing for the verification rule library (issue #22).

The `ris.report.finalized` event is lean (IDs only, no narrative -- Golden rule 2), so the
handler fetches the report narrative from fhir2 (its `conclusion`, the one clinical field
verification is entitled to, mirroring Impression Generation #16) and passes it here. This
module turns that free-text narrative into the structured `report.body` fields the PI rules
match on: laterality, sections, and the mammography codes (BI-RADS, ACR breast density).

Pure and deterministic: no I/O, no clock, no randomness. Same text in -> same fields out, so a
rule's firing is reproducible from the report alone. Owner: Saptarshi (PI).
"""
from __future__ import annotations
import re

# Section headers a radiology narrative commonly uses -> normalized key. `conclusion` folds into
# `impression` (same section clinically); `history`/`indication` fold into `clinicalHistory`.
_SECTION_HEADERS = {
    "clinical history": "clinicalHistory",
    "history": "clinicalHistory",
    "indication": "clinicalHistory",
    "technique": "technique",
    "comparison": "comparison",
    "findings": "findings",
    "impression": "impression",
    "conclusion": "impression",
    "recommendations": "recommendation",
    "recommendation": "recommendation",
    # MIMIC-CXR reports document result communication under a NOTIFICATION header; the
    # critical-comm-required rule scopes its evidence scan to this section (#92).
    "notification": "notification",
    "notifications": "notification",
}

# One alternation of every header label, longest first so "clinical history" wins over "history".
_HEADER_ALT = "|".join(re.escape(h) for h in sorted(_SECTION_HEADERS, key=len, reverse=True))
_HEADER_RE = re.compile(rf"(?i)\b({_HEADER_ALT})[ \t]*:")

_LEFT_RE = re.compile(r"\bleft\b", re.I)
_RIGHT_RE = re.compile(r"\bright\b", re.I)
_BILATERAL_RE = re.compile(r"\bbilateral\b", re.I)

# "BI-RADS 4", "BIRADS category 4a", "ACR BI-RADS: 7". Capture the number WITHOUT clamping the
# range: an out-of-range value (>6) is exactly what mammo-birads-code-valid must be able to see.
_BIRADS_RE = re.compile(r"bi[\s-]*rads[\s:]*(?:category[\s:]*)?(\d+)", re.I)

# ACR breast density A-D. `\b` after the cue makes the letter a standalone token, so the cue word
# "density" never captures its own leading "d" ("ACR density C" -> "C", via the density cue).
_DENSITY_RE = re.compile(r"(?:breast\s+density|density|acr)\b[\s:()]*(?:category[\s:()]*)?([A-Da-d])\b", re.I)


def detect_laterality(text: str) -> str | None:
    """left | right | bilateral | None. 'bilateral' when stated or when BOTH sides appear; a
    single side when only one appears; None when neither does -- so a laterality rule comparing
    two sides never fires on an absent one."""
    if not text:
        return None
    has_left = bool(_LEFT_RE.search(text))
    has_right = bool(_RIGHT_RE.search(text))
    if _BILATERAL_RE.search(text) or (has_left and has_right):
        return "bilateral"
    if has_left:
        return "left"
    if has_right:
        return "right"
    return None


def split_sections(text: str) -> dict[str, str]:
    """Split a narrative into {sectionKey: text} by its headers (FINDINGS:, IMPRESSION:, ...).
    Text before the first header is dropped (not attributable to a section). Returns {} when the
    narrative carries no recognizable headers. Duplicate headers append rather than clobber."""
    if not text:
        return {}
    matches = list(_HEADER_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        key = _SECTION_HEADERS[m.group(1).strip().lower()]
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        sections[key] = f"{sections[key]}\n{body}".strip() if key in sections else body
    return sections


def parse_birads(text: str) -> int | None:
    """The BI-RADS final assessment number, or None. Not range-clamped: 7 is returned so an
    out-of-range code can be flagged."""
    if not text:
        return None
    m = _BIRADS_RE.search(text)
    return int(m.group(1)) if m else None


def parse_breast_density(text: str) -> str | None:
    """The ACR breast-density category as an upper-case letter A-D, or None."""
    if not text:
        return None
    m = _DENSITY_RE.search(text)
    return m.group(1).upper() if m else None


def parse_report_body(text: str) -> dict:
    """Structure a report narrative into the `report.body` fields the rules match on. `present`
    marks that a narrative was actually parsed, so rules can stay inert (rather than fire on
    absence) when fhir2 gave us nothing."""
    stripped = (text or "").strip()
    if not stripped:
        return {"present": False, "text": "", "sections": {}, "laterality": None,
                "biradsAssessment": None, "breastDensity": None}
    return {
        "present": True,
        "text": stripped,
        "sections": split_sections(stripped),
        "laterality": detect_laterality(stripped),
        "biradsAssessment": parse_birads(stripped),
        "breastDensity": parse_breast_density(stripped),
    }
