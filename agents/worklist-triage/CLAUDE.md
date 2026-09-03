# CLAUDE.md — Worklist Triage Agent

**Owner:** Parvati · **Skill:** `triage.score` · **Port:** 8101 · **Stage:** Study Routing

## You own
`handler.py` (scoring logic), `server.py`, `tests/`, and the card `contracts/cards/worklist-triage.json`.

## Contract
Return shape: `contracts/skills/triage.schema.json` (`priorityScore` 0–100, `priorityTier`
STAT|URGENT|ROUTINE, `rationale[]`). Input: `{ studyContext }`.

## v1 vs later
- **v1:** transparent rule-of-thumb from order priority + reason codes + modality.
- **M2:** real signals (history, acuity models). Keep `rationale[]` populated — it's how
  radiologists trust the ordering.

## Data deps
None required in v1. Priority you return is published by the orchestrator to the Worklist API
(orchestrator is the source of truth — **no DICOM tag mutation**).

## Versioning: when `AGENT_VERSION` must move (#129)
`AGENT_VERSION` in `handler.py` is stamped on every score this agent returns. It has to move
whenever a study can land in a different tier or score for the same input, which for this agent
means the scoring tables and rules in `handler.py` itself. The CI gate cannot watch that file
(the constant lives in it, so the bump commit would violate itself), so there is NO gated surface
here: bump by judgement, as #133 did when token order stopped flipping the tier. Bump the minor
(`0.x.0`) and the card `contracts/cards/worklist-triage.json` together (#124).

The gate (`scripts/check_agent_version_bumps.py`) still runs on every merge request and still
needs the card and handler to agree; it just has nothing of this agent's to compare.

## Run / test
`cd agents/worklist-triage && python -m pytest -q` · serve: `uvicorn server:asgi_app --port 8101`

## Do NOT touch
Other agents, `orchestrator/`, `studycontext.schema.json`, the A2A factory.
