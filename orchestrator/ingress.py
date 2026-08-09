"""Event ingress (FastAPI): Orthanc stable-study webhook + RIS DiagnosticReport poller.

Owner: Pranathi (lead). Trigger map: ARCHITECTURE.md
- POST /webhooks/orthanc : starts one StudyWorkflow per new stable study.
- background poller       : detects RIS sign-off and signals the waiting workflow.
"""
from __future__ import annotations

import asyncio
import hmac
import html as html_mod
import logging
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from radagent_common import validate_against, paths
from radagent_common.client import parse_push_callback
from radagent_common.fhir_client import Fhir2Client
from radagent_common.openmrs_rest import OpenmrsRestClient
from radagent_common.orthanc_client import OrthancClient
from radagent_common.validation import validate_skill_output
from radagent_common.tracing import now_iso, new_trace_id, new_span_id, init_tracing, tracing_enabled
from .state import TASK_QUEUE
from .workflow import StudyWorkflow
from .ingress_store import (
    KIND_POST_ARCHIVE_ADDENDUM,
    KIND_SIGNOFF_DROP,
    IngressStore,
    default_store_path,
)
from . import activities

TEMPORAL_TARGET = os.environ.get("TEMPORAL_TARGET", "temporal:7233")
POLL_INTERVAL_S = int(os.environ.get("RIS_POLL_INTERVAL_S", "30"))
# Shared secret for A2A push callbacks (#24): agents echo it in X-A2A-Notification-Token.
# Empty (the default) accepts unauthenticated callbacks â€" dev/compose posture, same as the
# Orthanc webhook; set it in any deployment that leaves the compose network.
A2A_CALLBACK_TOKEN = os.environ.get("A2A_CALLBACK_TOKEN", "")
# Shared secret for the sign-off override (#57). REQUIRED, unlike the two above: those endpoints
# deliver facts, this one waives a safety verdict on a report. Unset -> the endpoint 503s rather
# than accepting anonymous releases (see signoff_override).
SIGNOFF_OVERRIDE_TOKEN = os.environ.get("SIGNOFF_OVERRIDE_TOKEN", "")
# Cap on the free-text reason. It is a clinician's audit note, not a report: bounded so a stray
# paste cannot bloat workflow history (which is replayed on every worker pickup).
SIGNOFF_REASON_MAX = int(os.environ.get("SIGNOFF_REASON_MAX", "500"))
# Reconcile the index against Temporal every N polls (plus once at startup) to evict rows for
# workflows that completed/terminated without ever delivering a report.
RECONCILE_EVERY_POLLS = int(os.environ.get("INGRESS_RECONCILE_EVERY_POLLS", "120"))
# A failing fhir2 poll is warned on the first failure, then every Nth, so a persistent outage
# (fhir2 down, credentials rejected â€" #53) stays visible without flooding the log every poll.
# Clamped to >= 1: a 0 would make the throttle modulo raise inside the poller's own exception
# handler, permanently killing the loop the throttle exists to keep observable.
FAILED_POLLS_PER_WARNING = max(1, int(os.environ.get("INGRESS_FAILED_POLLS_PER_WARNING", "10")))

_client: Client | None = None
_log = logging.getLogger("orchestrator.ingress")

# Greedy to the LAST @ before the path (so an un-encoded @ inside a password masks whole);
# ? and # excluded so a query/fragment @ after a pathless host never swallows the hostname.
_USERINFO_RE = re.compile(r"://[^/?#\s]+@")


def _redacted(exc: BaseException) -> str:
    """str(exc) with any URL userinfo masked -- exception text is the one place a
    credentials-in-the-URL deployment would leak them into routine logs."""
    return _USERINFO_RE.sub("://***@", str(exc))


# Durable report->workflow index + poll cursor (#6): must survive an ingress restart during the
# hours-long human-gate wait, or the sign-off is silently lost. Backed by SQLite at
# INGRESS_STORE_PATH, created lazily so importing this module has no side effect; tests override
# `_STORE` with a temp DB. See orchestrator/ingress_store.py.
_STORE: IngressStore | None = None


def _default_store_path() -> str:
    """Absolute + CWD-independent, so a restart from any working directory finds the same DB.

    Delegates to ingress_store so the worker's dead-letter activity (#54) resolves the SAME file
    from the other process in this container â€" one store, one /admin/dead-letters.
    """
    return default_store_path()


def _store() -> IngressStore:
    global _STORE
    if _STORE is None:
        _STORE = IngressStore(_default_store_path())
    return _STORE


# Read-only OpenMRS REST client for accession -> RadiologyOrder resolution (#11, #70). fhir2 cannot
# serve this: `ServiceRequest?identifier=<accession>` returns HTTP 400 on the deployed fhir2 4.1.0
# and the ServiceRequest exposes no accession identifier, so the accession is resolved through the
# radiology module's own REST search handler instead (see radagent_common.openmrs_rest). Lazily
# constructed so importing this module has no side effect; tests override `_OPENMRS_REST` with a fake.
_OPENMRS_REST: OpenmrsRestClient | None = None


def _openmrs_rest() -> OpenmrsRestClient:
    global _OPENMRS_REST
    if _OPENMRS_REST is None:
        _OPENMRS_REST = OpenmrsRestClient()
    return _OPENMRS_REST


# Read-only Orthanc client for the study's DICOM description (#62). Same lazy shape as _FHIR.
_ORTHANC: OrthancClient | None = None


def _orthanc() -> OrthancClient:
    global _ORTHANC
    if _ORTHANC is None:
        _ORTHANC = OrthancClient()
    return _ORTHANC


async def _temporal() -> Client:
    global _client
    if _client is None:
        # OTel (#28): when enabled, the interceptor spans workflow starts/signals and injects trace
        # context into Temporal headers, linking the webhook span to the worker-side workflow span.
        interceptors: list = []
        if tracing_enabled():
            from temporalio.contrib.opentelemetry import TracingInterceptor
            interceptors = [TracingInterceptor()]
        _client = await Client.connect(TEMPORAL_TARGET, interceptors=interceptors)
    return _client


async def _build_study_context(event: dict) -> dict:
    """Map an Orthanc event to a (schema-valid) StudyContext: the patient + order resolved from the
    accession via fhir2 (#11), the study's description read back from Orthanc (#62).

    Both lookups are best-effort. A fhir2 error or miss falls back to the `Patient/UNRESOLVED`
    placeholder; an Orthanc error leaves the description unset. The workflow always starts --
    ingestion must never fail the PACS. A later stable re-fire (see orthanc_webhook) repairs an
    UNRESOLVED first pass without a restart.

    The two lookups hit different servers and neither needs the other's answer, so they run
    concurrently: the PACS is blocked on this webhook, and there is no reason to make it wait for
    fhir2 and Orthanc in series.
    """
    wf_id = f"wf_{event['orthancStudyId']}"
    (patient, order), description = await asyncio.gather(
        _resolve_patient_order(event.get("accessionNumber")),
        _study_description(event["orthancStudyId"]),
    )
    study = {
        "studyInstanceUID": event["studyInstanceUID"],
        "accessionNumber": event.get("accessionNumber"),
        "orthancStudyId": event["orthancStudyId"],
        "modality": event["modality"],
    }
    # Omitted rather than set to "" when Orthanc has no description: the schema types it as a
    # string, and an absent key says "unknown" where an empty one would assert "no description".
    if description:
        study["studyDescription"] = description
    return {
        "schemaVersion": "1.0.0",
        "workflowId": wf_id,
        "study": study,
        "patient": patient,
        "order": order,
        "meta": {
            "traceId": new_trace_id(),
            "spanId": new_span_id(),
            "emittedAt": now_iso(),
            "source": "orchestrator.ingress",
        },
    }


# The order fields beyond the join ref that the StudyContext carries. `triage.score` scores on both
# and can do nothing without them, so dropping them here silently demotes every study to ROUTINE --
# which is exactly what ingress did until #61.
_ORDER_SIGNALS = ("priority", "reasonCode")


async def _resolve_patient_order(accession: str | None) -> tuple[dict, dict]:
    """(patient, order) blocks for the StudyContext. Resolve the real refs -- and the order's
    urgency signal, which triage scores on (#61) -- from the accession; on a miss or ANY failure,
    return the UNRESOLVED placeholder -- never fail ingestion (#11).

    Resolution goes through the radiology module's REST search handler, not fhir2 (#70): fhir2 has no
    searchable accession (`ServiceRequest?identifier=` 400s on 4.1.0), and the ServiceRequest id the
    resolver returns is the SAME order uuid the signed report's `basedOn` carries, so the sign-off
    join closes on one ServiceRequest/<order uuid>.

    An order that carries no priority resolves to the join ref alone; triage treats an absent
    priority as neutral, which is honest. What is NOT honest is dropping one that was there.
    """
    if accession:
        try:
            resolved = await _openmrs_rest().resolve_radiology_order_by_accession(accession)
        except Exception as exc:  # noqa: BLE001 - OpenMRS down/unreachable must not fail the webhook
            # The reason matters in the log: "connection refused" is an outage to wait out, but an
            # InsecureReadTransportError (#67) is a config error that will fail EVERY ingest until
            # someone fixes the base URL or sets the opt-in -- a reasonless warning hid that.
            # Redacted: httpx errors embed the request URL, and a base URL configured with
            # userinfo (http://user:pass@host/...) would put Basic credentials in this line on
            # EVERY DICOM arrival.
            _log.warning("order resolution failed for accession %s (%s: %s); starting "
                         "Patient/UNRESOLVED", accession, type(exc).__name__, _redacted(exc))
            resolved = None
        if resolved:
            order = {"fhirServiceRequestId": resolved["fhirServiceRequestId"]}
            # Copy by allow-list: `order` is additionalProperties:false, so an unexpected key from
            # a future resolver would fail StudyContext validation and drop the study.
            order.update({k: resolved[k] for k in _ORDER_SIGNALS if resolved.get(k)})
            return {"fhirPatientId": resolved["fhirPatientId"]}, order
    return {"fhirPatientId": "Patient/UNRESOLVED"}, {}


async def _study_description(orthanc_study_id: str) -> str:
    """The study's DICOM StudyDescription, read back from Orthanc (#62).

    This is what the interpretation tool registry selects on -- `select_tools(modality,
    studyDescription)` picks `ich-detect`/`stroke-detect` for a CT HEAD and falls through to
    `generic-ct-screen` without it -- and what triage's description keywords scan. The Orthanc
    stable event does not carry it, so ingress reads it from the source.

    Best-effort, like the fhir2 resolve above: Orthanc being unreachable costs the study its
    body-region tools, which is bad, but failing the webhook would cost it the whole workflow.
    """
    try:
        return await _orthanc().get_study_description(orthanc_study_id)
    except Exception:  # noqa: BLE001 - Orthanc down/unreachable must not fail the webhook
        _log.warning("Orthanc description lookup failed for study %s; starting without one "
                     "(the interpretation registry will fall back to its generic tool)",
                     orthanc_study_id)
        return ""


def _index_workflow(ctx: dict) -> None:
    """Record a study's join keys -> workflowId so its finalized report can find it later.

    Ingest resolves the order from the accession (#11), so when fhir2 answers we hold both the
    accession (from the Orthanc event) and the ServiceRequest ref. Index whatever is present.

    The ServiceRequest ref is the robust join. The accession is only the fallback for a study that
    resolved nothing (fhir2 down), and a DICOM accession is an order identifier not guaranteed
    unique per study, so on an accession collision we WARN rather than silently overwrite.
    """
    wf_id = ctx["workflowId"]
    accession = (ctx.get("study") or {}).get("accessionNumber")
    service_request = (ctx.get("order") or {}).get("fhirServiceRequestId")
    keys = [k for k in (accession, service_request) if k]
    if not keys:
        _log.warning("study %s has no join key; its finalized report cannot be matched", wf_id)
    store = _store()
    for key in keys:
        existing = store.workflow_id_for(key)
        if existing and existing != wf_id:
            _log.warning("join key %r re-points %s -> %s (accession not unique?)", key, existing, wf_id)
        store.put_index(key, wf_id)


def _workflow_id_for_report(report: dict) -> str | None:
    """Map a finalized report back to its workflow via the keys recorded at start. Prefer the
    ServiceRequest ref (the robust join from #11); fall back to the accession."""
    store = _store()
    for key in (report.get("serviceRequestRef"), report.get("accessionNumber")):
        if key:
            wf_id = store.workflow_id_for(key)
            if wf_id:
                return wf_id
    return None


def _dedup_key(report: dict) -> str:
    """Dedup identity for the boundary re-scan: report id PLUS its update stamp.

    An addendum is the SAME resource re-returned with a bumped _lastUpdated (#66) -- keyed on the
    bare id, a signed report's dedup entry permanently swallowed its own amendment on a quiet
    system (and the kept-filter in _advance_cursor even refreshed the entry, because the
    amendment's stamp sits at the new cursor), so report_addended never fired. The stamp makes
    each observed VERSION its own event while the same-stamp boundary re-scan still dedups.
    A string, not a tuple: the set round-trips through the store as JSON."""
    return f"{report.get('diagnosticReportId')}@{report.get('lastUpdatedCursor') or ''}"


async def _process_batch(client: Client, reports: list[dict], skip_ids: set[str]) -> tuple[set[str], list[dict]]:
    """Signal each mapped, not-yet-signalled report to its waiting workflow; return (dedup keys
    newly signalled, mapped reports whose signal FAILED). Report VERSIONS already signalled at the
    current cursor (`skip_ids`, keyed by _dedup_key) are deduped. A report with no known workflow
    is LOGGED and skipped.

    A failed report is returned rather than dropped so `_advance_cursor` can hold the cursor at
    it (#29): the inclusive ge-window then re-returns it next poll â€" a retry for free. The retry
    naturally stops when the workflow is truly gone: reconciliation evicts its index row and the
    report re-enters as unmapped. Failed attempts are tracked durably so that final unmapped
    re-entry is recognized as OURS and captured as a dead letter (#29) â€" a permanently dropped
    sign-off a human must see â€" instead of blending into the routine "never ours" fhir2 noise."""
    store = _store()
    signalled: set[str] = set()
    failed: list[dict] = []
    for report in reports:
        report_id = report.get("diagnosticReportId")
        if _dedup_key(report) in skip_ids:
            continue
        wf_id = _workflow_id_for_report(report)
        if not wf_id:
            was_failing = store.failed_signal_for(report_id) if report_id else None
            if was_failing:
                # The kind must tell the truth about WHAT was dropped (lead ruling, #66 audit) --
                # and the truth lives in the RECORDED failure history, not in whatever version
                # fhir2 serves now: a report amended DURING the outage re-enters as `amended`,
                # but if a report_finalized delivery ever failed, the thing still undelivered is
                # the SIGN-OFF (classifying that as an addendum would hide a lost signature --
                # the dangerous direction). Only a failure history that is addendum-only files as
                # the post-archive addendum a human must re-verify by hand.
                if was_failing.get("signal") == "addendum":
                    kind = KIND_POST_ARCHIVE_ADDENDUM
                    reason = ("addendum arrived after its workflow finished; the correction was "
                              "never re-verified")
                else:
                    kind = KIND_SIGNOFF_DROP
                    reason = "workflow evicted while its sign-off signal was still failing"
                store.add_dead_letter(
                    report_id, was_failing["workflowId"], was_failing["attempts"],
                    reason, now_iso(), kind=kind)
                store.clear_failed_signal(report_id)
                _log.error("DEAD LETTER (%s): report %s for workflow %s dropped after %d failed "
                           "attempt(s); see /admin/dead-letters", kind, report_id,
                           was_failing["workflowId"], was_failing["attempts"])
            elif report.get("status") in ("amended", "corrected"):
                # Unmapped addendum: no index row, no failure record -- either fhir2 noise for a
                # study never tracked, or a correction arriving long after its workflow's rows
                # were reconciled away (>~1h post-completion). The two are indistinguishable
                # here, so no dead letter is fabricated -- but the log line names the second
                # possibility so a grep can find it. Surfacing the long-tail case durably needs
                # a retention design (lead decision pending).
                _log.warning("amended/corrected report %s matched no waiting workflow (dropped; "
                             "possible post-archive addendum for an already-reconciled study)",
                             report_id)
            else:
                _log.warning("finalized report %s matched no waiting workflow (dropped)", report_id)
            continue
        # Route on status: a `final` report is the radiologist's sign-off (report_finalized, leaves
        # AWAITING_RADIOLOGIST); an `amended`/`corrected` report is an addendum to an already-signed
        # report (report_addended, re-verifies against the correction at the sign-off gate, #56 (a)
        # / #66). The poll (fhir_client._SIGNOFF_STATUSES) returns both; the record carries `status`.
        signal = (
            StudyWorkflow.report_addended
            if report.get("status") in ("amended", "corrected")
            else StudyWorkflow.report_finalized
        )
        try:
            await client.get_workflow_handle(wf_id).signal(signal, report)
            signalled.add(_dedup_key(report))  # mapping is reclaimed on completion by _reconcile_index
            store.clear_failed_signal(report_id)  # delivered: retire any failure record
        except Exception:  # noqa: BLE001 - workflow gone/unreachable
            failed.append(report)
            if report_id:  # a report with no id can't be tracked; the held cursor still retries it
                store.record_failed_signal(
                    report_id, wf_id, now_iso(),
                    signal="addendum" if signal is StudyWorkflow.report_addended else "final")
            _log.warning("failed to signal workflow %s for report %s; will retry next poll", wf_id, report_id)
    return signalled, failed


def _advance_cursor(cursor: str, high_water: str | None, reports: list[dict], signalled: set[str],
                    failed: list[dict] | None = None) -> tuple[str, set[str]]:
    """Advance to the high-water mark â€" but never past a mapped report whose signal failed (#29).

    ge{cursor} re-returns reports at the boundary second, so we remember which of those we already
    signalled and drop the rest (older ids fall out of the query window). If the high-water didn't
    move, hold the cursor and keep accumulating dedup ids at this boundary.

    Holding at the earliest failure keeps that report inside the ge-window so the next poll
    retries it; before, a later success in the same batch advanced the cursor past it and the
    sign-off was silently lost. While held back, the dedup set keeps EVERY already-signalled
    version key (_dedup_key, id@stamp) at-or-after the cursor (not just the boundary), so the
    wider re-scan does not re-signal -- while an ADDENDUM, the same id at a NEWER stamp, is a
    fresh key and still gets through (#66).
    (End-to-end delivery is still at-least-once â€" e.g. a crash after signal but before
    save_cursor replays the batch â€" which is safe because report_finalized is an idempotent
    overwrite; keep it that way.)
    """
    floor = None
    if failed:
        stamps = [r.get("lastUpdatedCursor") for r in failed]
        # A failure we cannot place in time pins the cursor entirely (rare: report with no meta).
        # Clamp at the current cursor: it advances or holds, never retreats.
        floor = cursor if None in stamps else max(min(stamps), cursor)
    target = high_water
    if floor is not None and (target is None or floor < target):
        target = floor
    if not target or target == cursor:
        return cursor, signalled
    kept = {
        _dedup_key(r) for r in reports
        if (r.get("lastUpdatedCursor") or "") >= target and _dedup_key(r) in signalled
    }
    return target, kept


async def _is_open(client: Client, wf_id: str) -> bool:
    """Whether a workflow is still running. Only an AFFIRMATIVE answer counts as closed: a
    NOT_FOUND (workflow truly gone) or a successful describe showing a non-RUNNING status.
    UNREACHABLE IS NOT CLOSED (#29): during a Temporal outage â€" the very condition that makes
    signals fail and the poller hold its cursor â€" a reconcile sweep would otherwise evict every
    index row, turning each held retry into a permanent sign-off loss and stranding every other
    in-flight study. On a transport error we assume open, keep the row, and let a later sweep
    decide."""
    try:
        desc = await client.get_workflow_handle(wf_id).describe()
    except RPCError as e:
        return e.status != RPCStatusCode.NOT_FOUND  # gone -> closed; anything else -> assume open
    except Exception:  # noqa: BLE001 - unexpected transport failure: same stance, assume open
        return True
    return desc.status == WorkflowExecutionStatus.RUNNING


async def _reconcile_index(client: Client) -> int:
    """Evict index rows whose workflow is no longer open, the durable and restart-safe form of
    'evict on workflow completion' (#6). Reconciliation is the only eviction path. A delivered
    report keeps its row while the workflow runs so an addendum can still route, and this sweep
    reclaims that row once the workflow closes. It also reclaims studies that never delivered a
    report (cancelled, QC-rejected, or terminated), so index growth stays bounded to the set of
    studies still awaiting sign-off. Runs at poller startup (the GC point after a restart) and
    periodically. Returns rows pruned."""
    store = _store()
    pruned = 0
    for wf_id in store.indexed_workflow_ids():
        if not await _is_open(client, wf_id):
            store.evict_workflow(wf_id)
            pruned += 1
    if pruned:
        _log.info("index reconcile: evicted %d row(s) for closed/absent workflows", pruned)
    return pruned


# Real-time nudge (#25): the RIS-side hook (OpenMRS module hook or an Atomfeed bridge) POSTs to
# /webhooks/ris/event and the poller sweeps NOW instead of waiting out the interval. The cursor
# sweep stays the single source of truth â€" a nudge carries no data and can be lost, duplicated,
# or fired spuriously without affecting correctness; interval polling remains the fallback.
# Lazily created so importing this module has no side effect; tests reset `_WAKE`.
_WAKE: asyncio.Event | None = None


def _wake_event() -> asyncio.Event:
    global _WAKE
    if _WAKE is None:
        _WAKE = asyncio.Event()
    return _WAKE


async def _sleep_or_nudge(seconds: float) -> bool:
    """Wait for the next sweep: a full interval, or sooner if the RIS event webhook nudges.
    Returns True when nudged. A nudge that lands DURING a sweep is not lost â€" the event stays
    set until consumed here, so bursts coalesce into exactly one immediate re-sweep."""
    wake = _wake_event()
    try:
        await asyncio.wait_for(wake.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        wake.clear()


async def _ris_poller() -> None:
    """Poll fhir2 for finalized reports and signal the matching workflows.

    Advances an inclusive high-water cursor and dedups by report id at the boundary, so no
    sign-off is dropped at a shared-second timestamp and none is signalled twice. A RIS event
    nudge (#25) only shortens the wait before a sweep; it never bypasses the cursor.
    """
    store = _store()
    cursor, signalled_at_cursor = store.load_cursor()
    if cursor is None:                                   # fresh start: begin at "now" and persist
        cursor = now_iso()                               # it, so a restart before the first poll
        store.save_cursor(cursor, signalled_at_cursor)   # resumes here, not a later "now" (no gap).
    try:
        await _reconcile_index(await _temporal())  # GC rows for workflows closed during downtime
    except Exception:  # noqa: BLE001 - reconciliation is best-effort; never block delivery
        _log.warning("index reconciliation failed at startup")
    polls = 0
    consecutive_failures = 0
    while True:
        await _sleep_or_nudge(POLL_INTERVAL_S)
        polls += 1
        if polls % RECONCILE_EVERY_POLLS == 0:
            try:
                await _reconcile_index(await _temporal())
            except Exception:  # noqa: BLE001
                _log.warning("periodic index reconciliation failed")
        try:
            reports, high_water = await activities.poll_finalized_reports(cursor)
        except NotImplementedError:
            continue  # fhir2 client not wired in this environment
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            # Swallowing keeps the loop alive, but a fhir2 that is down (or rejecting our
            # credentials, #53) must not be silent: every failed poll is a window in which a
            # radiologist sign-off goes undetected. Warn on the first failure, then throttle.
            # Type + redacted message, NOT exc_info: the formatted traceback ends with str(exc),
            # and an HTTPStatusError embeds the full request URL -- with userinfo credentials in
            # it if the deployment configured them that way (the 401/#53 case is exactly an
            # HTTPStatusError, so the leak would fire precisely when credentials are wrong).
            consecutive_failures += 1
            if consecutive_failures == 1 or consecutive_failures % FAILED_POLLS_PER_WARNING == 0:
                _log.warning("fhir2 poll failed (%d consecutive; %s: %s); sign-off detection is "
                             "stalled", consecutive_failures, type(exc).__name__, _redacted(exc))
            continue
        if consecutive_failures:
            _log.warning("fhir2 poll recovered after %d consecutive failure(s)", consecutive_failures)
            consecutive_failures = 0
        failed: list[dict] = []
        if reports:
            newly, failed = await _process_batch(await _temporal(), reports, signalled_at_cursor)
            signalled_at_cursor |= newly
        cursor, signalled_at_cursor = _advance_cursor(cursor, high_water, reports, signalled_at_cursor, failed)
        store.save_cursor(cursor, signalled_at_cursor)  # durable: a restart mid-wait resumes here


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast on an invalid FHIR2_BASIC_* pair (#53). Every live consumer constructs the
    # client inside a swallow-and-continue path, where the constructor's ValueError would
    # masquerade as an fhir2 outage forever â€" startup is the only place it can be loud.
    Fhir2Client()
    poller = asyncio.create_task(_ris_poller())
    yield
    poller.cancel()


app = FastAPI(title="LH-Radiology Orchestrator Ingress", lifespan=lifespan)

# OpenTelemetry (#28): span the webhook + poller; the Temporal client interceptor in _temporal()
# then propagates context to the workflow. Fully off unless OTel is configured; all imports lazy
# so the module (and its tests) never require the [otel] extra.
#
# Instrumented HERE, at import, and NOT inside the lifespan: instrument_app() injects ASGI
# middleware, and by the time the lifespan body runs Starlette has already built and cached its
# middleware stack â€" the call would be silently ignored and the ingress would export no spans at
# all (no error, no warning). The HTTPX instrumentation is process-global, so it rides along.
if tracing_enabled():
    init_tracing("orchestrator-ingress")
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()  # fhir2 poller calls inject the traceparent


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/webhooks/ris/event", status_code=202)
async def ris_event() -> dict:
    """RIS-side change hook (#25): nudge the poller to sweep immediately.

    Deliberately takes NO payload: the fhir2 cursor sweep remains the single source of truth,
    so this endpoint is PHI-free, idempotent, and safe for the RIS side to fire on any event
    (or never â€" interval polling is the unchanged fallback)."""
    _wake_event().set()
    return {"nudged": True}


@app.post("/events/ohif-opened", status_code=202)
async def ohif_opened_event(body: dict) -> dict:
    """OHIF extension reports a study opened by the radiologist (#73 item 1).

    Best-effort observability sink for the ohif.study.opened event. Producer is
    integrations/ohif-extension/src/api/eventClient.ts, wired on the WorkList row click.
    Consumer TODAY is the ingress log stream â€" the acceptance criterion for #73 item 1
    is "recorded by the ingress (visible in logs/store)" and log capture satisfies that
    for the demo. A future pre-read-assist consumer could grow durable store persistence
    at that time; not gilding it here means no new schema, no new migration risk, and
    zero coupling between "extension can post" and "downstream reader exists".

    Wire shape: contracts/events/ohif-opened.schema.json.

    Behavior:
      * Validates the payload against the schema. Failure -> 400. Producer treats any
        non-2xx as `console.warn` and never blocks navigation, so this is diagnostic only.
      * Returns 202 on success. Producer is fire-and-forget; body is not read.

    Deliberately does NOT signal a workflow: this endpoint would otherwise couple the
    viewer's open-click to the orchestrator's Temporal runtime for no current benefit.
    The extension's own doc calls this "publish is visibility, not correctness";
    keeping the endpoint side-effect-thin honors that.
    """
    try:
        validate_against(body, paths.contracts_dir() / "events" / "ohif-opened.schema.json")
    except Exception as exc:  # ContractError from validate_against
        raise HTTPException(status_code=400, detail=f"schema: {exc}")
    _log = logging.getLogger("orchestrator.ingress.ohif")
    _log.info(
        "ohif.study.opened accepted study=%s radiologist=%s at=%s",
        body["studyInstanceUID"],
        body.get("radiologistId") or "-",
        body["openedAt"],
    )
    return {"accepted": True, "studyInstanceUID": body["studyInstanceUID"]}


# --- Sign-off override: the confirm page and the release endpoint (#57) ------------------------
#
# The escalation payload has always carried an `overrideUrl` pointing at this path, but the URL
# answered only to a hand-built POST: the one deliberately-human step in the pipeline was a curl.
# The GET below renders what is about to be waived and a form whose button POSTs to the same
# path -- the ack surface's GET/POST split, for the same reason: once the URL is something people
# open, a bare GET that released the gate would be reachable by a link prefetch.


def _signoff_page(title: str, body_html: str, status: int = 200) -> HTMLResponse:
    """Minimal self-contained page, the ack-surface style. `body_html` is trusted, pre-escaped
    markup assembled by the builders below -- everything dynamic in it goes through
    `html_mod.escape` at the call site."""
    return HTMLResponse(
        f"<!doctype html><html><head><meta name=\"viewport\" "
        f"content=\"width=device-width, initial-scale=1\">"
        f"<title>{html_mod.escape(title)}</title></head>"
        f"<body style=\"font-family: sans-serif; max-width: 36em; margin: 3em auto;\">"
        f"<h1 style=\"font-size:1.25em\">{html_mod.escape(title)}</h1>{body_html}</body></html>",
        status_code=status,
    )


def _verification_lines(sc: dict | None) -> str:
    """The verdict being waived, as safe markup. Rule IDs and severities ONLY: this renders on a
    page served before any token is presented, and issue `message`/`location` may quote report
    text (lean-reference discipline) -- so they are never read, even if present."""
    if not sc or not sc.get("verificationStatus"):
        return ""
    status = html_mod.escape(str(sc["verificationStatus"]))
    review = " (requires human review)" if sc.get("requiresHumanReview") else ""
    rules = ", ".join(
        f"{html_mod.escape(str(i.get('ruleId') or 'unknown-rule'))}"
        f" [{html_mod.escape(str(i.get('severity') or '?'))}]"
        for i in (sc.get("issues") or [])
    )
    out = f"<p>Verification: <strong>{status}</strong>{review}.</p>"
    if rules:
        out += f"<p>Held on: {rules}.</p>"
    return out


def _override_form(workflow_id: str, sc: dict | None, notice: str = "") -> HTMLResponse:
    wf = html_mod.escape(workflow_id)
    body = f"<p>Workflow <code>{wf}</code>.</p>"
    if notice:
        body += f"<p><em>{html_mod.escape(notice)}</em></p>"
    body += _verification_lines(sc)
    body += (
        "<p>Releasing does <strong>not</strong> re-verify the report: the study proceeds to "
        "communication carrying both the verification verdict and this acknowledgement, and "
        "your name and reason become part of the workflow's audit record.</p>"
        f"<form method=\"post\" action=\"/signoff/{wf}/override\">"
        "<p><label>Acknowledged by<br>"
        "<input name=\"acknowledgedBy\" required style=\"width:100%\" "
        "placeholder=\"your name or Practitioner reference\"></label></p>"
        "<p><label>Reason<br>"
        f"<textarea name=\"reason\" required rows=\"3\" maxlength=\"{SIGNOFF_REASON_MAX}\" "
        "style=\"width:100%\"></textarea></label></p>"
        "<p><label>Sign-off token<br>"
        "<input type=\"password\" name=\"token\" required style=\"width:100%\"></label></p>"
        "<button type=\"submit\" style=\"font-size:1.1em; padding:0.6em 1.2em;\">"
        "Release the gate</button></form>"
    )
    return _signoff_page("Release the sign-off gate", body)


@app.get("/signoff/{workflow_id}/override")
async def signoff_override_confirm(workflow_id: str) -> HTMLResponse:
    """Renders the confirmation page. Deliberately changes NO state (see the section comment).

    Best-effort context: `signoff_context` (state + the verdict being waived) with a fallback to
    `current_state` for a worker predating that query, and a fallback to the bare form when the
    workflow cannot be queried at all -- an on-call clinician following a paged overrideUrl must
    land on a working page even when Temporal is having a moment; the POST re-checks everything
    that matters."""
    sc: dict | None = None
    state: str | None = None
    try:
        client = await _temporal()
        handle = client.get_workflow_handle(workflow_id)
        try:
            sc = await handle.query(StudyWorkflow.signoff_context)
            state = (sc or {}).get("state")
        except Exception:  # noqa: BLE001 -- deployed worker may predate the query
            state = await handle.query(StudyWorkflow.current_state)
    except Exception as e:  # noqa: BLE001 -- unknown workflow / Temporal down
        _log.warning("override page: could not query %s: %s", workflow_id, _redacted(e))
        return _override_form(
            workflow_id, None,
            notice="The workflow could not be queried right now; if the gate is not actually "
                   "held, submitting will tell you.")

    if state is not None and state != "AWAITING_SIGNOFF":
        return _signoff_page(
            "Nothing to release",
            f"<p>Workflow <code>{html_mod.escape(workflow_id)}</code> is at "
            f"<strong>{html_mod.escape(state)}</strong>, not AWAITING_SIGNOFF -- "
            f"the sign-off gate is not holding this study.</p>")
    return _override_form(workflow_id, sc)


@app.post("/signoff/{workflow_id}/override", status_code=202)
async def signoff_override_route(
    workflow_id: str,
    request: Request,
    x_signoff_token: str = Header(default=""),
) -> Response:
    """Content-type seam in front of `signoff_override`. JSON callers keep the API contract
    exactly as it was (202 + the JSON receipt; errors as JSON `detail`). The confirm page's
    form-encoded submit carries the token as its `token` field -- a browser form cannot set a
    header -- and gets an HTML receipt or an HTML error page back, with the same status codes."""
    ct = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in ct:
        # Parsed with the stdlib, not request.form(): starlette delegates ALL form parsing --
        # urlencoded included -- to the optional python-multipart package, and a three-field
        # form is not worth a new dependency in every image that runs the ingress.
        raw = (await request.body()).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        body = {"acknowledgedBy": form.get("acknowledgedBy"), "reason": form.get("reason")}
        token = x_signoff_token or form.get("token") or ""
        try:
            out = await signoff_override(workflow_id, body, x_signoff_token=token)
        except HTTPException as e:
            return _signoff_page(
                "The gate is still held",
                f"<p>{html_mod.escape(str(e.detail))}</p>"
                f"<p>Go back and try again.</p>",
                status=e.status_code)
        return _signoff_page(
            "Sign-off override -- study released",
            f"<p>Workflow <code>{html_mod.escape(out['workflowId'])}</code>.</p>"
            f"<p>Recorded as acknowledged by "
            f"<strong>{html_mod.escape(out['acknowledgedBy'])}</strong> at "
            f"{html_mod.escape(out['acknowledgedAt'])}.</p>"
            f"<p>The study proceeds to communication carrying both the verification verdict "
            f"and this acknowledgement.</p>")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    return JSONResponse(
        await signoff_override(workflow_id, body, x_signoff_token=x_signoff_token),
        status_code=202)


async def signoff_override(
    workflow_id: str,
    body: dict,
    x_signoff_token: str = "",
) -> dict:
    """A radiologist acknowledges a verification finding and releases the sign-off gate (#57).

    THE MISSING PRODUCER. `signoff_acknowledged` is the signal AWAITING_SIGNOFF waits on, and until
    this endpoint existed nothing in production sent it -- it lived only in tests. A study whose
    report FAILed verification with requiresHumanReview therefore paged its way up the ladder and
    then waited forever: it never reached COMMUNICATE, so the very finding that made verification
    FAIL was never dispatched, and it never archived (#56).

    The study does NOT get re-verified. Re-running report.verify on the unchanged report re-derives
    the same FAIL and drops it straight back into the gate. It proceeds to COMMUNICATE carrying the
    FAIL *and* this acknowledgement, both on the record (#56 decision (b)). At M2 the addendum flow
    (#56 (a)) becomes the main path -- an addendum updates the report, so re-verification is finally
    meaningful -- and this endpoint remains the escape hatch.

    AUTHENTICATION IS MANDATORY, unlike the Orthanc webhook and the A2A callback, whose tokens are
    optional for the compose posture. Those endpoints deliver facts; this one lets a caller waive a
    safety verdict on a radiology report. With SIGNOFF_OVERRIDE_TOKEN unset the endpoint refuses to
    act at all rather than silently accepting anonymous releases -- an override nobody can be
    identified with is not an override, it is a hole.

    A plain coroutine, not a route: `signoff_override_route` parses the transport (JSON or the
    confirm page's form) and lands here, and the handler-level tests keep driving it directly.
    """
    if not SIGNOFF_OVERRIDE_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="sign-off override is not configured (set SIGNOFF_OVERRIDE_TOKEN)",
        )
    # Compare as BYTES. hmac.compare_digest on str raises TypeError the moment either side holds a
    # non-ASCII character -- so a junk header with one accented byte would crash the auth check into
    # an unhandled 500 instead of a clean 401, and a deployment whose token happened to be non-ASCII
    # would 500 on every call, i.e. brick the escape hatch.
    if not hmac.compare_digest(x_signoff_token.encode("utf-8"),
                               SIGNOFF_OVERRIDE_TOKEN.encode("utf-8")):
        raise HTTPException(status_code=401, detail="bad sign-off override token")

    # Who and why are the whole point: this is the audit record of a human waiving a safety verdict.
    # An override with an anonymous author, or with no stated reason, is not auditable -- reject it
    # rather than record a blank.
    who = str(body.get("acknowledgedBy") or "").strip()
    why = str(body.get("reason") or "").strip()
    if not who or not why:
        raise HTTPException(
            status_code=422,
            detail="acknowledgedBy and reason are both required: an override must say who and why",
        )
    if len(why) > SIGNOFF_REASON_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"reason must be at most {SIGNOFF_REASON_MAX} characters",
        )

    ack = {"acknowledgedBy": who, "reason": why, "acknowledgedAt": now_iso()}
    try:
        client = await _temporal()
        await client.get_workflow_handle(workflow_id).signal(
            StudyWorkflow.signoff_acknowledged, ack)
    except Exception as e:  # noqa: BLE001 - unknown workflow / Temporal down -> tell the caller
        _log.warning("sign-off override for %s could not be signalled: %s", workflow_id, e)
        raise HTTPException(
            status_code=502,
            detail="could not signal the workflow; the gate is still held",
        )

    # `reason` is free text a clinician typed, so it is NOT echoed into the log (lean-reference: it
    # is the one field here that could carry PHI). It lives in the workflow history, which is where
    # the audit trail belongs.
    _log.warning("SIGN-OFF OVERRIDE: %s released by %s", workflow_id, who)
    return {"acknowledged": True, "workflowId": workflow_id, "acknowledgedBy": who,
            "acknowledgedAt": ack["acknowledgedAt"]}


@app.get("/admin/dead-letters")
async def dead_letters() -> dict:
    """Everything the pipeline permanently gave up on. Rows carry IDs only (lean-reference, no
    PHI). Empty is the healthy state; anything here needs a human. Read `kind`:

    * `signoff-drop` (#29) â€" the RIS delivered a finalized report, it mapped to a workflow, every
      signal attempt failed, and the workflow closed before one landed. Reconcile the study in the
      RIS against the orchestrator's history.
    * `escalation-policy-load-failure` (#54) â€" a sign-off gate could not load its escalation
      ladder, so it fell back to a single flat page. The study is still escalated and readable, but
      escalation is DEGRADED until the policy is fixed: check escalation-policy.yaml and any
      ESCALATION_POLICY_PATH override.
    * `signoff-abandoned` (#57) â€" a sign-off gate paged its whole escalation ladder and NOBODY
      acknowledged. The study was released to COMMUNICATE (so the finding that made verification
      FAIL still got dispatched) and archived, but its report carries a verification FAIL that no
      human ever signed off. Go and look at the study. This is the loudest row on this surface.
    * `post-archive-addendum` (#66) â€" a correction (amended/corrected report) arrived for a
      workflow that had already finished, and its delivery failed until the rows were reclaimed.
      The pipeline never re-verified the corrected body: read the amended report in the RIS and
      run the correction through review by hand.
    """
    letters = _store().dead_letters()
    return {"count": len(letters), "deadLetters": letters}


# Artifact parts buffered per push task until its terminal event arrives (#24): the SDK sender
# POSTs the result (artifactUpdate) and the terminal state (statusUpdate) as SEPARATE callbacks.
# In-memory on purpose: entries are popped at the terminal event. If the artifact half is lost
# (one un-retried POST, or an ingress restart between the two), the surviving terminal COMPLETED
# has no result to relay and is reported to the workflow as a failure â€" the workflow re-runs the
# skill (bounded; see StudyWorkflow._call_push). Size-capped oldest-first because a task whose
# terminal never arrives would otherwise leak its parts forever â€" and because with the token
# unset (dev posture) this endpoint is unauthenticated, an uncapped dict is a free memory-DoS.
# Tests reset this dict.
_PUSH_PARTS: dict[str, list] = {}
_PUSH_PARTS_CAP = int(os.environ.get("A2A_PUSH_BUFFER_CAP", "1024"))


@app.post("/callbacks/a2a/{workflow_id}", status_code=202)
async def a2a_push_callback(
    workflow_id: str,
    body: dict,
    skill: str = "",
    x_a2a_notification_token: str = Header(default=""),
) -> dict:
    """A2A push-notification receiver (#24): an agent reports progress on a push-mode skill.

    Non-terminal events are acknowledged and ignored; the artifact event's data parts are
    buffered; the terminal event is relayed to the workflow as a `skill_completed` signal keyed
    by taskId. The workflowId and skillId ride in the callback URL â€" minted by
    start_agent_skill_activity â€" so no task->workflow index is needed. A well-behaved agent
    validated its output before emitting it, but this endpoint may be reachable by others (the
    token is optional), so a delivered result is re-validated against the skill's contract here
    and relayed as a failure if it doesn't conform. A relay the workflow never receives (dropped
    POST, Temporal briefly down) is recovered by the workflow re-running the skill after its
    wait times out."""
    if A2A_CALLBACK_TOKEN and x_a2a_notification_token != A2A_CALLBACK_TOKEN:
        raise HTTPException(status_code=401, detail="bad callback token")
    try:
        parsed = parse_push_callback(body)
    except Exception as e:  # noqa: BLE001 - not a StreamResponse -> reject, don't 500
        raise HTTPException(status_code=422, detail=f"unparseable push callback: {e}")
    if parsed is None:
        return {"ignored": "non-terminal event"}
    task_id = parsed["taskId"]
    if parsed["kind"] == "artifact":
        if task_id not in _PUSH_PARTS:
            while len(_PUSH_PARTS) >= _PUSH_PARTS_CAP:  # evict oldest orphan (insertion order)
                _PUSH_PARTS.pop(next(iter(_PUSH_PARTS)))
        _PUSH_PARTS.setdefault(task_id, []).extend(parsed["parts"])
        return {"buffered": task_id}

    parts = parsed["parts"] or _PUSH_PARTS.pop(task_id, [])
    _PUSH_PARTS.pop(task_id, None)  # failure path cleanup: don't leak buffered parts
    completed = parsed["state"] == "TASK_STATE_COMPLETED" and bool(parts)
    if completed and skill:
        try:
            validate_skill_output(skill, parts[0])
        except Exception:  # noqa: BLE001 - non-conforming/forged result (log IDs only, no values)
            _log.warning("push result for wf=%s task=%s failed the %s output contract; "
                         "relaying as failure", workflow_id, task_id, skill)
            completed = False
    event = ({"taskId": task_id, "result": parts[0]} if completed
             else {"taskId": task_id, "failed": True})
    try:
        client = await _temporal()
    except Exception:  # noqa: BLE001 - Temporal briefly down: tell the sender, don't 500
        _log.warning("push callback for %s task %s: temporal unavailable", workflow_id, task_id)
        raise HTTPException(status_code=503, detail="temporal unavailable")
    try:
        await client.get_workflow_handle(workflow_id).signal(
            StudyWorkflow.skill_completed, event)
    except Exception:  # noqa: BLE001 - workflow gone: the workflow-side wait timeout re-runs
        _log.warning("push callback for %s task %s could not be signalled", workflow_id, task_id)
        raise HTTPException(status_code=404, detail="workflow not found")
    return {"relayed": task_id}


@app.post("/webhooks/orthanc")
async def orthanc_webhook(event: dict) -> dict:
    try:
        validate_against(event, paths.contracts_dir() / "events" / "orthanc-stable.schema.json")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))

    ctx = await _build_study_context(event)
    # (Re)index the join keys so this study's finalized report can find it. On a re-fire this also
    # REPAIRS a first pass that resolved nothing: _build_study_context just re-ran fhir2 resolution,
    # so if fhir2 is back the ServiceRequest ref is now present and gets indexed here (#11).
    _index_workflow(ctx)
    client = await _temporal()
    try:
        await client.start_workflow(
            StudyWorkflow.run,
            ctx,
            id=ctx["workflowId"],
            task_queue=TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        # Orthanc re-fires OnStableStudy for the same study (a late instance reopens it, so it goes
        # stable again). The workflow id is deterministic (wf_<orthancStudyId>), so a duplicate event
        # is normal PACS behaviour, not an error: return 200 with the existing id so the plugin and
        # its retries (#47) see success instead of a 500. The re-index above is the #11 "second
        # chance" -- an UNRESOLVED first pass gets its ServiceRequest join repaired here (index-side)
        # with no restart; the already-running workflow keeps its original ctx.
        _log.info("duplicate stable-study event for %s; re-indexed, workflow already running", ctx["workflowId"])
        return {"started": ctx["workflowId"], "duplicate": True}
    return {"started": ctx["workflowId"]}
