#!/usr/bin/env python3
"""CI gate: every /contracts schema must be a valid Draft 2020-12 schema, every card
must be valid JSON, and every fixture must validate against its schema.

Run: python scripts/validate_contracts.py
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
errors: list[str] = []


def _load(p: Path) -> dict:
    with p.open() as f:
        return json.load(f)


# 1. All schema files are themselves valid schemas.
schema_files = sorted(CONTRACTS.rglob("*.schema.json"))
for sf in schema_files:
    try:
        Draft202012Validator.check_schema(_load(sf))
    except Exception as e:  # noqa: BLE001
        errors.append(f"[schema invalid] {sf.relative_to(ROOT)}: {e}")

# 2. All agent cards parse as JSON, carry the minimum fields, and advertise the version their
#    agent actually stamps on its results (#124).
#
#    The two fields are not redundant. `AGENT_VERSION` is contractual: it is `required` on every
#    skill output schema, so it stamps each result with the build that produced it. The card's
#    `version` is what the agent ADVERTISES over A2A at /.well-known/agent-card.json. Until this
#    check they drifted freely -- interpretation-assistant's card said 0.1.0 from the initial
#    import while its handler had moved through six minor versions, so a peer resolving the card
#    was told 0.1.0 while every payload it got back was stamped 0.6.0. Nothing reads the card
#    version at runtime today, so nothing was broken; the cost was to the audit story, which is
#    the same story `_tool_version` exists to serve.
#
#    Parsed by regex rather than import: a handler must stay importable without its siblings on
#    sys.path, and importing one here would drag agent deps into the CI gate (golden rule 4 keeps
#    handlers free of a2a.*, and this keeps the validator free of handlers).
#
#    KNOWN LIMIT, do not mistake this for more than it is: this catches the two fields
#    DISAGREEING. It cannot catch both being stale together. An agent whose behaviour changes
#    without anyone bumping AGENT_VERSION stays permanently "in sync" and permanently wrong. When
#    to bump is a discipline no gate here enforces (PI note on #124).
_AGENT_VERSION_RE = re.compile(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', re.M)

for cf in sorted((CONTRACTS / "cards").glob("*.json")):
    try:
        card = _load(cf)
        for key in ("name", "url", "version", "skills"):
            if key not in card:
                errors.append(f"[card missing '{key}'] {cf.relative_to(ROOT)}")
        # Cards with no matching agent directory are skipped, not failed: a card may legitimately
        # describe an agent that does not live in this repo.
        handler_py = ROOT / "agents" / cf.stem / "handler.py"
        if "version" in card and handler_py.exists():
            m = _AGENT_VERSION_RE.search(handler_py.read_text())
            if m is None:
                errors.append(
                    f"[card version unverifiable] {cf.relative_to(ROOT)}: no AGENT_VERSION in "
                    f"{handler_py.relative_to(ROOT)}")
            elif m.group(1) != card["version"]:
                errors.append(
                    f"[card version drift] {cf.relative_to(ROOT)}: card says "
                    f"{card['version']}, {handler_py.relative_to(ROOT)} stamps {m.group(1)}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"[card invalid json] {cf.relative_to(ROOT)}: {e}")

# 3. Fixtures validate against their schema. Add a family here when a new fixture
#    filename prefix maps to a contract; individual fixtures are auto-discovered.
_studycontext_schema = CONTRACTS / "studycontext.schema.json"
_skills = CONTRACTS / "skills"
_fixtures = ROOT / "mocks" / "fixtures"
fixture_families = {
    "studycontext": _studycontext_schema,
    # CritCom comms-skill outputs (#52) validate against their per-skill schemas.
    "comms.dispatch.output": _skills / "comms.dispatch.schema.json",
    "comms.checkAck.output": _skills / "comms.checkAck.schema.json",
}
fixture_count = 0
for prefix, schema in fixture_families.items():
    fixtures = sorted(_fixtures.glob(f"{prefix}.*.json"))
    if not fixtures:
        print(f"WARNING: no fixtures found for family '{prefix}'")
    v = Draft202012Validator(_load(schema))
    for fixture in fixtures:
        fixture_count += 1
        for err in sorted(v.iter_errors(_load(fixture)), key=lambda e: e.path):
            errors.append(f"[fixture invalid] {fixture.name}: {list(err.path)} {err.message}")

# 4. Escalation policy (#29): the YAML matrix validates against its schema -- structurally, and
#    for the cross-field invariants JSON Schema can't express (ordering, single terminal repeat).
_escalation_schema = CONTRACTS / "escalation-policy.schema.json"
_escalation_policy = ROOT / "orchestrator" / "config" / "escalation-policy.yaml"
if _escalation_schema.exists() and _escalation_policy.exists():
    with _escalation_policy.open() as f:
        policy = yaml.safe_load(f)
    v = Draft202012Validator(_load(_escalation_schema))
    struct_errs = sorted(v.iter_errors(policy), key=lambda e: list(e.path))
    for err in struct_errs:
        errors.append(f"[escalation-policy] {list(err.path)} {err.message}")
    if not struct_errs:  # cross-field checks only make sense on a structurally-valid doc
        tiers = policy.get("tiers", {})
        if policy.get("defaultTier") not in tiers:
            errors.append(f"[escalation-policy] defaultTier {policy.get('defaultTier')!r} is not a tier")
        for tier, cfg in tiers.items():
            levels = cfg.get("levels", [])
            nums = [lv["level"] for lv in levels]
            mins = [lv["afterMinutes"] for lv in levels]
            if nums != sorted(nums) or len(set(nums)) != len(nums):
                errors.append(f"[escalation-policy] tier {tier}: level numbers must strictly increase, got {nums}")
            if mins != sorted(mins) or len(set(mins)) != len(mins):
                errors.append(f"[escalation-policy] tier {tier}: afterMinutes must strictly increase, got {mins}")
            repeats = [i for i, lv in enumerate(levels) if lv.get("repeat")]
            if len(repeats) > 1:
                errors.append(f"[escalation-policy] tier {tier}: at most one level may set repeat")
            if repeats and repeats[0] != len(levels) - 1:
                errors.append(f"[escalation-policy] tier {tier}: repeat may only be set on the final level")
            for lv in levels:
                if lv.get("repeat") and "repeatEveryMinutes" not in lv:
                    errors.append(f"[escalation-policy] tier {tier} level {lv['level']}: repeat requires repeatEveryMinutes")

# 5. Specialty routing (#58): the comms agent's study->subspecialty table validates against its
#    schema, plus the one cross-field invariant JSON Schema can't express cleanly: a rule with
#    neither modalities nor keywords can never match, and a dead rule in a paging table is a
#    config mistake someone meant to be real.
_routing_schema = CONTRACTS / "specialty-routing.schema.json"
_routing_table = ROOT / "agents" / "communications" / "specialty-routing.yaml"
if _routing_schema.exists() and _routing_table.exists():
    with _routing_table.open() as f:
        routing = yaml.safe_load(f)
    v = Draft202012Validator(_load(_routing_schema))
    routing_errs = sorted(v.iter_errors(routing), key=lambda e: list(e.path))
    for err in routing_errs:
        errors.append(f"[specialty-routing] {list(err.path)} {err.message}")
    if not routing_errs:
        for i, rule in enumerate(routing.get("rules", [])):
            if not rule.get("modalities") and not rule.get("keywords"):
                errors.append(f"[specialty-routing] rule {i} ({rule.get('specialty')!r}) has no "
                              "modalities and no keywords, so it can never match")

if errors:
    print("CONTRACT VALIDATION FAILED:")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print(
    f"OK: {len(schema_files)} schemas, cards, {fixture_count} fixtures, "
    "the escalation policy, and the specialty routing table validated."
)
