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
final WITH OUR OWN CONCLUSION is skipped; an already-final report with a DIFFERENT conclusion
is a stale seeded final (a rehearsal `finalize` that predates the read, #102) and is refused
loudly, or projected over when BRIDGE_OVERWRITE_STALE_FINAL is set. Runs as the
`ris-sign-bridge` compose service.
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

# What to do with a COMPLETED report whose seeded DiagnosticReport is already final with a
# DIFFERENT conclusion (#102): that final is not our work, it is a leftover rehearsal
# `finalize` (run-book §6) from before the radiologist read the study. "Never overwrite a
# final report" stays the default, so the bridge refuses -- but now loudly and retrying, so a
# `report_seeder.py restage` (which returns the seed to preliminary) lets the sign bridge on
# the next cycle without a container restart. Set to 1/true to project the human sign over the
# stale final instead: the signed read is newer than any seed by construction, but overwriting
# a final chart record is a deliberate per-deployment call, not a silent default.
OVERWRITE_STALE_FINAL = (
    os.environ.get("BRIDGE_OVERWRITE_STALE_FINAL", "").strip().lower() in ("1", "true", "yes"))


def _note_miss(missing: dict[int, int], report_id: int, what: str) -> None:
    n = missing[report_id] = missing.get(report_id, 0) + 1
    if n == 1 or n % MISS_LOG_EVERY == 0:
        print(f"report {report_id}: {what}; retrying (attempt {n})", flush=True)


def _fhir_instant(dt) -> str | None:
    """A naive OpenMRS datetime -> a FHIR instant, or None if there is nothing to stamp.

    The module's datetimes carry no timezone and OpenMRS stores them in the server's zone, which
    is UTC on every stack we run (checked against the #70 sign: date_created 19:09 matched the
    19:09Z the bridge and the orchestrator both logged). Treating them as UTC is therefore exact
    here and off by the server offset nowhere we deploy. A deployment that runs OpenMRS on a
    non-UTC server would need this to read that offset instead of assuming it.
    """
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def completed_reports(conn):
    # p.uuid rides along so the bridged DiagnosticReport can carry the signer as a
    # Practitioner reference (#93): fhir2 maps Practitioner ids to provider uuids.
    #
    # The sign instant (#110) is coalesce(date_changed, date_created), NOT report_date: the
    # module's report_date is a DATE, so it carries the day and drops the time, and a
    # day-precision issued cannot serve the time-in-state metrics that read it. A report is
    # completed once and not edited after, so its last write IS the completion.
    with conn.cursor() as cur:
        cur.execute(
            "select rr.report_id, rr.report_body, o.accession_number, pn.given_name, "
            "pn.family_name, p.uuid, coalesce(rr.date_changed, rr.date_created) "
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
    already final with our own conclusion); `missing` counts consecutive resolve misses AND
    stale-final refusals per report_id, so each is RETRIED every cycle and logged on the first
    and every MISS_LOG_EVERY-th attempt -- a resolve miss is routinely transient (ETL still
    loading, fhir2 hiccup, order arriving late), a stale final clears on an operator restage,
    and a permanent skip turns either into a silently lost human sign until a container
    restart (#90, #102)."""
    for report_id, body, accession, given, family, provider_uuid, signed_at in completed_reports(conn):
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
        r = c._fget(f"DiagnosticReport/{fhir_id}")
        signer = f"{given or ''} {family or ''}".strip()
        body_text = strip_html(body)
        conclusion, was_truncated = clamp_conclusion(body_text, reserve=len(TRUNCATION_MARKER))
        if was_truncated:
            conclusion = TRUNCATION_MARKER + conclusion
        if r.get("status") == "final":
            # Two ways a seed gets to final, and only one is ours (#102). Our own write is
            # byte-reproducible from the RIS body (the strip + clamp + marker above), so a
            # matching conclusion is a previous cycle's work: quiet idempotent skip. A
            # differing conclusion is a stale rehearsal finalize that predates the read; the
            # human sign has NOT reached fhir2, and silence here cost a live demo study
            # (s50279568, 2026-08-06).
            if r.get("conclusion") == conclusion:
                missing.pop(report_id, None)
                bridged.add(report_id)
                continue
            if not OVERWRITE_STALE_FINAL:
                # Not added to `bridged`: keep retrying so a run-book restage (seed back to
                # preliminary) is picked up on the next cycle, and reuse the miss cadence so
                # the refusal stays visible without per-cycle spam.
                _note_miss(missing, report_id,
                           f"seeded DiagnosticReport/{fhir_id} for {accession} is already "
                           f"final with a DIFFERENT conclusion ({len(r.get('conclusion') or '')} "
                           f"chars stored vs {len(body_text)} signed); REFUSING to overwrite -- "
                           f"restage the study or set BRIDGE_OVERWRITE_STALE_FINAL=1")
                continue
            print(f"report {report_id}: seeded DiagnosticReport/{fhir_id} for {accession} was "
                  f"already final with a different conclusion "
                  f"({len(r.get('conclusion') or '')} chars stored vs {len(body_text)} signed); "
                  f"BRIDGE_OVERWRITE_STALE_FINAL is set, projecting the human sign over it",
                  flush=True)
        missing.pop(report_id, None)
        if was_truncated:
            print(f"report {report_id}: signed body is {len(body_text)} chars but fhir2 caps "
                  f"conclusion at {FHIR2_CONCLUSION_MAX}; keeping the FINDINGS/IMPRESSION end "
                  f"WITH an in-band marker -- downstream verification sees a partial body",
                  flush=True)
        r["conclusion"] = conclusion
        r["status"] = "final"
        # Restamp the sign instant (#110). The flip reuses the study's SEEDED DiagnosticReport, so
        # without this `issued` keeps the ETL seed's timestamp: the #70 hosted run produced a
        # report signed 2026-08-07 carrying issued 2026-07-24. docs/signoff-link.md maps
        # issued -> the poller's signedAt, so every time-in-state and turnaround number computed
        # off a rehearsal (#76) would be wrong by the age of the cohort load, and wrong in the
        # flattering direction. Unlike performer and identifier, `issued` genuinely round-trips on
        # this fhir2 (PUT echo AND readback, probed live 2026-08-07), so this is ours to fix.
        # A row with no timestamp at all leaves the seed value rather than inventing one.
        issued = _fhir_instant(signed_at)
        if issued:
            r["issued"] = issued
        # Stamp the signer as performer (#93): a UI sign attributes the report, so the bridged
        # equivalent must too, or the final chart copy carries no radiologist. Practitioner id =
        # the interpreter's provider uuid in fhir2. A COMPLETED report without an interpreter
        # row still bridges (losing the sign would be worse than losing the attribution), it is
        # just left unstamped and the bridged log line says so.
        # KNOWN LIMIT (live-verified 2026-08-05): this fhir2 build (4.1.0) accepts the PUT and
        # silently DROPS performer AND resultsInterpreter, the same accepts-and-drops family as
        # its identifier handling. The stamp still goes out because it costs nothing and starts
        # working the day the translator does; until then the RIS row
        # (radiology_report.principal_results_interpreter) and this log line are the
        # authoritative attribution record.
        if provider_uuid:
            performer = {"reference": f"Practitioner/{provider_uuid}"}
            if signer:
                performer["display"] = signer
            r["performer"] = [performer]
        c._fput("DiagnosticReport", fhir_id, r)
        bridged.add(report_id)
        print(f"bridged RIS report {report_id} ({accession}, signed by "
              f"{signer or 'UNKNOWN: no interpreter recorded'}) "
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
