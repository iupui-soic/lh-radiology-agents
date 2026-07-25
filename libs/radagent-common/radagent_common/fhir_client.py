"""Thin OpenMRS fhir2 (FHIR R4) client. Mostly READ-ONLY (see architecture notes: Risk R1);
`write_presign_impression` (#26) is the one write path. Verified live against the docker o3
build: DiagnosticReport create/update works only when `code` resolves to a real Concept (a
text-only code 500s with codeRequired), and the idempotency lookup searches by `subject`
because `based-on` and `status` both 400 on this fhir2.

Methods are stubs for M0; wire to the live fhir2 base URL in M1. Every agent that needs
clinical data uses THIS client (lean-reference: fetch from source, do not pass PHI in messages).
"""
from __future__ import annotations
from typing import Any, Optional
import logging
import os
from urllib.parse import urlparse
import httpx

from .ack_link import ack_secret, sign_ack_task
from .fhir_models import DiagnosticReport, ServiceRequest


def _typed_ref(resource_type: str, id_or_ref: str) -> str:
    """Accept a bare id ('abc') or an already-qualified reference ('DiagnosticReport/abc')."""
    return id_or_ref if "/" in id_or_ref else f"{resource_type}/{id_or_ref}"


def _basic_auth_from_env() -> Optional[tuple[str, str]]:
    """(user, pass) for live fhir2, or None to stay unauthenticated (mocks, unit tests).

    Live fhir2 401s every unauthenticated read — and callers deliberately swallow fhir2 errors
    to protect ingestion, so a missing credential shows up as silence, not a crash (#53). A
    half-set pair is therefore rejected loudly here rather than silently downgraded. The values
    themselves must never be logged.
    """
    user = os.environ.get("FHIR2_BASIC_USER")
    password = os.environ.get("FHIR2_BASIC_PASS")
    if bool(user) != bool(password):
        raise ValueError("FHIR2_BASIC_USER and FHIR2_BASIC_PASS must be set together")
    return (user, password) if user else None


# OpenMRS fhir2 requires a DiagnosticReport.code that resolves to a real Concept: a text-only code
# (or an unmapped LOINC) maps to code=null and the create 500s (codeRequired). This is the concept
# the pre-sign draft (#26) is coded with, overridable per deployment. The default is the LH-Radiology
# "AI pre-sign impression draft" concept -- a dedicated authorship stamp we provision in every
# deployment via docker/openmrs/bootstrap_presign_concept.py. See docs/presign-concept.md for the
# rationale (why NOT CIEL "Provisional diagnosis": authorship-collision risk with a RIS drafting its
# own preliminary reports on the same concept, and honesty about what the resource is -- coding an
# AI draft as "Provisional diagnosis" implies the AI made a diagnosis). A deployment that provisions
# the concept at a different UUID sets FHIR2_PRESIGN_REPORT_CONCEPT to that UUID; a deployment using
# our bootstrap script leaves the default alone.
_DEFAULT_PRESIGN_REPORT_CONCEPT = "e3641471-3f25-57b4-ab27-a3ebc66e481e"


def _presign_report_concept() -> str:
    return os.environ.get("FHIR2_PRESIGN_REPORT_CONCEPT", _DEFAULT_PRESIGN_REPORT_CONCEPT)


# The critical-result notification concept (#79): the Observation.code authorship stamp on the
# in-EHR notification the comms agent's ehr-inbox channel writes. Same lifecycle as the pre-sign
# concept above -- provisioned at stack startup by docker/openmrs/bootstrap_presign_concept.py,
# derived uuid5(uuid5(NAMESPACE_DNS, "librehealth.org"),
# "lh-radiology.ai-critical-result-notification.v1"), drift-guarded by
# tests/test_presign_concept_drift.py. Datatype Text, because the obs carries a valueString and
# fhir2 refuses an obs whose value does not match its concept's datatype. A deployment that
# provisions it elsewhere sets FHIR2_CRITICAL_NOTIFICATION_CONCEPT.
_DEFAULT_CRITICAL_NOTIFICATION_CONCEPT = "ea215431-5e85-5040-adf0-1da297c154c3"


def _critical_notification_concept() -> str:
    return os.environ.get(
        "FHIR2_CRITICAL_NOTIFICATION_CONCEPT", _DEFAULT_CRITICAL_NOTIFICATION_CONCEPT)


def ehr_inbox_write_enabled() -> bool:
    """#79 master switch for the in-EHR critical-result notification write. Default OFF: the
    ehr-inbox channel keeps its stubbed v1 semantics until the PI write-path sign-off recorded on
    #79 flips this in the deployment. Truthy set matches the other write gates byte-for-byte
    (FHIR2_ALLOW_INSECURE_WRITE / ORTHANC_PRESIGN_WRITE_ENABLED) -- two switches with different
    token sets is an operator trap (!73 review, item 3).
    """
    return os.environ.get("EHR_INBOX_WRITE_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def _ack_task_marker(ack_task_id: str) -> str:
    """The valueString segment naming the LIVE ack loop (comms.checkAck tracks this Task)."""
    return f"ack task {ack_task_id}"


def _notification_anchor(accession: str, ack_task_id: str) -> str:
    """The idempotency anchor for the chart notification: one entry per critical result.

    The ACCESSION, not the ack-task id, because only the accession is stable across a Temporal
    retry of the dispatch: each retry re-mints the ledger Communication AND ack Task (new id), so
    a task-id anchor could never match the previous attempt's entry and every retry would add a
    chart entry. Anchored on the accession, a retry UPDATES the entry in place and it always
    names the newest -- the live -- ack loop. Fallback for a study with no accession is the exact
    ack-task segment (retry dedup is then lost, correlation is not).

    Matching is by EXACT " | "-delimited segment, never substring: the comms ledger (HAPI JPA)
    assigns sequential numeric Task ids, so "ack task 5" IS a substring of "ack task 52" and a
    substring match would let one critical result's dispatch overwrite ANOTHER's chart entry
    (found by adversarial review before first merge; pinned in the tests)."""
    if accession:
        return f"accession {accession}"
    return _ack_task_marker(ack_task_id)


_log = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_plaintext_remote(base_url: str) -> bool:
    """Plaintext `http` to a non-loopback host: the transport that exposes anything sent on it.
    The shared predicate behind BOTH transport guards -- writes (_write_transport_is_secure, #30)
    and reads (_read_transport_is_secure, #67) -- so the two policies cannot drift."""
    parsed = urlparse(base_url)
    return parsed.scheme != "https" and (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS


def _write_transport_is_secure(base_url: str) -> bool:
    """Is it safe to send a fhir2 WRITE to this base URL (#30)?

    A DiagnosticReport write carries PHI -- the pre-sign impression text -- and rides HTTP Basic
    credentials in every request. Over plaintext `http` to a remote host, both are exposed on the
    wire to anyone on the path. We therefore refuse a plaintext write UNLESS the target is loopback
    (local dev / unit tests) or the deployment has explicitly accepted the risk on a trusted
    internal network via FHIR2_ALLOW_INSECURE_WRITE. `https` is always fine.

    This guards the WRITE only. Reads use the same base URL and the same HTTP Basic credentials;
    they are gated by their own guard, _read_transport_is_secure below (#67), which shares this
    function's predicate and inherits this opt-in when no read-specific one is set.
    """
    if not _is_plaintext_remote(base_url):
        return True  # https, or a loopback host
    return os.environ.get("FHIR2_ALLOW_INSECURE_WRITE", "").strip().lower() in {"1", "true", "yes"}


def _read_transport_is_secure(url: str) -> bool:
    """Is it safe to send a fhir2 READ to this URL (#67, the read side of #30's F2)?

    Every read method returns PHI -- demographics, conditions, allergies, medications, labs, and
    the radiologist's narrative (get_report_conclusion) -- and rides the SAME HTTP Basic
    credentials as the write, so plaintext-to-remote exposes exactly what the write guard refuses
    to expose. The RIS poller reads every 30s, so a write-inert deployment is exposed
    CONTINUOUSLY; securing the write did nothing for it. Same predicate as the write guard
    (`_is_plaintext_remote`) so the two policies cannot drift.

    Opt-in: FHIR2_ALLOW_INSECURE_READ, DEFAULTING to FHIR2_ALLOW_INSECURE_WRITE when unset. The
    trust statement is about the TRANSPORT, not the verb -- a deployment that already accepted
    cleartext on this hop for writes (the compose stack does, explicitly) has accepted it for the
    reads that ride the same wire, and keeps working unchanged. Setting FHIR2_ALLOW_INSECURE_READ
    explicitly (e.g. "0") overrides that inheritance in either direction.
    """
    if not _is_plaintext_remote(url):
        return True  # https, or a loopback host
    opt_in = os.environ.get("FHIR2_ALLOW_INSECURE_READ")
    if opt_in is None:
        opt_in = os.environ.get("FHIR2_ALLOW_INSECURE_WRITE", "")
    return opt_in.strip().lower() in {"1", "true", "yes"}


class InsecureWriteTransportError(RuntimeError):
    """A fhir2 write refused because the transport is plaintext to a non-loopback host and the
    insecure opt-in is not set (#30).

    Subclass of RuntimeError so existing `except RuntimeError` paths still catch it. It is NAMED so
    the orchestrator can mark it non-retryable BY TYPE without string-matching the message:
    temporalio converts a raised exception to an ApplicationError whose `type` is
    `exception.__class__.__name__`, so `non_retryable_error_types=["InsecureWriteTransportError"]` at
    the activity call site keys off this name -- and the shared lib still never imports temporalio. A
    misconfigured transport is a config error, not a transient fault; retrying it only burns the
    bounded retry budget before the same skip.
    """


def _guard_write_transport(base_url: str) -> None:
    if not _write_transport_is_secure(base_url):
        raise InsecureWriteTransportError(
            "refusing a fhir2 write over plaintext HTTP to a non-loopback host: the pre-sign "
            "impression (PHI) and the HTTP Basic credentials would travel in cleartext. Use an "
            "https base URL, or set FHIR2_ALLOW_INSECURE_WRITE=1 for a trusted internal network."
        )
    # The write is proceeding. If it is only allowed because of the insecure opt-in (the guard
    # passed but the transport is still plaintext-to-remote), leave an audit trail that PHI plus
    # credentials went out in cleartext on this hop. Host only -- never the impression text.
    if _is_plaintext_remote(base_url):
        _log.warning(
            "fhir2 write proceeding over PLAINTEXT http to %s under FHIR2_ALLOW_INSECURE_WRITE: the "
            "pre-sign impression (PHI) and HTTP Basic credentials are in cleartext on this hop",
            urlparse(base_url).hostname,
        )


class InsecureReadTransportError(RuntimeError):
    """A fhir2 read refused because the transport is plaintext to a non-loopback host and the
    insecure opt-in is not set (#67). Same shape and rationale as InsecureWriteTransportError:
    a RuntimeError subclass, NAMED so an activity call site can mark it non-retryable by type
    (`non_retryable_error_types=["InsecureReadTransportError"]`) -- a misconfigured transport is a
    config error, not a transient fault, and the RIS poller must not burn its retry budget (or its
    30s cadence) re-asking a question with a config-shaped answer."""


# Hosts already warned about cleartext reads this process. The write path warns on EVERY write
# (writes are rare and each is an auditable event); reads happen every 30s from the poller alone,
# and a per-read warning is log spam that trains operators to ignore the message that matters.
_INSECURE_READ_WARNED: set[str] = set()


def _guard_read_transport(url: str) -> None:
    if not _read_transport_is_secure(url):
        # Host named (never the full URL: it could carry userinfo) so the operator fixes the
        # RIGHT base URL -- this guard also fronts the OpenMRS REST surface (#70), whose URL may
        # come from OPENMRS_REST_BASE_URL rather than FHIR2_BASE_URL.
        raise InsecureReadTransportError(
            f"refusing a PHI read over plaintext HTTP to non-loopback host "
            f"{(urlparse(url).hostname or '')!r}: the response is PHI and the HTTP Basic "
            "credentials travel with the request, both in cleartext. Use an https base URL "
            "(FHIR2_BASE_URL, or OPENMRS_REST_BASE_URL for the REST surface), or set "
            "FHIR2_ALLOW_INSECURE_READ=1 (or the existing FHIR2_ALLOW_INSECURE_WRITE=1, which "
            "reads inherit) for a trusted internal network."
        )
    if _is_plaintext_remote(url):
        host = (urlparse(url).hostname or "").lower()
        if host not in _INSECURE_READ_WARNED:
            _INSECURE_READ_WARNED.add(host)
            _log.warning(
                "fhir2 reads proceeding over PLAINTEXT http to %s under the insecure opt-in: PHI "
                "and HTTP Basic credentials are in cleartext on this hop (warned once per process)",
                host,
            )


class Fhir2Client:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 15.0):
        self.base_url = (base_url or os.environ.get("FHIR2_BASE_URL", "http://openmrs:8080/openmrs/ws/fhir2/R4")).rstrip("/")
        self._timeout = timeout
        self._auth = _basic_auth_from_env()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        # `path` may be a relative resource ("DiagnosticReport") or an absolute Bundle next-page URL.
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        # Guard the URL actually fetched, not just base_url: a Bundle `next` link is absolute and
        # server-authored, and PHI must not follow it onto a plaintext remote hop either (#67).
        _guard_read_transport(url)
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, resource: dict) -> dict:
        _guard_write_transport(self.base_url)  # no PHI/credentials over plaintext to a remote host (#30)
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.post(f"{self.base_url}/{path.lstrip('/')}", json=resource)
            r.raise_for_status()
            return r.json()

    async def _put(self, path: str, resource: dict) -> dict:
        _guard_write_transport(self.base_url)  # no PHI/credentials over plaintext to a remote host (#30)
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.put(f"{self.base_url}/{path.lstrip('/')}", json=resource)
            r.raise_for_status()
            return r.json()

    # --- read helpers used by EHR Assistant / orchestrator ------------------

    async def get_patient(self, fhir_patient_id: str) -> Optional[dict]:
        """GET Patient/{id} — accepts either the bare id ('demo-1') or the reference
        form ('Patient/demo-1'). Returns None if the patient is not found (404).

        Kept for other agents' use; the EHR Assistant itself does not consume patient
        demographics (its output surfaces refs + codes, not name / DOB — lean-reference)."""
        if not fhir_patient_id:
            return None
        ref = fhir_patient_id if "/" in fhir_patient_id else f"Patient/{fhir_patient_id}"
        try:
            return await self._get(ref)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def search_imaging_studies(self, fhir_patient_id: str) -> list[dict]:
        """GET ImagingStudy?patient=... — prior imaging for context.

        Returns a lean list ({ref, modality, date}) matching the priorStudies items in
        `contracts/skills/ehr.schema.json`. `reportRef` is deliberately NOT populated;
        linking each ImagingStudy to its DiagnosticReport needs a `_revinclude` spike
        against the live OpenMRS fhir2 (M2). Follows Bundle `next` links.
        """
        return [_lean_imaging_study(r) for r in await self._collect(
            "ImagingStudy", {"patient": _patient_query(fhir_patient_id)})]

    async def search_observations(self, fhir_patient_id: str, codes: list[str]) -> list[dict]:
        """GET Observation?patient=...&code=<loinc-csv> — latest observations for the
        requested LOINCs. Codes are joined with `,` per FHIR search syntax so a single
        request covers the whole panel (creatinine + every eGFR LOINC variant we care
        about for contrast decisions).

        Returns lean records ({code, display, value, unit, date}) matching the
        relevantLabs items in `contracts/skills/ehr.schema.json`. Empty `codes` -> [].
        """
        if not codes:
            return []
        return [_lean_observation(r) for r in await self._collect(
            "Observation",
            {"patient": _patient_query(fhir_patient_id), "code": ",".join(codes)})]

    async def search_conditions(self, fhir_patient_id: str) -> list[dict]:
        """GET Condition?patient=...&clinical-status=active — problem list.

        Returns lean records ({code, display}) matching the activeProblems items in
        the schema. Client-side filters out non-active clinical statuses in case the
        server ignores the `clinical-status` search parameter (OpenMRS fhir2 has
        historically had gaps — see `poll_finalized_reports` for the status/400 case)."""
        entries = await self._collect(
            "Condition",
            {"patient": _patient_query(fhir_patient_id), "clinical-status": "active"})
        return [_lean_condition(r) for r in entries if _condition_is_active(r)]

    async def search_allergies(self, fhir_patient_id: str) -> list[dict]:
        """GET AllergyIntolerance?patient=... — allergies + criticality.

        Returns lean records ({code, criticality}) matching the allergies items in
        the schema. `code` is the primary coding value (RxNorm / SNOMED / other) — the
        first coded system found is used, freeing downstream from parsing FHIR
        CodeableConcept structure."""
        return [_lean_allergy(r) for r in await self._collect(
            "AllergyIntolerance", {"patient": _patient_query(fhir_patient_id)})]

    async def search_medications(self, fhir_patient_id: str) -> list[dict]:
        """GET MedicationRequest?patient=... — current medications.

        Returns lean records ({code, display}) — one per active med — used by the
        EHR Assistant to derive medicationFlags (onMetformin / onAnticoagulant / etc.)
        via RxNorm code match with a case-insensitive text fallback. The `status` search
        param is deliberately NOT sent: live fhir2 returns 500 on
        `MedicationRequest?status=...` (a server NPE, verified against the o3 fhir2 build),
        which the caller would swallow into an empty slice, silently disabling every med
        flag. Activeness is filtered client-side via `_medication_is_active` instead — the
        same status-param-avoidance `resolve_order_by_accession` uses."""
        entries = await self._collect(
            "MedicationRequest",
            {"patient": _patient_query(fhir_patient_id)})
        return [_lean_medication(r) for r in entries if _medication_is_active(r)]

    async def _collect(self, path: str, params: dict[str, Any]) -> list[dict]:
        """Follow Bundle `next` links and yield every resource entry — the common
        paging idiom shared by every search_* method above. Matches the paging
        behavior of `poll_finalized_reports` so all searches degrade the same way
        under an OpenMRS with paged responses."""
        collected: list[dict] = []
        target: Optional[str] = path
        current_params: dict[str, Any] | None = params
        while target:
            try:
                bundle = await self._get(target, current_params)
            except httpx.HTTPStatusError as e:
                # A resource type the fhir2 build does not expose (e.g. ImagingStudy on the
                # o3 image) answers 404. Treat "not found / unsupported" as no results rather
                # than raising, so one unavailable slice degrades to [] the way get_patient's
                # 404 degrades to None — instead of failing the whole search.
                if e.response.status_code == 404:
                    return collected
                raise
            for entry in bundle.get("entry", []) or []:
                resource = entry.get("resource") or {}
                if resource:
                    collected.append(resource)
            target, current_params = _bundle_next_link(bundle), None  # next link is absolute
        return collected

    async def resolve_order_by_accession(self, accession: str) -> Optional[dict]:
        """Resolve a DICOM accession to its patient + order refs and triage signals (#11, #61).

        Searches ServiceRequest by its accession identifier and returns what the ingress needs to
        build the StudyContext `patient` and `order` blocks:
            {"fhirPatientId": "Patient/<id>", "fhirServiceRequestId": "ServiceRequest/<id>",
             "priority": "stat", "reasonCode": ["J93.1"]}
        `priority` and `reasonCode` are omitted when the order carries none. Returns None when
        nothing matches. Read-only (a search GET).

        The two optional keys are how the order's urgency reaches triage (#61). They are the only
        clinical content in the envelope, and they are there because `triage.score` scores on them
        and cannot do its job without them -- lean-reference means the minimum a downstream agent
        needs, not nothing at all. `studycontext.schema.json` has always declared both. No name, no
        narrative, no free text (see `_order_reason_codes`).

        NOTE: matches the accession as a bare FHIR `identifier` value (any system). If the live
        fhir2 needs the ACSN system pinned (`identifier=<system>|<value>`), narrow it here once the
        deployed OpenMRS is confirmed.
        """
        if not accession:
            return None
        bundle = await self._get("ServiceRequest", {"identifier": accession})
        for entry in bundle.get("entry", []) or []:
            resource = entry.get("resource") or {}
            if resource.get("resourceType") != "ServiceRequest":
                continue
            patient_ref = (resource.get("subject") or {}).get("reference")
            sr_id = resource.get("id")
            if patient_ref and sr_id:
                resolved = {"fhirPatientId": patient_ref,
                            "fhirServiceRequestId": f"ServiceRequest/{sr_id}"}
                priority = _order_priority(resource)
                if priority:
                    resolved["priority"] = priority
                reason_codes = _order_reason_codes(resource)
                if reason_codes:
                    resolved["reasonCode"] = reason_codes
                return resolved
        return None

    async def get_report_conclusion(self, diagnostic_report_id: str) -> Optional[str]:
        """Fetch a finalized report's narrative conclusion by id (issue #16).

        The `ris.report.finalized` event is lean (IDs + refs only, no narrative -- Golden rule 2),
        so Impression Generation reads the report CONTENT from source: GET DiagnosticReport/<id>
        and return its `conclusion` (the radiologist's summary the impression structures from).
        Returns None when the id is empty, the report is missing, or it carries no conclusion.
        Read-only. The conclusion is the one clinical field the impression is entitled to consume.
        """
        if not diagnostic_report_id:
            return None
        ref = diagnostic_report_id if "/" in diagnostic_report_id else f"DiagnosticReport/{diagnostic_report_id}"
        resource = await self._get(ref)
        conclusion = resource.get("conclusion")
        return conclusion if isinstance(conclusion, str) and conclusion.strip() else None

    # --- typed clinical reads for the Communications Agent (#52) ---------------------
    # CritCom decides WHO to call and HOW LOUDLY from the report and its order, so it needs the
    # whole resource, not just the conclusion: the ACR-category extension, presentedForm, and the
    # order's priority/requester. Both are READ-ONLY -- fhir2 stays a source of clinical context
    # (the notification and its ack are written to the comms ledger; see comms_ledger.py).

    async def get_diagnostic_report(self, diagnostic_report_id: str) -> Optional[DiagnosticReport]:
        """GET DiagnosticReport/{id} as a typed model. None if the id is empty or it is missing.

        Distinct from `get_report_conclusion`, which returns just the narrative for Impression
        Generation's keyword scan (#16). The Communications Agent needs the whole report.
        """
        if not diagnostic_report_id:
            return None
        ref = _typed_ref("DiagnosticReport", diagnostic_report_id)
        try:
            return DiagnosticReport.model_validate(await self._get(ref))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def get_service_request(self, service_request_id: str) -> Optional[ServiceRequest]:
        """GET ServiceRequest/{id} as a typed model -- the order behind a report. None if missing.

        Read by id ONLY. `ServiceRequest?identifier=` (the DICOM accession) is NOT supported by the
        deployed fhir2 -- it 400s, and the field is absent from the resource entirely -- so the
        accession join stays where it is (#11), and this is the by-reference read that works.
        """
        if not service_request_id:
            return None
        ref = _typed_ref("ServiceRequest", service_request_id)
        try:
            return ServiceRequest.model_validate(await self._get(ref))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def write_presign_impression(
        self, service_request_ref: str, patient_ref: str, impression_text: str,
    ) -> str:
        """Offer the pre-sign draft impression into the RIS as a `preliminary` DiagnosticReport
        (issue #26) -- advisory only, never transitioned to `final`; the radiologist's own signed
        report is a separate `final` DiagnosticReport the RIS creates on sign-off.

        `code` references a real OpenMRS Concept by UUID (see `_presign_report_concept`): live
        fhir2 rejects a text-only code with 500 codeRequired, so the human label rides in
        `code.text` while `code.coding` drives the concept resolution.

        Idempotent per order: a pre-sign re-run (e.g. more aiFindings tools complete) updates the
        SAME draft instead of accumulating duplicates, found via `_find_presign_draft`.

        Returns the written DiagnosticReport's bare id.
        """
        # Guard the transport UP FRONT, before the idempotency lookup -- not only inside _post/_put.
        # `_find_presign_draft` issues a credentialed GET; on the exact deployment this guard refuses
        # (plaintext to a remote host, no opt-in) that GET's `Authorization: Basic` header would go out
        # before the write is ever refused. Raising here means a refused write leaks nothing. The guard
        # stays in _post/_put as a backstop for any other write path.
        _guard_write_transport(self.base_url)
        existing_id = await self._find_presign_draft(service_request_ref, patient_ref)
        resource = {
            "resourceType": "DiagnosticReport",
            "status": "preliminary",
            "code": {
                "coding": [{"code": _presign_report_concept()}],
                "text": "AI pre-sign impression draft",
            },
            "subject": {"reference": patient_ref},
            "basedOn": [{"reference": service_request_ref}],
            "conclusion": impression_text,
        }
        if existing_id:
            resource["id"] = existing_id
            written = await self._put(f"DiagnosticReport/{existing_id}", resource)
        else:
            written = await self._post("DiagnosticReport", resource)
        return written["id"]

    async def _find_presign_draft(self, service_request_ref: str, patient_ref: str) -> Optional[str]:
        """OUR OWN pre-sign draft for this order, if we already wrote one -- the idempotency key an
        update reuses instead of duplicating. None means "nothing of ours here", and the caller then
        POSTs a new report rather than overwriting whatever it found.

        AUTHORSHIP IS THE POINT (#26). `preliminary` is also the status a RIS gives a radiologist's
        OWN unsigned draft (it flips to `final` on sign-off), so matching on status alone cannot
        tell our draft from theirs -- and `write_presign_impression` PUTs the full resource over the
        match, which would replace a human's text with the AI's. The discriminator is therefore the
        `code` concept the draft is stamped with on create (`_presign_report_concept`) AND the
        order: a report is ours only if it is preliminary, based on this order, AND carries our
        concept.

        The stamp is the CODE, not an `identifier`: this fhir2 build silently DROPS
        `DiagnosticReport.identifier` on write, so an identifier stamp would vanish on the way in
        and every lookup would miss -- accumulating a new draft per re-run.

        Searches by `subject` (the patient), then filters client-side. Neither `based-on` nor
        `status` can be a search param -- both 400 on live fhir2 ("does not know how to handle GET
        operation[DiagnosticReport] with parameters") -- even though `basedOn` DOES round-trip on
        the resource body.
        """
        ours = _presign_report_concept()
        bundle = await self._get("DiagnosticReport", {"subject": patient_ref})
        for entry in bundle.get("entry", []) or []:
            resource = entry.get("resource") or {}
            if resource.get("resourceType") != "DiagnosticReport":
                continue
            if resource.get("status") != "preliminary":
                continue
            based_on = [ref.get("reference") for ref in (resource.get("basedOn") or [])]
            if service_request_ref not in based_on:
                continue
            codes = [c.get("code") for c in ((resource.get("code") or {}).get("coding") or [])]
            if ours not in codes:
                # Someone else's preliminary report on this order -- most likely the radiologist's
                # own draft. Leave it alone.
                continue
            return resource.get("id")
        return None

    async def write_critical_result_notification(
        self,
        *,
        patient_ref: str,
        finding: str,
        accession: str,
        ack_task_id: str,
        sent_iso: str,
    ) -> Optional[str]:
        """Deliver a critical-result notification INTO the chart (#79): an Observation on the
        patient, stamped with the dedicated notification concept, its valueString carrying the
        finding label + accession + ack-task correlation. Never the report narrative -- the chart
        gets a pointer, the ledger Communication stays the record of what was communicated.
        Returns the Observation id, or None WITHOUT any I/O when EHR_INBOX_WRITE_ENABLED is off
        (the default) -- the flag-off short-circuit-before-IO shape the Orthanc SC write (!73)
        established.

        The #26-class write conditions, and where each is enforced:
          * best-effort/advisory -- the CALLER (tools.deliver_critical_result_to_chart) maps an
            exception to a FAILED channel result and never re-raises past the dispatch: by then
            the ledger Communication and ack Task exist, so failing the dispatch activity would
            make Temporal retry it and page the same human twice;
          * authorship-stamped -- the dedicated concept is the discriminator, and the idempotent
            re-run (`_find_notification_obs`) updates ONLY an obs carrying our concept AND this
            critical result's exact anchor segment (see `_notification_anchor`), so it can never
            overwrite clinician-authored data or another result's entry;
          * gated inert -- default-off flag, PI sign-off recorded on #79 before any deployment
            flips it.

        ONE chart entry per critical result: anchored on the accession, a dispatch retry (which
        re-mints the ack loop) updates the entry in place so it names the live ack Task instead
        of accumulating. Raises ValueError when accession AND ack_task_id are both empty -- an
        uncorrelatable notification is not written, and the caller reports the channel FAILED.
        """
        if not ehr_inbox_write_enabled():
            return None
        if not accession and not ack_task_id:
            raise ValueError(
                "refusing an uncorrelatable chart notification: no accession and no ack task id")
        # Guard up front, before the idempotency GET, so a refused write leaks no credentialed
        # request (same hoist rationale as write_presign_impression).
        _guard_write_transport(self.base_url)
        patient = _typed_ref("Patient", patient_ref)
        anchor = _notification_anchor(accession, ack_task_id)
        segments = [finding]
        if accession:
            segments.append(f"accession {accession}")
        if ack_task_id:
            segments.append(_ack_task_marker(ack_task_id))
        link_base = os.environ.get("CRITCOM_ACK_BASE_URL", "").rstrip("/")
        if link_base and ack_task_id and ack_secret():
            # The ack surface (the worklist-api /ack route): the link carries an HMAC so a forged
            # or enumerated task id never reaches the acknowledge flow; WHO acknowledged is
            # separately the endpoint's job (radagent_common.ack_link has the split rationale).
            # Base URL AND secret must both be configured, or no link is emitted -- an unsigned
            # link is never minted.
            segments.append(
                f"ack link: {link_base}/ack/{ack_task_id}?sig={sign_ack_task(ack_task_id)}")
        value = " | ".join(segments)
        # NO `basedOn` here, deliberately: live fhir2 4.1.0 500s on ANY Observation write carrying
        # it (HAPI-0389 NullPointerException in the translator; bisected live 2026-07-19 -- the
        # identical resource minus basedOn is a 201). The order correlation rides in valueString as
        # the accession (the pipeline's join key), and the ledger Communication keeps the typed
        # ServiceRequest reference.
        resource = {
            "resourceType": "Observation",
            "status": "final",
            "code": {
                "coding": [{"code": _critical_notification_concept()}],
                "text": "AI critical result notification",
            },
            "subject": {"reference": patient},
            "effectiveDateTime": sent_iso,
            "valueString": value,
        }
        existing_id = await self._find_notification_obs(patient, anchor)
        if existing_id:
            resource["id"] = existing_id
            written = await self._put(f"Observation/{existing_id}", resource)
        else:
            written = await self._post("Observation", resource)
        return written["id"]

    async def _find_notification_obs(self, patient_ref: str, anchor: str) -> Optional[str]:
        """OUR notification for this critical result, if an earlier dispatch attempt already
        wrote one -- the idempotency key an update reuses instead of duplicating. Same authorship
        logic as `_find_presign_draft`: an obs is ours only if it carries BOTH our concept in
        code.coding AND the anchor as an EXACT " | "-delimited valueString segment. Anything else
        on the patient -- labs, clinician-entered obs, another critical result's notification --
        is left alone. Exact-segment, never substring: HAPI's sequential Task ids make
        "ack task 5" a substring of "ack task 52" (see `_notification_anchor`).

        Unlike the presign finder, subject-only search is NOT enough here: a real patient carries
        a labs-heavy obs list (134 on the dev-stack probe patient) and fhir2 pages at 10, so ours
        need not be on page 1. `code=<concept uuid>` narrows server-side -- verified to work on
        live fhir2 4.1.0 (2026-07-19: subject+code returned exactly the stamped obs) -- and the
        Bundle `next` links are followed anyway so a miss can never come from paging. The
        client-side re-check stays authoritative for authorship."""
        ours = _critical_notification_concept()
        target: Optional[str] = "Observation"
        params: dict[str, Any] | None = {"subject": patient_ref, "code": ours}
        while target:
            bundle = await self._get(target, params)
            for entry in bundle.get("entry", []) or []:
                resource = entry.get("resource") or {}
                if resource.get("resourceType") != "Observation":
                    continue
                codes = [c.get("code") for c in ((resource.get("code") or {}).get("coding") or [])]
                if ours not in codes:
                    continue
                if anchor not in (resource.get("valueString") or "").split(" | "):
                    continue
                return resource.get("id")
            target, params = _bundle_next_link(bundle), None  # next link is an absolute URL
        return None

    async def poll_finalized_reports(self, since_iso: str) -> tuple[list[dict], Optional[str]]:
        """RIS sign-off detection. Returns (sign-off records oldest-first, high-water cursor).

        Covers the FINAL report (the radiologist's sign-off) AND any later ADDENDUM -- an
        amended/corrected DiagnosticReport (#56 (a) / #66). Each record carries `status`, and the
        poller routes on it: `final` -> report_finalized, `amended`/`corrected` -> report_addended.
        An addendum re-enters this poll naturally: amending a report bumps its `_lastUpdated`, so it
        reappears past the cursor as a fresh hit under the same id.

        `status` is NOT a searchable param on the live fhir2 (OpenMRS 5.7.9) —
        `DiagnosticReport?status=final` returns 400 (verified in the #3 spike) — so we page by
        `_lastUpdated` and filter status client-side.

        Correctness of the cursor (issue #12 acceptance):
          * query `ge` (INCLUSIVE) + dedup by id in the poller, so a report sharing the boundary
            second is never lost to strict-greater (OpenMRS timestamps are second-precision);
          * follow every Bundle `next` link, so nothing is missed past page 1;
          * high-water = max `meta.lastUpdated` across ALL entries seen (any status, computed by
            max not by trusting `_sort`), so the poller advances past non-signed reports too.
        Records are lean + PHI-free (IDs + refs + cursor).
        """
        reports: list[dict] = []
        high_water: Optional[str] = None
        target: Optional[str] = "DiagnosticReport"
        params: dict[str, Any] | None = {"_lastUpdated": f"ge{since_iso}", "_sort": "_lastUpdated"}
        while target:
            bundle = await self._get(target, params)
            for entry in bundle.get("entry", []) or []:
                resource = entry.get("resource") or {}
                if resource.get("resourceType") != "DiagnosticReport":
                    continue
                updated = (resource.get("meta") or {}).get("lastUpdated")
                if updated and (high_water is None or updated > high_water):
                    high_water = updated
                if resource.get("status") in _SIGNOFF_STATUSES:
                    reports.append(finalized_report_record(resource))
            target, params = _bundle_next_link(bundle), None  # next link is an absolute URL
        return reports, high_water


def _bundle_next_link(bundle: dict) -> Optional[str]:
    """The absolute URL of the Bundle's `next` page, if the server paged the result."""
    for link in bundle.get("link", []) or []:
        if isinstance(link, dict) and link.get("relation") == "next":
            return link.get("url")
    return None


# FHIR ServiceRequest.priority and StudyContext order.priority are the same four values -- but the
# envelope pins them as a schema ENUM, so anything else fhir2 might answer with has to become "no
# priority" rather than a value that would fail StudyContext validation and kill the ingest.
_ORDER_PRIORITIES = frozenset({"stat", "urgent", "asap", "routine"})


def _order_priority(resource: dict) -> Optional[str]:
    """ServiceRequest.priority -> `order.priority`, or None if absent/unrecognised (#61)."""
    priority = str(resource.get("priority") or "").strip().lower()
    return priority if priority in _ORDER_PRIORITIES else None


def _order_reason_codes(resource: dict) -> list[str]:
    """ServiceRequest.reasonCode (CodeableConcept[]) -> the bare code strings the envelope carries
    (#61). Deduped, order preserved.

    Takes every coding whatever its system: `order.reasonCode` is system-agnostic by schema, triage
    matches ICD-10 prefixes and ignores what it doesn't recognise, so filtering by system here would
    only risk dropping the codes a live OpenMRS actually sends.

    A concept's `text` is deliberately NOT read. A code is a code; free text on a referral reason is
    where a clinician's narrative -- and PHI -- ends up, and it has no business on the wire.
    """
    codes: list[str] = []
    for concept in resource.get("reasonCode") or []:
        for coding in (concept or {}).get("coding") or []:
            code = (coding or {}).get("code")
            if isinstance(code, str) and code and code not in codes:
                codes.append(code)
    return codes


# DiagnosticReport statuses the RIS poller treats as a sign-off event. `final` is the radiologist's
# original sign-off (-> report_finalized); `amended`/`corrected` are addenda to an already-signed
# report (-> report_addended, #56 (a) / #66). `preliminary` is deliberately excluded -- it is the
# pre-sign AI draft (#26), not a human sign-off.
_SIGNOFF_STATUSES = frozenset({"final", "amended", "corrected"})


def finalized_report_record(report: dict) -> dict:
    """Project a FHIR DiagnosticReport to the lean, PHI-free record the RIS poller signals:
    IDs + join refs + the `_lastUpdated` cursor. No patient name or clinical content."""
    meta = report.get("meta") or {}
    return {
        "diagnosticReportId": f"DiagnosticReport/{report.get('id')}",
        "status": report.get("status"),
        "serviceRequestRef": _based_on_service_request(report),
        "accessionNumber": _accession_number(report),
        "signedAt": report.get("issued"),
        "lastUpdatedCursor": meta.get("lastUpdated"),
    }


def _based_on_service_request(report: dict) -> Optional[str]:
    """The order the report was based on (the robust join #11 resolves at ingest)."""
    for based_on in report.get("basedOn", []) or []:
        reference = based_on.get("reference", "") if isinstance(based_on, dict) else ""
        if "ServiceRequest/" in reference:
            return reference
    return None


def _accession_number(report: dict) -> Optional[str]:
    """Accession usually rides as a FHIR identifier of type ACSN (the join we have at ingest)."""
    for ident in report.get("identifier", []) or []:
        if not isinstance(ident, dict):
            continue
        codings = ((ident.get("type") or {}).get("coding")) or []
        if any(isinstance(c, dict) and c.get("code") == "ACSN" for c in codings):
            return ident.get("value")
    return None


# --- lean projections for EHR context assembly (issue #4) ----------------
# Each helper takes a raw FHIR resource (as returned by fhir2) and returns the
# decision-relevant slice that matches the corresponding items schema in
# `contracts/skills/ehr.schema.json`. NO raw record dumps — refs, codes, values
# only (lean-reference: PHI minimization).


def _patient_query(fhir_patient_id: str) -> str:
    """FHIR search accepts either a bare id or a reference; normalize to the bare id
    since some OpenMRS builds reject the reference form on `patient=` search params."""
    return fhir_patient_id.split("/", 1)[1] if "/" in fhir_patient_id else fhir_patient_id


def _first_coding_value(codeable_concept: dict) -> tuple[Optional[str], Optional[str]]:
    """Pull the first (code, display) pair from a FHIR CodeableConcept. Callers use
    this to avoid parsing FHIR CodeableConcept structure in every lean projector."""
    for coding in (codeable_concept or {}).get("coding", []) or []:
        if isinstance(coding, dict) and coding.get("code"):
            return coding["code"], coding.get("display")
    return None, (codeable_concept or {}).get("text")


def _lean_imaging_study(resource: dict) -> dict:
    """ImagingStudy -> {ref, modality, date}. `modality` on ImagingStudy is a list of
    Coding under `modality[]`; we take the first code. `date` is `started` (per FHIR R4)."""
    modality_codings = resource.get("modality") or []
    modality = (modality_codings[0].get("code") if (modality_codings
                and isinstance(modality_codings[0], dict)) else None) or ""
    out: dict = {"ref": f"ImagingStudy/{resource.get('id')}"}
    if modality:
        out["modality"] = modality
    started = resource.get("started")
    if started:
        out["date"] = started
    return out


_LOINC_SYSTEM = "http://loinc.org"


def _lean_observation(resource: dict) -> dict:
    """Observation -> {code, display, value?, unit?, date?}. Handles `valueQuantity` and
    `valueString` — the two commonest lab shapes. Missing pieces are simply omitted so
    schema `required: ["code"]` is met and optionals only appear when known.

    Prefers a LOINC-system coding when the concept carries one: live fhir2 lists the local
    concept uuid first and the mapped LOINC second, and downstream matching (the EHR
    assistant's eGFR panel) compares against LOINC — the first-coding pick handed it the
    uuid instead (M4 arc-4 dry run)."""
    cc = resource.get("code") or {}
    code = display = None
    for coding in cc.get("coding", []) or []:
        if isinstance(coding, dict) and coding.get("system") == _LOINC_SYSTEM and coding.get("code"):
            code, display = coding["code"], coding.get("display") or cc.get("text")
            break
    if not code:
        code, display = _first_coding_value(cc)
    out: dict = {"code": code or ""}
    if display:
        out["display"] = display
    if "valueQuantity" in resource:
        vq = resource["valueQuantity"] or {}
        if "value" in vq:
            out["value"] = vq["value"]
        if vq.get("unit"):
            out["unit"] = vq["unit"]
    elif "valueString" in resource:
        out["value"] = resource["valueString"]
    date = resource.get("effectiveDateTime") or resource.get("issued")
    if date:
        out["date"] = date
    return out


def _condition_is_active(resource: dict) -> bool:
    """Client-side re-filter: keep only Conditions whose clinicalStatus is 'active'.
    The server search may or may not honor `clinical-status=active` (OpenMRS fhir2 has
    documented gaps — see the #3 spike). Conditions without any clinicalStatus at all
    are treated as active (defensive: better to surface than to hide)."""
    clinical_status = resource.get("clinicalStatus") or {}
    codings = clinical_status.get("coding") or []
    if not codings:
        return True
    return any(isinstance(c, dict) and c.get("code") == "active" for c in codings)


def _lean_condition(resource: dict) -> dict:
    """Condition -> {code, display}. First coding on the `code` CodeableConcept."""
    code, display = _first_coding_value(resource.get("code") or {})
    out: dict = {"code": code or ""}
    if display:
        out["display"] = display
    return out


def _lean_allergy(resource: dict) -> dict:
    """AllergyIntolerance -> {code, criticality}. `criticality` is a native FHIR field
    (low | high | unable-to-assess); omitted if absent."""
    code, _ = _first_coding_value(resource.get("code") or {})
    out: dict = {"code": code or ""}
    criticality = resource.get("criticality")
    if criticality:
        out["criticality"] = criticality
    return out


def _medication_is_active(resource: dict) -> bool:
    return (resource.get("status") or "").lower() == "active"


def _lean_medication(resource: dict) -> dict:
    """MedicationRequest -> {code, display} projected from `medicationCodeableConcept`
    (the inline-code form). A `medicationReference` still needs no follow-up GET for its
    inline `display` — live fhir2 serves the demo cohort's drug orders exactly that way
    ("Heparin Sodium" etc.), and dropping it silently blanked every medicationFlag in the
    M4 arc-4 dry run; the flags' text fallback matches on this display."""
    med_cc = resource.get("medicationCodeableConcept") or {}
    code, display = _first_coding_value(med_cc)
    if not display:
        display = (resource.get("medicationReference") or {}).get("display")
    out: dict = {"code": code or ""}
    if display:
        out["display"] = display
    return out
