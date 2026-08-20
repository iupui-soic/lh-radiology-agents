"""Do the pre-sign drafts agree with the findings that authorised them? (#76 / #77)

The pre-sign draft is written into a patient's chart BEFORE a radiologist reads the study, and it
is only written when some AI finding came back COMPLETE (#26). So a draft that asserts the study
is normal, or that never mentions the pathology which fired, is a contradiction sitting in a chart
under an AI authorship stamp. This tool counts them.

    python showcase_draft_audit.py                 # live, against the running stack
    python showcase_draft_audit.py --json

Two things it gets right that a naive version does not, both learned by getting them wrong first:

**Join on `basedOn`, never on the patient.** The #68 cohort deliberately includes studies from
patients with a prior (the priors bucket), so one patient can carry several pre-sign drafts. On
the demo host 31 of the cohort's patients do. Fetching a patient's DiagnosticReports and taking
the first pre-sign one attributes a sibling study's draft to this study, which manufactures
contradictions that are not there. The draft for a study is the one whose `basedOn` names that
study's ServiceRequest.

**A qualified normality claim is not a contradiction.** "Pleural effusion is present. No acute
cardiopulmonary abnormalities are OTHERWISE identified." is how a radiologist writes a report
with one finding. Only flag a normality phrase when the prose ALSO failed to assert a confirmed
finding; on its own it is good prose, not a defect.

Both checks reuse the same negation-aware idea as the #78 scanners: a mention that is negated
("no pneumothorax") does not count as an assertion.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import urllib.request
from collections import Counter
from typing import Any, Optional

# The pre-sign draft concept (#55), the stamp that marks a DiagnosticReport as the AI's own draft.
PRESIGN_CONCEPT = "e3641471-3f25-57b4-ab27-a3ebc66e481e"

# tool -> the pathology word its label asserts, for the agreement check.
PATHOLOGY = {"pneumothorax-detect": "pneumothorax", "effusion-detect": "effusion"}

# Global "this study is normal" claims. Only meaningful alongside a missed finding (see docstring).
NORMAL_PHRASES = ("no acute cardiopulmonary", "no acute intrathoracic", "unremarkable",
                  "no acute abnormality", "no acute findings", "within normal limits")


def is_negated(text: str, word: str) -> Optional[bool]:
    """True if every mention of `word` is negated, False if any is asserted, None if never mentioned."""
    hits = list(re.finditer(rf"\b{word}\w*", text))
    if not hits:
        return None
    for h in hits:
        before = text[max(0, h.start() - 30):h.start()]
        if not re.search(r"\b(no|without|free of|negative for|resolved)\b[^.]*$", before):
            return False
    return True


def complete_tools(row: dict) -> list[str]:
    """The pixel tools that returned COMPLETE for this study, in PATHOLOGY order."""
    statuses = {f.get("toolId"): f.get("status")
                for f in (row.get("aiFindings") or {}).get("findings", [])}
    return [t for t in PATHOLOGY if statuses.get(t) == "COMPLETE"]


def draft_problems(conclusion: str, tools: list[str]) -> list[str]:
    """What is wrong with this draft given the findings that authorised it. Empty means it agrees."""
    low = (conclusion or "").strip().lower()
    problems: list[str] = []
    for tool in tools:
        word = PATHOLOGY[tool]
        negated = is_negated(low, word)
        if negated is None:
            problems.append(f"omits-{word}")
        elif negated:
            problems.append(f"negates-{word}")
    # Deliberately AFTER the assertion checks, and conditional on them: see the module docstring.
    if problems and any(p in low for p in NORMAL_PHRASES):
        problems.append("asserts-normal")
    return problems


def draft_for_order(drafts: list[dict], order_ref: str) -> Optional[dict]:
    """The pre-sign draft belonging to THIS study: the one whose basedOn names its order."""
    for d in drafts:
        if order_ref in [(x or {}).get("reference") for x in (d.get("basedOn") or [])]:
            return d
    return None


def audit(rows: list[dict], drafts_by_accession: dict[str, list[dict]],
          order_by_accession: dict[str, str]) -> dict[str, Any]:
    """Pure roll-up, so the join and the heuristics are testable without a live stack."""
    tally: Counter = Counter()
    contradictions: list[dict] = []
    multi_draft_patients = 0
    for row in rows:
        acc = row.get("accessionNumber")
        tools = complete_tools(row)
        if not tools:
            tally["no COMPLETE finding (no draft expected)"] += 1
            continue
        order_ref = order_by_accession.get(acc)
        drafts = drafts_by_accession.get(acc) or []
        if len(drafts) > 1:
            multi_draft_patients += 1
        draft = draft_for_order(drafts, order_ref) if order_ref else None
        if draft is None:
            tally["COMPLETE finding but no draft for this order"] += 1
            continue
        problems = draft_problems(draft.get("conclusion") or "", tools)
        if problems:
            tally["contradicts"] += 1
            contradictions.append({"accession": acc, "problems": problems,
                                   "conclusion": (draft.get("conclusion") or "").strip()[:200]})
        else:
            tally["agrees"] += 1
    return {
        "tally": dict(tally),
        "contradictions": contradictions,
        "patientsWithMultipleDrafts": multi_draft_patients,
    }


# --- live fetch ---------------------------------------------------------------------------

async def fetch_live(worklist_url: str) -> tuple[list[dict], dict, dict]:
    """Rows plus, per accession, that patient's pre-sign drafts and the study's order ref."""
    from radagent_common.fhir_client import Fhir2Client          # noqa: PLC0415 - live path only
    from radagent_common.openmrs_rest import OpenmrsRestClient   # noqa: PLC0415

    rows = json.load(urllib.request.urlopen(worklist_url)).get("items", [])
    rest, fhir = OpenmrsRestClient(), Fhir2Client()
    drafts_by_accession: dict[str, list[dict]] = {}
    order_by_accession: dict[str, str] = {}
    for row in rows:
        acc = row["accessionNumber"]
        if not complete_tools(row):
            continue                      # no draft is expected, so spend no requests on it
        resolved = await rest.resolve_radiology_order_by_accession(acc)
        if not resolved:
            continue
        order_by_accession[acc] = resolved["fhirServiceRequestId"]
        pid = resolved["fhirPatientId"].split("/", 1)[1]
        bundle = await fhir._get("DiagnosticReport", {"subject": pid, "_count": "50"})
        drafts_by_accession[acc] = [
            e["resource"] for e in (bundle.get("entry") or [])
            if (((e["resource"].get("code") or {}).get("coding") or [{}])[0].get("code")) == PRESIGN_CONCEPT
        ]
    return rows, drafts_by_accession, order_by_accession


def render(summary: dict[str, Any]) -> str:
    lines = [
        "Pre-sign draft agreement (joined on basedOn)",
        "",
        "  " + "\n  ".join(f"{k}: {v}" for k, v in summary["tally"].items()),
        "",
        f"patients carrying more than one pre-sign draft: {summary['patientsWithMultipleDrafts']} "
        f"(a patient-only join would misattribute these)",
    ]
    if summary["contradictions"]:
        lines += ["", f"contradicting drafts ({len(summary['contradictions'])}):"]
        for c in summary["contradictions"]:
            lines.append(f"  {c['accession']}  {','.join(c['problems'])}")
            lines.append(f"      {c['conclusion']}")
    else:
        lines += ["", "no contradicting drafts"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Audit pre-sign drafts against their findings (#76).")
    ap.add_argument("--worklist-url", default="http://worklist-api:8107/worklist",
                    help="the running worklist API (default: the in-cluster address)")
    ap.add_argument("--json", action="store_true", help="machine summary instead of the table")
    args = ap.parse_args(argv)

    rows, drafts, orders = asyncio.run(fetch_live(args.worklist_url))
    if not rows:
        print("no worklist rows found", file=sys.stderr)
        return 1
    summary = audit(rows, drafts, orders)
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
