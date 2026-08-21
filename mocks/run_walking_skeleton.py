"""Walking skeleton (M0) — runs the whole agent pipeline IN-PROCESS, no Temporal / no A2A
servers required, and validates every hop against /contracts.

This is the M0 "it runs and the contracts hold together" proof. The live wiring
(Temporal workflow + A2A transport) is exercised in M1; here we call each agent's
pure handler directly in the order the StudyWorkflow would.

Run:  python mocks/run_walking_skeleton.py [mocks/fixtures/studycontext.*.json]
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"
FIXTURES = ROOT / "mocks" / "fixtures"
sys.path.insert(0, str(ROOT / "libs" / "radagent-common"))

from radagent_common.fhir_models import (  # noqa: E402
    CodeableConcept,
    Coding,
    PractitionerRole,
    Reference,
    ServiceRequest,
)
from radagent_common.validation import validate_skill_output  # noqa: E402


def load_handler(agent_dir: str):
    """Import an agent's handler.py despite the hyphenated (non-package) directory."""
    adir = AGENTS / agent_dir
    sys.path.insert(0, str(adir))  # so the handler's sibling imports (registry/rules) resolve
    spec = importlib.util.spec_from_file_location(f"{agent_dir}_handler", adir / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    return mod.handle


class _DemoFhir:
    """Stands in for Fhir2Client so the in-process skeleton exercises Impression's real
    report-content fetch (#16) without a live fhir2. The finalized event is lean (no narrative),
    so the handler fetches the DiagnosticReport `conclusion` from source -- here, this stub.

    It reads the `diagnosticreport.*.final.json` fixtures and indexes them by FHIR id, the way
    the real client addresses them (GET DiagnosticReport/<id>). Before #125 this returned ONE
    hardcoded pneumothorax narrative and ignored the id it was handed, so all five fixtures were
    impressed, verified and communicated from the same report: a routine screening mammogram
    reported a pneumothorax and paged the on-call, every run. Contract validation could not see
    it, because a constant is schema-valid -- the same shape as #118.
    """

    def __init__(self) -> None:
        self._reports: dict[str, dict] = {}
        self._by_scenario: dict[str, str] = {}
        for path in sorted(FIXTURES.glob("diagnosticreport.*.final.json")):
            resource = json.loads(path.read_text())
            scenario = path.name[len("diagnosticreport."):-len(".final.json")]
            self._reports[resource["id"]] = resource
            self._by_scenario[scenario] = resource["id"]

    def report_ref_for(self, scenario: str) -> str:
        """The report reference for a StudyContext fixture, by filename convention:
        `studycontext.<scenario>.json` <-> `diagnosticreport.<scenario>.final.json`.

        A missing report fixture is a hard error rather than a fallback to some default report.
        A fallback is exactly how #125 happened: every scenario silently shared one narrative.
        """
        try:
            return f"DiagnosticReport/{self._by_scenario[scenario]}"
        except KeyError:
            raise AssertionError(
                f"no report fixture for scenario '{scenario}': add "
                f"mocks/fixtures/diagnosticreport.{scenario}.final.json. Every StudyContext "
                f"fixture needs its own report, so the skeleton walks distinct scenarios (#125)."
            ) from None

    async def get_report_conclusion(self, diagnostic_report_id: str) -> str | None:
        """Mirrors Fhir2Client.get_report_conclusion, including its Optional return: accepts a
        bare id or a typed reference, and returns None for an empty id, an unknown report, or one
        with no usable conclusion. The old stub's non-Optional `str` was narrower than the real
        contract, so the skeleton never walked the None branch the handlers guard against."""
        if not diagnostic_report_id:
            return None
        resource = self._reports.get(diagnostic_report_id.split("/")[-1])
        if resource is None:
            return None
        conclusion = resource.get("conclusion")
        return conclusion if isinstance(conclusion, str) and conclusion.strip() else None

    async def get_service_request(self, ref: str) -> ServiceRequest | None:
        if not ref:
            return None
        return ServiceRequest(
            id=ref.split("/")[-1],
            subject=Reference(reference="Patient/demo-1"),
            requester=Reference(reference="Practitioner/demo-ordering"),
        )

    async def write_critical_result_notification(
        self, *, patient_ref: str, finding: str, accession: str,
        ack_task_id: str, sent_iso: str,
    ) -> str | None:
        """The #79 ehr-inbox chart write (#118). Without this the CRITICAL dispatch path raised
        AttributeError on every fixture -- swallowed by tools.deliver_critical_result_to_chart's
        never-raise contract into a FAILED channel, which the summary then dropped. The skeleton
        stayed green over a hop that failed five times out of five.

        Mirrors the real client's contract, flag included: None WITHOUT any I/O when
        EHR_INBOX_WRITE_ENABLED is off (the default), so the skeleton's default run exercises the
        same short-circuit production takes. Flag on, it returns an id like a landed write, which
        is what makes the flag-on path reachable in-process at all."""
        if os.environ.get("EHR_INBOX_WRITE_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
            return None
        return f"demo-observation-{accession}"


class _DemoLedger:
    """In-memory stand-in for the communications agent's FHIR ledger."""

    def __init__(self) -> None:
        self._next_id = 0

    def _id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    async def create_communication(self, communication):
        communication.id = self._id("communication")
        return communication

    async def create_task(self, task):
        task.id = self._id("task")
        return task

    async def search_on_call_roles(self, specialty_code: str | None = None):
        return [
            PractitionerRole(
                id="role-oncall",
                practitioner=Reference(reference="Practitioner/demo-oncall"),
                code=[CodeableConcept(coding=[Coding(code="on-call")])],
            )
        ]


async def run_fixture(fixture: Path, handlers: tuple, fhir: "_DemoFhir") -> dict:
    """Run and validate every pipeline hop for one StudyContext fixture, and return what it
    produced.

    The return value exists so the #125 differentiation tests can assert on THIS function rather
    than re-deriving the pipeline themselves. A test that rebuilt the report reference on its own
    passed while the constant `DiagnosticReport/demo-1` was still here -- it never executed the
    line that carried the bug. The harness and its guard have to walk the same code.
    """
    ctx = json.loads(fixture.read_text())
    wf = ctx["workflowId"]
    scenario = fixture.name[len("studycontext."):-len(".json")]
    triage, ehr, interp, impression, verify, comms = handlers

    # 1) Pre-read fan-out (triage ‖ ehr ‖ interpretation)
    t, e, a = await asyncio.gather(
        triage("triage.score", {"studyContext": ctx}),
        ehr("ehr.assembleContext", {"studyContext": ctx}),
        interp("interpretation.runTools", {"studyContext": ctx}),
    )
    for skill, out in [("triage.score", t), ("ehr.assembleContext", e), ("interpretation.runTools", a)]:
        validate_skill_output(skill, out)

    # 2) (radiologist signs report in RIS — simulated finalized event)
    # The id is per-scenario (#125). It used to be the constant "DiagnosticReport/demo-1" for
    # every fixture, which -- together with a stub that ignored the id -- collapsed everything
    # downstream of this point onto one narrative.
    report_event = {"schemaVersion": "1.0.0", "eventType": "ris.report.finalized",
                    "diagnosticReportId": fhir.report_ref_for(scenario), "status": "final",
                    "lastUpdatedCursor": "2026-06-26T12:30:00Z"}

    # 3) Impression
    imp = await impression("impression.generate",
                           {"studyContext": ctx, "report": report_event, "ehrContext": e, "aiFindings": a})
    validate_skill_output("impression.generate", imp)

    # 4) Verify
    ver = await verify("report.verify",
                       {"studyContext": ctx, "report": report_event, "impression": imp, "ehrContext": e, "aiFindings": a})
    validate_skill_output("report.verify", ver)

    # 5) Communicate (last, matching StudyWorkflow.run)
    dispatch = await comms(
        "comms.dispatch",
        {
            "studyContext": ctx,
            "report": report_event,
            "impression": imp,
            "verification": ver,
        },
    )
    validate_skill_output("comms.dispatch", dispatch)

    tools = ",".join(tool["toolId"] for tool in a["toolsSelected"]) or "none"
    # channel:STATUS, not the bare channel name (#118). The schema accepts "FAILED" as a valid
    # value, so contract validation can never be the signal here -- printing the status is what
    # makes a failed hop visible at all.
    channel_results = dispatch.get("channelResults", [])
    channels = ",".join(
        f"{result['channel']}:{result.get('status', '?')}" for result in channel_results
    ) or "none"
    failed_channels = [r["channel"] for r in channel_results if r.get("status") == "FAILED"]
    # Labels contain spaces ("aortic dissection"), and every other field on this line is a
    # space-free key=value pair, so spaces are collapsed rather than left to break the pattern a
    # reader (or a grep) uses to scan it.
    flags = ",".join(
        "-".join(f["label"].split()) for f in imp.get("criticalFlags", [])
    ) or "none"
    print(
        f"{fixture.name}: workflow={wf} triage={t['priorityTier']} "
        f"tools={tools} critical={flags} verification={ver['verificationStatus']} "
        f"comms={dispatch['dispatchStatus']} channels={channels}"
    )
    # A delivery that did not happen is a failed hop, even though comms.dispatch is CORRECT to
    # report it instead of raising (a raise would retry the activity and double-page the human).
    # The skeleton is the only place left that can turn that status back into a red run.
    if failed_channels:
        raise AssertionError(
            f"channel delivery failed: {', '.join(failed_channels)}"
        )
    return {
        "scenario": scenario,
        "workflowId": wf,
        "reportRef": report_event["diagnosticReportId"],
        "triageTier": t["priorityTier"],
        "tools": tuple(tool["toolId"] for tool in a["toolsSelected"]),
        "criticalFlags": tuple(sorted(f["label"] for f in imp.get("criticalFlags", []))),
        "verificationStatus": ver["verificationStatus"],
        "dispatchStatus": dispatch["dispatchStatus"],
        "channels": {r["channel"]: r.get("status") for r in channel_results},
    }


async def main() -> int:
    fixture_dir = ROOT / "mocks" / "fixtures"
    if len(sys.argv) > 2:
        print(f"Usage: python {Path(__file__).as_posix()} [fixture.json]", file=sys.stderr)
        return 2

    fixtures = [Path(sys.argv[1])] if len(sys.argv) == 2 else sorted(
        fixture_dir.glob("studycontext.*.json")
    )
    if not fixtures:
        print(f"No StudyContext fixtures found in {fixture_dir}", file=sys.stderr)
        return 1

    triage = load_handler("worklist-triage")
    ehr = load_handler("ehr-assistant")
    interp = load_handler("interpretation-assistant")
    demo_fhir = _DemoFhir()
    impression = load_handler("impression-generation")
    impression.__globals__["_FHIR"] = demo_fhir  # inject the fhir2 the handler fetches from (#16)
    verify = load_handler("report-verification")
    verify.__globals__["_FHIR"] = demo_fhir  # verify parses report.body from the same conclusion (#22)
    comms = load_handler("communications")
    comms.__globals__["_FHIR"] = demo_fhir
    comms.__globals__["_LEDGER"] = _DemoLedger()
    handlers = triage, ehr, interp, impression, verify, comms

    failures = 0
    for fixture in fixtures:
        try:
            await run_fixture(fixture, handlers, demo_fhir)
        except Exception as exc:
            failures += 1
            print(f"{fixture}: FAILED: {exc}", file=sys.stderr)

    if failures:
        print(f"\nValidation failed for {failures} of {len(fixtures)} fixture(s).", file=sys.stderr)
        return 1

    print(f"\nAll hops validated against /contracts. ✅ {len(fixtures)} fixture(s) checked.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
