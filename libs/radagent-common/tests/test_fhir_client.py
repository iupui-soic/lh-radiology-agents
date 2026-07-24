"""Unit tests for Fhir2Client.poll_finalized_reports (issue #12).

Mocks the fhir2 HTTP layer (no live server) and checks: the query pages by INCLUSIVE `_lastUpdated`
(NOT the `status` param, which 400s on live fhir2), the sign-off statuses (final/amended/corrected,
#66) are filtered client-side, Bundle `next` pages are followed, and the high-water cursor is the
max lastUpdated across ALL entries. Each report is projected to a lean, PHI-free record.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from unittest import mock

import pytest
import yaml

from radagent_common.fhir_client import (
    Fhir2Client,
    finalized_report_record,
    _write_transport_is_secure,
    _guard_write_transport,
    _guard_read_transport,
    _read_transport_is_secure,
    InsecureReadTransportError,
    InsecureWriteTransportError,
)

# A loopback base URL the write-transport guard (#30) always permits. The pre-sign write LOGIC tests
# below exercise idempotency and authorship, not transport, so they run over loopback rather than the
# default plaintext-remote openmrs URL, which the hoisted guard now refuses without the opt-in. Using
# loopback -- not FHIR2_ALLOW_INSECURE_WRITE -- keeps them independent of that env var, which the
# transport tests mutate.
_LOOPBACK_BASE = "http://localhost:8080/openmrs/ws/fhir2/R4"

_FINAL = {
    "resourceType": "DiagnosticReport", "id": "rep-1", "status": "final",
    "basedOn": [{"reference": "ServiceRequest/sr-1"}],
    "identifier": [{"type": {"coding": [{"code": "ACSN"}]}, "value": "ACC-CXR-001"}],
    "issued": "2026-06-27T12:30:00Z",
    "meta": {"lastUpdated": "2026-06-27T12:30:05Z"},
}
_PRELIM = {
    "resourceType": "DiagnosticReport", "id": "rep-2", "status": "preliminary",
    "meta": {"lastUpdated": "2026-06-27T12:31:00Z"},  # newest overall, but NOT final
}
_FINAL2 = {
    "resourceType": "DiagnosticReport", "id": "rep-3", "status": "final",
    "meta": {"lastUpdated": "2026-06-27T12:40:00Z"},
}


def _bundle(*resources, next_url=None):
    b = {"resourceType": "Bundle", "type": "searchset",
         "entry": [{"resource": r} for r in resources]}
    if next_url:
        b["link"] = [{"relation": "next", "url": next_url}]
    return b


def test_poll_uses_inclusive_ge_filters_final_and_reports_high_water():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle(_FINAL, _PRELIM)

    client._get = fake_get  # type: ignore[assignment]
    reports, high_water = asyncio.run(client.poll_finalized_reports("2026-06-27T00:00:00Z"))

    # Inclusive ge (NOT gt, NOT status=final) so a same-second report is never lost.
    assert calls[0] == ("DiagnosticReport",
                        {"_lastUpdated": "ge2026-06-27T00:00:00Z", "_sort": "_lastUpdated"})
    # Only the finalized report survives the client-side status filter.
    assert [r["diagnosticReportId"] for r in reports] == ["DiagnosticReport/rep-1"]
    assert reports[0]["serviceRequestRef"] == "ServiceRequest/sr-1"
    assert reports[0]["accessionNumber"] == "ACC-CXR-001"
    # High-water = max lastUpdated across ALL entries (incl. the non-final one) so the poller
    # advances past non-final reports instead of stalling.
    assert high_water == "2026-06-27T12:31:00Z"
    assert set(reports[0]) == {"diagnosticReportId", "status", "serviceRequestRef",
                              "accessionNumber", "signedAt", "lastUpdatedCursor"}


def test_poll_returns_addenda_amended_and_corrected_but_not_preliminary():
    """RIS sign-off detection covers the FINAL sign-off AND later ADDENDA (#56 (a) / #66): an
    amended or corrected DiagnosticReport is returned too, each carrying its status so the poller
    routes it to report_addended. The pre-sign AI draft (`preliminary`, #26) stays excluded -- it is
    not a human sign-off. High-water still advances across ALL entries, addenda included."""
    client = Fhir2Client()
    amended = {"resourceType": "DiagnosticReport", "id": "rep-amd", "status": "amended",
               "meta": {"lastUpdated": "2026-06-27T14:00:00Z"}}
    corrected = {"resourceType": "DiagnosticReport", "id": "rep-cor", "status": "corrected",
                 "meta": {"lastUpdated": "2026-06-27T15:00:00Z"}}

    async def fake_get(path, params=None):
        return _bundle(_FINAL, amended, _PRELIM, corrected)

    client._get = fake_get  # type: ignore[assignment]
    reports, high_water = asyncio.run(client.poll_finalized_reports("2026-06-27T00:00:00Z"))

    got = {r["diagnosticReportId"]: r["status"] for r in reports}
    assert got == {
        "DiagnosticReport/rep-1": "final",
        "DiagnosticReport/rep-amd": "amended",
        "DiagnosticReport/rep-cor": "corrected",
    }, "addenda (amended/corrected) must be polled alongside final; preliminary must stay excluded"
    assert high_water == "2026-06-27T15:00:00Z"


def test_poll_follows_bundle_next_link():
    client = Fhir2Client()
    page1 = _bundle(_PRELIM, next_url="http://fhir/DiagnosticReport?page=2")  # page 1: no final
    page2 = _bundle(_FINAL2)
    responses = [page1, page2]
    paths = []

    async def fake_get(path, params=None):
        paths.append(path)
        return responses.pop(0)

    client._get = fake_get  # type: ignore[assignment]
    reports, high_water = asyncio.run(client.poll_finalized_reports("2026-06-27T00:00:00Z"))

    # The final report on page 2 is collected — not dropped by reading only page 1.
    assert [r["diagnosticReportId"] for r in reports] == ["DiagnosticReport/rep-3"]
    # Page 2 was fetched by its absolute next URL.
    assert paths == ["DiagnosticReport", "http://fhir/DiagnosticReport?page=2"]
    assert high_water == "2026-06-27T12:40:00Z"


def test_poll_empty_bundle():
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return {"resourceType": "Bundle", "type": "searchset"}

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.poll_finalized_reports("2026-06-27T00:00:00Z")) == ([], None)


def test_finalized_record_tolerates_missing_refs():
    rec = finalized_report_record({"id": "x", "status": "final"})
    assert rec["diagnosticReportId"] == "DiagnosticReport/x"
    assert rec["serviceRequestRef"] is None
    assert rec["accessionNumber"] is None
    assert rec["lastUpdatedCursor"] is None


# --- resolve_order_by_accession (issue #11) --------------------------------
_SR = {
    "resourceType": "ServiceRequest", "id": "sr-9",
    "subject": {"reference": "Patient/pat-9"},
    "identifier": [{"type": {"coding": [{"code": "ACSN"}]}, "value": "ACC-9"}],
}


def test_resolve_by_accession_returns_patient_and_order_refs():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle(_SR)

    client._get = fake_get  # type: ignore[assignment]
    resolved = asyncio.run(client.resolve_order_by_accession("ACC-9"))
    # Searches ServiceRequest by its identifier (the accession) -- not by status, which 400s.
    assert calls[0] == ("ServiceRequest", {"identifier": "ACC-9"})
    assert resolved == {"fhirPatientId": "Patient/pat-9",
                        "fhirServiceRequestId": "ServiceRequest/sr-9"}


def test_resolve_by_accession_none_on_miss():
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return {"resourceType": "Bundle", "type": "searchset"}

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.resolve_order_by_accession("NOPE")) is None


def test_resolve_by_accession_skips_serviceRequest_without_subject():
    client = Fhir2Client()
    partial = {"resourceType": "ServiceRequest", "id": "sr-x"}  # no subject -> not resolvable

    async def fake_get(path, params=None):
        return _bundle(partial)

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.resolve_order_by_accession("ACC-X")) is None


def test_resolve_by_accession_empty_accession_makes_no_call():
    client = Fhir2Client()
    called = False

    async def fake_get(path, params=None):
        nonlocal called
        called = True
        return _bundle()

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.resolve_order_by_accession("")) is None
    assert called is False  # empty accession short-circuits, no fhir2 round-trip


# --- the order's triage signals ride along with the refs (issue #61) --------
def _resolve(resource: dict):
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle(resource)

    client._get = fake_get  # type: ignore[assignment]
    return asyncio.run(client.resolve_order_by_accession("ACC-9"))


def test_resolve_carries_priority_and_reason_codes():
    resolved = _resolve({**_SR, "priority": "stat", "reasonCode": [
        {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "J93.1"}]}]})
    # Without these two the study reaches triage with no urgency at all and scores ROUTINE (#61).
    assert resolved["priority"] == "stat"
    assert resolved["reasonCode"] == ["J93.1"]


def test_resolve_omits_the_signals_the_order_does_not_carry():
    # An order with neither is not an error -- it resolves to the join ref alone, as before #61.
    assert _resolve(_SR) == {"fhirPatientId": "Patient/pat-9",
                             "fhirServiceRequestId": "ServiceRequest/sr-9"}


def test_resolve_drops_a_priority_outside_the_envelope_enum():
    # order.priority is a schema ENUM. A fhir2 answering "emergency" must yield no priority rather
    # than a value that fails StudyContext validation and drops the study on the floor.
    resolved = _resolve({**_SR, "priority": "emergency"})
    assert "priority" not in resolved


def test_resolve_takes_every_coding_and_dedupes():
    resolved = _resolve({**_SR, "reasonCode": [
        {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "J93.1"},
                    {"system": "http://snomed.info/sct", "code": "36118008"}]},
        {"coding": [{"code": "J93.1"}]},          # same code again, different concept
        {"coding": [{"code": "R07.9"}]},
    ]})
    assert resolved["reasonCode"] == ["J93.1", "36118008", "R07.9"]


def test_resolve_never_carries_a_reason_narrative():
    # `text` on a reason is where a clinician's free-text ends up -- PHI's front door. A concept
    # with no coding contributes NOTHING, and the narrative never reaches the wire (Golden rule 2).
    resolved = _resolve({**_SR, "reasonCode": [
        {"text": "Mrs Patel, ?tension pneumothorax, deteriorating on the ward"}]})
    assert "reasonCode" not in resolved
    assert "Patel" not in str(resolved)


# --- get_report_conclusion (issue #16) -------------------------------------
def test_get_report_conclusion_reads_the_typed_ref():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return {"resourceType": "DiagnosticReport", "id": "rep-1",
                "conclusion": "Large left pneumothorax."}

    client._get = fake_get  # type: ignore[assignment]
    text = asyncio.run(client.get_report_conclusion("DiagnosticReport/rep-1"))
    # A prefixed ref is fetched as-is; a bare id would be prefixed with DiagnosticReport/.
    assert calls == ["DiagnosticReport/rep-1"]
    assert text == "Large left pneumothorax."


def test_get_report_conclusion_prefixes_a_bare_id():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return {"resourceType": "DiagnosticReport", "id": "rep-1", "conclusion": "x"}

    client._get = fake_get  # type: ignore[assignment]
    asyncio.run(client.get_report_conclusion("rep-1"))
    assert calls == ["DiagnosticReport/rep-1"]


def test_get_report_conclusion_none_when_absent_or_blank():
    client = Fhir2Client()

    async def no_conclusion(path, params=None):
        return {"resourceType": "DiagnosticReport", "id": "rep-1"}  # no conclusion field

    client._get = no_conclusion  # type: ignore[assignment]
    assert asyncio.run(client.get_report_conclusion("DiagnosticReport/rep-1")) is None

    async def blank_conclusion(path, params=None):
        return {"resourceType": "DiagnosticReport", "conclusion": "   "}

    client._get = blank_conclusion  # type: ignore[assignment]
    assert asyncio.run(client.get_report_conclusion("DiagnosticReport/rep-1")) is None


def test_get_report_conclusion_empty_id_makes_no_call():
    client = Fhir2Client()
    called = False

    async def fake_get(path, params=None):
        nonlocal called
        called = True
        return {}

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.get_report_conclusion("")) is None
    assert called is False  # empty id short-circuits, no fhir2 round-trip


# --- write_presign_impression (issue #26) ----------------------------------

def test_write_presign_impression_creates_when_no_existing_draft():
    client = Fhir2Client(base_url=_LOOPBACK_BASE)
    get_calls = []
    post_calls = []

    async def fake_get(path, params=None):
        get_calls.append((path, params))
        return _bundle()  # no existing preliminary draft for this order

    async def fake_post(path, resource):
        post_calls.append((path, resource))
        return {"id": "new-draft-1"}

    client._get = fake_get  # type: ignore[assignment]
    client._post = fake_post  # type: ignore[assignment]
    report_id = asyncio.run(client.write_presign_impression(
        "ServiceRequest/sr-1", "Patient/pat-1", "No acute findings identified."))

    # Searches by `subject` -- NOT `based-on` or `status`, both of which 400 on live fhir2.
    assert get_calls == [("DiagnosticReport", {"subject": "Patient/pat-1"})]
    assert post_calls[0][0] == "DiagnosticReport"
    posted = post_calls[0][1]
    assert posted["status"] == "preliminary"
    assert posted["subject"] == {"reference": "Patient/pat-1"}
    assert posted["basedOn"] == [{"reference": "ServiceRequest/sr-1"}]
    assert posted["conclusion"] == "No acute findings identified."
    # code must resolve to a real Concept (coding.code = a concept UUID); text-only 500s on fhir2.
    assert posted["code"]["coding"][0]["code"] == "e3641471-3f25-57b4-ab27-a3ebc66e481e"
    assert posted["code"]["text"] == "AI pre-sign impression draft"
    assert "id" not in posted  # a create, not an update
    assert report_id == "new-draft-1"


def test_write_presign_impression_updates_the_existing_draft():
    client = Fhir2Client(base_url=_LOOPBACK_BASE)
    put_calls = []

    async def fake_get(path, params=None):
        # The subject search returns OUR earlier draft for this order: preliminary, based on this
        # order, and carrying our concept stamp (#26 — the authorship discriminator).
        return _bundle({"resourceType": "DiagnosticReport", "id": "draft-9", "status": "preliminary",
                        "basedOn": [{"reference": "ServiceRequest/sr-1"}],
                        "code": {"coding": [{"code": _OUR_CONCEPT}]}})

    async def fake_put(path, resource):
        put_calls.append((path, resource))
        return {"id": "draft-9"}

    async def fail_post(path, resource):
        raise AssertionError("must update the existing draft, not create a new one")

    client._get = fake_get  # type: ignore[assignment]
    client._put = fake_put  # type: ignore[assignment]
    client._post = fail_post  # type: ignore[assignment]
    report_id = asyncio.run(client.write_presign_impression(
        "ServiceRequest/sr-1", "Patient/pat-1", "Findings consistent with pneumothorax."))

    assert put_calls[0][0] == "DiagnosticReport/draft-9"
    assert put_calls[0][1]["id"] == "draft-9"
    assert put_calls[0][1]["conclusion"] == "Findings consistent with pneumothorax."
    assert report_id == "draft-9"


def test_find_presign_draft_matches_preliminary_for_this_order_only():
    """The subject search returns every report for the patient. Only OUR OWN draft is the
    idempotency key: preliminary, based on THIS order, AND stamped with our concept. A signed
    report, a draft for a different order, and — the one that matters (#26) — the RADIOLOGIST's own
    preliminary draft on this very order must all be ignored."""
    client = Fhir2Client()
    seen_params = {}

    async def fake_get(path, params=None):
        seen_params.update(params or {})
        return _bundle(
            {"resourceType": "DiagnosticReport", "id": "final-1", "status": "final",
             "basedOn": [{"reference": "ServiceRequest/sr-1"}],
             "code": {"coding": [{"code": _OUR_CONCEPT}]}},
            {"resourceType": "DiagnosticReport", "id": "other-order", "status": "preliminary",
             "basedOn": [{"reference": "ServiceRequest/sr-99"}],
             "code": {"coding": [{"code": _OUR_CONCEPT}]}},
            # The radiologist's own unsigned draft on THIS order — right status, right order,
            # NOT ours. Overwriting this is the defect the concept stamp exists to prevent.
            {"resourceType": "DiagnosticReport", "id": "human-draft", "status": "preliminary",
             "basedOn": [{"reference": "ServiceRequest/sr-1"}],
             "code": {"coding": [{"code": "radiology-report-concept"}]}},
            {"resourceType": "DiagnosticReport", "id": "draft-2", "status": "preliminary",
             "basedOn": [{"reference": "ServiceRequest/sr-1"}],
             "code": {"coding": [{"code": _OUR_CONCEPT}]}},
        )

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client._find_presign_draft("ServiceRequest/sr-1", "Patient/pat-1")) == "draft-2"
    assert seen_params == {"subject": "Patient/pat-1"}  # searched by subject, not based-on/status


def test_find_presign_draft_none_when_no_preliminary_report():
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "DiagnosticReport", "id": "final-1", "status": "final",
                        "basedOn": [{"reference": "ServiceRequest/sr-1"}]})

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client._find_presign_draft("ServiceRequest/sr-1", "Patient/pat-1")) is None


# ---- basic auth from env (issue #53) ----------------------------------------------

def test_auth_from_env_and_passed_to_httpx(monkeypatch):
    monkeypatch.setenv("FHIR2_BASIC_USER", "poller")
    monkeypatch.setenv("FHIR2_BASIC_PASS", "s3cret")
    # Loopback, not the default remote base: this test exercises a REAL _get (fake httpx), and the
    # read-transport guard (#67) rightly refuses plaintext-to-remote -- credential passing is what
    # is under test here, not transport policy (which has its own tests below).
    client = Fhir2Client(base_url="http://127.0.0.1:8080/openmrs/ws/fhir2/R4")
    assert client._auth == ("poller", "s3cret")

    # The credential must reach the HTTP layer: live fhir2 401s every unauthenticated read,
    # and callers swallow fhir2 errors, so a dropped credential fails silently (#53).
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"resourceType": "Bundle"}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            return _FakeResponse()

    import radagent_common.fhir_client as fhir_client_module
    monkeypatch.setattr(fhir_client_module.httpx, "AsyncClient", _FakeClient)
    asyncio.run(client._get("DiagnosticReport"))
    assert captured["auth"] == ("poller", "s3cret")


def test_no_env_stays_unauthenticated(monkeypatch):
    monkeypatch.delenv("FHIR2_BASIC_USER", raising=False)
    monkeypatch.delenv("FHIR2_BASIC_PASS", raising=False)
    assert Fhir2Client()._auth is None  # mocks and unit tests keep working with no env set


def test_half_set_credentials_fail_loudly(monkeypatch):
    """A partial pair silently downgrading to unauthenticated would recreate the silent-401
    disease #53 exists to cure — reject it at construction instead."""
    import pytest

    monkeypatch.setenv("FHIR2_BASIC_USER", "poller")
    monkeypatch.delenv("FHIR2_BASIC_PASS", raising=False)
    with pytest.raises(ValueError):
        Fhir2Client()
# --- EHR read helpers (issue #4) ---------------------------------------------

def test_get_patient_accepts_bare_id_and_reference():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return {"resourceType": "Patient", "id": "demo-1", "gender": "female"}

    client._get = fake_get  # type: ignore[assignment]
    asyncio.run(client.get_patient("demo-1"))
    asyncio.run(client.get_patient("Patient/demo-1"))
    # Bare id gets normalised to the reference form; a full ref passes through as-is.
    assert calls == ["Patient/demo-1", "Patient/demo-1"]


def test_get_patient_none_on_empty_id():
    client = Fhir2Client()
    called = False

    async def fake_get(path, params=None):
        nonlocal called
        called = True
        return {}

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.get_patient("")) is None
    assert called is False


def test_get_patient_none_on_404():
    import httpx as _httpx
    client = Fhir2Client()

    async def raise_404(path, params=None):
        raise _httpx.HTTPStatusError(
            "not found", request=_httpx.Request("GET", "http://x"),
            response=_httpx.Response(404))

    client._get = raise_404  # type: ignore[assignment]
    assert asyncio.run(client.get_patient("Patient/nope")) is None


# --- search_imaging_studies --------------------------------------------------

def test_search_imaging_studies_projects_to_lean_priors():
    client = Fhir2Client()
    calls = []
    resource = {
        "resourceType": "ImagingStudy", "id": "img-1",
        "started": "2025-04-12T00:00:00Z",
        "modality": [{"system": "http://dicom.nema.org/resources/ontology/DCM", "code": "CT"}],
    }

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle(resource)

    client._get = fake_get  # type: ignore[assignment]
    priors = asyncio.run(client.search_imaging_studies("Patient/demo-1"))
    # patient search param uses the BARE id, not the reference form (some OpenMRS
    # builds reject 'Patient/demo-1' on `patient=` — see _patient_query).
    assert calls[0] == ("ImagingStudy", {"patient": "demo-1"})
    assert priors == [{"ref": "ImagingStudy/img-1", "modality": "CT", "date": "2025-04-12T00:00:00Z"}]


def test_search_imaging_studies_follows_bundle_next_link():
    client = Fhir2Client()
    r1 = {"resourceType": "ImagingStudy", "id": "img-1"}
    r2 = {"resourceType": "ImagingStudy", "id": "img-2"}
    responses = [_bundle(r1, next_url="http://fhir/ImagingStudy?page=2"), _bundle(r2)]

    async def fake_get(path, params=None):
        return responses.pop(0)

    client._get = fake_get  # type: ignore[assignment]
    priors = asyncio.run(client.search_imaging_studies("demo-1"))
    assert [p["ref"] for p in priors] == ["ImagingStudy/img-1", "ImagingStudy/img-2"]


def test_search_imaging_studies_tolerates_missing_modality_and_date():
    """Some scanners omit ImagingStudy.modality or .started; the lean projector must
    still return a schema-valid item (`ref` is the only `required` field)."""
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "ImagingStudy", "id": "img-x"})

    client._get = fake_get  # type: ignore[assignment]
    priors = asyncio.run(client.search_imaging_studies("demo-1"))
    assert priors == [{"ref": "ImagingStudy/img-x"}]


# --- search_observations -----------------------------------------------------

def test_search_observations_joins_system_qualified_codes():
    """FHIR search: `code=<a>,<b>` = OR, one query for the whole panel — and every
    bare code is LOINC-system-qualified. The live fhir2 build matches ZERO rows on a
    bare `code=2160-0` even when the concept carries the LOINC reference map, while
    `http://loinc.org|2160-0` matches all of them (0 vs 16 on the same patient,
    verified 2026-07-23 on the #68 cohort). A caller passing an already-qualified
    token must not get it double-prefixed."""
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle({
            "resourceType": "Observation", "id": "creat-1",
            "code": {"coding": [{"system": "http://loinc.org", "code": "2160-0", "display": "Creatinine"}]},
            "valueQuantity": {"value": 1.1, "unit": "mg/dL"},
            "effectiveDateTime": "2026-06-20"})

    client._get = fake_get  # type: ignore[assignment]
    labs = asyncio.run(client.search_observations(
        "demo-1", ["2160-0", "33914-3", "http://snomed.info/sct|62238-1"]))
    assert calls[0] == ("Observation", {
        "patient": "demo-1",
        "code": "http://loinc.org|2160-0,http://loinc.org|33914-3,http://snomed.info/sct|62238-1"})
    assert labs == [{"code": "2160-0", "display": "Creatinine",
                     "value": 1.1, "unit": "mg/dL", "date": "2026-06-20"}]


def test_search_medications_falls_back_to_reference_display():
    """This fhir2 serializes drug orders as `medicationReference` (no inline
    codeable concept). The lean projection must surface the reference's display so
    the medicationFlags text fallback can match it — without the fallback a patient
    on IV heparin scored onAnticoagulant=false (verified live on the #68 cohort).
    The inline-concept form must still win when present."""
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle(
            {"resourceType": "MedicationRequest", "id": "m1", "status": "active",
             "medicationReference": {"reference": "Medication/abc",
                                     "display": "Heparin Sodium"}},
            {"resourceType": "MedicationRequest", "id": "m2", "status": "active",
             "medicationCodeableConcept": {"coding": [
                 {"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                  "code": "6809", "display": "Metformin"}]}},
            # both forms on one resource: the inline concept must win, the reference
            # display must NOT clobber it
            {"resourceType": "MedicationRequest", "id": "m3", "status": "active",
             "medicationCodeableConcept": {"coding": [
                 {"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                  "code": "1364430", "display": "Apixaban"}]},
             "medicationReference": {"reference": "Medication/xyz",
                                     "display": "Eliquis 5mg tab"}},
        )

    client._get = fake_get  # type: ignore[assignment]
    meds = asyncio.run(client.search_medications("demo-1"))
    assert {"code": "", "display": "Heparin Sodium"} in meds
    assert {"code": "6809", "display": "Metformin"} in meds
    assert {"code": "1364430", "display": "Apixaban"} in meds


def test_search_observations_empty_codes_skips_the_call():
    client = Fhir2Client()
    called = False

    async def fake_get(path, params=None):
        nonlocal called
        called = True
        return _bundle()

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.search_observations("demo-1", [])) == []
    assert called is False


def test_search_observations_handles_valuestring():
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "Observation", "id": "o1",
                        "code": {"coding": [{"code": "X"}]},
                        "valueString": "positive"})

    client._get = fake_get  # type: ignore[assignment]
    labs = asyncio.run(client.search_observations("demo-1", ["X"]))
    assert labs == [{"code": "X", "value": "positive"}]


# --- search_conditions -------------------------------------------------------

def test_search_conditions_asks_for_active_and_re_filters_clientside():
    """Sends `clinical-status=active` (the server SHOULD honor it) AND re-filters
    client-side (in case it doesn't — matches the poll_finalized_reports approach
    to OpenMRS's spotty search-param support)."""
    client = Fhir2Client()
    calls = []
    active = {"resourceType": "Condition", "id": "c1",
              "clinicalStatus": {"coding": [{"code": "active"}]},
              "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10",
                                   "code": "C34.1", "display": "Lung neoplasm"}]}}
    resolved = {"resourceType": "Condition", "id": "c2",
                "clinicalStatus": {"coding": [{"code": "resolved"}]},
                "code": {"coding": [{"code": "J18.9"}]}}

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle(active, resolved)  # server returned both; we drop the resolved one

    client._get = fake_get  # type: ignore[assignment]
    problems = asyncio.run(client.search_conditions("demo-1"))
    assert calls[0] == ("Condition", {"patient": "demo-1", "clinical-status": "active"})
    assert problems == [{"code": "C34.1", "display": "Lung neoplasm"}]


def test_search_conditions_treats_missing_clinicalstatus_as_active():
    """Some deployments omit clinicalStatus entirely. Better to surface the problem
    than to hide it — the radiologist can judge relevance from the code."""
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "Condition", "id": "c1",
                        "code": {"coding": [{"code": "I10", "display": "HTN"}]}})

    client._get = fake_get  # type: ignore[assignment]
    problems = asyncio.run(client.search_conditions("demo-1"))
    assert problems == [{"code": "I10", "display": "HTN"}]


# --- search_allergies --------------------------------------------------------

def test_search_allergies_projects_code_and_criticality():
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "AllergyIntolerance", "id": "a1",
                        "code": {"coding": [{"code": "iodine-contrast"}]},
                        "criticality": "high"})

    client._get = fake_get  # type: ignore[assignment]
    allergies = asyncio.run(client.search_allergies("demo-1"))
    assert allergies == [{"code": "iodine-contrast", "criticality": "high"}]


def test_search_allergies_omits_missing_criticality():
    """Schema requires `code` only; `criticality` is optional — omit it when absent."""
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "AllergyIntolerance", "id": "a2",
                        "code": {"coding": [{"code": "penicillin"}]}})

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.search_allergies("demo-1")) == [{"code": "penicillin"}]


# --- search_medications ------------------------------------------------------

def test_search_medications_returns_active_only_from_medicationCC():
    client = Fhir2Client()
    calls = []
    metformin = {"resourceType": "MedicationRequest", "id": "m1", "status": "active",
                 "medicationCodeableConcept": {
                     "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                 "code": "6809", "display": "Metformin"}]}}
    completed = {"resourceType": "MedicationRequest", "id": "m2", "status": "completed",
                 "medicationCodeableConcept": {"coding": [{"code": "11289"}]}}

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle(metformin, completed)

    client._get = fake_get  # type: ignore[assignment]
    meds = asyncio.run(client.search_medications("demo-1"))
    # No `status` param: live fhir2 500s on MedicationRequest?status=... so activeness is
    # filtered client-side. The completed med is dropped by _medication_is_active, not the server.
    assert calls[0] == ("MedicationRequest", {"patient": "demo-1"})
    assert meds == [{"code": "6809", "display": "Metformin"}]


def test_collect_treats_404_as_empty():
    """A resource type the fhir2 build doesn't expose (e.g. ImagingStudy on o3) answers 404;
    the search degrades to [] instead of raising, so the slice just goes empty."""
    import httpx

    client = Fhir2Client()

    async def fake_get(path, params=None):
        request = httpx.Request("GET", "http://fhir/ImagingStudy")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.search_imaging_studies("demo-1")) == []


# --- #26: the pre-sign draft only ever overwrites ITS OWN draft ----------------------

_OUR_CONCEPT = "e3641471-3f25-57b4-ab27-a3ebc66e481e"   # _DEFAULT_PRESIGN_REPORT_CONCEPT


def _draft(report_id, *, concept, status="preliminary", order="ServiceRequest/sr-1"):
    return {"resourceType": "DiagnosticReport", "id": report_id, "status": status,
            "basedOn": [{"reference": order}],
            "code": {"coding": [{"code": concept}]}}


def test_presign_write_never_overwrites_a_radiologists_own_draft():
    """THE one that matters (#26). `preliminary` is also the status a RIS gives a radiologist's own
    unsigned draft. Matching on status alone would PUT the AI's text over the human's report."""
    client = Fhir2Client(base_url=_LOOPBACK_BASE)
    calls = []

    async def fake_get(path, params=None):
        calls.append(("GET", path))
        # The radiologist has started their own draft on this order. Different concept -> not ours.
        return _bundle(_draft("radiologist-draft", concept="some-other-concept"))

    async def fake_post(path, resource):
        calls.append(("POST", path))
        return {"id": "our-new-draft"}

    async def fake_put(path, resource):
        calls.append(("PUT", path))
        return {"id": path.split("/")[-1]}

    client._get, client._post, client._put = fake_get, fake_post, fake_put  # type: ignore[assignment]
    written = asyncio.run(client.write_presign_impression(
        "ServiceRequest/sr-1", "Patient/p1", "AI draft text"))

    # We POST a NEW report and leave theirs untouched. A PUT here would destroy a human's work.
    assert ("POST", "DiagnosticReport") in calls
    assert not any(verb == "PUT" for verb, _ in calls)
    assert written == "our-new-draft"


def test_presign_write_updates_its_own_earlier_draft():
    """Idempotency still holds for OUR draft: same order, same concept -> update, don't duplicate."""
    client = Fhir2Client(base_url=_LOOPBACK_BASE)
    calls = []

    async def fake_get(path, params=None):
        return _bundle(_draft("our-draft", concept=_OUR_CONCEPT))

    async def fake_post(path, resource):
        calls.append("POST")
        return {"id": "should-not-happen"}

    async def fake_put(path, resource):
        calls.append("PUT")
        assert path == "DiagnosticReport/our-draft"
        return {"id": "our-draft"}

    client._get, client._post, client._put = fake_get, fake_post, fake_put  # type: ignore[assignment]
    written = asyncio.run(client.write_presign_impression(
        "ServiceRequest/sr-1", "Patient/p1", "AI draft text v2"))

    assert calls == ["PUT"]           # updated in place, not duplicated
    assert written == "our-draft"


def test_presign_draft_lookup_ignores_our_draft_on_a_DIFFERENT_order():
    """Our own concept, but a different order -> not this study's draft. Don't touch it."""
    client = Fhir2Client(base_url=_LOOPBACK_BASE)

    async def fake_get(path, params=None):
        return _bundle(_draft("other-order", concept=_OUR_CONCEPT, order="ServiceRequest/sr-999"))

    async def fake_post(path, resource):
        return {"id": "our-new-draft"}

    async def fake_put(path, resource):
        raise AssertionError("must not PUT over another order's report")

    client._get, client._post, client._put = fake_get, fake_post, fake_put  # type: ignore[assignment]
    assert asyncio.run(client.write_presign_impression(
        "ServiceRequest/sr-1", "Patient/p1", "text")) == "our-new-draft"


def test_presign_draft_lookup_ignores_a_FINAL_report():
    """A signed report is never our draft, whatever it is coded with."""
    client = Fhir2Client(base_url=_LOOPBACK_BASE)

    async def fake_get(path, params=None):
        return _bundle(_draft("signed", concept=_OUR_CONCEPT, status="final"))

    async def fake_post(path, resource):
        return {"id": "our-new-draft"}

    async def fake_put(path, resource):
        raise AssertionError("must not PUT over a signed report")

    client._get, client._post, client._put = fake_get, fake_post, fake_put  # type: ignore[assignment]
    assert asyncio.run(client.write_presign_impression(
        "ServiceRequest/sr-1", "Patient/p1", "text")) == "our-new-draft"


# --- #30 security review: write-transport secrecy -------------------------------------------------
#
# A DiagnosticReport write carries PHI (the impression) and rides HTTP Basic credentials. Over
# plaintext http to a remote host both are exposed. The guard is hoisted to the top of
# write_presign_impression (so it refuses before the idempotency GET), and kept in _post/_put as a
# backstop -- the transport-mocking tests above are unaffected because they run over loopback.

def test_write_transport_secure_allows_https_and_loopback_and_optin():
    # https and loopback must be allowed on their OWN merits, so clear the opt-in first. With an
    # ambient FHIR2_ALLOW_INSECURE_WRITE=1 exported, a broken https/loopback bypass would still
    # return True via the opt-in fallback -- the var would MASK the regression. That matters more
    # now that this MR makes the var a normal thing to have set.
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        assert _write_transport_is_secure("https://openmrs.example.org/openmrs/ws/fhir2/R4")
        assert _write_transport_is_secure("http://localhost:8080/openmrs/ws/fhir2/R4")
        assert _write_transport_is_secure("http://127.0.0.1:8080/fhir2/R4")
    with mock.patch.dict(os.environ, {"FHIR2_ALLOW_INSECURE_WRITE": "1"}):
        assert _write_transport_is_secure("http://openmrs:8080/openmrs/ws/fhir2/R4")


def test_write_transport_insecure_rejects_plaintext_to_a_remote_host():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        assert not _write_transport_is_secure("http://openmrs:8080/openmrs/ws/fhir2/R4")
        assert not _write_transport_is_secure("http://fhir.hospital.example:8080/fhir2/R4")


def test_insecure_optin_write_leaves_an_audit_line(caplog):
    # When the opt-in lets a plaintext-remote write through, that accepted risk must be recorded:
    # PHI + credentials are on the wire in cleartext. The audit names the host, never the impression.
    with mock.patch.dict(os.environ, {"FHIR2_ALLOW_INSECURE_WRITE": "1"}):
        with caplog.at_level(logging.WARNING, logger="radagent_common.fhir_client"):
            _guard_write_transport("http://openmrs:8080/openmrs/ws/fhir2/R4")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("PLAINTEXT" in m and "openmrs" in m for m in warnings), warnings


def test_secure_transport_writes_are_not_audited(caplog):
    # No warning noise on the paths that are actually safe -- https or loopback. Cleared env for the
    # same masking reason as the allow-test: an ambient opt-in must not change these outcomes.
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        with caplog.at_level(logging.WARNING, logger="radagent_common.fhir_client"):
            _guard_write_transport("https://openmrs.example.org/openmrs/ws/fhir2/R4")
            _guard_write_transport("http://localhost:8080/openmrs/ws/fhir2/R4")
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == []


def test_insecure_write_error_is_a_named_runtime_error_subclass():
    # The guard raises a NAMED subclass so the orchestrator can mark it non-retryable by type
    # (temporalio keys ApplicationError.type off __class__.__name__), while still being caught by
    # any existing `except RuntimeError`. Both properties are load-bearing; pin them.
    assert issubclass(InsecureWriteTransportError, RuntimeError)
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        with pytest.raises(InsecureWriteTransportError) as ei:
            _guard_write_transport("http://openmrs:8080/openmrs/ws/fhir2/R4")
    assert ei.value.__class__.__name__ == "InsecureWriteTransportError"


# --- read-transport guard (#67): the read side of #30's F2 -------------------------------------
# Every read returns PHI over the same base URL and Basic credentials as the write; the RIS poller
# reads every 30s, so a write-inert deployment is exposed CONTINUOUSLY. Reads therefore refuse
# plaintext-to-remote exactly like writes -- same predicate, so the two policies cannot drift --
# with the opt-in inherited from the write's (the trust statement is about the transport, not the
# verb; the compose stack that already sets FHIR2_ALLOW_INSECURE_WRITE=1 keeps working unchanged).

def test_read_transport_secure_allows_https_and_loopback():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_READ", None)
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        assert _read_transport_is_secure("https://openmrs.example.org/openmrs/ws/fhir2/R4")
        assert _read_transport_is_secure("http://localhost:8080/openmrs/ws/fhir2/R4")
        assert _read_transport_is_secure("http://127.0.0.1:8080/fhir2/R4")


def test_read_transport_rejects_plaintext_to_a_remote_host():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_READ", None)
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        assert not _read_transport_is_secure("http://openmrs:8080/openmrs/ws/fhir2/R4")
        assert not _read_transport_is_secure("http://fhir.hospital.example:8080/fhir2/R4")


def test_reads_inherit_the_write_opt_in():
    # THE compose-compat contract: a deployment that accepted cleartext on this hop for writes
    # (FHIR2_ALLOW_INSECURE_WRITE=1, as docker-compose.yml sets) keeps its reads working without a
    # second variable. Weaken this to a read-only opt-in and every existing stack's poller dies.
    with mock.patch.dict(os.environ, {"FHIR2_ALLOW_INSECURE_WRITE": "1"}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_READ", None)
        assert _read_transport_is_secure("http://openmrs:8080/openmrs/ws/fhir2/R4")


def test_explicit_read_opt_out_overrides_the_write_opt_in():
    # An explicit FHIR2_ALLOW_INSECURE_READ wins over the inherited write value, in both directions.
    with mock.patch.dict(
        os.environ, {"FHIR2_ALLOW_INSECURE_WRITE": "1", "FHIR2_ALLOW_INSECURE_READ": "0"}, clear=False
    ):
        assert not _read_transport_is_secure("http://openmrs:8080/openmrs/ws/fhir2/R4")
    with mock.patch.dict(os.environ, {"FHIR2_ALLOW_INSECURE_READ": "1"}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        assert _read_transport_is_secure("http://openmrs:8080/openmrs/ws/fhir2/R4")


def test_a_plaintext_remote_read_is_refused_before_any_http_call():
    # The refusal must happen BEFORE the request goes out -- refusing after sending would defeat
    # the point. The fake httpx records every attempt; the guard must leave it untouched.
    attempts: list[str] = []

    class _RecordingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            attempts.append(url)
            raise AssertionError("the guard must refuse before any HTTP call")

    import radagent_common.fhir_client as fhir_client_module
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_READ", None)
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        with mock.patch.object(fhir_client_module.httpx, "AsyncClient", _RecordingClient):
            client = Fhir2Client(base_url="http://openmrs:8080/openmrs/ws/fhir2/R4")
            with pytest.raises(InsecureReadTransportError):
                asyncio.run(client._get("DiagnosticReport"))
    assert attempts == []


def test_a_bundle_next_link_onto_a_plaintext_remote_is_refused_too():
    # _get follows absolute, server-authored Bundle `next` URLs. The guard checks the URL actually
    # fetched, so PHI cannot follow a next-link off a safe base onto a plaintext remote hop.
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_READ", None)
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        client = Fhir2Client(base_url=_LOOPBACK_BASE)  # base itself is fine
        with pytest.raises(InsecureReadTransportError):
            asyncio.run(client._get("http://fhir.hospital.example:8080/fhir2/R4?page=2"))


def test_insecure_read_error_is_a_named_runtime_error_subclass():
    # Same load-bearing properties as the write error: catchable as RuntimeError, non-retryable by
    # type at an activity call site (temporalio keys ApplicationError.type off __class__.__name__).
    assert issubclass(InsecureReadTransportError, RuntimeError)
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_READ", None)
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        with pytest.raises(InsecureReadTransportError) as ei:
            _guard_read_transport("http://openmrs:8080/openmrs/ws/fhir2/R4")
    assert ei.value.__class__.__name__ == "InsecureReadTransportError"


def test_insecure_optin_read_warns_once_per_process_per_host(caplog):
    # The warn-once behavior is a design point (the poller reads every 30s; per-read warnings are
    # alarm fatigue). Two guarded reads to the same host under the opt-in must produce exactly ONE
    # audit line. A fresh host still gets its own line.
    import radagent_common.fhir_client as fhir_client_module
    fhir_client_module._INSECURE_READ_WARNED.clear()  # isolate from other tests in this process
    with mock.patch.dict(os.environ, {"FHIR2_ALLOW_INSECURE_READ": "1"}, clear=False):
        with caplog.at_level(logging.WARNING, logger="radagent_common.fhir_client"):
            _guard_read_transport("http://openmrs:8080/openmrs/ws/fhir2/R4")
            _guard_read_transport("http://openmrs:8080/openmrs/ws/fhir2/R4")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [w.getMessage() for w in warnings]
    assert "PLAINTEXT" in warnings[0].getMessage()
    # ...and per-HOST, not once-per-process globally: a second host gets its own audit line. A
    # regression collapsing the set to a global flag would silence the line for every host but
    # the first while this suite stayed green.
    with mock.patch.dict(os.environ, {"FHIR2_ALLOW_INSECURE_READ": "1"}, clear=False):
        with caplog.at_level(logging.WARNING, logger="radagent_common.fhir_client"):
            _guard_read_transport("http://fhir.other.example:8080/fhir2/R4")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, [w.getMessage() for w in warnings]


def test_post_backstop_refuses_an_insecure_transport():
    # The guard is hoisted into write_presign_impression, but it stays in _post as a backstop for any
    # other write path. Pin it directly, so deleting it from _post fails HERE (not silently green).
    client = Fhir2Client(base_url="http://openmrs:8080/openmrs/ws/fhir2/R4")
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        with pytest.raises(InsecureWriteTransportError, match="plaintext HTTP"):
            asyncio.run(client._post("DiagnosticReport", {"resourceType": "DiagnosticReport"}))


def test_put_backstop_refuses_an_insecure_transport():
    # Same for _put, the idempotent re-run path. Without this, deleting the guard from _put (leaving
    # it in _post) keeps the suite green -- only the create path would be covered.
    client = Fhir2Client(base_url="http://openmrs:8080/openmrs/ws/fhir2/R4")
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        with pytest.raises(InsecureWriteTransportError, match="plaintext HTTP"):
            asyncio.run(client._put("DiagnosticReport/x", {"resourceType": "DiagnosticReport"}))


def test_write_refuses_a_plaintext_remote_transport_before_the_idempotency_read():
    """The guard is hoisted to the top of write_presign_impression (backstopped in _post/_put), so a
    write to a plaintext remote host raises before ANY request leaves the process -- the credentialed
    idempotency lookup (_find_presign_draft -> _get, carrying the Authorization header) included.
    Propagates as a failed activity -> bounded retry -> skip, so it never strands the read, but it
    never leaks either."""
    client = Fhir2Client(base_url="http://openmrs:8080/openmrs/ws/fhir2/R4")
    got = []

    async def spy_get(path, params=None):
        got.append(path)  # if this ever runs, a credentialed GET already went out
        return _bundle()

    client._get = spy_get  # type: ignore[assignment]
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        with pytest.raises(InsecureWriteTransportError, match="plaintext HTTP"):
            asyncio.run(client.write_presign_impression(
                "ServiceRequest/sr-1", "Patient/p1", "Findings consistent with pneumothorax."))
    assert got == [], "the idempotency lookup issued a credentialed GET before the guard refused"


def test_compose_orchestrator_write_is_permitted_by_the_transport_guard():
    """Drift guard (#30), same spirit as the #55 UUID-drift test. The HIGH review item was a
    config/code mismatch no test caught: deleting FHIR2_ALLOW_INSECURE_WRITE from compose (while
    FHIR2_BASE_URL stays plaintext-remote) silently makes every pre-sign write refuse, the draft
    never appears, and CI stays green. Pin the coupling: under the compose orchestrator env exactly
    as shipped, the guard must PERMIT the write. Delete the env line and this fails."""
    compose = yaml.safe_load((Path(__file__).resolve().parents[3] / "docker-compose.yml").read_text())
    env = compose["services"]["orchestrator"]["environment"]
    base = env["FHIR2_BASE_URL"]
    allow = str(env.get("FHIR2_ALLOW_INSECURE_WRITE", "")).strip()
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FHIR2_ALLOW_INSECURE_WRITE", None)
        if allow:
            os.environ["FHIR2_ALLOW_INSECURE_WRITE"] = allow
        assert _write_transport_is_secure(base), (
            "the compose orchestrator would have its pre-sign fhir2 write refused by the transport "
            "guard: FHIR2_BASE_URL is plaintext-remote but FHIR2_ALLOW_INSECURE_WRITE is not set "
            "truthy on that service")


def test_authorship_guard_collides_when_the_concept_is_shared():
    """WHY the deployed concept MUST be dedicated (#30 -> #55).

    The authorship discriminator is the `code` concept: _find_presign_draft treats a preliminary
    report on this order as OURS iff it carries our concept. If a deployment points at a SHARED
    concept -- e.g. the CIEL "Provisional diagnosis" a RIS may reuse for its own preliminary reports
    -- a radiologist's own provisional draft coded with that concept matches as ours, and
    write_presign_impression would PUT the AI text over it. #55 is precisely the fix: it moved
    `main`'s default OFF that shared CIEL concept onto a dedicated "AI pre-sign impression draft"
    concept nobody else emits. This test pins the collision that default now prevents, by forcing the
    shared concept back on via FHIR2_PRESIGN_REPORT_CONCEPT. Inert regardless today (the write is
    gated on a COMPLETE finding, and no stub tool emits one).
    """
    client = Fhir2Client()
    shared_concept = "160249AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # the CIEL stand-in a RIS may also use

    async def fake_get(path, params=None):
        return _bundle(_draft("radiologist-own-draft", concept=shared_concept))

    client._get = fake_get  # type: ignore[assignment]
    with mock.patch.dict(os.environ, {"FHIR2_PRESIGN_REPORT_CONCEPT": shared_concept}):
        hit = asyncio.run(client._find_presign_draft("ServiceRequest/sr-1", "Patient/p1"))
    # The collision: a human's draft is matched as ours. Main's dedicated default (#55) is what
    # prevents this -- with a distinct concept the same lookup returns None (see the tests above).
    assert hit == "radiologist-own-draft"


# --- typed clinical reads for the Communications Agent (#52) -------------------------
# Read-only: fhir2 stays the source of clinical context. The notification and its ack are
# written to the comms ledger instead (see test_comms_ledger.py).

def test_get_diagnostic_report_returns_the_whole_typed_report():
    """Distinct from get_report_conclusion (#16), which returns only the narrative. CritCom
    decides who to call and how loudly from the ACR extension too, so it needs the resource."""
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return {"resourceType": "DiagnosticReport", "id": "rep-1", "status": "final",
                "subject": {"reference": "Patient/p1"},
                "basedOn": [{"reference": "ServiceRequest/sr-1"}],
                "conclusion": "Large tension pneumothorax.",
                "extension": [{"url": "http://critcom/StructureDefinition/acr-category",
                               "valueCode": "Cat1"}]}

    client._get = fake_get  # type: ignore[assignment]
    report = asyncio.run(client.get_diagnostic_report("rep-1"))

    assert calls == ["DiagnosticReport/rep-1"]      # bare id gets prefixed
    assert report.conclusion == "Large tension pneumothorax."
    assert report.acr_category == "Cat1"
    assert report.service_request_id == "sr-1"
    assert report.patient_id == "p1"


def test_get_diagnostic_report_accepts_a_qualified_ref_and_empty_id():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return {"resourceType": "DiagnosticReport", "id": "rep-1", "status": "final"}

    client._get = fake_get  # type: ignore[assignment]
    asyncio.run(client.get_diagnostic_report("DiagnosticReport/rep-1"))
    assert calls == ["DiagnosticReport/rep-1"]      # not double-prefixed
    assert asyncio.run(client.get_diagnostic_report("")) is None
    assert calls == ["DiagnosticReport/rep-1"]      # empty id makes no call


def test_get_service_request_reads_the_order_by_id():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return {"resourceType": "ServiceRequest", "id": "sr-1", "status": "active",
                "priority": "stat", "subject": {"reference": "Patient/p1"},
                "requester": {"reference": "Practitioner/dr-1"}}

    client._get = fake_get  # type: ignore[assignment]
    order = asyncio.run(client.get_service_request("sr-1"))

    assert calls == ["ServiceRequest/sr-1"]
    assert order.priority == "stat"                 # drives how loudly CritCom escalates
    assert order.requester.reference == "Practitioner/dr-1"   # the physician to notify


def test_missing_report_or_order_is_none_not_a_crash():
    """A 404 degrades to None so the agent can fall back, rather than failing the dispatch."""
    import httpx

    client = Fhir2Client()

    async def fake_get(path, params=None):
        request = httpx.Request("GET", "http://fhir/x")
        raise httpx.HTTPStatusError(
            "not found", request=request, response=httpx.Response(404, request=request))

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.get_diagnostic_report("nope")) is None
    assert asyncio.run(client.get_service_request("nope")) is None
