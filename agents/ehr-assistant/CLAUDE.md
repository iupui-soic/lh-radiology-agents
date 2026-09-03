# CLAUDE.md — EHR Assistant Agent

**Owner:** Parvati · **Skill:** `ehr.assembleContext` · **Port:** 8102 · **Stage:** Interpretation

## You own
`handler.py`, `server.py`, `tests/`, card `contracts/cards/ehr-assistant.json`.

## Contract
Return shape: `contracts/skills/ehr.schema.json` — a **distilled** packet (priorStudies,
relevantLabs, activeProblems, contrastFlags, medicationFlags, allergies) as references +
minimal derived values. **Not** a raw record dump (lean-reference / PHI minimization).

## v1 vs later
- **v1:** returns an empty-but-valid packet.
- **M1:** fetch from `fhir2` via `radagent_common.fhir_client.Fhir2Client` (READ-ONLY).
  Use `mocks/fixtures/fhir_bundle.sample.json` to develop offline.

## Data deps
`fhir2` reads only. Never write. Never put full clinical text in the response — surface the
decision-relevant slice + references.

## Versioning: when `AGENT_VERSION` must move (#129)
`AGENT_VERSION` in `handler.py` is stamped on every packet this agent returns. It has to move
whenever the packet can differ for the same chart: which labs, problems, flags or allergies are
selected, and how they are summarised. That logic lives in `handler.py`, which the CI gate cannot
watch (the constant lives there too), so there is NO gated surface here: bump by judgement, as
#135 did when the allergy display started being kept. Bump the minor (`0.x.0`) and the card
`contracts/cards/ehr-assistant.json` together (#124).

The gate (`scripts/check_agent_version_bumps.py`) still runs on every merge request and still
needs the card and handler to agree; it just has nothing of this agent's to compare.

## Run / test
`cd agents/ehr-assistant && python -m pytest -q`

## Do NOT touch
Other agents, `orchestrator/`, shared envelope, the A2A factory.
