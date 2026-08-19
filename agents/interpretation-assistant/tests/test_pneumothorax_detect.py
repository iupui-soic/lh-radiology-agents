"""The real CXR model behind pneumothorax-detect (#71, slice of #27).

These run WITHOUT torch. The handler decides once at import whether the pixel/model extras exist
(handler.PIXEL_TOOLING) and reaches Orthanc and the model through module-level names, so a test can
substitute both. A seam that only exists when a 1.5GB dependency is installed is a seam CI never
exercises -- and the agent-tests lane installs neither extra, by design (conftest defaults it off).

What is guarded here is the HANDLER's contract around the model, not the model's accuracy:
  * a POSITIVE screen (Pneumothorax p >= threshold) becomes a COMPLETE finding with confidence and
    the instance it scored -- and that COMPLETE is what arms the pre-sign draft;
  * a NEGATIVE screen (p < threshold) reports STUBBED, NOT COMPLETE ("draft only on positives"), so
    a normal study never triggers a pre-sign chart write -- while still recording (evidenceRef +
    version) that the model DID run;
  * every way the model can fail to LOOK degrades to the referral rule / stub, never a fabricated
    negative; a model that threw is an honest ERROR;
  * toolsSelected[].version never claims a model that did not run.
"""
import pytest

import handler
from handler import handle

# reasonCode R91.8 does NOT match the pneumothorax referral rule (J93*/S270XXA/J95811), so a
# degrade falls through to a bare stub -- keeps the pixel-vs-stub tests unambiguous. The J93 variant
# is exercised explicitly below.
CXR_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_cxr",
    "study": {
        "studyInstanceUID": "1.2.3",
        "orthancStudyId": "orth-cxr-1",
        "modality": "CR",
        "studyDescription": "CHEST PA AND LATERAL",
    },
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"priority": "routine", "reasonCode": ["R91.8"]},
    "meta": {"traceId": "trc", "emittedAt": "2026-07-15T00:00:00Z", "source": "test"},
}


class _FakeOrthanc:
    """Stands in for a PACS. instances=[] models a study Orthanc has metadata for but no images.

    tags=None models a client/PACS where per-instance tags cannot be fetched -- the frontal-first
    reorder must then leave acquisition order alone, which is also why every pre-existing test in
    this file (they never set tags) still describes the old first-scoreable behaviour."""

    def __init__(self, instances=("inst-frontal", "inst-lateral"), tags=None):
        self._instances = list(instances)
        self._tags = tags

    async def list_study_instances(self, study_id):
        return self._instances

    async def get_instance_dicom(self, instance_id):
        return instance_id.encode()  # bytes just carry which instance this is

    async def get_instance_tags(self, instance_id):
        if self._tags is None:
            raise RuntimeError("simplified-tags unavailable")
        return self._tags.get(instance_id, {})


def _pixels_on(monkeypatch, *, pneumothorax_p):
    """Turn the pixel path on with fakes for Orthanc, the decoder, and the model. The model returns
    a probability dict with the pneumothorax head set to `pneumothorax_p`."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc())
    monkeypatch.setattr(handler, "dicom_to_greyscale", lambda b: [[0, 1], [2, 3]])
    monkeypatch.setattr(handler, "score", lambda arr: {"Pneumothorax": pneumothorax_p, "Nodule": 0.10})


def _ptx(out):
    return next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")


def _selected(out, tool_id):
    return next(t for t in out["toolsSelected"] if t["toolId"] == tool_id)


async def test_a_positive_screen_becomes_a_complete_finding_with_confidence(monkeypatch):
    _pixels_on(monkeypatch, pneumothorax_p=0.87)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "COMPLETE"
    assert f["confidence"] == 0.87
    assert "pneumothorax" in f["label"].lower()
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"  # the frontal, first in order
    assert _selected(out, "pneumothorax-detect")["version"] == "cxr-densenet121-res224-all"
    # a lone positive pixel finding makes the whole run PARTIAL (cxr-screen alongside stays STUBBED)
    assert out["overallStatus"] == "PARTIAL"


async def test_a_negative_screen_is_stubbed_not_complete_so_normals_stay_inert(monkeypatch):
    """THE #71 decision, mutation-checked. A negative screen must NOT emit COMPLETE: COMPLETE trips
    the unconditional pre-sign chart write in workflow.py, which would put "No acute findings" into
    every normal patient's chart ahead of the read. So a below-threshold pneumothorax reports
    STUBBED -- the model ran (evidenceRef + version prove it), it just offers no draft.

    Flip the `>=` branch in _head_finding to always return COMPLETE and this fails."""
    _pixels_on(monkeypatch, pneumothorax_p=0.12)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "STUBBED"                      # NOT COMPLETE -> no pre-sign draft
    assert f["confidence"] is None
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"  # but the model DID run
    assert "negative" in f["label"].lower()
    # version still records the model ran, distinguishing it from a never-ran stub
    assert _selected(out, "pneumothorax-detect")["version"] == "cxr-densenet121-res224-all"
    # nothing COMPLETE anywhere -> the whole run is STUBBED, so _has_complete_finding stays False
    assert out["overallStatus"] == "STUBBED"


async def test_a_negative_screen_label_carries_no_critical_keyword_that_would_trip_a_flag(monkeypatch):
    """Belt-and-suspenders on the safety property. Even though STUBBED labels are not scanned by
    impression-generation today, the negative label is negation-worded so it stays correct once the
    scan becomes negation-aware (#78): it says "negative"/"no finding", never a bare critical claim."""
    _pixels_on(monkeypatch, pneumothorax_p=0.30)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    label = _ptx(out)["label"].lower()
    assert "negative" in label and "no finding" in label


async def test_the_model_result_supersedes_the_referral_reason_code(monkeypatch):
    """A study coded J93 (suspected pneumothorax) that the model scores NEGATIVE reports the model's
    STUBBED negative, NOT the referral-reason cross-check: the pixel read is the better signal, and
    falling back to "referral coded J93" after actually looking would be misleading."""
    _pixels_on(monkeypatch, pneumothorax_p=0.12)
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["J93.1"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    f = _ptx(out)
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"   # model, not order.reasonCode=J93.1
    assert "negative" in f["label"].lower()


async def test_it_scores_the_first_instance_in_order_not_an_arbitrary_one(monkeypatch):
    """The frontal, not the lateral. list_study_instances guarantees (SeriesNumber, InstanceNumber)
    order; the handler must take the first scoreable instance and say which one it scored."""
    _pixels_on(monkeypatch, pneumothorax_p=0.90)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _ptx(out)["evidenceRef"] == "orthanc:instance/inst-frontal"


async def test_a_lateral_first_study_scores_the_frontal_view(monkeypatch):
    """Acquisition order is NOT projection order on real data: 17/100 MIMIC showcase studies lead
    with the lateral (measured live 2026-07-24), and the model is frontal-trained. ViewPosition
    must outrank acquisition order."""
    _pixels_on(monkeypatch, pneumothorax_p=0.90)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc(
        instances=("inst-lateral", "inst-frontal"),
        tags={"inst-lateral": {"ViewPosition": "LL"}, "inst-frontal": {"ViewPosition": "PA"}}))
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _ptx(out)["evidenceRef"] == "orthanc:instance/inst-frontal"


async def test_an_unlabelled_view_outranks_a_known_lateral(monkeypatch):
    """A portable AP often ships a blank ViewPosition. Unknown MIGHT be frontal; LL definitely is
    not -- so blank sorts ahead of a known non-frontal, and a study with no frontal at all still
    gets scored (never a new degrade path)."""
    _pixels_on(monkeypatch, pneumothorax_p=0.90)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc(
        instances=("inst-lateral", "inst-portable"),
        tags={"inst-lateral": {"ViewPosition": "LATERAL"}, "inst-portable": {}}))
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _ptx(out)["evidenceRef"] == "orthanc:instance/inst-portable"


async def test_tag_fetch_failure_keeps_acquisition_order(monkeypatch):
    """Reordering is best-effort: a PACS that cannot serve per-instance tags costs nothing but the
    reorder. The default _FakeOrthanc (tags=None) raises on get_instance_tags, so this is also the
    posture every other test in this file runs under."""
    _pixels_on(monkeypatch, pneumothorax_p=0.90)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc(
        instances=("inst-lateral", "inst-frontal")))
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _ptx(out)["evidenceRef"] == "orthanc:instance/inst-lateral"


async def test_without_the_model_extras_it_falls_back_to_the_referral_rule(monkeypatch):
    """The agent-tests CI lane (PIXEL_TOOLING False, the conftest default). A J93 study degrades to
    the referral-reason STUBBED cross-check, not a pixel result -- and must not claim a model."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", False)
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["J93.1"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    f = _ptx(out)
    assert f["status"] == "STUBBED"
    assert f["evidenceRef"] == "order.reasonCode=J93.1"
    assert _selected(out, "pneumothorax-detect")["version"] == "referral-rule-1"


async def test_a_study_with_no_instances_degrades_and_does_not_invent_a_negative(monkeypatch):
    """Orthanc has the study but no images. The tool could not look, so it must not say "nothing
    found" -- it falls through to the referral rule (here unmatched -> bare stub), never COMPLETE."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc(instances=[]))
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "STUBBED"
    assert f["label"] == ""          # NOT "no acute findings", NOT a model negative
    assert f["confidence"] is None


async def test_a_model_failure_is_an_honest_error_not_a_negative(monkeypatch):
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc())
    monkeypatch.setattr(handler, "dicom_to_greyscale", lambda b: [[0, 1], [2, 3]])

    def boom(arr):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(handler, "score", boom)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "ERROR"
    assert f["confidence"] is None
    assert "RuntimeError" in f["label"]
    # the model reached a real instance before throwing, so the ERROR records which one -- and only
    # then does the audit attribute it to the model
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"
    assert _selected(out, "pneumothorax-detect")["version"] == "cxr-densenet121-res224-all"


async def test_an_orthanc_outage_degrades_and_is_not_attributed_to_the_model(monkeypatch):
    """A transport failure (Orthanc unreachable) is NOT a model failure: the model never ran. It
    must DEGRADE to the referral rule / stub, never ERROR, and the audit must NOT claim the model
    version -- claiming a model that never ran is the same lie as inventing a finding.

    Mutation: fold the fetch stage back under the model-stage try (so Orthanc errors become ERROR),
    or stamp the model version on status==ERROR, and this fails."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)

    class _DeadOrthanc:
        async def list_study_instances(self, study_id):
            raise ConnectionError("orthanc unreachable")

        async def get_instance_dicom(self, instance_id):  # pragma: no cover - never reached
            raise ConnectionError("orthanc unreachable")

    monkeypatch.setattr(handler, "OrthancClient", lambda: _DeadOrthanc())
    # J93 so the degrade lands on the referral-reason cross-check, proving it fell through
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["J93.1"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    f = _ptx(out)
    assert f["status"] == "STUBBED"                      # degraded, NOT ERROR
    assert f["evidenceRef"] == "order.reasonCode=J93.1"  # fell through to the referral rule
    assert _selected(out, "pneumothorax-detect")["version"] == "referral-rule-1"  # NOT the model


async def test_a_model_missing_the_target_head_is_an_honest_error(monkeypatch):
    """If the loaded weights ever lack a Pneumothorax head, reading it is a KeyError -- which must
    surface as an honest ERROR, not a crash and not a silent miss."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc())
    monkeypatch.setattr(handler, "dicom_to_greyscale", lambda b: [[0, 1], [2, 3]])
    monkeypatch.setattr(handler, "score", lambda arr: {"Nodule": 0.10})  # no Pneumothorax head
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _ptx(out)["status"] == "ERROR"


async def test_skips_a_non_image_instance_and_scores_the_first_real_image(monkeypatch):
    """A non-image object -- a Structured Report, a radiation-dose SR, a presentation state -- can
    sort AHEAD of the frontal image. The tool must SKIP it and score the first real image, not abort
    the whole study on instances[0]."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)

    class _SRThenImage:
        async def list_study_instances(self, study_id):
            return ["inst-SR", "inst-frontal"]      # the SR sorts first

        async def get_instance_dicom(self, instance_id):
            return instance_id.encode()

    monkeypatch.setattr(handler, "OrthancClient", lambda: _SRThenImage())

    def decode(b):
        if b == b"inst-SR":
            raise handler.NotAnImage("Structured Report, no PixelData")
        return [[0, 1], [2, 3]]

    monkeypatch.setattr(handler, "dicom_to_greyscale", decode)
    monkeypatch.setattr(handler, "score", lambda arr: {"Pneumothorax": 0.91})
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "COMPLETE"                             # scored, not degraded
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"  # the frontal, not the SR


async def test_a_study_with_no_orthanc_id_degrades(monkeypatch):
    _pixels_on(monkeypatch, pneumothorax_p=0.90)
    ctx = {**CXR_CONTEXT, "study": {**CXR_CONTEXT["study"], "orthancStudyId": ""}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    assert _ptx(out)["status"] == "STUBBED"      # no study -> could not look -> not COMPLETE


async def test_the_model_never_runs_on_a_non_chest_study(monkeypatch):
    """The registry is the ONLY thing keeping non-CXRs away from a model that will confidently score
    anything. A head CT must not select pneumothorax-detect at all."""
    _pixels_on(monkeypatch, pneumothorax_p=0.90)
    ctx = {
        **CXR_CONTEXT,
        "study": {**CXR_CONTEXT["study"], "modality": "CT", "studyDescription": "CT HEAD W/O"},
    }
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    assert not any(f["toolId"] == "pneumothorax-detect" for f in out["findings"])


# --- margin fields + configurable operating point (#86) -----------------------------------------
# Calibrated positives crowd into 0.50-0.53 because the op-norm maps the head's tiny raw operating
# point (~0.0098) to 0.5. The finding therefore carries the raw sigmoid and the op point so a
# surface can show a margin, and the threshold is env-configurable per deployment.

def test_raw_sigmoid_inverts_the_op_norm_exactly():
    """Pin the inverse against the forward formula: c = r/(2t) below t, 1 - (1-r)/(2(1-t)) above.
    Mutation: swap a sign or a branch and the round trip breaks."""
    t = 0.0098
    for r in (0.0, 0.0021, t, 0.02, 0.31, 0.97):
        c = (r / (2 * t)) if r < t else 1.0 - ((1.0 - r) / (2 * (1.0 - t)))
        assert handler._raw_sigmoid(c, t) == pytest.approx(r, abs=1e-12)
    # the op point itself maps to exactly calibrated 0.5, both directions
    assert handler._raw_sigmoid(0.5, t) == pytest.approx(t)
    # no op-norm applied -> the calibrated score IS the raw sigmoid
    assert handler._raw_sigmoid(0.51, None) == 0.51


async def test_findings_carry_raw_score_and_op_threshold_on_both_outcomes(monkeypatch):
    """#86 ask 1: the margin rides the payload, positive AND negative. A calibrated 0.51 with the
    real op point is raw ~0.0298 -- three times the operating point, not a coin flip."""
    _pixels_on(monkeypatch, pneumothorax_p=0.51)
    monkeypatch.setattr(handler, "op_threshold", lambda name: 0.0098)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "COMPLETE"
    assert f["opThreshold"] == 0.0098
    assert f["rawScore"] == pytest.approx(1.0 - 2 * (1 - 0.0098) * (1 - 0.51))
    assert "raw" in f["label"] and "op" in f["label"]  # the margin is readable, not just machine

    _pixels_on(monkeypatch, pneumothorax_p=0.20)
    monkeypatch.setattr(handler, "op_threshold", lambda name: 0.0098)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "STUBBED"
    assert f["confidence"] is None                      # the #71 negative posture is untouched
    assert f["rawScore"] == pytest.approx(2 * 0.0098 * 0.20)
    assert f["opThreshold"] == 0.0098


async def test_margin_fields_degrade_to_null_when_the_op_point_is_unknown(monkeypatch):
    """No op point (no-torch lane, or weights without one) -> nulls and the old label, never a
    fabricated number. rawScore falls back to the calibrated score itself."""
    _pixels_on(monkeypatch, pneumothorax_p=0.87)
    monkeypatch.setattr(handler, "op_threshold", None)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "COMPLETE"
    assert f["opThreshold"] is None
    assert f["rawScore"] == 0.87
    assert "raw" not in f["label"]


async def test_positive_threshold_is_env_configurable(monkeypatch):
    """#86 ask 2: a deployment can tighten the operating point without a release. p=0.51 clears the
    0.5 default but must NOT clear a 0.55 override."""
    _pixels_on(monkeypatch, pneumothorax_p=0.51)
    monkeypatch.setattr(handler, "POSITIVE_THRESHOLD", 0.55)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _ptx(out)["status"] == "STUBBED"


def test_threshold_env_read_survives_the_empty_compose_passthrough(monkeypatch):
    """The compose pass-through hands an EMPTY string when the var is unset; a bare float() of it
    would keep the whole agent from booting. Exercise the REAL module-level read by reloading:
    empty -> the 0.5 default, a value -> that value. The final reload restores the default for the
    rest of the suite (monkeypatch undoes the env)."""
    import importlib

    monkeypatch.setenv("CXR_POSITIVE_THRESHOLD", "")
    assert importlib.reload(handler).POSITIVE_THRESHOLD == 0.5   # would raise before the fix
    monkeypatch.setenv("CXR_POSITIVE_THRESHOLD", "0.62")
    assert importlib.reload(handler).POSITIVE_THRESHOLD == 0.62
    monkeypatch.delenv("CXR_POSITIVE_THRESHOLD")
    assert importlib.reload(handler).POSITIVE_THRESHOLD == 0.5
