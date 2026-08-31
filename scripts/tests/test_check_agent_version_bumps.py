"""The #129 gate: an agent's behaviour surface must not move without AGENT_VERSION moving.

Hermetic tests build a throwaway git repo, so they pin the RULE rather than this repo's history.
The last two run against real history instead, because the gate's whole claim is that it would
have caught what actually happened -- and that only real commits can show.
"""
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import check_agent_version_bumps as gate  # noqa: E402

REPO_ROOT = HERE.parents[1]


# --- a throwaway repo -------------------------------------------------------

def _run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo with one agent at 0.1.0 and one rule, committed as `base`."""
    _run("git", "init", "-q", "-b", "main", cwd=tmp_path)
    _run("git", "config", "user.email", "t@example.invalid", cwd=tmp_path)
    _run("git", "config", "user.name", "T", cwd=tmp_path)
    agent = tmp_path / "agents" / "report-verification"
    (agent / "rules").mkdir(parents=True)
    (agent / "handler.py").write_text('AGENT_VERSION = "0.1.0"\n')
    (agent / "rules" / "a.yaml").write_text("id: a\n")
    _run("git", "add", "-A", cwd=tmp_path)
    _run("git", "commit", "-qm", "base", cwd=tmp_path)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    return tmp_path


def _commit(repo, message):
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-qm", message, cwd=repo)


def test_a_rule_change_without_a_bump_is_a_violation(repo):
    (repo / "agents/report-verification/rules/b.yaml").write_text("id: b\n")
    _commit(repo, "add a rule")
    (v,) = gate.violations("HEAD~1")
    assert "report-verification" in v and "rules/b.yaml" in v
    assert "0.1.0" in v and "#124" in v, "the message must name the version and the card pairing"


def test_a_rule_change_with_a_bump_passes(repo):
    (repo / "agents/report-verification/rules/b.yaml").write_text("id: b\n")
    (repo / "agents/report-verification/handler.py").write_text('AGENT_VERSION = "0.2.0"\n')
    _commit(repo, "add a rule and bump")
    assert gate.violations("HEAD~1") == []


def test_handler_only_edits_are_not_a_behaviour_change(repo):
    # handler.py is deliberately NOT a surface: it holds the constant, so counting it would make
    # the bump commit itself a violation, and its plumbing edits are not behaviour.
    (repo / "agents/report-verification/handler.py").write_text(
        'import logging\nAGENT_VERSION = "0.1.0"\n')
    _commit(repo, "tidy imports")
    assert gate.violations("HEAD~1") == []


def test_the_skip_token_releases_the_gate(repo):
    (repo / "agents/report-verification/rules/a.yaml").write_text("id: a  # typo fix\n")
    _commit(repo, f"fix a comment {gate._SKIP_TOKEN}")
    assert gate.violations("HEAD~1") == []


def test_the_skip_token_is_honoured_anywhere_in_the_range(repo):
    (repo / "agents/report-verification/rules/a.yaml").write_text("id: a  # one\n")
    _commit(repo, f"docstring only {gate._SKIP_TOKEN}")
    (repo / "agents/report-verification/rules/a.yaml").write_text("id: a  # two\n")
    _commit(repo, "another comment tweak")
    assert gate.violations("HEAD~2") == []


def test_a_handler_with_no_version_constant_is_reported(repo):
    (repo / "agents/report-verification/handler.py").write_text("# no constant here\n")
    (repo / "agents/report-verification/rules/b.yaml").write_text("id: b\n")
    _commit(repo, "rule without a stampable version")
    (v,) = gate.violations("HEAD~1")
    assert "no AGENT_VERSION" in v


def test_an_untracked_agent_is_ignored(repo):
    (repo / "agents" / "some-other-agent").mkdir(parents=True)
    (repo / "agents/some-other-agent/registry.py").write_text("x = 1\n")
    _commit(repo, "unrelated agent")
    assert gate.violations("HEAD~1") == [], "only the #129 surfaces are gated"


# --- against real history ---------------------------------------------------

def test_it_would_have_caught_the_real_unstamped_rule_commits():
    """#129's headline: 12 commits changed report-verification's rules, 0 moved the version."""
    out = subprocess.run(
        ("git", "log", "--format=%h", "-3", "origin/main", "--",
         "agents/report-verification/rules/"),
        cwd=REPO_ROOT, capture_output=True, text=True)
    shas = out.stdout.split()
    if not shas:
        pytest.skip("no origin/main history available here")
    for sha in shas:
        assert gate.violations(f"{sha}~1", sha), f"{sha} changed rules without a bump, uncaught"


def test_it_does_not_fire_on_a_change_that_bumped():
    """The counter-example, so the gate is not just always-red: interpretation-assistant's
    version has moved with its registry, and such a change must pass."""
    out = subprocess.run(
        ("git", "log", "--format=%h", "-40", "origin/main", "--",
         "agents/interpretation-assistant/registry.py"),
        cwd=REPO_ROOT, capture_output=True, text=True)
    shas = out.stdout.split()
    if not shas:
        pytest.skip("no origin/main history available here")
    passing = [s for s in shas if not gate.violations(f"{s}~1", s)]
    assert passing, "no registry commit passed the gate; it would be unconditionally red"
