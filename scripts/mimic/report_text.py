"""Report-body text helpers shared by the ETL seed path (load_cohort) and the ris-sign-bridge.

fhir2 validates DiagnosticReport.conclusion against a 1,024-char column; anything longer is a
422 and the whole write is lost (live-bisected 2026-08-04: 1024 -> 200, 1025 -> 422; and
presentedForm does not persist on this build, so there is no fhir2-side home for the full
text). Both writers therefore clamp -- and both must clamp in the SAME direction, or the seed
and the sign disagree about which half of a long report survives.
"""
import html
import re

FHIR2_CONCLUSION_MAX = 1024


def strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def clamp_conclusion(text: str, limit: int = FHIR2_CONCLUSION_MAX,
                     reserve: int = 0) -> tuple[str, bool]:
    """Fit the report into fhir2's conclusion column without losing the sections the pipeline
    parses. Prefer dropping the preamble (wet read, history) by starting at FINDINGS; fall back
    to keeping the tail, because MIMIC reports end with IMPRESSION and verification (#42) and
    the flip-to-final rehearsal both need that section present.

    Returns (text, was_truncated): the caller owns the side effects of a cut (the sign-bridge
    prefixes its in-band marker and logs loudly; the ETL seed path does neither). `reserve`
    holds back marker room from the limit ONLY when truncation happens, so a body that already
    fits is never cut just to make space for a marker it doesn't need.
    """
    if len(text) <= limit:
        return text, False
    budget = limit - reserve
    i = text.find("FINDINGS")
    if i != -1 and len(text) - i <= budget:
        return text[i:], True
    return text[-budget:], True
