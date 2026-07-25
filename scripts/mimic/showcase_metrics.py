"""Showcase metrics over captured workflow results (#76).

The evaluation half of the MIMIC-CXR showcase: read the per-study workflow-result payloads a
session produces and roll them into the numbers #76 asks for -- verification hit rate, sign-off
gate / override behaviour, triage tier spread, and critical-result acknowledgement outcomes.

Input is one JSON file per study, each the dict `StudyWorkflow.run` returns (see
`orchestrator/workflow.py`): workflowId, finalState, triage, verification, comms, ack, signoff.
Capture them live with `temporal workflow show -o json <wf>` and take the result payload from the
`WorkflowExecutionCompleted` event, or point this at a directory of already-extracted result JSONs.

What this tool computes is exactly what the *result* payload carries and no more. Three metrics #76
also names need a richer capture than the result alone and are deliberately NOT invented here:

  - AI-vs-label concordance   -- needs the interpretation findings (a pass-forward derived result,
                                 not in the final payload) joined to the cohort manifest labels by
                                 accession. The result has neither the findings nor the accession
                                 (its key is workflowId = wf_<orthancStudyId>), so the join key must
                                 come from the captured StudyContext.
  - draft-vs-signed agreement -- needs the pre-sign impression draft text and the final report body,
                                 both read from fhir2/the RIS, not the workflow result.
  - time-in-state             -- needs event timestamps from the workflow HISTORY; the result is
                                 timing-free by construction (the workflow has no wall-clock).

Those are flagged in the output so a partial capture never reads as a complete evaluation. Run
`python showcase_metrics.py <dir-or-files...>` for a table, add `--json` for the machine summary
(that JSON is the shape a notebook would load).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, Iterable


# --- loading -----------------------------------------------------------------

def _iter_result_paths(paths: Iterable[str]) -> list[str]:
    """Expand a mix of files and directories into a sorted list of *.json result files."""
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            out.extend(
                os.path.join(p, f) for f in os.listdir(p) if f.endswith(".json")
            )
        else:
            out.append(p)
    return sorted(set(out))


def load_results(paths: Iterable[str]) -> list[dict]:
    """Load result payloads, skipping files that do not parse as a workflow result.

    A workflow result is recognised by a top-level workflowId; anything else (a manifest, a stray
    config) is skipped with a warning rather than corrupting the counts.
    """
    results: list[dict] = []
    for path in _iter_result_paths(paths):
        try:
            with open(path) as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"skip {path}: {e}", file=sys.stderr)
            continue
        if isinstance(doc, dict) and "workflowId" in doc:
            results.append(doc)
        else:
            print(f"skip {path}: not a workflow result (no workflowId)", file=sys.stderr)
    return results


# --- metrics -----------------------------------------------------------------

def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def summarize(results: list[dict]) -> dict[str, Any]:
    """Roll a list of workflow-result payloads into a structured summary.

    Every field is derived from the result shape in orchestrator/workflow.py:run(). Missing
    sub-objects degrade to "not present" rather than raising, so a partial capture still summarises.
    """
    n = len(results)
    final_states = Counter(r.get("finalState", "UNKNOWN") for r in results)

    # Triage -- one triage block per study (may be absent if the study never reached triage).
    tiers = Counter()
    scores: list[float] = []
    triaged = 0
    for r in results:
        t = r.get("triage") or {}
        if not t:
            continue
        triaged += 1
        tiers[t.get("priorityTier", "UNKNOWN")] += 1
        score = t.get("priorityScore")
        if isinstance(score, (int, float)):
            scores.append(float(score))

    # Verification -- the safety-net verdict. hitRate = share that did NOT pass clean.
    verif_status = Counter()
    requires_review = 0
    verified = 0
    rule_hits = Counter()
    severity_hits = Counter()
    for r in results:
        v = r.get("verification") or {}
        if not v:
            continue
        verified += 1
        verif_status[v.get("verificationStatus", "UNKNOWN")] += 1
        if v.get("requiresHumanReview"):
            requires_review += 1
        for issue in v.get("issues") or []:
            rule_hits[issue.get("ruleId", "unknown")] += 1
            severity_hits[issue.get("severity", "UNKNOWN")] += 1
    non_pass = verified - verif_status.get("PASS", 0)

    # Sign-off gate -- a non-empty signoff records how a gate ENDED: ACKNOWLEDGED (an
    # authenticated #57 override released it) or ABANDONED (the ladder ran out). NOTE the
    # result payload cannot see every opened gate: a gate released by a signed addendum whose
    # re-verify passed leaves signoff empty (#66; the addendum audit field is a noted
    # follow-up), indistinguishable from a gate that never opened -- so this is a count of
    # RECORDED releases, not of gate openings, and the gap is named in requiresRicherCapture.
    gate_recorded = 0
    gate_status = Counter()
    overrides = 0
    for r in results:
        s = r.get("signoff") or {}
        if not s:
            continue
        gate_recorded += 1
        gate_status[s.get("status", "UNKNOWN")] += 1
        # The override signal stamps who released it and why (ingress /signoff/{id}/override).
        if s.get("acknowledgedBy"):
            overrides += 1

    # Critical-result ack loop (#52) -- ack is {} for routine/skipped dispatches;
    # escalations is the workflow's int counter (_await_ack), never a list.
    ack_status = Counter()
    escalation_counts: list[float] = []
    acked = 0
    for r in results:
        a = r.get("ack") or {}
        if not a:
            continue
        acked += 1
        ack_status[a.get("ackStatus", "UNKNOWN")] += 1
        escs = a.get("escalations")
        if isinstance(escs, (int, float)):
            escalation_counts.append(float(escs))

    return {
        "studies": n,
        "byFinalState": dict(final_states),
        "triage": {
            "triaged": triaged,
            "byTier": dict(tiers),
            "meanPriorityScore": _mean(scores),
        },
        "verification": {
            "verified": verified,
            "byStatus": dict(verif_status),
            # hitRate: of studies the safety-net verified, how many it flagged (WARN or FAIL).
            "hitRate": round(non_pass / verified, 3) if verified else None,
            "requiresHumanReviewRate": round(requires_review / verified, 3) if verified else None,
            "issuesBySeverity": dict(severity_hits),
            "topRules": dict(rule_hits.most_common(10)),
        },
        "signoffGate": {
            "recordedReleases": gate_recorded,
            "byStatus": dict(gate_status),
            "authenticatedOverrides": overrides,
        },
        "criticalAck": {
            "withAckClock": acked,
            "byStatus": dict(ack_status),
            "meanEscalations": _mean(escalation_counts),
        },
        # #76 metrics that the result payload cannot carry; named so a partial capture is honest.
        "requiresRicherCapture": [
            "ai-vs-label concordance (interpretation findings + manifest labels)",
            "draft-vs-signed agreement (pre-sign impression + final report body)",
            "time-in-state (workflow history event timestamps)",
            "gates released by a signed addendum (signoff stays empty in the result; #66 "
            "audit field is a noted follow-up)",
        ],
    }


# --- rendering ---------------------------------------------------------------

def _fmt_counter(d: dict) -> str:
    if not d:
        return "  (none)"
    width = max(len(str(k)) for k in d)
    return "\n".join(f"  {str(k):<{width}}  {v}" for k, v in d.items())


def render_table(s: dict[str, Any]) -> str:
    v = s["verification"]
    t = s["triage"]
    g = s["signoffGate"]
    a = s["criticalAck"]
    lines = [
        f"MIMIC-CXR showcase metrics -- {s['studies']} study(ies)",
        "",
        "Final state:",
        _fmt_counter(s["byFinalState"]),
        "",
        f"Triage ({t['triaged']} scored, mean priorityScore {t['meanPriorityScore']}):",
        _fmt_counter(t["byTier"]),
        "",
        f"Verification ({v['verified']} verified, hit rate {v['hitRate']}, "
        f"requires-human-review rate {v['requiresHumanReviewRate']}):",
        _fmt_counter(v["byStatus"]),
        "  rules fired:",
        "\n".join(f"    {k}  {n}" for k, n in v["topRules"].items()) or "    (none)",
        "",
        f"Sign-off gate: {g['recordedReleases']} recorded release(s), "
        f"authenticated overrides {g['authenticatedOverrides']} "
        f"(addendum releases leave no trace in the result -- see below)",
        _fmt_counter(g["byStatus"]),
        "",
        f"Critical-result ack: {a['withAckClock']} with an ack clock, "
        f"mean escalations {a['meanEscalations']}",
        _fmt_counter(a["byStatus"]),
        "",
        "Needs richer capture than the result payload (not computed here):",
        "\n".join(f"  - {m}" for m in s["requiresRicherCapture"]),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Roll captured workflow results into showcase metrics (#76).")
    ap.add_argument("paths", nargs="+", help="workflow-result JSON files and/or directories of them")
    ap.add_argument("--json", action="store_true", help="emit the machine summary instead of the table")
    args = ap.parse_args(argv)

    results = load_results(args.paths)
    if not results:
        print("no workflow-result payloads found", file=sys.stderr)
        return 1

    summary = summarize(results)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(render_table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
