"""#57: the sign-off override endpoint, driven over a REAL HTTP transport against a REAL running
workflow -- the integration tier the other override tests do not reach.

test_signoff_override.py already covers the endpoint's LOGIC thoroughly, but it does so by calling
`ingress.signoff_override(...)` as a plain coroutine with a mocked Temporal client: it never crosses
the ASGI boundary and never releases an actual workflow. test_signoff_override_deploy.py drives a real
workflow, but signals it directly through the Temporal client, not through the endpoint. Neither
exercises the seam a real deployment runs through:

  radiologist -> HTTP POST /signoff/{workflowId}/override -> FastAPI routing + `X-Signoff-Token`
  Header parsing -> auth -> `_temporal().signal(...)` -> the parked StudyWorkflow releases -> ARCHIVED

This test closes that gap. It stands up the Temporal test server, runs the real StudyWorkflow + a
worker (with the agent calls mocked so report.verify FAILs and the sign-off gate opens), and reaches
the real orchestrator ingress app over httpx's ASGI transport -- real routing, real Header extraction,
the real handler, the real signal path. Then it asserts the study actually archives via the override.

What it deliberately does NOT re-assert (owned by test_signoff_override.py at the handler level, no
transport needed): the full 422/503/502 matrix and the non-ASCII-token crash guard. Here we keep just
one rejected call and one accepted call, to prove the ASGI seam itself carries auth through to a
signal -- the rest would be duplication.

Skipped unless temporalio + the a2a extra are installed (the ris-poller CI lane has both, and runs
`orchestrator/tests`, so this file runs there).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")
pytest.importorskip("a2a", reason="a2a extra (a2a-sdk) not installed")

from httpx import ASGITransport  # noqa: E402
from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402
import orchestrator.ingress as ingress  # noqa: E402

_TOKEN = "e2e-secret"

CTX = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf-http-e2e",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {},
    "meta": {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "emittedAt": "2026-06-26T00:00:00Z",
        "source": "test",
    },
}


class _Spy:
    def __init__(self):
        self.verifies = 0
        self.dispatches = 0
        self.dead_letters: list = []


def _activities(spy: _Spy):
    @activity.defn(name="call_agent_skill_activity")
    async def call_agent_skill_activity(agent, skill_id, payload):
        if skill_id == "report.verify":
            # Nobody edited the report, so verification can only re-derive the same FAIL: the
            # premise of #56. That FAIL + requiresHumanReview is what opens the sign-off gate.
            spy.verifies += 1
            return {"verificationStatus": "FAIL", "requiresHumanReview": True,
                    "issues": [{"ruleId": "sections-missing", "severity": "FAIL",
                                "message": "NEVER-LEAVES-THE-WORKFLOW: quotes report text",
                                "location": "body"}]}
        if skill_id == "triage.score":
            return {"priorityTier": "ROUTINE", "priorityScore": 50}
        if skill_id == "comms.dispatch":
            spy.dispatches += 1
            return {"dispatchStatus": "SENT", "channelResults": []}
        return {"ok": True}

    @activity.defn(name="publish_priority_activity")
    async def publish_priority_activity(workflow_id, study_instance_uid, triage):
        return None

    @activity.defn(name="publish_findings_activity")
    async def publish_findings_activity(workflow_id, study_instance_uid, ai_result):
        return None

    @activity.defn(name="publish_state_activity")
    async def publish_state_activity(workflow_id, study_instance_uid, read_state, changed_at):
        return None

    @activity.defn(name="escalate_activity")
    async def escalate_activity(workflow_id, reason, escalation=None):
        return None

    @activity.defn(name="load_escalation_policy_activity")
    async def load_escalation_policy_activity(tier):
        # A single rung so far out it never fires within the test: the study parks at the gate
        # awaiting the ack, so the HTTP override -- not a timer -- is what releases it. Keeps the
        # run deterministic and isolates what this test is about (the endpoint), not the ladder.
        return [{"level": 1, "afterMinutes": 100000, "targetRole": "reading-radiologist",
                 "channels": ["in-app"], "urgency": "routine"}]

    @activity.defn(name="record_signoff_abandoned_activity")
    async def record_signoff_abandoned_activity(workflow_id, tier, pages, *a, **k):
        spy.dead_letters.append(workflow_id)
        return None

    @activity.defn(name="record_policy_failure_activity")
    async def record_policy_failure_activity(workflow_id, tier, *a, **k):
        return None

    return [
        call_agent_skill_activity,
        publish_priority_activity,
        publish_findings_activity,
        publish_state_activity,
        escalate_activity,
        load_escalation_policy_activity,
        record_signoff_abandoned_activity,
        record_policy_failure_activity,
    ]


async def _drive() -> dict:
    spy = _Spy()
    saved_client, saved_token = ingress._client, ingress.SIGNOFF_OVERRIDE_TOKEN
    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            # Point the REAL ingress endpoint's Temporal client at this same test server, and give
            # it a token to check against -- both are what the endpoint reads at call time.
            ingress._client = env.client
            ingress.SIGNOFF_OVERRIDE_TOKEN = _TOKEN

            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=_activities(spy), max_cached_workflows=0):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, CTX, id=CTX["workflowId"], task_queue=TASK_QUEUE)
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})

                for _ in range(600):
                    if await handle.query(StudyWorkflow.current_state) == "AWAITING_SIGNOFF":
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError("workflow never reached AWAITING_SIGNOFF")

                url = f"/signoff/{CTX['workflowId']}/override"
                good = {"acknowledgedBy": "Practitioner/dr-rao",
                        "reason": "reviewed with referrer; critical already phoned through"}
                out: dict = {}
                out["signoff_context"] = await handle.query(StudyWorkflow.signoff_context)
                transport = ASGITransport(app=ingress.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                    # The confirm page, against the REALLY parked workflow (the fake-client tier
                    # in test_signoff_override_page.py owns the page's branch matrix).
                    page = await c.get(url)
                    out["page_status"], out["page_body"] = page.status_code, page.text

                    # A rejected call must not release the gate (auth carried over the ASGI seam).
                    rej = await c.post(url, headers={"X-Signoff-Token": "wrong"}, json=good)
                    out["rejected_status"] = rej.status_code
                    out["state_after_reject"] = await handle.query(StudyWorkflow.current_state)

                    # The real thing: a valid override over the wire releases the study.
                    ok = await c.post(url, headers={"X-Signoff-Token": _TOKEN}, json=good)
                    out["accepted_status"] = ok.status_code
                    out["accepted_body"] = ok.json()

                verifies_before = spy.verifies
                result = await asyncio.wait_for(handle.result(), timeout=30)

        out["result"] = result
        out["dispatches"] = spy.dispatches
        out["dead_letters"] = list(spy.dead_letters)
        out["reverified_after_ack"] = spy.verifies - verifies_before
        return out
    finally:
        ingress._client, ingress.SIGNOFF_OVERRIDE_TOKEN = saved_client, saved_token


@pytest.fixture(scope="module")
def driven():
    """One deploy: stand up Temporal + a worker, park a study at the gate, and drive the real HTTP
    override endpoint. Shared by every assertion below."""
    return asyncio.run(_drive())


def test_signoff_context_query_reports_the_held_verdict_without_issue_text(driven):
    sc = driven["signoff_context"]
    assert sc["state"] == "AWAITING_SIGNOFF"
    assert sc["verificationStatus"] == "FAIL"
    assert sc["requiresHumanReview"] is True
    # Rule id + severity ONLY: message/location may quote report text and must be stripped
    # AT THE QUERY, not merely ignored by the page that happens to render it today.
    assert sc["issues"] == [{"ruleId": "sections-missing", "severity": "FAIL"}]


def test_the_confirm_page_renders_for_a_really_parked_workflow(driven):
    assert driven["page_status"] == 200
    assert "<form" in driven["page_body"]
    assert "FAIL" in driven["page_body"] and "sections-missing" in driven["page_body"]
    assert "Release the gate" in driven["page_body"]
    assert "NEVER-LEAVES-THE-WORKFLOW" not in driven["page_body"]


def test_a_wrong_token_over_the_wire_is_rejected_and_leaves_the_gate_held(driven):
    assert driven["rejected_status"] == 401
    assert driven["state_after_reject"] == "AWAITING_SIGNOFF", (
        "a rejected override must not release the gate"
    )


def test_a_valid_override_over_the_wire_is_accepted(driven):
    assert driven["accepted_status"] == 202
    assert driven["accepted_body"]["acknowledged"] is True
    assert driven["accepted_body"]["acknowledgedBy"] == "Practitioner/dr-rao"


def test_the_http_override_actually_releases_the_workflow_to_archived(driven):
    """The whole point: the endpoint's signal reaches the parked workflow and it completes -- the
    study archives, carrying its verification FAIL to COMMUNICATE rather than re-deriving it."""
    result = driven["result"]
    assert result["finalState"] == "ARCHIVED"
    assert result["verification"]["verificationStatus"] == "FAIL"
    assert driven["dispatches"] == 1, "the study must reach COMMUNICATE and dispatch its finding"
    assert driven["reverified_after_ack"] == 0, "the unchanged report must not be re-verified"
    assert driven["dead_letters"] == [], "an acknowledged gate must not be dead-lettered as abandoned"


def test_the_acknowledgement_is_on_the_record(driven):
    signoff = driven["result"]["signoff"]
    assert signoff["status"] == "ACKNOWLEDGED"
    assert signoff["acknowledgedBy"] == "Practitioner/dr-rao"
    assert "phoned through" in signoff["reason"]
    assert signoff["acknowledgedAt"]
