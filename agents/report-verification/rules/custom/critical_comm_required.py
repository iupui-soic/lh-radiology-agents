"""Custom rule: a critical finding requires DOCUMENTED result communication.

Replaces the v1 YAML rule of the same id, which fired on every non-empty criticalFlags
regardless of what the report said -- so a signed report whose NOTIFICATION section already
documented the call still FAILed and parked at the sign-off gate (M4 run-book arc 2 signs and
pages immediately; the gate is arc 3's beat). The check the rule always described: FAIL only
when the impression carries a critical flag AND the report body does not document that the
result was communicated. A body that cannot be fetched still fails -- an unverifiable
communication is an undocumented one. Owner: Saptarshi.

Evidence polarity (#92). The first cut matched evidence keywords over the whole narrative,
so "the referring team was NOT notified", "UNABLE to reach the ordering physician by
telephone", "findings WILL BE communicated", and a CLINICAL HISTORY mention of "family
informed" all satisfied the rule -- the explicit record of a FAILED communication read as
proof of a successful one. Evidence now has to be an AFFIRMATIVE clause in a
notification-bearing scope:

  * Scope: the notification / impression / findings / recommendation sections. The
    anamnestic sections (clinical history, technique, comparison) can never satisfy the
    rule -- "family informed of biopsy plan last week" is history, not result
    communication. An unstructured body (no parsed sections) is scanned whole.
  * Clause model: same boundaries as radagent_common.negation (#78) -- sentence
    punctuation, parentheses, spaced/doubled dashes; newlines collapse first because
    reports hard-wrap mid-sentence. The vocabularies differ on purpose, so the module is
    not imported: the finding scanners suppress only on TIGHT negation cues (a missed
    finding pages nobody), while evidence dies on ANY disqualifier in its clause.
  * Disqualifiers: negation/failure ("not notified", "unable to reach", "without
    success"), and future/plan tense ("will be communicated", "pending", "to be
    relayed") -- a promised call is not a documented one. A disqualifier anywhere in the
    clause voids that clause's cues; a later affirmative clause still counts ("unable to
    reach Dr A; findings discussed with Dr B" documents the Dr B call).

DESIGN BIAS: this is the inverse of the negation module's. These clauses decide whether the
sign-off gate opens, so a FALSE PASS (failed communication read as documented) is the failure
class that matters; a FALSE FAIL merely over-gates and a human waves it through. Anything
ambiguous therefore stays NOT-evidence. Residual accepted with that bias: "communicated to
Dr X, who did NOT have questions" over-gates (the "not" voids its clause).
"""
from __future__ import annotations

import re

# Documentation-of-communication phrasing, incl. the MIMIC-CXR notification idioms ("NOTIFICATION:",
# "findings were discussed with/relayed to Dr ___ by telephone/phone", "the team was notified/paged").
_COMM_EVIDENCE = re.compile(
    r"\bnotification\b|\bcommunicat\w*\b|\brelayed\b|\bnotified\b|\bpaged\b"
    r"|\bdiscussed with\b|\binformed\b|\btelephone\b|\bby phone\b",
    re.IGNORECASE,
)

# Clause boundaries, mirroring radagent_common.negation's model: sentence punctuation (the colon
# also splits a section header off its content), parentheses, spaced or doubled dashes. A single
# unspaced hyphen is a compound ("follow-up") and does not split.
_BOUNDARY = re.compile(r"[.;:()\[\]]|\s[-–—]{1,2}\s|[-–—]{2,}")

# Anything here, anywhere in the clause, voids that clause's evidence cues. Two families:
#   * negation/failure -- the clause records a communication that did NOT happen;
#   * future/plan -- the clause promises a communication that has not happened YET.
_DISQUALIFIER = re.compile(
    r"\b(?:not|no|never|unable|failed|failure|could\s+not|couldn'?t|cannot|can'?t|"
    r"without\s+success|unsuccessful\w*|"
    r"will\s+be|to\s+be|pending|await\w*|plan(?:s|ned)?\s+to|attempt\w*)\b",
    re.IGNORECASE,
)

# Sections that can carry result-communication evidence. History/technique/comparison are
# deliberately absent: a communication mentioned there is anamnesis, not documentation.
_EVIDENCE_SECTIONS = ("notification", "impression", "findings", "recommendation")


def _evidence_scope(body: dict) -> str:
    sections = body.get("sections") or {}
    if not sections:
        return body.get("text") or ""      # unstructured body: scan it whole
    return "\n".join(sections.get(k) or "" for k in _EVIDENCE_SECTIONS)


def _documents_communication(text: str) -> bool:
    for clause in _BOUNDARY.split(text.replace("\n", " ")):
        if not clause:
            continue
        if _COMM_EVIDENCE.search(clause) and not _DISQUALIFIER.search(clause):
            return True
    return False


def check(ctx: dict) -> dict | None:
    if not (ctx.get("impression") or {}).get("criticalFlags"):
        return None
    body = (ctx.get("report") or {}).get("body") or {}
    if body.get("present") and _documents_communication(_evidence_scope(body)):
        return None  # an affirmative, in-scope clause documents the communication
    return {
        "ruleId": "critical-comm-required",
        "severity": "FAIL",
        "message": "Critical finding present; ensure result communication is documented.",
        "location": "impression.criticalFlags",
    }
