"""Bootstrap the AI authorship-stamp concepts in the OpenMRS concept
dictionary.

Two concepts, each at a stable, well-known UUID so its client-side default
(see ``libs/radagent-common/radagent_common/fhir_client.py``) can be a fixed
configuration value across deployments:

* "AI pre-sign impression draft" (``FHIR2_PRESIGN_REPORT_CONCEPT``) -- the
  ``DiagnosticReport.code`` stamp on the pre-sign draft (#26/#55).
* "AI critical result notification" (``FHIR2_CRITICAL_NOTIFICATION_CONCEPT``)
  -- the ``Observation.code`` stamp on the in-EHR critical-result
  notification the ehr-inbox channel writes (#79). Datatype **Text**,
  because that Observation carries a ``valueString`` and fhir2 refuses an
  obs whose value does not match its concept's datatype.

Run once at stack startup as a docker-compose one-shot service that depends
on the ``mariadb`` and ``openmrs`` services being healthy. (The compose
service and this file keep their original ``presign``-era names: the compose
mount and the #55 drift test reference them, and a rename would churn both
for no behavioural gain.)

Idempotent per concept: a concept already present at its target UUID is
skipped without being touched. Safe to run on every ``docker compose up``.
See ``docker/openmrs/README.md`` and ``docs/presign-concept.md`` for the
design rationale.

Why direct SQL rather than the OpenMRS REST endpoint
----------------------------------------------------
The OpenMRS ``POST /ws/rest/v1/concept`` endpoint auto-assigns UUIDs and
does not honour a caller-supplied UUID on create -- confirmed against the
webservices.rest concept resource behaviour and the OpenMRS Talk thread
"Create object via REST API with specified UUID" (2017, still current per
the module source at the pinned openmrs version). A caller-supplied UUID
is exactly what makes ``FHIR2_PRESIGN_REPORT_CONCEPT`` a stable
configuration value across deployments -- without it, every deployment
would have a different UUID and the env-var override would have to be
hand-set post-provisioning. Direct SQL insert bypasses REST and lets us
pin the UUID.

Trade-off: SQL insert bypasses OpenMRS's Hibernate-level ``ConceptService
.saveConcept`` hooks (audit event, module notifications, second-level
cache invalidation for the specific concept row). The ``creator`` and
``date_created`` columns are populated, so the row is still identifiable.
For clinical data-entry this would be unacceptable. For a deployment-time
insert of an authorship-stamp concept whose lifecycle is "created once,
never updated, never retired" it is a reasonable exchange.

Not a workaround for a bug that needs raising upstream: the REST
behaviour is by design.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Optional

import pymysql  # pure-Python; installed via `pip install pymysql` at container start


# --- Concept metadata --------------------------------------------------------
# These UUIDs are UUID5 hashes derived from a stable name in the librehealth.org
# DNS namespace; anyone can regenerate them from the same seed:
#     ns = uuid.uuid5(uuid.NAMESPACE_DNS, "librehealth.org")
#     concept       = uuid.uuid5(ns, "lh-radiology.ai-presign-impression-draft.v1")
#     concept_name  = uuid.uuid5(ns, "lh-radiology.ai-presign-impression-draft.v1.name.en")
#     concept_desc  = uuid.uuid5(ns, "lh-radiology.ai-presign-impression-draft.v1.description.en")
# Changing ANY of these three requires a corresponding update to
# libs/radagent-common/radagent_common/fhir_client.py::_DEFAULT_PRESIGN_REPORT_CONCEPT.
PRESIGN_CONCEPT_UUID = "e3641471-3f25-57b4-ab27-a3ebc66e481e"
PRESIGN_CONCEPT_NAME_UUID = "29e05193-b2ff-558c-b753-78d405211ffb"
PRESIGN_CONCEPT_DESCRIPTION_UUID = "51a62a88-c4f7-54f0-8a0f-936d2343234b"

PRESIGN_CONCEPT_NAME = "AI pre-sign impression draft"
PRESIGN_CONCEPT_DESCRIPTION = (
    "Authorship stamp for AI-generated pre-sign impression drafts written to "
    "DiagnosticReport.code by the LH-Radiology orchestrator. Not a clinical "
    "diagnosis; identifies the source of the draft so it can be safely updated "
    "on re-run without overwriting a radiologist's own preliminary draft."
)

# The critical-result notification concept (#79) derives the same way:
#     concept       = uuid.uuid5(ns, "lh-radiology.ai-critical-result-notification.v1")
#     concept_name  = uuid.uuid5(ns, "lh-radiology.ai-critical-result-notification.v1.name.en")
#     concept_desc  = uuid.uuid5(ns, "lh-radiology.ai-critical-result-notification.v1.description.en")
# Changing ANY of these three requires a corresponding update to
# libs/radagent-common/radagent_common/fhir_client.py::_DEFAULT_CRITICAL_NOTIFICATION_CONCEPT.
NOTIFICATION_CONCEPT_UUID = "ea215431-5e85-5040-adf0-1da297c154c3"
NOTIFICATION_CONCEPT_NAME_UUID = "ac13adf6-ff97-50bc-8d74-0e221075ad51"
NOTIFICATION_CONCEPT_DESCRIPTION_UUID = "0a55837a-b562-5b85-b313-eceafbfc90c1"

NOTIFICATION_CONCEPT_NAME = "AI critical result notification"
NOTIFICATION_CONCEPT_DESCRIPTION = (
    "Authorship stamp for the in-EHR critical-result notification Observation "
    "written by the LH-Radiology communications agent (#79). Not a clinical "
    "finding; identifies the source so a re-run updates its own notification "
    "and never touches clinician-authored data."
)

# OpenMRS reference UUIDs -- stable across every OpenMRS install (present in
# openmrs-core seed data since 2004). We look up the numeric IDs at insert time
# because concept_datatype_id and concept_class_id are auto-increment and not
# guaranteed to be stable (they usually are, but relying on the ID would be
# fragile).
DATATYPE_NA_UUID = "8d4a4c94-c2cc-11de-8d13-0010c6dffd0f"       # "N/A" -- label-only concept
DATATYPE_TEXT_UUID = "8d4a4ab4-c2cc-11de-8d13-0010c6dffd0f"     # "Text" -- carries obs value_text
CLASS_DIAGNOSIS_UUID = "8d4918b0-c2cc-11de-8d13-0010c6dffd0f"   # "Diagnosis" -- mirrors CIEL Provisional
CLASS_MISC_UUID = "8d492774-c2cc-11de-8d13-0010c6dffd0f"        # "Misc" -- a delivery artifact, not a diagnosis

# One row per provisioned concept, so adding concept N+1 is a table row -- not
# another copy of the INSERT choreography.
_CONCEPTS = [
    {
        "uuid": PRESIGN_CONCEPT_UUID,
        "name_uuid": PRESIGN_CONCEPT_NAME_UUID,
        "description_uuid": PRESIGN_CONCEPT_DESCRIPTION_UUID,
        "name": PRESIGN_CONCEPT_NAME,
        "description": PRESIGN_CONCEPT_DESCRIPTION,
        "datatype_uuid": DATATYPE_NA_UUID,
        "class_uuid": CLASS_DIAGNOSIS_UUID,
    },
    {
        "uuid": NOTIFICATION_CONCEPT_UUID,
        "name_uuid": NOTIFICATION_CONCEPT_NAME_UUID,
        "description_uuid": NOTIFICATION_CONCEPT_DESCRIPTION_UUID,
        "name": NOTIFICATION_CONCEPT_NAME,
        "description": NOTIFICATION_CONCEPT_DESCRIPTION,
        "datatype_uuid": DATATYPE_TEXT_UUID,
        "class_uuid": CLASS_MISC_UUID,
    },
]


# The radiology module refuses to render any order/report page until
# `radiology.radiologyConceptClasses` names the concept classes orderable as
# radiology procedures (IllegalStateException "Configuration required",
# live-verified 2026-07-22 on a fresh boot). The module registers the global
# property but ships it EMPTY, so every fresh stack needs this one-time set.
# Resolved by class NAME, not a hardcoded uuid, so it holds even if a future
# o3 image regenerates class uuids.
RADIOLOGY_CONCEPT_CLASSES_GP = "radiology.radiologyConceptClasses"

# The legacy patient dashboard's Overview tab renders the latest obs for the concepts listed
# here, and nothing else surfaces a bare Observation: the dashboard's other widgets are
# encounter-driven. Ships unset on this build, which is why the critical-result notification
# was written correctly, returned by fhir2, and invisible to the physician it was written for
# (#123, found in the #76 arc 2 rehearsal when a referring physician opened the chart and
# found nothing). The ack link lives inside that entry, so the closing loop of the critical
# result pathway was unreachable without someone passing the link along out of band.
OVERVIEW_SHOW_CONCEPTS_GP = "dashboard.overview.showConcepts"
RADIOLOGY_PROCEDURE_CLASS_NAME = "Radiology/Imaging Procedure"


log = logging.getLogger("bootstrap_presign_concept")


def _connect_with_retry(
    host: str, database: str, user: str, password: str,
    attempts: int = 30, delay_seconds: float = 2.0,
) -> pymysql.connections.Connection:
    """Connect to mariadb with a bounded retry loop.

    docker-compose ``depends_on: {condition: service_healthy}`` on mariadb
    means we should not normally hit connection errors -- but a slow network
    or a mariadb still finalising warmup can cause the first connect to
    fail. Retry a few times before giving up. Never blocks indefinitely.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return pymysql.connect(
                host=host,
                database=database,
                user=user,
                password=password,
                autocommit=False,
                connect_timeout=5,
            )
        except pymysql.Error as e:
            last_error = e
            log.info("mariadb not yet reachable (attempt %d/%d): %s", attempt, attempts, e)
            time.sleep(delay_seconds)
    raise RuntimeError(
        f"mariadb never became reachable at {host} after {attempts} attempts: {last_error}"
    )


def _provision_concept(cursor, spec: dict, admin_user_id: int) -> Optional[int]:
    """Provision ONE concept from its `_CONCEPTS` row. Returns its concept_id,
    or None when the OpenMRS reference seed data (datatype/class) is missing.
    Idempotent: a concept already present at the target UUID is left untouched.
    """
    cursor.execute(
        "SELECT concept_id FROM concept WHERE uuid = %s", (spec["uuid"],),
    )
    row = cursor.fetchone()
    if row is not None:
        concept_id = row[0]
        log.info(
            "Concept %s already exists (concept_id=%d), skipping insert.",
            spec["uuid"], concept_id,
        )
        return concept_id

    # Resolve the reference datatype UUID -> ID.
    cursor.execute(
        "SELECT concept_datatype_id FROM concept_datatype WHERE uuid = %s",
        (spec["datatype_uuid"],),
    )
    row = cursor.fetchone()
    if row is None:
        log.error(
            "Reference concept datatype (UUID %s) not found. The OpenMRS core "
            "seed data appears not to be loaded. Bring up the openmrs service first "
            "and wait for its healthcheck to pass before running this bootstrap.",
            spec["datatype_uuid"],
        )
        return None
    datatype_id = row[0]

    # Resolve the reference class UUID -> ID.
    cursor.execute(
        "SELECT concept_class_id FROM concept_class WHERE uuid = %s",
        (spec["class_uuid"],),
    )
    row = cursor.fetchone()
    if row is None:
        log.error(
            "Reference concept class (UUID %s) not found. Same likely cause "
            "as a datatype miss.",
            spec["class_uuid"],
        )
        return None
    class_id = row[0]

    # Insert the concept row.
    cursor.execute(
        """
        INSERT INTO concept
            (retired, datatype_id, class_id, is_set, creator, date_created, uuid)
        VALUES (0, %s, %s, 0, %s, NOW(), %s)
        """,
        (datatype_id, class_id, admin_user_id, spec["uuid"]),
    )
    concept_id = cursor.lastrowid

    # Insert the fully-specified English name.
    cursor.execute(
        """
        INSERT INTO concept_name
            (concept_id, name, locale, locale_preferred, creator, date_created,
             concept_name_type, voided, uuid)
        VALUES (%s, %s, 'en', 1, %s, NOW(), 'FULLY_SPECIFIED', 0, %s)
        """,
        (concept_id, spec["name"], admin_user_id, spec["name_uuid"]),
    )

    # Insert the English description.
    cursor.execute(
        """
        INSERT INTO concept_description
            (concept_id, description, locale, creator, date_created, uuid)
        VALUES (%s, %s, 'en', %s, NOW(), %s)
        """,
        (
            concept_id, spec["description"],
            admin_user_id, spec["description_uuid"],
        ),
    )

    log.info(
        "Provisioned concept %s (concept_id=%d, name=%r).",
        spec["uuid"], concept_id, spec["name"],
    )
    return concept_id


def _configure_radiology_concept_classes(cursor) -> bool:
    """Point `radiology.radiologyConceptClasses` at the Radiology/Imaging
    Procedure concept class. Returns False only on the loud-failure case.

    Idempotent and non-clobbering:
      * GP row absent -> radiology module not installed; nothing to configure
        (info, success).
      * GP already set -> an operator's choice; never overwritten (info, success).
      * GP present but empty, class found -> set it (the fresh-boot fix).
      * GP present but empty, class MISSING -> the module is installed but its
        seed is off; order pages will 500. Loud failure so CI/ops sees it.
    """
    cursor.execute(
        "SELECT property_value FROM global_property WHERE property = %s",
        (RADIOLOGY_CONCEPT_CLASSES_GP,),
    )
    row = cursor.fetchone()
    if row is None:
        log.info("%s not registered (radiology module absent); skipping.",
                 RADIOLOGY_CONCEPT_CLASSES_GP)
        return True
    if row[0]:
        log.info("%s already set (%r); leaving the operator's value untouched.",
                 RADIOLOGY_CONCEPT_CLASSES_GP, row[0])
        return True

    cursor.execute(
        "SELECT uuid FROM concept_class WHERE name = %s AND retired = 0 LIMIT 1",
        (RADIOLOGY_PROCEDURE_CLASS_NAME,),
    )
    row = cursor.fetchone()
    if row is None:
        log.error(
            "%s is empty and the %r concept class is missing: the radiology "
            "module is installed but its seed data is not loaded, and every "
            "order/report page will fail with 'Configuration required'.",
            RADIOLOGY_CONCEPT_CLASSES_GP, RADIOLOGY_PROCEDURE_CLASS_NAME,
        )
        return False

    cursor.execute(
        "UPDATE global_property SET property_value = %s WHERE property = %s",
        (row[0], RADIOLOGY_CONCEPT_CLASSES_GP),
    )
    log.info("Set %s = %s (%r).", RADIOLOGY_CONCEPT_CLASSES_GP, row[0],
             RADIOLOGY_PROCEDURE_CLASS_NAME)
    return True


def _show_notification_on_the_patient_overview(cursor) -> bool:
    """Add the critical-result notification concept to `dashboard.overview.showConcepts` (#123).

    APPENDS rather than replaces, unlike `_configure_radiology_concept_classes` above. That GP is
    a single value where an operator's choice is the answer; this one is a LIST, so "already set"
    is not a reason to skip -- it would leave the notification invisible on exactly the
    deployments that use the overview for something else. Ours is added once and any existing
    entries are preserved in order.

    Never a hard failure: a chart that does not surface the notification is a real problem, but it
    is not a reason to fail stack startup and take the whole demo with it. The concept row itself
    is provisioned earlier in this same run, so a miss here means the GP is absent (module//core
    version without it), which is worth a warning and nothing more.
    """
    cursor.execute(
        "SELECT concept_id FROM concept WHERE uuid = %s AND retired = 0 LIMIT 1",
        (NOTIFICATION_CONCEPT_UUID,),
    )
    row = cursor.fetchone()
    if row is None:
        log.warning("notification concept %s not found; cannot add it to %s.",
                    NOTIFICATION_CONCEPT_UUID, OVERVIEW_SHOW_CONCEPTS_GP)
        return True
    concept_id = str(row[0])

    cursor.execute(
        "SELECT property_value FROM global_property WHERE property = %s",
        (OVERVIEW_SHOW_CONCEPTS_GP,),
    )
    row = cursor.fetchone()
    if row is None:
        log.warning("%s is not registered on this OpenMRS; the critical-result notification "
                    "will not appear on the patient overview.", OVERVIEW_SHOW_CONCEPTS_GP)
        return True

    current = (row[0] or "").strip()
    entries = [e.strip() for e in current.split(",") if e.strip()]
    if concept_id in entries:
        log.info("%s already lists the notification concept (%s); nothing to do.",
                 OVERVIEW_SHOW_CONCEPTS_GP, concept_id)
        return True

    entries.append(concept_id)
    value = ",".join(entries)
    cursor.execute(
        "UPDATE global_property SET property_value = %s WHERE property = %s",
        (value, OVERVIEW_SHOW_CONCEPTS_GP),
    )
    log.info("Set %s = %r (added the critical-result notification concept %s).",
             OVERVIEW_SHOW_CONCEPTS_GP, value, concept_id)
    return True


def bootstrap(
    host: str, database: str, user: str, password: str,
) -> int:
    """Return exit code. 0 = success (idempotent no-op or created), non-zero = failure."""
    conn = _connect_with_retry(host=host, database=database, user=user, password=password)
    try:
        with conn.cursor() as cursor:
            # Get the admin user id for the audit columns. Fall back to user_id=1 (the OpenMRS
            # default admin), which every seeded install has.
            cursor.execute("SELECT user_id FROM users WHERE system_id = %s LIMIT 1", ("admin",))
            row = cursor.fetchone()
            admin_user_id = row[0] if row is not None else 1

            for spec in _CONCEPTS:
                if _provision_concept(cursor, spec, admin_user_id) is None:
                    conn.rollback()
                    return 2

            if not _configure_radiology_concept_classes(cursor):
                conn.rollback()
                return 2

            # Runs AFTER the concepts exist: it looks the notification concept up by uuid.
            _show_notification_on_the_patient_overview(cursor)

            conn.commit()
            return 0
    except pymysql.Error as e:
        conn.rollback()
        log.exception("Bootstrap failed with a mariadb error: %s", e)
        return 3
    finally:
        conn.close()


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=os.environ.get("OMRS_DB_HOSTNAME", "mariadb"))
    parser.add_argument("--database", default=os.environ.get("OMRS_DB_NAME", "openmrs"))
    parser.add_argument("--user", default=os.environ.get("OMRS_DB_USERNAME", "openmrs"))
    parser.add_argument("--password", default=os.environ.get("OMRS_DB_PASSWORD", "openmrs"))
    return parser.parse_args(argv)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = _parse_args()
    return bootstrap(
        host=args.host, database=args.database, user=args.user, password=args.password,
    )


if __name__ == "__main__":
    sys.exit(main())
