"""#139 step 2: adding the post-ack follow-up check must not break studies ALREADY archived.

The check inserts an activity (and possibly a child-workflow start) after the ack loop, where an
archived study's history ends. Closed histories are replayed (a query, a reset), so without the
patch marker every acknowledged critical study archived before this deploy would raise
NondeterminismError on its next query instead of answering.

The fixture is a REAL history recorded at the parent commit (origin/main 8193bed, before this
change) with test_ack_loop's own fakes: a CRITICAL result the ordering physician acknowledged, the
exact study the new block acts on. Synthetic ids only. Re-record (only if that shape ever
legitimately changes) by running test_ack_loop._run({"critical": True,
"acked_after_escalations": 0}, ..., want_history=True) at the parent commit.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio.client import WorkflowHistory  # noqa: E402
from temporalio.worker import Replayer  # noqa: E402

from orchestrator.tests.test_ack_replay import _decoded_payloads  # noqa: E402
from orchestrator.workflow import FollowUpCheckWorkflow, StudyWorkflow  # noqa: E402

_PRE_CHECK_HISTORY = Path(__file__).parent / "fixtures" / "followup_precheck_history.json"


def test_the_fixture_really_predates_the_check_and_would_reach_it():
    """Guard the guard: the replay below proves something only if the fixture carries no marker,
    really is CLOSED, and is a study whose ack loop ended COMPLETED (the block's only trigger).
    Verified by re-introducing the bug: without `patched()`, the replay below fails."""
    raw = _PRE_CHECK_HISTORY.read_text()
    assert "post-ack-followup-check-v1" not in raw
    history = json.loads(raw)
    assert history["events"][-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"
    payloads = _decoded_payloads(history)
    assert '"ackStatus":"COMPLETED"' in payloads.replace(" ", ""), "no acknowledged ack loop"


def test_an_archived_acknowledged_study_still_replays_after_the_check_ships():
    history = WorkflowHistory.from_json("wf-pre-followup", json.loads(_PRE_CHECK_HISTORY.read_text()))
    asyncio.run(Replayer(workflows=[StudyWorkflow, FollowUpCheckWorkflow]).replay_workflow(history))
