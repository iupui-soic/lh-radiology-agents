# Contributing

Welcome! This is the fast path from clone to a passing walking skeleton. For deep detail on
any topic below (module layout, contracts, agent-specific rules), see [`CLAUDE.md`](./CLAUDE.md)
and the per-directory `CLAUDE.md` guides — this document intentionally doesn't duplicate them.

## Prerequisites

- Python 3.11
- git
- Docker (optional — only needed if you want to run the full dev stack)

## Environment setup

This mirrors what CI runs (`.gitlab-ci.yml` is the source of truth if anything drifts):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e libs/radagent-common pytest pytest-asyncio
python scripts/validate_contracts.py
python mocks/run_walking_skeleton.py
```

If the walking skeleton script runs clean, your environment is good.

### Working on the orchestrator?

You'll need a few extra dependencies on top of the base setup:

```bash
pip install -e "libs/radagent-common[a2a]" "temporalio==1.29.0" fastapi uvicorn
```

## Running tests

Each agent is a standalone root. Run its tests from inside that agent's directory, e.g.:

```bash
cd agents/worklist-triage
python -m pytest -q
```

Orchestrator tests are the exception. They live outside the default test paths (see
`testpaths` in `pyproject.toml`), so a bare `pytest` from the root skips them — point pytest at
the directory explicitly, from the repo root, with the orchestrator deps above installed:

```bash
python -m pytest orchestrator/tests -q
```

## The golden rules

1. **Contract-first** — schema and payload change together, always.
2. **Lean-reference** — no PHI in messages.
3. **Scope discipline** — edit only the directory you own.
4. **Pure handlers** — never import `a2a.*` directly.

These are the ones that trip people up fastest. See `CLAUDE.md` for the full list (including determinism rules for orchestrator workflows) and the rationale behind each.

## Finding work

- Issues labeled **`intro`** are the best starting point for new contributors.
- The **milestone backlog** tracks the broader roadmap; open work sits under `M4`/`M5`.
- Check the **Ownership table** in [`CLAUDE.md`](./CLAUDE.md) — it maps each workstream to
  its owner, so you know who to loop in for review on the area you're touching.

## Merge request conventions

- Branch from `main`.
- Keep commit subjects to a single line.
- Reference the issue number in your commit message and MR description (e.g. `Fixes #42`).

---

Questions not answered here almost certainly belong in `CLAUDE.md` — start there, then ask
in the issue if you're still stuck.
