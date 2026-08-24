"""The comms ledger: a FHIR R4 store for the *communication record* (#52, MR 2).

WHY THIS IS A SEPARATE STORE FROM fhir2
---------------------------------------
The Communications Agent has to answer two questions durably and forever: "did we tell someone?"
(`Communication`) and "did they acknowledge?" (`Task`, with the ack deadline on
`Task.restriction.period`). It also needs an on-call directory (`PractitionerRole`) to know WHO to
tell.

Our OpenMRS fhir2 implements none of those: no `Communication`, no `PractitionerRole` at any
version, and its `Task` has nowhere to put an ack deadline. So they cannot live there — and the
locked decision keeps fhir2 a READ-ONLY source of clinical context anyway. The writes therefore
land in a small, separate FHIR server (HAPI, `comms-ledger` in docker-compose), and CritCom's
clinical logic runs essentially as-built against it.

The split, and it is the whole point of this module:

    fhir2         READ-ONLY.  Patient / ServiceRequest / DiagnosticReport  -> Fhir2Client
    comms ledger  READ+WRITE. Communication / Task / Practitioner(Role)    -> CommsLedgerClient

Nothing here writes to fhir2, and nothing here is a source of clinical truth: a report's content
is always fetched from fhir2, never copied into the ledger. What the ledger stores is what WE did.

NO RETRIES HERE, on purpose. Every caller is a Temporal activity (see the escalation path in
orchestrator/activities.py), and Temporal owns the durable retry clock — a second retry layer
underneath it just multiplies attempts and hides failures from the workflow's history. Errors
propagate; the activity's RetryPolicy decides.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from .fhir_models import (
    Bundle,
    Communication,
    Practitioner,
    PractitionerRole,
    Task,
    TaskStatus,
)

# The on-call directory marks a role as reachable-right-now with this code. `search_on_call_roles`
# filters on it client-side: `PractitionerRole.code` is a CodeableConcept and HAPI's `role` search
# param would need the system as well, so a code-only match is both simpler and store-agnostic.
ON_CALL_CODE = "on-call"


def _basic_auth_from_env() -> Optional[tuple[str, str]]:
    """(user, pass) for a secured ledger, or None for the unauthenticated compose default.

    A half-set pair is rejected loudly rather than silently downgraded to anonymous: a ledger that
    401s every write is a critical result that never got recorded, and the caller would only see
    an activity failure with no hint why (same reasoning as fhir2's, #53). Never logged.
    """
    user = os.environ.get("COMMS_LEDGER_USER")
    password = os.environ.get("COMMS_LEDGER_PASS")
    if bool(user) != bool(password):
        raise ValueError("COMMS_LEDGER_USER and COMMS_LEDGER_PASS must be set together")
    return (user, password) if user else None


def _dump(resource: Any) -> dict:
    """FHIR JSON for a model. by_alias so Task.for_ serializes as `for`; exclude_none so we never
    POST explicit nulls (HAPI rejects some, and a null is not the same as an absent element)."""
    return resource.model_dump(mode="json", exclude_none=True, by_alias=True)


class CommsLedgerClient:
    """FHIR R4 client for the comms ledger. Read+write — unlike Fhir2Client, that is the point."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 15.0):
        self.base_url = (
            base_url
            or os.environ.get("COMMS_LEDGER_BASE_URL", "http://comms-ledger:8080/fhir")
        ).rstrip("/")
        self._timeout = timeout
        self._auth = _basic_auth_from_env()

    # --- transport ------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.get(f"{self.base_url}/{path.lstrip('/')}", params=params)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, resource: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.post(f"{self.base_url}/{path.lstrip('/')}", json=resource)
            r.raise_for_status()
            return r.json()

    async def _put(self, path: str, resource: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.put(f"{self.base_url}/{path.lstrip('/')}", json=resource)
            r.raise_for_status()
            return r.json()

    @staticmethod
    def _entries(bundle_json: dict) -> list[dict]:
        return [e.resource for e in Bundle.model_validate(bundle_json).entry if e.resource]

    # --- Communication: "we told someone" -------------------------------------------

    async def create_communication(self, comm: Communication) -> Communication:
        return Communication.model_validate(await self._post("Communication", _dump(comm)))

    async def get_communication(self, resource_id: str) -> Communication:
        return Communication.model_validate(await self._get(f"Communication/{resource_id}"))

    async def search_communications(self, service_request_id: str) -> list[Communication]:
        """Every notification sent for one order — the precise audit key.

        Searches `based-on`, which is why Communication.basedOn is populated alongside `about`
        (`about` is not a default HAPI search parameter; see fhir_models.Communication).
        """
        bundle = await self._get("Communication", {
            "based-on": _ref("ServiceRequest", service_request_id), "_sort": "-sent"})
        return [Communication.model_validate(r) for r in self._entries(bundle)]

    async def search_communications_by_patient(self, patient_id: str) -> list[Communication]:
        bundle = await self._get("Communication", {
            "subject": _ref("Patient", patient_id), "_sort": "-sent"})
        return [Communication.model_validate(r) for r in self._entries(bundle)]

    # --- Task: "did they acknowledge?" ----------------------------------------------

    async def create_task(self, task: Task) -> Task:
        return Task.model_validate(await self._post("Task", _dump(task)))

    async def get_task(self, resource_id: str) -> Task:
        return Task.model_validate(await self._get(f"Task/{resource_id}"))

    async def update_task_status(self, resource_id: str, status: TaskStatus) -> Task:
        """Read-modify-write: FHIR PUT replaces the whole resource, so the current one is fetched
        first. A blind PUT of a status-only body would silently drop `focus`, `owner`, and the ack
        deadline in `restriction` — i.e. destroy the open loop we are trying to close."""
        task = await self.get_task(resource_id)
        task.status = status
        return Task.model_validate(await self._put(f"Task/{resource_id}", _dump(task)))

    async def complete_ack_task(self, resource_id: str, *, acknowledged_by: str,
                                at_iso: str, on_behalf_of: str | None = None,
                                late_deadline_iso: str | None = None) -> Task:
        """Close the loop AND say who closed it (#79's explicit ack): a FHIR Annotation on
        `Task.note` naming the authenticated acknowledger, and status -> COMPLETED unless the loop
        already terminated. The note is the audit fact the plain status flip cannot carry —
        `Task.owner` stays the INTENDED recipient, so "sent to Dr A, acknowledged by Dr B" remains
        readable from one resource. Same read-modify-write rationale as update_task_status.

        `on_behalf_of` (#127): set when the acknowledger is NOT the Task's addressee. Both parties
        go in the note, because a covering physician acknowledging for the addressee is normal and
        the record has to show it happened rather than silently substituting one clinician for the
        other. Pass the addressee's reference/display; None means the acknowledger IS the owner.

        `late_deadline_iso` (#128): set when the ack arrives after `restriction.period.end`. The
        note then records the lateness AND the deadline it missed.

        STATUS RULE, and the reason this method no longer just assigns COMPLETED: a Task the
        escalation already marked FAILED keeps that status. FAILED is the record that nobody
        acknowledged in time and that on-call was paged; overwriting it to COMPLETED erased the
        miss and left the ledger reading like a clean in-time acknowledgement of a result that was
        actually missed (#128, reproduced live 2026-08-23). A late ack on a Task that has NOT yet
        escalated still completes: no second human was engaged, so there is no lapse to preserve,
        and the note carries the timing truth either way.
        """
        task = await self.get_task(resource_id)
        if task.status != TaskStatus.FAILED:
            task.status = TaskStatus.COMPLETED
        if late_deadline_iso:
            text = (f"late acknowledgement by {acknowledged_by}; deadline was "
                    f"{late_deadline_iso}")
        else:
            text = f"acknowledged by {acknowledged_by}"
        if on_behalf_of:
            text = f"{text} (on behalf of {on_behalf_of})"
        task.note = list(task.note) + [{"text": text, "time": at_iso}]
        return Task.model_validate(await self._put(f"Task/{resource_id}", _dump(task)))

    async def search_tasks_for_communication(self, communication_id: str) -> list[Task]:
        bundle = await self._get("Task", {
            "focus": _ref("Communication", communication_id), "_sort": "-_lastUpdated"})
        return [Task.model_validate(r) for r in self._entries(bundle)]

    # --- the on-call directory ------------------------------------------------------

    async def get_practitioner(self, resource_id: str) -> Practitioner:
        return Practitioner.model_validate(await self._get(f"Practitioner/{resource_id}"))

    async def get_practitioner_role(self, resource_id: str) -> PractitionerRole:
        return PractitionerRole.model_validate(await self._get(f"PractitionerRole/{resource_id}"))

    async def search_practitioner_roles(self, practitioner_id: str) -> list[PractitionerRole]:
        bundle = await self._get("PractitionerRole", {
            "practitioner": _ref("Practitioner", practitioner_id), "active": "true"})
        return [PractitionerRole.model_validate(r) for r in self._entries(bundle)]

    async def search_on_call_roles(self, specialty_code: str | None = None) -> list[PractitionerRole]:
        """Active roles currently tagged on-call, optionally narrowed to a specialty.

        `active=true` and `specialty` are pushed to the server; the on-call code is matched
        client-side (see ON_CALL_CODE). An empty result is a real answer -- nobody is on call --
        and the caller must escalate rather than treat it as an error.
        """
        params: dict[str, Any] = {"active": "true"}
        if specialty_code:
            params["specialty"] = specialty_code
        roles = [PractitionerRole.model_validate(r)
                 for r in self._entries(await self._get("PractitionerRole", params))]
        return [r for r in roles
                if any(c.code == ON_CALL_CODE for cc in r.code for c in cc.coding)]

    # --- audit ----------------------------------------------------------------------

    async def search_audit(self, service_request_id: str | None = None,
                           patient_id: str | None = None) -> dict[str, list[dict]]:
        """The full communication history for a case: every Communication and the Tasks hanging
        off each one. By order when we have it (precise), else by patient (broad)."""
        if service_request_id:
            comms = await self.search_communications(service_request_id)
        elif patient_id:
            comms = await self.search_communications_by_patient(patient_id)
        else:
            return {"communications": [], "tasks": []}

        tasks: list[dict] = []
        for c in comms:
            if c.id:
                tasks.extend(t.model_dump(mode="json") for t
                             in await self.search_tasks_for_communication(c.id))
        return {"communications": [c.model_dump(mode="json") for c in comms], "tasks": tasks}

    # --- seeding (fixtures / the compose demo) ---------------------------------------

    async def upsert(self, resource_type: str, resource_id: str, body: dict) -> dict:
        """PUT a resource at a known id (create-or-update). Used to seed the on-call directory."""
        return await self._put(f"{resource_type}/{resource_id}", body)


def _ref(resource_type: str, id_or_ref: str) -> str:
    """Accept either a bare id ('123') or an already-qualified reference ('Patient/123')."""
    return id_or_ref if "/" in id_or_ref else f"{resource_type}/{id_or_ref}"
