# Where CAD inference runs (and what the viewer actually shows)

The model is **not in the OHIF viewer**. Inference runs server-side in the
**interpretation-assistant agent**; the viewer only renders a finding that was computed long
before anyone opened the study. This page is the discoverability pointer — the authoritative,
maintained write-up (state machine, interlocks, threshold rationale) is
`agents/interpretation-assistant/CLAUDE.md`.

## The model (#71, first real slice of #27)

`agents/interpretation-assistant/cxr_model.py`: TorchXRayVision's pretrained **DenseNet-121**
(`densenet121-res224-all`, Cohen et al.), trained on the pooled public CXR corpora (NIH
ChestX-ray14, PadChest, CheXpert, MIMIC-CXR, OpenI, Kaggle). The model exposes 18 pathology
heads; the `pneumothorax-detect` tool reads exactly one — `Pneumothorax` — against a single
global `POSITIVE_THRESHOLD = 0.5`, the model's own nominal operating point. Tuning that
threshold is a clinical decision that needs population data we do not have (the #64 corpus
argument), so it is deliberately untuned.

CPU inference completes in well under a second; the demo host needs no GPU.

## Where the weights live

Baked into the agent's Docker image at build time (`/root/.torchxrayvision`, see the agent
Dockerfile). An agent that reaches for the network mid-study to download a model is an agent
that fails mid-study. torch/torchvision/torchxrayvision exist **only** in this agent's image:
the shared CI lanes install neither, and if the import fails at startup the handler leaves the
pixel tool stubbed rather than discovering the gap mid-study.

## The path from pixels to the viewer banner

1. Registry selection (`registry.py`): CXR modality/region — plus the J93*/J95.811 order
   reason-code slice — selects `pneumothorax-detect` for the study.
2. The agent pulls the pixels from Orthanc (read-only), scores them, and reports a **positive
   screen only** as a `COMPLETE` finding with `p` and an `evidenceRef` into the existing DICOM.
3. The orchestrator publishes the finding to the worklist-api findings store.
4. The OHIF reading mode (`/read`, !100) fetches it same-origin via `/reading-api` and renders
   the banner: *"Pneumothorax screening signal (not a read): positive at p=…"*.

Negatives never emit `COMPLETE` — by design, because a `COMPLETE` finding is what arms the
pre-sign draft write into fhir2 (`orchestrator/workflow.py`, `_has_complete_finding`).

## What this is not

A screening signal, not a diagnosis, and not a substitute for the read:

- **It scores anything.** Handed uniform noise it returns confident numbers (measured, not
  hypothetical). There is no in-distribution check; the registry's modality+region selection is
  the only thing keeping scouts, laterals, and non-chest studies away from it.
- **The training labels are noisy** (NLP over report text), and the operating point is not
  calibrated to any particular population.

Say both out loud in the demo (run-book arc 2); radiologists will ask.
