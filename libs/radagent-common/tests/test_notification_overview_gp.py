"""Guard: the critical-result notification is registered on the patient overview (#123).

The notification Observation was written correctly, returned by fhir2, and INVISIBLE to the
referring physician it was written for: the legacy patient dashboard renders a bare obs only via
`dashboard.overview.showConcepts`, and that GP ships unset. The ack link lives inside that chart
entry, so the closing loop of the critical-result pathway could not be reached without someone
passing the link along by hand. Found driving arc 2 in a browser during the #76 rehearsal.

Lives in `ris-poller-tests` alongside test_presign_concept_drift.py for the same reason: a guard
CI never runs is not a guard.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP_PATH = REPO_ROOT / "docker" / "openmrs" / "bootstrap_presign_concept.py"


def _load_bootstrap_module():
    """Import the bootstrap by path, stubbing pymysql (not installed in this test env; the
    script installs it at container start)."""
    if "pymysql" not in sys.modules:
        stub = types.ModuleType("pymysql")
        stub.Error = Exception
        stub.connections = types.SimpleNamespace(Connection=object)
        sys.modules["pymysql"] = stub
    spec = importlib.util.spec_from_file_location("bootstrap_presign_concept", BOOTSTRAP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCursor:
    """Answers the two SELECTs the function makes, and records the UPDATE."""

    def __init__(self, concept_id=42, gp_value="", gp_registered=True, concept_found=True):
        self.concept_id, self.gp_value = concept_id, gp_value
        self.gp_registered, self.concept_found = gp_registered, concept_found
        self.updates: list[tuple] = []
        self._next = None

    def execute(self, sql, params=()):
        if sql.startswith("SELECT concept_id"):
            self._next = (self.concept_id,) if self.concept_found else None
        elif sql.startswith("SELECT property_value"):
            self._next = (self.gp_value,) if self.gp_registered else None
        elif sql.startswith("UPDATE global_property"):
            self.updates.append(params)
            self._next = None

    def fetchone(self):
        return self._next


def _run(cursor):
    mod = _load_bootstrap_module()
    assert mod._show_notification_on_the_patient_overview(cursor) is True
    return cursor


def test_an_unset_gp_gets_the_notification_concept():
    """The shipped state, and the bug: nothing listed means nothing renders."""
    cur = _run(FakeCursor(concept_id=42, gp_value=""))
    assert cur.updates == [("42", "dashboard.overview.showConcepts")]


def test_an_operators_existing_concepts_are_preserved_not_replaced():
    """This GP is a LIST. Skipping when it is non-empty (the single-value convention used for
    radiology.radiologyConceptClasses) would leave the notification invisible on exactly the
    deployments that already use the overview for something else."""
    cur = _run(FakeCursor(concept_id=42, gp_value="7, 9"))
    assert cur.updates == [("7,9,42", "dashboard.overview.showConcepts")]


def test_it_is_idempotent():
    """Runs on every stack start; must not grow the list each boot."""
    cur = _run(FakeCursor(concept_id=42, gp_value="7,42,9"))
    assert cur.updates == []


def test_a_missing_gp_is_a_warning_not_a_startup_failure():
    """A chart that does not surface the notification is a real problem, but it is not a reason
    to fail stack startup and take the whole demo with it."""
    cur = _run(FakeCursor(gp_registered=False))
    assert cur.updates == []


def test_a_missing_concept_is_a_warning_not_a_startup_failure():
    cur = _run(FakeCursor(concept_found=False))
    assert cur.updates == []


def test_the_gp_name_is_the_one_the_legacy_dashboard_reads():
    """Pin the property name: a typo here is silent, and the symptom is an empty chart."""
    mod = _load_bootstrap_module()
    assert mod.OVERVIEW_SHOW_CONCEPTS_GP == "dashboard.overview.showConcepts"
