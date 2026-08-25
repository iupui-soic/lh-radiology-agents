"""Negation window for the deterministic critical-finding scanners (#78).

The whole point of the module is what it must NOT flag (pertinent negatives) without going blind to
what it MUST flag (a real finding). The design bias is conservative -- ambiguous stays asserted --
because these scanners page physicians. These tests pin both directions and the #78 acceptance
cases; the regression blocks below pin every sentence that was REPRODUCED returning a false
result against an earlier cut of this module -- each of those is a real dictation shape.
"""
from radagent_common.negation import find_asserted_terms, scannable_text

TERMS = ("pneumothorax", "hemorrhage", "dissection", "embolism", "mass", "effusion")


def asserted(text):
    return find_asserted_terms(text, TERMS)


# --- #78 acceptance cases (the two that must hold) --------------------------------------------
def test_pertinent_negative_list_flags_nothing():
    # "No" governs the whole comma list -> zero criticalFlags, verification PASS.
    assert asserted("No pneumothorax, pleural effusion, or focal consolidation.") == []


def test_a_stated_positive_finding_is_flagged():
    assert asserted("Large right-sided pneumothorax.") == ["pneumothorax"]


# --- the dangerous direction: a real finding must NOT be suppressed ---------------------------
def test_adversative_after_a_negation_re_asserts_the_finding():
    # "no ... but a large pneumothorax" -> the pneumothorax is REAL; the "no" must not reach it.
    assert "pneumothorax" in asserted("No acute cardiopulmonary process, but a large pneumothorax.")


def test_a_negated_finding_and_a_separate_positive_one_in_the_same_report():
    out = asserted("No pneumothorax. There is a large pleural effusion.")
    assert "pneumothorax" not in out
    assert "effusion" in out


def test_second_positive_mention_wins_over_a_first_negated_one():
    assert "pneumothorax" in asserted("No small apical pneumothorax; large basal pneumothorax present.")


def test_positive_across_a_period_is_not_swallowed_by_an_earlier_negation():
    out = asserted("No pneumothorax. Aortic dissection is present.")
    assert out == ["dissection"]


# --- the annoying direction: pertinent negatives must be suppressed ---------------------------
def test_various_negation_cues_suppress():
    assert asserted("No evidence of pneumothorax.") == []
    assert asserted("Negative for pneumothorax.") == []
    assert asserted("Chest is free of pneumothorax.") == []
    assert asserted("No signs of pneumothorax.") == []
    assert asserted("Without pneumothorax or effusion.") == []


def test_post_negation_resolution_suppresses():
    assert asserted("Pneumothorax has resolved.") == []
    assert asserted("Pneumothorax is absent.") == []
    assert asserted("Pneumothorax not identified.") == []


def test_rule_out_indication_does_not_flag():
    # An indication "r/o pneumothorax" is a question, not a finding.
    assert asserted("Indication: rule out pneumothorax.") == []
    assert asserted("r/o dissection") == []


# --- word-boundary safety (unchanged from the old scanners, must still hold) ------------------
def test_mass_does_not_fire_on_massive():
    assert asserted("Massive consolidation without a discrete mass lesion.") == []
    assert asserted("There is a spiculated mass.") == ["mass"]


def test_empty_and_none_text_is_no_flags():
    assert asserted("") == []
    assert find_asserted_terms(None, TERMS) == []


# --- ordering + de-dup contract ---------------------------------------------------------------
def test_returns_terms_in_the_order_given_deduped():
    text = "Pneumothorax and pneumothorax again with a dissection."
    assert asserted(text) == ["pneumothorax", "dissection"]  # order of TERMS, deduped



# --- reproduced-false-result regressions, first sweep: each case returned the wrong answer -----

# "no <change/improvement/doubt> in the <finding>" suppressed a PRESENT, stable finding.
def test_no_change_in_a_finding_asserts_the_finding():
    assert asserted("No significant change in the large pneumothorax.") == ["pneumothorax"]
    assert asserted("No improvement in the pneumothorax.") == ["pneumothorax"]
    assert asserted("No significant interval change in the large right pneumothorax.") == ["pneumothorax"]
    assert "embolism" in asserted("Compared to prior there is no interval change of the known pulmonary embolism")
    assert asserted("There is no longer any doubt about the pneumothorax.") == ["pneumothorax"]


def test_no_directly_governing_the_finding_still_suppresses():
    # The meta-noun guard must not weaken the plain negative: here "no" DOES govern the finding.
    assert asserted("No significant pneumothorax.") == []
    assert asserted("No interval development of pneumothorax.") == []


# A leading "no"/"without" reached across commas and silenced a POSITIVE finding
# appended later in the same sentence.
def test_a_positive_statement_after_a_comma_is_not_swallowed_by_an_earlier_negation():
    assert asserted("No pleural effusion, large pneumothorax present.") == ["pneumothorax"]
    assert asserted("No consolidation, moderate hemorrhage identified.") == ["hemorrhage"]
    assert "pneumothorax" in asserted("Lungs clear without effusion, new large pneumothorax")
    assert "pneumothorax" in asserted("No focal consolidation, effusion stable, large left pneumothorax unchanged")
    assert asserted("No comparison available, pneumothorax is present.") == ["pneumothorax"]
    assert asserted("Without contrast, a large pneumothorax is seen.") == ["pneumothorax"]


def test_the_negated_list_shapes_still_suppress():
    # The comma-scope rule must not weaken the list forms the module exists for.
    assert asserted("No pneumothorax, pleural effusion, or focal consolidation.") == []
    assert asserted("No pneumothorax, pleural effusion, or focal consolidation is seen.") == []
    assert asserted("No pneumothorax is seen.") == []      # cue and term in the same segment


# A post-negation about the PRIOR study suppressed a finding that is present NOW.
def test_prior_study_negation_does_not_suppress_the_current_finding():
    assert asserted("Pneumothorax is not seen on the prior study but is now present.") == ["pneumothorax"]
    assert asserted("Pneumothorax not seen previously, now large.") == ["pneumothorax"]
    assert asserted("Pneumothorax which was excluded on prior CT is now present.") == ["pneumothorax"]
    assert asserted("Pneumothorax was excluded, but is now present.") == ["pneumothorax"]


# "cannot/unable to rule out X" is a live concern that must page -- it was suppressed as if
# it were the indication query form (a paging regression vs the pre-#78 scanners).
def test_hedged_rule_out_asserts():
    assert asserted("Cannot rule out dissection.") == ["dissection"]
    assert "pneumothorax" in asserted("Unable to rule out pneumothorax.")
    assert "dissection" in asserted(
        "Mediastinal widening. Cannot rule out dissection. CT angiography recommended emergently.")


def test_bare_rule_out_still_reads_as_the_indication_query():
    assert asserted("Indication: rule out pneumothorax.") == []
    assert asserted("r/o dissection") == []
    assert asserted("Pneumothorax was ruled out.") == []


def test_to_rule_out_reads_as_an_active_recommendation_and_pages():
    # The bare "to" hedge alternative is deliberate: "recommend CTA TO rule out X" is an active
    # recommendation about a live concern, not the indication query. Pinned so the documented
    # behavior cannot silently regress (remove the `|to` alternative and this fails).
    assert asserted("Recommend CTA to rule out dissection.") == ["dissection"]
    assert "embolism" in asserted("Further imaging advised to rule out pulmonary embolism.")


# The scanners read the INDICATION/HISTORY sections, where the SUSPICION is named.
def test_scannable_text_drops_indication_history_technique_comparison():
    narrative = (
        "INDICATION: Chest pain, evaluate for pneumothorax.\n"
        "TECHNIQUE: PA and lateral.\n"
        "COMPARISON: Prior CXR with small pneumothorax.\n"
        "FINDINGS: Lungs are clear.\n"
        "IMPRESSION: No pneumothorax."
    )
    scoped = scannable_text(narrative)
    assert "evaluate for" not in scoped
    assert "PA and lateral" not in scoped
    assert "Prior CXR" not in scoped
    assert "Lungs are clear" in scoped and "No pneumothorax" in scoped
    assert find_asserted_terms(scoped, TERMS) == []


def test_scannable_text_without_headers_is_the_whole_text():
    # Absence of structure must not reduce safety: a bare narrative stays fully scanned.
    assert scannable_text("Large right-sided pneumothorax.") == "Large right-sided pneumothorax."
    assert scannable_text("") == ""


def test_scannable_text_keeps_the_preamble_before_the_first_header():
    narrative = "Large pneumothorax.\nTECHNIQUE: portable AP."
    assert "Large pneumothorax." in scannable_text(narrative)


def test_finding_dictated_after_a_trailing_skip_header_is_still_scanned():
    """Reproduced under-flag: a short report with skip headers but NO FINDINGS header dictates
    the finding as bare trailing text. Positionally it sits inside the last skip section, and
    dropping it to end-of-text silenced a tension pneumothorax that an unscoped scan flagged --
    the exact failure class this module must never introduce. A skip section owns only its header
    line (plus hard-wrap continuations); a new sentence on a new line is scanned."""
    narrative = (
        "INDICATION: Trauma.\n"
        "COMPARISON: None available.\n"
        "Large right tension pneumothorax with mediastinal shift."
    )
    assert find_asserted_terms(scannable_text(narrative), TERMS) == ["pneumothorax"]

    # Same shape with the skip header in the MIDDLE of the report.
    narrative2 = (
        "FINDINGS: Limited exam.\n"
        "TECHNIQUE: Portable AP.\n"
        "Large right pneumothorax is new."
    )
    assert find_asserted_terms(scannable_text(narrative2), TERMS) == ["pneumothorax"]


def test_hard_wrapped_skip_section_content_stays_skipped():
    """The clamp must not undo the skip's purpose: a MIMIC-style hard-wrapped indication whose
    sentence continues onto the next line is still the indication -- 'concern for pneumothorax'
    is a suspicion, not a finding, wherever the line breaks."""
    narrative = (
        "INDICATION: 55M with chest pain, concern for\n"
        "pneumothorax after fall.\n"
        "FINDINGS: Lungs are clear."
    )
    scoped = scannable_text(narrative)
    assert "concern for" not in scoped and "after fall" not in scoped
    assert find_asserted_terms(scoped, TERMS) == []


# --- reproduced-false-result regressions, second sweep: found against the once-fixed module ----

def test_hedged_rule_out_with_intervening_adverbs_asserts():
    assert asserted("Cannot completely rule out dissection.") == ["dissection"]
    assert "embolism" in asserted("Cannot entirely rule out pulmonary embolism.")
    assert "dissection" in asserted("Unable to definitively rule out dissection.")
    assert "pneumothorax" in asserted("Cannot reliably rule out a small pneumothorax.")
    assert "hemorrhage" in asserted("Difficult to completely rule out hemorrhage.")


def test_existence_presupposing_comparison_nouns_assert():
    assert asserted("No interval enlargement of the known mediastinal mass.") == ["mass"]
    assert "pneumothorax" in asserted("No significant increase in the size of the large pneumothorax.")
    assert asserted("No interval resolution of the pneumothorax.") == ["pneumothorax"]
    assert "effusion" in asserted("No interval decrease in the moderate pleural effusion.")
    assert "dissection" in asserted("No extension of the known type B dissection.")
    assert "mass" in asserted("No appreciable growth of the right renal mass.")
    assert "embolism" in asserted("No interval migration of the known pulmonary embolism.")
    # qualifier window: three qualifiers, and a hyphenated one
    assert "pneumothorax" in asserted("No definite significant interval change in the pneumothorax.")
    assert "pneumothorax" in asserted("No short-term interval change in the large pneumothorax.")


def test_new_finding_appearance_negations_still_suppress():
    # "development"/"evidence" denote APPEARANCE of something new -- these negate the finding.
    assert asserted("No interval development of pneumothorax.") == []
    assert asserted("No evidence of pneumothorax.") == []


def test_hedged_concern_after_a_comma_asserts():
    assert asserted("No acute fracture, findings concerning for pulmonary embolism.") == ["embolism"]
    assert "mass" in asserted("No pneumothorax, suspicious mass in the right upper lobe.")
    assert "embolism" in asserted("No consolidation, findings compatible with pulmonary embolism.")
    assert "dissection" in asserted("No fracture, findings worrisome for aortic dissection.")
    assert asserted("Negative for dissection, positive for pulmonary embolism.") == ["embolism"]
    assert "pneumothorax" in asserted("No consolidation, probable small pneumothorax.")
    assert "hemorrhage" in asserted("No effusion, likely small hemorrhage in the right frontal lobe.")


def test_sized_or_measured_finding_after_a_comma_asserts():
    # Nobody writes a size on a finding they are negating.
    assert asserted("No pleural effusion, 2 cm right apical pneumothorax.") == ["pneumothorax"]
    assert "effusion" in asserted("No pneumothorax, small left pleural effusion.")
    assert "effusion" in asserted("No pneumothorax, moderate right pleural effusion.")
    assert "pneumothorax" in asserted("No effusion, trace pneumothorax at the apex.")


def test_and_plus_full_statement_after_a_comma_asserts():
    assert asserted("No effusion, and a large pneumothorax is present.") == ["pneumothorax"]
    assert "effusion" in asserted("No pneumothorax, and there is a moderate left pleural effusion.")
    # but a bare and-tail is still a negated list
    assert asserted("No pneumothorax, effusion, and consolidation.") == []


def test_post_negation_does_not_jump_the_comma_into_the_next_noun_phrase():
    out = asserted("Large left pneumothorax, resolved right pleural effusion.")
    assert "pneumothorax" in out  # the "resolved" belongs to the NEXT finding, not this one


def test_persistent_and_redemonstrated_count_as_assertions():
    assert asserted("No new consolidation, persistent left pleural effusion.") == ["effusion"]
    assert "effusion" in asserted("No pneumothorax, persistent moderate effusion.")
    assert "mass" in asserted("No effusion, redemonstration of the known right hilar mass.")


def test_dash_joined_positive_after_a_negative_asserts():
    assert asserted("No acute fracture -- large pneumothorax present.") == ["pneumothorax"]
    assert asserted("No fracture - large pneumothorax present.") == ["pneumothorax"]
    # unspaced hyphens are not boundaries: ranges and compounds stay intact
    assert asserted("No fracture of T10-T12.") == []


def test_not_seen_to_verb_negates_the_growth_not_the_finding():
    assert asserted("The pneumothorax is not seen to have enlarged.") == ["pneumothorax"]
    assert "pneumothorax" in asserted("Pneumothorax is not seen to extend to the base.")
    assert asserted("Pneumothorax is not seen.") == []  # the plain absence form still suppresses


def test_unknown_caps_subheader_terminates_a_skip_section():
    narrative = ("HISTORY: Fall from ladder.\n"
                 "CHEST: Large right pneumothorax with mediastinal shift.\n"
                 "IMPRESSION: See above.")
    scoped = scannable_text(narrative)
    assert "Fall from ladder" not in scoped
    assert "Large right pneumothorax" in scoped
    assert find_asserted_terms(scoped, TERMS) == ["pneumothorax"]


# --- reproduced-false-result regressions, third sweep -------------------------------------------

def test_plural_and_quantified_findings_match():
    assert asserted("Bilateral pulmonary emboli are present.") == ["embolism"]
    assert asserted("Multifocal intraparenchymal hemorrhages.") == ["hemorrhage"]
    assert asserted("Bilateral pneumothoraces.") == ["pneumothorax"]
    assert "mass" in asserted("Multiple bilateral pulmonary masses, concerning for metastatic disease.")
    assert asserted("Bilateral pleural effusions.") == ["effusion"]
    # and the negated plural still suppresses
    assert asserted("No pleural effusions or pneumothoraces.") == []


def test_exceptive_connectors_assert_the_exception():
    assert asserted("No acute intracranial abnormality with the exception of a small subdural hemorrhage.") == ["hemorrhage"]
    assert asserted("No acute osseous abnormality other than a small right apical pneumothorax.") == ["pneumothorax"]
    assert asserted("No acute abnormality apart from a moderate left pleural effusion.") == ["effusion"]


def test_a_parenthetical_negation_does_not_govern_the_main_finding():
    assert asserted(
        "Portable chest radiograph (no previous imaging available) demonstrates a large right tension pneumothorax."
    ) == ["pneumothorax"]
    out = asserted("Large right pneumothorax (no prior imaging) and a moderate left pleural effusion.")
    assert "pneumothorax" in out and "effusion" in out
    # a negation WITH its term inside the parenthetical still suppresses
    assert asserted("Lungs clear (no pneumothorax).") == []


def test_commaless_and_statement_is_re_anchored():
    assert asserted("No pleural effusion and a large right pneumothorax is present.") == ["pneumothorax"]
    assert "embolism" in asserted(
        "The aorta is intact without evidence of dissection and there is a large central pulmonary embolism.")
    # a bare and-list stays negated
    assert asserted("No pneumothorax and effusion.") == []


def test_hard_line_wrap_does_not_break_a_hedge():
    assert "dissection" in asserted("Mediastinal widening. Cannot\nrule out aortic dissection.")


def test_colon_bounds_negation_scope():
    assert "embolism" in asserted("CTA negative for dissection: incidental pulmonary embolism in the right lower lobe.")


def test_hedge_survives_intervening_punctuation():
    assert asserted("Cannot, however, rule out dissection.") == ["dissection"]


def test_unspaced_double_dash_is_a_boundary():
    assert asserted("No acute fracture--large pneumothorax present.") == ["pneumothorax"]
    assert asserted("No fracture of T10-T12.") == []  # single unspaced hyphen still is not


def test_wider_meta_noun_window_and_reduction():
    assert asserted("No significant short term interval change in the large pneumothorax.") == ["pneumothorax"]
    assert asserted("No interval reduction in the moderate pleural effusion.") == ["effusion"]


def test_template_header_carrying_the_finding_is_kept():
    narrative = ("FINDINGS:\nPNEUMOTHORAX: Large, under tension, with mediastinal shift.\nEFFUSION: None.")
    scoped = scannable_text(narrative)
    assert "pneumothorax" in scoped.lower()
    hits = find_asserted_terms(scoped, TERMS)
    assert "pneumothorax" in hits  # the finding named BY the template header must be seen
    # Known residual (tolerated direction): "EFFUSION: None." over-flags because the colon bounds
    # scope, so the "None" cannot reach back across it. An over-flag a human dismisses; the
    # under-flag above is the failure class that matters.


def test_title_case_subheader_terminates_a_skip_section():
    narrative = ("History: Fall from ladder.\nChest: Large right pneumothorax.\nImpression: See above.")
    scoped = scannable_text(narrative)
    assert "Fall from ladder" not in scoped
    assert find_asserted_terms(scoped, TERMS) == ["pneumothorax"]


def test_dash_style_header_is_recognized():
    narrative = "INDICATION: Trauma.\nFINDINGS - Large right pneumothorax."
    scoped = scannable_text(narrative)
    assert "Trauma" not in scoped
    assert find_asserted_terms(scoped, TERMS) == ["pneumothorax"]


# --- a realistic normal MIMIC-style CXR report is fully silent --------------------------------
def test_a_realistic_normal_cxr_report_flags_nothing():
    report = (
        "FINDINGS: The lungs are clear without focal consolidation, pleural effusion, or "
        "pneumothorax. The cardiomediastinal silhouette is normal. No acute osseous abnormality.\n"
        "IMPRESSION: No acute cardiopulmonary process. No pneumothorax."
    )
    assert asserted(report) == []


def test_a_realistic_positive_cxr_report_flags_the_finding():
    report = (
        "FINDINGS: There is a large right-sided pneumothorax with mediastinal shift.\n"
        "IMPRESSION: Large tension pneumothorax; recommend immediate decompression."
    )
    assert "pneumothorax" in asserted(report)


# --- KNOWN RESIDUALS of the skip-clamp's terminal-punctuation proxy -------------------
# These two tests pin CURRENT behaviour, not desired behaviour (module policy: "fix if
# mechanical, otherwise document"). Both are strictly smaller errors than the pre-clamp
# swallow-to-next-header. If either assertion starts failing, the heuristic changed --
# re-decide the trade-off deliberately, don't just flip the assert.

def test_residual_a_skip_header_that_ends_no_sentence_swallows_the_continuation_chain():
    """UNDER-flag residual: "COMPARISON: chest CT 2024" has no terminal ./; so following lines
    read as hard-wrap continuations of the skip section until one completes a sentence -- a
    finding dictated there is silenced, and a HARD-WRAPPED finding is swallowed whole, both
    lines (the pre-#78-fix behaviour silenced to end-of-text; this ends at the first ./;)."""
    report = ("FINDINGS: Lungs clear.\n"
              "COMPARISON: chest CT 2024\n"
              "Large right pneumothorax is new.")
    assert "pneumothorax" not in scannable_text(report).lower()  # documented residual
    wrapped = ("FINDINGS: Lungs clear.\n"
               "COMPARISON: chest CT 2024\n"
               "Large right tension\n"
               "pneumothorax is new.")
    assert scannable_text(wrapped).strip() == "Lungs clear."     # the whole chain, not one line


def test_residual_later_sentences_of_a_multiline_skip_paragraph_leak_and_flag():
    """OVER-flag residual (the tolerated direction, but a REAL false page): line 2 of a
    TECHNIQUE paragraph whose line 1 ends in "." is scanned, and a rule-out phrase there
    ASSERTS -- "to exclude" is not a negation cue and the hedge rule voids suppression rather
    than adding it. A normal study wraps a rule-out onto line 2 -> it gets flagged."""
    report = ("TECHNIQUE: Portable AP radiograph.\n"
              "Obtained to exclude pneumothorax.\n"
              "FINDINGS: Lungs clear.")
    leaked = scannable_text(report)
    assert "exclude pneumothorax" in leaked.lower()           # the leak, pinned
    assert find_asserted_terms(leaked, ["pneumothorax"]) == [
        "pneumothorax"]                                       # ...and it FLAGS (plain terms,
    # exactly how both scanners call this function -- a regex passed as a term is re.escape()d
    # and can never match, which is how the first cut of this pin managed to assert nothing)


# --- #131: a narrative with no line breaks defeated the skip clamp ---------

def test_a_skip_section_stops_at_its_first_sentence_even_with_no_line_breaks():
    """#131. _clamped_skip_end consumed lines[0] whole, so a narrative with NO newline had
    nothing to stop at and the skip swallowed to the next header -- the pre-clamp behaviour
    this function exists to prevent. The RIS path produces exactly that shape: TinyMCE bodies
    are <p>-delimited, and strip_html replaces tags with a SPACE, so a signed report reaches
    fhir2 as one line."""
    one_line = ("FINDINGS: Interval change. IMPRESSION: Limited by technique: portable AP film. "
                "Large left pneumothorax is present.")
    assert "pneumothorax" in scannable_text(one_line)


def test_the_multi_line_path_still_drops_a_hard_wrapped_skip_section():
    """The clamp's original job, unchanged: a skip header line that does not finish a sentence
    still consumes its hard-wrap continuation."""
    wrapped = ("COMPARISON: chest CT dated\n2024 with no acute finding\n"
               "FINDINGS: Large left pneumothorax is present.")
    out = scannable_text(wrapped)
    assert "pneumothorax" in out, "the FINDINGS section must still be scanned"
    assert "2024 with no acute finding" not in out, "the continuation is still the skip's prose"


def test_a_genuine_indication_is_still_dropped_on_one_line():
    """The safe direction must not invert: the indication names the SUSPICION, not a finding,
    and re-flagged every normal study before it was skipped (#78)."""
    normal = ("INDICATION: Rule out pneumothorax after line placement. "
              "FINDINGS: Lungs clear. IMPRESSION: No acute cardiopulmonary process.")
    out = scannable_text(normal)
    assert "Rule out pneumothorax" not in out, "the indication must stay dropped"


def test_a_semicolon_ends_a_skip_clause_the_same_as_a_full_stop():
    """The multi-line clamp has always treated "." and ";" alike; the single-line clamp must
    agree, or the same dictation is scanned or dropped depending on its punctuation."""
    one_line = ("COMPARISON: none available; Large left pneumothorax is present. "
                "IMPRESSION: See findings.")
    assert "pneumothorax" in scannable_text(one_line)


def test_a_completed_sentence_stops_the_skip_before_its_wrap_chain():
    """The first line may CONTAIN a sentence end without ENDING on one. The clamp stops at that
    sentence; it must not then go on consuming hard-wrap continuations, or the finding sitting
    on the next line is swallowed by a section that already finished."""
    wrapped = ("TECHNIQUE: portable AP film. Large left pneumothorax\n"
               "is present and under tension\n"
               "IMPRESSION: See findings.")
    out = scannable_text(wrapped)
    assert "pneumothorax" in out, "the skip ended at its first sentence; the rest is scanned"
