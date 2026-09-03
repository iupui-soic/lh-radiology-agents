#!/usr/bin/env python3
"""Fail when an agent's BEHAVIOUR SURFACE changed without `AGENT_VERSION` moving (#129).

`#124` made the card agree with the handler, so the two fields can no longer disagree. This is
the staleness underneath that: they can both be stale together and CI stays green. The #124 check
in `validate_contracts.py` points here for exactly that reason. This is that gate.

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
imports, tracing) are not behaviour. That is also the gate's known blind spot: an agent whose
behaviour lives in `handler.py` (worklist-triage scoring, ehr-assistant's packet) is bumped by
judgement, and each agent's CLAUDE.md says so.

SHARED SURFACES. A shared-library module that decides what an agent does is that agent's surface
too, even though it lives under `libs/`. `radagent_common/negation.py` chooses which findings
report-verification's rules scan AND which flags impression-generation raises, so an edit there
asks for a bump from both. One path can map to several agents.

ESCAPE HATCH, PER AGENT. A comment, docstring or test-only edit inside a surface is not a
behaviour change. Excuse it by NAMING the agent in any commit message in the range:
`[no-behaviour-change: report-verification]` (several: comma-separate, or repeat the token).
The token excuses only the agent it names, so a docstring edit in one agent can never wave
through a real behaviour change in another. A bare `[no-behaviour-change]` excuses nothing; the
first version of this gate accepted it MR-wide, and that was the hole #129 records.
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
# repo-relative path -> every agent whose behaviour it decides. Prefix-matched like the table above.
_SHARED_SURFACES = {
    "libs/radagent-common/radagent_common/negation.py": (
        "report-verification", "impression-generation"),
}
_SKIP_TOKEN = "[no-behaviour-change"
_SKIP_TOKEN_RE = re.compile(r"\[no-behaviour-change:\s*([A-Za-z0-9_,\s-]+?)\s*\]")
_BARE_SKIP_TOKEN_RE = re.compile(r"\[no-behaviour-change\s*\]")
_AGENT_VERSION_RE = re.compile(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', re.M)


def _git(*args: str) -> str:
    return subprocess.run(("git", *args), cwd=ROOT, capture_output=True,
                          text=True, check=False).stdout


def _version_at(ref: str, agent: str) -> str | None:
    """`AGENT_VERSION` in this agent's handler at `ref`, or None if absent there."""
    src = _git("show", f"{ref}:agents/{agent}/handler.py")
    m = _AGENT_VERSION_RE.search(src)
    return m.group(1) if m else None


def excused_agents(messages: str) -> set[str]:
    """The agents named by `[no-behaviour-change: a, b]` tokens anywhere in the commit messages."""
    names: set[str] = set()
    for group in _SKIP_TOKEN_RE.findall(messages):
        names.update(n.strip() for n in group.split(",") if n.strip())
    return names


def touched_surfaces(changed: list[str]) -> dict[str, list[str]]:
    """agent -> the changed paths that are its behaviour surface (own dir or a shared module)."""
    out: dict[str, list[str]] = {}
    for agent, surfaces in _BEHAVIOUR_SURFACES.items():
        for p in changed:
            if any(p.startswith(f"agents/{agent}/{s}") for s in surfaces):
                out.setdefault(agent, []).append(p)
    for shared, agents in _SHARED_SURFACES.items():
        for p in changed:
            if p.startswith(shared):
                for agent in agents:
                    out.setdefault(agent, []).append(p)
    return {a: sorted(set(ps)) for a, ps in out.items()}


def violations(base: str, head: str = "HEAD") -> list[str]:
    changed = [p for p in _git("diff", "--name-only", f"{base}...{head}").splitlines() if p]
    messages = _git("log", "--format=%B", f"{base}..{head}")
    excused = excused_agents(messages)
    bare = bool(_BARE_SKIP_TOKEN_RE.search(messages))
    out = []
    for agent, touched in sorted(touched_surfaces(changed).items()):
        if agent in excused:
            continue
        before, after = _version_at(base, agent), _version_at(head, agent)
        if after is None:
            out.append(f"[no AGENT_VERSION] agents/{agent}/handler.py defines none, so the "
                       f"change to {touched[0]} cannot be stamped")
        elif before == after:
            msg = (
                f"[behaviour changed, version did not] {agent}: {', '.join(touched)} changed "
                f"but AGENT_VERSION is still {after}. Every result this agent produces is "
                f"stamped with it, so two different behaviours would answer under one version. "
                f"Bump it (and the card, per #124), or put [no-behaviour-change: {agent}] in "
                f"the commit message if this edit is comments, docstrings or tests only.")
            if bare:
                msg += (" A bare [no-behaviour-change] is in the range and excuses nothing: "
                        "the token must name the agent.")
            if excused:
                msg += f" (agents excused by name in this range: {', '.join(sorted(excused))})"
            out.append(msg)
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
