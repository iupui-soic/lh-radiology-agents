"""Focused tests for declarative rule comparisons."""
from pathlib import Path

import pytest

from rules.engine import Rule, evaluate, load_rules, load_yaml_rules


def _comparison_rule(op: str, value: object) -> Rule:
    return Rule(
        id=f"test-{op}",
        severity="WARN",
        when={"field": "report.value", "op": op, "value": value},
        message="Comparison matched.",
    )


@pytest.mark.parametrize(
    ("op", "field_value", "rule_value"),
    [
        ("contains", 42, "finding"),
        ("gt", "unexpected text", 5),
        ("lt", [1, 2], 5),
    ],
)
def test_mismatched_comparison_types_do_not_fire(op, field_value, rule_value):
    rule = _comparison_rule(op, rule_value)

    assert evaluate(rule, {"report": {"value": field_value}}) is None


@pytest.mark.parametrize(
    ("op", "field_value", "rule_value"),
    [
        ("contains", "critical finding", "finding"),
        ("gt", 6, 5),
        ("lt", 4, 5),
    ],
)
def test_compatible_comparisons_still_fire(op, field_value, rule_value):
    rule = _comparison_rule(op, rule_value)

    assert evaluate(rule, {"report": {"value": field_value}}) is not None


def _write_rule(path: Path, *, rule_id: str = "test-rule", severity: str = "WARN",
                op: str = "exists") -> None:
    path.write_text(
        f"id: {rule_id}\n"
        f"severity: {severity}\n"
        "when:\n"
        "  field: report.value\n"
        f"  op: {op}\n"
        "message: Test rule fired.\n"
    )


def test_valid_yaml_rule_loads(tmp_path):
    _write_rule(tmp_path / "valid-rule.yaml")

    rules = load_yaml_rules(tmp_path)

    assert len(rules) == 1
    assert rules[0].id == "test-rule"
    assert rules[0].severity == "WARN"
    assert rules[0].when["op"] == "exists"


def test_bad_severity_names_rule_file(tmp_path):
    path = tmp_path / "bad-severity.yaml"
    _write_rule(path, severity="WARNING")

    with pytest.raises(ValueError, match=r"bad-severity\.yaml: unknown severity 'WARNING'"):
        load_yaml_rules(tmp_path)


def test_bad_op_names_rule_file(tmp_path):
    path = tmp_path / "bad-op.yaml"
    _write_rule(path, op="equal")

    with pytest.raises(ValueError, match=r"bad-op\.yaml: unknown op 'equal'"):
        load_yaml_rules(tmp_path)


def test_duplicate_id_names_both_rule_files(tmp_path):
    _write_rule(tmp_path / "first.yaml", rule_id="duplicate-id")
    _write_rule(tmp_path / "second.yaml", rule_id="duplicate-id")

    with pytest.raises(ValueError) as exc_info:
        load_yaml_rules(tmp_path)

    message = str(exc_info.value)
    assert "second.yaml: duplicate rule id 'duplicate-id'" in message
    assert "already defined in first.yaml" in message


@pytest.mark.parametrize("missing_key", ["id", "when"])
def test_missing_required_key_names_rule_file(tmp_path, missing_key):
    contents = {
        "id": "id: test-rule\n",
        "when": "when:\n  field: report.value\n  op: exists\n",
    }
    path = tmp_path / f"missing-{missing_key}.yaml"
    path.write_text("".join(value for key, value in contents.items() if key != missing_key))

    with pytest.raises(
        ValueError,
        match=rf"missing-{missing_key}\.yaml: missing required key '{missing_key}'",
    ):
        load_yaml_rules(tmp_path)


def test_shipped_yaml_rules_still_load():
    rules_dir = Path(__file__).parents[1] / "rules"
    yaml_files = list(rules_dir.glob("*.yaml"))

    rules = load_yaml_rules(rules_dir)

    assert len(rules) == len(yaml_files)


# --- load-once (#96): the library is loaded and validated at agent boot, not per verify call ----


def test_load_rules_bundles_the_whole_library():
    rules_dir = Path(__file__).parents[1] / "rules"

    loaded = load_rules(rules_dir)

    assert len(loaded.yaml_rules) == len(list(rules_dir.glob("*.yaml")))
    assert loaded.custom_checks  # the shipped custom checks came along
    assert all(callable(c) for c in loaded.custom_checks)


def test_load_rules_raises_on_a_malformed_rule_naming_the_file(tmp_path):
    """The boot-refusal path: handler.py calls load_rules at import, so this raise IS the failed
    agent start the #96 acceptance asks for (instead of a per-request failure that Temporal's
    unbounded retry turns into a quietly parked study)."""
    (tmp_path / "custom").mkdir()
    _write_rule(tmp_path / "bad-severity.yaml", severity="WARNING")

    with pytest.raises(ValueError, match=r"bad-severity\.yaml: unknown severity 'WARNING'"):
        load_rules(tmp_path)
