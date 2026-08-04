"""Guard: every LOINC the EHR Assistant searches must be provisioned with a concept map (#84).

The EHR Assistant finds labs with `Observation?code=http://loinc.org|<code>` (see
`Fhir2Client.search_observations`). fhir2 matches that token only against codings the concept
actually carries, so a lab concept with no LOINC reference map is one whose observations are
invisible to the assistant -- the rows sit in the DB and `relevantLabs` comes back empty.

That failure is silent by construction: `handler._degrade` turns the empty search into an empty
list rather than an error, so nothing surfaces it. The only signal is a demo where the pre-read
EHR packet is quietly missing the labs it was supposed to show.

Two lists therefore have to agree, and they live in different trees:
  - `agents/ehr-assistant/handler.py::_LAB_LOINCS`  -- what the assistant asks for
  - `bootstrap_radiology_concept.LAB_LOINC_TO_CONCEPT` -- what the ETL provisions maps for
A code in the first but not the second is a lab the assistant can never see. This test is that
pin. It runs in the `mimic-tests` lane, which does install this suite -- "a guard CI never runs
is not a guard" (Pranathi, in review of #55).

The handler is read with `ast` rather than imported: it pulls in `radagent_common`, which the
slim mimic-tests lane deliberately does not install.
"""
import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bootstrap_radiology_concept as B  # noqa: E402

REPO_ROOT = HERE.parents[2]
HANDLER_PATH = REPO_ROOT / "agents" / "ehr-assistant" / "handler.py"


def _literal_module_assignments(path: pathlib.Path) -> dict:
    """Module-level `NAME = <literal>` assignments, without importing the module.

    Only plain literals are captured; anything built by a call or comprehension is skipped,
    which is fine because the panel pieces we need are a bare string and a list of strings.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            out[target.id] = ast.literal_eval(node.value)
        except ValueError:
            continue  # not a literal (e.g. the starred _LAB_LOINCS join) -- rebuilt below
    return out


def _assistant_panel() -> list[str]:
    """The LOINCs `_LAB_LOINCS` resolves to.

    `_LAB_LOINCS = [_CREATININE_LOINC, *_EGFR_LOINCS]` is a starred join, not a literal, so it
    is reassembled from its two literal parts rather than evaluated.
    """
    values = _literal_module_assignments(HANDLER_PATH)
    assert "_CREATININE_LOINC" in values, (
        "_CREATININE_LOINC not found as a module-level literal in handler.py; "
        "if the panel was restructured, update this guard."
    )
    assert "_EGFR_LOINCS" in values, (
        "_EGFR_LOINCS not found as a module-level literal in handler.py; "
        "if the panel was restructured, update this guard."
    )
    return [values["_CREATININE_LOINC"], *values["_EGFR_LOINCS"]]


def test_every_searched_loinc_is_provisioned_or_deliberately_not():
    """The pin itself. A code the assistant searches but the bootstrap does not map is a lab
    that silently never appears in the pre-read EHR packet, unless leaving it unmapped was a
    decision someone wrote down. `LAB_LOINC_DELIBERATELY_UNMAPPED` is that written record, so
    the guard still catches an accidental omission while allowing a considered one."""
    panel = set(_assistant_panel())
    provisioned = set(B.LAB_LOINC_TO_CONCEPT)

    missing = sorted(panel - provisioned - set(B.LAB_LOINC_DELIBERATELY_UNMAPPED))
    assert not missing, (
        f"_LAB_LOINCS searches {missing} but LAB_LOINC_TO_CONCEPT provisions no concept map "
        f"for them, so observations under those codes are invisible to the EHR Assistant. "
        f"Add them to LAB_LOINC_TO_CONCEPT, or to LAB_LOINC_DELIBERATELY_UNMAPPED with a "
        f"reason, in bootstrap_radiology_concept.py."
    )


def test_deliberately_unmapped_codes_are_real_panel_codes():
    """A code parked in LAB_LOINC_DELIBERATELY_UNMAPPED that the panel no longer searches is
    dead weight, and worse, it would silence the guard for a code that later comes back."""
    panel = set(_assistant_panel())
    strays = sorted(set(B.LAB_LOINC_DELIBERATELY_UNMAPPED) - panel)
    assert not strays, (
        f"LAB_LOINC_DELIBERATELY_UNMAPPED lists {strays}, which _LAB_LOINCS does not search. "
        f"Drop them, or the exemption outlives the thing it was exempting."
    )


def test_deliberately_unmapped_codes_are_not_also_mapped():
    """The two lists must not overlap, or the exemption is a lie about what gets provisioned."""
    overlap = sorted(set(B.LAB_LOINC_DELIBERATELY_UNMAPPED) & set(B.LAB_LOINC_TO_CONCEPT))
    assert not overlap, (
        f"{overlap} appear in both LAB_LOINC_TO_CONCEPT and LAB_LOINC_DELIBERATELY_UNMAPPED."
    )


def test_provisioned_codes_all_target_a_known_concept():
    """Every mapped code must point at a concept the bootstrap actually creates. A typo'd UUID
    would provision no map at all, reintroducing #84 for that code."""
    known = {spec["uuid"] for spec in B.CONCEPTS}
    strays = sorted(code for code, uuid_ in B.LAB_LOINC_TO_CONCEPT.items() if uuid_ not in known)
    assert not strays, (
        f"LAB_LOINC_TO_CONCEPT maps {strays} onto UUIDs that are not in CONCEPTS, so "
        f"provision() never creates them."
    )


def test_lab_concepts_are_numeric():
    """Labs are numeric observations. fhir2 rejects a numeric obs against a non-numeric concept,
    so a datatype slip here breaks the load rather than just the search."""
    targets = set(B.LAB_LOINC_TO_CONCEPT.values())
    for spec in B.CONCEPTS:
        if spec["uuid"] in targets:
            assert spec["numeric"] is not None, (
                f"{spec['name']} is a LOINC-mapped lab concept but is provisioned with "
                f"numeric=None; lab observations carry decimal values."
            )
