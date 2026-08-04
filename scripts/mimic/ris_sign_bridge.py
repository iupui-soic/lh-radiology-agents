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
import os
import time

from omrs_client import OmrsClient
from report_text import FHIR2_CONCLUSION_MAX, clamp_conclusion, strip_html

POLL_SECONDS = int(os.environ.get("BRIDGE_POLL_SECONDS", "10"))

# fhir2 rejects a DiagnosticReport.conclusion over 1024 chars outright (422; live-bisected:
# 1024 -> 200, 1025 -> 422), and presentedForm does not persist on this build -- so there is
# nowhere fhir2-side for the full text. The clamp (report_text.clamp_conclusion, shared with
# the ETL seed path) keeps FINDINGS onward or the IMPRESSION tail -- the sections Verification
# parses -- but a silent cut is still not acceptable (#91): the marker below is PREFIXED
# in-band (head-side, since it is the head that was dropped) and the cut is logged loudly.
# The full text remains authoritative in the RIS only.
TRUNCATION_MARKER = "[TRUNCATED BY ris-sign-bridge: fhir2 caps conclusion at 1024 chars; full text in RIS] "

# A resolve miss is re-logged on this cadence (~5 min at the default poll): a lost human sign
# is the highest-consequence state this bridge has, and a single line that scrolled away hours
# ago is functionally silence -- while every-cycle logging is one line per 10s per stuck report.
MISS_LOG_EVERY = 30


def _note_miss(missing: dict[int, int], report_id: int, what: str) -> None:
    n = missing[report_id] = missing.get(report_id, 0) + 1
    if n == 1 or n % MISS_LOG_EVERY == 0:
        print(f"report {report_id}: {what}; retrying (attempt {n})", flush=True)


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
    # Imported here, not at module scope: the mimic-etl-tests lane installs no pymysql
    # (DB-path-only dependency, the same treatment omrs_client gives it at _db()), and a
    # module-level import makes every test that touches this module fail to collect.
    import pymysql
    return pymysql.connect(
        host=os.environ.get("OMRS_DB_HOST", "mariadb"),
        port=int(os.environ.get("OMRS_DB_PORT", "3306")),
        user=os.environ.get("OMRS_DB_USER", "openmrs"),
        password=os.environ.get("OMRS_DB_PASS", "openmrs"),
        database=os.environ.get("OMRS_DB_NAME", "openmrs"),
        autocommit=True)


def bridge_cycle(conn, c, bridged: set[int], missing: dict[int, int]) -> None:
    """One pass over COMPLETED RIS reports. `bridged` holds report_ids DONE (flipped, or found
    already final); `missing` counts consecutive resolve misses per report_id, so the miss is
    RETRIED every cycle and logged on the first and every MISS_LOG_EVERY-th attempt -- a
    resolve miss is routinely transient (ETL still loading, fhir2 hiccup, order arriving late),
    and the old permanent skip turned any hiccup into a silently lost human sign until a
    container restart (#90)."""
    for report_id, body, accession, given, family in completed_reports(conn):
        if report_id in bridged:
            continue
        order = c.order_for_accession(accession)
        if not order:
            _note_miss(missing, report_id, f"no order for {accession}")
            continue
        fhir_id = c.find_seeded_report(order["patient_uuid"], order["order_uuid"])
        if not fhir_id:
            _note_miss(missing, report_id, f"no seeded fhir report for {accession}")
            continue
        missing.pop(report_id, None)
        r = c._fget(f"DiagnosticReport/{fhir_id}")
        if r.get("status") == "final":
            bridged.add(report_id)
            continue
        signer = f"{given or ''} {family or ''}".strip()
        body_text = strip_html(body)
        conclusion, was_truncated = clamp_conclusion(body_text, reserve=len(TRUNCATION_MARKER))
        if was_truncated:
            print(f"report {report_id}: signed body is {len(body_text)} chars but fhir2 caps "
                  f"conclusion at {FHIR2_CONCLUSION_MAX}; keeping the FINDINGS/IMPRESSION end "
                  f"WITH an in-band marker -- downstream verification sees a partial body",
                  flush=True)
            conclusion = TRUNCATION_MARKER + conclusion
        r["conclusion"] = conclusion
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
    missing: dict[int, int] = {}
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
