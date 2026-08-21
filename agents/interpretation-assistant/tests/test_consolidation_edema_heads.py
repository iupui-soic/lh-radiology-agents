"""Rows three and four: consolidation-detect and edema-detect read the shared CXR model's
Consolidation and Edema heads (#27, PI-approved "all three: effusion, consolidation and edema").

What is guarded here is the TABLE scaling past two rows, not a new pipeline. Three properties
that only become testable once more than one non-critical row exists:

  * ONE forward pass still serves every selected row, however many there are -- the cost of a
    new row must be a dict lookup, never another inference;
  * each row reads ITS OWN head, so two rows on the same study can disagree (one COMPLETE, one
    STUBBED) without contaminating each other;
  * a head missing from the loaded weights costs ONLY its own row (ERROR), never the study.

Deliberately NOT tested here (cross-agent, documented in _PIXEL_TOOL_SPECS): neither
"Consolidation" nor "Pulmonary edema" contains a term from impression-generation's
_CRITICAL_KEYWORDS, so a positive on either surfaces on the worklist, in OHIF and in the
pre-sign draft without ever tripping a criticalFlag, a page, or a sign-off escalation. Paging
stays deterministic off report text (#78). Runs without torch, same seams as the sibling tests.
"""
import handler
from handler import handle

CXR_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_cxr_conso",
    "study": {
        "studyInstanceUID": "1.2.3",
        "orthancStudyId": "orth-cxr-2",
        "modality": "CR",
        "studyDescription": "CHEST PA AND LATERAL",
    },
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"priority": "routine", "reasonCode": ["R91.8"]},
    "meta": {"traceId": "trc", "emittedAt": "2026-08-20T00:00:00Z", "source": "test"},
}


class _CountingOrthanc:
    """Counts DICOM fetches so "one pass per study" is asserted, not assumed."""
    fetches = 0

    async def list_study_instances(self, study_id):
        return ["inst-frontal", "inst-lateral"]

    async def get_instance_dicom(self, instance_id):
        type(self).fetches += 1
        return instance_id.encode()

    async def get_instance_tags(self, instance_id):
        raise RuntimeError("simplified-tags unavailable")


def _pixels_on(monkeypatch, *, probs, counter=None):
    calls = {"score": 0}
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: (counter or _CountingOrthanc)())
    monkeypatch.setattr(handler, "dicom_to_greyscale", lambda b: [[0, 1], [2, 3]])

    def _score(arr):
        calls["score"] += 1
        return probs

    monkeypatch.setattr(handler, "score", _score)
    return calls


def _find(out, tool_id):
    return next(f for f in out["findings"] if f["toolId"] == tool_id)


ALL_QUIET = {"Pneumothorax": 0.10, "Effusion": 0.10, "Consolidation": 0.10, "Edema": 0.10}


# --- the rows exist and are selected on a chest film ------------------------------------

async def test_both_new_rows_are_selected_on_a_chest_film(monkeypatch):
    _pixels_on(monkeypatch, probs=ALL_QUIET)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    selected = {t["toolId"] for t in out["toolsSelected"]}
    assert {"consolidation-detect", "edema-detect"} <= selected


# --- positive -> COMPLETE, negative -> STUBBED, per row ---------------------------------

async def test_a_positive_consolidation_is_complete(monkeypatch):
    _pixels_on(monkeypatch, probs={**ALL_QUIET, "Consolidation": 0.80})
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _find(out, "consolidation-detect")
    assert f["status"] == "COMPLETE"
    assert f["confidence"] == 0.80
    assert f["evidenceRef"].startswith("orthanc:instance/")
    assert "Consolidation" in f["label"]


async def test_a_positive_edema_is_complete_and_says_pulmonary_edema(monkeypatch):
    _pixels_on(monkeypatch, probs={**ALL_QUIET, "Edema": 0.90})
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _find(out, "edema-detect")
    assert f["status"] == "COMPLETE"
    assert "Pulmonary edema" in f["label"]


async def test_negatives_stay_stubbed_so_a_normal_film_arms_nothing(monkeypatch):
    _pixels_on(monkeypatch, probs=ALL_QUIET)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    for tool_id in ("consolidation-detect", "edema-detect"):
        f = _find(out, tool_id)
        assert f["status"] == "STUBBED", tool_id
        # A negative reports no confidence but still records WHICH instance was looked at --
        # "the model ran and found nothing" must be distinguishable from "nothing ran".
        assert f["confidence"] is None
        assert f["evidenceRef"].startswith("orthanc:instance/")


# --- the properties that only appear once the table has several rows --------------------

async def test_four_rows_still_cost_exactly_one_forward_pass(monkeypatch):
    """A new row is a dict lookup, not another inference. If this ever fails, adding heads has
    started costing real compute per row and the table design is broken."""
    _CountingOrthanc.fetches = 0
    calls = _pixels_on(monkeypatch, probs=ALL_QUIET)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    pixel_rows = [f for f in out["findings"]
                  if f["toolId"] in {"pneumothorax-detect", "effusion-detect",
                                     "consolidation-detect", "edema-detect"}]
    assert len(pixel_rows) == 4
    assert calls["score"] == 1


async def test_two_rows_on_one_study_do_not_contaminate_each_other(monkeypatch):
    """Consolidation positive while edema is negative, on the same forward pass."""
    _pixels_on(monkeypatch, probs={**ALL_QUIET, "Consolidation": 0.85})
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _find(out, "consolidation-detect")["status"] == "COMPLETE"
    assert _find(out, "edema-detect")["status"] == "STUBBED"
    assert _find(out, "pneumothorax-detect")["status"] == "STUBBED"


async def test_a_missing_head_costs_only_its_own_row(monkeypatch):
    """Weights without an Edema head: edema-detect is an honest ERROR and every other row is
    unaffected. A silent skip here would read as "screened, nothing found"."""
    probs = {k: v for k, v in ALL_QUIET.items() if k != "Edema"}
    _pixels_on(monkeypatch, probs={**probs, "Consolidation": 0.85})
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _find(out, "edema-detect")["status"] == "ERROR"
    assert _find(out, "consolidation-detect")["status"] == "COMPLETE"
    assert _find(out, "pneumothorax-detect")["status"] == "STUBBED"


async def test_neither_label_carries_an_impression_critical_keyword(monkeypatch):
    """The cross-agent property these rows depend on, pinned HERE because the coupling is easy to
    break from this side: impression-generation scans each COMPLETE finding LABEL against
    _CRITICAL_KEYWORDS (#26), so renaming a display string to something that contains one would
    silently turn a non-critical screening signal into a page."""
    critical_terms = ("dissection", "pneumothorax", "hemorrhage", "occlusion", "rupture",
                      "infarct", "embolism", "fracture", "mass", "tumor")
    _pixels_on(monkeypatch, probs={**ALL_QUIET, "Consolidation": 0.85, "Edema": 0.85})
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    for tool_id in ("consolidation-detect", "edema-detect"):
        label = _find(out, tool_id)["label"].lower()
        assert not [t for t in critical_terms if t in label], (tool_id, label)
