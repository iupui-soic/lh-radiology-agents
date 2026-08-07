"""Orchestrator end-to-end on Temporal against mock A2A agents (#9).

Drives StudyWorkflow start -> ARCHIVED on a real Temporal server, with every agent served as
a real A2A server reached over the real a2a-sdk client (radagent_common.client). This is the
integration proof #9 asks for — NOT the in-process handler harness (mocks/run_walking_skeleton.py).

The three acceptance checks of #9:
  1. Worker + workflow complete the full state machine (finalState == ARCHIVED).
  2. the `report_finalized` signal releases the AWAITING_RADIOLOGIST gate.
  3. the `current_state` query reflects transitions.

Temporal flavor: we use WorkflowEnvironment.start_time_skipping() (the official Temporal *test
server*), not start_local() (the dev server). Both are real Temporal servers the Worker talks to
over gRPC — exercising the real client/worker/activity/signal/query path — but the test server
starts fast and reliably in constrained CI/dev, whereas the dev server's fixed 5s startup
health-check flakes under load. Time-skipping is inert on the happy path here (report.verify
PASSes, so no sign-off timer fires); the gate wait is on a signal condition, not a timer.

Agents: the five contract agents are served by the project mock (mocks/mock_agent.py, #19); the
Communications agent's mock lives in this test (comms.dispatch), so #19's reusable mock + CI
wiring stay Chaitra's to complete. Skipped unless the a2a extra + temporalio are installed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("a2a", reason="a2a extra (a2a-sdk) not installed")
pytest.importorskip("temporalio", reason="temporalio not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")  # skips cleanly in the poller lane

import uvicorn  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from radagent_common.a2a import build_agent_app  # noqa: E402
from radagent_common.tracing import now_iso  # noqa: E402
from orchestrator.state import TASK_QUEUE, State  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402
from orchestrator.activities import (  # noqa: E402
    call_agent_skill_activity,
    publish_findings_activity,
    publish_priority_activity,
    publish_state_activity,
    escalate_activity,
    load_escalation_policy_activity,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_mock_handle():
    """Import mocks/mock_agent.py by path (it lives outside any package)."""
    spec = importlib.util.spec_from_file_location("mock_agent", REPO_ROOT / "mocks" / "mock_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.handle


async def _comms_handle(skill_id: str, payload: dict) -> dict:
    """Communications mock (comms.dispatch) — kept here, not in mocks/mock_agent.py, so #19
    stays Chaitra's to finish. Output must satisfy contracts/skills/comms.dispatch.schema.json."""
    wf = payload.get("studyContext", {}).get("workflowId", "wf_mock")
    return {
        "schemaVersion": "1.0.0", "workflowId": wf, "dispatchStatus": "SENT",
        "channelResults": [{"channel": "mock", "status": "SENT"}],
        "agentVersion": "mock", "dispatchedAt": now_iso(),
    }


# agent-dir -> handler (None => the shared 5-skill project mock).
_AGENTS: dict[str, object] = {
    "worklist-triage": None,
    "ehr-assistant": None,
    "interpretation-assistant": None,
    "impression-generation": None,
    "report-verification": None,
    "communications": _comms_handle,
}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(agent_dir: str, handler):
    """Boot one mock A2A server on a loopback port; return (server, thread, base_url)."""
    port = _free_port()
    app = build_agent_app(agent_dir, handler).build()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/.well-known/agent-card.json", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError(f"mock agent {agent_dir!r} did not become ready in time")
    return server, thread, base


@pytest.fixture()
def mock_agent_fleet(monkeypatch):
    """Boot all six agents on loopback ports and point AGENT_URL_* at them for the test."""
    mock_handle = _load_mock_handle()
    running = []
    try:
        for agent_dir, handler in _AGENTS.items():
            server, thread, base = _serve(agent_dir, handler or mock_handle)
            running.append((server, thread))
            monkeypatch.setenv("AGENT_URL_" + agent_dir.upper().replace("-", "_"), base)
        yield
    finally:
        for server, thread in running:
            server.should_exit = True
            thread.join(timeout=5)


STUDY_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_e2e_9",
    "study": {"studyInstanceUID": "1.2.9", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"}, "order": {},
    "meta": {"traceId": "t9", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


def test_orchestrator_end_to_end_on_temporal(mock_agent_fleet):
    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                activities=[call_agent_skill_activity, publish_priority_activity, publish_findings_activity,
                            publish_state_activity, escalate_activity, load_escalation_policy_activity],
            ):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-e2e-9", task_queue=TASK_QUEUE,
                )

                # (2)+(3): the query observes the workflow park in the human gate before we release it.
                gate_seen = False
                for _ in range(300):
                    if await handle.query(StudyWorkflow.current_state) == State.AWAITING_RADIOLOGIST.value:
                        gate_seen = True
                        break
                    await asyncio.sleep(0.02)
                assert gate_seen, "workflow never reached AWAITING_RADIOLOGIST (gate)"

                # (2): the RIS signal releases the gate.
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/9"})
                result = await handle.result()

        # (1): the whole state machine ran to completion over real A2A transport.
        assert result["finalState"] == State.ARCHIVED.value
        assert result["workflowId"] == "wf_e2e_9"
        # COMMUNICATE actually round-tripped the Communications mock (start -> ARCHIVED reached).
        assert result["comms"]["dispatchStatus"] == "SENT"
        # triage came back over the wire from the mock worklist-triage agent.
        assert result["triage"]["priorityTier"] == "ROUTINE"

    asyncio.run(scenario())
