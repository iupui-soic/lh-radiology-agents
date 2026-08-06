# CLAUDE.md — Interpretation Assistant Agent

**Owner:** Chaitra · **Skill:** `interpretation.runTools` · **Port:** 8103 · **Stage:** Interpretation

## You own
`handler.py`, `registry.py` (tool selection), `cxr_model.py`, `server.py`, `tests/`,
card `contracts/cards/interpretation-assistant.json`.

## Contract
Return shape: `contracts/skills/interpretation.schema.json` (`toolsSelected[]`, `findings[]`
with `evidenceRef`, `overallStatus` STUBBED|COMPLETE|PARTIAL|ERROR). A `findings[]` item has
`additionalProperties:false` — only `toolId,label,confidence,evidenceRef,status`. Add a field
(region, severity, bbox) only by changing the schema in the same commit (golden rule 1).

## Three levels of reality (visible in the output, not implied)
- **PIXELS — `pneumothorax-detect` (#71, slice of #27).** The first tool that actually reads the
  image. It fetches pixels from Orthanc (read-only), runs a pretrained TorchXRayVision DenseNet-121
  (`cxr_model.py`), and reads the model's **Pneumothorax** head. Reports `COMPLETE` on a positive
  screen, with a real `confidence` and `evidenceRef: orthanc:instance/<id>`.
- **REFERRAL REASON — `pe-detect` (#27).** Cross-checks `order.reasonCode` (ICD-10 family prefix,
  dot-stripped, e.g. `"I26"`), not pixels. A genuine but narrow interim signal, not CAD, so it
  stays `STUBBED` with a plain-text `evidenceRef` (`order.reasonCode=I26.99`). Table-driven via
  `_REASON_CODE_RULES`. **It is also the degrade path for `pneumothorax-detect`** when the pixel
  read can't run (see below).
- **STUBBED — everything else,** until it gets a real implementation. Version reads `stub-0`.

## `pneumothorax-detect` behaviour (the state machine)
- **Positive** (`Pneumothorax` p ≥ `POSITIVE_THRESHOLD`) → `COMPLETE`, confidence = p, label names
  the pathology, `evidenceRef` = the scored instance.
- **Negative** (p < threshold) → `STUBBED` — see "draft only on positives" below. The model *ran*
  (evidenceRef records the instance; `toolsSelected[].version` still reports the model id), it just
  emits no COMPLETE, so a normal study stays inert.
- **Cannot look** (extras absent / no Orthanc study / only non-image instances) → falls through to
  the referral-reason cross-check, then a bare stub. **Never invents a negative.**
- **Model threw** on real pixels (uniform frame, missing target head, inference error) → `ERROR`,
  honestly, carrying the instance it reached. (A transport failure means it could not LOOK — that
  degrades, above, and is never an ERROR.) Workflow proceeds; the read is never blocked.

`version` in `toolsSelected[]` distinguishes the three: the model id
(`cxr-densenet121-res224-all`) whenever the model scored an instance (even a STUBBED negative),
`referral-rule-1` for a reason-code hit, `stub-0` otherwise.

## Threshold policy (#71, revisited by #86)
A single global `POSITIVE_THRESHOLD` (`handler.py`) — default 0.5, the model's nominal operating
point, overridable per deployment via `CXR_POSITIVE_THRESHOLD` (calibrated domain; empty = default).
The **default stays deliberately un-tuned in code**: tuning it is a clinical decision that needs a
real cohort (same argument as #64's registry corpus), so #86 made it *deployable* without making the
call. There is no per-pathology threshold. Findings additionally carry `rawScore` (op-norm-inverted
raw sigmoid) and `opThreshold` (the head's raw operating point, `cxr_model.op_threshold`) so
surfaces can show a margin — the calibrated score compresses every positive into ~0.50–0.53 and
reads as a coin flip on its own (#86).

## "Draft only on positives" — the #71 decision (read before changing negative behaviour)
#71 asked whether a negative screen should emit `COMPLETE` (a pre-sign draft on **every** study) or
stay inert (a draft **only on positives**). It **must be only on positives**, because the pre-sign
write downstream is **unconditional on any `COMPLETE` finding**: `orchestrator/workflow.py`
`_presign_impression` calls `impression.generate` and writes its `impressionText` to the chart
regardless of whether anything was flagged. A `COMPLETE` *negative* would therefore write "No acute
findings identified" — a fixed negative impression authored by nobody — into every normal patient's
chart ahead of the read, which is exactly the automation-bias trap the #26 `COMPLETE` gate exists to
prevent. So a negative screen reports `STUBBED` (honest via evidenceRef+version), and only a
positive arms the pre-sign draft + Cat1 critical-comm path.

A cleaner long-term fix lives in the orchestrator, not here: gate the pre-sign *write* on
`impression.criticalFlags` being non-empty (only offer a draft when there's something to flag),
which would let a negative report `COMPLETE` honestly without a chart write. That's
`orchestrator/`-owned (lead) and out of this agent's scope.

## ⚠️ This change arms the pre-sign fhir2 write (interlock — lead/#30)
`pneumothorax-detect` is the first tool that can emit `COMPLETE`, and `COMPLETE` is what trips the
pre-sign chart write (`workflow.py:_has_complete_finding`). Once this lands, a **positive** screen on
a study whose order carries a `fhirServiceRequestId`, against an `https`/loopback fhir2, will attempt
a real preliminary DiagnosticReport (PHI) write to the chart **before the read, with no second human
decision**.

**There is no single on/off disable switch for that write today** — verified: `PRESIGN_WRITE_ENABLED`
does not exist anywhere in the code — do not reference it.
The write is gated only by, in order: the `PATCH_PRESIGN_IMPRESSION` replay marker; `_has_complete_finding`
(which THIS change first opens); the order having a `fhirServiceRequestId` (workflow.py — an
`UNRESOLVED` order writes nothing); and the #30/!57 **transport** guard `FHIR2_ALLOW_INSECURE_WRITE`,
which only refuses *plaintext-http-to-a-remote-host* and **allows `https`/loopback** (the normal
posture). None of these is a "hold the write pending review" switch.

**So the safe posture until #30 (fhir2 write-back security/PHI review) closes must be enforced
deliberately, and it belongs in the orchestrator (#30, lead), not here:** e.g. add a real
`_presign_impression`/activity disable gate, or don't provision the #55 report concept on the stack
(note the write is still *attempted*), or keep fhir2 off an allowed transport. This is the top review
item on this change. Refs #30, #26.

## Data deps
Orthanc via `radagent_common.orthanc_client`. #71 added the **pixel-READ** path there
(`list_study_instances`, `get_instance_dicom`) — read-only, PHI scored-and-discarded, never in an
A2A message (golden rule 2). **Writing** AI-made images/overlays back as a DICOM SC/overlay
`evidenceRef` is still deferred (#59, needs its own safety review); `evidenceRef` stays a text
locator.

## Packaging
torch/torchvision (CPU index) + torchxrayvision live only in this agent's Dockerfile, with the
weights **baked at build** (`/root/.torchxrayvision`, `HOME=/root`). The `[imaging]` extra
(pydicom+numpy) is on `radagent-common`. The agent-tests CI lane installs none of these, so
`PIXEL_TOOLING` is False and the pixel path is exercised only by tests that fake Orthanc + the model.

## Run / test
`cd agents/interpretation-assistant && python -m pytest -q`

## Do NOT touch
Other agents, `orchestrator/`, shared envelope, the A2A factory.
