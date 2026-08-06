"""Regression tests for ris-presign-bridge (!107).

The bridge shipped as an untested workaround; these pin the failure modes the
!107 review filed. Each guards a specific way the AI's pre-sign draft can be lost,
duplicated, forged, or overwrite a radiologist's own text -- and one, the paging
wedge, guards the cursor from silently skipping AI drafts past the first page:

  - Module import stays collectable in the mimic-etl-tests lane, which installs
    no pymysql (!107 round 3 point 2, same treatment ris_sign_bridge got in !111).
  - `has_our_stamp` accepts only DiagnosticReports carrying our authorship concept
    -- a radiologist's own preliminary draft (different code) MUST be left alone.
  - `service_request_uuid` returns the order UUID from `basedOn`, or None when
    absent -- a resource with no basedOn is unroutable and must be skipped.
  - `poll_fhir_reports` follows every Bundle `next` link and computes the
    high-water from the MAX `lastUpdated` across ALL entries seen, including
    non-matching ones. This is the !107 round 3 point 1 fix: without it, a page
    with no AI-stamped preliminary would wedge the cursor, and an AI draft with
    an earlier lastUpdated than page 1's max would be skipped forever.
  - `bridge_report` decides insert/update/skip by strict rules -- no duplicate
    row for the same order, no overwrite of a radiologist-touched row, and no
    action on a resource missing the AI stamp or the basedOn ServiceRequest.
"""
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import ris_presign_bridge as bridge  # noqa: E402


# --- fakes ------------------------------------------------------------------

class _FakeResp:
    """Minimal httpx.Response stand-in for pagination via `next` links."""

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeHttp:
    """Serves `_http.get(absolute_url)` -- the second-page-onward path in the
    bridge's paginated poll. The `next` URL is opaque to us here; each call just
    pops the next bundle in the script."""

    def __init__(self, bundles):
        self._bundles = list(bundles)
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return _FakeResp(self._bundles.pop(0))


class _FakeClient:
    """OmrsClient touchpoints poll_fhir_reports uses.

    First page comes through `_fget(path, params)` (auth + base URL from the client's
    own config); subsequent pages come through `_http.get(absolute_url)` because the
    fhir2 `next` link is absolute and already carries its cursor in the query string.
    """

    def __init__(self, first_page, next_pages=()):
        self._first_page = first_page
        self._http = _FakeHttp(next_pages)
        self.fget_calls = []

    def _fget(self, path, params=None):
        self.fget_calls.append((path, dict(params or {})))
        return self._first_page


def _drop_sql(monkeypatch, name):
    """Replace a SQL helper so tests never touch pymysql. Any call becomes a
    scripted return value or an assertion trap."""
    def _refuse(*a, **k):
        raise AssertionError(f"{name} was called but the test did not script it")
    monkeypatch.setattr(bridge, name, _refuse)


# --- CI collectability (!107 round 3 point 2) -------------------------------

def test_no_module_scope_pymysql_import():
    # The mimic-etl-tests lane installs no pymysql. A module-scope import would
    # ImportError at collection time and take the whole suite down with it, same
    # bug ris_sign_bridge fixed in !111. Bridge must load lazily inside connect_db().
    assert "pymysql" not in bridge.__dict__, \
        "pymysql leaked back to module scope; the CI lane cannot collect this module"


# --- has_our_stamp: accept our concept, reject anything else ----------------

def test_has_our_stamp_matches_our_concept():
    r = {"code": {"coding": [{"code": bridge.FHIR2_PRESIGN_REPORT_CONCEPT}]}}
    assert bridge.has_our_stamp(r) is True


def test_has_our_stamp_rejects_human_preliminary():
    # A radiologist's OWN preliminary draft has some other code -- must be left
    # alone (they may be typing their report right now).
    r = {"code": {"coding": [{"code": "some-other-concept-uuid"}]}}
    assert bridge.has_our_stamp(r) is False


def test_has_our_stamp_rejects_missing_code():
    assert bridge.has_our_stamp({}) is False
    assert bridge.has_our_stamp({"code": {}}) is False
    assert bridge.has_our_stamp({"code": {"coding": []}}) is False


# --- service_request_uuid: basedOn parsing ----------------------------------

def test_service_request_uuid_parses_basedOn():
    r = {"basedOn": [{"reference": "ServiceRequest/abc-123"}]}
    assert bridge.service_request_uuid(r) == "abc-123"


def test_service_request_uuid_returns_none_when_missing():
    # No basedOn at all: an orphaned test result or global observation, unroutable.
    assert bridge.service_request_uuid({}) is None
    assert bridge.service_request_uuid({"basedOn": []}) is None


def test_service_request_uuid_ignores_non_service_request_refs():
    # A basedOn with a different resource type (e.g. CarePlan) is not our join key.
    r = {"basedOn": [{"reference": "CarePlan/xyz"}]}
    assert bridge.service_request_uuid(r) is None


def test_service_request_uuid_picks_first_service_request_when_mixed():
    r = {"basedOn": [
        {"reference": "CarePlan/xyz"},
        {"reference": "ServiceRequest/order-1"},
        {"reference": "ServiceRequest/order-2"},
    ]}
    assert bridge.service_request_uuid(r) == "order-1"


# --- poll_fhir_reports: cursor advances past no-match pages (!107 round 3 point 1) -

def _entry(**kw):
    """Shorthand builder for a Bundle entry."""
    r = {"resourceType": "DiagnosticReport"}
    r.update(kw)
    return {"resource": r}


def _ai_preliminary(fhir_id, order_uuid, when, conclusion="AI text"):
    return _entry(
        id=fhir_id,
        status="preliminary",
        code={"coding": [{"code": bridge.FHIR2_PRESIGN_REPORT_CONCEPT}]},
        basedOn=[{"reference": f"ServiceRequest/{order_uuid}"}],
        conclusion=conclusion,
        meta={"lastUpdated": when},
    )


def _final(when):
    """A `final` DiagnosticReport (not ours to bridge) -- shows up in the raw
    stream and must still move the cursor forward."""
    return _entry(status="final", meta={"lastUpdated": when})


def test_poll_advances_cursor_from_raw_bundle_even_when_no_matches():
    # The whole page is finals -- the pre-fix bridge would have refetched this
    # same page every 10s forever because the filtered list was empty. Post-fix
    # the cursor jumps to the latest raw entry.
    page = {"entry": [_final("2026-08-06T10:00:00Z"), _final("2026-08-06T11:00:00Z")]}
    c = _FakeClient(first_page=page)

    matching, high_water = bridge.poll_fhir_reports(c, "2026-08-06T00:00:00Z")

    assert matching == []
    assert high_water == "2026-08-06T11:00:00Z", \
        "cursor must advance from raw entries so a no-match page cannot wedge the poll"


def test_poll_follows_next_link_and_finds_page2_ai_draft():
    # Page 1 is all finals with LATER timestamps; page 2 has the AI draft with an
    # EARLIER timestamp. Without following the `next` link, the AI draft on page
    # 2 would be missed AND the cursor would sail past it (past page 1's max),
    # dropping the draft permanently. Follow the link and match it.
    page1 = {
        "entry": [
            _final("2026-08-06T10:00:00Z"),
            _final("2026-08-06T11:00:00Z"),
            _final("2026-08-06T12:00:00Z"),
        ],
        "link": [{"relation": "next",
                  "url": "http://openmrs:8080/openmrs/ws/fhir2/R4/DiagnosticReport?page=2"}],
    }
    page2 = {"entry": [_ai_preliminary("ai-1", "order-1", "2026-08-06T09:00:00Z")]}

    c = _FakeClient(first_page=page1, next_pages=[page2])
    matching, high_water = bridge.poll_fhir_reports(c, "2026-08-06T00:00:00Z")

    assert len(matching) == 1 and matching[0]["id"] == "ai-1", \
        "AI draft on page 2 must be found, not skipped"
    assert high_water == "2026-08-06T12:00:00Z", \
        "high-water is the MAX across ALL pages, not the last page's max"
    assert len(c._http.calls) == 1, "next link should have been followed exactly once"


def test_poll_stops_at_page_without_next_link():
    # Absent `next` means the last page -- do not keep hammering.
    page = {"entry": [_ai_preliminary("ai-1", "order-1", "2026-08-06T09:00:00Z")]}
    c = _FakeClient(first_page=page)
    matching, high_water = bridge.poll_fhir_reports(c, "2026-08-06T00:00:00Z")
    assert len(matching) == 1
    assert high_water == "2026-08-06T09:00:00Z"
    assert c._http.calls == [], "no next link -> no follow-up fetch"


def test_poll_filters_human_preliminary_but_still_advances_cursor():
    # A radiologist's OWN preliminary draft is preliminary but carries the wrong
    # concept code -- must be excluded from `matching` and must NOT be overwritten
    # in the DB. It still counts toward the high-water though.
    page = {"entry": [
        _entry(
            id="human-1", status="preliminary",
            code={"coding": [{"code": "some-radiologist-concept"}]},
            basedOn=[{"reference": "ServiceRequest/order-9"}],
            conclusion="Radiologist wrote this by hand",
            meta={"lastUpdated": "2026-08-06T15:00:00Z"},
        ),
    ]}
    c = _FakeClient(first_page=page)

    matching, high_water = bridge.poll_fhir_reports(c, "2026-08-06T00:00:00Z")
    assert matching == [], "a human draft must never end up in the AI-bridge queue"
    assert high_water == "2026-08-06T15:00:00Z", "still moves the cursor -- otherwise re-fetch"


def test_poll_cursor_never_moves_backward():
    # A resource with a lastUpdated OLDER than the incoming cursor must not drag
    # the high-water backward -- otherwise a duplicate refetch on the next cycle.
    page = {"entry": [_final("2026-01-01T00:00:00Z")]}
    c = _FakeClient(first_page=page)
    matching, high_water = bridge.poll_fhir_reports(c, "2026-08-06T00:00:00Z")
    assert matching == []
    assert high_water == "2026-08-06T00:00:00Z", "cursor must be monotonic"


# --- bridge_report: the write branches ---------------------------------------

def _ai_resource(fhir_id="fhir-1", order_uuid="order-1", conclusion="AI impression text"):
    return {
        "id": fhir_id,
        "conclusion": conclusion,
        "basedOn": [{"reference": f"ServiceRequest/{order_uuid}"}],
    }


def test_bridge_report_inserts_when_no_row_exists(monkeypatch):
    # The primary happy path: order resolves, no existing row -> INSERT DRAFT with
    # our conclusion + service_user_id as creator.
    monkeypatch.setattr(bridge, "order_id_for_uuid", lambda conn, u: 42)
    monkeypatch.setattr(bridge, "existing_report", lambda conn, oid: None)
    insert_calls = []
    monkeypatch.setattr(bridge, "insert_draft",
                        lambda conn, oid, body, uid: insert_calls.append((oid, body, uid)) or 99)
    _drop_sql(monkeypatch, "update_draft_body")

    outcome = bridge.bridge_report(conn=None, resource=_ai_resource(), service_user_id=7)

    assert insert_calls == [(42, "AI impression text", 7)]
    assert "insert" in outcome and "report_id=99" in outcome and "order_id=42" in outcome


def test_bridge_report_updates_when_empty_draft(monkeypatch):
    # A DRAFT row with no body -- the module-side UI created it (e.g. user opened
    # the report form) but nobody has typed anything. Safe to fill in.
    monkeypatch.setattr(bridge, "order_id_for_uuid", lambda conn, u: 42)
    monkeypatch.setattr(bridge, "existing_report", lambda conn, oid: (5, "DRAFT", None))
    update_calls = []
    monkeypatch.setattr(bridge, "update_draft_body",
                        lambda conn, rid, body, uid: update_calls.append((rid, body, uid)))
    _drop_sql(monkeypatch, "insert_draft")

    outcome = bridge.bridge_report(conn=None, resource=_ai_resource(), service_user_id=7)

    assert update_calls == [(5, "AI impression text", 7)]
    assert "update" in outcome and "report_id=5" in outcome


def test_bridge_report_skip_touched_when_radiologist_typed(monkeypatch):
    # A DRAFT row with existing text -- radiologist has typed something even if
    # they have not hit Complete. HANDS OFF: never overwrite their own work.
    monkeypatch.setattr(bridge, "order_id_for_uuid", lambda conn, u: 42)
    monkeypatch.setattr(bridge, "existing_report",
                        lambda conn, oid: (5, "DRAFT", "Radiologist typed this by hand"))
    _drop_sql(monkeypatch, "insert_draft")
    _drop_sql(monkeypatch, "update_draft_body")

    outcome = bridge.bridge_report(conn=None, resource=_ai_resource(), service_user_id=7)

    assert "skip-touched" in outcome and "report_id=5" in outcome


def test_bridge_report_skip_touched_when_status_past_draft(monkeypatch):
    # Anything past DRAFT (COMPLETED, whatever) means the radiologist has already
    # signed off; never touch a signed row even if the body is somehow empty.
    monkeypatch.setattr(bridge, "order_id_for_uuid", lambda conn, u: 42)
    monkeypatch.setattr(bridge, "existing_report",
                        lambda conn, oid: (5, "COMPLETED", ""))
    _drop_sql(monkeypatch, "insert_draft")
    _drop_sql(monkeypatch, "update_draft_body")

    outcome = bridge.bridge_report(conn=None, resource=_ai_resource(), service_user_id=7)

    assert "skip-touched" in outcome and "status=COMPLETED" in outcome


def test_bridge_report_noop_when_same_text_already_there(monkeypatch):
    # Our own previous write is still in place -- the idempotent path. Poll cycles
    # after the first INSERT hit this over and over, must not spam UPDATEs.
    monkeypatch.setattr(bridge, "order_id_for_uuid", lambda conn, u: 42)
    monkeypatch.setattr(bridge, "existing_report",
                        lambda conn, oid: (5, "DRAFT", "AI impression text"))
    _drop_sql(monkeypatch, "insert_draft")
    _drop_sql(monkeypatch, "update_draft_body")

    outcome = bridge.bridge_report(conn=None, resource=_ai_resource(), service_user_id=7)

    assert "noop-same-text" in outcome and "report_id=5" in outcome


def test_bridge_report_no_order_when_unresolvable(monkeypatch):
    # ServiceRequest uuid does not resolve to a mariadb orders.order_id -- ETL not
    # caught up yet, or a voided order. Skip; retries on future poll cycles.
    monkeypatch.setattr(bridge, "order_id_for_uuid", lambda conn, u: None)
    _drop_sql(monkeypatch, "existing_report")
    _drop_sql(monkeypatch, "insert_draft")
    _drop_sql(monkeypatch, "update_draft_body")

    outcome = bridge.bridge_report(conn=None, resource=_ai_resource(order_uuid="ghost-order"),
                                   service_user_id=7)

    assert "no-order" in outcome and "sr=ghost-order" in outcome


def test_bridge_report_skip_empty_conclusion(monkeypatch):
    # A DiagnosticReport with our stamp but no conclusion text -- unusable, skip.
    _drop_sql(monkeypatch, "order_id_for_uuid")
    _drop_sql(monkeypatch, "existing_report")
    _drop_sql(monkeypatch, "insert_draft")
    _drop_sql(monkeypatch, "update_draft_body")

    outcome = bridge.bridge_report(conn=None,
                                   resource=_ai_resource(conclusion=""),
                                   service_user_id=7)

    assert "skip-empty" in outcome


def test_bridge_report_skip_no_basedOn(monkeypatch):
    # A resource with our stamp but no basedOn -- unroutable to an order, skip.
    _drop_sql(monkeypatch, "order_id_for_uuid")
    _drop_sql(monkeypatch, "existing_report")
    _drop_sql(monkeypatch, "insert_draft")
    _drop_sql(monkeypatch, "update_draft_body")

    r = {"id": "fhir-1", "conclusion": "AI text"}   # no basedOn
    outcome = bridge.bridge_report(conn=None, resource=r, service_user_id=7)

    assert "skip-no-basedOn" in outcome


# --- #100: the body is written verbatim, no truncation ----------------------
#
# This block briefly pinned a 240-char clamp, from when OpenMRS core validated
# RadiologyReport.body against hibernate metadata reporting its default of 255. lh-radiology!91
# removed that cap, and the clamp is now unreachable anyway: the input is a fhir2
# DiagnosticReport.conclusion, and fhir2 rejects anything over 1024 chars outright. These pin the
# reversal, so a truncation cannot creep back in unnoticed.

def test_a_long_conclusion_is_written_verbatim(monkeypatch):
    """A realistic impression is far longer than the old 240-char cap. It must reach the
    Diagnosis field whole: a silently shortened impression reads as the AI's entire opinion,
    and the radiologist signs it."""
    long_text = ("Large left-sided pneumothorax with associated mediastinal shift. " * 12).strip()
    assert len(long_text) > 240
    monkeypatch.setattr(bridge, "order_id_for_uuid", lambda conn, u: 42)
    monkeypatch.setattr(bridge, "existing_report", lambda conn, oid: None)
    written = []
    monkeypatch.setattr(bridge, "insert_draft",
                        lambda conn, oid, body, uid: written.append(body) or 99)
    _drop_sql(monkeypatch, "update_draft_body")

    bridge.bridge_report(conn=None, resource=_ai_resource(conclusion=long_text),
                         service_user_id=7)

    assert written == [long_text]


def test_no_truncation_marker_is_ever_appended(monkeypatch):
    """Guards the specific regression: an in-band marker in a clinical field the radiologist is
    about to sign."""
    monkeypatch.setattr(bridge, "order_id_for_uuid", lambda conn, u: 42)
    monkeypatch.setattr(bridge, "existing_report", lambda conn, oid: None)
    written = []
    monkeypatch.setattr(bridge, "insert_draft",
                        lambda conn, oid, body, uid: written.append(body) or 99)
    _drop_sql(monkeypatch, "update_draft_body")

    bridge.bridge_report(conn=None, resource=_ai_resource(conclusion="z" * 5000),
                         service_user_id=7)

    assert written[0] == "z" * 5000
    assert "truncat" not in written[0].lower()


def test_a_long_row_we_already_wrote_reads_as_noop_not_as_a_radiologist_edit(monkeypatch):
    """Whatever the bridge writes must compare equal when it reads the row back next cycle. If a
    transform were ever applied on write but not on compare, every later cycle would report
    skip-touched, which is the bridge claiming a radiologist edited a row it wrote itself."""
    long_text = "w" * 5000
    monkeypatch.setattr(bridge, "order_id_for_uuid", lambda conn, u: 42)
    monkeypatch.setattr(bridge, "existing_report", lambda conn, oid: (5, "DRAFT", long_text))
    _drop_sql(monkeypatch, "insert_draft")
    _drop_sql(monkeypatch, "update_draft_body")

    outcome = bridge.bridge_report(conn=None, resource=_ai_resource(conclusion=long_text),
                                   service_user_id=7)

    assert "noop-same-text" in outcome
    assert "skip-touched" not in outcome
