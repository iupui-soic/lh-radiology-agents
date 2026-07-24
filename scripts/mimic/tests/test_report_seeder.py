"""report_seeder CLI: both documented spellings must work.

The docstring, scripts/mimic/README.md and the run-book all spell the command
`report_seeder.py finalize <accession>`, while argparse originally only accepted the bare
`report_seeder.py <accession>` -- so the documented form failed exactly when a demo needed it.
These tests pin both spellings (and that the success line names the real accession in both).
No I/O: finalize/OmrsClient are stubbed, only the CLI surface is under test.
"""
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import report_seeder  # noqa: E402


@pytest.fixture
def stubbed(monkeypatch):
    calls = []

    def fake_finalize(client, accession):
        calls.append(accession)
        return "rid-123"

    monkeypatch.setattr(report_seeder, "OmrsClient", lambda: object())
    monkeypatch.setattr(report_seeder, "finalize", fake_finalize)
    return calls


def test_bare_accession_spelling(stubbed, capsys):
    assert report_seeder.main(["s51350342"]) == 0
    assert stubbed == ["s51350342"]
    out = capsys.readouterr().out
    assert "DiagnosticReport/rid-123" in out
    # the success line must name the accession that was finalized (not None)
    assert "accession s51350342" in out


def test_documented_finalize_verb_spelling(stubbed, capsys):
    assert report_seeder.main(["finalize", "s51350342"]) == 0
    assert stubbed == ["s51350342"]
    assert "accession s51350342" in capsys.readouterr().out


def test_no_arguments_is_a_usage_error(stubbed):
    with pytest.raises(SystemExit) as e:
        report_seeder.main([])
    assert e.value.code == 2
    assert stubbed == []


def test_finalize_verb_without_an_accession_is_a_usage_error(stubbed):
    # the verb must not shift into the accession slot and finalize a study named "finalize"
    with pytest.raises(SystemExit) as e:
        report_seeder.main(["finalize"])
    assert e.value.code == 2
    assert stubbed == []


def test_unknown_verb_is_a_usage_error(stubbed):
    with pytest.raises(SystemExit) as e:
        report_seeder.main(["frobnicate", "s51350342"])
    assert e.value.code == 2
    assert stubbed == []
