"""Report seeding + the flip-to-final rehearsal cue (#68 build item 4).

Seeding (a `preliminary` DiagnosticReport basedOn the order) happens in load_cohort. This tool adds
the rehearsal cue: `finalize <accession>` flips that study's seeded report to `final`, which makes
the RIS poller fire `report_finalized` and drive the human-gated sign-off loop WITHOUT a live RIS
sign. In the live demo, radiologists sign in the RIS instead and this tool is not used.

Proven end to end (#68): an order loaded for accession `s68proof1`, its DICOM pushed and the
workflow parked at AWAITING_RADIOLOGIST, then `finalize s68proof1` released the gate.
"""
from __future__ import annotations
import argparse

from omrs_client import OmrsClient


def finalize(c: OmrsClient, accession: str) -> str:
    order = c.order_for_accession(accession)
    if not order:
        raise SystemExit(f"no RadiologyOrder for accession {accession!r} (load the FHIR side first)")
    report_id = c.find_seeded_report(order["patient_uuid"], order["order_uuid"])
    if not report_id:
        raise SystemExit(f"no seeded report basedOn ServiceRequest/{order['order_uuid']}")
    c.finalize_diagnostic_report(report_id)
    return report_id


def restage(c: OmrsClient, accession: str, manifest_path: str) -> dict:
    """Put a study back to unread: void its report, restore its narrative, reopen it (#105).

    The reverse of `finalize`, and of a real read. The run-book tells the operator to restage
    between takes and arc 1 opens on a restaged study, but until now nothing implemented it, so
    every reset was hand-run SQL. Worse, a study does not come back on its own after a read: the
    sign-bridge projects the signed body OVER the seeded narrative, so a rehearsed study keeps the
    rehearsal's text forever. 10 studies on the demo host were found in that state.

    Three steps, in this order:
      1. VOID the RIS report rows (never DELETE -- the row and its author survive for audit),
      2. restore the study's own narrative from the manifest, through the same
         `clamp_conclusion` the ETL seeds with, because fhir2 refuses a conclusion over
         FHIR2_CONCLUSION_MAX with a 422,
      3. set the seeded report back to `preliminary`.

    Returns a summary dict. Re-firing the study's Orthanc event (so a fresh workflow runs) is left
    to the caller: it needs the ingress, not fhir2. See the run-book's restage step.
    """
    import json

    import pymysql  # local import: only this verb needs the DB (mirrors ris_presign_bridge)

    from report_text import clamp_conclusion

    raw = json.load(open(manifest_path))
    entries = raw if isinstance(raw, list) else raw.get("studies", [])
    source = next((e.get("report_text") or "" for e in entries if e["study_id"] == accession), None)
    if not source:
        raise SystemExit(f"no narrative for {accession!r} in {manifest_path}")

    order = c.order_for_accession(accession)
    if not order:
        raise SystemExit(f"no RadiologyOrder for accession {accession!r}")
    report_id = c.find_seeded_report(order["patient_uuid"], order["order_uuid"])
    if not report_id:
        raise SystemExit(f"no seeded report basedOn ServiceRequest/{order['order_uuid']}")

    voided = []
    conn = pymysql.connect(host=c.cfg.db_host, port=c.cfg.db_port, user=c.cfg.db_user,
                           password=c.cfg.db_pass, database=c.cfg.db_name, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rr.report_id FROM radiology_report rr JOIN orders o ON o.order_id = rr.order_id "
                "WHERE o.accession_number = %s AND rr.voided = 0", (accession,))
            for (rid,) in cur.fetchall():
                cur.execute(
                    "UPDATE radiology_report SET voided = 1, date_voided = NOW(), void_reason = %s "
                    "WHERE report_id = %s", (f"restage {accession}", rid))
                voided.append(rid)
    finally:
        conn.close()

    seedable, clamped = clamp_conclusion(source)
    res = c._fget(f"DiagnosticReport/{report_id}")
    res["conclusion"] = seedable
    res["status"] = "preliminary"
    c._fput("DiagnosticReport", report_id, res)

    cleared = _clear_worklist_read_state(accession)

    return {"accession": accession, "report_id": report_id, "voided_ris_reports": voided,
            "restored_chars": len(seedable), "source_chars": len(source), "clamped": clamped,
            "read_state_cleared": cleared}


def _clear_worklist_read_state(accession: str) -> bool:
    """Return the study to the UNREAD worklist (#108). Best-effort; never fails a restage.

    Since #108 the worklist marks a study Read from the orchestrator's terminal-state publish, and
    that record outlives a restage: the re-fired workflow only republishes when it archives AGAIN,
    so without this the row would read Read for the whole of the next rehearsal. That is exactly
    the stale-worklist problem #108 set out to remove, so leaving it would reintroduce the bug via
    the reset path.

    The store is keyed on StudyInstanceUID and the seeder only holds an accession, so the mapping
    comes from the worklist's own listing, which carries both. Deliberately not from Orthanc: this
    tool has no Orthanc credentials, and the service that owns the record is the right place to
    ask. A worklist-api that is down, or one predating the endpoint, costs the operator nothing:
    the study is still restaged, its row just stays marked until the next archive.
    """
    import os

    base = os.environ.get("WORKLIST_API_URL", "http://worklist-api:8107").rstrip("/")
    try:
        import httpx
        rows = httpx.get(f"{base}/worklist", timeout=10.0).json().get("items", [])
        uid = next((r.get("studyInstanceUID") for r in rows
                    if r.get("accessionNumber") == accession), None)
        if not uid:
            print(f"warning: {accession} has no worklist row; read-state left as is", flush=True)
            return False
        return 200 <= httpx.delete(f"{base}/state/{uid}", timeout=5.0).status_code < 300
    except Exception as e:  # noqa: BLE001 -- a reset tool must not die on a visibility call
        print(f"warning: could not clear the worklist read-state for {accession}: {e}", flush=True)
        return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Flip a seeded report to final (rehearsal sign-off cue).")
    # `finalize` is accepted as an optional leading verb: this module's own docstring,
    # scripts/mimic/README.md and the run-book's restage step all spell the command
    # `report_seeder.py finalize <accession>`, and argparse rejected it as an extra positional --
    # so the documented command failed at exactly the moment a demo needs it. Tolerate both spellings
    # rather than re-document three places (and any run-book copy already in someone's notes).
    p.add_argument("verb", nargs="?", default=None, help=argparse.SUPPRESS)
    p.add_argument("accession", nargs="?", default=None, help="the study accession (MIMIC study_id)")
    p.add_argument("--manifest", default=None,
                   help="showcase manifest.json; required by `restage` (source of the narrative)")
    args = p.parse_args(argv)

    verb, accession = args.verb, args.accession
    if accession is None and verb in ("finalize", "restage"):
        # `report_seeder.py finalize` with the accession forgotten: without this guard the
        # verb would shift into the accession slot and we'd look up a study named "finalize".
        p.error("an accession is required")
    if accession is None:          # `report_seeder.py <accession>` -- the verb slot holds it
        verb, accession = "finalize", verb
    if accession is None:
        p.error("an accession is required")
    if verb not in ("finalize", "restage"):
        p.error(f"unknown command {verb!r} (expected 'finalize' or 'restage')")

    if verb == "restage":
        if not args.manifest:
            p.error("restage needs --manifest (the study's own narrative comes from it)")
        out = restage(OmrsClient(), accession, args.manifest)
        clamped = " (clamped to fit fhir2)" if out["clamped"] else ""
        print(f"restaged {accession}: voided RIS reports {out['voided_ris_reports'] or 'none'}, "
              f"DiagnosticReport/{out['report_id']} restored to {out['restored_chars']} chars"
              f"{clamped} and set preliminary")
        print("re-fire the study's Orthanc event to start a fresh workflow (run-book 6a)")
        return 0

    rid = finalize(OmrsClient(), accession)
    print(f"finalized DiagnosticReport/{rid} for accession {accession} "
          f"-> the RIS poller will detect report_finalized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
