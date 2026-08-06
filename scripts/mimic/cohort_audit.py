"""Audit every cohort study's seeded narrative in fhir2 against the source manifest (#105).

Read-only. Answers one question per study: is the narrative fhir2 serves still this study's OWN
narrative, whole?

    python3 cohort_audit.py --manifest /path/to/manifest.json

Why this exists: on 2026-08-06 a sweep of the demo host found 10 studies whose narrative had been
replaced by rehearsal placeholder text (a radiologist signs, the sign-bridge projects that body
over the seeded report, and the study silently stops being the study it was), one carrying a
DIFFERENT study's text entirely, and 3 missing their IMPRESSION section. None of it was visible
from the RIS, the worklist or any health check -- only from comparing against the source.

Prints study ids, lengths and verdicts. Never prints narrative text, so the corpus stays where the
DUA says it stays.

Verdicts:
  OK          matches this study's manifest narrative
  TRUNCATED   a portion of its own narrative (the fhir2 conclusion cap clamps long ones)
  CROSSED     matches a DIFFERENT study's narrative
  DIVERGED    matches nothing in the manifest (typically rehearsal placeholder text)
  NO-REPORT   no seeded DiagnosticReport for the order
  NO-ORDER    the accession does not resolve to an order
"""
from __future__ import annotations

import argparse
import json
import re

from omrs_client import OmrsClient
from report_text import FHIR2_CONCLUSION_MAX

MARKER = "TRUNCATED BY ris-sign-bridge"


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def load_manifest(path: str) -> dict[str, str]:
    raw = json.load(open(path))
    entries = raw if isinstance(raw, list) else raw.get("studies", [])
    return {e["study_id"]: (e.get("report_text") or "") for e in entries}


def classify(stored: str, mine: str, by_text: dict[str, list[str]], sid: str) -> str:
    """Verdict for one study. `by_text` maps normalised manifest text -> study ids."""
    got = norm(strip_html(stored))
    mine_n = norm(mine)
    if got == mine_n:
        return "OK"
    # Drop the sign-bridge's in-band marker wherever it sits -- it PREFIXES on the sign path and
    # can be appended elsewhere -- so a clamped body is still recognisable as whose text it is.
    probe = norm(re.sub(r"\[\s*" + re.escape(MARKER) + r"[^\]]*\]", " ", got))
    if probe and mine_n and probe in mine_n:
        return "TRUNCATED"
    # a fragment of somebody else's narrative: the sign-bridge projected the wrong body
    head = probe[:120]
    if head:
        for text, ids in by_text.items():
            if head in text:
                other = [i for i in ids if i != sid]
                if other:
                    return f"CROSSED->{other[0]}"
    return "DIVERGED"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--manifest", required=True, help="showcase manifest.json (the source of truth)")
    p.add_argument("--quiet", action="store_true", help="counts only, no per-study lines")
    args = p.parse_args(argv)

    own = load_manifest(args.manifest)
    by_text = {}
    for sid, text in own.items():
        if text:
            by_text.setdefault(norm(text), []).append(sid)

    client = OmrsClient()
    counts: dict[str, int] = {}
    problems: list[tuple[str, str, str]] = []

    for sid in sorted(own):
        order = client.order_for_accession(sid)
        if not order:
            verdict, detail = "NO-ORDER", ""
        else:
            report_id = client.find_seeded_report(order["patient_uuid"], order["order_uuid"])
            if not report_id:
                verdict, detail = "NO-REPORT", ""
            else:
                res = client._fget(f"DiagnosticReport/{report_id}")
                stored = res.get("conclusion") or ""
                verdict = classify(stored, own[sid], by_text, sid)
                detail = (f"status={res.get('status')} stored={len(norm(stored))} "
                          f"source={len(norm(own[sid]))}")
        key = verdict.split("->")[0]
        counts[key] = counts.get(key, 0) + 1
        if key != "OK":
            problems.append((sid, verdict, detail))

    print(f"audited {len(own)} studies (fhir2 conclusion cap = {FHIR2_CONCLUSION_MAX})")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")
    if problems and not args.quiet:
        print("\nstudies differing from the source:")
        for sid, verdict, detail in problems:
            print(f"  {sid}  {verdict:<22} {detail}")
    return 1 if any(k in counts for k in ("CROSSED", "DIVERGED", "NO-ORDER", "NO-REPORT")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
