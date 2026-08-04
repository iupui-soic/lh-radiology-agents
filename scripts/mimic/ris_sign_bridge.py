"""RIS sign -> fhir2 bridge (M4 workaround for the o3 module's broken sign emit).

When a radiologist completes a report, `RadiologyReportServiceImpl.emitFhirDiagnosticReport`
throws `ServiceNotFoundException: FhirDiagnosticReportService` (the fhir2 module's service is
not resolvable from the radiology module on this build), the error is swallowed into a log
line, and the sign never reaches fhir2 -- so the RIS poller never fires and every read parks
at AWAITING_SIGNOFF forever. Found live in the M4 recording rehearsal (2026-07-25); the real
fix belongs in the o3 sibling repo.

Until that lands, this bridge does the emit from outside: poll the module's radiology_report
table for COMPLETED reports, and for each one project the authored diagnosis into the study's
seeded DiagnosticReport (conclusion = RIS text, status = final) -- the same flip the
report_seeder rehearsal cue performs, so the poller and everything post-sign behave
identically to a working module emit. Idempotent: a report whose DiagnosticReport is already
final is skipped. Runs as the `ris-sign-bridge` compose service.
"""
import html
import os
import re
import time

import pymysql

from omrs_client import OmrsClient

POLL_SECONDS = int(os.environ.get("BRIDGE_POLL_SECONDS", "10"))

# fhir2 rejects a DiagnosticReport.conclusion over 1024 chars outright (422; live-bisected:
# 1024 -> 200, 1025 -> 422), and presentedForm does not persist on this build -- so there is
# nowhere fhir2-side for the full text. The cap therefore stays, but a silent cut is not
# acceptable: Verification parses this body, and a quietly amputated report yields false
# PASS/FAIL verdicts (#91). Truncation is now marked in-band (the marker fits inside the cap)
# and logged loudly; the full text remains authoritative in the RIS only.
FHIR2_CONCLUSION_MAX = 1024
TRUNCATION_MARKER = " [TRUNCATED BY ris-sign-bridge: fhir2 caps conclusion at 1024 chars; full text in RIS]"


def strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def capped_conclusion(report_id: int, body_text: str) -> str:
    """The signed body, fitted to fhir2's hard cap -- loudly and with an in-band marker."""
    if len(body_text) <= FHIR2_CONCLUSION_MAX:
        return body_text
    print(f"report {report_id}: signed body is {len(body_text)} chars but fhir2 caps "
          f"conclusion at {FHIR2_CONCLUSION_MAX}; truncating WITH marker -- downstream "
          f"verification sees a partial body", flush=True)
    return body_text[:FHIR2_CONCLUSION_MAX - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def completed_reports(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select rr.report_id, rr.report_body, o.accession_number, pn.given_name, pn.family_name "
            "from radiology_report rr "
            "join orders o on o.order_id = rr.order_id "
            "left join provider p on p.provider_id = rr.principal_results_interpreter "
            "left join person_name pn on pn.person_id = p.person_id and pn.voided = 0 "
            "where rr.report_status = 'COMPLETED' and rr.voided = 0")
        return cur.fetchall()


def connect():
    return pymysql.connect(
        host=os.environ.get("OMRS_DB_HOST", "mariadb"),
        port=int(os.environ.get("OMRS_DB_PORT", "3306")),
        user=os.environ.get("OMRS_DB_USER", "openmrs"),
        password=os.environ.get("OMRS_DB_PASS", "openmrs"),
        database=os.environ.get("OMRS_DB_NAME", "openmrs"),
        autocommit=True)


def bridge_cycle(conn, c, bridged: set[int], missing: set[int]) -> None:
    """One pass over COMPLETED RIS reports. `bridged` holds report_ids DONE (flipped, or found
    already final); `missing` holds report_ids whose accession/seeded-report resolve has missed
    at least once, so the miss is logged exactly once and RETRIED every cycle -- a resolve miss
    is routinely transient (ETL still loading, fhir2 hiccup, order arriving late), and the old
    permanent skip turned any hiccup into a silently lost human sign until a container restart
    (#90)."""
    for report_id, body, accession, given, family in completed_reports(conn):
        if report_id in bridged:
            continue
        order = c.order_for_accession(accession)
        if not order:
            if report_id not in missing:
                print(f"report {report_id}: no order for {accession}; will keep retrying",
                      flush=True)
                missing.add(report_id)
            continue
        fhir_id = c.find_seeded_report(order["patient_uuid"], order["order_uuid"])
        if not fhir_id:
            if report_id not in missing:
                print(f"report {report_id}: no seeded fhir report for {accession}; "
                      f"will keep retrying", flush=True)
                missing.add(report_id)
            continue
        missing.discard(report_id)
        r = c._fget(f"DiagnosticReport/{fhir_id}")
        if r.get("status") == "final":
            bridged.add(report_id)
            continue
        signer = f"{given or ''} {family or ''}".strip()
        r["conclusion"] = capped_conclusion(report_id, strip_html(body))
        r["status"] = "final"
        c._fput("DiagnosticReport", fhir_id, r)
        bridged.add(report_id)
        print(f"bridged RIS report {report_id} ({accession}, signed by {signer}) "
              f"-> DiagnosticReport/{fhir_id} final", flush=True)


def main() -> None:
    c = OmrsClient()
    conn = None
    print(f"ris-sign-bridge up; polling every {POLL_SECONDS}s", flush=True)
    bridged: set[int] = set()
    missing: set[int] = set()
    while True:
        try:
            if conn is None:
                conn = connect()
            conn.ping()
            bridge_cycle(conn, c, bridged, missing)
        except Exception as e:  # noqa: BLE001 -- keep the bridge alive across transient outages
            print(f"bridge cycle error: {e!r}", flush=True)
            conn = None
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
