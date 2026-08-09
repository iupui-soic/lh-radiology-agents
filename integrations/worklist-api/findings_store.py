"""Findings store for the Worklist API — durable index of AI evidence keyed by study
(#89: client-side CAD evidence rendering).

The orchestrator's workflow publishes `interpretation.runTools` output here after the
pre-read fan-out completes. The OHIF extension reads back via GET /findings/{studyInstanceUID}
to render the AI evidence banner client-side, WITHOUT any archive-write path (contrast with
#59, which writes DICOM SC objects into Orthanc — a separate class of change gated on PI
sign-off and #30 review).

Same shape as PriorityStore (see store.py) — SQLite + WAL, single-process, upsert-on-write,
no PHI beyond the finding label the tool itself produced. Callers depend only on the method
surface so the backing store can swap to Redis/Postgres in M3 without touching endpoint code.

Rendering policy stays in the OHIF extension (COMPLETE -> prominent, STUBBED -> silent,
ERROR -> subdued). This store returns findings verbatim; it does not filter.

Owner: Parvati.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS study_findings (
    study_instance_uid TEXT PRIMARY KEY,
    workflow_id        TEXT NOT NULL,
    findings_json      TEXT NOT NULL,
    overall_status     TEXT NOT NULL,
    generated_at       TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_study_findings_wf ON study_findings (workflow_id);
"""


class FindingsStore:
    """Durable per-study index of AI findings from interpretation.runTools."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def put(
        self,
        study_instance_uid: str,
        workflow_id: str,
        findings: list[dict],
        overall_status: str,
        generated_at: str,
        updated_at: str,
    ) -> None:
        """Upsert (a re-fired interpretation replaces the prior findings for the same study).

        `findings` is serialized as JSON in one column rather than normalized into a child table:
          * the OHIF extension consumes the whole set as a JSON blob anyway (single fetch per
            study open), so any query that would benefit from row-per-finding shape does not exist;
          * upsert semantics stay atomic — no partial write ever leaves half-a-study visible;
          * the schema evolves independently of the interpretation contract (see
            `contracts/skills/interpretation.schema.json`); a new field on a finding is a no-op
            here rather than a store migration."""
        self._db.execute(
            "INSERT INTO study_findings "
            "  (study_instance_uid, workflow_id, findings_json, overall_status, generated_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(study_instance_uid) DO UPDATE SET "
            "  workflow_id     = excluded.workflow_id, "
            "  findings_json   = excluded.findings_json, "
            "  overall_status  = excluded.overall_status, "
            "  generated_at    = excluded.generated_at, "
            "  updated_at      = excluded.updated_at",
            (
                study_instance_uid,
                workflow_id,
                json.dumps(findings),
                overall_status,
                generated_at,
                updated_at,
            ),
        )
        self._db.commit()

    def get(self, study_instance_uid: str) -> Optional[dict]:
        """Return the stored findings for a study, or None if not yet published.

        `None` is the "no findings yet" signal — the endpoint converts it to a 404 so the OHIF
        extension can distinguish "workflow hasn't run interpretation yet" from "workflow ran and
        found nothing" (which returns 200 with an empty `findings` array via overall_status STUBBED).
        """
        row = self._db.execute(
            "SELECT study_instance_uid, workflow_id, findings_json, overall_status, "
            "       generated_at, updated_at "
            "FROM study_findings WHERE study_instance_uid = ?",
            (study_instance_uid,),
        ).fetchone()
        if not row:
            return None
        return {
            "studyInstanceUID": row[0],
            "workflowId":       row[1],
            "findings":         json.loads(row[2]),
            "overallStatus":    row[3],
            "generatedAt":      row[4],
            "updatedAt":        row[5],
        }

    def all(self) -> dict[str, dict]:
        """Map from StudyInstanceUID to findings record. Used by the /worklist join (#107),
        same shape and rationale as PriorityStore.all(): single query, no N+1.

        Callers get the JSON-decoded findings list -- same shape `get()` returns for the
        per-study fetch, minus the studyInstanceUID key (it's the outer dict key here).
        The list can be empty (all-STUBBED published run) but never missing.
        """
        rows = self._db.execute(
            "SELECT study_instance_uid, workflow_id, findings_json, overall_status, "
            "       generated_at, updated_at "
            "FROM study_findings").fetchall()
        return {
            r[0]: {
                "studyInstanceUID": r[0],
                "workflowId":       r[1],
                "findings":         json.loads(r[2]),
                "overallStatus":    r[3],
                "generatedAt":      r[4],
                "updatedAt":        r[5],
            }
            for r in rows
        }

    def size(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM study_findings").fetchone()[0]
