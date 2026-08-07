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
from datetime import datetime

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


# The last field is the sign instant (#110), coalesce(date_changed, date_created) from the RIS row.
SIGNED_AT = datetime(2026, 8, 7, 19, 11, 38)
ROW = (7, "<p>Pneumothorax.</p>", "s123", "Jake", "Doctor", "prov-uuid-1", SIGNED_AT)


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


def test_our_own_previous_write_short_circuits_quietly(monkeypatch, capsys):
    # The idempotence the final-skip exists for (#102 AC2): a final whose conclusion is
    # byte-identical to what we would write is our own earlier flip -- done, no PUT, no log.
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1",
                    report={"status": "final", "conclusion": "Pneumothorax."})
    bridged, missing = set(), {}
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, bridged, missing)
    assert 7 in bridged and not c.put_calls
    assert capsys.readouterr().out == "", "our own write must not spam the log per cycle"


# --- #102: a stale seeded final is never a silent drop ----------------------


STALE = {"status": "final", "conclusion": "the 454-char seeded cohort narrative"}


def test_stale_final_is_refused_loudly_with_both_lengths(monkeypatch, capsys):
    # The live loss: report 42 / s50279568 -- seeded final from a rehearsal finalize, signed
    # body only in the RIS, bridge log empty. Default behaviour is still "never overwrite a
    # final", but now it says so, names the report, and gives both lengths.
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1",
                    report=dict(STALE))
    bridged, missing = set(), {}
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, bridged, missing)
    out = capsys.readouterr().out
    assert not c.put_calls, "default must not overwrite a final report"
    assert "REFUSING" in out and "s123" in out
    assert f"{len(STALE['conclusion'])} chars stored" in out
    assert f"{len('Pneumothorax.')} signed" in out
    assert "restage" in out and "BRIDGE_OVERWRITE_STALE_FINAL" in out, \
        "the line must name both remedies"
    assert 7 not in bridged, \
        "a refused stale final must keep retrying so a restage bridges without a restart"


def test_stale_final_relogs_on_cadence_not_every_cycle(monkeypatch, capsys):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1",
                    report=dict(STALE))
    bridged, missing = set(), {}
    _rows(monkeypatch, [ROW])
    for _ in range(bridge.MISS_LOG_EVERY + 1):
        bridge.bridge_cycle(None, c, bridged, missing)
    out = capsys.readouterr().out
    assert out.count("REFUSING") == 2, "expected attempt 1 and attempt MISS_LOG_EVERY only"


def test_stale_final_bridges_after_an_operator_restage(monkeypatch):
    # report_seeder.py restage returns the seed to preliminary; the refused sign must then
    # bridge on the very next cycle, no container restart (#102 AC4's other half).
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1",
                    report=dict(STALE))
    bridged, missing = set(), {}
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, bridged, missing)
    assert not c.put_calls
    c.report = {"status": "preliminary"}
    bridge.bridge_cycle(None, c, bridged, missing)
    assert 7 in bridged and c.put_calls, "the restaged seed must accept the retried sign"
    (_, body), = c.put_calls
    assert body["conclusion"] == "Pneumothorax." and body["status"] == "final"


def test_stale_final_is_projected_over_when_the_flag_says_so(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "OVERWRITE_STALE_FINAL", True)
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1",
                    report=dict(STALE))
    bridged, missing = set(), {}
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, bridged, missing)
    (_, body), = c.put_calls
    assert body["conclusion"] == "Pneumothorax." and body["status"] == "final"
    assert 7 in bridged
    out = capsys.readouterr().out
    assert "projecting the human sign over it" in out, "an overwrite must never be silent either"


def test_our_own_truncated_write_still_reads_as_ours(monkeypatch):
    # The comparison is "modulo strip_html and the #91 marker" by construction: what we compare
    # against is the marker-prefixed clamp we would write today. Pin it so a marker change
    # cannot silently reclassify every past truncated write as stale.
    long_body = "HISTORY: dyspnea. " * 100 + "FINDINGS: " + "w" * 1500 + " IMPRESSION: large ptx."
    from report_text import strip_html
    stripped = strip_html(long_body)
    clamped, cut = clamp_conclusion(stripped, reserve=len(bridge.TRUNCATION_MARKER))
    assert cut is True
    ours = bridge.TRUNCATION_MARKER + clamped
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1",
                    report={"status": "final", "conclusion": ours})
    bridged = set()
    _rows(monkeypatch, [(7, long_body, "s123", "Jake", "Doctor", "prov-uuid-1", SIGNED_AT)])
    bridge.bridge_cycle(None, c, bridged, {})
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
    _rows(monkeypatch, [(7, long_body, "s123", "Jake", "Doctor", "prov-uuid-1", SIGNED_AT)])
    bridge.bridge_cycle(None, c, set(), {})
    (_, body), = c.put_calls
    assert body["status"] == "final"
    assert body["conclusion"].startswith(bridge.TRUNCATION_MARKER), "the cut must be visible in-band (#91)"
    assert body["conclusion"].endswith("IMPRESSION: large pneumothorax."), \
        "the clamp must keep the reading end -- IMPRESSION is what verification parses"
    assert len(body["conclusion"]) <= FHIR2_CONCLUSION_MAX


def test_bridge_logs_the_cut(monkeypatch, capsys):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1")
    _rows(monkeypatch, [(7, "v" * 3000, "s123", "Jake", "Doctor", "prov-uuid-1", SIGNED_AT)])
    bridge.bridge_cycle(None, c, set(), {})
    assert "caps conclusion" in capsys.readouterr().out


def test_bridge_short_body_gets_no_marker(monkeypatch):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1")
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, set(), {})
    (_, body), = c.put_calls
    assert bridge.TRUNCATION_MARKER not in body["conclusion"]
    assert body["conclusion"] == "Pneumothorax."


# --- #93: the signer reaches the bridged resource ---------------------------


def test_bridge_stamps_the_signer_as_performer(monkeypatch):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1")
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, set(), {})
    (_, body), = c.put_calls
    assert body["performer"] == [{"reference": "Practitioner/prov-uuid-1",
                                  "display": "Jake Doctor"}], \
        "a UI sign attributes the report; the bridged equivalent must too (#93)"


def test_missing_interpreter_still_bridges_without_performer(monkeypatch, capsys):
    # Losing the sign would be worse than losing the attribution: an interpreter-less
    # COMPLETED row still flips, unstamped, and the log line says so.
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1")
    _rows(monkeypatch, [(7, "Pneumothorax.", "s123", None, None, None, SIGNED_AT)])
    bridge.bridge_cycle(None, c, set(), {})
    (_, body), = c.put_calls
    assert body["status"] == "final"
    assert "performer" not in body
    assert "UNKNOWN: no interpreter recorded" in capsys.readouterr().out


# --- #110: the flip restamps the sign instant -------------------------------

def test_bridge_restamps_issued_with_the_sign_instant(monkeypatch):
    # The flip reuses the study's SEEDED report, so without a restamp `issued` keeps the ETL
    # seed's timestamp. The #70 hosted run produced a report signed 2026-08-07 carrying an
    # issued of 2026-07-24, and docs/signoff-link.md maps issued -> the poller's signedAt.
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1",
                    report={"status": "preliminary", "issued": "2026-07-24T02:14:10.000+00:00"})
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, set(), {})
    (_, body), = c.put_calls
    assert body["issued"] == "2026-08-07T19:11:38.000+00:00", \
        "issued must carry the sign instant, not the seed's timestamp (#110)"


def test_bridge_leaves_issued_alone_when_the_row_carries_no_timestamp(monkeypatch):
    # Both columns null is not a reason to invent a sign time: keep the seed value, which is at
    # least a real timestamp, rather than stamping "now" and calling a bridge cycle the read.
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1",
                    report={"status": "preliminary", "issued": "2026-07-24T02:14:10.000+00:00"})
    _rows(monkeypatch, [(7, "Pneumothorax.", "s123", "Jake", "Doctor", "prov-uuid-1", None)])
    bridge.bridge_cycle(None, c, set(), {})
    (_, body), = c.put_calls
    assert body["issued"] == "2026-07-24T02:14:10.000+00:00"
    assert body["status"] == "final", "a missing timestamp must never cost the sign itself"


def test_fhir_instant_formats_a_naive_datetime_as_utc():
    assert bridge._fhir_instant(datetime(2026, 8, 7, 19, 11, 38)) == "2026-08-07T19:11:38.000+00:00"
    assert bridge._fhir_instant(None) is None


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
