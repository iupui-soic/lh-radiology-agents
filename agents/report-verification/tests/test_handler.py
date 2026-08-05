"""Verification contract test + a rule-firing assertion."""
from handler import handle
from radagent_common.validation import validate_skill_output

SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_test",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"}, "order": {},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


async def test_clean_report_passes():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "impression": {"impressionText": "No acute findings.", "criticalFlags": [], "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    assert out["verificationStatus"] == "PASS"
    assert out["requiresHumanReview"] is False


async def test_critical_flag_fails_and_requires_review():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "impression": {"impressionText": "Tension pneumothorax.",
                       "criticalFlags": [{"label": "tension pneumothorax", "severity": "critical"}],
                       "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    assert out["verificationStatus"] == "FAIL"
    assert out["requiresHumanReview"] is True
    assert any(i["ruleId"] == "critical-comm-required" for i in out["issues"])


async def test_critical_flag_with_documented_communication_passes():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "impression": {"impressionText": "Tiny right apical pneumothorax.",
                       "criticalFlags": [{"label": "pneumothorax", "severity": "critical"}],
                       "recommendations": []},
        "report": {"conclusion": "FINDINGS:\n\nTiny right apical pneumothorax.\n\n"
                                 "IMPRESSION:\n\nTiny right apical pneumothorax.\n\n"
                                 "NOTIFICATION: Findings were relayed by Dr. A to Dr. B "
                                 "by phone at 15:44 (0 minutes after discovery)."},
    })
    validate_skill_output("report.verify", out)
    assert not any(i["ruleId"] == "critical-comm-required" for i in out["issues"])


async def test_critical_flag_without_documented_communication_fails():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "impression": {"impressionText": "Large left pneumothorax.",
                       "criticalFlags": [{"label": "pneumothorax", "severity": "critical"}],
                       "recommendations": []},
        "report": {"conclusion": "FINDINGS:\n\nLarge left pneumothorax.\n\n"
                                 "IMPRESSION:\n\nLarge left pneumothorax."},
    })
    validate_skill_output("report.verify", out)
    assert out["verificationStatus"] == "FAIL"
    assert any(i["ruleId"] == "critical-comm-required" for i in out["issues"])


# --- evidence polarity (#92): each of these four bodies satisfied the keyword-only cut of the
#     rule. A negated, failed, promised, or anamnestic mention is NOT documentation. ------------

_CRITICAL_IMPRESSION = {"impressionText": "Large left pneumothorax.",
                        "criticalFlags": [{"label": "pneumothorax", "severity": "critical"}],
                        "recommendations": []}


async def _verify_body(conclusion: str) -> dict:
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "impression": _CRITICAL_IMPRESSION,
        "report": {"conclusion": conclusion},
    })
    validate_skill_output("report.verify", out)
    return out


async def test_negated_communication_is_not_evidence():
    out = await _verify_body("FINDINGS:\n\nLarge left pneumothorax.\n\n"
                             "IMPRESSION:\n\nLarge left pneumothorax. "
                             "The referring team was not notified.")
    assert any(i["ruleId"] == "critical-comm-required" for i in out["issues"])


async def test_failed_communication_attempt_is_not_evidence():
    out = await _verify_body("IMPRESSION: Large left pneumothorax. "
                             "We were unable to reach the ordering physician by telephone.")
    assert any(i["ruleId"] == "critical-comm-required" for i in out["issues"])


async def test_future_tense_communication_is_not_evidence():
    out = await _verify_body("IMPRESSION: Large left pneumothorax. "
                             "Findings will be communicated to the team.")
    assert any(i["ruleId"] == "critical-comm-required" for i in out["issues"])


async def test_history_mention_cannot_satisfy_the_rule():
    out = await _verify_body("CLINICAL HISTORY: Family informed of biopsy plan last week.\n\n"
                             "FINDINGS:\n\nLarge left pneumothorax.\n\n"
                             "IMPRESSION:\n\nLarge left pneumothorax.")
    assert any(i["ruleId"] == "critical-comm-required" for i in out["issues"])


async def test_failed_then_successful_communication_is_evidence():
    # The failed attempt and the successful call are separate clauses; the second one counts.
    out = await _verify_body("IMPRESSION: Large left pneumothorax.\n\n"
                             "NOTIFICATION: Unable to reach Dr. A; findings discussed with "
                             "covering physician Dr. B by telephone at 14:05.")
    assert not any(i["ruleId"] == "critical-comm-required" for i in out["issues"])


# --- empty-impression (#44): blank or absent impression text WARNs. Absent-fires-too is the
#     decided behavior (see the rule's description): for a safety net both states mean there is
#     no impression text to review, and WARN requests review without gating. ---------------------


async def test_empty_impression_text_warns():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "impression": {"impressionText": "", "criticalFlags": [], "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    assert out["verificationStatus"] == "WARN"
    assert out["requiresHumanReview"] is True
    assert any(i["ruleId"] == "empty-impression" for i in out["issues"])


async def test_absent_impression_input_also_warns():
    out = await handle("report.verify", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("report.verify", out)
    assert any(i["ruleId"] == "empty-impression" for i in out["issues"])


async def test_normal_impression_text_does_not_warn():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "impression": {"impressionText": "No acute findings.", "criticalFlags": [], "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    assert not any(i["ruleId"] == "empty-impression" for i in out["issues"])


# --- report-body parsing feeds the PI rules (#22). An inline `conclusion` stands in for the fhir2
#     fetch so these stay hermetic (the handler prefers it over a network read). -----------------

def _rule_ids(out: dict) -> set[str]:
    return {i["ruleId"] for i in out["issues"]}


async def test_mammo_rules_fire_from_parsed_body():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "IMPRESSION: BI-RADS 4 suspicious mass, left breast."},
        "impression": {"impressionText": "Suspicious mass.", "criticalFlags": [], "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    ids = _rule_ids(out)
    assert "mammo-actionable-needs-followup" in ids  # BI-RADS 4 with no recommendation
    assert "mammo-density-stated" in ids             # mammo read with no density stated
    assert out["verificationStatus"] == "WARN"


async def test_out_of_range_birads_flagged():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "BI-RADS 7. Breast density C."},
        "impression": {"impressionText": "See report.", "criticalFlags": [], "recommendations": [{"text": "f/u"}]},
    })
    validate_skill_output("report.verify", out)
    assert "mammo-birads-code-valid" in _rule_ids(out)


async def test_laterality_mismatch_warns():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "Nodule seen in the right upper lobe."},
        "impression": {"impressionText": "Left upper lobe nodule; follow-up advised.",
                       "criticalFlags": [], "recommendations": [{"text": "CT in 3 months"}]},
    })
    validate_skill_output("report.verify", out)
    assert "laterality-consistency" in _rule_ids(out)


async def test_critical_in_body_but_unflagged_warns():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "Moderate pneumothorax on the right."},
        "impression": {"impressionText": "Findings noted.", "criticalFlags": [], "recommendations": [{"text": "x"}]},
    })
    validate_skill_output("report.verify", out)
    assert "critical-finding-unflagged" in _rule_ids(out)


async def test_pertinent_negative_body_does_not_warn_or_fail():
    """#78 acceptance (verification side): a normal report of pertinent negatives must NOT trip the
    unflagged-critical WARN. Before the negation window, "No pneumothorax..." matched the term, and
    since the impression carried no flag, this rule WARNed on every normal study."""
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "IMPRESSION: No pneumothorax, pleural effusion, or focal consolidation."},
        "impression": {"impressionText": "No acute cardiopulmonary process.",
                       "criticalFlags": [], "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    assert "critical-finding-unflagged" not in _rule_ids(out)
    assert out["verificationStatus"] == "PASS"


async def test_indication_naming_the_suspicion_does_not_warn():
    """#78 regression (reproduced): the rule scanned the WHOLE narrative including the INDICATION section, so
    "evaluate for pneumothorax" re-flagged every normal study ordered to exclude it. The scan is
    scoped to the finding-bearing sections (falling back to full text only when no headers parse)."""
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": ("INDICATION: Chest pain, evaluate for pneumothorax.\n"
                                  "FINDINGS: Lungs are clear.\n"
                                  "IMPRESSION: No pneumothorax.")},
        "impression": {"impressionText": "No acute findings.", "criticalFlags": [], "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    assert "critical-finding-unflagged" not in _rule_ids(out)
    assert out["verificationStatus"] == "PASS"


async def test_a_finding_in_the_preheader_preamble_still_warns():
    """#78 regression (reproduced): report_body.split_sections drops text before the first header, so a
    finding dictated ahead of the headers went unscanned. The rule scans via the shared
    scannable_text, which keeps the preamble."""
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": ("Large right pneumothorax.\n"
                                  "COMPARISON: None available.\n"
                                  "IMPRESSION: No acute cardiopulmonary process.")},
        "impression": {"impressionText": "Stable.", "criticalFlags": [], "recommendations": [{"text": "x"}]},
    })
    validate_skill_output("report.verify", out)
    assert "critical-finding-unflagged" in _rule_ids(out)


async def test_a_stable_but_present_finding_in_the_body_still_warns():
    """#78 regression (reproduced false negative): "no significant change in the large
    pneumothorax" is standard dictation for a PRESENT finding -- the WARN must fire when the
    impression carries no flag."""
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "FINDINGS: No significant change in the large pneumothorax."},
        "impression": {"impressionText": "Stable.", "criticalFlags": [], "recommendations": [{"text": "x"}]},
    })
    validate_skill_output("report.verify", out)
    assert "critical-finding-unflagged" in _rule_ids(out)


async def test_findings_without_impression_warns():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "FINDINGS: Stable chest. No acute process.\nTECHNIQUE: PA and lateral."},
        "impression": {"impressionText": "Stable.", "criticalFlags": [], "recommendations": [{"text": "x"}]},
    })
    validate_skill_output("report.verify", out)
    assert "impression-section-present" in _rule_ids(out)


async def test_clean_report_with_narrative_still_passes():
    # A well-formed report body must not trip any rule (guards against false positives).
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "IMPRESSION: No acute cardiopulmonary process."},
        "impression": {"impressionText": "No acute findings.", "criticalFlags": [], "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    assert out["verificationStatus"] == "PASS"
    assert out["issues"] == []


# --- load-once (#96): report.verify must not re-read the rules directory or re-exec the custom
#     checks. The library is bound at import; these loaders exploding proves nothing calls them. --


async def test_verify_calls_do_not_reload_the_rule_library(monkeypatch):
    import rules.engine as engine

    def _must_not_be_called(*_a, **_k):
        raise AssertionError("report.verify re-read the rules directory (#96 regression)")

    monkeypatch.setattr(engine, "load_yaml_rules", _must_not_be_called)
    monkeypatch.setattr(engine, "load_custom_checks", _must_not_be_called)
    for _ in range(2):
        out = await handle("report.verify", {
            "studyContext": SAMPLE_CONTEXT,
            "impression": {"impressionText": "No acute findings.",
                           "criticalFlags": [], "recommendations": []},
        })
        validate_skill_output("report.verify", out)
        assert out["verificationStatus"] == "PASS"
