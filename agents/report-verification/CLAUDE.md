# CLAUDE.md — Report Verification Agent

**Engine owner:** Pranathi (lead) · **Rules owner:** Saptarshi (PI)
**Skill:** `report.verify` · **Port:** 8105 · **Stage:** Report

## You own
- **Engine (lead):** `rules/engine.py`, `handler.py`, `server.py`, `tests/`, card.
- **Rules (PI):** `rules/*.yaml` and `rules/custom/*.py`. Add rules WITHOUT touching the engine.

## Contract
Return shape: `contracts/skills/report.schema.json` (`verificationStatus` PASS|WARN|FAIL,
`requiresHumanReview`, `issues[]` with `ruleId`/`severity`/`message`/`location`).

## Authoring rules (PI)
Declarative YAML (format described below). A rule's `when` describes the **problem** condition; if it
evaluates true, an issue is emitted. Ops: exists, not_exists, empty, non_empty, equals,
not_equals, contains, gt, lt. Paths are dotted and may index lists
(`impression.structuredFindings.0.laterality`). A field that resolves to **null** is treated like a
missing one for equals/not_equals/contains/gt/lt (the rule does not fire), so gt/lt never raise on an
absent parsed field. Complex logic → `rules/custom/<id>.py` with `def check(ctx) -> dict | None`.
Context keys: `report`, `impression`, `ehrContext`, `aiFindings`.

**Parsed report body (#22).** The handler fetches the finalized report's `conclusion` from fhir2 and
`rules/report_body.py` structures it into `report.body`, so rules can match on:
`report.body.present` (bool — gate custom rules on this so they stay inert when fhir2 gave nothing),
`report.body.laterality` (left|right|bilateral|null), `report.body.sections` (`{findings, impression,
technique, comparison, clinicalHistory, recommendation, notification}` — a CONCLUSION header folds
into `impression`; NOTIFICATION is the MIMIC result-communication section, #92),
`report.body.biradsAssessment` (int, NOT range-clamped — an out-of-range value is left visible so a
validity rule can flag it), `report.body.breastDensity` (A–D), and `report.body.text` (the raw
narrative, for keyword scans). The impression's own laterality is derived from its text into
`impression.derivedLaterality` for the laterality cross-check (the impression agent emits none).

Status: FAIL if any FAIL issue, else WARN if any WARN, else PASS. `requiresHumanReview` is true
for WARN/FAIL.

## v1 vs later
- **v1:** engine + sample YAML rules + custom examples.
- **M2 (#22, done):** `report_body.py` parses the fetched conclusion into `report.body`; the PI rule
  library fires on real reports (laterality mismatch, unflagged critical finding, missing impression
  section, mammography BI-RADS/density checks).
- **M3:** replace the keyword/regex parse with the LLM/structured extraction path (negation-aware);
  richer body sources than the conclusion (e.g. `presentedForm`).

## Run / test
`cd agents/report-verification && python -m pytest -q`

## Do NOT touch
Other agents, `orchestrator/`, shared envelope, the A2A factory.
