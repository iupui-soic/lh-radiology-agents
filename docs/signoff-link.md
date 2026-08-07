# The RIS sign-off link: RIS report -> fhir2 -> orchestrator (#70)

How a radiologist signing a report in the LH-Radiology RIS reaches the orchestrator's post-sign
pipeline, the two places that link was broken against the deployed stack, and the fix.

## The one join the poller can make

The RIS poller (`orchestrator/ingress.py`, `orchestrator/activities.poll_finalized_reports`) sweeps
fhir2 for `DiagnosticReport`s and, for each `status=final` one, must find the workflow it belongs to.
`_workflow_id_for_report` can match on **exactly two keys**, both recorded at ingest:

| join key | consumer source | producer must supply |
|---|---|---|
| `serviceRequestRef` (preferred, the #11 robust join) | `basedOn[].reference` = `ServiceRequest/<id>` | the signed report's `basedOn` |
| `accessionNumber` (fallback) | `identifier` of type `ACSN` | the signed report's ACSN identifier |

A final report carrying neither is logged `matched no waiting workflow (dropped)` and the study waits
forever at `AWAITING_RADIOLOGIST`.

## Field mapping (RIS -> fhir2 -> pipeline consumer)

Intended producer: `RadiologyReportServiceImpl.emitFhirDiagnosticReport` (sibling `lh-radiology`,
o3), fired when a report is signed (`saveRadiologyReport` sets status COMPLETED). **On the deployed
stack that emit never runs** (#82: `ServiceNotFoundException` on `FhirDiagnosticReportService`), so
the real producer today is `scripts/mimic/ris_sign_bridge.py`, which polls the RIS
`radiology_report` table for COMPLETED rows and projects each one into that study's seeded
DiagnosticReport. The table below is the intended mapping; the section after it records where the
bridge's output differs, measured on the demo host.

| RIS `RadiologyReport` field | fhir2 `DiagnosticReport` field | pipeline consumer |
|---|---|---|
| status COMPLETED | `status = final` | poller filter `status == "final"` |
| order concept | `code.coding[0].code` = concept uuid | (fhir2 `codeRequired`; not a join key) |
| order patient | `subject = Patient/<uuid>` | comms subject, presign idempotency |
| report `body` | `conclusion` | `get_report_conclusion` -> impression / verification |
| report `date` | `issued` | poller `signedAt` |
| **the order** | **`basedOn = ServiceRequest/<order uuid>`** | **poller `serviceRequestRef` join** |
| (n/a) | `identifier` (ACSN) | poller `accessionNumber` join |

## What the deployed stack actually produces (demo host, 2026-08-07)

Measured on a real human sign: study s56373033, signed in the RIS by Radiologist One, bridged four
seconds later. Three fields do not match the table above, so write consumers against this section
rather than against the intended mapping.

| field | intended | what actually lands | consequence |
|---|---|---|---|
| `status` | `final` | as intended | the gate releases |
| `basedOn` | `ServiceRequest/<order uuid>` | as intended | the join works, and it is the only key that does |
| `conclusion` | the report body | the body **wrapped by the RIS editor**, e.g. `'<p>FINDINGS: ... IMPRESSION: ...</p>'` | verification still parses it, but a chart view or a whole-narrative comparison gets literal markup |
| `issued` | the report date, read as poller `signedAt` | **unchanged from the seed**: `2026-07-24` on a report signed `2026-08-07` | the bridge flips an existing seeded report and never restamps `issued`, so any turnaround or time-in-state metric reads the seed date |
| `performer` | the signer as `Practitioner/<provider uuid>` (#93) | **dropped**, reads back as absent | fhir2 accepts and silently discards `performer` and `resultsInterpreter`, the same accepts-and-drops family as the `identifier` case above. The RIS stays the attribution record and the fhir2 copy names no radiologist |

The `issued` and `performer` gaps belong to the bridge-as-producer path, so repairing the module's
own emit closes both. The `conclusion` markup comes from the RIS editor and would survive that
repair.

## What was broken (verified live against fhir2 4.1.0, o3 stack, 2026-07-15)

1. **Producer emitted no join key.** `emitFhirDiagnosticReport` set status/code/subject/issued/
   conclusion but neither `basedOn` nor an `identifier`. Every signed report was unroutable.
2. **The ACSN fallback cannot work here anyway.** A DiagnosticReport `identifier` is **silently
   dropped on write** by this fhir2 (POST echo and readback both show `identifier: null`). So
   `basedOn` is the only viable key.
3. **Ingest could not resolve the accession either.** `_resolve_patient_order` used fhir2
   `ServiceRequest?identifier=<accession>`, which returns **HTTP 400** on this fhir2 (the search
   param is unimplemented for ServiceRequest; bare and `system|value` both 400). fhir2 also exposes
   no accession identifier on the ServiceRequest at all. So ingest fell back to `Patient/UNRESOLVED`
   and indexed only the raw accession, which no report can carry back (see 2).

Net: the sign-off join was broken on **both** sides. The consumer test suite stayed green only
because it feeds synthetic bundles that already carry `basedOn`/`ACSN`.

## The fix (two-sided, both tie to one `ServiceRequest/<order uuid>`)

- **Producer** (`emitFhirDiagnosticReport`, sibling repo): add
  `diagnosticReport.addBasedOn(new Reference("ServiceRequest/" + radiologyOrder.getUuid()))`.
  Confirmed live that `basedOn` round-trips on this fhir2's DiagnosticReport body.
- **Ingest** (`orchestrator/ingress.py` + `radagent_common/openmrs_rest.py`): resolve the accession
  through the radiology module's own REST search handler,
  `GET /ws/rest/v1/radiologyorder?accessionNumber=<acc>` (fhir2 cannot). It returns the order uuid,
  which is exactly the id fhir2 uses for that order's ServiceRequest, so both sides land on the same
  `ServiceRequest/<order uuid>` and the poller joins.

Because fhir2 keys a `RadiologyOrder`'s ServiceRequest on the order uuid, the accession never has to
be a fhir2-searchable identifier. The DICOM `AccessionNumber` still ties the Orthanc study to the
order (module REST), and the order uuid ties the order to the signed report (`basedOn`).

## Verified end-to-end on the live stack (2026-07-15)

Against the o3 docker stack with both fixes (o3 image rebuilt with the producer `basedOn`,
orchestrator rebuilt with the module-REST resolver):

1. Inserted a RadiologyOrder with accession `E2E70ACC` -> fhir2 exposes it as
   `ServiceRequest/<order uuid>` (status active).
2. The new ingest resolver mapped `E2E70ACC` -> that `ServiceRequest/<order uuid>` + patient.
3. Pushed a CXR with `AccessionNumber=E2E70ACC` -> the orchestrator started the workflow, resolved
   and indexed the ServiceRequest ref, and reached `AWAITING_RADIOLOGIST`.
4. Landed a `final` DiagnosticReport carrying ONLY `basedOn=ServiceRequest/<order uuid>` (no
   accession identifier -- the exact shape the rebuilt producer emits). The poller joined it and the
   workflow left the gate: `AWAITING_RADIOLOGIST -> COMMUNICATE`. No "matched no waiting workflow
   (dropped)".

Because the report carried no accession identifier, the release could ONLY have come from the
ServiceRequest-ref join -- proving both fixes close the loop together.

NOTE on scope: the RIS has no REST create for orders or reports (`newDelegate` throws), so a report
is signed through the legacy UI / the module Java service, never headless. Step 4 therefore injects
the byte-faithful report the rebuilt `emitFhirDiagnosticReport` produces (shape independently
confirmed to round-trip on this fhir2) in place of a human RIS sign. Acceptance criterion 1 (a human
sign in the RIS releasing the gate) was closed separately, by the hosted run below.

## Verified with a human sign on the demo host (2026-08-07)

No seeder and no synthetic write anywhere in the chain. Driven through the browser as
`radiologist1` on the TLS overlay:

1. Opened `radiologyReport.form?orderId=5b386e42-8705-11f1-9cc6-ceaf1c67aa2e` for study
   s56373033, which claimed the report and created draft 48. There is no Claim Report button on
   the order page in this build (#109); the claim is implicit in that navigation.
2. Authored the body, set Results Interpreter to Radiologist One, clicked **Complete**, and the
   RIS showed "Report completed".
3. `19:11:38Z` the bridge: `bridged RIS report 48 (s56373033, signed by Radiologist One) ->
   DiagnosticReport/7fecd3eb-... final`.
4. `19:11:40Z` to `19:11:42Z` the poller joined on `basedOn` and the post-sign pipeline ran:
   verification **PASS** with no issues, comms dispatched `ehr-inbox SENT`, workflow **ARCHIVED**.

About four seconds from Complete to ARCHIVED. The study was restaged afterwards, so the cohort is
unchanged. One transient `401` hit the bridge's `radiologyorder` REST call on the cycle before it
succeeded, and it recovered on the next 10s cycle without operator action.

## Showcase corollary (#68)

The MIMIC ETL must create orders **through the radiology module** (so each order has a module
accession and a fhir2 `ServiceRequest/<order uuid>`), and the DICOM `AccessionNumber` must equal that
order's accession. A ServiceRequest loaded straight into fhir2 (not via the module) would get a
different id and carry no module accession, so neither the ingest resolve nor the report `basedOn`
would line up, and the study would never leave the read gate.
