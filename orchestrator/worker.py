"""Temporal worker: hosts StudyWorkflow + activities on the study task queue."""
from __future__ import annotations
import asyncio
import logging
import os
from temporalio.client import Client
from temporalio.worker import Worker

from radagent_common.tracing import init_tracing, tracing_enabled
from .state import TASK_QUEUE
from .workflow import StudyWorkflow
from .activities import (
    call_agent_skill_activity,
    start_agent_skill_activity,
    publish_findings_activity,
    publish_priority_activity,
    publish_state_activity,
    write_presign_impression_activity,
    escalate_activity,
    load_escalation_policy_activity,
    record_policy_failure_activity,
    record_signoff_abandoned_activity,
)

TEMPORAL_TARGET = os.environ.get("TEMPORAL_TARGET", "temporal:7233")
_log = logging.getLogger(__name__)


async def _connect_with_retry(target: str, attempts: int = 60, delay: float = 2.0,
                              interceptors: list | None = None) -> Client:
    """Temporal (auto-setup) is usually not ready when the container starts, so retry the
    connect instead of crashing the worker on the first failed attempt."""
    for i in range(1, attempts + 1):
        try:
            return await Client.connect(target, interceptors=interceptors or [])
        except Exception as exc:  # noqa: BLE001 - broad on purpose while Temporal is booting
            if i == attempts:
                raise
            _log.warning("Temporal not ready at %s (attempt %d/%d): %s", target, i, attempts, exc)
            await asyncio.sleep(delay)


# The activity list the study worker serves. A module-level constant, not a literal buried in
# main(), so test_worker_registration.py can assert it is COMPLETE against activities.py rather
# than re-declaring a copy that would drift. An activity defined but missing from this list imports
# fine and fails at RUNTIME, when the workflow dispatches a name the worker never registered (#26).
ACTIVITIES = [
    call_agent_skill_activity,
    start_agent_skill_activity,
    publish_findings_activity,
    publish_priority_activity,
    publish_state_activity,
    write_presign_impression_activity,
    escalate_activity,
    load_escalation_policy_activity,
    record_policy_failure_activity,
    record_signoff_abandoned_activity,
]


def tracing_interceptors() -> list:
    """OpenTelemetry (#28): the interceptor list for the Temporal CLIENT. Empty unless enabled.

    ONE TracingInterceptor, on the client, spans workflow starts, workflow execution, and every
    activity, propagating context through Temporal headers (replay-safe -- no spans are created
    inside workflow.py). Imports are lazy so the [otel] extra stays optional.
    """
    if not tracing_enabled():
        return []
    init_tracing("orchestrator-worker")
    from temporalio.contrib.opentelemetry import TracingInterceptor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()  # A2A + fhir2/Orthanc calls inject the traceparent
    return [TracingInterceptor()]


def build_worker(client: Client) -> Worker:
    """The study worker. Takes NO interceptors, on purpose.

    TracingInterceptor subclasses both the client and the worker Interceptor bases, and Worker
    prepends the ones already on the client to its own list without de-duping ("should not be
    explicitly given here" -- temporalio). Handing it to both wraps every workflow and activity
    twice, exporting each as two self-nested spans. The client's interceptor already covers the
    worker side; test_worker_tracing.py pins this.
    """
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[StudyWorkflow],
        activities=ACTIVITIES,
    )


async def main() -> None:
    client = await _connect_with_retry(TEMPORAL_TARGET, interceptors=tracing_interceptors())
    await build_worker(client).run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
