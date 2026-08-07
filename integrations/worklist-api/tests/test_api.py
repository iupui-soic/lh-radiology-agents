"""API endpoint tests. Uses FastAPI's TestClient with injected fakes
(no real Orthanc / no real fhir2 / in-memory priority store)."""
from __future__ import annotations

from typing import Optional

import pytest
from fastapi.testclient import TestClient

from main import create_app
from store import PriorityStore


class FakeOrthanc:
    """Minimal OrthancClient stand-in. Test sets `.studies` = pre-projected
    lean-study dicts (the shape `_lean_study` produces), or `.raise_on_list=True`
    to simulate an outage."""

    def __init__(self, studies=None, raise_on_list=False):
        self.studies = studies or []
        self.raise_on_list = raise_on_list

    async def list_completed_studies(self) -> list[dict]:
        if self.raise_on_list:
            raise RuntimeError("Orthanc unreachable")
        return self.studies


class FakeAssignment:
    """`assignments` maps studyInstanceUID -> dict; missing key returns None."""

    def __init__(self, assignments: Optional[dict] = None):
        self.assignments = assignments or {}

    async def get(self, uid: str):
        return self.assignments.get(uid)


def _lean(uid: str, orthanc_id: str = None, modality: str = "CT",
          study_date: str = "20260701", **extra) -> dict:
    """Build a lean-study dict as if it came from OrthancClient._lean_study."""
    return {
        "orthancStudyId":   orthanc_id or f"o-{uid}",
        "studyInstanceUID": uid,
        "accessionNumber":  extra.get("accession", ""),
        "modality":         modality,
        "studyDescription": extra.get("description", ""),
        "studyDate":        study_date,
        "lastUpdate":       extra.get("lastUpdate", ""),
    }


def _client(orthanc=None, store=None, assignment=None) -> TestClient:
    orthanc = orthanc or FakeOrthanc()
    store = store or PriorityStore(":memory:")
    assignment = assignment or FakeAssignment()
    return TestClient(create_app(orthanc=orthanc, store=store, assignment=assignment))


# --- /healthz ----------------------------------------------------------------

def test_healthz_ok_and_reports_store_size():
    store = PriorityStore(":memory:")
    store.put("uid1", "wf_1", "STAT", 90, "t")
    r = _client(store=store).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "priorityStoreSize": 1}


# --- POST /priority ----------------------------------------------------------

def test_priority_push_stores_the_value():
    store = PriorityStore(":memory:")
    r = _client(store=store).post("/priority", json={
        "studyInstanceUID": "1.2.3",
        "workflowId": "wf_1",
        "priorityTier": "STAT",
        "priorityScore": 95,
    })
    assert r.status_code == 204
    got = store.get("1.2.3")
    assert got["priorityTier"] == "STAT"
    assert got["priorityScore"] == 95


def test_priority_push_rejects_invalid_tier():
    """Guard against an accidental tier typo — pydantic pattern catches it, no
    row gets inserted."""
    store = PriorityStore(":memory:")
    r = _client(store=store).post("/priority", json={
        "studyInstanceUID": "1.2.3", "workflowId": "wf_1",
        "priorityTier": "CRITICAL", "priorityScore": 95,
    })
    assert r.status_code == 422
    assert store.size() == 0


def test_priority_push_rejects_out_of_range_score():
    store = PriorityStore(":memory:")
    r = _client(store=store).post("/priority", json={
        "studyInstanceUID": "1.2.3", "workflowId": "wf_1",
        "priorityTier": "STAT", "priorityScore": 150,
    })
    assert r.status_code == 422
    assert store.size() == 0


def test_priority_push_is_idempotent():
    store = PriorityStore(":memory:")
    c = _client(store=store)
    c.post("/priority", json={"studyInstanceUID": "1.2.3", "workflowId": "wf_1",
                              "priorityTier": "ROUTINE", "priorityScore": 50})
    c.post("/priority", json={"studyInstanceUID": "1.2.3", "workflowId": "wf_1",
                              "priorityTier": "URGENT",  "priorityScore": 72})
    assert store.size() == 1
    assert store.get("1.2.3")["priorityTier"] == "URGENT"


# --- GET /worklist -----------------------------------------------------------

def test_worklist_empty_when_orthanc_has_no_studies():
    r = _client().get("/worklist")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert "generatedAt" in body


def test_worklist_503_when_orthanc_down():
    """A live worklist read must not silently swallow an Orthanc outage —
    it's the OHIF UI's cue to show an error banner rather than an empty list
    (which would look like 'no studies to read', a dangerous ambiguity)."""
    r = _client(orthanc=FakeOrthanc(raise_on_list=True)).get("/worklist")
    assert r.status_code == 503
    assert "Orthanc" in r.json()["detail"]


def test_worklist_annotates_studies_with_priority_and_assignment():
    """Happy path: one study, one priority record, one assignment. Verify
    the join produces a single row with all three sources merged."""
    orthanc = FakeOrthanc([_lean("1.2.3", modality="CT",
                                 description="CT CHEST STAT")])
    store = PriorityStore(":memory:")
    store.put("1.2.3", "wf_1", "STAT", 95, "t")
    assignment = FakeAssignment({"1.2.3": {"radiologistId": "rad-1",
                                           "assignedAt": "2026-07-10T00:00:00Z"}})
    r = _client(orthanc=orthanc, store=store, assignment=assignment).get("/worklist")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["studyInstanceUID"] == "1.2.3"
    assert it["priorityTier"] == "STAT"
    assert it["priorityScore"] == 95
    assert it["workflowId"] == "wf_1"
    assert it["assignment"] == {"radiologistId": "rad-1",
                                "assignedAt": "2026-07-10T00:00:00Z"}


def test_worklist_defaults_untriaged_studies_to_routine():
    """A study Orthanc knows about but the orchestrator hasn't triaged yet
    (webhook delayed, restart race, etc.) must still appear on the worklist —
    just at the bottom. Silently dropping them would hide reads from the
    radiologist."""
    orthanc = FakeOrthanc([_lean("untriaged-1")])
    r = _client(orthanc=orthanc).get("/worklist")
    it = r.json()["items"][0]
    assert it["priorityTier"] == "ROUTINE"
    assert it["priorityScore"] == 50
    assert it["workflowId"] is None
    assert it["assignment"] is None


def test_worklist_sort_stat_above_urgent_above_routine():
    """The primary sort key is priorityTier bucket, then priorityScore desc."""
    orthanc = FakeOrthanc([
        _lean("routine-1"),
        _lean("stat-1"),
        _lean("urgent-1"),
    ])
    store = PriorityStore(":memory:")
    store.put("stat-1",    "wf_s", "STAT",    95, "t")
    store.put("urgent-1",  "wf_u", "URGENT",  70, "t")
    store.put("routine-1", "wf_r", "ROUTINE", 40, "t")

    order = [it["studyInstanceUID"] for it in
             _client(orthanc=orthanc, store=store).get("/worklist").json()["items"]]
    assert order == ["stat-1", "urgent-1", "routine-1"]


def test_worklist_sort_within_tier_uses_score_then_date():
    """Two STATs: higher score first. Two studies with the same tier+score:
    older studyDate first (queued longer -> read first)."""
    orthanc = FakeOrthanc([
        _lean("newer-hi",  study_date="20260710"),
        _lean("older-hi",  study_date="20260701"),
        _lean("stat-mid",  study_date="20260703"),
    ])
    store = PriorityStore(":memory:")
    store.put("newer-hi", "wf_1", "STAT", 95, "t")
    store.put("older-hi", "wf_2", "STAT", 95, "t")
    store.put("stat-mid", "wf_3", "STAT", 80, "t")

    order = [it["studyInstanceUID"] for it in
             _client(orthanc=orthanc, store=store).get("/worklist").json()["items"]]
    # Same tier+score: older date wins. Same tier, lower score: below.
    assert order == ["older-hi", "newer-hi", "stat-mid"]


def test_worklist_sort_missing_studydate_sorts_last_within_tier():
    """A study with no StudyDate must not float to the top of its tier as if it
    were the oldest case; an empty studyDate sorts after real dates."""
    orthanc = FakeOrthanc([
        _lean("no-date", study_date=""),
        _lean("dated",   study_date="20260701"),
    ])
    store = PriorityStore(":memory:")
    store.put("no-date", "wf_1", "STAT", 90, "t")
    store.put("dated",   "wf_2", "STAT", 90, "t")

    order = [it["studyInstanceUID"] for it in
             _client(orthanc=orthanc, store=store).get("/worklist").json()["items"]]
    assert order == ["dated", "no-date"]


def test_worklist_mixed_triaged_and_untriaged_studies():
    """Realistic mix: some studies triaged (STAT/URGENT), some not yet
    (default ROUTINE 50). Untriaged land last but are not lost."""
    orthanc = FakeOrthanc([
        _lean("untriaged-1"),
        _lean("stat-1"),
        _lean("untriaged-2"),
        _lean("urgent-1"),
    ])
    store = PriorityStore(":memory:")
    store.put("stat-1",   "wf_s", "STAT",   95, "t")
    store.put("urgent-1", "wf_u", "URGENT", 70, "t")

    body = _client(orthanc=orthanc, store=store).get("/worklist").json()
    order = [it["studyInstanceUID"] for it in body["items"]]
    assert order[0:2] == ["stat-1", "urgent-1"]
    assert set(order[2:]) == {"untriaged-1", "untriaged-2"}   # both present at bottom


# --- #108: the read-complete signal -----------------------------------------

def test_state_push_stores_the_read_state():
    store = PriorityStore(":memory:")
    r = _client(store=store).post("/state", json={
        "studyInstanceUID": "1.2.3",
        "workflowId": "wf_1",
        "readState": "ARCHIVED",
        "changedAt": "2026-08-07T19:11:42+00:00",
    })
    assert r.status_code == 204
    got = store.all_read_states()["1.2.3"]
    assert got["readState"] == "ARCHIVED"
    assert got["readStateChangedAt"] == "2026-08-07T19:11:42+00:00"


def test_worklist_marks_a_read_study_and_leaves_the_rest_unread():
    """The #108 defect itself: before the read-state publish existed, a signed and archived
    study rendered byte-identically to an unread one."""
    store = PriorityStore(":memory:")
    store.put("read-uid", "wf_1", "ROUTINE", 45, "t")
    store.put("fresh-uid", "wf_2", "ROUTINE", 45, "t")
    store.put_read_state("read-uid", "wf_1", "ARCHIVED", "2026-08-07T19:11:42+00:00", "t")
    orthanc = FakeOrthanc([_lean("read-uid"), _lean("fresh-uid")])
    items = _client(orthanc=orthanc, store=store).get("/worklist").json()["items"]
    by_uid = {it["studyInstanceUID"]: it for it in items}
    assert by_uid["read-uid"]["readState"] == "ARCHIVED"
    assert by_uid["read-uid"]["readAt"] == "2026-08-07T19:11:42+00:00"
    assert by_uid["fresh-uid"]["readState"] is None, "an unread study must stay null, not ''"
    assert by_uid["fresh-uid"]["readAt"] is None


def test_a_read_study_sinks_below_every_unread_one_whatever_its_tier():
    """A signed STAT study outranking an unread STAT study is the failure this fixes: the
    reading list is work left to do, and finished work does not belong at the top of it."""
    store = PriorityStore(":memory:")
    store.put("read-stat", "wf_1", "STAT", 100, "t")
    store.put("unread-routine", "wf_2", "ROUTINE", 45, "t")
    store.put_read_state("read-stat", "wf_1", "ARCHIVED", "2026-08-07T19:11:42+00:00", "t")
    orthanc = FakeOrthanc([_lean("read-stat"), _lean("unread-routine")])
    items = _client(orthanc=orthanc, store=store).get("/worklist").json()["items"]
    assert [it["studyInstanceUID"] for it in items] == ["unread-routine", "read-stat"]


def test_clearing_the_state_returns_a_study_to_the_unread_list():
    """The run-book reset path: restage plus an event re-fire puts a study back for a fresh
    read, and the re-fired workflow only republishes when it archives AGAIN."""
    store = PriorityStore(":memory:")
    store.put("uid1", "wf_1", "ROUTINE", 45, "t")
    store.put_read_state("uid1", "wf_1", "ARCHIVED", "2026-08-07T19:11:42+00:00", "t")
    client = _client(orthanc=FakeOrthanc([_lean("uid1")]), store=store)
    assert client.get("/worklist").json()["items"][0]["readState"] == "ARCHIVED"

    assert client.delete("/state/uid1").status_code == 204
    assert client.get("/worklist").json()["items"][0]["readState"] is None


def test_a_restaged_study_read_twice_upserts_rather_than_colliding():
    store = PriorityStore(":memory:")
    store.put_read_state("uid1", "wf_1", "ARCHIVED", "2026-08-01T10:00:00+00:00", "t1")
    store.put_read_state("uid1", "wf_1", "ARCHIVED", "2026-08-07T19:11:42+00:00", "t2")
    got = store.all_read_states()["uid1"]
    assert got["readStateChangedAt"] == "2026-08-07T19:11:42+00:00"
