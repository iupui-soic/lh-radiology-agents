"""Communications Agent handler — owner: Pranathi (lead).

The real CritCom (Critical-Results Communication) agent (#52). Three skills:
  - comms.dispatch : classify the finding (ACR) + notify the ordering provider + open an ack clock
  - comms.checkAck : read the acknowledgement Task; report status + `overdue`
  - comms.escalate : an unacknowledged critical result goes to the on-call provider

TWO DIFFERENT GATES. Do not confuse them, and do not double-page:
  * #29's escalation ladder is the "radiologist hasn't RESOLVED the verification hold" gate: a
    SIGNED report failed verification and sits at the sign-off gate awaiting its radiologist.
    Its fired rung arrives here as an `escalation` input slice on comms.dispatch. The ladder
    already chose who/how/how-loudly AND owns the cadence (Temporal durable timers fire the next
    rung), so those channels are dispatched VERBATIM and NO ack clock is opened -- a comms-side
    Task would put the same human on two clocks at once.
  * checkAck/escalate are the "physician didn't ACK a critical result" gate: a signed report whose
    critical finding was communicated, and nobody confirmed receipt. That loop is what the
    Communication/Task pair in the comms ledger tracks.

Handlers stay pure: radagent_common + siblings only, never a2a.* (golden rule 4). Clinical context
is read from fhir2; the notification and its ack are written to the comms ledger (golden rule 2 --
the A2A payload carries IDs, and content is fetched from source).

The agent NEVER self-fires a timer. It opens the ack clock and reports the deadline; the
orchestrator owns the durable wait and calls back on comms.checkAck / comms.escalate (MR 4).

Contracts: contracts/skills/comms.{dispatch,checkAck,escalate}.schema.json
"""
from __future__ import annotations

import logging
import re

from radagent_common.comms_ledger import CommsLedgerClient
from radagent_common.fhir_client import Fhir2Client
from radagent_common.tracing import now_iso

from classifier import ACRCategory, classify, escalation_ack_minutes
from composer import compose_notification
from routing import derive_specialty, out_of_specialty_fallback
from tools import (
    ack_state,
    deliver_critical_result_to_chart,
    dispatch_communication,
    escalate_to_on_call,
    open_ack_task,
    resolve_on_call_provider,
    resolve_ordering_provider,
)

AGENT_VERSION = "0.2.0"
_log = logging.getLogger("agents.communications")

_ROUTINE_CHANNEL = "ehr-inbox"       # every finalized report posts to the ordering provider's inbox
_CRITICAL_CHANNEL = "oncall-pager"   # critical results also page (closed-loop comms)

# Lazily constructed so importing this module has no side effect; tests/harness override these.
_FHIR: Fhir2Client | None = None
_LEDGER: CommsLedgerClient | None = None


def _fhir() -> Fhir2Client:
    global _FHIR
    if _FHIR is None:
        _FHIR = Fhir2Client()
    return _FHIR


def _ledger() -> CommsLedgerClient:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = CommsLedgerClient()
    return _LEDGER


def _refs(payload: dict) -> tuple[str, str]:
    """(patient_ref, service_request_ref) -- the explicit inputs when the orchestrator resolved
    them, else the StudyContext envelope's."""
    ctx = payload["studyContext"]
    patient = payload.get("patientId") or (ctx.get("patient") or {}).get("fhirPatientId") or ""
    order = (payload.get("serviceRequestId")
             or (ctx.get("order") or {}).get("fhirServiceRequestId") or "")
    return patient, order


def _out(payload: dict, **fields) -> dict:
    return {"schemaVersion": "1.0.0", "workflowId": payload["studyContext"]["workflowId"],
            "agentVersion": AGENT_VERSION, **fields}


# --- comms.dispatch -------------------------------------------------------------------

async def _dispatch(payload: dict) -> dict:
    escalation = payload.get("escalation") or {}
    if escalation:
        # A fired sign-off ladder rung (#29) -- the OTHER gate. The ladder picked the channels, so
        # send them as asked. No Communication, no ack clock: the ladder itself is the clock
        # (Temporal owns the cadence and the next rung), and a comms-side Task would put the
        # same human on two clocks at once.
        return _out(
            payload,
            dispatchStatus="SENT",
            channelResults=[{"channel": c, "status": "SENT"} for c in escalation["channels"]],
            dispatchedAt=now_iso(),
        )

    result = classify(payload.get("impression") or {}, payload.get("verification") or {})

    # Routine result: it posts to the EHR inbox like any report, and there is nothing to
    # acknowledge -- opening an ack clock on a normal chest X-ray is how alert fatigue starts.
    if not result.is_critical:
        return _out(
            payload,
            dispatchStatus="SENT",
            acrCategory=result.category.value,
            channelResults=[{"channel": _ROUTINE_CHANNEL, "status": "SENT"}],
            dispatchedAt=now_iso(),
        )

    patient_ref, order_ref = _refs(payload)
    recipient = await resolve_ordering_provider(_fhir(), order_ref)
    out_of_specialty = False
    if not recipient:
        # No requester on the order (e.g. a study ingested unresolved, #11). A critical finding
        # with nobody to tell must not be silently dropped -- go to on-call, narrowed to the
        # study's specialty when one derives (#58: an intracranial bleed should page neuro call,
        # not whoever the directory lists first).
        specialty = derive_specialty(payload["studyContext"].get("study") or {})
        resolution = await resolve_on_call_provider(
            _ledger(), specialty=specialty, fallback=out_of_specialty_fallback())
        recipient, out_of_specialty = resolution.reference, resolution.out_of_specialty
        _log.warning("no requester on %s; addressing the critical result to on-call (%s%s)",
                     order_ref or "<no order>", recipient,
                     ", out of specialty" if out_of_specialty else "")
    if not recipient:
        # Nobody to tell, at all. SKIPPED is the honest answer -- reporting SENT would claim a page
        # that never happened. Nothing re-pages in response (the #29 ladder pages only at the
        # post-sign verification hold, decoupled from dispatch): the miss lives in this log line
        # and the workflow's record.
        _log.error("no recipient for a %s finding on %s", result.category.value, order_ref)
        return _out(
            payload,
            dispatchStatus="SKIPPED",
            acrCategory=result.category.value,
            channelResults=[],
            dispatchedAt=now_iso(),
        )

    # Optional LLM prose (composer.py): upgrades ONLY the message text. Category, recipient and
    # deadline are already decided above; None (the default) keeps the deterministic one-liner.
    message = await compose_notification(
        acr_category=result.category.value, finding=result.finding,
        ack_minutes=result.ack_minutes,
    ) or result.finding
    comm = await dispatch_communication(
        _ledger(),
        patient_ref=patient_ref,
        service_request_ref=order_ref,
        recipient_ref=recipient,
        acr_category=result.category.value,
        # The composed prose (or the deterministic label) is what the physician reads; the chart
        # write below deliberately keeps result.finding -- the LABEL -- so the Observation stays
        # minimal-content whatever the composer produced.
        finding=message,
        out_of_specialty=out_of_specialty,
    )
    task, deadline = await open_ack_task(
        _ledger(),
        communication_ref=f"Communication/{comm.id}",
        patient_ref=patient_ref,
        owner_ref=recipient,
        ack_minutes=result.ack_minutes or 60,
    )
    dispatched_at = now_iso()
    # ehr-inbox goes real behind the #79 flag: the notification lands IN the chart (an Observation
    # stamped with the dedicated concept), and the channel result reports what actually happened.
    # Flag off (default) keeps the stubbed v1 claim unchanged. Runs AFTER the ledger writes so the
    # chart entry can name the ack task; its failure never touches the page (best-effort).
    inbox_status = await deliver_critical_result_to_chart(
        _fhir(),
        patient_ref=patient_ref,
        service_request_ref=order_ref,
        finding=result.finding,
        accession=(payload["studyContext"].get("study") or {}).get("accessionNumber") or "",
        ack_task_id=task.id or "",
        sent_iso=dispatched_at,
    )
    return _out(
        payload,
        dispatchStatus="SENT",
        acrCategory=result.category.value,
        communicationId=comm.id or "",
        taskId=task.id or "",
        deadline=deadline.isoformat(),
        recipient=recipient,
        channelResults=[{"channel": _ROUTINE_CHANNEL, "status": inbox_status},
                        {"channel": _CRITICAL_CHANNEL, "status": "SENT"}],
        dispatchedAt=dispatched_at,
    )


# --- comms.checkAck -------------------------------------------------------------------

async def _check_ack(payload: dict) -> dict:
    task_id = payload["taskId"]
    status, deadline, overdue = ack_state(await _ledger().get_task(task_id))
    return _out(
        payload,
        taskId=task_id,
        ackStatus=status,
        deadline=deadline.isoformat() if deadline else "",
        overdue=overdue,
        checkedAt=now_iso(),
    )


# Every escalation rung prepends "[ESCALATED] " to the finding it pages with, and `_escalate`
# reads that finding back off the PREVIOUS Communication. Unstripped, rung 2 pages
# "[ESCALATED] [ESCALATED] pneumothorax" and it compounds from there -- seen live on the demo
# host during the #76 arc 2 rehearsal, on the acknowledgement receipt a physician actually read.
# The prefix is routing decoration, not part of the finding, so strip it back to the clinical
# text before reusing it: the pager then carries exactly one marker however many rungs have
# fired, and the chart entry carries none.
_ESCALATED_PREFIX = re.compile(r"^(?:\s*\[ESCALATED\]\s*)+", re.IGNORECASE)


def _plain_finding(finding: str) -> str:
    """The clinical finding with any accumulated "[ESCALATED] " markers removed."""
    return _ESCALATED_PREFIX.sub("", finding or "").strip()


# --- comms.escalate -------------------------------------------------------------------

async def _escalate(payload: dict) -> dict:
    task_id = payload["taskId"]
    patient_ref, order_ref = _refs(payload)

    # Re-derive the urgency from the Task's own Communication rather than trusting an input: the
    # escalation must carry the SAME category as the notification nobody answered.
    task = await _ledger().get_task(task_id)
    acr, finding = ACRCategory.CAT1.value, "unacknowledged critical result"
    if task.focus and task.focus.reference:
        comm = await _ledger().get_communication(task.focus.reference.split("/")[-1])
        if comm.category and comm.category[0].coding:
            acr = comm.category[0].coding[0].code or acr
        finding = _plain_finding(comm.finding_summary) or finding

    result = await escalate_to_on_call(
        _ledger(),
        overdue_task_id=task_id,
        patient_ref=patient_ref,
        service_request_ref=order_ref,
        acr_category=acr,
        finding=finding,
        # NOT the category's window. This result is already late -- re-using it would give on-call a
        # fresh 24h on a Cat2, so a finding that had to be communicated within 24h could take 48.
        # See classifier.escalation_ack_minutes (this replaces a payload.get("ackMinutes") read the
        # input schema could never satisfy -- @sunbiz on #52).
        ack_minutes=escalation_ack_minutes(),
        # Same routing as dispatch's on-call fallback (#58): the escalated page aims at the
        # study's own specialty rota first.
        specialty=derive_specialty(payload["studyContext"].get("study") or {}),
        fallback=out_of_specialty_fallback(),
    )
    # The escalated loop needs the SAME chart delivery the first dispatch got (#123). Two things
    # depend on this call and both were broken without it, found driving arc 2 in a browser:
    #
    #   * the chart entry is anchored on the accession, so re-delivering UPDATES it in place to
    #     name the LIVE ack task. Without this it kept pointing at the task the escalation had
    #     just marked FAILED, so the physician's only link was a dead one;
    #   * the signed ack link is minted INSIDE write_critical_result_notification, so the on-call
    #     recipient got a page whose whole payload was "[ESCALATED] <finding>" and no way to
    #     acknowledge. An escalation that cannot be acknowledged just escalates again.
    #
    # The plain `finding`, not the "[ESCALATED] " prefixed one the Communication carries: the
    # chart entry is the record of THIS critical result, one per accession, and rewriting its
    # text on every rung would churn the patient's chart to say nothing new.
    #
    # Best-effort exactly like dispatch's: deliver_critical_result_to_chart swallows every
    # exception and returns a status, so a chart-write failure never costs the page or the ledger
    # record that already exist. Nothing is added to the return value -- comms.escalate's output
    # is additionalProperties:false and this is a side effect, not a new contract field.
    if result.get("escalated") and result.get("newTaskId"):
        await deliver_critical_result_to_chart(
            _fhir(),
            patient_ref=patient_ref,
            service_request_ref=order_ref,
            finding=finding,
            accession=(payload["studyContext"].get("study") or {}).get("accessionNumber") or "",
            ack_task_id=result["newTaskId"],
            sent_iso=now_iso(),
        )
    return _out(payload, escalatedAt=now_iso(), **result)


_SKILLS = {
    "comms.dispatch": _dispatch,
    "comms.checkAck": _check_ack,
    "comms.escalate": _escalate,
}


async def handle(skill_id: str, payload: dict) -> dict:
    fn = _SKILLS.get(skill_id)
    if fn is None:
        raise ValueError(f"unexpected skill {skill_id}")
    return await fn(payload)
