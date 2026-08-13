"""Impression Generation handler — owner: Chaitra.

v1 returns a deterministic stub draft (no LLM call), but it now STRUCTURES from the report
content (issue #16): the `report` payload is the lean `ris.report.finalized` event
({diagnosticReportId, status, lastUpdatedCursor, ...}) and never carries narrative text inline
(Golden rule 2), so we read the report's `conclusion` from fhir2 by its id and scan it for
critical findings. The fetch is best-effort: if fhir2 is unreachable the draft degrades to "no
acute findings" rather than failing the post-sign safety-net.

Pre-sign (#26): before a report exists, `report` is omitted/empty, so the only signal available
is `aiFindings` (contracts/skills/interpretation.schema.json). Each COMPLETE finding's `label`
runs through the same negation-aware critical-term scan as the report conclusion — but every
signal is scanned SEPARATELY (the conclusion's finding-bearing sections, then each label on its
own, #78), so a pertinent negative in one signal can never silence a positive in another.
STUBBED/ERROR labels are excluded: a STUBBED label may describe a NEGATIVE screen or a referral
code (not a model-asserted finding), and an ERROR label describes a failure -- only COMPLETE
labels assert findings. Post-sign, an aiFindings hit still surfaces even if the conclusion
text misses it. This keeps the handler's own I/O timing-agnostic; wiring the orchestrator to
actually call this skill pre-sign and write the draft back into the RIS is orchestrator/shared-lib
work tracked separately on #26 (out of scope here).

Input  : { studyContext, report?, ehrContext?, aiFindings? }
Output : contracts/skills/impression.schema.json

#77: `impressionText`/`recommendations` prose is LLM-authored (llm_draft.py) when config-gated
on; `criticalFlags`/`structuredFindings` stay this deterministic keyword scan always -- criticality
is never the LLM's call (see #78).
"""
from __future__ import annotations
import logging

from radagent_common.fhir_client import Fhir2Client
from radagent_common.negation import find_asserted_terms, scannable_text
from radagent_common.tracing import now_iso

from llm_draft import draft_impression

AGENT_VERSION = "0.2.0"
_log = logging.getLogger(__name__)

# Read-only fhir2 client for report-content lookup (#16). Lazily built so importing this module
# has no side effect; tests/harness override `_FHIR` with a fake.
_FHIR: Fhir2Client | None = None


def _fhir() -> Fhir2Client:
    global _FHIR
    if _FHIR is None:
        _FHIR = Fhir2Client()
    return _FHIR


# Each keyword maps to its own correct clinical label.
_CRITICAL_KEYWORDS: dict[str, str] = {
    "dissection":  "aortic dissection",
    "pneumothorax": "pneumothorax",
    "hemorrhage":  "intracranial hemorrhage",
    "occlusion":   "vascular occlusion",
    "rupture":     "rupture",
    "infarct":     "infarction",
    "embolism":    "pulmonary embolism",
    "fracture":    "fracture",
    "mass":        "mass lesion",
    "tumor":       "neoplasm",
}


async def _report_conclusion(report: dict) -> str:
    """The report narrative to structure from. An inline `conclusion` (M2 pre-sign draft, or a
    test) wins; otherwise fetch it from fhir2 by diagnosticReportId. Best-effort: a fhir2 miss or
    error yields "" so the safety-net still returns a valid draft."""
    inline = report.get("conclusion")
    if isinstance(inline, str) and inline.strip():
        return inline
    report_id = report.get("diagnosticReportId")
    if not report_id:
        return ""
    try:
        return await _fhir().get_report_conclusion(report_id) or ""
    except Exception:  # noqa: BLE001 - fhir2 down must not fail the post-sign impression
        _log.warning("fhir2 conclusion fetch failed for %s; drafting without critical detection", report_id)
        return ""


def _complete_finding_labels(ai_findings: dict) -> list[str]:
    """Labels of COMPLETE findings (#26), one entry per finding. STUBBED/ERROR labels are excluded:
    a STUBBED label may describe a NEGATIVE screen or a referral code (not a model-asserted finding)
    and an ERROR label a failure -- only COMPLETE labels assert findings. A LIST, not a joined
    string: each label is scanned on its own, so a negation cue in one tool's label
    ("No hemorrhage") can never bleed across and silence a positive term in the next tool's (#78)."""
    return [
        finding.get("label") or ""
        for finding in ai_findings.get("findings", [])
        if finding.get("status") == "COMPLETE"
    ]


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "impression.generate":
        raise ValueError(f"unexpected skill {skill_id}")

    ctx = payload["studyContext"]
    report = payload.get("report") or {}
    ai_findings = payload.get("aiFindings") or {}
    conclusion = await _report_conclusion(report)

    # Deterministically detect critical findings from whichever signal is available (pre-sign:
    # aiFindings only; post-sign: report conclusion, plus aiFindings if also passed forward).
    # Negation-aware (#78): a real NORMAL report is pertinent negatives ("No pneumothorax, effusion,
    # or consolidation"), and a bare keyword match flagged every one of them, FAILing verification
    # and parking the whole normal cohort at the sign-off gate. Three scan rules, each of which a
    # reproduced false flag or false silence forced:
    #   * only the finding-bearing SECTIONS of the narrative are scanned (scannable_text): the
    #     INDICATION names the suspicion ("evaluate for pneumothorax"), not a finding;
    #   * every signal is scanned SEPARATELY -- the conclusion and EACH finding label -- so a
    #     negation in one can never bleed across and silence a positive in another;
    #   * word-boundary match, so "mass" never fires on "massive".
    finding_labels = _complete_finding_labels(ai_findings)
    scan_texts = [scannable_text(conclusion)] + finding_labels
    hits: set[str] = set()
    for text in scan_texts:
        hits |= set(find_asserted_terms(text, _CRITICAL_KEYWORDS))
    critical_flags = [
        {"label": label, "severity": "critical"}
        for keyword, label in _CRITICAL_KEYWORDS.items()
        if keyword in hits
    ]

    structured_findings = [
        {"label": flag["label"], "severity": flag["severity"]}
        for flag in critical_flags
    ]

    # #77: LLM-authored prose when IMPRESSION_LLM_BASE_URL/MODEL are configured; unset (the
    # default) or ANY failure falls back to the deterministic template below. The try/except here
    # is a defense-in-depth backstop -- draft_impression() is contracted to never raise -- since
    # this is the one read-path call that must never strand a sign-off on the LLM hosting choice.
    try:
        llm_draft = await draft_impression(
            conclusion=conclusion,
            finding_labels=finding_labels,
            critical_flags=critical_flags,
            ehr_context=payload.get("ehrContext") or {},
        )
    except Exception:  # noqa: BLE001 - advisory only; must never strand the read (#77)
        _log.warning("impression LLM draft raised unexpectedly; falling back to the template")
        llm_draft = None

    if llm_draft is not None:
        impression = llm_draft.impression_text
        recommendations = [{"text": r} for r in llm_draft.recommendations]
    elif critical_flags:
        impression = (
            f"Findings are consistent with {critical_flags[0]['label']}. "
            "Urgent clinical correlation and appropriate follow-up recommended."
        )
        recommendations = [{"text": "Urgent clinical consultation recommended."}]
    elif finding_labels:
        # A COMPLETE screening finding whose label carries no critical keyword (the first such
        # producer is interpretation's effusion-detect). The constant negative below would be
        # plainly false here -- "No acute findings identified" for a study a model just flagged,
        # and pre-sign that text is WRITTEN to the chart -- so recite the screening labels
        # instead. Non-critical stays non-critical: criticalFlags is empty, so nothing pages and
        # sign-off does not escalate; this branch only fixes what the draft SAYS.
        impression = (
            "AI screening finding: " + "; ".join(finding_labels) + ". "
            "Clinical correlation recommended."
        )
        recommendations = [{"text": "Clinical correlation recommended."}]
    else:
        impression = "No acute findings identified. Clinical correlation recommended."
        recommendations = [{"text": "Routine follow-up as clinically indicated."}]

    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "impressionText": impression,
        "structuredFindings": structured_findings,
        "recommendations": recommendations,
        "criticalFlags": critical_flags,
        "agentVersion": AGENT_VERSION,
        "generatedAt": now_iso(),
    }
