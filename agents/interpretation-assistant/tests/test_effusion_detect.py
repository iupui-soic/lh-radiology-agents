"""The second pixel-tool row: effusion-detect reads the shared CXR model's Effusion head.

What is guarded here is the TABLE, not a new pipeline: a row added to _PIXEL_TOOL_SPECS must
behave exactly like the first one did under #71's contract (positive->COMPLETE,
negative->STUBBED, degrade paths intact), the study must be fetched and scored ONCE no matter
how many rows are selected, and a broken head must cost only its own row.

Deliberately NOT tested here (cross-agent, documented in _PIXEL_TOOL_SPECS): "effusion" is not
on impression-generation's critical-keyword list, so a positive effusion surfaces on the
worklist/OHIF/pre-sign draft without ever tripping a criticalFlag, a page, or a sign-off
escalation. Runs without torch, same seams as test_pneumothorax_detect.py.
"""
import handler
from handler import handle

CXR_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_cxr_eff",
    "study": {
        "studyInstanceUID": "1.2.3",
        "orthancStudyId": "orth-cxr-1",
        "modality": "CR",
        "studyDescription": "CHEST PA AND LATERAL",
    },
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"priority": "routine", "reasonCode": ["R91.8"]},
    "meta": {"traceId": "trc", "emittedAt": "2026-08-12T00:00:00Z", "source": "test"},
}


class _FakeOrthanc:
    def __init__(self, instances=("inst-frontal", "inst-lateral")):
        self._instances = list(instances)

    async def list_study_instances(self, study_id):
        return self._instances

    async def get_instance_dicom(self, instance_id):
        return instance_id.encode()

    async def get_instance_tags(self, instance_id):
        raise RuntimeError("simplified-tags unavailable")


def _pixels_on(monkeypatch, *, effusion_p, pneumothorax_p=0.10):
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc())
    monkeypatch.setattr(handler, "dicom_to_greyscale", lambda b: [[0, 1], [2, 3]])
    monkeypatch.setattr(handler, "score",
                        lambda arr: {"Pneumothorax": pneumothorax_p, "Effusion": effusion_p})


def _eff(out):
    return next(f for f in out["findings"] if f["toolId"] == "effusion-detect")


def _selected(out, tool_id):
    return next(t for t in out["toolsSelected"] if t["toolId"] == tool_id)


async def test_a_positive_effusion_screen_is_complete_under_its_display_name(monkeypatch):
    """The row's spec drives everything visible: the label carries the clinical display name
    ("Pleural effusion", not the head's bare "Effusion"), the same screening-only wording as the
    first row, and the audit claims the model."""
    _pixels_on(monkeypatch, effusion_p=0.83)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _eff(out)
    assert f["status"] == "COMPLETE"
    assert f["confidence"] == 0.83
    assert f["label"].startswith("Pleural effusion (screening p=0.83")
    assert "screening signal only, not a read" in f["label"]
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"
    assert _selected(out, "effusion-detect")["version"] == "cxr-densenet121-res224-all"


async def test_a_negative_effusion_screen_is_stubbed_so_normals_stay_inert(monkeypatch):
    """#71's "draft only on positives" applies to every row, not just the first: a below-threshold
    effusion must not emit the COMPLETE that arms the pre-sign chart write."""
    _pixels_on(monkeypatch, effusion_p=0.12)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _eff(out)
    assert f["status"] == "STUBBED"
    assert f["confidence"] is None
    assert "negative" in f["label"].lower()
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"  # the model DID run
    assert _selected(out, "effusion-detect")["version"] == "cxr-densenet121-res224-all"


async def test_both_heads_positive_yield_two_complete_findings(monkeypatch):
    """A pneumothorax WITH an effusion is one study, one forward pass, two findings."""
    _pixels_on(monkeypatch, effusion_p=0.83, pneumothorax_p=0.87)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    complete = {f["toolId"] for f in out["findings"] if f["status"] == "COMPLETE"}
    assert complete == {"pneumothorax-detect", "effusion-detect"}
    # both findings cite the same scored instance -- one fetch, one score, two head reads
    refs = {f["evidenceRef"] for f in out["findings"] if f["status"] == "COMPLETE"}
    assert refs == {"orthanc:instance/inst-frontal"}


async def test_the_study_is_scored_once_no_matter_how_many_rows_are_selected(monkeypatch):
    """THE efficiency contract of the table (mutation-checked): revert handle() to per-tool
    scoring and this fails with two calls. The model scores every head in a single forward pass;
    a second inference per row would be the same numbers at N times the cost."""
    calls = []

    def counting_score(arr):
        calls.append(1)
        return {"Pneumothorax": 0.9, "Effusion": 0.9}

    _pixels_on(monkeypatch, effusion_p=0.9)
    monkeypatch.setattr(handler, "score", counting_score)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert len([f for f in out["findings"] if f["status"] == "COMPLETE"]) == 2
    assert len(calls) == 1


async def test_a_missing_effusion_head_errors_that_row_only(monkeypatch):
    """Weights without an Effusion head cost the effusion row an honest ERROR; the pneumothorax
    row still screens. One row's head is never another row's failure."""
    _pixels_on(monkeypatch, effusion_p=0.0)
    monkeypatch.setattr(handler, "score", lambda arr: {"Pneumothorax": 0.87})  # no Effusion head
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _eff(out)["status"] == "ERROR"
    assert "KeyError" in _eff(out)["label"]
    ptx = next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")
    assert ptx["status"] == "COMPLETE"


async def test_effusion_referral_rule_matches_the_j90_j91_families():
    """The degrade path (no pixel tooling, the conftest default): a study whose order is coded
    for pleural effusion cross-checks the referral reason, stays STUBBED, and never claims the
    model. J91.0 (malignant) matches through dot-normalisation + the J91 family prefix."""
    for code in ("J90", "J91.0"):
        ctx = {**CXR_CONTEXT, "order": {"priority": "routine", "reasonCode": [code]}}
        out = await handle("interpretation.runTools", {"studyContext": ctx})
        f = _eff(out)
        assert f["status"] == "STUBBED"
        assert f["evidenceRef"] == f"order.reasonCode={code}"
        assert "pleural effusion" in f["label"]
        assert _selected(out, "effusion-detect")["version"] == "referral-rule-1"


async def test_other_pleural_condition_codes_stay_unmatched():
    """J94.x (chylous effusion, hemothorax, ...) is other-pleural, not the effusion families --
    the rule must not fire on it (over-narrow beats over-broad, same doctrine as the TAVR
    exclusion)."""
    ctx = {**CXR_CONTEXT, "order": {"priority": "routine", "reasonCode": ["J94.0"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    f = _eff(out)
    assert f["status"] == "STUBBED"
    assert f["label"] == ""          # bare stub, not a referral hit
    assert f["evidenceRef"] is None
