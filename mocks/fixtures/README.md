# Fixtures (synthetic)

All data here is **synthetic and de-identified** — safe to commit. Never add real PHI.

- `studycontext.*.json` — `StudyContext` envelopes, one per demo scenario (validated in CI).
- `diagnosticreport.<scenario>.final.json` — the signed report each scenario's radiologist
  produced. The walking skeleton's `_DemoFhir` indexes these by FHIR `id` and serves
  `get_report_conclusion` from them, the way `Fhir2Client` does against a live fhir2.
- `comms.dispatch.output.*.json`, `comms.checkAck.output.*.json` — CritCom skill outputs (#52).
- `fhir_bundle.sample.json` — a small synthetic FHIR R4 bundle for EHR Assistant development.

## The studycontext ↔ diagnosticreport pairing

`studycontext.<scenario>.json` and `diagnosticreport.<scenario>.final.json` are matched **by
filename**, and a StudyContext with no matching report is a hard error rather than a fallback to
some default report (`_DemoFhir.report_ref_for`). That is not fussiness: before #125 the skeleton
hardcoded one report id for all five fixtures and the stub ignored the id it was handed, so every
scenario was impressed, verified and communicated from the same narrative — a routine screening
mammogram reported a pneumothorax and paged the on-call in every run. **Add both files, or
neither.** `mocks/tests/` fails if they drift apart, or if the scenarios stop differing.

Criticality is **derived**, not declared: the impression agent scans the report's `conclusion`,
so what a fixture *says* is the only thing that decides whether it pages. The scenarios below are
deliberately mixed so both paths are walked.

| scenario | conclusion | critical? | verification | channels |
|---|---|---|---|---|
| `ct_aortic_dissection` | aortic dissection | yes | FAIL | ehr-inbox + oncall-pager |
| `cxr_pneumothorax` | large left tension pneumothorax | yes | FAIL | ehr-inbox + oncall-pager |
| `mammo_routine` | negative screening, BI-RADS 1 | no | PASS | ehr-inbox |
| `mr_brain` | stable demyelinating disease | no | PASS | ehr-inbox |
| `sample` | stable solitary pulmonary nodule | no | PASS | ehr-inbox |

Two of these exercise the scanners rather than just the happy path: the CXR names its suspicion in
`INDICATION:` (a section `scannable_text` drops) and negates a second keyword ("No rib fracture"),
and the mammogram negates one ("No suspicious mass"). Both are still classified correctly, so the
section-skipping and negation logic is walked, not bypassed.

Add more fixtures as agents grow; wire new families into `scripts/validate_contracts.py`.
