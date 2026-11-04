# The in-EHR critical-result notification (#79, slice 1)

`ehr-inbox` has been a stubbed channel name since #17: `comms.dispatch` reported
`{"channel": "ehr-inbox", "status": "SENT"}` with no I/O behind it. This slice makes it real for
**critical** results: `Fhir2Client.write_critical_result_notification` writes an Observation into
the patient's chart, and the channel result reports what actually happened. Routine results and
#29 sign-off-rung pass-throughs still write nothing — a chart entry per normal CXR is alert
fatigue relocated, and a rung has no ack task for the entry to name.

## Why an Observation

Live-probed against the dev stack's fhir2 4.1.0 (CapabilityStatement + write round-trip):

| Candidate | Verdict |
|---|---|
| `Observation` | **create/update supported — chosen.** Renders in the chart, carries a `valueString`, accepts our concept stamp. |
| `Flag` | read/search only on this fhir2 — cannot be the write path. |
| Patient Flags Module | installed (3.0.10) but assignment is criteria-evaluated, not POSTable — an optional *rendering* layer later, not a delivery artifact. |
| `Communication` | not implemented by fhir2 at all (that is why the comms ledger exists). |

## What the entry carries

A pointer, not a narrative: `<finding label> | accession <ACSN> | ack task <ledger Task id>`
(plus, when `CRITCOM_ACK_BASE_URL` is set, an ack link — the ack-surface slice). The finding
label is the classifier's one-liner from `criticalFlags`; the report text stays in the RIS and
the communication record stays in the ledger.

## Where the entry renders

At the top of the patient dashboard's **Overview** tab, in the radiology module's *AI critical
result notifications* box (#136, legacyui's `org.openmrs.patientDashboard.overviewBox` extension
point, o3 `o3-048ef7d6` onward). The box lists every notification for the patient, newest first,
read-only. The ack link becomes an **Acknowledge** link only when it is an https URL on the
chart's own host under `/openmrs/ack/`; the box never prints the URL itself, so an older
`/reading-api/ack/` entry is listed without one.

It used to be registered on `dashboard.overview.showConcepts` (#123). That widget links the whole
value to the observation edit form, shows only the latest entry per patient and offers a "New"
control on the concept, so `bootstrap_presign_concept.py` now takes the concept back off that
list. The removal needs an o3 image that has the box, and `test_notification_overview_gp.py`
holds the two together.

## The #26-class conditions, as enforced

- **Gated inert.** `EHR_INBOX_WRITE_ENABLED` defaults off; with it unset the write path performs
  zero I/O and `ehr-inbox` keeps its stubbed claim byte-for-byte. The flag is flipped per
  deployment only after the PI write-path sign-off is recorded on #79. Truthy tokens match the
  other write gates exactly (!73 review, item 3).
- **Authorship-stamped.** `Observation.code` is the dedicated "AI critical result notification"
  concept (`ea215431-5e85-5040-adf0-1da297c154c3`, datatype **Text**, uuid5-derived from
  `lh-radiology.ai-critical-result-notification.v1`), provisioned by
  `docker/openmrs/bootstrap_presign_concept.py` and drift-guarded by
  `test_presign_concept_drift.py`. The idempotent re-run updates only an obs carrying **our
  concept and this critical result's exact anchor segment** — the accession (ack-task id when a
  study has none), matched as an exact `" | "`-delimited segment, never a substring. One chart
  entry per critical result: a Temporal retry of the dispatch re-mints the ack loop (new Task
  id — pre-existing ledger semantics), and the accession anchor makes the retry update the same
  entry in place so it always names the *live* ack task. A write with neither accession nor task
  id is refused (`ValueError` → `FAILED` channel result) rather than written uncorrelatable.
  *Why this shape: pre-merge adversarial review reproduced two failure modes of the naive
  task-id-substring anchor — HAPI's sequential Task ids make `ack task 5` a substring of
  `ack task 52` (one result's dispatch overwrote another's entry), and retry-minted task ids
  meant retries accumulated entries. Both are pinned in `test_fhir_client_notification.py`.*
- **Best-effort, and one deliberate deviation from !73.** The Orthanc SC write re-raises a
  transport refusal; this write must not: it runs *after* the ledger Communication and ack Task
  exist, so any raise fails the dispatch activity, Temporal retries it, and the same human is
  paged twice. `tools.deliver_critical_result_to_chart` therefore swallows every exception into a
  logged `FAILED` channel result. A misconfigured deployment still surfaces — every critical
  dispatch logs the exception and records `FAILED`.

## Live-verified on the dev stack (2026-07-19)

Bootstrap provisions the concept idempotently on the running mariadb; the write lands on live
fhir2 (loopback transport), reads back with concept, `effectiveDateTime` and `valueString`
intact; a second call with the same ack task updates the same Observation id. One negative
finding shaped the resource: **fhir2 4.1.0 500s on any Observation write carrying `basedOn`**
(HAPI-0389 translator NPE; the identical resource without it is a 201 — bisected live). The
order correlation therefore rides in `valueString` as the accession, and the ledger
Communication keeps the typed `ServiceRequest` reference.

## Sign-off gate (before any deployment flips the flag)

1. PI write-path sign-off recorded **on #79** (best-effort / authorship-stamped / minimal
   content — this doc is written to those conditions).
2. Transport: https route (#75) or an explicitly opted-in trusted network.
3. Least-privilege fhir2 account (#30 condition; the same account work as the pre-sign draft).

## The ack surface (slice 3, same branch line)

The link in the chart entry resolves at the worklist-api's `GET /ack/{task_id}?sig=` route
(`integrations/worklist-api/ack.py`): signature verified FIRST (a forged link never solicits
credentials), then the human authenticates with their own OpenMRS account (HTTP Basic resolved
through `/ws/rest/v1/session`), then the ledger Task closes with WHO on a `Task.note`, along with
what the acknowledger says has been done so far (#139; recorded only, nothing reads it yet)
(`complete_ack_task`) — `comms.checkAck` then reports COMPLETED and no escalation fires. Inert
until `CRITCOM_ACK_HMAC_SECRET` is configured: producer-side no link is minted without it, and
the verifier fails closed. `CRITCOM_ACK_BASE_URL` should be the externally-reachable route (for
the #75 overlay: `https://<host>/reading-api`).

## Out of scope in this slice

The Patient Flags banner (optional rendering, needs a criterion-type probe), ETL requester
seeding (#68 side), and any orchestrator/contract change (`channelResults` already carries
per-channel results).
