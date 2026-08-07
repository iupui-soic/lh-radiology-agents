"""Temporal activities. ALL network/PHI I/O happens here (never in the workflow)."""
from __future__ import annotations
from typing import Any
from temporalio import activity

import os
from pathlib import Path
from urllib.parse import quote

import yaml

from radagent_common.client import call_agent_skill, start_agent_skill
from radagent_common.fhir_client import Fhir2Client
from radagent_common.worklist_client import (
    publish_findings as publish_findings_to_worklist,
    publish_priority as publish_priority_to_worklist,
    publish_state as publish_state_to_worklist,
)
from radagent_common.tracing import now_iso
from . import state
from .ingress_store import IngressStore, default_store_path


@activity.defn(name=state.ACT_CALL_AGENT)
async def call_agent_skill_activity(agent: str, skill_id: str, payload: dict[str, Any]) -> dict:
    """Invoke an A2A agent skill and return its (contract-validated) JSON output."""
    base_url = state.agent_base_url(agent)
    return await call_agent_skill(base_url, skill_id, payload)


@activity.defn(name=state.ACT_START_AGENT)
async def start_agent_skill_activity(agent: str, skill_id: str, payload: dict[str, Any],
                                     workflow_id: str) -> str:
    """Start a skill in push-notification mode and return its A2A taskId (#24).

    The agent POSTs the result to this ingress (/callbacks/a2a/<workflowId>), which relays it to
    the workflow as a `skill_completed` signal — the workflow correlates on the returned taskId.
    The shared A2A_CALLBACK_TOKEN (env) authenticates the callback; the callback URL carries the
    workflowId (so ingress needs no task->workflow index) and the skillId (so ingress can
    re-validate the delivered result against its contract before relaying it)."""
    base_url = state.agent_base_url(agent)
    callback_url = (f"{state.callback_base_url()}/callbacks/a2a/{workflow_id}"
                    f"?skill={quote(skill_id)}")
    return await start_agent_skill(
        base_url, skill_id, payload,
        callback_url=callback_url,
        callback_token=os.environ.get("A2A_CALLBACK_TOKEN", ""),
    )


@activity.defn(name=state.ACT_PUBLISH_PRIORITY)
async def publish_priority_activity(workflow_id: str, study_instance_uid: str, triage: dict) -> None:
    """Make the triage priority visible to the Worklist API (orchestrator = source of truth).

    Best-effort publish: the Worklist API's /priority endpoint stores the tier/score so OHIF's
    reading list can sort by priority (issue #20). A failed publish is a visibility loss, NOT a
    correctness bug — the study still gets interpreted, reported, and signed either way — so we
    swallow errors in the helper and log the outcome here rather than fail the workflow. See
    radagent_common.worklist_client for the "never raises" contract.

    No DICOM tag mutation.
    """
    tier = triage.get("priorityTier")
    score = triage.get("priorityScore")
    activity.logger.info(
        "publish priority wf=%s study=%s tier=%s score=%s",
        workflow_id, study_instance_uid, tier, score,
    )
    if tier is None or score is None:
        # Malformed triage output — the activity contract expects both fields; without them the
        # Worklist API would 422. Log and skip so we don't emit a doomed request.
        activity.logger.warning(
            "publish priority skipped (missing tier/score) wf=%s study=%s",
            workflow_id, study_instance_uid,
        )
        return
    await publish_priority_to_worklist(
        state.worklist_api_base_url(),
        study_instance_uid=study_instance_uid,
        workflow_id=workflow_id,
        priority_tier=tier,
        priority_score=score,
    )


@activity.defn(name=state.ACT_PUBLISH_STATE)
async def publish_state_activity(
    workflow_id: str, study_instance_uid: str, read_state: str, changed_at: str,
) -> None:
    """Tell the Worklist API the study's read is finished (#108).

    Before this existed the reading worklist had no read-complete signal of any kind: the row
    was assembled from Orthanc plus the triage priority publish, so a signed and archived study
    rendered identically to an unread one. Measured on the demo host during the #70 sign run,
    where a study archived and `/reading` still served all 100 rows unchanged.

    Best-effort, exactly like publish_priority: a failed publish means the row keeps showing as
    unread until something republishes, which is a visibility loss and not a correctness bug.
    The never-raises contract lives in radagent_common.worklist_client.

    No DICOM tag mutation, and the orchestrator remains the source of truth for the worklist.
    """
    activity.logger.info(
        "publish read-state wf=%s study=%s state=%s at=%s",
        workflow_id, study_instance_uid, read_state, changed_at,
    )
    await publish_state_to_worklist(
        state.worklist_api_base_url(),
        study_instance_uid=study_instance_uid,
        workflow_id=workflow_id,
        read_state=read_state,
        changed_at=changed_at,
    )


@activity.defn(name=state.ACT_PUBLISH_FINDINGS)
async def publish_findings_activity(
    workflow_id: str, study_instance_uid: str, ai_result: dict,
) -> None:
    """Publish interpretation.runTools output to the Worklist API for client-side CAD evidence
    rendering in OHIF (#89, the showcase-safe alternative to #59's archive-write path).

    Best-effort visibility publish: a failed publish means the OHIF extension does not render
    the AI banner for that study; the workflow's interpretation, reporting, and sign-off paths
    are unaffected. worklist_client.publish_findings never raises (bounded internal retry,
    logs on give-up); this activity swallows the return value into an info log line.

    No PHI beyond what interpretation.runTools already emits (tool id + label + confidence +
    evidenceRef pointer). The rendering-side extension applies its own policy (COMPLETE
    prominent, STUBBED silent, ERROR subdued) — this activity is dumb transport.
    """
    findings = ai_result.get("findings") or []
    overall_status = ai_result.get("overallStatus", "STUBBED")
    generated_at = ai_result.get("ranAt", "")
    activity.logger.info(
        "publish findings wf=%s study=%s n=%d status=%s",
        workflow_id, study_instance_uid, len(findings), overall_status,
    )
    await publish_findings_to_worklist(
        state.worklist_api_base_url(),
        study_instance_uid=study_instance_uid,
        workflow_id=workflow_id,
        findings=findings,
        overall_status=overall_status,
        generated_at=generated_at,
    )


@activity.defn(name=state.ACT_WRITE_PRESIGN_IMPRESSION)
async def write_presign_impression_activity(
    service_request_ref: str, patient_ref: str, impression_text: str,
) -> str:
    """Offer the pre-sign draft impression into the RIS as a preliminary DiagnosticReport (#26).

    Thin wrapper over Fhir2Client.write_presign_impression, which is idempotent per order and
    reuses the existing draft on a re-run. This is advisory and is the one fhir2 write path.
    Errors propagate so the workflow can retry with its bounded policy and then skip the draft on
    final failure, so a fhir2 outage never strands the human read (see workflow._presign_impression).
    Returns the written DiagnosticReport id.
    """
    report_id = await Fhir2Client().write_presign_impression(
        service_request_ref, patient_ref, impression_text,
    )
    activity.logger.info(
        "wrote pre-sign impression draft report=%s order=%s", report_id, service_request_ref,
    )
    return report_id


def _escalation_policy_path() -> Path:
    """Env override -> the in-repo default (baked into the worker image)."""
    default = Path(__file__).resolve().parent / "config" / "escalation-policy.yaml"
    return Path(os.environ.get("ESCALATION_POLICY_PATH", default))


@activity.defn(name=state.ACT_LOAD_ESCALATION_POLICY)
async def load_escalation_policy_activity(tier: str | None) -> list[dict]:
    """Resolve the sign-off escalation ladder for a priority tier (#29).

    Reads orchestrator/config/escalation-policy.yaml (CI-validated against
    contracts/escalation-policy.schema.json) and returns the tier's ordered rungs; an unknown or
    missing tier gets the policy's defaultTier ladder. Read fresh per gate entry -- no cache --
    so a policy edit (or re-pointed ESCALATION_POLICY_PATH) takes effect without a worker
    restart. Runs as an activity so the workflow stays deterministic: the resolved ladder is
    recorded in history, and a mid-wait policy edit cannot desync a replay.
    """
    with _escalation_policy_path().open() as f:
        policy = yaml.safe_load(f)
    tiers = policy["tiers"]
    ladder = tiers.get(tier or "") or tiers[policy["defaultTier"]]
    levels = ladder["levels"]
    # Validate the two schedule fields the workflow reads with bare subscripts: afterMinutes on
    # every rung, and repeatEveryMinutes on a repeating rung. A parseable-but-malformed policy
    # that omits one would otherwise raise a KeyError inside @workflow.run -- which fails only the
    # workflow TASK and hot-retries forever, wedging the gate with no escalation and no fallback.
    # Surfacing it here as an activity failure routes the gate to its legacy fallback instead (a
    # config disaster must not silence escalation). The in-repo policy is CI-validated against the
    # schema; this guards a live edit or an ESCALATION_POLICY_PATH override, both of which read
    # fresh per gate entry and bypass CI entirely.
    for rung in levels:
        if "afterMinutes" not in rung:
            raise ValueError(f"escalation rung missing afterMinutes: {rung!r}")
        if rung.get("repeat") and "repeatEveryMinutes" not in rung:
            raise ValueError(f"repeating escalation rung missing repeatEveryMinutes: {rung!r}")
    return levels


@activity.defn(name=state.ACT_RECORD_POLICY_FAILURE)
async def record_policy_failure_activity(workflow_id: str, tier: str | None, reason: str,
                                         attempts: int) -> None:
    """A sign-off gate could not load its escalation ladder -> make it operator-visible (#54).

    The gate's soft fallback stays exactly as it was (one tier timeout, one flat page): a broken
    policy deploy must degrade, never wedge. But degraded-and-silent is its own failure -- the
    ladder collapses to a single page and the system looks healthy -- so record a dead letter on
    the same surface an operator already watches, /admin/dead-letters, alongside the existing
    warning log.

    An activity because the workflow must stay deterministic (golden rule 5): this is the I/O.
    Writes to the SAME sqlite store the ingress poller uses (see ingress_store.default_store_path);
    the worker and the ingress share a container, so one file backs both writers and the endpoint.
    """
    store = IngressStore(default_store_path())
    try:
        store.add_policy_load_failure(workflow_id, tier, attempts, reason, now_iso())
    finally:
        store.close()
    activity.logger.error(
        "DEAD LETTER: escalation policy unavailable for %s (tier=%s) after %d attempt(s); the "
        "sign-off gate fell back to a single flat page. See /admin/dead-letters",
        workflow_id, tier or "unknown", attempts,
    )


@activity.defn(name=state.ACT_RECORD_SIGNOFF_ABANDONED)
async def record_signoff_abandoned_activity(workflow_id: str, tier: str | None,
                                            pages: int) -> None:
    """A sign-off gate ran out of ladder with nobody acknowledging (#57) -> make it visible.

    The gate no longer holds forever (see StudyWorkflow._hold_signoff_gate): it releases the study
    to COMMUNICATE so the finding that made verification FAIL actually gets dispatched. But a
    release nobody authorised is exactly the thing an operator must see, so it lands on the surface
    they already watch: /admin/dead-letters. The study archives with the FAIL unacknowledged.

    An activity because the workflow must stay deterministic (golden rule 5): this is the I/O.
    """
    store = IngressStore(default_store_path())
    try:
        store.add_signoff_abandoned(workflow_id, tier, pages, now_iso())
    finally:
        store.close()
    activity.logger.error(
        "DEAD LETTER: sign-off gate for %s (tier=%s) exhausted its ladder after %d page(s) with no "
        "acknowledgement; released to COMMUNICATE with the verification FAIL unacknowledged. "
        "See /admin/dead-letters",
        workflow_id, tier or "unknown", pages,
    )


# Where a paged human goes to release the gate (#57). Injected into every ladder page so a study
# sitting at the gate always carries the pointer to its own exit -- a page that says "act" without
# saying HOW is how a gate ends up stranded. Read here, in the I/O layer: the workflow may not read
# env (golden rule 5). Unset (dev/compose default) simply omits the field.
SIGNOFF_OVERRIDE_URL = os.environ.get("SIGNOFF_OVERRIDE_URL", "")


@activity.defn(name=state.ACT_ESCALATE)
async def escalate_activity(workflow_id: str, reason: str, escalation: dict | None = None) -> dict:
    """The sign-off human gate is still open with no radiologist action -> page a human (#23/#29).

    The orchestrator owns the durable escalation clock (Temporal); each fired ladder rung maps
    onto one Communications Agent dispatch via the A2A `comms.dispatch` boundary. The payload's
    `escalation` slice (contracts/escalation-policy.schema.json $defs/dispatchEscalation) tells
    the agent who to reach (targetRole), how (channels), and how loudly (urgency) -- policy + IDs
    only (lean-reference; no PHI in the message). `escalation=None` is the legacy flat page, kept
    for the workflow's fallback when the policy itself cannot be loaded.

    TODO(M3): wire the REAL Communications Agent (CritCom). This dispatch targets the in-repo
    comms.dispatch STUB; CritCom is shaped differently and needs an adapter:
      1. protocol: A2A `message/send` + `X-API-Key` with a natural-language instruction, not this
         structured `comms.dispatch` skill;
      2. identifiers: a real FHIR ref (DiagnosticReport / ServiceRequest / DICOM accession),
         resolved from the study (#11) -- `workflowId` is meaningless to CritCom;
      3. context/creds: FHIR endpoint + token as A2A metadata, plus a Gemini/Vertex key;
      4. reply: parse CritCom's Task/free-text result back into dispatchStatus/channelResults.
    Note: CritCom's own gate is 'ordering physician didn't ACK a critical result', a DIFFERENT gate
    from this 'radiologist didn't SIGN' one -- when wiring CritCom, don't double-page.
    """
    activity.logger.warning(
        "ESCALATE wf=%s level=%s attempt=%s reason=%s",
        workflow_id, (escalation or {}).get("level"), (escalation or {}).get("attempt", 1), reason,
    )
    payload: dict[str, Any] = {"studyContext": {"workflowId": workflow_id}}
    if escalation is None:
        # Legacy flat page: a FAIL verification is the only "page now" lever the pre-#29 payload
        # offered (it trips the on-call pager route in agents/communications handler._is_critical).
        payload["verification"] = {"verificationStatus": "FAIL"}
    else:
        # Pass forward only the dispatch slice of the rung (its scheduling fields stay internal).
        payload["escalation"] = {
            "level": escalation["level"],
            "targetRole": escalation["targetRole"],
            "channels": escalation["channels"],
            "urgency": escalation["urgency"],
            "attempt": escalation.get("attempt", 1),
            "reason": reason,
        }
        # Every page carries the way OUT of the gate it is paging about (#57). The person being
        # woken is the person who can release the study; telling them to act without telling them
        # how is how a study sits at this gate until its ladder runs out.
        if SIGNOFF_OVERRIDE_URL:
            payload["escalation"]["overrideUrl"] = SIGNOFF_OVERRIDE_URL.replace(
                "{workflowId}", workflow_id)
    return await call_agent_skill(state.agent_base_url("communications"), "comms.dispatch", payload)


# Convenience for ingress: the RIS poller uses this to find finalized reports.
# Returns (finalized records oldest-first, high-water `_lastUpdated` cursor).
async def poll_finalized_reports(since_iso: str) -> tuple[list[dict], str | None]:
    return await Fhir2Client().poll_finalized_reports(since_iso)
