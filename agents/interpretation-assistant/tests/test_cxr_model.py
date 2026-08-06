"""Coverage for the REAL model behind pneumothorax-detect: cxr_model.score / preprocessing.

Skipped unless torch + torchxrayvision are installed -- the agent-tests CI lane installs neither, so
this stays torch-free there, the same gate as handler.PIXEL_TOOLING. Where they ARE installed (a dev
machine, or a torch-enabled lane), this runs the actual DenseNet over a synthetic frame and pins the
handler's contract with the model: score() returns a probability per head, the target head is
present, and a signal-less frame is refused rather than scored as a fabricated negative.
"""
import pytest

pytest.importorskip("torch")
pytest.importorskip("torchxrayvision")
np = pytest.importorskip("numpy")

import cxr_model  # noqa: E402
from cxr_model import TARGET_PATHOLOGY, score  # noqa: E402


def _synthetic_frame():
    """A non-uniform greyscale frame. The content is irrelevant -- these tests pin mechanics, not
    accuracy -- but it must carry a range, or score() correctly refuses it."""
    rng = np.linspace(0.0, 4000.0, 64 * 64, dtype=np.float32).reshape(64, 64)
    return rng


def test_score_returns_a_probability_per_head_including_the_target():
    probs = score(_synthetic_frame())
    assert isinstance(probs, dict) and probs
    assert TARGET_PATHOLOGY in probs, "the model must expose the head pneumothorax-detect reads"
    for name, p in probs.items():
        assert isinstance(p, float)
        assert 0.0 <= p <= 1.0, f"{name} probability out of range: {p}"


def test_suppressed_head_is_not_reported():
    probs = score(_synthetic_frame())
    assert "Enlarged Cardiomediastinum" not in probs


def test_a_uniform_frame_is_refused_not_scored_as_a_negative():
    """A flat frame carries no signal. Scoring it would fabricate a NEGATIVE the caller then reports
    -- the automation-bias trap. score() raises so the handler marks the tool ERROR instead."""
    flat = np.full((64, 64), 7.0, dtype=np.float32)
    with pytest.raises(ValueError):
        score(flat)


def test_model_is_loaded_once_and_cached():
    score(_synthetic_frame())
    first = cxr_model._model
    score(_synthetic_frame())
    assert cxr_model._model is first, "the DenseNet must be built once and reused"


def test_op_threshold_reports_the_heads_operating_point_after_a_score():
    """#86: the head's raw operating point backs the margin fields. Scored first so the weights are
    in memory -- op_threshold deliberately never loads them itself (see its docstring)."""
    score(_synthetic_frame())
    t = cxr_model.op_threshold(TARGET_PATHOLOGY)
    assert t is not None and 0.0 < t < 0.1, "the -all weights put Pneumothorax's op point near 0.0098"
    assert cxr_model.op_threshold("NotARealHead") is None
