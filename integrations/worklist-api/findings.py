"""FastAPI router for the AI findings read/write surface on the Worklist API (#89).

The orchestrator publishes `interpretation.runTools` output here after the pre-read fan-out
completes; the OHIF extension reads back to render client-side annotation. No archive write
(contrast with #59's DICOM SC path).

Two routes, deliberately unauthenticated:
  * POST /findings           — orchestrator publishes. Same shape as /priority: source of truth
                                is the workflow, this store is the visibility layer.
  * GET /findings/{studyUID} — OHIF fetches. 404 signals "no findings yet" (workflow hasn't
                                run interpretation) so the extension can distinguish that from
                                "ran and found nothing" (200 with overallStatus STUBBED).

Auth posture matches /worklist and /priority (no auth, same-origin nginx boundary). Findings
are diagnostic signals but not narrative; the same trust boundary that protects the worklist
already protects this. Raise auth to HTTP Basic against /ws/rest/v1/session if the trust model
changes; the code shape is copy-pasteable from ack.py.

Kept as a sibling module (the assignment.py / ack.py pattern) so main.py stays the thin app
factory.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from findings_store import FindingsStore
from radagent_common.tracing import now_iso

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire-shape models — mirror contracts/skills/interpretation.schema.json in
# the fields we care about. Extra fields ignored on inbound so a schema
# evolution on interpretation does not 422 this endpoint.
# ---------------------------------------------------------------------------
class Finding(BaseModel):
    toolId: str
    label: str
    confidence: Optional[float] = None
    # The CAD margin (#86): rawScore is the head's raw sigmoid (op-norm inverted from the
    # calibrated confidence) and opThreshold is the head's raw operating point. Together they
    # give a real distance-from-threshold that the calibrated p=0.50-0.53 near-op cluster
    # compresses out of the display. Carried on POSITIVE and NEGATIVE findings from the pixel
    # tool (see agents/interpretation-assistant/handler.py); null on tools without an op point
    # (referral rules, stubs) and in the no-torch lane. Adding them here so the /worklist join
    # sees them -- pre-!134 they were dropped at this validator's `extra = "ignore"`.
    rawScore: Optional[float] = None
    opThreshold: Optional[float] = None
    evidenceRef: Optional[str] = None
    status: str

    class Config:
        extra = "ignore"


class FindingsPublish(BaseModel):
    studyInstanceUID: str = Field(..., min_length=1)
    workflowId: str = Field(..., min_length=1)
    findings: list[Finding]
    overallStatus: str
    generatedAt: str

    class Config:
        extra = "ignore"


def create_findings_router(store: FindingsStore) -> APIRouter:
    """Build the router with the store bound at construction; keeps the app factory declarative
    and lets tests inject an in-memory store without patching module globals."""
    router = APIRouter()

    @router.post("/findings", status_code=204)
    async def push_findings(body: FindingsPublish) -> None:
        """Orchestrator publishes interpretation.runTools output. Idempotent per studyInstanceUID
        (upsert). No response body — mirrors /priority for consistency with the existing
        orchestrator -> worklist-api publish channel."""
        store.put(
            study_instance_uid=body.studyInstanceUID,
            workflow_id=body.workflowId,
            findings=[f.model_dump() for f in body.findings],
            overall_status=body.overallStatus,
            generated_at=body.generatedAt,
            updated_at=now_iso(),
        )
        log.info(
            "findings published wf=%s study=%s n=%d status=%s",
            body.workflowId, body.studyInstanceUID, len(body.findings), body.overallStatus,
        )

    @router.get("/findings/{study_instance_uid}")
    async def get_findings(study_instance_uid: str) -> dict:
        """OHIF fetches on study open. 404 signals "workflow has not published yet" so the
        extension can distinguish that from "ran and found nothing." The latter returns 200
        with `overallStatus == "STUBBED"` and an empty or all-STUBBED findings array."""
        entry = store.get(study_instance_uid)
        if entry is None:
            raise HTTPException(status_code=404, detail="no findings yet")
        return entry

    return router
