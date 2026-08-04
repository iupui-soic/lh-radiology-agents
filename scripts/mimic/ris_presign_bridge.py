"""fhir2 -> RIS presign bridge (M5 workaround for the o3 module's read-side gap).

The AI's pre-sign impression is written to fhir2 as a `preliminary` DiagnosticReport
stamped with our authorship concept (see docs/presign-concept.md), but the o3
radiology module's Report form reads from its own `radiology_report` mariadb table --
not from fhir2 -- so the DiagnosticReport is a shadow write that never reaches the
"Diagnosis" text field the radiologist actually opens. Without a row in
`radiology_report`, the form shows nothing and the radiologist writes from scratch.

This bridge does the read from the other direction: poll fhir2 for AI-authored
preliminary DiagnosticReports, resolve each to its OpenMRS order, and INSERT/UPDATE
a `radiology_report` row with `report_status='DRAFT'` and `report_body=<AI
conclusion>`. When the radiologist opens `radiologyReport.form?reportId=<n>`, the
Diagnosis field is already populated -- they edit or accept, hit Complete, and the
existing `ris_sign_bridge` (post-sign direction) flows the sign-off back to fhir2.

Safety rails:
- Only INSERT when no `radiology_report` row exists for the order yet -- never
  create a duplicate.
- Only UPDATE when the existing row is `DRAFT` AND its `report_body` is NULL/empty
  -- never overwrite a radiologist's own text, even if we later have a "better"
  draft. The radiologist's own work is authoritative.
- Only touch rows whose fhir2 DiagnosticReport carries OUR concept stamp -- a
  radiologist's own preliminary draft (different code) is left alone.
- Written rows are stamped to a dedicated service user (`ai-presign-bridge`), not
  to admin -- the audit trail says who wrote the draft.

Best-effort: transient fhir2/db errors are logged and the loop continues. The
radiologist can always write the report by hand; a missing draft is a graceful
degrade, not a broken workflow.
"""
from __future__ import annotations
import os
import time
import uuid

import pymysql

from omrs_client import OmrsClient

POLL_SECONDS = int(os.environ.get("BRIDGE_POLL_SECONDS", "10"))

# The AI authorship stamp -- must match the canonical concept configured on the
# orchestrator via ``FHIR2_PRESIGN_REPORT_CONCEPT`` (see
# libs/radagent-common/radagent_common/fhir_client.py:_presign_report_concept()) and
# the UUID provisioned by docker/openmrs/bootstrap_presign_concept.py. A
# DiagnosticReport without this exact concept code is a human draft (or someone
# else's system) and is NOT ours to touch.
#
# Env var name kept in lockstep with the orchestrator per !107 review: if a
# deployment moves the concept there, the bridge follows via the same knob.
FHIR2_PRESIGN_REPORT_CONCEPT = os.environ.get(
    "FHIR2_PRESIGN_REPORT_CONCEPT",
    "e3641471-3f25-57b4-ab27-a3ebc66e481e",
)

# Service user for audit stamping (!107 review). The `radiology_report` row's
# `creator`, `changed_by` (and the underlying person's `creator`) all point at this
# user's id so a bridge-written draft is distinguishable from an admin-written one
# in the audit trail. Provisioned on first bridge start if missing; the row itself
# is created as a person + person_name + users triple, unretired, no roles assigned
# (this account is never intended for interactive login, only as an authorship
# stamp). Username is overridable via env so a deployment can point at an existing
# service user rather than have the bridge auto-create.
SERVICE_USER_SYSTEM_ID = os.environ.get(
    "RIS_PRESIGN_BRIDGE_USERNAME", "ai-presign-bridge"
)


def connect_db():
    """Fresh pymysql connection, matching ris_sign_bridge's config surface."""
    return pymysql.connect(
        host=os.environ.get("OMRS_DB_HOST", "mariadb"),
        port=int(os.environ.get("OMRS_DB_PORT", "3306")),
        user=os.environ.get("OMRS_DB_USER", "openmrs"),
        password=os.environ.get("OMRS_DB_PASS", "openmrs"),
        database=os.environ.get("OMRS_DB_NAME", "openmrs"),
        autocommit=True,
    )


def ensure_service_user(conn) -> int:
    """Look up (or provision) the AI presign bridge's service user, return user_id.

    Per !107 review, the `radiology_report` row's audit columns must distinguish a
    bridge write from an admin write. Uses `system_id` as the discriminator (same
    convention `docker/openmrs/bootstrap_presign_concept.py` uses to find admin).

    Provisioning strategy on first bridge start when the user is missing:
      1. INSERT into person (gender='M' is a placeholder; OpenMRS requires a value)
      2. INSERT into person_name (given='AI', family='Presign Bridge')
      3. INSERT into users (system_id, username, person_id -> the row we just made)
    Each INSERT uses admin's user_id (1) as its own `creator` -- the seed superuser
    is the only user guaranteed to exist at bridge boot. From then on, the bridge
    stamps its own writes to the new user.

    No role assignment: this account is never intended for interactive login. If a
    deployment provisions the user out-of-band (via bootstrap or by hand) with the
    same system_id, this function finds it and skips the INSERT.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM users WHERE system_id=%s AND retired=0 LIMIT 1",
            (SERVICE_USER_SYSTEM_ID,),
        )
        row = cur.fetchone()
        if row:
            return row[0]

        # Create person
        person_uuid = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO person (gender, dead, creator, date_created, uuid, voided) "
            "VALUES ('M', 0, 1, NOW(), %s, 0)",
            (person_uuid,),
        )
        person_id = cur.lastrowid

        # Create person_name
        name_uuid = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO person_name "
            "(person_id, preferred, given_name, family_name, creator, date_created, uuid, voided) "
            "VALUES (%s, 1, %s, %s, 1, NOW(), %s, 0)",
            (person_id, "AI", "Presign Bridge", name_uuid),
        )

        # Create users row
        user_uuid = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO users "
            "(system_id, username, person_id, creator, date_created, uuid, retired) "
            "VALUES (%s, %s, %s, 1, NOW(), %s, 0)",
            (SERVICE_USER_SYSTEM_ID, SERVICE_USER_SYSTEM_ID, person_id, user_uuid),
        )
        new_user_id = cur.lastrowid
        print(
            f"  provisioned service user system_id={SERVICE_USER_SYSTEM_ID} "
            f"user_id={new_user_id} person_id={person_id}",
            flush=True,
        )
        return new_user_id


def order_id_for_uuid(conn, order_uuid: str):
    """Internal orders.order_id from its uuid -- the join key `radiology_report` needs.

    Returns None if the order is missing or voided; a bridge that can't resolve the
    order simply skips the study rather than writing an orphaned row.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT order_id FROM orders WHERE uuid=%s AND voided=0",
            (order_uuid,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def existing_report(conn, order_id: int):
    """The current radiology_report row for this order, or None.

    Returns (report_id, report_status, report_body) so the caller can decide
    whether to skip (radiologist has touched it), UPDATE (empty draft), or INSERT
    (nothing yet).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT report_id, report_status, report_body "
            "FROM radiology_report WHERE order_id=%s AND voided=0 "
            "ORDER BY report_id DESC LIMIT 1",
            (order_id,),
        )
        return cur.fetchone()


def insert_draft(conn, order_id: int, body: str, service_user_id: int) -> int:
    """Create a fresh DRAFT radiology_report row so the radiology module's Report
    form shows the AI's Diagnosis text on first open.

    `creator=service_user_id` per !107 review -- the audit trail names the bridge,
    not admin. `voided` defaults to 0 but is set explicitly to be self-documenting.
    """
    row_uuid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO radiology_report "
            "(order_id, report_status, report_body, creator, date_created, uuid, voided) "
            "VALUES (%s, 'DRAFT', %s, %s, NOW(), %s, 0)",
            (order_id, body, service_user_id, row_uuid),
        )
        return cur.lastrowid


def update_draft_body(
    conn, report_id: int, body: str, service_user_id: int
) -> None:
    """Fill in an empty DRAFT row's report_body. Never runs on a row with existing
    text or a non-DRAFT status -- caller enforces that; this function is dumb on
    purpose so an accidental call from the wrong branch cannot silently overwrite.

    `changed_by=service_user_id` per !107 review -- the audit trail names the
    bridge for the update, not admin.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE radiology_report "
            "SET report_body=%s, changed_by=%s, date_changed=NOW() "
            "WHERE report_id=%s",
            (body, service_user_id, report_id),
        )


def has_our_stamp(resource: dict) -> bool:
    """Whether this DiagnosticReport was written by our AI presign path.

    The discriminator is the concept code on `code.coding` -- mirrors the
    `_find_presign_draft` check in libs/radagent-common/radagent_common/fhir_client.py.
    A resource without our stamp is either a radiologist's own preliminary draft or
    another system's output; either way, not ours to bridge.
    """
    codes = [c.get("code") for c in ((resource.get("code") or {}).get("coding") or [])]
    return FHIR2_PRESIGN_REPORT_CONCEPT in codes


def service_request_uuid(resource: dict):
    """The ServiceRequest reference this DiagnosticReport is basedOn, or None.

    The reference format is `ServiceRequest/<uuid>` per fhir2's link convention.
    A DiagnosticReport with no basedOn is either an orphaned test or a global
    result; either way we can't route it to a specific order, so we skip it.
    """
    for ref in resource.get("basedOn") or []:
        r = ref.get("reference") or ""
        if r.startswith("ServiceRequest/"):
            return r.split("/", 1)[1]
    return None


def poll_fhir_reports(c: OmrsClient, cursor_iso: str) -> list:
    """AI-authored preliminary DiagnosticReports updated since `cursor_iso`.

    fhir2 doesn't accept `status` as a search parameter on this build (400s), same
    as the ris_sign_bridge design note observes. So we search by `_lastUpdated`
    only and filter client-side on both status and our concept stamp.
    """
    bundle = c._fget(
        "DiagnosticReport",
        {"_lastUpdated": f"ge{cursor_iso}", "_sort": "_lastUpdated", "_count": "50"},
    )
    out = []
    for entry in bundle.get("entry", []) or []:
        r = entry.get("resource") or {}
        if r.get("resourceType") != "DiagnosticReport":
            continue
        if r.get("status") != "preliminary":
            continue
        if not has_our_stamp(r):
            continue
        out.append(r)
    return out


def bridge_report(conn, resource: dict, service_user_id: int) -> str:
    """Bridge one AI DiagnosticReport into the radiology_report table.

    Returns a short outcome tag for the log line: `no-order`, `skip-touched`,
    `insert`, `update`, or `noop-same-text`. Never raises: any error becomes a
    warning tag and the poll loop continues.
    """
    fhir_id = resource.get("id") or "?"
    conclusion = (resource.get("conclusion") or "").strip()
    if not conclusion:
        return f"{fhir_id}: skip-empty"

    sr_uuid = service_request_uuid(resource)
    if not sr_uuid:
        return f"{fhir_id}: skip-no-basedOn"

    order_id = order_id_for_uuid(conn, sr_uuid)
    if order_id is None:
        return f"{fhir_id}: no-order (sr={sr_uuid})"

    current = existing_report(conn, order_id)
    if current is None:
        new_id = insert_draft(conn, order_id, conclusion, service_user_id)
        return f"{fhir_id}: insert -> report_id={new_id} order_id={order_id}"

    report_id, status, body = current
    body_stripped = (body or "").strip()

    # Radiologist has already touched it: hands off. Includes anything past DRAFT,
    # or a DRAFT that already carries text (radiologist typed something even if
    # they haven't hit Complete yet).
    if status != "DRAFT" or body_stripped:
        if body_stripped == conclusion:
            return f"{fhir_id}: noop-same-text report_id={report_id}"
        return f"{fhir_id}: skip-touched report_id={report_id} status={status}"

    # DRAFT with empty body: our first write. Fill it.
    update_draft_body(conn, report_id, conclusion, service_user_id)
    return f"{fhir_id}: update report_id={report_id}"


def main() -> None:
    print(f"ris-presign-bridge up; polling every {POLL_SECONDS}s", flush=True)
    print(f"  AI concept stamp: {FHIR2_PRESIGN_REPORT_CONCEPT}", flush=True)

    c = OmrsClient()
    conn = None
    service_user_id = None
    # Cursor: a fixed early date on first start, then bumped to the latest
    # lastUpdated we saw. Kept in-memory only -- a restart re-processes from the
    # cursor floor, which is fine because every write path is idempotent
    # (order-scoped INSERT-if-missing, radiologist-touch-preserving UPDATE).
    cursor_iso = os.environ.get("BRIDGE_START_ISO", "2026-01-01T00:00:00.000Z")

    while True:
        try:
            if conn is None:
                conn = connect_db()
            conn.ping()

            if service_user_id is None:
                service_user_id = ensure_service_user(conn)
                print(f"  service user id: {service_user_id}", flush=True)

            reports = poll_fhir_reports(c, cursor_iso)
            for r in reports:
                try:
                    outcome = bridge_report(conn, r, service_user_id)
                    print(f"  {outcome}", flush=True)
                except Exception as e:  # noqa: BLE001 -- keep the bridge alive per-item
                    print(f"  {r.get('id', '?')}: ERROR {e!r}", flush=True)

                # Advance cursor to the latest we saw (fhir2 returns ascending by _sort).
                lu = (r.get("meta") or {}).get("lastUpdated")
                if lu and lu > cursor_iso:
                    cursor_iso = lu
        except Exception as e:  # noqa: BLE001 -- transient outages must not kill the bridge
            print(f"bridge cycle error: {e!r}", flush=True)
            conn = None
            service_user_id = None  # re-run ensure on reconnect
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
