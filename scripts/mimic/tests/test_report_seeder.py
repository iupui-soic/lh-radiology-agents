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


def test_restage_returns_the_seed_to_preliminary_so_a_new_sign_can_bridge(monkeypatch, tmp_path):
    # #102 AC4: a restaged study must be signable again -- the seeded DiagnosticReport back to
    # `preliminary` (the state ris_sign_bridge projects into) and the RIS rows voided, never
    # deleted. Fake DB + client, no I/O; the fake pymysql also keeps this collectable in the
    # mimic-etl-tests lane, which installs none.
    import json
    import types

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(
        [{"study_id": "s1", "report_text": "FINDINGS: x. IMPRESSION: y."}]))

    class FakeCursor:
        def __init__(self):
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append(sql)

        def fetchall(self):
            return [(42,)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeConn:
        cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

        def close(self):
            pass

    fake_pymysql = types.ModuleType("pymysql")
    fake_conn = FakeConn()
    fake_pymysql.connect = lambda **kw: fake_conn
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)

    class FakeClient:
        cfg = types.SimpleNamespace(db_host="h", db_port=3306, db_user="u",
                                    db_pass="p", db_name="d")
        put = None

        def order_for_accession(self, a):
            return {"patient_uuid": "p", "order_uuid": "o"}

        def find_seeded_report(self, p, o):
            return "dr-1"

        def _fget(self, path):
            return {"status": "final", "conclusion": "the rehearsal's projected text"}

        def _fput(self, res, rid, body):
            self.put = (rid, body)
            return body

    c = FakeClient()
    out = report_seeder.restage(c, "s1", str(manifest))
    rid, body = c.put
    assert rid == "dr-1"
    assert body["status"] == "preliminary", "a restaged seed must accept the next sign (#102)"
    assert body["conclusion"] == "FINDINGS: x. IMPRESSION: y."
    assert out["voided_ris_reports"] == [42]
    assert any("voided = 1" in sql for sql in fake_conn.cursor_obj.executed), \
        "RIS rows are voided for audit, not deleted"
