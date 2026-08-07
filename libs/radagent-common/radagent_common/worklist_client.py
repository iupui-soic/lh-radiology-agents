"""Thin HTTP client for the Worklist API (see integrations/worklist-api/main.py).

The Worklist API is a plain FastAPI service (not an A2A agent) — it exposes
POST /priority for the orchestrator's publish channel and GET /worklist for
OHIF. This module is a per-external-system client, matching the pattern of
orthanc_client.py and fhir_client.py.

Best-effort by design (READ THIS): the publish is *visibility*, not
*correctness*. If the Worklist API is unreachable when the orchestrator
publishes a triage result, the study still gets interpreted, reported, and
signed — it just doesn't show priority-ordered in OHIF. So this helper NEVER
raises; a failed publish is a WARNING log line and a falsy return value, not
an exception that would fail the Temporal activity and block the workflow.

Bounded self-heal (per #20 review): publish_priority_activity is called once
per study on the way to READY_FOR_READ, and nothing republishes if that call
loses to a 2-second network blip. The whole point of the feature is that an
urgent study floats to the top of the reading list, so silently dropping the
publish on a transient blip turns a cosmetic outage into a clinical
prioritization failure. To self-heal transient blips without ever raising,
the helper retries a bounded number of times inside itself (see
_PUBLISH_MAX_ATTEMPTS below), with exponential backoff and jitter. Same
philosophy as BOUNDED_ACTIVITY_RETRY in orchestrator/workflow.py.

Retries fire on network errors and 5xx server responses; 4xx (client bug in
the payload we sent — bad tier, out-of-range score) does NOT retry because
the same payload will 422 forever. Any error path still returns False, never
raises: the activity keeps its "workflow never fails on a publish outage"
contract.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import httpx

_log = logging.getLogger(__name__)

# Short per-attempt timeout: this call is on the readiness-for-read path; a
# slow publish should not stall the transition. The bounded retry loop
# (see _PUBLISH_MAX_ATTEMPTS) handles transient blips.
_DEFAULT_TIMEOUT = 5.0

# Bounded retry to self-heal transient blips without dragging the read path.
# Mirrors orchestrator.workflow.BOUNDED_ACTIVITY_RETRY (maximum_attempts=3).
# Worst-case retry latency at the current backoff (0.25s + 0.5s = 0.75s of
# sleep) sits well under the activity's start_to_close_timeout budget.
_PUBLISH_MAX_ATTEMPTS = 3
_PUBLISH_BACKOFF_BASE = 0.25  # seconds; doubles per retry


async def publish_priority(
    base_url: str,
    study_instance_uid: str,
    workflow_id: str,
    priority_tier: str,
    priority_score: int,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bool:
    """POST the study's triage priority to the Worklist API.

    Returns True on 2xx, False on any error (network, timeout, non-2xx) after
    bounded retries are exhausted. Never raises.

    Retries: bounded internal loop on network errors and 5xx server errors.
    Does NOT retry on 4xx (a Worklist API 422 means we sent a malformed
    payload — retrying with the same body will fail identically). The caller
    (publish_priority_activity) uses the return value only for its structured
    log line; there is no branching on it.

    Payload shape matches integrations/worklist-api/main.py's PriorityPush
    pydantic model.
    """
    return await _publish_json(
        base_url.rstrip("/") + "/priority",
        {
            "studyInstanceUID": study_instance_uid,
            "workflowId": workflow_id,
            "priorityTier": priority_tier,
            "priorityScore": priority_score,
        },
        label="worklist",
        base_url=base_url,
        workflow_id=workflow_id,
        study_instance_uid=study_instance_uid,
        timeout=timeout,
    )


async def _publish_json(
    url: str,
    payload: dict,
    label: str,
    base_url: str,
    workflow_id: str,
    study_instance_uid: str,
    timeout: float,
) -> bool:
    """The shared never-raises, bounded-retry POST behind every publish_* helper.

    Extracted when publish_state (#108) became the third caller: the priority and findings
    helpers already carried byte-identical copies of this loop, differing only in the log
    label, so a third copy would have made the same bug fixable in three places. `label`
    keeps each caller's log lines exactly as they were, because operators and the run-book
    grep for them.
    """
    last_reason: Optional[str] = None
    for attempt in range(1, _PUBLISH_MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                resp = await c.post(url, json=payload)
        except (httpx.InvalidURL, httpx.UnsupportedProtocol) as e:
            # The configured base_url is fundamentally unusable: a bad port, a
            # bracketed host, a missing or unknown scheme. This is a config error,
            # not a transient blip. Retrying the same URL fails identically, so give
            # up now without burning the readiness-for-read budget. Caught explicitly
            # because httpx.InvalidURL is NOT an httpx.HTTPError. Without this clause
            # it escapes and breaks the never-raises contract, and because the
            # activity runs under Temporal's unbounded retry it wedges the study at
            # READY_FOR_READ forever instead of degrading to a logged visibility loss.
            _log.warning(
                f"{label} publish skipped wf=%s study=%s: unusable WORKLIST_API_URL %r (%s, no retry)",
                workflow_id, study_instance_uid, base_url, e,
            )
            return False
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            # Transient by classification: network hiccup, DNS blip, timeout.
            last_reason = f"network ({e.__class__.__name__}: {e})"
            if attempt < _PUBLISH_MAX_ATTEMPTS:
                await _sleep_backoff(attempt)
                continue
            _log.warning(
                f"{label} publish failed after %s attempts wf=%s study=%s: %s",
                _PUBLISH_MAX_ATTEMPTS, workflow_id, study_instance_uid, last_reason,
            )
            return False
        except Exception as e:  # noqa: BLE001 (never-raises backstop)
            # The docstring promises this helper NEVER raises: an escape here fails
            # the publish activity and, under its unbounded retry, wedges the study.
            # So we swallow any unforeseen error too, treating it as permanent (no
            # retry) and logging it distinctly so a genuine bug still surfaces.
            # asyncio.CancelledError is a BaseException, not an Exception, so
            # Temporal activity cancellation still propagates through this clause.
            _log.warning(
                f"{label} publish skipped wf=%s study=%s: unexpected %s: %s (no retry)",
                workflow_id, study_instance_uid, e.__class__.__name__, e,
            )
            return False

        if 200 <= resp.status_code < 300:
            return True
        if 400 <= resp.status_code < 500:
            # Caller-side bug — same payload will 422 again. Log and give up.
            _log.warning(
                f"{label} publish rejected wf=%s study=%s: HTTP %s %s (no retry, caller bug)",
                workflow_id, study_instance_uid, resp.status_code, _brief(resp),
            )
            return False
        # 5xx: server-side transient. Retry within budget.
        last_reason = f"HTTP {resp.status_code} {_brief(resp)}"
        if attempt < _PUBLISH_MAX_ATTEMPTS:
            await _sleep_backoff(attempt)
            continue
        _log.warning(
            f"{label} publish failed after %s attempts wf=%s study=%s: %s",
            _PUBLISH_MAX_ATTEMPTS, workflow_id, study_instance_uid, last_reason,
        )
        return False

    # Loop always returns from inside; this line is unreachable but keeps type
    # checkers happy and marks the "should never happen" boundary explicitly.
    return False


async def publish_findings(
    base_url: str,
    study_instance_uid: str,
    workflow_id: str,
    findings: list[dict],
    overall_status: str,
    generated_at: str,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bool:
    """POST the study's interpretation.runTools output to the Worklist API (#89, client-side
    CAD evidence rendering).

    Same never-raises / bounded-retry contract as publish_priority: a failed publish is a
    visibility loss, NOT a correctness bug. The workflow still interprets, reports, signs;
    the study just won't show the AI evidence banner in OHIF. Returns True on 2xx, False
    after bounded retries.

    Payload shape matches integrations/worklist-api/findings.py's FindingsPublish model.
    """
    return await _publish_json(
        base_url.rstrip("/") + "/findings",
        {
            "studyInstanceUID": study_instance_uid,
            "workflowId": workflow_id,
            "findings": findings,
            "overallStatus": overall_status,
            "generatedAt": generated_at,
        },
        label="findings",
        base_url=base_url,
        workflow_id=workflow_id,
        study_instance_uid=study_instance_uid,
        timeout=timeout,
    )


async def publish_state(
    base_url: str,
    study_instance_uid: str,
    workflow_id: str,
    read_state: str,
    changed_at: str,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bool:
    """POST the study's terminal workflow state to the Worklist API (#108).

    Without this the reading worklist has no read-complete signal at all: a study whose
    workflow archived still renders exactly like an unread one, so a signed study sits in the
    list looking unread and a second reader has nothing telling them otherwise. Measured on the
    demo host during the #70 sign run.

    Same never-raises / bounded-retry contract as the two helpers above: a failed publish is a
    visibility loss, not a correctness bug. The study is signed, verified and archived either
    way; the row just keeps showing as unread until the next publish.

    Payload shape matches integrations/worklist-api/main.py's StatePush model.
    """
    return await _publish_json(
        base_url.rstrip("/") + "/state",
        {
            "studyInstanceUID": study_instance_uid,
            "workflowId": workflow_id,
            "readState": read_state,
            "changedAt": changed_at,
        },
        label="read-state",
        base_url=base_url,
        workflow_id=workflow_id,
        study_instance_uid=study_instance_uid,
        timeout=timeout,
    )


async def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with jitter — 0.25s / 0.5s / 1s in the base case,
    ± up to 50% jitter to spread reconnect storms when the Worklist API
    restarts and every in-flight workflow retries at once."""
    base = _PUBLISH_BACKOFF_BASE * (2 ** (attempt - 1))
    await asyncio.sleep(base * (1.0 + random.random() * 0.5))


def _brief(resp: httpx.Response) -> str:
    """First 200 chars of the response body — enough to see a FastAPI 422 detail
    without spamming the log with a full stack trace on a large error page."""
    try:
        return (resp.text or "")[:200]
    except Exception:  # noqa: BLE001 — defensive; log helper must never raise
        return "<unreadable>"
