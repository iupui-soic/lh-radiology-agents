"""AI finding vs MIMIC label concordance (#76 evaluation build item).

`showcase_metrics.py` names this as one of the three metrics the workflow-result payload cannot
carry, because the result holds no findings and no accession. This tool computes it from the two
artefacts that DO carry them: the worklist listing (accession + aiFindings, the same data the
demo puts on screen) and the cohort manifest (the MIMIC labels).

    curl -s http://worklist-api:8107/worklist > worklist.json
    python showcase_concordance.py worklist.json --manifest ~/mimic-secure/showcase-v1/manifest.json

WHAT THE LABELS ACTUALLY SUPPORT, which decides the whole design:

`curate_cohort.concordant()` keeps a label only where chexpert.csv AND negbio.csv both say 1.0, so
the manifest holds POSITIVES ONLY. An absent label is not a negative: it is a true negative, an
uncertain (-1), a not-mentioned, or a study the two labellers disputed, and nothing downstream can
tell those apart. Scoring absence as negative would invent a specificity the cohort cannot support.

So each study lands in one of three reference classes per tool, and only two of them are scored:

  positive       the tool's label is a concordant 1.0        -> sensitivity
  normal         "No Finding" is a concordant 1.0            -> specificity / false-positive rate
  indeterminate  neither                                     -> COUNTED AND REPORTED, never scored

Two more distinctions the finding shape forces:

  - `rawScore is not None` means the model RAN. A tool stubbed by design (cxr-screen) never ran and
    is excluded from every denominator rather than scored as a miss.
  - `status == "COMPLETE"` means it FIRED. A model that ran and stayed under its operating point
    reports status STUBBED with a negation-worded label, which is a true negative, not a no-op.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

# tool -> the manifest label that is its reference standard.
#
# effusion-detect maps to "Pleural Effusion" ALONE, not to curate_cohort's EFFUSION_GROUP
# ("Pleural Effusion", "Consolidation", "Edema"). That group is a cohort COMPOSITION bucket, three
# pathologies sampled together; the model's head is trained on effusion. Scoring it against
# consolidation or edema would credit or blame it for a call it never made.
TOOL_LABELS: dict[str, str] = {
    "pneumothorax-detect": "Pneumothorax",
    "effusion-detect": "Pleural Effusion",
}

NORMAL_LABEL = "No Finding"


def load_json(path: str) -> Any:
    with open(path) as f:
        return json.load(f)


def worklist_rows(doc: Any) -> list[dict]:
    """Accept either the /worklist envelope or a bare list of rows."""
    if isinstance(doc, dict):
        return doc.get("items") or []
    return doc if isinstance(doc, list) else []


def manifest_labels(doc: Any) -> dict[str, dict]:
    """study_id -> labels mapping, from the manifest's list or {"studies": [...]} shape."""
    entries = doc if isinstance(doc, list) else (doc or {}).get("studies", [])
    return {e["study_id"]: (e.get("labels") or {}) for e in entries if e.get("study_id")}


def _findings(row: dict) -> dict[str, dict]:
    return {f.get("toolId"): f for f in ((row.get("aiFindings") or {}).get("findings") or [])
            if f.get("toolId")}


def _reference_class(labels: dict, target: str) -> str:
    if labels.get(target):
        return "positive"
    if labels.get(NORMAL_LABEL):
        return "normal"
    return "indeterminate"


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 3) if den else None


def concordance(rows: list[dict], labels_by_study: dict[str, dict]) -> dict[str, Any]:
    """Per-tool concordance, plus the join accounting that says who was left out and why."""
    matched, unmatched = [], []
    for row in rows:
        acc = row.get("accessionNumber")
        if acc in labels_by_study:
            matched.append((acc, row))
        else:
            unmatched.append(acc)

    tools: dict[str, Any] = {}
    for tool, target in TOOL_LABELS.items():
        buckets = {c: {"studies": 0, "fired": 0} for c in ("positive", "normal", "indeterminate")}
        did_not_run = 0
        for acc, row in matched:
            finding = _findings(row).get(tool)
            if not finding:
                continue
            if finding.get("rawScore") is None:      # stubbed by design: never entered a denominator
                did_not_run += 1
                continue
            cls = _reference_class(labels_by_study[acc], target)
            buckets[cls]["studies"] += 1
            if finding.get("status") == "COMPLETE":
                buckets[cls]["fired"] += 1

        pos, norm, ind = buckets["positive"], buckets["normal"], buckets["indeterminate"]
        tools[tool] = {
            "referenceLabel": target,
            "positives": {
                "n": pos["studies"], "fired": pos["fired"],
                "sensitivity": _rate(pos["fired"], pos["studies"]),
            },
            "concordantNormals": {
                "n": norm["studies"], "fired": norm["fired"],
                "falsePositiveRate": _rate(norm["fired"], norm["studies"]),
                "specificity": _rate(norm["studies"] - norm["fired"], norm["studies"]),
            },
            "indeterminate": {
                "n": ind["studies"], "fired": ind["fired"],
                "note": "not scored: absence of a concordant label is not a negative",
            },
            "modelDidNotRun": did_not_run,
        }

    return {
        "studiesInWorklist": len(rows),
        "joinedToManifest": len(matched),
        "unmatchedAccessions": [a for a in unmatched if a],
        "tools": tools,
        "labelPolicy": (
            "manifest labels are chexpert AND negbio agreeing at 1.0 (curate_cohort.concordant), "
            "so only positives and 'No Finding' normals are scorable; everything else is "
            "indeterminate and is reported, not scored"),
    }


def render_table(s: dict[str, Any]) -> str:
    lines = [
        f"AI vs MIMIC label concordance -- {s['joinedToManifest']}/{s['studiesInWorklist']} "
        f"worklist studies joined to the manifest",
        "",
    ]
    if s["unmatchedAccessions"]:
        lines += [f"NOT joined ({len(s['unmatchedAccessions'])}): "
                  f"{', '.join(s['unmatchedAccessions'][:8])}"
                  f"{' ...' if len(s['unmatchedAccessions']) > 8 else ''}", ""]
    for tool, t in s["tools"].items():
        p, n, i = t["positives"], t["concordantNormals"], t["indeterminate"]
        lines += [
            f"{tool}  (reference label: {t['referenceLabel']})",
            f"  label-positive      n={p['n']:<4} fired={p['fired']:<4} sensitivity={p['sensitivity']}",
            f"  concordant normal   n={n['n']:<4} fired={n['fired']:<4} "
            f"specificity={n['specificity']}  false-positive rate={n['falsePositiveRate']}",
            f"  indeterminate       n={i['n']:<4} fired={i['fired']:<4} ({i['note']})",
        ]
        if t["modelDidNotRun"]:
            lines.append(f"  model did not run on {t['modelDidNotRun']} study(ies): excluded from every denominator")
        lines.append("")
    lines.append("Label policy: " + s["labelPolicy"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI finding vs MIMIC label concordance (#76).")
    ap.add_argument("worklist", help="captured /worklist JSON (envelope or bare list of rows)")
    ap.add_argument("--manifest", required=True,
                    help="cohort manifest with the MIMIC labels (DUA: keep it off this repo)")
    ap.add_argument("--json", action="store_true", help="emit the machine summary instead of the table")
    args = ap.parse_args(argv)

    for path in (args.worklist, args.manifest):
        if not os.path.exists(path):
            print(f"no such file: {path}", file=sys.stderr)
            return 1

    rows = worklist_rows(load_json(args.worklist))
    labels = manifest_labels(load_json(args.manifest))
    if not rows:
        print("no worklist rows found", file=sys.stderr)
        return 1
    if not labels:
        print("no labelled studies found in the manifest", file=sys.stderr)
        return 1

    summary = concordance(rows, labels)
    print(json.dumps(summary, indent=2) if args.json else render_table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
