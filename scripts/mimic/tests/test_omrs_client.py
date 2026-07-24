"""Real-client seam tests for the #76 referring-physician seeding.

The load_cohort tests drive a _FakeClient, so they pin the WIRING but never execute the real
OmrsClient methods #76 adds -- an adversarial review proved a mutation dropping the orderer at the
SQL insert stayed green. These tests exercise the REAL methods:
  - ensure_referring_provider against a faked REST layer (get-or-create + cache + best-effort user),
  - insert_radiology_order's orderer against a faked DB (fresh-insert stamp + re-run backfill),
so a regression in either is caught without a live stack. (Codebase convention: raw-SQL paths are
otherwise live-verified via the #70 E2E; these fakes cover only the #76 orderer branch.)
"""
import sys
import pathlib

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from omrs_client import (  # noqa: E402
    OmrsClient, RADIOLOGY_ORDER_TYPE_UUID, RADIOLOGY_CARE_SETTING_UUID, fhir_instant,
)


# --- fhir_instant (the lab effectiveDateTime normalizer) -------------------
def test_sql_style_timestamp_gets_the_T_separator():
    # MIMIC-IV ships `YYYY-MM-DD HH:MM:SS`; HAPI refuses the space ("Expected character 'T' at
    # index 10"), which silently 400s every lab in a cohort load.
    assert fhir_instant("2180-08-07 06:15:00") == "2180-08-07T06:15:00"


def test_offset_without_a_colon_is_repaired():
    assert fhir_instant("2180-08-07T06:15:00+0000") == "2180-08-07T06:15:00+00:00"


def test_both_defects_in_one_value():
    assert fhir_instant("2180-08-07 06:15:00+0000") == "2180-08-07T06:15:00+00:00"


def test_already_valid_values_are_untouched():
    for good in ("2180-08-07T06:15:00", "2180-08-07T06:15:00Z", "2180-08-07T06:15:00+00:00"):
        assert fhir_instant(good) == good


def test_non_datetime_values_are_never_rewritten():
    # a bare date has no time half: nothing to separate, so it must pass through unchanged
    assert fhir_instant("2180-08-07") == "2180-08-07"
    assert fhir_instant("") == ""
    assert fhir_instant(None) == ""


# --- ensure_referring_provider (REST get-or-create + cache) ---------------
class _FakeRest:
    def __init__(self, providers=None, users=None, fail_user=False):
        self.providers = providers or {}   # identifier -> {"uuid":..., "person":{"uuid":...}}
        self.users = set(users or [])       # usernames that already exist
        self.fail_user = fail_user
        self.posts = []                     # [(resource, body), ...]

    def rget(self, res, params=None):
        params = params or {}
        if res == "provider":
            ident = params.get("q")
            p = self.providers.get(ident)
            return {"results": [dict(identifier=ident, **p)] if p else []}
        if res == "user":
            uname = params.get("q")
            return {"results": [{"uuid": "u-x", "username": uname}] if uname in self.users else []}
        return {"results": []}

    def rpost(self, res, body):
        self.posts.append((res, body))
        if res == "provider":
            return {"uuid": "prov-new", "person": {"uuid": "person-new"}}
        if res == "user":
            if self.fail_user:
                raise RuntimeError("role not permitted on this image")
            return {"uuid": "user-new"}
        return {"uuid": "x"}

    def resources(self):
        return [r for r, _ in self.posts]


def _client_with_rest(fake: _FakeRest) -> OmrsClient:
    c = OmrsClient()
    c._rget = fake.rget
    c._rpost = fake.rpost
    return c


def test_ensure_referring_provider_creates_once_then_caches():
    fake = _FakeRest()  # nothing exists yet
    c = _client_with_rest(fake)
    got = c.ensure_referring_provider("dr.reyes", "Marisol", "Reyes", gender="F")
    assert got == "prov-new"
    # exactly one provider + one user create, and NO separate person create (inline person)
    assert fake.resources() == ["provider", "user"]
    # second call for the same physician is served from cache -- no further writes
    again = c.ensure_referring_provider("dr.reyes", "Marisol", "Reyes", gender="F")
    assert again == "prov-new"
    assert fake.resources() == ["provider", "user"]  # unchanged


def test_provider_create_inlines_the_person_and_keys_on_username():
    fake = _FakeRest()
    _client_with_rest(fake).ensure_referring_provider("dr.okafor", "Chidi", "Okafor", gender="M")
    body = next(b for r, b in fake.posts if r == "provider")
    assert body["identifier"] == "dr.okafor"          # username is the idempotency key
    assert body["person"]["names"][0]["familyName"] == "Okafor"   # person is inlined, not pre-created
    assert body["person"]["gender"] == "M"


def test_existing_provider_is_reused_not_duplicated():
    fake = _FakeRest(providers={"dr.novak": {"uuid": "prov-existing", "person": {"uuid": "p-existing"}}},
                     users={"dr.novak"})
    c = _client_with_rest(fake)
    got = c.ensure_referring_provider("dr.novak", "Tomas", "Novak", gender="M")
    assert got == "prov-existing"
    assert fake.posts == []          # get-or-create: no provider, no person, no user writes


def test_referrer_user_failure_is_best_effort():
    fake = _FakeRest(fail_user=True)
    c = _client_with_rest(fake)
    got = c.ensure_referring_provider("dr.reyes", "Marisol", "Reyes", gender="F")
    assert got == "prov-new"         # the requester still seeds; only the login degraded
    assert "user" in fake.resources()  # it was attempted


def test_blank_username_raises():
    with pytest.raises(ValueError):
        _client_with_rest(_FakeRest()).ensure_referring_provider("  ", "X", "Y")


# --- insert_radiology_order orderer (SQL insert + re-run backfill) ---------
class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = 0
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.lower().split())
        self.conn.executed.append((s, params))
        if s.startswith("select o.uuid, o.orderer, o.order_id from orders"):
            self._result = self.conn.existing_order
        elif s.startswith("insert into orders"):
            self.conn.seq += 1
            self.lastrowid = self.conn.seq
            self.conn.inserted_orders.append(params)
            self._result = None
        elif s.startswith("select uuid from orders where order_id"):
            self._result = (f"ord-uuid-{params[0]}",)
        elif s.startswith("update orders set orderer"):
            self.conn.updates.append(params)
            self._result = None
        else:  # test_order / radiology_order inserts
            self._result = None

    def fetchone(self):
        return self._result


class _FakeConn:
    def __init__(self, existing_order=None):
        self.existing_order = existing_order  # None -> fresh insert; (uuid, orderer_id, order_id) -> exists
        self.executed = []
        self.inserted_orders = []
        self.updates = []
        self.seq = 100

    def cursor(self):
        return _FakeCursor(self)


# id resolutions the insert needs; the referring provider resolves to 99, the ETL admin to 7
_IDS = {
    "pat": 1, "enc": 2, "concept": 3,
    RADIOLOGY_ORDER_TYPE_UUID: 4, RADIOLOGY_CARE_SETTING_UUID: 5,
    "prov-ref": 99, "admin-uuid": 7,
}


def _client_with_db(conn: _FakeConn) -> OmrsClient:
    c = OmrsClient()
    c._db = lambda: conn
    c._id_by_uuid = lambda table, id_col, uuid: _IDS.get(uuid)
    c._provider_uuid = "admin-uuid"   # the default orderer, if the referring one were dropped
    return c


def test_fresh_order_is_stamped_with_the_supplied_orderer_not_admin():
    conn = _FakeConn(existing_order=None)
    c = _client_with_db(conn)
    c.insert_radiology_order("pat", "enc", "ACC1", "concept", orderer_provider_uuid="prov-ref")
    assert conn.inserted_orders, "an order row was inserted"
    params = conn.inserted_orders[0]
    assert 99 in params and 7 not in params   # the referring provider's id, never the admin fallback


def test_missing_orderer_falls_back_to_admin_on_a_fresh_order():
    conn = _FakeConn(existing_order=None)
    c = _client_with_db(conn)
    c.insert_radiology_order("pat", "enc", "ACC1", "concept")  # no orderer supplied
    params = conn.inserted_orders[0]
    assert 7 in params        # admin fallback -- unchanged pre-#76 behaviour


def test_rerun_backfills_the_orderer_on_an_existing_admin_order():
    conn = _FakeConn(existing_order=("ord-uuid-existing", 7, 55))  # admin orderer=7, order_id=55
    c = _client_with_db(conn)
    out = c.insert_radiology_order("pat", "enc", "ACC1", "concept", orderer_provider_uuid="prov-ref")
    assert out == "ord-uuid-existing"
    assert conn.updates == [(99, 55)]     # orderer UPDATEd to the referring provider for order 55
    assert not conn.inserted_orders       # idempotent: no new order


def test_rerun_backfill_is_a_noop_when_the_orderer_already_matches():
    conn = _FakeConn(existing_order=("ord-uuid-existing", 99, 55))  # already the referring provider
    c = _client_with_db(conn)
    c.insert_radiology_order("pat", "enc", "ACC1", "concept", orderer_provider_uuid="prov-ref")
    assert conn.updates == []


def test_existing_order_without_a_supplied_orderer_is_left_alone():
    conn = _FakeConn(existing_order=("ord-uuid-existing", 7, 55))
    c = _client_with_db(conn)
    out = c.insert_radiology_order("pat", "enc", "ACC1", "concept")  # no orderer
    assert out == "ord-uuid-existing"
    assert conn.updates == []             # no backfill without a target orderer
