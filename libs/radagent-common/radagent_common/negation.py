"""Negation-aware critical-term detection for the deterministic safety scanners (#78).

Two scanners derive `criticalFlags` / the unflagged-critical WARN deterministically -- the impression
agent's keyword scan and report-verification's `critical_finding_unflagged` rule. They are the
audit-able, model-free trigger that FAILs `critical-comm-required`, opens the sign-off ladder, and
pages physicians, and the fallback whenever the LLM impression path is off or down. Both had the
same blind spot: no negation handling. A real NORMAL chest report is written as pertinent negatives
-- "No pneumothorax, pleural effusion, or focal consolidation" -- and a bare `\\bpneumothorax\\b`
match on that text flags every normal study, FAILs verification, and parks the whole cohort at the
sign-off gate paging the ladder.

This module is the shared negation window. It lives in the shared lib, NOT in either agent, because
Golden rule 4 forbids one agent importing another -- but each agent keeps its OWN critical
vocabulary (they differ on purpose) and passes it in here. Pure and deterministic: same text in ->
same terms out, no I/O, no clock (Golden rule 5), so a flag is reproducible from the report alone.

DESIGN BIAS: conservative suppression. These scanners page people, so a FALSE NEGATIVE (missing a
real finding) is far worse than a FALSE POSITIVE (an over-flag a human dismisses). Suppression
therefore needs a TIGHT, explicit reading; anything ambiguous stays ASSERTED. This is regex-grade
negation tuned for radiology dictation / MIMIC report style, deliberately NOT clinical NLP.

RESIDUAL POLICY (#78's own acceptance model: "fix or consciously accept; residuals go in the
run-book"). Every scope rule below exists because a realistic dictation sentence was REPRODUCED
returning the wrong result against an earlier cut of this module; each such sentence is pinned in
tests/test_negation.py. Regex-grade negation has a long tail by construction; the tail that
remains is accepted as residual and its SYSTEMATIC measurement is the #68 cohort-wide validation
table (build item 2), not further sentence-by-sentence patching. When a residual is found: add the
sentence to the test file, fix if mechanical, otherwise document it in the run-book. The invariant
that must never regress: an over-flag is tolerated, an under-flag (a present finding silenced) is
the failure class that matters.

THE SCOPE MODEL:

  * Clause = bounded by sentence punctuation ([.;:]), parentheses, and spaced or doubled dashes
    (see _BOUNDARY). NEWLINES ARE NOT boundaries -- reports hard-wrap mid-sentence, so
    find_asserted_terms collapses them to spaces first. Negation never crosses a clause.
  * Within a clause, a PRE-cue ("no", "without", ...) governs its own COMMA-SEGMENT fully -- that is
    what keeps "no A, B, or C" suppressing B and C -- but reaches a LATER segment only if that
    segment still reads as part of the list: it starts with or/and/nor, or it is a bare noun phrase.
    A later segment that ASSERTS ("..., large pneumothorax PRESENT", "..., moderate hemorrhage
    IDENTIFIED") is a new statement, and the finding is real.
  * A cue that governs a COMPARISON/META noun is not negating the finding: "no significant CHANGE in
    the large pneumothorax" asserts a stable, present pneumothorax. The cue is voided.
  * A HEDGED rule-out asserts: "CANNOT rule out dissection" is a live concern that must page, not a
    negation. Bare "rule out pneumothorax" (the indication/query form) still suppresses.
  * A POST-cue ("...has resolved", "...not identified") is voided when it refers to a PRIOR study
    ("not seen on the PRIOR study") or is overturned later in the clause ("not seen previously BUT
    is now PRESENT") -- mirror of the pre-side adversative reset.
"""
from __future__ import annotations

import re
from typing import Iterable

# A clause/sentence boundary. Negation never reaches across one of these. A period inside a
# measurement ("1.5 cm") can over-split, but a critical term rarely sits mid-decimal, and
# over-splitting only ever makes the scanner MORE likely to assert (safe direction). Boundaries:
#   * sentence punctuation [.;:] -- the colon bounds scope too ("negative for dissection:
#     incidental pulmonary embolism" reports the embolism);
#   * parentheses -- an aside is its own scope ("(no previous imaging available)" must not
#     negate the finding the sentence exists to report);
#   * a spaced dash or an unspaced DOUBLE dash ("No acute fracture--large pneumothorax present");
#     a single unspaced hyphen is a compound/range ("T10-T12", "follow-up") and is not.
# NEWLINES ARE NOT BOUNDARIES: MIMIC-style reports hard-wrap mid-sentence ("Cannot\nrule out
# aortic dissection"), so find_asserted_terms collapses them to spaces before splitting.
_BOUNDARY = re.compile(r"[.;:()\[\]]|\s[-–—]{1,2}\s|[-–—]{2,}")

# Adversative / polarity-flip cues. A pre-negation before one of these does NOT reach a term after
# it: "no acute process BUT a large pneumothorax" asserts the pneumothorax. EXCEPTIVE connectors
# belong here too -- "no acute abnormality WITH THE EXCEPTION OF a small subdural hemorrhage" /
# "OTHER THAN a small apical pneumothorax" assert the exception, which is the sentence's only
# content ("exception of" is spelled out because \bexcept\b cannot match "exception").
_ADVERSATIVE = re.compile(
    r"\b(?:but|however|although|though|except|exception\s+of|other\s+than|apart\s+from|"
    r"save\s+for|aside\s+from|otherwise|rather|yet)\b", re.I)

# PRE-negation: a cue BEFORE the term negates it (subject to the scope rules below). Kept tight on
# purpose (see module docstring). "rule out"/"r/o" is the indication-context query form
# ("r/o pneumothorax" is a question, not a finding). Bare "not" is deliberately excluded -- it is
# too broad ("not only a pneumothorax but...") and would suppress real findings.
# ORDER IS LOAD-BEARING: re alternation is first-listed-wins at a given position, so the longer
# "no evidence of / no signs of" phrases are listed BEFORE the bare "no" and win -- which matters
# because the cue's END feeds the meta-noun {0,4}-qualifier window in _cue_is_void. The effect
# shows once the long cue's words would exhaust that window: "no evidence of definite significant
# interval change in the pneumothorax" voids (asserts the pneumothorax) with the long cue, but
# under bare-"no"-first the five intervening words overrun the window and the sentence would
# wrongly suppress. Either way a plain negative suppresses; the long forms exist so the window
# starts after the whole phrase.
_PRE_NEGATION = re.compile(
    r"\b(?:no\s+evidence\s+(?:of|for)|no\s+(?:sign|signs)\s+of|no|without|negative\s+for|"
    r"free\s+of|absence\s+of|rule\s+out|ruled\s+out|r/o)\b", re.I)

# A cue governing a COMPARISON/META noun is not negating a finding: "no significant CHANGE in the
# large pneumothorax" states the (present) pneumothorax is stable; "no interval ENLARGEMENT of the
# known mass" presupposes the mass; "no longer any DOUBT about the pneumothorax" asserts it
# outright. The nouns here all PRESUPPOSE the finding exists (its change/size/resolution is what is
# being negated) -- which is exactly why "development"/"evidence" are NOT here: "no interval
# development of pneumothorax" negates the finding itself. Up to four qualifier words (hyphens
# included: "short-term") may sit between the cue and the meta noun.
_META_AFTER_CUE = re.compile(
    r"^\s*(?:[\w-]+\s+){0,4}(?:change|changes|improvement|worsening|progression|regression|"
    r"difference|doubt|comparison|enlargement|increase|decrease|reduction|resolution|extension|"
    r"growth|migration|displacement)\b", re.I)

# A HEDGE before a rule-out cue flips it into an assertion of live concern: "CANNOT rule out aortic
# dissection" must page -- and so must "cannot COMPLETELY rule out" and "cannot, HOWEVER, rule out":
# up to two adverb/qualifier words (commas attached or not) may sit between the hedge and the cue.
# The bare "to" alternative is deliberate: "recommend CTA TO rule out dissection" is an active
# recommendation about a live concern, not the bare indication query, and over-flagging it is the
# tolerated direction. Checked against the 40 chars before the cue.
_HEDGE_BEFORE_RULEOUT = re.compile(
    r"\b(?:cannot|can\s*not|can't|unable\s+to|difficult\s+to|impossible\s+to|not\s+(?:possible|able)\s+to|"
    r"failed?\s+to|to)[,;]?\s+(?:[\w-]+[,;]?\s+){0,2}$|\b(?:cannot|can\s*not|can't)\s*$", re.I)
_RULEOUT_CUE = re.compile(r"^(?:rule\s+out|ruled\s+out|r/o)$", re.I)

# POST-negation: a cue just AFTER the term negates it ("pneumothorax has resolved", "pneumothorax is
# absent", "pneumothorax not identified"). Allow a few filler/linking words between the term and the
# cue, but no further -- a distant "resolved" belongs to a different finding. Two hard limits, each
# from a reproduced false negative:
#   * the window never crosses a COMMA: in "Large left pneumothorax, resolved right effusion" the
#     "resolved" heads the NEXT finding's noun phrase, not this one's;
#   * "not seen" is not a negation when it heads an infinitive: "not seen TO have enlarged" negates
#     the growth, not the finding.
_POST_NEGATION = re.compile(
    r"^[^\w,;.]*(?:(?:is|are|was|were|has|have|had|been|now|appears?|seen|identified|which|that)\s+){0,3}"
    r"(?:resolved|ruled\s+out|excluded|absent|none|not\s+seen(?!\s+to\b)|not\s+identified|not\s+present)\b", re.I)

# A post-negation that talks about a PRIOR study is not negating the CURRENT one: "not seen on the
# prior study", "excluded on the preoperative CT". Checked in the clause tail after the term.
_PRIOR_REFERENCE = re.compile(r"\b(?:prior|previous|previously|preoperative|earlier|preceding)\b", re.I)

# Tokens that mark a comma-segment as a NEW STATEMENT about a present finding rather than a
# continuation of a negated list. Three families, each forced by a reproduced false negative:
#   * presence/copula verbs ("..., large pneumothorax PRESENT", "..., moderate hemorrhage
#     IDENTIFIED") including persistence/re-demonstration stems ("persistent", "redemonstration");
#   * HEDGED-CONCERN vocabulary ("..., findings CONCERNING for PE", "POSITIVE for embolism") --
#     the live-concern phrasings are precisely the ones that must page;
#   * SIZE/measurement ("..., 2 CM right apical pneumothorax", "..., TRACE pneumothorax") --
#     nobody writes a size on a finding they are negating.
# Also used to overturn a post-negation after an adversative ("not seen previously, but NOW LARGE").
_ASSERTION = re.compile(
    r"\b(?:is|are|was|were|present|persist\w*|remains?|remained|seen|identified|noted|"
    r"(?:re)?demonstrat\w*|visuali[sz]\w*|stable|unchanged|new|now|again|residual|increased|"
    r"increasing|enlarging|enlarged|worsened|worsening|large[rst]?|small|moderate|trace|tiny|"
    r"minimal|extensive|massive|concerning|suspicious|worrisome|compatible|consistent|suggestive|"
    r"positive|probable|likely)\b|\d", re.I)

# A negated-list tail: "no A, B, OR C is seen" keeps C negated even though its segment carries an
# assertion verb -- the verb belongs to the whole negated disjunction. ONLY or/nor: an "and" that
# introduces a full asserting statement ("no effusion, AND a large pneumothorax IS PRESENT") is a
# new clause and falls through to the assertion check (a bare "no A, B, and C" still has no
# assertion token, so it stays a negated list).
_LIST_TAIL = re.compile(r"^\s*(?:or|nor)\b", re.I)

# Segment dividers inside a clause: a comma, or a bare "and" -- transcription drops the comma
# ("No pleural effusion and a large right pneumothorax is present"), and the and-joined statement
# must get the same assertion re-anchoring the comma form gets.
_SEGMENT_DIV = re.compile(r",|\band\b", re.I)

# Plural morphology: findings are routinely dictated quantified-plural ("bilateral pulmonary
# EMBOLI", "multifocal HEMORRHAGES", "bilateral PNEUMOTHORACES", "multiple MASSES") and an
# exact-singular \b<term>\b is blind to the whole class. Regular plurals get (e?s)?; the two
# irregulars in the scanners' vocabularies are spelled out.
_IRREGULAR_PLURALS = {
    "embolism": r"embolism|embolisms|emboli",
    "pneumothorax": r"pneumothorax|pneumothoraces",
}


def _term_pattern(term: str) -> "re.Pattern[str]":
    t = str(term).lower()
    body = _IRREGULAR_PLURALS.get(t, rf"{re.escape(t)}(?:e?s)?")
    return re.compile(rf"\b(?:{body})\b")


def _split_clauses(text: str) -> list[str]:
    """Break text into clauses on sentence boundaries. Commas are NOT clause boundaries -- comma
    scope is handled segment-wise inside _is_asserted, so a pertinent-negative list ("no A, B, or
    C") keeps its leading "no" while an appended new statement ("..., large B present") does not."""
    return _BOUNDARY.split(text)


def _cue_is_void(clause: str, cue: re.Match) -> bool:
    """A matched pre-cue that is not actually negating a downstream finding."""
    after = clause[cue.end():]
    if _META_AFTER_CUE.search(after):
        return True  # "no <significant> change/improvement/doubt ..." -- the finding is present
    if _RULEOUT_CUE.match(cue.group(0).strip()) and _HEDGE_BEFORE_RULEOUT.search(clause[max(0, cue.start() - 40):cue.start()]):
        return True  # "cannot/unable to ... rule out <finding>" -- a live concern, must page
    return False


def _pre_negated(clause: str, start: int) -> bool:
    """Does an effective pre-cue govern the term starting at `start` in this clause?"""
    pre = clause[:start]
    # An adversative resets polarity: only cues AFTER the last adversative can govern the term.
    breaks = list(_ADVERSATIVE.finditer(pre))
    scope_from = breaks[-1].end() if breaks else 0

    cues = [c for c in _PRE_NEGATION.finditer(clause) if scope_from <= c.start() < start
            and not _cue_is_void(clause, c)]
    if not cues:
        return False

    # Segment scope. A cue governs its own segment fully; it reaches a LATER segment only if that
    # segment still reads as part of the negated list (starts with or/nor, or asserts nothing).
    # Segments are bounded by commas OR a bare "and" (transcription drops the comma), on BOTH
    # sides: in "no A, B, or C is seen" the "is seen" belongs to C's segment, and must not leak
    # into B's and make B look like a new positive statement.
    last_cue = cues[-1]
    pre_divs = [m.end() for m in _SEGMENT_DIV.finditer(clause, 0, start)]
    segment_start = pre_divs[-1] if pre_divs else 0
    if last_cue.start() >= segment_start:
        return True  # cue inside the term's own segment -- "no pneumothorax is seen" stays negated
    nxt_div = _SEGMENT_DIV.search(clause, start)
    segment = clause[segment_start: nxt_div.start() if nxt_div else len(clause)]
    if _LIST_TAIL.match(segment):
        return True  # "..., or focal consolidation is seen" -- the coordinator keeps it in the list
    # A cross-segment reach that ASSERTS is a new positive statement: "..., large pneumothorax
    # present". One that stays bare is a list item: "no A, B" keeps B negated.
    return not _ASSERTION.search(segment)


def _post_negated(clause: str, end: int) -> bool:
    """Does a post-cue negate the term ending at `end` -- about THIS study, and not overturned?"""
    post = clause[end:]
    nxt = _ADVERSATIVE.search(post)
    post_window = post[: nxt.start()] if nxt else post
    if not _POST_NEGATION.search(post_window):
        return False
    if _PRIOR_REFERENCE.search(post_window):
        return False  # "not seen on the PRIOR study" negates the past, not this study
    if nxt and _ASSERTION.search(post[nxt.end():]):
        return False  # "...not seen previously, BUT is now PRESENT" -- overturned downstream
    return True


def _is_asserted(clause: str, start: int, end: int) -> bool:
    """Is the term at [start, end) in this clause ASSERTED (present), not negated?"""
    return not _pre_negated(clause, start) and not _post_negated(clause, end)


def find_asserted_terms(text: str, terms: Iterable[str]) -> list[str]:
    """The subset of `terms` that appear ASSERTED (present and not negated) in `text`.

    Returns them in the ORDER `terms` is given (so a caller's flag order is stable), de-duplicated.
    A term counts as asserted if ANY single occurrence of it is asserted -- "no small pneumothorax
    but a large pneumothorax remains" asserts pneumothorax on the second mention. Matching is
    word-boundary and case-insensitive ("mass" never fires on "massive").
    """
    # Newlines collapse to spaces BEFORE clause-splitting: MIMIC-style reports hard-wrap mid-
    # sentence ("Cannot\nrule out aortic dissection"), and treating the wrap as a boundary strands
    # the hedge in the previous clause and turns a live concern into a suppressed indication query.
    low = (text or "").lower().replace("\n", " ")
    if not low.strip():
        return []
    clauses = _split_clauses(low)
    out: list[str] = []
    for term in terms:
        pat = _term_pattern(term)  # singular AND plural forms ("emboli", "pneumothoraces", ...)
        if any(_is_asserted(clause, m.start(), m.end())
               for clause in clauses for m in pat.finditer(clause)):
            out.append(term)
    return out



# --- narrative sectioning for the scanners ------------------------------------------------------
# A production fhir2 `conclusion` carries the full sectioned narrative (INDICATION: / FINDINGS: /
# IMPRESSION: ... -- the same field report-verification's report_body.py parses). The INDICATION /
# HISTORY section names the SUSPICION ("evaluate for pneumothorax", "concern for PE"), which is not
# a finding: scanning it re-flags every normal study and defeats the entire point of #78. The
# COMPARISON section describes the PRIOR study, and TECHNIQUE the acquisition. So the scanners read
# only the result-bearing sections when headers are present -- and the whole text when they are not
# (a bare unstructured conclusion stays fully scanned; absence of headers must not reduce safety).
# Vocabulary mirrors report_body.py's headers; report-verification keeps its own richer parser for
# the PI rules (Golden rule 4 -- this helper exists so the impression agent does not need it).

_SCAN_SECTIONS = frozenset({"findings", "impression", "conclusion", "recommendation", "recommendations"})
_SKIP_SECTIONS = frozenset({"clinical history", "history", "indication", "technique", "comparison"})
_ALL_HEADERS = sorted(_SCAN_SECTIONS | _SKIP_SECTIONS, key=len, reverse=True)
# A header's separator is a colon OR a spaced dash ("FINDINGS - Large right pneumothorax").
_HEADER_SEP = r"(?:[ \t]*:|[ \t]+[-–—](?=[ \t]))"
_HEADER_RE = re.compile(r"(?i)\b(" + "|".join(re.escape(h) for h in _ALL_HEADERS) + r")" + _HEADER_SEP)

# An UNKNOWN header still ends the previous section. Organ/region sub-headers ("CHEST:", "Chest:",
# "LUNGS:") are dictated capitalized at line start; without this, a finding under "Chest:" that
# follows a History: block is swallowed with the history (a reproduced false negative). Unknown-
# headed content is always KEPT -- only the explicitly-known skip sections are dropped.
_SUB_HEADER_RE = re.compile(r"(?m)^[ \t]*([A-Z][A-Za-z /-]{1,30}?)" + _HEADER_SEP)


# A sentence terminator must be FOLLOWED BY WHITESPACE (or end the text). A bare [.;] also
# matches inside a decimal or an abbreviation, and since a skip section commonly carries a
# date ("COMPARISON: CT 1.5.2026 demonstrated a large right pneumothorax, since resolved"),
# clamping at that first dot leaks the rest of the section back into the scan and raises a
# false critical off a resolved prior. Over-flag is the safe direction here, but it is
# avoidable, so avoid it (PI, on !190).
_SENTENCE_END = re.compile(r"[.;](?=\s|$)")


def _clamped_skip_end(text: str, body_start: int, hard_end: int) -> int:
    """Where a skipped section's PROVABLE content ends: its header line plus hard-wrap
    continuations.

    A skip section must not swallow everything to the next header: a short report that dictates
    the finding WITHOUT a FINDINGS header, after a trailing INDICATION/COMPARISON/TECHNIQUE line
    ("COMPARISON: None available.\\nLarge right tension pneumothorax..."), would positionally
    attribute the finding to the skip section and delete it -- a reproduced under-flag, the
    failure class this module exists to prevent. So the skip consumes further lines only while
    they are hard-wrap continuations (the previous line did not finish a sentence); a blank line,
    or a new line after a completed sentence, is no longer provably the section's own prose and
    is scanned instead (doubt resolves toward scanning).

    THE SAME RULE APPLIES WITHIN A LINE (#131). A skip section ends at the first sentence
    terminator on its first line, and only falls through to the hard-wrap chain when that line
    finished no sentence. Clamping on the LINE boundary alone was an accident of how the rule
    was first written, and production does not have line boundaries: `strip_html` replaces tags
    with a SPACE and TinyMCE bodies are <p>-delimited, so a report authored or edited in the RIS
    reaches fhir2 as ONE line. With no newline to stop at, the skip ran to the end of the text
    and deleted every finding after it -- the pre-clamp behaviour, reproduced. Seeded narratives
    keep their newlines and were never affected, which is why the cohort looked clean until a
    human touched a study.

    A terminator must be FOLLOWED BY WHITESPACE (or end the text), because a bare [.;] also
    matches inside a decimal or an abbreviation. Without that, "COMPARISON: CT 1.5.2026
    demonstrated a large right pneumothorax, since resolved" clamps at the dot in the date and
    leaks the rest of the section back into the scan, raising a critical off a prior that the
    same sentence says is resolved.

    KNOWN RESIDUALS of the terminal-punctuation proxy (pinned in tests, accepted per the module
    policy -- all are strictly SMALLER errors than the pre-clamp swallow-to-next-header):
      * UNDER-flag: a skip header line that does NOT end a sentence ANYWHERE ("COMPARISON: chest
        CT 2024") consumes the following continuation CHAIN -- every line up to the first one
        that ends in ./; (or a blank line / the next header). A finding dictated there, including
        a hard-wrapped one swallowed whole, is silenced. The pre-clamp behaviour silenced
        everything to the next header; this residual ends at the first completed sentence, and
        since #131 it is smaller again: a first line that completes a sentence never reaches the
        chain at all.
      * OVER-flag: an abbreviation or an ordinal followed by a space ("Dr. Smith", "1. Chest")
        ends the skip early, so the remainder of that section is scanned. Same tolerated
        direction as the residual below, and smaller: it costs a section's tail, not a page.
      * OVER-flag: the second-and-later sentences of a MULTI-LINE skip paragraph whose lines
        each end in "." leak into scanning, and a rule-out phrase there DOES flag ("Obtained to
        exclude pneumothorax" asserts: "to exclude" is not a negation cue, and the hedge rule
        deliberately voids suppression rather than adding it). A normal study whose TECHNIQUE
        paragraph wraps a rule-out onto line 2+ is over-flagged -- the tolerated direction,
        but a real false page, not a harmless leak.
    """
    lines = text[body_start:hard_end].split("\n")
    first_end = _SENTENCE_END.search(lines[0])
    end_offset = first_end.end() if first_end else len(lines[0])
    if first_end:
        return body_start + end_offset
    prev = lines[0]
    for line in lines[1:]:
        if not line.strip() or prev.rstrip().endswith((".", ";")):
            break
        end_offset += 1 + len(line)
        prev = line
    return body_start + end_offset


def scannable_text(narrative: str) -> str:
    """The finding-bearing portion of a report narrative, for the critical-term scanners.

    With recognizable section headers: everything EXCEPT the INDICATION / HISTORY / TECHNIQUE /
    COMPARISON sections -- the indication names the SUSPICION, not a finding, and the comparison
    describes the PRIOR study. Unknown headers (organ sub-headers like "CHEST:", or a template's
    per-finding headers like "PNEUMOTHORAX:") both TERMINATE a skipped section and have their
    content kept WITH the header name -- a template header can itself carry the finding
    ("PNEUMOTHORAX: Large, under tension"). A skipped section only spans its own header line and
    hard-wrap continuations (see _clamped_skip_end); anything after that within its span is kept.
    Only what is provably non-finding text is dropped; every doubt resolves toward scanning (the
    safe direction). Without headers: the text unchanged. Text before the first header is kept
    for the same reason. Sections are joined with a period so one section's clause can never leak
    negation into the next.
    """
    text = narrative or ""
    seen_starts: set[int] = set()
    boundaries: list[tuple[int, int, str, bool]] = []  # (start, body_start, name, known)
    for m in _HEADER_RE.finditer(text):
        boundaries.append((m.start(), m.end(), m.group(1).strip().lower(), True))
        seen_starts.add(m.start())
    for m in _SUB_HEADER_RE.finditer(text):
        if m.start() not in seen_starts and m.start(1) not in seen_starts:
            boundaries.append((m.start(), m.end(), m.group(1).strip().lower(), False))
    boundaries.sort()
    if not boundaries:
        return text
    parts: list[str] = []
    preamble = text[: boundaries[0][0]].strip()
    if preamble:
        parts.append(preamble)
    for i, (start, body_start, name, known) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        if name in _SKIP_SECTIONS:
            # Keep what the skip cannot prove is its own (a finding dictated after the section's
            # last line, with no header of its own).
            tail = text[_clamped_skip_end(text, body_start, end):end].strip()
            if tail:
                parts.append(tail)
            continue
        # An unknown header's NAME is content ("PNEUMOTHORAX: Large..."); a known structural
        # header's ("FINDINGS:") is not.
        body = (text[start:end] if not known else text[body_start:end]).strip()
        if body:
            parts.append(body)
    return ". ".join(parts)
