# Design note: an LLM picks extra EHR context per study, with the fixed packet as the floor (#138)

Status: design only, nothing built. Question from SIIM CAIMI26 (2026-10-26): can an LLM decide what
EHR data to pull for a study? This note answers #138's "done when": the allow-list, the contract
change, and the fallback. Decisions that are the PI's are marked **PI**.

## 1. What exists today

`agents/ehr-assistant/handler.py` builds one fixed packet for every study (`ehr.assembleContext`),
in parallel with triage and interpretation, before the study is `READY_FOR_READ`:

| Slice | fhir2 read | Used by |
|---|---|---|
| `priorStudies` | `ImagingStudy?patient=` | context only |
| `relevantLabs` | `Observation?patient=&code=` creatinine + six eGFR LOINCs | `contrastFlags.egfr`, impression prompt |
| `activeProblems` | `Condition?patient=&clinical-status=active` | impression prompt |
| `allergies` | `AllergyIntolerance?patient=` | `contrastFlags.priorReaction` |
| `medicationFlags` | `MedicationRequest?patient=` matched to five RxNorm/text rules | `contrastFlags.onMetformin`, verification `anticoagulant_bleed_unaddressed` |

Every read is lean (codes, values, dates, never a record dump) and failure-isolated: one slice
failing comes back empty without failing the packet. The packet rides in the orchestrator's
workflow state (pass-forward) to impression and verification.

## 2. The constraint that shapes everything: the fixed packet is the floor

Verification's `anticoagulant_bleed_unaddressed` rule reads `medicationFlags.onAnticoagulant`. If an
LLM chose the reads, and skipped the medications, that rule would silently stop firing, with no
error anywhere. So:

- The fixed packet is computed exactly as today, first, and does not depend on the model at all.
- The model can only **add** reads. It cannot remove, replace, or reorder a fixed slice.
- Any model failure leaves the output identical to today's.

## 3. Where it runs: a second skill, after the study is ready to read (recommended)

`ehr.assembleContext` sits on the pre-read path: the workflow waits for it, together with triage and
interpretation, before the study reaches the worklist. An LLM call there delays the worklist.

**Recommended:** a separate skill, `ehr.extendContext`, that the orchestrator calls once the study is
`READY_FOR_READ`, in parallel with the radiologist's read (hours, not seconds, are available). Its
result joins the workflow state before `IMPRESSION`, which is where it is useful. The fixed packet
and its timing do not change at all.

Alternative: extend `ehr.assembleContext` itself with a hard time budget (for example 3 s) for the
model call. Simpler to wire, but it puts model latency on the pre-read path, so it is not
recommended. **PI**: which of the two.

## 4. The allow-list

The model never writes a query. It returns ids from a fixed list; each id maps to one read-only
fhir2 search with fixed parameters and a lean projection, all in code and reviewed like any rule.

Starter list (LOINC codes to be confirmed against the live fhir2, see the `search_observations`
note on system-qualified tokens):

| Read id | fhir2 search | Projection | Example study that needs it |
|---|---|---|---|
| `coagulation` | `Observation` INR (6301-6, 34714-6), platelets (777-3, 26515-7) | code, value, unit, date | head CT after a fall on an anticoagulant |
| `d-dimer` | `Observation` D-dimer (48065-7, 48066-5) | as above | CT pulmonary angiogram |
| `cardiac-markers` | `Observation` troponin I/T (10839-9, 6598-7, 89579-7) | as above | chest pain, CT coronary or aorta |
| `inflammatory-markers` | `Observation` CRP (1988-5), WBC (6690-2), lactate (2524-7) | as above | abdominal CT for sepsis or abscess |
| `renal-extended` | `Observation` potassium (2823-3), BUN (3094-0) | as above | contrast study with a borderline eGFR |
| `pregnancy` | `Observation` hCG (2106-3, 21198-7) | value, date only | abdomen or pelvis CT in a patient who could be pregnant |
| `vitals-latest` | `Observation` heart rate, BP, SpO2, temperature (8867-4, 85354-9, 59408-5, 8310-5) | latest value each | PE or pneumothorax workup |
| `recent-encounters` | `Encounter?patient=&date=ge<30 days>` | type, date | context for a follow-up study |

Rules for the list:

- Read-only searches only, through `radagent_common.fhir_client`, never free-form parameters.
- Each read has a result cap (for example the 5 latest values) and the same failure isolation as
  the fixed slices.
- `pregnancy` is sensitive. **PI**: include it at all, and whether it may reach the impression prompt.
- Adding an id is a code change with review, never configuration the model can reach.

## 5. How the model chooses

Input (no patient identifiers, and no EHR values): modality, study description, order reason codes
(ICD-10), patient age band and sex if the PI allows them, and the allow-list as `id: one-line purpose`.

Output: `{"extraReads": ["coagulation", "vitals-latest"]}`, at most 3 ids. The parser keeps only ids
on the allow-list, drops duplicates, and caps the count; anything else (bad JSON, unknown ids, a
timeout) means no extra reads. Same hosting-agnostic OpenAI-compatible call as
`agents/impression-generation/llm_draft.py`, with its own `EHR_LLM_BASE_URL` / `EHR_LLM_MODEL` and the
same plaintext-transport guard. Unset is off.

Hosting: the selection prompt carries no EHR values, but the study description and reason codes are
cohort data, so run-book 3a still applies: Gemini with zero data retention, or a local model. No
other cloud endpoint.

A deterministic baseline is worth building next to it: a small table from reason-code families to
read ids (I26 to `d-dimer`, S06 with an anticoagulant to `coagulation`). It needs no model, is
auditable, and is what the model should be measured against.

## 6. The contract change

Contract-first (golden rule 1): the schema and the code change in the same MR.

If `ehr.extendContext` (recommended), a new `contracts/skills/ehr.extend.schema.json`:

```json
{
  "title": "ehr.extendContext output",
  "type": "object",
  "additionalProperties": false,
  "required": ["schemaVersion", "workflowId", "extraReads", "agentVersion", "assembledAt"],
  "properties": {
    "schemaVersion": {"type": "string"},
    "workflowId": {"type": "string"},
    "selectedBy": {"enum": ["model", "rules", "none"]},
    "extraReads": {
      "type": "array",
      "maxItems": 3,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["readId", "items"],
        "properties": {
          "readId": {"type": "string"},
          "items": {"type": "array", "items": {"type": "object"}},
          "error": {"type": "string"}
        }
      }
    },
    "agentVersion": {"type": "string"},
    "assembledAt": {"type": "string", "format": "date-time"}
  }
}
```

plus the skill on `contracts/cards/ehr-assistant.json`, and the orchestrator call. If instead
`ehr.assembleContext` is extended, the same `extraReads` array is added to
`contracts/skills/ehr.schema.json` as an OPTIONAL property, so every current producer and consumer
stays valid.

## 7. Fallback, in one table

| What fails | Result |
|---|---|
| Model unset, down, slow, or returns junk | `extraReads: []`, `selectedBy: "none"`; fixed packet unchanged |
| Model picks an id not on the list | that id is dropped; the rest run |
| One extra read fails in fhir2 | that entry carries `error`, the others are kept |
| The whole skill fails (recommended design) | the workflow proceeds without extras, logged; nothing waits on it |

## 8. Who uses the extra context

First consumer: the impression prompt (`_summarize_ehr_context`), which already lists labs and
problems. Later, a verification rule could use it (for example INR next to an intracranial bleed),
but a rule must never depend on a read the model may skip: rules keep reading the fixed packet.

## 9. How to evaluate it

On the 100-study showcase cohort, locally (DUA):
1. For each study, a clinician (or the PI) lists which reads they would want.
2. Compare the model's picks and the reason-code baseline against that list: reads missed, reads
   added for nothing.
3. Measure latency and failure rate of the selection call.
4. Check the impression prose: do drafts use the extra context correctly, and never as a finding?

Note: the chest X-ray cohort exercises few of these reads; the EMBED or CT data (#31, #27) would.

## 10. Open decisions (PI)

1. Separate skill after READY_FOR_READ (recommended) or a time-budgeted step in the current skill.
2. The starter allow-list, and whether `pregnancy` is on it.
3. Whether age band and sex may be sent to the model.
4. Model versus the reason-code table: build the table first and only add the model if it beats it.
5. Owner: the EHR Assistant is Parvati's area (she was asked on #138, no reply as of 2026-11-23).

## 11. Suggested build order

1. `ehr.extendContext` skill with the reason-code table only (no model), the schema, card and
   orchestrator wiring.
2. The model selector behind `EHR_LLM_*`, falling back to the table.
3. The impression prompt reads `extraReads`.
4. The evaluation in section 9, then the PI decides whether the model stays.
