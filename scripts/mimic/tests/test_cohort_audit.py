"""Tests for cohort_audit.classify (#105).

The classifier is the whole value of the audit: it is what turned "the demo looks fine" into
"10 studies carry rehearsal text and 3 lost their IMPRESSION". Pure function, no fhir2 needed.
"""
from __future__ import annotations

import cohort_audit as audit

MINE = ("FINDINGS: The lungs are clear without focal consolidation. "
        "IMPRESSION: No acute cardiopulmonary process.")
OTHER = ("FINDINGS: Small right apical pneumothorax is present. "
         "IMPRESSION: Persistent tiny right apical pneumothorax.")
BY_TEXT = {audit.norm(MINE): ["s1"], audit.norm(OTHER): ["s2"]}


def test_exact_match_is_ok():
    assert audit.classify(MINE, MINE, BY_TEXT, "s1") == "OK"


def test_whitespace_and_html_differences_are_still_ok():
    stored = "<p>FINDINGS: The lungs are clear without focal   consolidation.<br/>" \
             "IMPRESSION: No acute cardiopulmonary process.</p>"
    assert audit.classify(stored, MINE, BY_TEXT, "s1") == "OK"


def test_prefix_of_own_text_is_truncated():
    assert audit.classify(MINE[:60], MINE, BY_TEXT, "s1") == "TRUNCATED"


def test_another_studys_narrative_is_crossed():
    # The failure the sweep was written for: the sign-bridge projected one study's signed body
    # over another study's seeded report, so the chart read as somebody else's findings.
    assert audit.classify(OTHER, MINE, BY_TEXT, "s1") == "CROSSED->s2"


def test_truncated_copy_of_another_study_is_still_crossed():
    stored = OTHER[:70] + " [TRUNCATED BY ris-sign-bridge: fhir2 caps conclusion at 1024 chars]"
    assert audit.classify(stored, MINE, BY_TEXT, "s1") == "CROSSED->s2"


def test_rehearsal_placeholder_is_diverged():
    assert audit.classify("test - text", MINE, BY_TEXT, "s1") == "DIVERGED"


def test_empty_stored_is_diverged():
    assert audit.classify("", MINE, BY_TEXT, "s1") == "DIVERGED"
