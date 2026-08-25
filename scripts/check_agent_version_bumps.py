#!/usr/bin/env python3
"""Fail when an agent's BEHAVIOUR SURFACE changed without `AGENT_VERSION` moving (#129).

`#124` made the card agree with the handler, so the two fields can no longer disagree. This is
the staleness underneath that: they can both be stale together and CI stays green. The #124 check
in `validate_contracts.py` says so itself -- "it cannot catch both being stale together ... when
to bump is a discipline no gate here enforces". This is that gate.

`AGENT_VERSION` is not decoration: it is `required` on every `contracts/skills/*.schema.json`
output and those schemas are `additionalProperties: false`, so it is stamped on EVERY result the
system produces. It exists so a consumer -- or anyone auditing why a chart says what it says --
can tell which build answered. Two results stamped the same version must not be able to come from
agents that would reach opposite verdicts on the same report. #129 measured 37 behaviour commits
against 3 bumps, so today they can.

WHY A DIFF GATE AND NOT A CONTRACTS RULE. Whether behaviour changed is a fact about a CHANGE, not
about a tree, so it cannot be checked by looking at one revision. `validate_contracts.py` stays
git-free and locally runnable; this needs a base ref and therefore lives in its own lane.

THE SURFACES ARE #129's TABLE, NOT A GUESS. Each entry is a path whose edits change what the agent
DOES. `handler.py` is deliberately NOT a surface: it is where the version constant lives, so
including it would make the bump commit itself a violation, and its plumbing edits (logging,
imports, tracing) are not behaviour.

ESCAPE HATCH. A comment, docstring or test-only edit inside a surface is not a behaviour change.
Put `[no-behaviour-change]` in any commit message in the range and the check passes -- visible in
the log and in review, rather than a silent skip.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# agent directory -> the paths, relative to it, whose edits change what it does (#129).
_BEHAVIOUR_SURFACES = {
    "report-verification": ("rules/",),
    "impression-generation": ("llm_draft.py",),
    "interpretation-assistant": ("registry.py",),
    "communications": ("tools.py",),
}
_SKIP_TOKEN = "[no-behaviour-change]"
_AGENT_VERSION_RE = re.compile(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', re.M)


def _git(*args: str) -> str:
    return subprocess.run(("git", *args), cwd=ROOT, capture_output=True,
                          text=True, check=False).stdout


def _version_at(ref: str, agent: str) -> str | None:
    """`AGENT_VERSION` in this agent's handler at `ref`, or None if absent there."""
    src = _git("show", f"{ref}:agents/{agent}/handler.py")
    m = _AGENT_VERSION_RE.search(src)
    return m.group(1) if m else None


def violations(base: str, head: str = "HEAD") -> list[str]:
    changed = [p for p in _git("diff", "--name-only", f"{base}...{head}").splitlines() if p]
    messages = _git("log", "--format=%B", f"{base}..{head}")
    if _SKIP_TOKEN in messages:
        return []
    out = []
    for agent, surfaces in sorted(_BEHAVIOUR_SURFACES.items()):
        touched = sorted(
            p for p in changed
            for s in surfaces
            if p.startswith(f"agents/{agent}/{s}")
        )
        if not touched:
            continue
        before, after = _version_at(base, agent), _version_at(head, agent)
        if after is None:
            out.append(f"[no AGENT_VERSION] agents/{agent}/handler.py defines none, so the "
                       f"change to {touched[0]} cannot be stamped")
        elif before == after:
            out.append(
                f"[behaviour changed, version did not] {agent}: {', '.join(touched)} changed "
                f"but AGENT_VERSION is still {after}. Every result this agent produces is "
                f"stamped with it, so two different behaviours would answer under one version. "
                f"Bump it (and the card, per #124), or put {_SKIP_TOKEN} in the commit message "
                f"if this edit is comments, docstrings or tests only.")
    return out


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    if not _git("rev-parse", "--verify", f"{base}^{{commit}}").strip():
        print(f"cannot resolve base ref {base!r}; nothing to compare", file=sys.stderr)
        return 2
    found = violations(base)
    for v in found:
        print(v, file=sys.stderr)
    if found:
        print(f"\n{len(found)} agent(s) changed behaviour without moving AGENT_VERSION (#129)",
              file=sys.stderr)
        return 1
    print(f"OK: no agent changed a behaviour surface without moving AGENT_VERSION (vs {base}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
