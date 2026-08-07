"""Workflow state enum, the derived WorkflowState store, and agent endpoint config."""
from __future__ import annotations
import os
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class State(str, Enum):
    RECEIVED = "RECEIVED"
    READY_FOR_READ = "READY_FOR_READ"
    AWAITING_RADIOLOGIST = "AWAITING_RADIOLOGIST"
    IMPRESSION = "IMPRESSION"
    VERIFY = "VERIFY"
    AWAITING_SIGNOFF = "AWAITING_SIGNOFF"
    COMMUNICATE = "COMMUNICATE"
    ARCHIVED = "ARCHIVED"


# Task queue + activity names (strings keep the workflow sandbox free of I/O imports).
TASK_QUEUE = "lhrad-study-tq"
ACT_CALL_AGENT = "call_agent_skill_activity"
ACT_START_AGENT = "start_agent_skill_activity"
ACT_PUBLISH_PRIORITY = "publish_priority_activity"
ACT_PUBLISH_FINDINGS = "publish_findings_activity"
ACT_PUBLISH_STATE = "publish_state_activity"
ACT_ESCALATE = "escalate_activity"
ACT_LOAD_ESCALATION_POLICY = "load_escalation_policy_activity"
ACT_WRITE_PRESIGN_IMPRESSION = "write_presign_impression_activity"
ACT_RECORD_POLICY_FAILURE = "record_policy_failure_activity"
ACT_RECORD_SIGNOFF_ABANDONED = "record_signoff_abandoned_activity"


def callback_base_url() -> str:
    """Where agents POST push-notification callbacks (#24): this ingress, reachable from the
    agents' network. Env override -> docker-compose default."""
    return os.environ.get("INGRESS_CALLBACK_BASE_URL", "http://ingress:8090").rstrip("/")


def worklist_api_base_url() -> str:
    """Where the orchestrator publishes triage priority (issue #20 + follow-up).
    Env override -> docker-compose default. Consumed by publish_priority_activity."""
    return os.environ.get("WORKLIST_API_URL", "http://worklist-api:8107").rstrip("/")


def agent_base_url(agent: str) -> str:
    """Resolve an agent base URL (env override -> docker-compose default)."""
    defaults = {
        "worklist-triage": "http://worklist-triage:8101/",
        "ehr-assistant": "http://ehr-assistant:8102/",
        "interpretation-assistant": "http://interpretation-assistant:8103/",
        "impression-generation": "http://impression-generation:8104/",
        "report-verification": "http://report-verification:8105/",
        "communications": "http://communications:8106/",
    }
    env_key = "AGENT_URL_" + agent.upper().replace("-", "_")
    return os.environ.get(env_key, defaults[agent])


class WorkflowState(BaseModel):
    """Derived results accumulated by the orchestrator (lean-reference + pass-forward).

    Agents never refetch each other's outputs from source; the slice they need is passed
    into their skill input from here.
    """
    workflowId: str
    state: State = State.RECEIVED
    studyContext: dict[str, Any]
    triage: Optional[dict] = None
    ehrContext: Optional[dict] = None
    aiFindings: Optional[dict] = None
    report: Optional[dict] = None
    impression: Optional[dict] = None
    verification: Optional[dict] = None
    history: list[str] = Field(default_factory=list)
