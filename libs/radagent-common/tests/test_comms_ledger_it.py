"""#52: the comms ledger against a LIVE FHIR server. The one part of CritCom mocks cannot verify.

test_comms_ledger.py mocks the transport, so it asserts what we SEND. It passes even when the
server rejects every write -- which is exactly what happened: HAPI enforces referential integrity,
and the ledger deliberately holds references to resources that live in fhir2 and are absent here
(Communication.subject / Task.for -> Patient/*, basedOn / about -> ServiceRequest/*). Every write
came back HTTP 400 (HAPI-1094), and with integrity merely disabled the references were not INDEXED,
so the audit searches returned 0 rows -- an audit trail that silently does not exist. The compose
fix is `hapi.fhir.auto_create_placeholder_reference_targets`; these tests are what stop it
regressing, in the compose config, the search params, or the reference shapes.

The assertions deliberately overlap the mocked suite, because the failures they catch are all
server-side -- a 400 on write, or a search that silently returns nothing -- and a mock can express
neither. Verified by re-introducing the bug: with the placeholder setting off, five of these fail.

Runs only against a live ledger (the `comms-ledger-it` CI job, or locally):

    docker compose up -d comms-ledger
    cd libs/radagent-common
    COMMS_LEDGER_BASE_URL=http://localhost:8108/fhir python -m pytest tests/test_comms_ledger_it.py

Async is driven with asyncio.run, matching test_comms_ledger.py: the lib's test lanes install
pytest WITHOUT pytest-asyncio, so a coroutine test would not run there.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from radagent_common.comms_ledger import ON_CALL_CODE, CommsLedgerClient
from radagent_common.fhir_models import (
    Communication,
    CommunicationPayload,
    CommunicationStatus,
    Period,
    Reference,
    Task,
    TaskRestriction,
    TaskStatus,
)

_BASE_URL = os.environ.get("COMMS_LEDGER_BASE_URL")

live = pytest.mark.skipif(
    not _BASE_URL,
    reason="no live ledger; set COMMS_LEDGER_BASE_URL (docker compose up -d comms-ledger)",
)

SPECIALTY = "RAD"


def test_this_lane_is_not_silently_skipping():
    """A skipped integration suite is a green CI job that guards nothing -- the same failure mode as
    the mocked tests it exists to backstop. In GitLab, a missing base URL is a hard error, not a
    skip; locally it stays a skip so the lib's suite still runs without Docker."""
    if os.environ.get("GITLAB_CI") == "true":
        assert _BASE_URL, (
            "COMMS_LEDGER_BASE_URL is unset in CI: the ledger integration lane skipped every test "
            "and the job would have gone green while guarding nothing"
        )


@pytest.fixture
def ledger() -> CommsLedgerClient:
    return CommsLedgerClient(base_url=_BASE_URL)


@pytest.fixture
def case() -> dict:
    """One synthetic case. The Patient / ServiceRequest ids are CROSS-STORE on purpose: they name
    resources that live in fhir2 and are absent from the ledger. Fresh ids per test, so a search
    asserting "exactly one row" cannot be satisfied by a previous run's rows."""
    tag = uuid.uuid4().hex[:12]
    return {
        "patient": f"Patient/p-{tag}",
        "order": f"ServiceRequest/sr-{tag}",
        "practitioner": f"pr-{tag}",
        "role": f"role-{tag}",
    }


def _communication(case: dict) -> Communication:
    return Communication(
        status=CommunicationStatus.IN_PROGRESS,
        subject=Reference(reference=case["patient"]),        # -> fhir2, absent here
        basedOn=[Reference(reference=case["order"])],        # -> fhir2, absent here (search key)
        about=[Reference(reference=case["order"])],
        recipient=[Reference(reference=f"Practitioner/{case['practitioner']}")],
        sent=datetime.now(timezone.utc),
        payload=[CommunicationPayload(contentString="Cat1: tension pneumothorax, right")],
    )


def _task(case: dict, communication_id: str) -> Task:
    """The open loop: who must ack, and by when. The deadline lives in restriction.period.end."""
    return Task(
        status=TaskStatus.REQUESTED,
        focus=Reference(reference=f"Communication/{communication_id}"),
        for_=Reference(reference=case["patient"]),           # serializes as `for` (alias)
        owner=Reference(reference=f"Practitioner/{case['practitioner']}"),
        authoredOn=datetime.now(timezone.utc),
        restriction=TaskRestriction(
            period=Period(end=datetime.now(timezone.utc) + timedelta(minutes=60))
        ),
    )


def _open_loop(ledger: CommsLedgerClient, case: dict) -> tuple[Communication, Task]:
    """The pair every test starts from: we told someone, and they owe us an ack."""

    async def go():
        comm = await ledger.create_communication(_communication(case))
        task = await ledger.create_task(_task(case, comm.id))
        return comm, task

    return asyncio.run(go())


# --- 1. the writes that used to 400 --------------------------------------------------


@live
def test_cross_store_references_write_instead_of_400(ledger, case):
    """HAPI-1094: "Resource Patient/... not found". Without the placeholder-target setting the
    ledger cannot record a single notification, because every reference it holds points into fhir2.
    """
    comm, task = _open_loop(ledger, case)
    assert comm.id, "the ledger did not assign an id to the Communication"
    assert task.id, "the ledger did not assign an id to the Task"


# --- 2. Task.for is an alias, and aliases are where round-trips break -----------------


@live
def test_task_for_round_trips_as_the_patient(ledger, case):
    """`for` is a Python keyword, so the model calls it `for_` with alias="for". If the alias were
    dropped on dump, the Task would post `for_`, HAPI would ignore the unknown element, and the Task
    would come back attached to no patient at all -- an ack loop nobody can trace to a case."""
    _, created = _open_loop(ledger, case)

    fetched = asyncio.run(ledger.get_task(created.id))
    assert fetched.for_ is not None, "Task.for did not round-trip (alias dropped on write?)"
    assert fetched.for_.reference == case["patient"]


# --- 3. read-modify-write: the ack must not destroy the loop it closes ----------------


@live
def test_update_task_status_preserves_focus_owner_and_the_ack_deadline(ledger, case):
    """FHIR PUT REPLACES the resource. A blind status-only PUT would drop focus / owner /
    restriction -- closing the loop by deleting it. update_task_status read-modify-writes; prove it
    on a real server, where those fields would actually be lost."""
    comm, created = _open_loop(ledger, case)
    deadline = created.restriction.period.end

    updated = asyncio.run(ledger.update_task_status(created.id, TaskStatus.COMPLETED))
    assert updated.status == TaskStatus.COMPLETED

    after = asyncio.run(ledger.get_task(created.id))
    assert after.status == TaskStatus.COMPLETED
    assert after.focus.reference == f"Communication/{comm.id}", "focus lost on status update"
    assert after.owner.reference == f"Practitioner/{case['practitioner']}", "owner lost"
    assert after.for_.reference == case["patient"], "for lost"
    assert after.restriction and after.restriction.period, "the ack deadline was dropped"
    assert after.restriction.period.end == deadline, "the ack deadline changed"


@live
def test_the_ack_disposition_round_trips_on_a_real_ledger(ledger, case):
    """#139: the ack page's answer rides on the Task.note entry that names who acknowledged. The
    mocked tests prove the text we build; this proves a real HAPI keeps the note (an Annotation
    with text + time) through the read-modify-write PUT, and that the loop closes as before."""
    _, created = _open_loop(ledger, case)
    at = datetime.now(timezone.utc).isoformat()

    asyncio.run(ledger.complete_ack_task(
        created.id, acknowledged_by=f"Practitioner/{case['practitioner']}", at_iso=at,
        disposition="patient-contacted"))

    after = asyncio.run(ledger.get_task(created.id))
    assert after.status == TaskStatus.COMPLETED
    texts = [n.get("text") if isinstance(n, dict) else getattr(n, "text", None) for n in after.note]
    assert any(t and "acknowledged by" in t and t.endswith("; disposition: patient-contacted")
               for t in texts), texts
    assert after.restriction.period.end == created.restriction.period.end, "deadline changed"


# --- 4. the searches that silently returned nothing -----------------------------------


@live
def test_search_by_based_on_and_focus_find_the_rows(ledger, case):
    """THE regression this lane exists for. With referential integrity merely disabled, HAPI accepts
    the write but does not index a reference whose target is absent -- so the searches return 0 rows
    and the audit trail is silently empty. Nothing about the write itself looks wrong."""
    comm, _ = _open_loop(ledger, case)

    by_order = asyncio.run(ledger.search_communications(case["order"]))
    assert [c.id for c in by_order] == [comm.id], (
        "search by based-on found no Communication for the order: the cross-store reference was "
        "accepted but never indexed -- the audit trail that silently does not exist"
    )

    by_patient = asyncio.run(ledger.search_communications_by_patient(case["patient"]))
    assert [c.id for c in by_patient] == [comm.id], "the subject reference was never indexed"

    tasks = asyncio.run(ledger.search_tasks_for_communication(comm.id))
    assert len(tasks) == 1, "search by focus found no Task hanging off the Communication"
    assert tasks[0].focus.reference == f"Communication/{comm.id}"


# --- 5. the audit join ----------------------------------------------------------------


@live
def test_search_audit_joins_communications_and_their_tasks(ledger, case):
    """"Who did we tell about this order, and did they ack?" -- the question a safety review asks."""
    comm, task = _open_loop(ledger, case)

    audit = asyncio.run(ledger.search_audit(service_request_id=case["order"]))
    assert [c["id"] for c in audit["communications"]] == [comm.id]
    assert [t["id"] for t in audit["tasks"]] == [task.id]

    by_patient = asyncio.run(ledger.search_audit(patient_id=case["patient"]))
    assert [c["id"] for c in by_patient["communications"]] == [comm.id]


# --- 6. the on-call directory ---------------------------------------------------------


@live
def test_the_on_call_directory_resolves(ledger, case):
    """Seeded by PUT at known ids (upsert), then read back the way the agent reads it: who is on
    call right now, optionally narrowed to a specialty. An empty result here means CritCom has
    nobody to page -- and it escalates rather than erroring, so a broken directory is silent."""

    async def seed():
        await ledger.upsert("Practitioner", case["practitioner"], {
            "resourceType": "Practitioner",
            "id": case["practitioner"],
            "name": [{"family": "Rao", "given": ["Anita"]}],
            "telecom": [{"system": "pager", "value": "555-0101"}],
        })
        await ledger.upsert("PractitionerRole", case["role"], {
            "resourceType": "PractitionerRole",
            "id": case["role"],
            "active": True,
            "practitioner": {"reference": f"Practitioner/{case['practitioner']}"},
            # search_on_call_roles pushes `active` + `specialty` to the server and matches the
            # on-call CODE client-side (PractitionerRole.code is a CodeableConcept, so a server-side
            # token match would need the system as well).
            "code": [{"coding": [{"code": ON_CALL_CODE}]}],
            "specialty": [{"coding": [{"code": SPECIALTY}]}],
        })

    asyncio.run(seed())

    roles = asyncio.run(ledger.search_practitioner_roles(case["practitioner"]))
    assert [r.id for r in roles] == [case["role"]], "the role did not index against its practitioner"

    on_call = asyncio.run(ledger.search_on_call_roles())
    assert case["role"] in [r.id for r in on_call], "the seeded role is not on call"

    narrowed = asyncio.run(ledger.search_on_call_roles(SPECIALTY))
    assert case["role"] in [r.id for r in narrowed], (
        f"specialty={SPECIALTY} narrowed the search to nothing: the server-side specialty filter "
        "does not match how the on-call directory is seeded"
    )
