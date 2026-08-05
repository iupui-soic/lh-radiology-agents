"""Report Verification handler — engine owner: Pranathi; rules owner: Saptarshi.

Input  : { studyContext, report?, impression?, ehrContext?, aiFindings? }
Output : contracts/skills/report.schema.json

The finalized `report` is lean (IDs only, no narrative -- Golden rule 2). To let the PI rules fire
on real reports (#22) we fetch the report `conclusion` from fhir2 by its id -- the same one
clinical field Impression Generation is entitled to (#16) -- and parse it into `report.body`
(laterality, sections, BI-RADS, density) before running the rules. Best-effort: if fhir2 is
unreachable the body stays empty and body-dependent rules simply do not fire; the safety-net still
returns a valid result.
"""
from __future__ import annotations
import logging
from pathlib import Path

from radagent_common.fhir_client import Fhir2Client
from radagent_common.tracing import now_iso
from rules.engine import enrich_report_body, load_rules, run_rules

AGENT_VERSION = "0.1.0"
_RULES_DIR = Path(__file__).resolve().parent / "rules"
# Loaded and validated ONCE, at import (#96): server.py imports this module, so a malformed rule
# file refuses to boot the agent with an error naming the file. Before this, run_rules re-read the
# directory and re-exec'd every custom check per report.verify call, and a bad rule raised per
# request instead, which Temporal's unbounded activity retry turned into a quietly parked study.
_RULES = load_rules(_RULES_DIR)
_log = logging.getLogger(__name__)

# Read-only fhir2 client for report-content lookup (#22). Lazily built so importing this module has
# no side effect; the harness/tests override `_FHIR` with a fake.
_FHIR: Fhir2Client | None = None


def _fhir() -> Fhir2Client:
    global _FHIR
    if _FHIR is None:
        _FHIR = Fhir2Client()
    return _FHIR


async def _report_narrative(report: dict) -> str:
    """The report narrative to verify against. An inline `conclusion` (test / pre-sign draft) wins;
    otherwise fetch it from fhir2 by diagnosticReportId. Best-effort: a miss or fhir2 error yields
    "" so the post-sign safety-net still runs (body-dependent rules just stay inert)."""
    inline = report.get("conclusion")
    if isinstance(inline, str) and inline.strip():
        return inline
    report_id = report.get("diagnosticReportId")
    if not report_id:
        return ""
    try:
        return await _fhir().get_report_conclusion(report_id) or ""
    except Exception:  # noqa: BLE001 - fhir2 down must not fail the safety-net
        _log.warning("fhir2 conclusion fetch failed for %s; verifying without report-body rules", report_id)
        return ""


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "report.verify":
        raise ValueError(f"unexpected skill {skill_id}")
    ctx = payload["studyContext"]
    rule_ctx = {
        "report": payload.get("report") or {},
        "impression": payload.get("impression") or {},
        "ehrContext": payload.get("ehrContext") or {},
        "aiFindings": payload.get("aiFindings") or {},
    }
    enrich_report_body(rule_ctx, await _report_narrative(rule_ctx["report"]))
    status, requires_review, issues = run_rules(rule_ctx, _RULES)
    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "verificationStatus": status,
        "requiresHumanReview": requires_review,
        "issues": issues,
        "agentVersion": AGENT_VERSION,
        "verifiedAt": now_iso(),
    }
