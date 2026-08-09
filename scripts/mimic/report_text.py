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

# A tag starts with a letter, `/` or `!` after the `<`. Dictated comparisons -- "effusion
# < 3 cm and > baseline" -- must never be eaten as a pseudo-tag, and the old any-char pattern
# (`<[^>]+>`) did exactly that whenever a later `>` existed in the sentence.
_TAG = re.compile(r"</?[A-Za-z!][^>]*>")


def strip_html(s: str) -> str:
    """Markup -> text, run to a FIXPOINT: strip tags, unescape entities, repeat until stable.

    One strip-then-unescape pass is not enough for what the RIS actually stores. TinyMCE
    re-encodes markup handed to it as text, so a pasted body reaches the radiology_report row
    double-encoded (`<p>&lt;p&gt;FINDINGS...`) -- and a single pass strips the outer tags,
    then UNESCAPES the inner ones back into literal `<p>`. That is how mangled conclusions
    reached fhir2 and parked a signed study at the verification gate (s56689183, 2026-08-09).
    The loop is bounded, not while-True, so a pathological body can never wedge the bridge.

    Whitespace runs are deliberately never collapsed: the cohort forensics fingerprint
    stored-vs-manifest bodies on exactly that invariant.
    """
    text = s or ""
    for _ in range(4):  # depth 4 >> any real editor round-trip (the live case needed 2)
        stripped = html.unescape(_TAG.sub(" ", text))
        if stripped == text:
            break
        text = stripped
    return text.strip()


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
