"""Contract + behavior tests for the EHR Assistant handler (issue #4).

Uses a FakeFhir2Client to intercept every fhir2 read, so tests exercise the
handler's real assembly logic (concurrent gather, degradation, projection to the
schema shape, contrast/medication flag derivation) without touching a live
OpenMRS. Handler's own `_client()` factory is monkey-patched to return the fake.
"""
from __future__ import annotations

from typing import Any

import handler
from handler import handle
from radagent_common.validation import validate_skill_output


SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_test",
    "study": {
        "studyInstanceUID": "1.2.3",
        "orthancStudyId": "abc123",
        "modality": "CT",
        "studyDescription": "CT CHEST W/O",
    },
    "patient": {"fhirPatientId": "Patient/demo-1"},
    "order": {"priority": "routine", "reasonCode": ["R91.8"]},
    "meta": {"traceId": "trc_x", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


class FakeFhir2Client:
    """Mimics the Fhir2Client surface the handler calls. Every method returns a
    canned result unless a per-instance override is set (raise_on set to the method
    name makes that method raise, so the degrade path is exercised)."""

    def __init__(
        self,
        priors: list[dict] | None = None,
        labs: list[dict] | None = None,
        problems: list[dict] | None = None,
        allergies: list[dict] | None = None,
        meds: list[dict] | None = None,
        raise_on: set[str] | None = None,
    ):
        self.priors = priors or []
        self.labs = labs or []
        self.problems = problems or []
        self.allergies = allergies or []
        self.meds = meds or []
        self.raise_on = raise_on or set()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def search_imaging_studies(self, pid):
        self.calls.append(("search_imaging_studies", (pid,)))
        if "search_imaging_studies" in self.raise_on:
            raise RuntimeError("boom")
        return self.priors

    async def search_observations(self, pid, codes):
        self.calls.append(("search_observations", (pid, tuple(codes))))
        if "search_observations" in self.raise_on:
            raise RuntimeError("boom")
        return self.labs

    async def search_conditions(self, pid):
        self.calls.append(("search_conditions", (pid,)))
        if "search_conditions" in self.raise_on:
            raise RuntimeError("boom")
        return self.problems

    async def search_allergies(self, pid):
        self.calls.append(("search_allergies", (pid,)))
        if "search_allergies" in self.raise_on:
            raise RuntimeError("boom")
        return self.allergies

    async def search_medications(self, pid):
        self.calls.append(("search_medications", (pid,)))
        if "search_medications" in self.raise_on:
            raise RuntimeError("boom")
        return self.meds


def _install(monkeypatch, fake: FakeFhir2Client) -> None:
    monkeypatch.setattr(handler, "_client", lambda: fake)


# --- Contract preservation ---------------------------------------------------

async def test_output_conforms_to_contract(monkeypatch):
    _install(monkeypatch, FakeFhir2Client())
    out = await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("ehr.assembleContext", out)  # raises ContractError on violation
    assert out["workflowId"] == "wf_test"


async def test_unresolved_patient_returns_empty_valid_packet(monkeypatch):
    """Ingress could not resolve the patient — no point fetching against
    Patient/UNRESOLVED. Verify: no fhir2 calls are made, packet is empty-valid."""
    fake = FakeFhir2Client()
    _install(monkeypatch, fake)
    ctx = {**SAMPLE_CONTEXT, "patient": {"fhirPatientId": "Patient/UNRESOLVED"}}
    out = await handle("ehr.assembleContext", {"studyContext": ctx})
    validate_skill_output("ehr.assembleContext", out)
    assert out["priorStudies"] == []
    assert out["relevantLabs"] == []
    assert out["contrastFlags"] == {"egfr": None, "priorReaction": False, "onMetformin": False}
    assert fake.calls == []  # no round-trip to fhir2 when patient is unresolved


async def test_missing_patient_id_returns_empty_valid_packet(monkeypatch):
    """Same as unresolved: missing fhirPatientId altogether must not crash."""
    fake = FakeFhir2Client()
    _install(monkeypatch, fake)
    ctx = {**SAMPLE_CONTEXT, "patient": {}}
    out = await handle("ehr.assembleContext", {"studyContext": ctx})
    validate_skill_output("ehr.assembleContext", out)
    assert fake.calls == []


# --- Query construction (integration-adjacent) -------------------------------

async def test_all_slices_are_fetched_with_bare_patient_id(monkeypatch):
    """Handler must pass the FHIR reference form ('Patient/demo-1') to Fhir2Client
    (the client normalises to bare id internally — verified in test_fhir_client).
    Assertion here is the pass-through happened for every slice."""
    fake = FakeFhir2Client()
    _install(monkeypatch, fake)
    await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    called = {name for (name, _) in fake.calls}
    assert called == {"search_imaging_studies", "search_observations",
                      "search_conditions", "search_allergies", "search_medications"}
    for name, args in fake.calls:
        assert args[0] == "Patient/demo-1"


async def test_observation_search_asks_for_creatinine_and_egfr_panel(monkeypatch):
    fake = FakeFhir2Client()
    _install(monkeypatch, fake)
    await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    obs_calls = [c for c in fake.calls if c[0] == "search_observations"]
    codes = obs_calls[0][1][1]
    assert "2160-0" in codes                    # creatinine (safety net)
    assert "88293-6" in codes                   # eGFR CKD-EPI 2021 race-free (current recommendation)
    assert set(codes) >= {"33914-3", "48642-3", "48643-1", "62238-1", "88293-6", "98979-8"}


# --- Happy-path assembly against a fixture-like bundle -----------------------

async def test_full_assembly_from_sample_bundle_shape(monkeypatch):
    """Uses the same shapes as mocks/fixtures/fhir_bundle.sample.json (creatinine
    Observation + Condition + Patient). Adds eGFR + metformin so we exercise the
    contrastFlags / medicationFlags derivation end-to-end."""
    fake = FakeFhir2Client(
        priors=[{"ref": "ImagingStudy/prior-1", "modality": "CT", "date": "2024-08-01"}],
        labs=[
            {"code": "2160-0", "display": "Creatinine", "value": 1.1,
             "unit": "mg/dL", "date": "2026-06-20"},
            {"code": "88293-6", "display": "eGFR CKD-EPI", "value": 72,
             "unit": "mL/min/1.73m2", "date": "2026-06-20"},
        ],
        problems=[{"code": "C34.1", "display": "Lung neoplasm"}],
        allergies=[{"code": "penicillin", "criticality": "high"}],
        meds=[{"code": "6809", "display": "Metformin"}],
    )
    _install(monkeypatch, fake)
    out = await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("ehr.assembleContext", out)
    assert out["priorStudies"] == [{"ref": "ImagingStudy/prior-1",
                                    "modality": "CT", "date": "2024-08-01"}]
    assert out["activeProblems"] == [{"code": "C34.1", "display": "Lung neoplasm"}]
    assert out["allergies"] == [{"code": "penicillin", "criticality": "high"}]
    assert out["contrastFlags"]["egfr"] == 72
    assert out["contrastFlags"]["priorReaction"] is False   # penicillin != contrast
    assert out["contrastFlags"]["onMetformin"] is True      # mirrors medicationFlags
    assert out["medicationFlags"]["onMetformin"] is True
    # Bulletproof safety net: creatinine appears in relevantLabs even alongside eGFR.
    assert any(l["code"] == "2160-0" for l in out["relevantLabs"])


async def test_egfr_uses_latest_when_multiple_variants_present(monkeypatch):
    """Different LOINC eGFR variants may co-exist; the latest date wins."""
    fake = FakeFhir2Client(labs=[
        {"code": "33914-3", "value": 45, "date": "2024-01-01"},  # older MDRD
        {"code": "88293-6", "value": 68, "date": "2026-06-20"},  # newer race-free
    ])
    _install(monkeypatch, fake)
    out = await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    assert out["contrastFlags"]["egfr"] == 68


async def test_egfr_null_when_no_egfr_observation(monkeypatch):
    """The bulletproof-safety-net design: creatinine alone leaves egfr=null but
    still surfaces creatinine in relevantLabs so a reader can eyeball kidney fn."""
    fake = FakeFhir2Client(labs=[
        {"code": "2160-0", "display": "Creatinine", "value": 1.4, "date": "2026-06-20"},
    ])
    _install(monkeypatch, fake)
    out = await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    assert out["contrastFlags"]["egfr"] is None
    assert out["relevantLabs"] == [
        {"code": "2160-0", "display": "Creatinine", "value": 1.4, "date": "2026-06-20"}]


# --- Contrast allergy detection ---------------------------------------------

async def test_contrast_allergy_by_snomed(monkeypatch):
    fake = FakeFhir2Client(allergies=[{"code": "293637006", "criticality": "high"}])
    _install(monkeypatch, fake)
    out = await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    assert out["contrastFlags"]["priorReaction"] is True


async def test_contrast_allergy_by_freetext(monkeypatch):
    """Fallback matches 'contrast' or 'iodine' anywhere in the code/display."""
    fake = FakeFhir2Client(allergies=[{"code": "iodinated-contrast-media"}])
    _install(monkeypatch, fake)
    out = await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    assert out["contrastFlags"]["priorReaction"] is True


# --- Medication flag matrix -------------------------------------------------

async def test_med_flags_rxnorm_matches(monkeypatch):
    """Every flag matches by RxNorm code alone (precise path)."""
    fake = FakeFhir2Client(meds=[
        {"code": "6809"},                          # metformin
        {"code": "1599538"},                       # apixaban
        {"code": "20352"},                         # metoprolol
        {"code": "5856"},                          # insulin
        {"code": "10600"},                         # tacrolimus
    ])
    _install(monkeypatch, fake)
    out = await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    assert out["medicationFlags"] == {
        "onMetformin": True, "onAnticoagulant": True, "onBetaBlocker": True,
        "onInsulin": True, "onImmunosuppressant": True,
    }


async def test_med_flags_text_fallback_when_no_rxnorm(monkeypatch):
    """OpenMRS deployments often carry meds coded with SNOMED or free-text.
    Case-insensitive text search on display keeps flags working."""
    fake = FakeFhir2Client(meds=[
        {"code": "SN-12345", "display": "Warfarin sodium 5mg tablet"},
    ])
    _install(monkeypatch, fake)
    out = await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    assert out["medicationFlags"]["onAnticoagulant"] is True


async def test_med_flags_all_false_when_none_matched(monkeypatch):
    fake = FakeFhir2Client(meds=[
        {"code": "SN-99999", "display": "Acetaminophen 500mg"},
    ])
    _install(monkeypatch, fake)
    out = await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    for flag, value in out["medicationFlags"].items():
        assert value is False, f"expected {flag} False, got {value}"


# --- Graceful degradation on partial failure --------------------------------

async def test_one_slice_failure_does_not_starve_others(monkeypatch):
    """A single fhir2 endpoint failing must degrade only its own slice, not the
    whole packet. Verified: other slices still populate, output is schema-valid."""
    fake = FakeFhir2Client(
        priors=[{"ref": "ImagingStudy/p1", "modality": "MR"}],
        labs=[{"code": "2160-0", "display": "Creatinine", "value": 1.0}],
        problems=[{"code": "I10", "display": "HTN"}],
        raise_on={"search_conditions"},   # this slice will explode
    )
    _install(monkeypatch, fake)
    out = await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("ehr.assembleContext", out)
    assert out["priorStudies"] == [{"ref": "ImagingStudy/p1", "modality": "MR"}]
    assert out["relevantLabs"] == [{"code": "2160-0", "display": "Creatinine", "value": 1.0}]
    assert out["activeProblems"] == []   # degraded to empty
    # Other slices remained
    assert isinstance(out["contrastFlags"], dict)


async def test_total_fhir2_outage_returns_valid_packet(monkeypatch):
    """Every endpoint down: packet is empty-but-schema-valid, workflow proceeds."""
    fake = FakeFhir2Client(raise_on={
        "search_imaging_studies", "search_observations", "search_conditions",
        "search_allergies", "search_medications",
    })
    _install(monkeypatch, fake)
    out = await handle("ehr.assembleContext", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("ehr.assembleContext", out)
    assert out["priorStudies"] == []
    assert out["relevantLabs"] == []
    assert out["activeProblems"] == []
    assert out["allergies"] == []
    # Med flags default to all-False (no meds -> no matches)
    assert set(out["medicationFlags"].values()) == {False}


# --- Skill dispatch ---------------------------------------------------------

async def test_unexpected_skill_rejected():
    import pytest
    with pytest.raises(ValueError):
        await handle("ehr.notAssemble", {"studyContext": SAMPLE_CONTEXT})
