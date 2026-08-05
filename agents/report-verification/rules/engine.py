"""Declarative YAML rule engine for report verification.

Authoring model (ARCHITECTURE.md): Saptarshi (PI) writes rules in rules/*.yaml without
touching Python. Complex rules go in rules/custom/<id>.py exposing `check(ctx) -> dict|None`
returning an Issue dict {ruleId, severity, message, location} or None.

A rule's `when` clause describes the PROBLEM condition: if it evaluates True, an Issue
is emitted. Paths are dotted and may index lists, e.g. "impression.structuredFindings.0.laterality".
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml

from rules.report_body import detect_laterality, parse_report_body

_MISSING = object()
_VALID_SEVERITIES = ("INFO", "WARN", "FAIL")
_VALID_OPS = (
    "exists",
    "not_exists",
    "empty",
    "non_empty",
    "equals",
    "not_equals",
    "contains",
    "gt",
    "lt",
)


def enrich_report_body(ctx: dict, narrative: str) -> None:
    """Populate ctx['report']['body'] from the fetched report NARRATIVE (issue #22) so the
    body-dependent rules have structured fields (laterality, sections, BI-RADS, density) to match
    on, and derive the impression's own laterality from its text (the impression agent emits none)
    for the laterality cross-check. Mutates ctx in place. Pure: the handler does the fetch, parsing
    here has no I/O. An already-structured `report.body` dict is left untouched."""
    report = ctx.setdefault("report", {})
    if not isinstance(report.get("body"), dict):
        report["body"] = parse_report_body(narrative)
    impression = ctx.setdefault("impression", {})
    if "derivedLaterality" not in impression:
        impression["derivedLaterality"] = detect_laterality(impression.get("impressionText") or "")


@dataclass
class Rule:
    id: str
    severity: str            # INFO | WARN | FAIL
    when: dict
    message: str
    location: str = ""


def load_yaml_rules(rules_dir: Path) -> list[Rule]:
    rules: list[Rule] = []
    id_sources: dict[str, Path] = {}
    for path in sorted(rules_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"{path.name}: rule must be a YAML mapping")
        for key in ("id", "when"):
            if key not in data:
                raise ValueError(f"{path.name}: missing required key '{key}'")

        rule_id = data["id"]
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError(f"{path.name}: 'id' must be a non-empty string")

        when = data["when"]
        if not isinstance(when, dict):
            raise ValueError(f"{path.name}: 'when' must be a YAML mapping")
        if "op" not in when:
            raise ValueError(f"{path.name}: missing required key 'when.op'")

        severity = data.get("severity", "WARN")
        if severity not in _VALID_SEVERITIES:
            expected = "|".join(_VALID_SEVERITIES)
            raise ValueError(
                f"{path.name}: unknown severity {severity!r} (expected {expected})"
            )

        op = when["op"]
        if op not in _VALID_OPS:
            expected = "|".join(_VALID_OPS)
            raise ValueError(f"{path.name}: unknown op {op!r} (expected {expected})")

        if rule_id in id_sources:
            raise ValueError(
                f"{path.name}: duplicate rule id {rule_id!r} "
                f"(already defined in {id_sources[rule_id].name})"
            )
        id_sources[rule_id] = path

        rules.append(Rule(
            id=rule_id, severity=severity,
            when=when, message=data.get("message", rule_id),
            location=data.get("location", ""),
        ))
    return rules


def resolve(ctx: dict, path: str) -> Any:
    cur: Any = ctx
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg, _MISSING)
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            return _MISSING
        if cur is _MISSING:
            return _MISSING
    return cur


def _truthy_problem(when: dict, ctx: dict) -> bool:
    op = when.get("op")
    left = resolve(ctx, when["field"]) if "field" in when else _MISSING
    right = resolve(ctx, when["ref"]) if "ref" in when else when.get("value", _MISSING)

    if op == "exists":      return left is not _MISSING
    if op == "not_exists":  return left is _MISSING
    if op == "empty":       return left is _MISSING or left in ([], "", {}, None)
    if op == "non_empty":   return left is not _MISSING and bool(left)
    # equals/not_equals/contains/gt/lt need two real operands. A missing OR null value is not
    # comparable, so the rule does not fire -- and gt/lt never raise on None (a parsed report.body
    # carries its fields as null when absent, e.g. biradsAssessment on a non-mammography read).
    if left is _MISSING or right is _MISSING or left is None or right is None:
        return False
    if op == "equals":      return left == right
    if op == "not_equals":  return left != right
    try:
        if op == "contains": return right in left
        if op == "gt":       return left > right
        if op == "lt":       return left < right
    except TypeError:
        # Unexpected payload types fail closed instead of aborting report verification.
        return False
    return False


def evaluate(rule: Rule, ctx: dict) -> dict | None:
    if _truthy_problem(rule.when, ctx):
        msg = rule.message
        # best-effort interpolation of {field}/{ref} resolved values
        if "field" in rule.when:
            msg = msg.replace("{field}", str(resolve(ctx, rule.when["field"])))
        if "ref" in rule.when:
            msg = msg.replace("{ref}", str(resolve(ctx, rule.when["ref"])))
        return {"ruleId": rule.id, "severity": rule.severity, "message": msg, "location": rule.location}
    return None


def load_custom_checks(custom_dir: Path):
    checks = []
    for path in sorted(custom_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"custom_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        if hasattr(mod, "check"):
            checks.append(mod.check)
    return checks


@dataclass(frozen=True)
class LoadedRules:
    """The rule library, loaded and validated once (#96). run_rules takes this instead of a
    directory so a report.verify call never touches disk or re-execs a custom-check module."""
    yaml_rules: tuple[Rule, ...]
    custom_checks: tuple


def load_rules(rules_dir: Path) -> LoadedRules:
    """Load and validate the whole library in one shot. Raises (naming the offending file, via
    load_yaml_rules #43) on a malformed rule. The handler calls this at import time, so a bad
    rule refuses to BOOT the agent instead of failing every report.verify behind Temporal's
    unbounded activity retry, which is how a YAML typo used to become a quietly parked study."""
    return LoadedRules(
        yaml_rules=tuple(load_yaml_rules(rules_dir)),
        custom_checks=tuple(load_custom_checks(rules_dir / "custom")),
    )


def run_rules(ctx: dict, rules: LoadedRules) -> tuple[str, bool, list[dict]]:
    issues: list[dict] = []
    for rule in rules.yaml_rules:
        issue = evaluate(rule, ctx)
        if issue:
            issues.append(issue)
    for check in rules.custom_checks:
        issue = check(ctx)
        if issue:
            issues.append(issue)

    severities = {i["severity"] for i in issues}
    status = "FAIL" if "FAIL" in severities else "WARN" if "WARN" in severities else "PASS"
    requires_human_review = status in ("WARN", "FAIL")
    return status, requires_human_review, issues
