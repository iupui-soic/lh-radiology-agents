"""The walking skeleton must walk DISTINCT scenarios, not one scenario five times (#125).

Before this, `mocks/run_walking_skeleton.py` hardcoded `DiagnosticReport/demo-1` for every
fixture and `_DemoFhir.get_report_conclusion` ignored the id it was handed, returning one
pneumothorax narrative. Triage and tool selection genuinely varied, so the run LOOKED like five
studies; everything downstream of the report fetch collapsed onto one. A routine screening
mammogram reported a pneumothorax and paged the on-call, in every run of the harness.

Contract validation cannot catch that -- a constant is schema-valid -- which is the same shape as
#118: a payload that is correct about something that did not happen. So the guard has to be an
explicit differentiation assertion, and it has to pin BOTH ends. "Not all identical" alone is
satisfiable by making everything routine, which would silently drop the critical path instead.

The EHR hop is stubbed here rather than called. The real handler builds its own Fhir2Client and
reaches for a live `openmrs`, which has nothing to do with criticality and would put a network
call in a unit test; _degraded_ehr below returns exactly what the skeleton gets today when
that fetch degrades.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "mocks" / "fixtures"
sys.path.insert(0, str(ROOT / "libs" / "radagent-common"))

_spec = importlib.util.spec_from_file_location("wsk", ROOT / "mocks" / "run_walking_skeleton.py")
wsk = importlib.util.module_from_spec(_spec)
sys.modules["wsk"] = wsk
_spec.loader.exec_module(wsk)

STUDY_FIXTURES = sorted(FIXTURES.glob("studycontext.*.json"))
REPORT_FIXTURES = sorted(FIXTURES.glob("diagnosticreport.*.final.json"))

def _degraded_ehr(workflow_id: str) -> dict:
    """What ehr.assembleContext returns when every slice degrades -- which is what the skeleton
    already gets today, since the real handler reaches for a live `openmrs` that is not there.
    Schema-valid, because run_fixture validates this hop like every other."""
    return {"schemaVersion": "1.0.0", "workflowId": workflow_id, "priorStudies": [],
            "relevantLabs": [], "activeProblems": [], "allergies": [], "medicationFlags": {},
            "contrastFlags": {"egfr": None, "priorReaction": False, "onMetformin": False},
            "agentVersion": "test-stub", "assembledAt": "2026-06-27T10:00:00Z"}


def _scenario(path: Path) -> str:
    if path.name.startswith("studycontext."):
        return path.name[len("studycontext."):-len(".json")]
    return path.name[len("diagnosticreport."):-len(".final.json")]


def _run(coro):
    """asyncio.run, which CLOSES the loop it makes. Building a loop per call and leaving it open
    raises PytestUnraisableExceptionWarning once these accumulate."""
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def outcomes() -> dict[str, dict]:
    """Drive run_fixture itself for every StudyContext fixture, once.

    Deliberately the real function rather than a re-derivation of it. The first version of this
    fixture built the report reference itself and so passed with the constant still in place.

    Only the EHR handler is substituted: the real one builds its own Fhir2Client and reaches for a
    live `openmrs`, which has nothing to do with criticality and would put a network call in a
    unit test. The packet below is what the skeleton already gets when that fetch degrades.
    """
    fhir = wsk._DemoFhir()

    async def stub_ehr(_skill, payload):
        return _degraded_ehr(payload["studyContext"]["workflowId"])

    triage = wsk.load_handler("worklist-triage")
    interp = wsk.load_handler("interpretation-assistant")
    impression = wsk.load_handler("impression-generation")
    impression.__globals__["_FHIR"] = fhir
    verify = wsk.load_handler("report-verification")
    verify.__globals__["_FHIR"] = fhir
    comms = wsk.load_handler("communications")
    comms.__globals__["_FHIR"] = fhir
    comms.__globals__["_LEDGER"] = wsk._DemoLedger()
    handlers = (triage, stub_ehr, interp, impression, verify, comms)

    async def all_of() -> dict[str, dict]:
        out = {}
        for path in STUDY_FIXTURES:
            summary = await wsk.run_fixture(path, handlers, fhir)
            # resolved through the reference run_fixture actually built, not one rebuilt here
            summary["conclusion"] = await fhir.get_report_conclusion(summary["reportRef"])
            out[_scenario(path)] = summary
        return out

    return _run(all_of())


# --- the fixtures themselves -------------------------------------------------------------

def test_there_are_fixtures_to_check():
    """Guards the vacuity every assertion below shares: with no fixtures discovered, the pairing
    and distinctness tests compare empty sets and PASS. Same guard, and the same reason, as
    interpretation-assistant's test_corpus_fixture_is_not_empty."""
    assert STUDY_FIXTURES, f"no studycontext fixtures discovered under {FIXTURES}"
    assert REPORT_FIXTURES, f"no diagnosticreport fixtures discovered under {FIXTURES}"
    assert len(STUDY_FIXTURES) >= 2, "one fixture cannot demonstrate differentiation"


def test_every_studycontext_fixture_has_its_own_report_fixture():
    """The convention _DemoFhir.report_ref_for relies on. A StudyContext with no matching report
    is the state that produced #125, so it is an error rather than a fallback to a shared one."""
    assert {_scenario(p) for p in STUDY_FIXTURES} == {_scenario(p) for p in REPORT_FIXTURES}


def test_every_report_fixture_is_a_valid_diagnostic_report():
    """Nothing validated these before: `diagnosticreport` is not a family in
    scripts/validate_contracts.py, so diagnosticreport.ct_aortic_dissection.final.json sat in the
    repo referenced by nothing and checked by nothing until #125. Validate against the model the
    real client parses reports with, so a malformed fixture fails here rather than at the
    skeleton's next run."""
    from radagent_common.fhir_models import DiagnosticReport
    for path in REPORT_FIXTURES:
        resource = json.loads(path.read_text())
        DiagnosticReport.model_validate(resource)
        assert resource.get("conclusion", "").strip(), (
            f"{path.name}: a report with no conclusion gives the skeleton nothing to differentiate"
        )


def test_report_fixture_ids_are_distinct():
    ids = [json.loads(p.read_text())["id"] for p in REPORT_FIXTURES]
    assert len(ids) == len(set(ids)), f"duplicate DiagnosticReport ids: {ids}"


def test_report_conclusions_are_distinct(outcomes):
    """The constant this issue is about: one narrative reused across every fixture."""
    conclusions = [o["conclusion"] for o in outcomes.values()]
    assert all(conclusions), "every fixture must resolve a conclusion"
    assert len(set(conclusions)) == len(conclusions), "fixtures share a report conclusion"


# --- the stub resolves by id, like the real client ---------------------------------------

@pytest.mark.parametrize("ref", ["report-aorta-001", "DiagnosticReport/report-aorta-001"])
def test_conclusion_resolves_from_a_bare_id_or_a_typed_reference(ref):
    """Fhir2Client.get_report_conclusion normalises both forms; the stub must too."""
    assert "aortic dissection" in _run(wsk._DemoFhir().get_report_conclusion(ref))


@pytest.mark.parametrize("ref", ["", "DiagnosticReport/no-such-report"])
def test_unknown_or_empty_id_returns_none_rather_than_a_default_report(ref):
    """The old stub returned its one narrative whatever it was handed. Returning a report for an
    id that does not exist is how a wrong narrative reaches a chart."""
    assert _run(wsk._DemoFhir().get_report_conclusion(ref)) is None


def test_a_scenario_with_no_report_fixture_is_an_error_not_a_fallback():
    with pytest.raises(AssertionError, match="no report fixture"):
        wsk._DemoFhir().report_ref_for("not_a_scenario")


# --- differentiation, pinned at both ends ------------------------------------------------

def test_fixtures_do_not_all_produce_identical_critical_flags(outcomes):
    """The acceptance criterion for #125."""
    flags = {s: o["criticalFlags"] for s, o in outcomes.items()}
    assert len(set(flags.values())) > 1, f"every fixture produced the same criticalFlags: {flags}"


def test_at_least_one_fixture_is_critical_and_pages(outcomes):
    """Pins the OTHER end: 'not all identical' is also satisfied by making everything routine,
    which would drop the critical path instead of covering it."""
    critical = {s: o for s, o in outcomes.items() if o["criticalFlags"]}
    assert critical, "no fixture produces a critical finding"
    for scenario, o in critical.items():
        assert o["verificationStatus"] == "FAIL", f"{scenario}: critical finding did not FAIL verification"
        assert "oncall-pager" in o["channels"], f"{scenario}: critical finding did not page"


def test_at_least_one_fixture_is_routine_passes_and_does_not_page(outcomes):
    """The path that had ZERO skeleton coverage before #125: empty criticalFlags, a PASS, and no
    page. Every fixture paged the on-call, including a routine screening mammogram."""
    routine = {s: o for s, o in outcomes.items() if not o["criticalFlags"]}
    assert routine, "no fixture exercises the non-critical path"
    for scenario, o in routine.items():
        assert o["verificationStatus"] == "PASS", f"{scenario}: routine study did not PASS ({o['verificationStatus']})"
        assert "oncall-pager" not in o["channels"], f"{scenario}: routine study paged the on-call"


def test_the_routine_screening_mammogram_does_not_report_a_pneumothorax(outcomes):
    """The concrete symptom from the issue, pinned so it cannot come back quietly."""
    mammo = outcomes["mammo_routine"]
    assert "pneumothorax" not in (mammo["conclusion"] or "").lower()
    assert mammo["criticalFlags"] == ()
    assert set(mammo["channels"]) == {"ehr-inbox"}
