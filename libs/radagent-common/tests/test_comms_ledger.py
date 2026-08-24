"""CommsLedgerClient (#52, MR 2): the Communications Agent's write store.

Mocks the HTTP layer. What matters here is that the RESOURCES WE WRITE are valid FHIR and that
the QUERIES WE SEARCH BY are the ones the resources are actually indexed on — a Communication
written under the wrong element, or a Task searched by the wrong param, is a critical result whose
audit trail silently does not exist.
"""
from __future__ import annotations

import asyncio

import pytest

from radagent_common.comms_ledger import CommsLedgerClient
from radagent_common.fhir_models import (
    CodeableConcept,
    Coding,
    Communication,
    CommunicationPayload,
    CommunicationStatus,
    ContactPoint,
    Period,
    PractitionerRole,
    Reference,
    Task,
    TaskRestriction,
    TaskStatus,
)


def _bundle(*resources):
    return {"resourceType": "Bundle", "type": "searchset",
            "entry": [{"resource": r} for r in resources]}


def _role(role_id: str, *codes: str, phone: str | None = None) -> dict:
    return PractitionerRole(
        id=role_id,
        practitioner=Reference(reference=f"Practitioner/{role_id}"),
        code=[CodeableConcept(coding=[Coding(code=c)]) for c in codes],
        telecom=[ContactPoint(system="phone", value=phone)] if phone else [],
    ).model_dump(mode="json", exclude_none=True)


# --- Communication: what we actually POST ------------------------------------------

def test_create_communication_posts_valid_fhir():
    client = CommsLedgerClient(base_url="http://ledger/fhir")
    posted: dict = {}

    async def fake_post(path, resource):
        posted.update(path=path, body=resource)
        return {**resource, "id": "comm-1"}

    client._post = fake_post  # type: ignore[assignment]
    comm = Communication(
        status=CommunicationStatus.COMPLETED,
        subject=Reference(reference="Patient/p1"),
        basedOn=[Reference(reference="ServiceRequest/sr-1")],
        about=[Reference(reference="ServiceRequest/sr-1")],
        recipient=[Reference(reference="Practitioner/dr-1")],
        payload=[CommunicationPayload(contentString="Tension pneumothorax. Call back.")],
    )
    written = asyncio.run(client.create_communication(comm))

    assert posted["path"] == "Communication"
    body = posted["body"]
    assert body["resourceType"] == "Communication"
    assert body["status"] == "completed"
    # basedOn is what search_communications queries on; `about` is the topical twin.
    assert body["basedOn"] == [{"reference": "ServiceRequest/sr-1"}]
    assert body["about"] == [{"reference": "ServiceRequest/sr-1"}]
    # exclude_none: we never POST explicit nulls (an absent element != a null one).
    assert "id" not in body and "sent" not in body
    assert written.id == "comm-1"
    assert written.finding_summary == "Tension pneumothorax. Call back."


# --- Task: the `for` alias, and the read-modify-write that protects the open loop ----

def test_task_serializes_the_for_alias_not_the_python_keyword():
    """`for` is a Python keyword, so the field is `for_` — but FHIR wants `for`. If this ever
    regresses, the Task is written with no patient and every ack query misses it."""
    client = CommsLedgerClient(base_url="http://ledger/fhir")
    posted: dict = {}

    async def fake_post(path, resource):
        posted.update(resource)
        return {**resource, "id": "task-1"}

    client._post = fake_post  # type: ignore[assignment]
    task = Task(
        status=TaskStatus.REQUESTED,
        focus=Reference(reference="Communication/comm-1"),
        for_=Reference(reference="Patient/p1"),
        owner=Reference(reference="Practitioner/dr-1"),
    )
    asyncio.run(client.create_task(task))

    assert posted["for"] == {"reference": "Patient/p1"}
    assert "for_" not in posted


def test_update_task_status_preserves_the_open_loop():
    """FHIR PUT REPLACES the resource. A status-only body would drop focus/owner/restriction —
    destroying the ack deadline and the link to the Communication, i.e. the whole point of the
    Task. So the update must read-modify-write."""
    client = CommsLedgerClient(base_url="http://ledger/fhir")
    existing = Task(
        id="task-1",
        status=TaskStatus.REQUESTED,
        focus=Reference(reference="Communication/comm-1"),
        for_=Reference(reference="Patient/p1"),
        owner=Reference(reference="Practitioner/dr-1"),
        restriction=TaskRestriction(period=Period(end="2026-07-12T01:00:00Z")),
    ).model_dump(mode="json", exclude_none=True, by_alias=True)
    put: dict = {}

    async def fake_get(path, params=None):
        assert path == "Task/task-1"
        return existing

    async def fake_put(path, resource):
        put.update(path=path, body=resource)
        return resource

    client._get = fake_get      # type: ignore[assignment]
    client._put = fake_put      # type: ignore[assignment]
    updated = asyncio.run(client.update_task_status("task-1", TaskStatus.COMPLETED))

    assert updated.status is TaskStatus.COMPLETED
    body = put["body"]
    assert body["status"] == "completed"
    assert body["focus"] == {"reference": "Communication/comm-1"}     # not dropped
    assert body["owner"] == {"reference": "Practitioner/dr-1"}        # not dropped
    assert body["restriction"]["period"]["end"].startswith("2026-07-12T01:00:00")  # deadline kept


# --- the search params the resources are indexed on ---------------------------------

def test_search_communications_queries_based_on_not_about():
    """`about` is NOT a default HAPI search parameter — searching it would return nothing and the
    audit trail would look empty. `based-on` is the one that works."""
    client = CommsLedgerClient(base_url="http://ledger/fhir")
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle({"resourceType": "Communication", "id": "comm-1"})

    client._get = fake_get  # type: ignore[assignment]
    comms = asyncio.run(client.search_communications("sr-1"))

    assert calls[0] == ("Communication", {"based-on": "ServiceRequest/sr-1", "_sort": "-sent"})
    assert [c.id for c in comms] == ["comm-1"]


def test_search_tasks_queries_focus():
    client = CommsLedgerClient(base_url="http://ledger/fhir")
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle({"resourceType": "Task", "id": "task-1", "status": "requested"})

    client._get = fake_get  # type: ignore[assignment]
    tasks = asyncio.run(client.search_tasks_for_communication("comm-1"))

    assert calls[0] == ("Task", {"focus": "Communication/comm-1", "_sort": "-_lastUpdated"})
    assert [t.id for t in tasks] == ["task-1"]


def test_refs_accept_a_bare_id_or_a_qualified_reference():
    """Callers hold refs in both shapes ('sr-1' from a model id, 'ServiceRequest/sr-1' from a
    StudyContext). Double-prefixing would silently match nothing."""
    client = CommsLedgerClient(base_url="http://ledger/fhir")
    calls = []

    async def fake_get(path, params=None):
        calls.append(params)
        return _bundle()

    client._get = fake_get  # type: ignore[assignment]
    asyncio.run(client.search_communications("sr-1"))
    asyncio.run(client.search_communications("ServiceRequest/sr-1"))
    assert calls[0]["based-on"] == calls[1]["based-on"] == "ServiceRequest/sr-1"


# --- the on-call directory -----------------------------------------------------------

def test_on_call_roles_filter_to_the_on_call_code():
    client = CommsLedgerClient(base_url="http://ledger/fhir")

    async def fake_get(path, params=None):
        assert params["active"] == "true"
        return _bundle(_role("r1", "on-call", phone="555-0100"),
                       _role("r2", "attending"))          # active, but not on call

    client._get = fake_get  # type: ignore[assignment]
    roles = asyncio.run(client.search_on_call_roles())

    assert [r.id for r in roles] == ["r1"]
    assert roles[0].contact("phone") == "555-0100"


def test_nobody_on_call_is_an_empty_answer_not_an_error():
    """An empty on-call list is a real, actionable answer (escalate) — it must not raise."""
    client = CommsLedgerClient(base_url="http://ledger/fhir")

    async def fake_get(path, params=None):
        return _bundle(_role("r2", "attending"))

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.search_on_call_roles()) == []


def test_on_call_search_passes_specialty_through():
    client = CommsLedgerClient(base_url="http://ledger/fhir")
    calls = []

    async def fake_get(path, params=None):
        calls.append(params)
        return _bundle()

    client._get = fake_get  # type: ignore[assignment]
    asyncio.run(client.search_on_call_roles(specialty_code="394914008"))
    assert calls[0] == {"active": "true", "specialty": "394914008"}


# --- audit ---------------------------------------------------------------------------

def test_audit_joins_each_communication_to_its_tasks():
    client = CommsLedgerClient(base_url="http://ledger/fhir")

    async def fake_get(path, params=None):
        if path == "Communication":
            return _bundle({"resourceType": "Communication", "id": "comm-1"})
        return _bundle({"resourceType": "Task", "id": "task-1", "status": "completed"})

    client._get = fake_get  # type: ignore[assignment]
    audit = asyncio.run(client.search_audit(service_request_id="sr-1"))

    assert [c["id"] for c in audit["communications"]] == ["comm-1"]
    assert [t["id"] for t in audit["tasks"]] == ["task-1"]


def test_audit_without_a_key_returns_empty_and_makes_no_call():
    client = CommsLedgerClient(base_url="http://ledger/fhir")

    async def boom(*a, **k):
        raise AssertionError("should not have queried the ledger")

    client._get = boom  # type: ignore[assignment]
    assert asyncio.run(client.search_audit()) == {"communications": [], "tasks": []}


# --- credentials ---------------------------------------------------------------------

def test_half_set_credentials_fail_loudly(monkeypatch):
    """A ledger that 401s every write is a critical result that was never recorded. Silently
    downgrading to anonymous would hide that (same reasoning as fhir2's, #53)."""
    monkeypatch.setenv("COMMS_LEDGER_USER", "critcom")
    monkeypatch.delenv("COMMS_LEDGER_PASS", raising=False)
    with pytest.raises(ValueError):
        CommsLedgerClient()


def test_no_credentials_stays_unauthenticated(monkeypatch):
    monkeypatch.delenv("COMMS_LEDGER_USER", raising=False)
    monkeypatch.delenv("COMMS_LEDGER_PASS", raising=False)
    assert CommsLedgerClient()._auth is None


def test_base_url_defaults_to_the_compose_service(monkeypatch):
    monkeypatch.delenv("COMMS_LEDGER_BASE_URL", raising=False)
    assert CommsLedgerClient().base_url == "http://comms-ledger:8080/fhir"


def test_complete_ack_task_records_who_and_preserves_the_loop():
    """#79's explicit ack: COMPLETED + a note naming the authenticated acknowledger, while
    owner stays the INTENDED recipient -- "sent to Dr A, acknowledged by Dr B" must remain
    readable from the one resource. Same PUT-replaces-everything hazard as update_task_status."""
    client = CommsLedgerClient(base_url="http://ledger/fhir")
    existing = Task(
        id="task-1",
        status=TaskStatus.REQUESTED,
        focus=Reference(reference="Communication/comm-1"),
        for_=Reference(reference="Patient/p1"),
        owner=Reference(reference="Practitioner/dr-a"),
        restriction=TaskRestriction(period=Period(end="2026-07-12T01:00:00Z")),
        note=[{"text": "paged twice"}],
    ).model_dump(mode="json", exclude_none=True, by_alias=True)
    put: dict = {}

    async def fake_get(path, params=None):
        assert path == "Task/task-1"
        return existing

    async def fake_put(path, resource):
        put.update(path=path, body=resource)
        return resource

    client._get = fake_get      # type: ignore[assignment]
    client._put = fake_put      # type: ignore[assignment]
    updated = asyncio.run(client.complete_ack_task(
        "task-1", acknowledged_by="Dr B (uuid-b)", at_iso="2026-07-19T18:00:00+00:00"))

    assert updated.status is TaskStatus.COMPLETED
    body = put["body"]
    assert body["status"] == "completed"
    assert body["owner"] == {"reference": "Practitioner/dr-a"}          # intended recipient kept
    assert body["restriction"]["period"]["end"].startswith("2026-07-12T01:00:00")
    assert body["note"][0]["text"] == "paged twice"                     # earlier notes kept
    assert body["note"][1] == {"text": "acknowledged by Dr B (uuid-b)",
                               "time": "2026-07-19T18:00:00+00:00"}


def _ack_rig(status: TaskStatus, note=None):
    """A CommsLedgerClient wired to fakes, returning (client, put_capture)."""
    client = CommsLedgerClient(base_url="http://ledger/fhir")
    existing = Task(
        id="task-1", status=status,
        focus=Reference(reference="Communication/comm-1"),
        owner=Reference(reference="Practitioner/dr-a"),
        note=note or [],
    ).model_dump(mode="json", exclude_none=True, by_alias=True)
    put: dict = {}

    async def fake_get(path, params=None):
        return existing

    async def fake_put(path, resource):
        put.update(path=path, body=resource)
        return resource

    client._get = fake_get      # type: ignore[assignment]
    client._put = fake_put      # type: ignore[assignment]
    return client, put


def test_a_late_ack_does_not_overwrite_the_failed_status(monkeypatch):
    """#128, the core of it. `escalate_to_on_call` sets FAILED when the window lapses, and that
    status IS the record that nobody acknowledged in time and on-call was paged. A late ack must
    add to that record, never replace it.

    Reproduced live 2026-08-23: the endpoint flipped a FAILED task to COMPLETED, so the ledger
    read like a clean in-time acknowledgement of a result that was actually missed, while the
    on-call task stayed open and nobody stood them down.
    """
    client, put = _ack_rig(TaskStatus.FAILED)
    updated = asyncio.run(client.complete_ack_task(
        "task-1", acknowledged_by="Dr A (uuid-a)", at_iso="2026-08-23T19:14:00+00:00",
        late_deadline_iso="2026-08-23T19:03:06+00:00"))

    assert updated.status is TaskStatus.FAILED          # the lapse survives
    assert put["body"]["status"] == "failed"
    assert put["body"]["note"][0]["text"] == (
        "late acknowledgement by Dr A (uuid-a); deadline was 2026-08-23T19:03:06+00:00")


def test_a_late_ack_on_a_task_that_never_escalated_still_completes():
    """No second human was engaged, so there is no lapse to preserve -- but the note still tells
    the truth about the timing."""
    client, put = _ack_rig(TaskStatus.REQUESTED)
    updated = asyncio.run(client.complete_ack_task(
        "task-1", acknowledged_by="Dr A (uuid-a)", at_iso="2026-08-23T19:14:00+00:00",
        late_deadline_iso="2026-08-23T19:03:06+00:00"))
    assert updated.status is TaskStatus.COMPLETED
    assert "late acknowledgement" in put["body"]["note"][0]["text"]


def test_an_on_behalf_of_ack_names_both_parties():
    """#127: a covering physician acknowledging for the addressee is normal, and the record has to
    show BOTH rather than silently substituting one clinician for the other."""
    client, put = _ack_rig(TaskStatus.REQUESTED)
    asyncio.run(client.complete_ack_task(
        "task-1", acknowledged_by="Dr B (uuid-b)", at_iso="2026-08-23T19:14:00+00:00",
        on_behalf_of="Dr A"))
    assert put["body"]["note"][0]["text"] == "acknowledged by Dr B (uuid-b) (on behalf of Dr A)"
    # owner is untouched: the addressee is still who it was SENT to
    assert put["body"]["owner"] == {"reference": "Practitioner/dr-a"}


def test_an_ordinary_ack_note_is_unchanged():
    """The #79 wording must not drift for the normal case; it is what the run-book screenshots."""
    client, put = _ack_rig(TaskStatus.REQUESTED)
    asyncio.run(client.complete_ack_task(
        "task-1", acknowledged_by="Dr A (uuid-a)", at_iso="2026-08-23T19:14:00+00:00"))
    assert put["body"]["status"] == "completed"
    assert put["body"]["note"][0]["text"] == "acknowledged by Dr A (uuid-a)"
