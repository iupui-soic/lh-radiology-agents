#!/usr/bin/env python3
"""Audit which AI tools the interpretation registry picks for the study names a PACS really sends.

    python scripts/audit_registry_selection.py                       # against $ORTHANC_BASE_URL
    python scripts/audit_registry_selection.py --url http://pacs:8042
    python scripts/audit_registry_selection.py --url ... --json corpus.json

Why this exists (#64). `registry.select_tools(modality, studyDescription)` decides which tools a
study gets by keyword-matching the DICOM StudyDescription. That description is whatever the
department typed -- `CTPA`, `CXR`, `CT BRAIN` -- and the registry's keys are anatomical (`chest`,
`head`). The two only meet if the alias table happens to know the local naming convention.

It has been wrong in both directions already, and both were found by staring at the table rather
than by looking at data:

  - #63: `CXR`, `CTPA`, `CT BRAIN`, `MRI HEAD` matched nothing and fell to the generic screen.
  - !53 review: `CT FEMORAL HEAD` and `MRI HEAD OF FEMUR` matched the BRAIN tools; `CT ANGIO
    VERTEBRAL ARTERIES` matched the spine tool.

Guessing at the table one entry at a time does not converge. Point this at the real PACS instead:
it reports every distinct study name, what the registry selects for it, and -- the two columns that
matter -- which studies get NO regional tool, and which get a regional tool at all (so a human can
scan for the ones that got the wrong one).

Nothing is asserted about what SHOULD be selected. That is a clinical call, and this script exists
to put a real list in front of the person who can make it.

Read-only: a GET against /studies?expand=1. It writes nothing to Orthanc.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agents" / "interpretation-assistant"))

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("httpx is required: pip install -e libs/radagent-common")

from registry import select_tools  # noqa: E402  (needs the sys.path line above)

_GENERIC = {
    "generic-ct-screen", "generic-mr-screen", "generic-xr-screen",
    "generic-us-screen", "mammo-screen",
}


def fetch_studies(base_url: str, timeout: float) -> list[dict]:
    """Every study Orthanc knows, in the lean shape we need. Mirrors orthanc_client.

    requestedTags is load-bearing (#99, same fix as orthanc_client): a study record's
    MainDicomTags never carry a modality (Modality is series-level, ModalitiesInStudy is a
    computed tag returned only when asked), so without it every row audits as modality '?'
    and select_tools returns nothing for the whole corpus -- the audit reports a registry
    failure that is actually the audit's own fetch."""
    url = f"{base_url.rstrip('/')}/studies"
    with httpx.Client(timeout=timeout) as c:
        r = c.get(url, params={"expand": True, "requestedTags": "ModalitiesInStudy"})
        r.raise_for_status()
        raw = r.json() or []
    out = []
    for s in raw:
        tags = s.get("MainDicomTags") or {}
        requested = s.get("RequestedTags") or {}
        out.append({
            "modality": (requested.get("ModalitiesInStudy") or tags.get("ModalitiesInStudy")
                         or tags.get("Modality") or "").strip(),
            "studyDescription": (tags.get("StudyDescription") or "").strip(),
        })
    return out


def audit(studies: list[dict]) -> list[dict]:
    """One row per DISTINCT (modality, description), with what the registry picks for it."""
    counts = Counter((s["modality"], s["studyDescription"]) for s in studies)
    rows = []
    for (modality, description), n in sorted(counts.items()):
        tools = select_tools(modality, description)
        regional = [t for t in tools if t not in _GENERIC]
        rows.append({
            "modality": modality,
            "studyDescription": description,
            "studies": n,
            "tools": tools,
            # The two failure modes, made explicit so they can be counted rather than eyeballed:
            # nothing selected at all, and the modality's catch-all screen instead of a real tool.
            "selectsNothing": not tools,
            "generalOnly": bool(tools) and not regional,
        })
    return rows


def report(rows: list[dict]) -> int:
    total = sum(r["studies"] for r in rows)
    if not rows:
        print("No studies in this Orthanc — nothing to audit.")
        return 0

    width = max(len(r["studyDescription"] or "(none)") for r in rows)
    print(f"{len(rows)} distinct study names across {total} studies\n")
    print(f"  {'MOD':6} {'StudyDescription':{width}}  {'n':>4}  selects")
    print(f"  {'-'*6} {'-'*width}  {'-'*4}  {'-'*40}")
    for r in rows:
        desc = r["studyDescription"] or "(none)"
        flag = "  <-- no tool" if r["selectsNothing"] else ("  <-- generic only" if r["generalOnly"] else "")
        print(f"  {r['modality'] or '?':6} {desc:{width}}  {r['studies']:>4}  "
              f"{', '.join(r['tools']) or '(nothing)'}{flag}")

    gen = [r for r in rows if r["generalOnly"]]
    none = [r for r in rows if r["selectsNothing"]]
    gen_studies = sum(r["studies"] for r in gen)
    none_studies = sum(r["studies"] for r in none)

    print()
    print(f"  {gen_studies}/{total} studies get only the modality's generic screen "
          f"({len(gen)} distinct names)")
    print(f"  {none_studies}/{total} studies select NOTHING at all "
          f"({len(none)} distinct names — modality not in the registry)")
    print()
    print("  A high 'generic only' count means the alias table does not know this site's naming")
    print("  convention (#63). Read the list above for the opposite failure too: a study that got a")
    print("  regional tool it should not have (#64) — that one a human has to spot.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("ORTHANC_BASE_URL", "http://localhost:8042"),
                    help="Orthanc base URL (default: $ORTHANC_BASE_URL or http://localhost:8042)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--json", metavar="PATH",
                    help="also write the rows to PATH, to commit as a selection-test corpus")
    args = ap.parse_args()

    try:
        studies = fetch_studies(args.url, args.timeout)
    except Exception as exc:  # noqa: BLE001 - a CLI should say what went wrong, not traceback
        return f"could not read studies from {args.url}: {exc}"

    rows = audit(studies)
    rc = report(rows)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n")
        print(f"\n  wrote {args.json}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
