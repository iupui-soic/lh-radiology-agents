"""#140: may the radiologist's signed report clear a critical flag only the AI raised?

Off by default (option c, today's behaviour); IMPRESSION_SIGNED_NEGATION_CLEARS_AI_FLAG turns on
option (a). Kept in its own file so its setup cannot leak into test_handler.py's, and the reverse.
"""
import pytest

import handler
import llm_draft
from handler import handle
from radagent_common.validation import validate_skill_output

SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_test",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc123", "modality": "DX",
              "studyDescription": "CHEST (PA AND LAT)"},
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"priority": "routine"},
    "meta": {"traceId": "trc_x", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


class _FakeFhir:
    """A fhir2 that is down: the conclusion fetch raises, as in an outage."""
    async def get_report_conclusion(self, report_id: str):
        raise RuntimeError("fhir2 down")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Neither this switch nor the LLM path may leak in from the dev shell.
    for k in ("IMPRESSION_SIGNED_NEGATION_CLEARS_AI_FLAG", "IMPRESSION_LLM_BASE_URL",
              "IMPRESSION_LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    handler._FHIR = None
    handler.draft_impression = llm_draft.draft_impression
    yield
    handler._FHIR = None
    handler.draft_impression = llm_draft.draft_impression



_CLEARS = "IMPRESSION_SIGNED_NEGATION_CLEARS_AI_FLAG"
_PTX_LABEL = "Pneumothorax (screening p=0.91); screening signal only, not a read"
_PTX_AI = {"findings": [{"toolId": "pneumothorax-detect", "status": "COMPLETE", "label": _PTX_LABEL}]}
_PTX_FLAG = [{"label": "pneumothorax", "severity": "critical"}]


async def _post_sign(conclusion: str, ai_findings: dict = _PTX_AI) -> dict:
    out = await handle("impression.generate", {
        "studyContext": SAMPLE_CONTEXT, "report": {"conclusion": conclusion}, "aiFindings": ai_findings})
    validate_skill_output("impression.generate", out)
    return out


def _discordant(out: dict) -> list[str]:
    return [f["label"] for f in out["structuredFindings"] if f.get("severity") == "discordant"]


async def test_by_default_the_signed_negation_does_not_clear_the_ai_flag():
    """Option (c), today's behaviour, stays the default until the PI decides."""
    out = await _post_sign("No pneumothorax.")
    assert out["criticalFlags"] == _PTX_FLAG
    assert _discordant(out) == []


async def test_switched_on_a_signed_negation_clears_an_ai_only_flag(monkeypatch):
    """The #140 case: the AI says pneumothorax, the radiologist signs "No pneumothorax". With the
    switch on the signed read wins: no flag, so no sign-off hold and no Cat1 page -- and the case is
    kept as a discordant entry rather than disappearing."""
    monkeypatch.setenv(_CLEARS, "1")
    out = await _post_sign("No pneumothorax.")
    assert out["criticalFlags"] == []
    assert _discordant(out) == ["pneumothorax"]
    # The cleared screening label is not recited as a finding either.
    assert out["impressionText"] == "No acute findings identified. Clinical correlation recommended."


async def test_switched_on_a_report_that_asserts_the_finding_keeps_the_flag(monkeypatch):
    monkeypatch.setenv(_CLEARS, "1")
    out = await _post_sign("Small left apical pneumothorax.")
    assert out["criticalFlags"] == _PTX_FLAG
    assert _discordant(out) == []


async def test_switched_on_silence_is_not_a_negation(monkeypatch):
    """A report that never names the finding has not rejected it."""
    monkeypatch.setenv(_CLEARS, "1")
    out = await _post_sign("Lungs are clear. Heart size is normal.")
    assert out["criticalFlags"] == _PTX_FLAG


async def test_switched_on_a_rule_out_is_a_question_not_a_negation(monkeypatch):
    """The shared scanner reads "rule out" as a negation cue (right for flagging); here it must not
    clear anything. "Ruled out" is caught by the same neutraliser, so it keeps the flag too: the
    conservative direction, pinned so a change to it is deliberate."""
    monkeypatch.setenv(_CLEARS, "1")
    for conclusion in ("Rule out pneumothorax.", "R/O pneumothorax.", "Pneumothorax has been ruled out."):
        out = await _post_sign(conclusion)
        assert out["criticalFlags"] == _PTX_FLAG, conclusion


async def test_switched_on_a_negation_only_in_the_indication_never_clears(monkeypatch):
    """Only the finding-bearing sections count, as in the flag derivation (#78)."""
    monkeypatch.setenv(_CLEARS, "1")
    out = await _post_sign("INDICATION: No pneumothorax on the prior film. FINDINGS: Lungs are clear.")
    assert out["criticalFlags"] == _PTX_FLAG


async def test_switched_on_resolved_counts_as_a_negation(monkeypatch):
    monkeypatch.setenv(_CLEARS, "1")
    out = await _post_sign("The pneumothorax has resolved.")
    assert out["criticalFlags"] == []
    assert _discordant(out) == ["pneumothorax"]


async def test_switched_on_only_the_negated_finding_is_cleared(monkeypatch):
    monkeypatch.setenv(_CLEARS, "1")
    ai = {"findings": [
        {"toolId": "pneumothorax-detect", "status": "COMPLETE", "label": _PTX_LABEL},
        {"toolId": "dissection-detect", "status": "COMPLETE", "label": "Aortic dissection (p=0.80)"},
    ]}
    out = await _post_sign("No pneumothorax.", ai)
    assert out["criticalFlags"] == [{"label": "aortic dissection", "severity": "critical"}]
    assert _discordant(out) == ["pneumothorax"]


async def test_switched_on_nothing_is_cleared_without_report_text(monkeypatch):
    """Pre-sign there is no signed read to defer to, and a failed conclusion fetch leaves no text."""
    monkeypatch.setenv(_CLEARS, "1")
    pre = await handle("impression.generate", {"studyContext": SAMPLE_CONTEXT, "aiFindings": _PTX_AI})
    assert pre["criticalFlags"] == _PTX_FLAG
    handler._FHIR = _FakeFhir()
    post = await handle("impression.generate", {
        "studyContext": SAMPLE_CONTEXT, "report": {"diagnosticReportId": "DiagnosticReport/1"},
        "aiFindings": _PTX_AI})
    assert post["criticalFlags"] == _PTX_FLAG


async def test_the_switch_accepts_only_the_documented_truthy_values(monkeypatch):
    for value in ("0", "false", "no", ""):
        monkeypatch.setenv(_CLEARS, value)
        assert (await _post_sign("No pneumothorax."))["criticalFlags"] == _PTX_FLAG, value
    for value in ("1", "true", " YES "):
        monkeypatch.setenv(_CLEARS, value)
        assert (await _post_sign("No pneumothorax."))["criticalFlags"] == [], value


async def test_switched_on_the_cleared_finding_never_reaches_the_draft(monkeypatch):
    """The LLM prompt calls every screening label a confirmed finding the prose MUST assert, so a
    cleared label left in would force the draft to contradict the signed report."""
    monkeypatch.setenv(_CLEARS, "1")
    seen = {}

    async def _spy(**kwargs):
        seen.update(kwargs)
        return None

    handler.draft_impression = _spy
    await _post_sign("No pneumothorax.")
    assert seen["critical_flags"] == []
    assert seen["finding_labels"] == []
