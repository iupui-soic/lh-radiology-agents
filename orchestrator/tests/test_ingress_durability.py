"""Ingress durability across restart (issue #6, per Saptarshi's scope on the MR).

The radiologist human-gate lasts hours-to-days; if the ingress restarts in that window the
sign-off must NOT be lost. These tests prove the durable store (orchestrator/ingress_store.py)
closes the gap that Temporal-side restart tests alone can't see:

  * the report->workflow index + poll cursor survive a fresh process (store round-trip);
  * INTEGRATION: start a workflow -> "restart" the ingress (new store, same DB) -> finalize the
    report -> the durably-waiting gate still releases (workflow reaches ARCHIVED);
  * NEGATIVE CONTROL: a wiped (in-memory-only) index strands the report -> proves durability is
    what saves it.

Skipped when the orchestrator's deps (temporalio) aren't installed.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = str(Path(__file__).resolve().parents[2])

pytest.importorskip("temporalio", reason="temporalio not installed")
from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

ingress = pytest.importorskip("orchestrator.ingress", reason="orchestrator deps not installed")
from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402


# --- mock activities: drive the workflow to ARCHIVED with no I/O ------------------
@activity.defn(name="call_agent_skill_activity")
async def mock_call_agent(agent: str, skill_id: str, payload: dict) -> dict:
    if skill_id == "report.verify":  # PASS + no human review -> straight to COMMUNICATE
        return {"verificationStatus": "PASS", "requiresHumanReview": False, "issues": []}
    if skill_id == "triage.score":
        return {"priorityTier": "ROUTINE", "priorityScore": 50}
    return {"ok": True}


@activity.defn(name="publish_priority_activity")
async def mock_publish(workflow_id: str, study_instance_uid: str, triage: dict) -> None:
    return None

@activity.defn(name="publish_findings_activity")
async def mock_publish_findings(workflow_id: str, study_instance_uid: str, ai_result: dict) -> None:
    """Mock for #74 publish_findings_activity — never-raises like the production version."""
    return None


@activity.defn(name="publish_state_activity")
async def mock_publish_state(
    workflow_id: str, study_instance_uid: str, read_state: str, changed_at: str,
) -> None:
    """Mock for #108 publish_state_activity -- never-raises like the production version."""
    return None


@activity.defn(name="escalate_activity")
async def mock_escalate(workflow_id: str, reason: str) -> None:
    return None


STUDY_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_restart_6",
    "study": {"studyInstanceUID": "1.2.6", "orthancStudyId": "abc", "modality": "CT",
              "accessionNumber": "ACC-6"},
    "patient": {"fhirPatientId": "Patient/1"}, "order": {},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


@pytest.fixture(autouse=True)
def _reset_store():
    yield
    if ingress._STORE is not None:
        try:
            ingress._STORE.close()
        except Exception:  # noqa: BLE001
            pass
        ingress._STORE = None


async def _await_state(handle, target: str, tries: int = 400) -> None:
    for _ in range(tries):
        if await handle.query(StudyWorkflow.current_state) == target:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow never reached {target}")


# --- store round-trip: index + cursor survive a fresh process --------------------
def test_index_and_cursor_survive_a_restart(tmp_path):
    db = str(tmp_path / "ingress.db")
    s1 = ingress.IngressStore(db)
    s1.put_index("ACC-1", "wf_x")
    s1.put_index("ServiceRequest/sr-1", "wf_x")
    s1.save_cursor("2026-06-27T12:00:00Z", {"DiagnosticReport/r1@2026-06-27T12:00:00Z"})
    s1.close()

    s2 = ingress.IngressStore(db)  # brand-new process, same file
    assert s2.workflow_id_for("ACC-1") == "wf_x"
    # Version-keyed entries (#66) round-trip untouched; only legacy bare ids are migrated.
    assert s2.load_cursor() == (
        "2026-06-27T12:00:00Z", {"DiagnosticReport/r1@2026-06-27T12:00:00Z"})
    assert s2.index_size() == 2
    s2.evict_workflow("wf_x")  # both of the workflow's keys go, bounding growth
    assert s2.workflow_id_for("ACC-1") is None
    assert s2.index_size() == 0
    s2.close()


# --- real crash: writer process dies WITHOUT close(), state still recovers --------
def test_store_survives_a_real_process_crash(tmp_path):
    db = str(tmp_path / "crash.db")
    writer = (
        "import sys, os; sys.path.insert(0, %r);"
        "from orchestrator.ingress_store import IngressStore;"
        "s = IngressStore(%r);"
        "s.put_index('ACC-CRASH', 'wf_crash');"
        "s.save_cursor('2026-06-27T09:00:00Z', {'DiagnosticReport/rc'});"
        "os._exit(0)"  # hard exit: no close(), no atexit, no __del__ — a real crash
    ) % (REPO_ROOT, db)
    subprocess.run([sys.executable, "-c", writer], check=True)

    s = ingress.IngressStore(db)  # a fresh, separate process reopens the same file
    assert s.workflow_id_for("ACC-CRASH") == "wf_crash"
    # The crash-writer used the PRE-#66 bare-id format, so this doubles as the migration pin:
    # a legacy entry comes back as id@cursor (see IngressStore.load_cursor's upgrade shim).
    assert s.load_cursor() == (
        "2026-06-27T09:00:00Z", {"DiagnosticReport/rc@2026-06-27T09:00:00Z"})
    s.close()


# --- upgrade shim: pre-#66 bare-id dedup state is version-keyed on load -----------
def test_a_pre_66_store_is_migrated_to_version_keys_on_load(tmp_path):
    """Bare ids (pre-version-key writers) become id@cursor on load; already-versioned keys are
    untouched; and one save/load cycle makes the migration durable. Without this, the first
    post-upgrade poll re-signals the boundary report -- reproduced as a forever 'matched no
    waiting workflow' warning loop on quiet systems and a spurious signoff-drop dead letter."""
    db = str(tmp_path / "legacy.db")
    s1 = ingress.IngressStore(db)
    s1.save_cursor("2026-06-27T10:00:00Z",
                   {"DiagnosticReport/old", "DiagnosticReport/new@2026-06-27T10:00:00Z"})
    s1.close()

    s2 = ingress.IngressStore(db)
    cursor, ids = s2.load_cursor()
    assert ids == {"DiagnosticReport/old@2026-06-27T10:00:00Z",
                   "DiagnosticReport/new@2026-06-27T10:00:00Z"}
    s2.save_cursor(cursor, ids)                     # the next poll persists the migrated form
    assert s2.load_cursor() == (cursor, ids)
    s2.close()


# --- INTEGRATION: workflow survives ingress restart, gate still releases ----------
def test_gate_releases_after_ingress_restart(tmp_path):
    db = str(tmp_path / "ingress.db")

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[mock_call_agent, mock_publish, mock_publish_findings,
                                          mock_publish_state, mock_escalate]):
                # ingress process #1: index the study, start its workflow, let it park at the gate.
                ingress._STORE = ingress.IngressStore(db)
                ingress._index_workflow(STUDY_CONTEXT)  # persists ACC-6 -> wf_restart_6
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT,
                    id=STUDY_CONTEXT["workflowId"], task_queue=TASK_QUEUE,
                )
                await _await_state(handle, "AWAITING_RADIOLOGIST")
                ingress._STORE.close()  # <-- ingress crashes / redeploys mid-wait

                # ingress process #2 (RESTART): in-memory index is gone; only the DB remains.
                ingress._STORE = ingress.IngressStore(db)
                report = {"diagnosticReportId": "DiagnosticReport/r1",
                          "accessionNumber": STUDY_CONTEXT["study"]["accessionNumber"]}
                # the mapping survived the restart — the whole point:
                assert ingress._workflow_id_for_report(report) == STUDY_CONTEXT["workflowId"]
                newly, _failed = await ingress._process_batch(env.client, [report], set())
                # dedup keys are id@lastUpdatedCursor (#66); this record carries no stamp
                assert newly == {"DiagnosticReport/r1@"}
                result = await handle.result()  # gate released -> runs to ARCHIVED
                # once COMPLETED, reconciliation reclaims the study's index row (evict on completion)
                assert await ingress._reconcile_index(env.client) == 1
                assert ingress._STORE.index_size() == 0
        assert result["finalState"] == "ARCHIVED"  # gate released after the restart

    asyncio.run(scenario())


# --- eviction on COMPLETION: reconcile prunes closed workflows, keeps running ones -
def test_reconcile_evicts_completed_keeps_running(tmp_path):
    """#6 (Saptarshi: 'evict on workflow completion'): a finished workflow's index row is reclaimed
    by reconciliation; a still-waiting workflow's row is kept. Bounds growth for studies that never
    deliver a report (cancelled / QC-rejected / terminated)."""
    db = str(tmp_path / "ingress.db")

    def _ctx(wf, acc):
        return {**STUDY_CONTEXT, "workflowId": wf,
                "study": {**STUDY_CONTEXT["study"], "accessionNumber": acc}}

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[mock_call_agent, mock_publish, mock_publish_findings,
                                          mock_publish_state, mock_escalate]):
                ingress._STORE = ingress.IngressStore(db)

                # workflow that runs to completion (ARCHIVED). Reconciliation is the only eviction
                # path, so its row is reclaimed by the sweep below, not at delivery time.
                done = _ctx("wf_done", "ACC-DONE")
                ingress._index_workflow(done)
                h1 = await env.client.start_workflow(
                    StudyWorkflow.run, done, id="wf_done", task_queue=TASK_QUEUE)
                await _await_state(h1, "AWAITING_RADIOLOGIST")
                await h1.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DR/done"})
                await h1.result()  # -> ARCHIVED (closed)

                # workflow still parked at the gate (running)
                run = _ctx("wf_run", "ACC-RUN")
                ingress._index_workflow(run)
                h2 = await env.client.start_workflow(
                    StudyWorkflow.run, run, id="wf_run", task_queue=TASK_QUEUE)
                await _await_state(h2, "AWAITING_RADIOLOGIST")

                assert ingress._STORE.index_size() == 2
                pruned = await ingress._reconcile_index(env.client)
                assert pruned == 1  # only the completed workflow's row
                assert ingress._workflow_id_for_report({"accessionNumber": "ACC-DONE"}) is None
                assert ingress._workflow_id_for_report({"accessionNumber": "ACC-RUN"}) == "wf_run"

    asyncio.run(scenario())


# --- NEGATIVE CONTROL: without durability the report is stranded ------------------
def test_wiped_index_would_strand_the_report(tmp_path):
    async def scenario():
        # a fresh, empty store == an in-memory-only ingress after restart: it never saw the start.
        ingress._STORE = ingress.IngressStore(str(tmp_path / "empty.db"))
        report = {"diagnosticReportId": "DiagnosticReport/r1", "accessionNumber": "ACC-6"}
        assert ingress._workflow_id_for_report(report) is None  # nothing to signal
        newly, failed = await ingress._process_batch(None, [report], set())  # client never used on a miss
        assert newly == set()   # dropped -> the workflow would wait forever
        assert failed == []     # a miss is a drop, not a retry: the cursor must not stall on it

    asyncio.run(scenario())
