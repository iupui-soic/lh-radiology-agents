"""Bootstrap the read-only referring-physician access (#85).

The radiology module ships its ``Radiology: Referring physician`` role with ZERO privileges, so
a fresh referring-physician login can see nothing at all, not even the navigation gutter. The
working privilege set only ever existed as hand-built DB state on the showcase host. This script
makes that state reproducible:

1. Grants the role the proven read-only privilege set (captured verbatim from the working
   showcase host on 2026-08-07; every entry is a Get/View/dashboard-section privilege, no write
   privileges). Grant-only and idempotent: privileges an operator added stay, a re-run changes
   nothing. A privilege missing from the ``privilege`` table (its module is not installed on this
   image) is skipped with a warning, not a failure.
2. Optionally (``BOOTSTRAP_DEMO_REFERRERS=1``) provisions the showcase referring-physician
   accounts through the SAME code path the cohort ETL uses (``scripts/mimic``, mounted read-only
   at ``MIMIC_SRC``), then converges each login onto the role and verifies it loudly. Without the
   flag the role is ready and the ETL's own referrer seeding picks it up during cohort load.

Entry path note (run-book "Referring-physician access"): these logins must land on
``patientDashboard.form`` deep links. The legacy home page 500s for them (an infinite-nesting
serialization bug in upstream errorhandler.jsp), while the patient dashboard renders fine.

Least-privilege caveat: this is the PROVEN set, not a minimal one. The rehearsal-era curated 33
left the patient dashboard's Radiology tab with an empty orders table (#88: the module REST
search needs the ``Get *`` privilege family, for example ``Get Patients`` on the search handler's
patient resolve). Trimming the set again is #75 least-privilege work; verify through the real
pages before shrinking it.

Run once at stack startup as a docker-compose one-shot service (``referring-role-bootstrap``),
same family as ``bootstrap_presign_concept.py``. Direct SQL for the same reason documented there.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Optional

import pymysql  # pure-Python; installed via `pip install pymysql` at container start

ROLE_NAME = "Radiology: Referring physician"

# The proven read-only set, captured from the working showcase host (2026-08-07). Sorted, one
# privilege per line, so a future diff against a live host is mechanical:
#   SELECT privilege FROM role_privilege WHERE role='Radiology: Referring physician' ORDER BY 1;
PRIVILEGES = (
    "Get Admission Locations",
    "Get Allergies",
    "Get Bed Tags",
    "Get Bed Type",
    "Get Beds",
    "Get Care Settings",
    "Get Concept Attribute Types",
    "Get Concept Classes",
    "Get Concept Datatypes",
    "Get Concept Map Types",
    "Get Concept Proposals",
    "Get Concept Reference Terms",
    "Get Concept Sources",
    "Get Concepts",
    "Get Conditions",
    "Get Database Changes",
    "Get Diagnoses",
    "Get Diagnoses Attribute Types",
    "Get Encounter Roles",
    "Get Encounter Types",
    "Get Encounters",
    "Get Field Types",
    "Get Forms",
    "Get Global Properties",
    "Get HL7 Inbound Archive",
    "Get HL7 Inbound Exception",
    "Get HL7 Inbound Queue",
    "Get HL7 Source",
    "Get Identifier Types",
    "Get Location Attribute Types",
    "Get Locations",
    "Get Medication Dispense",
    "Get Notes",
    "Get Observations",
    "Get Order Frequencies",
    "Get Order Set Attribute Types",
    "Get Order Sets",
    "Get Order Types",
    "Get Orders",
    "Get Patient Cohorts",
    "Get Patient Identifiers",
    "Get Patient Programs",
    "Get Patients",
    "Get People",
    "Get Person Attribute Types",
    "Get Privileges",
    "Get Problems",
    "Get Procedure Types",
    "Get Procedures",
    "Get Programs",
    "Get Provider Attribute Types",
    "Get Provider Roles",
    "Get Providers",
    "Get Queue Entries",
    "Get Queue Rooms",
    "Get Queues",
    "Get Radiology Modalities",
    "Get Radiology Orders",
    "Get Radiology Report Templates",
    "Get Radiology Reports",
    "Get Radiology Studies",
    "Get Relationship Types",
    "Get Relationships",
    "Get Roles",
    "Get Users",
    "Get Visit Attribute Types",
    "Get Visit Types",
    "Get Visits",
    "Patient Dashboard - View Demographics Section",
    "Patient Dashboard - View Encounters Section",
    "Patient Dashboard - View Forms Section",
    "Patient Dashboard - View Graphs Section",
    "Patient Dashboard - View Overview Section",
    "Patient Dashboard - View Patient Summary",
    "Patient Dashboard - View Radiology Section",
    "Patient Dashboard - View Regimen Section",
    "Patient Overview - View Allergies",
    "Patient Overview - View Patient Actions",
    "Patient Overview - View Problem List",
    "Patient Overview - View Programs",
    "Patient Overview - View Relationships",
    "View Administration Functions",
    "View Allergies",
    "View Appointment Services",
    "View Appointments",
    "View Attachments",
    "View Bill Discounts",
    "View Bill Refunds",
    "View Calculations",
    "View Cashier Bills",
    "View Cashier Metadata",
    "View Cashier Timesheets",
    "View Cohorts In Cohort Module",
    "View Concept Classes",
    "View Concept Datatypes",
    "View Concept Proposals",
    "View Concept Sources",
    "View Concepts",
    "View Data Entry Statistics",
    "View Encounter Types",
    "View Encounters",
    "View Field Types",
    "View Forms",
    "View Global Properties",
    "View Identifier Types",
    "View Locations",
    "View Metadata Via Mapping",
    "View Navigation Menu",
    "View Navigation Menu - Radiology",
    "View Observations",
    "View Order Types",
    "View Orders",
    "View OrderTemplates",
    "View Patient Cohorts",
    "View Patient Identifiers",
    "View Patient Programs",
    "View Patients",
    "View People",
    "View Person Attribute Types",
    "View Privileges",
    "View Problems",
    "View Programs",
    "View Radiology Report Templates",
    "View Relationship Types",
    "View Relationships",
    "View Report Objects",
    "View Reports",
    "View RESTWS",
    "View Roles",
    "View Tasks",
    "View Token Registrations",
    "View Unpublished Forms",
    "View Users",
)

log = logging.getLogger("bootstrap_referring_role")


def _connect_with_retry(
    host: str, database: str, user: str, password: str,
    attempts: int = 30, delay_seconds: float = 2.0,
) -> pymysql.connections.Connection:
    """Same bounded retry as bootstrap_presign_concept: healthy-service depends_on should make
    this a first-try connect, but a slow warmup must not fail the one-shot."""
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return pymysql.connect(
                host=host, database=database, user=user, password=password,
                autocommit=False, connect_timeout=5,
            )
        except pymysql.Error as e:
            last_error = e
            log.info("mariadb not yet reachable (attempt %d/%d): %s", attempt, attempts, e)
            time.sleep(delay_seconds)
    raise RuntimeError(
        f"mariadb never became reachable at {host} after {attempts} attempts: {last_error}"
    )


def grant_role_privileges(conn) -> Optional[dict]:
    """Grant PRIVILEGES to ROLE_NAME. Returns counts, or None when the role does not exist
    (radiology module absent on this image; mirrors the presign bootstrap's module-absent
    handling: skip with an info line, not a failure)."""
    counts = {"granted": 0, "present": 0, "missing": 0}
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM role WHERE role = %s", (ROLE_NAME,))
        if cur.fetchone() is None:
            log.info("role %r not found (radiology module absent); skipping.", ROLE_NAME)
            return None
        cur.execute("SELECT privilege FROM role_privilege WHERE role = %s", (ROLE_NAME,))
        have = {row[0] for row in cur.fetchall()}
        for priv in PRIVILEGES:
            if priv in have:
                counts["present"] += 1
                continue
            cur.execute("SELECT privilege FROM privilege WHERE privilege = %s", (priv,))
            if cur.fetchone() is None:
                # That privilege's module is not on this image. Fine for optional modules
                # (billing, bedmanagement); the grant list is the superset of the proven host.
                log.warning("privilege %r not on this image; skipped.", priv)
                counts["missing"] += 1
                continue
            cur.execute(
                "INSERT INTO role_privilege (role, privilege) VALUES (%s, %s)",
                (ROLE_NAME, priv),
            )
            counts["granted"] += 1
    conn.commit()
    log.info("role %r: %d granted, %d already present, %d not on this image.",
             ROLE_NAME, counts["granted"], counts["present"], counts["missing"])
    return counts


def provision_demo_referrers() -> bool:
    """Create the showcase referring-physician accounts through the ETL's own machinery
    (identical idempotency keys: provider identifier and username), then converge each login
    onto ROLE_NAME and verify. Loud: a login that ends up missing or role-less returns False,
    because a silent miss here is exactly the fresh-stack archaeology #85 is about."""
    sys.path.insert(0, os.environ.get("MIMIC_SRC", "/opt/mimic"))
    from omrs_client import OmrsClient  # noqa: PLC0415 -- import after the mount-path insert
    from referrers import REFERRERS  # noqa: PLC0415

    client = OmrsClient()
    ok = True
    for ref in REFERRERS:
        username = ref["username"]
        try:
            client.ensure_referring_provider(
                username, ref["given"], ref["family"], gender=ref.get("gender", "U"))
            res = client._rget("user", {"q": username,
                                        "v": "custom:(uuid,username,roles:(uuid,display))"})
            user = next((u for u in res.get("results", [])
                         if u.get("username") == username), None)
            if user is None:
                log.error("referrer %s: login was not created (see ensure_referring_provider "
                          "warnings above).", username)
                ok = False
                continue
            roles = {r.get("display") for r in user.get("roles", [])}
            if ROLE_NAME not in roles:
                # ETL runs with an older MIMIC_REFERRER_ROLES default left Provider-only logins;
                # converge instead of failing. POST replaces the role set, so send the union.
                client._rpost(f"user/{user['uuid']}",
                              {"roles": [r["uuid"] for r in user.get("roles", [])] + [ROLE_NAME]})
                log.info("referrer %s: added role %r.", username, ROLE_NAME)
            else:
                log.info("referrer %s: present with role %r.", username, ROLE_NAME)
        except Exception as e:  # noqa: BLE001 -- one bad referrer must not hide the others
            log.error("referrer %s: %s", username, e)
            ok = False
    return ok


def bootstrap(host: str, database: str, user: str, password: str, demo_referrers: bool) -> int:
    """Exit code: 0 = success (idempotent no-op or converged), non-zero = failure."""
    conn = _connect_with_retry(host=host, database=database, user=user, password=password)
    try:
        counts = grant_role_privileges(conn)
    except pymysql.Error as e:
        conn.rollback()
        log.exception("role grant failed with a mariadb error: %s", e)
        return 3
    finally:
        conn.close()

    if counts is None:
        if demo_referrers:
            log.warning("demo referrers requested but the role is absent; not creating "
                        "Provider-only logins that would just re-open #85.")
        return 0

    if demo_referrers and not provision_demo_referrers():
        return 2
    return 0


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=os.environ.get("OMRS_DB_HOSTNAME", "mariadb"))
    parser.add_argument("--database", default=os.environ.get("OMRS_DB_NAME", "openmrs"))
    parser.add_argument("--user", default=os.environ.get("OMRS_DB_USERNAME", "openmrs"))
    parser.add_argument("--password", default=os.environ.get("OMRS_DB_PASSWORD", "openmrs"))
    parser.add_argument("--demo-referrers", action="store_true",
                        default=os.environ.get("BOOTSTRAP_DEMO_REFERRERS", "").lower()
                        in ("1", "true", "yes"))
    return parser.parse_args(argv)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = _parse_args()
    return bootstrap(host=args.host, database=args.database, user=args.user,
                     password=args.password, demo_referrers=args.demo_referrers)


if __name__ == "__main__":
    sys.exit(main())
