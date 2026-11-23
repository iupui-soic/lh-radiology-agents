"""validate_skill_output on an unknown skill id names it and the known domains (#46)."""
from __future__ import annotations

import pytest

from radagent_common.validation import ContractError, validate_skill_output


def test_unknown_skill_id_names_the_skill_and_the_known_domains():
    with pytest.raises(ContractError) as raised:
        validate_skill_output("triag.score", {})
    message = str(raised.value)
    assert message.startswith("no contract schema for skill 'triag.score'; known skill domains: ")
    domains = message.split("known skill domains: ", 1)[1].split(", ")
    # comms has a schema per skill (comms.dispatch, ...); it is listed once, as a domain.
    assert {"comms", "triage"} <= set(domains)
    assert domains == sorted(set(domains))


def test_a_known_skill_id_still_validates_against_its_schema():
    # The schema is found, so it is the payload that fails.
    with pytest.raises(ContractError) as raised:
        validate_skill_output("triage.score", {})
    assert str(raised.value).startswith("triage.schema.json: ")
