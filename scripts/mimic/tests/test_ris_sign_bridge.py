"""Regression tests for the ris-sign-bridge defect batch (#89/#90/#91).

The bridge shipped as an untested workaround (d210810); these pin the three failure modes the
post-merge audit filed, each of which corrupts or drops a HUMAN sign -- the single event the
whole post-sign pipeline hangs off:
  - #89: `find_seeded_report` picking the AI pre-sign draft (basedOn the same ServiceRequest)
    when fhir2's unspecified bundle order serves it first -- flipping an AI draft to `final`
    forges a human sign. Pinned with the draft FIRST in the bundle, the order that hides the
    bug on today's stack (which happens to serve oldest-first).
  - #90: a transient resolve miss (ETL not finished, fhir2 hiccup) permanently blacklisting the
    report_id in-memory -- the sign was silently lost until a container restart. Retries are
    forever; the log re-fires every MISS_LOG_EVERY attempts so a stuck sign stays visible.
  - #91: the fhir2 1024-char conclusion cap (live-bisected: 1024 -> 200, 1025 -> 422) applied
    SILENTLY and from the WRONG END. The clamp is now the seed path's (report_text, shared with
    load_cohort): keep FINDINGS onward, else the tail where IMPRESSION lives (#42) -- and the
    bridge prefixes an in-band marker and logs the cut.
"""
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import ris_sign_bridge as bridge  # noqa: E402
from omrs_client import OmrsClient  # noqa: E402
from report_text import FHIR2_CONCLUSION_MAX, clamp_conclusion  # noqa: E402


# --- fakes -----------------------------------------------------------------

class _FakeClient:
    """The four OmrsClient touchpoints bridge_cycle uses, scriptable per test."""

    def __init__(self, order=None, fhir_id=None, report=None):
        self.order = order
        self.fhir_id = fhir_id
        self.report = report if report is not None else {"status": "preliminary"}
        self.put_calls = []

    def order_for_accession(self, accession):
        return self.order

    def find_seeded_report(self, patient_uuid, order_uuid):
        return self.fhir_id

    def _fget(self, path):
        return dict(self.report)

    def _fput(self, res, rid, body):
        self.put_calls.append((rid, body))
        return body


def _rows(monkeypatch, rows):
    monkeypatch.setattr(bridge, "completed_reports", lambda conn: rows)


ROW = (7, "<p>Pneumothorax.</p>", "s123", "Jake", "Doctor")


# --- module import stays collectable in the pymysql-less CI lane ------------

def test_no_module_scope_pymysql_import():
    # The mimic-etl-tests lane deliberately installs no pymysql (DB-path-only dependency);
    # a module-scope import interrupted collection of the WHOLE suite. The dep must load
    # lazily, inside connect() -- same treatment omrs_client gives it.
    assert "pymysql" not in bridge.__dict__, \
        "pymysql leaked back to module scope; the CI lane cannot collect this module"


# --- #90: a resolve miss is retried, not blacklisted -----------------------

def test_transient_order_miss_is_retried_and_bridges_on_recovery(monkeypatch, capsys):
    c = _FakeClient(order=None, fhir_id="dr-1")
    bridged, missing = set(), {}
    _rows(monkeypatch, [ROW])

    bridge.bridge_cycle(None, c, bridged, missing)          # miss: order not resolvable yet
    assert 7 not in bridged, "a miss must not join the done-set (the old permanent skip, #90)"
    assert missing[7] == 1

    c.order = {"patient_uuid": "p", "order_uuid": "o"}      # the ETL catches up
    bridge.bridge_cycle(None, c, bridged, missing)
    assert 7 in bridged and 7 not in missing
    assert c.put_calls and c.put_calls[0][0] == "dr-1"


def test_seeded_report_miss_is_retried_too(monkeypatch):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id=None)
    bridged, missing = set(), {}
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, bridged, missing)
    assert 7 not in bridged and missing[7] == 1
    c.fhir_id = "dr-1"
    bridge.bridge_cycle(None, c, bridged, missing)
    assert 7 in bridged and c.put_calls


def test_stuck_miss_relogs_on_the_nth_attempt(monkeypatch, capsys):
    # One line ever is functionally silence for the bridge's highest-consequence state (a lost
    # human sign); one line per 10s poll is spam. Pin the compromise: first attempt and every
    # MISS_LOG_EVERY-th after it.
    c = _FakeClient(order=None)
    bridged, missing = set(), {}
    _rows(monkeypatch, [ROW])
    for _ in range(bridge.MISS_LOG_EVERY + 1):
        bridge.bridge_cycle(None, c, bridged, missing)
    out = capsys.readouterr().out
    assert out.count("no order for s123") == 2, "expected attempt 1 and attempt MISS_LOG_EVERY"
    assert "(attempt 1)" in out and f"(attempt {bridge.MISS_LOG_EVERY})" in out


def test_already_final_report_is_done_without_a_put(monkeypatch):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1",
                    report={"status": "final"})
    bridged, missing = set(), {}
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, bridged, missing)
    assert 7 in bridged and not c.put_calls


# --- #91: the shared clamp keeps the reading end, marked and logged ---------

def test_clamp_is_pure_and_reports_truncation(capsys):
    text, cut = clamp_conclusion("x" * 2000)
    assert cut is True and len(text) == FHIR2_CONCLUSION_MAX
    assert capsys.readouterr().out == "", "clamp_conclusion must not log; the caller owns that"


def test_clamp_short_text_untouched():
    assert clamp_conclusion("FINDINGS: ok. IMPRESSION: ok.") == ("FINDINGS: ok. IMPRESSION: ok.", False)


def test_reserve_is_charged_only_when_truncating():
    # A 1000-char body fits the 1024 cap raw; reserving marker room must not cut it anyway.
    text, cut = clamp_conclusion("y" * 1000, reserve=89)
    assert (text, cut) == ("y" * 1000, False)
    # Over the cap, the reserve comes out of the kept budget so marker + text fit the column.
    text, cut = clamp_conclusion("y" * 1025, reserve=89)
    assert cut is True and len(text) == FHIR2_CONCLUSION_MAX - 89


def test_clamp_drops_preamble_keeps_findings_and_impression():
    preamble = "WET READ: prelim note. " * 50   # pushes the total past the 1024 cap
    body = "FINDINGS: effusion. IMPRESSION: effusion."
    text, cut = clamp_conclusion(preamble + body)
    assert (text, cut) == (body, True), "FINDINGS onward fits -- preamble goes, IMPRESSION stays"


def test_clamp_keeps_tail_when_findings_alone_too_long():
    text = "FINDINGS: " + "z" * 2000 + " IMPRESSION: pneumothorax."
    out, cut = clamp_conclusion(text)
    assert cut is True and out.endswith("IMPRESSION: pneumothorax.")
    assert len(out) == FHIR2_CONCLUSION_MAX


def test_bridge_writes_marker_prefixed_tail(monkeypatch):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1")
    long_body = "HISTORY: dyspnea. " * 100 + "FINDINGS: " + "w" * 1500 + " IMPRESSION: large pneumothorax."
    _rows(monkeypatch, [(7, long_body, "s123", "Jake", "Doctor")])
    bridge.bridge_cycle(None, c, set(), {})
    (_, body), = c.put_calls
    assert body["status"] == "final"
    assert body["conclusion"].startswith(bridge.TRUNCATION_MARKER), "the cut must be visible in-band (#91)"
    assert body["conclusion"].endswith("IMPRESSION: large pneumothorax."), \
        "the clamp must keep the reading end -- IMPRESSION is what verification parses"
    assert len(body["conclusion"]) <= FHIR2_CONCLUSION_MAX


def test_bridge_logs_the_cut(monkeypatch, capsys):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1")
    _rows(monkeypatch, [(7, "v" * 3000, "s123", "Jake", "Doctor")])
    bridge.bridge_cycle(None, c, set(), {})
    assert "caps conclusion" in capsys.readouterr().out


def test_bridge_short_body_gets_no_marker(monkeypatch):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1")
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, set(), {})
    (_, body), = c.put_calls
    assert bridge.TRUNCATION_MARKER not in body["conclusion"]
    assert body["conclusion"] == "Pneumothorax."


# --- #89: the AI pre-sign draft is never the sign target --------------------

PRESIGN = "e3641471-3f25-57b4-ab27-a3ebc66e481e"


def _bundle(*resources):
    return {"entry": [{"resource": r} for r in resources]}


def _dr(rid, code=None, based_on="ServiceRequest/o"):
    r = {"id": rid, "basedOn": [{"reference": based_on}]}
    if code:
        r["code"] = {"coding": [{"code": code}]}
    return r


def _client_with_bundle(monkeypatch, bundle):
    c = OmrsClient()
    monkeypatch.setattr(c, "_fget", lambda path, params=None: bundle)
    return c


def test_draft_first_in_bundle_is_skipped(monkeypatch):
    # The dangerous ordering: fhir2's bundle order is unspecified, and if the draft comes first
    # the old first-match code flipped the AI draft to final -- a forged human sign (#89).
    c = _client_with_bundle(monkeypatch, _bundle(_dr("draft-1", code=PRESIGN), _dr("seeded-1")))
    assert c.find_seeded_report("p", "o") == "seeded-1"


def test_only_a_draft_matches_nothing(monkeypatch):
    c = _client_with_bundle(monkeypatch, _bundle(_dr("draft-1", code=PRESIGN)))
    assert c.find_seeded_report("p", "o") is None


def test_other_orders_reports_are_ignored(monkeypatch):
    c = _client_with_bundle(monkeypatch, _bundle(
        _dr("other-order", based_on="ServiceRequest/elsewhere"), _dr("seeded-1")))
    assert c.find_seeded_report("p", "o") == "seeded-1"


def test_concept_override_env_is_honoured(monkeypatch):
    monkeypatch.setenv("FHIR2_PRESIGN_REPORT_CONCEPT", "custom-concept")
    c = _client_with_bundle(monkeypatch, _bundle(
        _dr("draft-1", code="custom-concept"), _dr("seeded-1")))
    assert c.find_seeded_report("p", "o") == "seeded-1"
