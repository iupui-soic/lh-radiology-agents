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

# The AI authorship stamp -- must match `FHIR2_PRESIGN_REPORT_CONCEPT` in
# libs/radagent-common/radagent_common/fhir_client.py and the UUID provisioned by
# docker/openmrs/bootstrap_presign_concept.py. A DiagnosticReport without this exact
# concept code is a human draft (or someone else's system) and is NOT ours to touch.
AI_PRESIGN_CONCEPT_UUID = os.environ.get(
    "AI_PRESIGN_CONCEPT_UUID",
    "e3641471-3f25-57b4-ab27-a3ebc66e481e",
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


def insert_draft(conn, order_id: int, body: str) -> int:
    """Create a fresh DRAFT radiology_report row so the radiology module's Report
    form shows the AI's Diagnosis text on first open. creator=1 is the seed
    superuser (o3 demo dictionary); voided defaults to 0 but we set it explicitly
    to be self-documenting.
    """
    row_uuid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO radiology_report "
            "(order_id, report_status, report_body, creator, date_created, uuid, voided) "
            "VALUES (%s, 'DRAFT', %s, 1, NOW(), %s, 0)",
            (order_id, body, row_uuid),
        )
        return cur.lastrowid


def update_draft_body(conn, report_id: int, body: str) -> None:
    """Fill in an empty DRAFT row's report_body. Never runs on a row with existing
    text or a non-DRAFT status -- caller enforces that; this function is dumb on
    purpose so an accidental call from the wrong branch cannot silently overwrite.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE radiology_report "
            "SET report_body=%s, date_changed=NOW() "
            "WHERE report_id=%s",
            (body, report_id),
        )


def has_our_stamp(resource: dict) -> bool:
    """Whether this DiagnosticReport was written by our AI presign path.

    The discriminator is the concept code on `code.coding` -- mirrors the
    `_find_presign_draft` check in libs/radagent-common/radagent_common/fhir_client.py.
    A resource without our stamp is either a radiologist's own preliminary draft or
    another system's output; either way, not ours to bridge.
    """
    codes = [c.get("code") for c in ((resource.get("code") or {}).get("coding") or [])]
    return AI_PRESIGN_CONCEPT_UUID in codes


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


def bridge_report(c: OmrsClient, conn, resource: dict) -> str:
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
        new_id = insert_draft(conn, order_id, conclusion)
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
    update_draft_body(conn, report_id, conclusion)
    return f"{fhir_id}: update report_id={report_id}"


def main() -> None:
    print(f"ris-presign-bridge up; polling every {POLL_SECONDS}s", flush=True)
    print(f"  AI concept stamp: {AI_PRESIGN_CONCEPT_UUID}", flush=True)

    c = OmrsClient()
    conn = None
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

            reports = poll_fhir_reports(c, cursor_iso)
            for r in reports:
                try:
                    outcome = bridge_report(c, conn, r)
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
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
