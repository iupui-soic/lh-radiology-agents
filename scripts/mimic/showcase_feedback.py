"""Showcase feedback instrument: response template + tally (#76).

The evaluation half of #76 that the metrics tool cannot cover. `showcase_metrics.py` measures what
the PIPELINE did; this measures what the PEOPLE thought of it. The questions live in
`docs/showcase-feedback-form.md`; this module owns the machine-readable shape those answers are
recorded in, and rolls a directory of them into a summary.

One JSON file per participant, so a session's responses are just a directory:

    python showcase_feedback.py template > responses/r01.json     # blank to fill in
    python showcase_feedback.py responses/                        # the tally
    python showcase_feedback.py responses/ --json                 # machine summary

Three deliberate choices:

  - **Pseudonymous by construction.** The response carries a `participantId` the operator assigns
    (P01, P02), never a name, and there is nowhere to type one. Feedback on a MIMIC cohort is
    research data about a person's judgement, and the run-book's credentialing record is where
    identity belongs, not here.
  - **An unanswered item is null, never a zero.** Nulls are excluded from the means and the per
    item `n` is reported alongside, so a half-filled form cannot quietly drag a score down. A
    score outside the 1..5 scale is counted as INVALID and named, rather than clamped.
  - **Per-stage, not one overall number.** #76 asks for usefulness, trust and workflow fit at each
    stage the radiologist actually sees, because the interesting result is the spread between them
    (a stage can be useful and still not trusted, which is the finding worth having).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

# The stages a participant actually experiences, in run-book arc order. Keys are stable: they are
# the join between the form's numbered sections and this tally.
STAGES: list[tuple[str, str]] = [
    ("worklist", "Reading worklist: priority order and what sits at the top"),
    ("viewer", "Viewer: hanging protocol, AI finding banner, CAD evidence overlay"),
    ("draft", "Pre-sign AI draft impression in the RIS"),
    ("verification", "Post-sign verification verdict and the sign-off gate"),
    ("critical_comms", "Critical-result page and the acknowledgement loop"),
    ("ehr_packet", "Pre-read EHR context: labs, medications, problems"),
]

# The three Likert axes #76 names, per stage.
AXES: list[tuple[str, str]] = [
    ("usefulness", "Did this help you do the read?"),
    ("trust", "Would you rely on it as shown?"),
    ("workflow_fit", "Does it fit how you actually work?"),
]

SCALE_MIN, SCALE_MAX = 1, 5
ROLES = ("radiologist", "referring_physician", "trainee", "other")


def template() -> dict:
    """A blank response, with every key present so a filler sees the whole instrument."""
    return {
        "schemaVersion": "1.0.0",
        "participantId": "",            # pseudonymous: P01, P02. Never a name.
        "role": "",                     # one of ROLES
        "yearsExperience": None,
        "physionetCredentialed": None,  # run-book prerequisite 4; recorded, not enforced here
        "sessionDate": "",              # YYYY-MM-DD
        "arcsSeen": [],                 # e.g. [1, 2, 3, 4]
        "stages": {
            key: {**{axis: None for axis, _ in AXES}, "comment": ""}
            for key, _ in STAGES
        },
        "overall": {
            "wouldUseInPractice": None,     # true / false / null for undecided
            "biggestValue": "",
            "biggestConcern": "",
            "safetyConcerns": "",
            "anythingMissing": "",
        },
    }


def _iter_response_paths(paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            out.extend(os.path.join(p, f) for f in os.listdir(p) if f.endswith(".json"))
        else:
            out.append(p)
    return sorted(set(out))


def load_responses(paths: Iterable[str]) -> list[dict]:
    """Load response docs, skipping anything that is not one (a stray manifest, a metrics dump).

    A response is recognised by a `stages` mapping, the one key nothing else in this directory
    tree has.
    """
    out: list[dict] = []
    for path in _iter_response_paths(paths):
        try:
            with open(path) as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"skip {path}: {e}", file=sys.stderr)
            continue
        if isinstance(doc, dict) and isinstance(doc.get("stages"), dict):
            doc.setdefault("_path", path)
            out.append(doc)
        else:
            print(f"skip {path}: not a feedback response (no stages block)", file=sys.stderr)
    return out


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


def _valid_score(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and SCALE_MIN <= value <= SCALE_MAX


def summarize(responses: list[dict]) -> dict[str, Any]:
    """Roll responses into per-stage, per-axis scores plus the free text.

    `answered` is carried next to every mean so a thin cell is visible rather than implied, and
    `invalid` names off-scale entries instead of silently dropping them.
    """
    by_role = Counter((r.get("role") or "unspecified") for r in responses)
    credentialed = Counter(r.get("physionetCredentialed") for r in responses)

    stages: dict[str, Any] = {}
    invalid: list[str] = []
    for key, label in STAGES:
        axes: dict[str, Any] = {}
        for axis, _q in AXES:
            values: list[float] = []
            for r in responses:
                raw = ((r.get("stages") or {}).get(key) or {}).get(axis)
                if raw is None or raw == "":
                    continue
                if _valid_score(raw):
                    values.append(float(raw))
                else:
                    invalid.append(
                        f"{r.get('participantId') or r.get('_path', '?')}: "
                        f"{key}.{axis}={raw!r} outside {SCALE_MIN}..{SCALE_MAX}")
            axes[axis] = {
                "mean": _mean(values),
                "answered": len(values),
                "distribution": dict(sorted(Counter(int(v) for v in values).items())),
            }
        comments = [
            {"participantId": r.get("participantId") or "?",
             "comment": ((r.get("stages") or {}).get(key) or {}).get("comment", "").strip()}
            for r in responses
            if (((r.get("stages") or {}).get(key) or {}).get("comment") or "").strip()
        ]
        stages[key] = {"label": label, "axes": axes, "comments": comments}

    would_use = Counter()
    free_text: dict[str, list[dict]] = defaultdict(list)
    for r in responses:
        o = r.get("overall") or {}
        would_use[o.get("wouldUseInPractice")] += 1
        for field in ("biggestValue", "biggestConcern", "safetyConcerns", "anythingMissing"):
            text = (o.get(field) or "").strip()
            if text:
                free_text[field].append(
                    {"participantId": r.get("participantId") or "?", "text": text})

    # Completeness: which responses left a whole stage untouched. A blank stage is a legitimate
    # answer (the participant did not see that arc), so this reports rather than complains.
    incomplete = []
    for r in responses:
        missing = [
            key for key, _ in STAGES
            if not any(_valid_score(((r.get("stages") or {}).get(key) or {}).get(axis))
                       for axis, _ in AXES)
        ]
        if missing:
            incomplete.append({"participantId": r.get("participantId") or "?",
                               "stagesUnanswered": missing})

    return {
        "responses": len(responses),
        "byRole": dict(by_role),
        "physionetCredentialed": {str(k): v for k, v in credentialed.items()},
        "stages": stages,
        "overall": {
            "wouldUseInPractice": {str(k): v for k, v in would_use.items()},
            "freeText": {k: v for k, v in free_text.items()},
        },
        "incompleteResponses": incomplete,
        "invalidScores": invalid,
    }


def render_table(s: dict[str, Any]) -> str:
    lines = [
        f"MIMIC-CXR showcase feedback -- {s['responses']} response(s)",
        "",
        "Participants by role:",
        "\n".join(f"  {k:<20} {v}" for k, v in s["byRole"].items()) or "  (none)",
        "",
        f"Scale {SCALE_MIN}..{SCALE_MAX}; 'n' is how many people answered that cell.",
        "",
    ]
    lines.append(f"  {'stage':<16}" + "".join(f"{axis:>16}" for axis, _ in AXES))
    for key, _label in STAGES:
        row = f"  {key:<16}"
        for axis, _ in AXES:
            cell = s["stages"][key]["axes"][axis]
            mean = cell["mean"]
            shown = "-- (n=0)" if mean is None else f"{mean} (n={cell['answered']})"
            row += f"{shown:>16}"
        lines.append(row)
    lines.append("")
    lines.append("Would use in practice: " + (
        ", ".join(f"{k}={v}" for k, v in s["overall"]["wouldUseInPractice"].items()) or "(none)"))
    for field, entries in s["overall"]["freeText"].items():
        lines.append("")
        lines.append(f"{field}:")
        for e in entries:
            lines.append(f"  [{e['participantId']}] {e['text']}")
    for key, _ in STAGES:
        comments = s["stages"][key]["comments"]
        if comments:
            lines.append("")
            lines.append(f"comments on {key}:")
            for c in comments:
                lines.append(f"  [{c['participantId']}] {c['comment']}")
    if s["incompleteResponses"]:
        lines += ["", "Responses with an unanswered stage (blank is legitimate if the arc was not shown):"]
        for i in s["incompleteResponses"]:
            lines.append(f"  [{i['participantId']}] {', '.join(i['stagesUnanswered'])}")
    if s["invalidScores"]:
        lines += ["", "Off-scale entries (NOT counted):"]
        lines += [f"  {m}" for m in s["invalidScores"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Showcase feedback: emit a blank response, or tally filled ones (#76).")
    ap.add_argument("paths", nargs="*",
                    help="response JSON files and/or directories, or the single word 'template'")
    ap.add_argument("--json", action="store_true", help="emit the machine summary instead of the table")
    args = ap.parse_args(argv)

    if len(args.paths) == 1 and args.paths[0] == "template":
        print(json.dumps(template(), indent=2))
        return 0
    if not args.paths:
        ap.error("give response paths, or 'template'")

    responses = load_responses(args.paths)
    if not responses:
        print("no feedback responses found", file=sys.stderr)
        return 1
    summary = summarize(responses)
    print(json.dumps(summary, indent=2) if args.json else render_table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
