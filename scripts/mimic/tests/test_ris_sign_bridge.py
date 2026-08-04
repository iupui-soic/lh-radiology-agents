"""Regression tests for the ris-sign-bridge defect batch (#89/#90/#91).

The bridge shipped as an untested workaround (d210810); these pin the three failure modes the
post-merge audit filed, each of which corrupts or drops a HUMAN sign -- the single event the
whole post-sign pipeline hangs off:
  - #89: `find_seeded_report` picking the AI pre-sign draft (basedOn the same ServiceRequest)
    when fhir2's unspecified bundle order serves it first -- flipping an AI draft to `final`
    forges a human sign. Pinned with the draft FIRST in the bundle, the order that hides the
    bug on today's stack (which happens to serve oldest-first).
  - #90: a transient resolve miss (ETL not finished, fhir2 hiccup) permanently blacklisting the
    report_id in-memory -- the sign was silently lost until a container restart.
  - #91: the fhir2 1024-char conclusion cap (live-bisected: 1024 -> 200, 1025 -> 422) applied
    SILENTLY -- Verification then judges an amputated body. The cap is real and stays; the cut
    must be marked in-band and logged.
"""
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import ris_sign_bridge as bridge  # noqa: E402
from omrs_client import OmrsClient  # noqa: E402


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


# --- #90: a resolve miss is retried, not blacklisted -----------------------

def test_transient_order_miss_is_retried_and_bridges_on_recovery(monkeypatch, capsys):
    c = _FakeClient(order=None, fhir_id="dr-1")
    bridged, missing = set(), set()
    _rows(monkeypatch, [ROW])

    bridge.bridge_cycle(None, c, bridged, missing)          # miss: order not resolvable yet
    assert 7 not in bridged, "a miss must not join the done-set (the old permanent skip, #90)"
    assert 7 in missing

    c.order = {"patient_uuid": "p", "order_uuid": "o"}      # the ETL catches up
    bridge.bridge_cycle(None, c, bridged, missing)
    assert 7 in bridged and 7 not in missing
    assert c.put_calls and c.put_calls[0][0] == "dr-1"


def test_seeded_report_miss_is_retried_too(monkeypatch):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id=None)
    bridged, missing = set(), set()
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, bridged, missing)
    assert 7 not in bridged and 7 in missing
    c.fhir_id = "dr-1"
    bridge.bridge_cycle(None, c, bridged, missing)
    assert 7 in bridged and c.put_calls


def test_miss_is_logged_once_not_every_cycle(monkeypatch, capsys):
    # The old code logged the miss each poll; with retry-forever that would be one line per
    # 10s per stuck report. Log on the first miss only.
    c = _FakeClient(order=None)
    bridged, missing = set(), set()
    _rows(monkeypatch, [ROW])
    for _ in range(3):
        bridge.bridge_cycle(None, c, bridged, missing)
    assert capsys.readouterr().out.count("no order for s123") == 1


def test_already_final_report_is_done_without_a_put(monkeypatch):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1",
                    report={"status": "final"})
    bridged, missing = set(), set()
    _rows(monkeypatch, [ROW])
    bridge.bridge_cycle(None, c, bridged, missing)
    assert 7 in bridged and not c.put_calls


# --- #91: the fhir2 cap is applied loudly and marked ------------------------

def test_short_body_is_untouched(capsys):
    assert bridge.capped_conclusion(1, "x" * 1024) == "x" * 1024
    assert "truncating" not in capsys.readouterr().out


def test_long_body_is_capped_marked_and_logged(capsys):
    out = bridge.capped_conclusion(1, "x" * 3000)
    assert len(out) == bridge.FHIR2_CONCLUSION_MAX, "must fit fhir2's hard cap exactly"
    assert out.endswith(bridge.TRUNCATION_MARKER), "the cut must be visible in-band (#91)"
    assert "3000 chars" in capsys.readouterr().out


def test_boundary_1025_is_capped(capsys):
    out = bridge.capped_conclusion(1, "x" * 1025)
    assert len(out) <= bridge.FHIR2_CONCLUSION_MAX
    assert out.endswith(bridge.TRUNCATION_MARKER)


def test_bridge_writes_the_capped_body(monkeypatch):
    c = _FakeClient(order={"patient_uuid": "p", "order_uuid": "o"}, fhir_id="dr-1")
    _rows(monkeypatch, [(7, "y" * 5000, "s123", "Jake", "Doctor")])
    bridge.bridge_cycle(None, c, set(), set())
    (_, body), = c.put_calls
    assert len(body["conclusion"]) == bridge.FHIR2_CONCLUSION_MAX
    assert body["conclusion"].endswith(bridge.TRUNCATION_MARKER)
    assert body["status"] == "final"


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
