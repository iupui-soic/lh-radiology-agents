"""The first REAL model behind the tool registry: a chest-X-ray classifier read for pneumothorax
(#71, a slice of #27).

This one actually reads pixels and runs a pretrained classifier -- TorchXRayVision's DenseNet-121
(Cohen et al.), trained on the pooled public CXR corpora (NIH ChestX-ray14, PadChest, CheXpert,
MIMIC-CXR, OpenI, Kaggle). It has 18 pathology heads; `pneumothorax-detect` reads exactly one of
them (TARGET_PATHOLOGY). CPU inference in well under a second.

WHAT THIS IS NOT. It is a SCREENING signal, not a diagnosis, and it is not a substitute for the
read. Two properties make that more than boilerplate:

  * It scores ANYTHING. Handed uniform noise it returns confident numbers -- measured, not
    hypothetical. There is no in-distribution check; a scout view, a lateral, a rotated film, or a
    non-chest study that slipped past the registry all get a number back. The registry selecting on
    modality+region is the ONLY thing keeping non-CXRs away from it.
  * The training corpora are labelled by NLP over report text, so the labels are noisy and the
    operating points are not calibrated for any particular population.

Which is why the handler reports only a POSITIVE screen as COMPLETE ("draft only on positives" --
see handler.py / CLAUDE.md): a COMPLETE finding is exactly what arms the pre-sign fhir2 chart
write (orchestrator/workflow.py:_has_complete_finding), and there is no separate in-code disable
switch for that write today. Holding it pending the security review is #30's (orchestrator-side)
concern; this module's job is to never hand COMPLETE to anything but a genuine positive read of
real pixels. See the interlock section in the agent CLAUDE.md.

Weights are baked into the image at build time (see Dockerfile) -- an agent that reaches for the
network mid-study to download a model is an agent that fails mid-study.
"""
from __future__ import annotations

import logging
import threading

# EAGER, not lazy. This module's importability IS the "are the model extras installed?" signal the
# handler branches on: no torch -> ImportError here -> the handler leaves the pixel tool STUBBED,
# and the agent-tests CI lane (which installs neither torch nor the imaging extra) stays green.
# Importing torch lazily inside score() would make this module import fine everywhere, the handler
# would think the model was available, and it would go to the network mid-study to find out
# otherwise. Model WEIGHTS still load lazily (_load) -- that is the expensive part, not the import.
import torch
import torchxrayvision as xrv

log = logging.getLogger(__name__)

MODEL_WEIGHTS = "densenet121-res224-all"

# The single head `pneumothorax-detect` reads out of the 18-head multi-label model. Spelled exactly
# as TorchXRayVision names it in model.pathologies; a mismatch (a model without this head) surfaces
# as a KeyError the handler turns into an honest ERROR finding rather than a silent miss.
TARGET_PATHOLOGY = "Pneumothorax"

# The positive/negative decision itself lives in handler.POSITIVE_THRESHOLD (env-configurable,
# #86); this module only reports scores. What it CAN answer, because it owns the model, is where
# the head's operating point sits (op_threshold below) -- the number the op-norm calibration maps
# to 0.5, and the reason every calibrated positive on a screening cohort crowds into 0.50-0.53.

# Pathologies the model exposes but which are not screening-actionable on a plain film, or which
# duplicate another head. Excluded from the probability dict score() returns, so no future consumer
# of the head set picks them up by accident. Pneumothorax is NOT here -- it is the target.
_SUPPRESSED = frozenset({"Enlarged Cardiomediastinum"})

_model = None
_lock = threading.Lock()


def _load():
    """Load the WEIGHTS lazily, once, thread-safe.

    Deferred because it is the expensive part (~30MB off disk), not because of the import: a worker
    that never sees a chest film should never pay for it. Two concurrent studies must not each build
    a DenseNet, hence the double-checked lock.
    """
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                m = xrv.models.DenseNet(weights=MODEL_WEIGHTS)
                m.eval()
                _model = m
                log.info("pneumothorax-detect: loaded %s (%d heads)", MODEL_WEIGHTS, len(m.pathologies))
    return _model


def op_threshold(pathology: str) -> float | None:
    """The head's raw-sigmoid operating point, or None when the loaded weights carry none.

    The `-all` weights ship per-head operating thresholds and the forward pass op-norms outputs so
    that each head's threshold lands exactly at 0.5. For Pneumothorax that threshold is a raw
    sigmoid of ~0.0098, which is why calibrated positives compress into a razor-thin band above
    0.5 (#86): almost the entire raw axis above the op point maps into [0.5, 1) near its floor.
    Exposing the op point (and, in the handler, the raw sigmoid recovered from it) is what lets a
    downstream surface show a margin instead of a number that reads as a coin flip.

    NaN means "this head has no operating point in these weights" and is reported as None.

    Deliberately answers only from the ALREADY-loaded model (never _load()): on the pixel path
    score() has always run first, so the weights are in memory; anywhere else -- a handler test
    that fakes score() on a machine that happens to have torch -- this must not become the call
    that pulls a DenseNet off disk (or worse, the network) as a side effect. No model yet -> None.
    """
    model = _model
    if model is None:
        return None
    op_threshs = getattr(model, "op_threshs", None)
    if op_threshs is None:
        return None
    try:
        idx = list(model.pathologies).index(pathology)
    except ValueError:
        return None
    value = float(op_threshs[idx])
    return None if value != value else value  # NaN-safe without importing math for one check


def score(greyscale) -> dict[str, float]:
    """Score one 2-D greyscale array (MONOCHROME2 convention, from radagent_common.imaging).

    Returns {pathology: probability} for every head. Preprocessing follows the model card:
    per-image normalisation to [-1024, 1024], centre crop, resize to 224.

    Per-image min/max normalisation (rather than a fixed bit-depth maxval) is deliberate: after
    rescale slope/intercept the array is in real units whose range depends on modality and
    manufacturer, and the DICOM bit depth no longer describes it. Normalising to the image's own
    range is what the model card does for DICOM input and keeps contrast handling consistent across
    a 12-bit CR and a rescaled DX.
    """
    import numpy as np

    model = _load()

    arr = np.asarray(greyscale, dtype=np.float32)
    arr = arr - arr.min()
    peak = float(arr.max())
    if peak <= 0:
        # A uniform frame carries no signal. Returning zeros here would be a fabricated NEGATIVE --
        # the exact trap the #26 COMPLETE-gate exists to prevent -- so refuse instead and let the
        # caller mark the tool ERROR.
        raise ValueError("image is uniform (max == min); nothing to score")
    arr = xrv.datasets.normalize(arr, peak)          # -> [-1024, 1024]

    arr = arr[None, ...]                              # add channel
    arr = xrv.datasets.XRayCenterCrop()(arr)
    arr = xrv.datasets.XRayResizer(224)(arr)

    with torch.no_grad():
        out = model(torch.from_numpy(arr)[None, ...])[0]

    return {
        name: float(p)
        for name, p in zip(model.pathologies, out)
        if name and name not in _SUPPRESSED
    }
