"""Contract test + selection tests for interpretation assistant."""
import json
from pathlib import Path

import pytest

from handler import handle
from registry import select_tools
from radagent_common.validation import validate_skill_output

SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_test",
    "study": {
        "studyInstanceUID": "1.2.3",
        "orthancStudyId": "abc123",
        "modality": "CT",
        "studyDescription": "CT CHEST W/O",
    },
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"priority": "routine", "reasonCode": ["R91.8"]},
    "meta": {"traceId": "trc_x", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


async def test_output_conforms_to_contract():
    out = await handle("interpretation.runTools", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("interpretation.runTools", out)
    assert out["workflowId"] == "wf_test"


# Selection tests by modality/study-type
def test_ct_chest_selects_lung_nodule():
    tools = select_tools("CT", "CT CHEST W/O CONTRAST")
    assert "lung-nodule-detect" in tools

def test_ct_head_selects_ich():
    tools = select_tools("CT", "CT HEAD W/O CONTRAST")
    assert "ich-detect" in tools

def test_ct_aorta_selects_dissection():
    tools = select_tools("CT", "CT AORTA W CONTRAST")
    assert "aortic-dissection-detect" in tools

def test_mr_brain_selects_tumor_screen():
    tools = select_tools("MR", "MR BRAIN W/O CONTRAST")
    assert "brain-tumor-screen" in tools

def test_cr_chest_selects_cxr_screen():
    tools = select_tools("CR", "CHEST AP")
    assert "cxr-screen" in tools

def test_mg_selects_mammo_screen():
    tools = select_tools("MG", "MAMMOGRAM BILATERAL")
    assert "mammo-screen" in tools

def test_unknown_modality_returns_empty():
    tools = select_tools("XY", "UNKNOWN STUDY")
    assert tools == []

@pytest.mark.parametrize("modality,description,expected", [
    ("PT", "PET CT WHOLE BODY FDG", ["fdg-uptake-screen"]),
    ("NM", "NM BONE SCAN", ["bone-scan-screen"]),
    ("XA", "XA CORONARY ANGIOGRAM", ["coronary-stenosis-detect"]),
])
def test_new_modalities_select_regional_tools(modality, description, expected):
    assert select_tools(modality, description) == expected


@pytest.mark.parametrize("modality,description,expected", [
    ("CT", "CT PELVIS W CONTRAST", ["pelvic-fracture-detect"]),
    ("MR", "MR KNEE W/O CONTRAST", ["knee-mri-screen"]),
    ("MR", "MR SHOULDER W/O CONTRAST", ["shoulder-mri-screen"]),
    ("US", "US THYROID", ["thyroid-nodule-detect"]),
    ("US", "US PELVIS", ["pelvic-us-screen"]),
])
def test_new_regions_select_regional_tools(modality, description, expected):
    assert select_tools(modality, description) == expected


@pytest.mark.parametrize("modality,expected", [
    ("CT", ["generic-ct-screen"]),
    ("MR", ["generic-mr-screen"]),
    ("CR", ["generic-xr-screen"]),
    ("DX", ["generic-xr-screen"]),
    ("MG", ["mammo-screen"]),
    ("US", ["generic-us-screen"]),
    ("PT", ["generic-pet-screen"]),
    ("NM", ["generic-nm-screen"]),
    ("XA", ["generic-xa-screen"]),
])
def test_every_registered_modality_has_a_fallback(modality, expected):
    assert select_tools(modality, "MISC RESEARCH PROTOCOL") == expected


def test_ct_multi_region_collects_all_matching_regions():
    tools = select_tools("CT", "CT CHEST ABDOMEN PELVIS")
    assert tools == [
        "lung-nodule-detect",
        "pe-detect",
        "liver-lesion-detect",
        "pelvic-fracture-detect",
    ]

def test_ct_no_region_match_falls_back_to_star():
    tools = select_tools("CT", "CT MISC PROTOCOL")
    assert tools == ["generic-ct-screen"]


# Real first slice (#27): pneumothorax-detect cross-checks the referral reason code.
CXR_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_cxr_test",
    "study": {
        "studyInstanceUID": "1.2.3.4",
        "orthancStudyId": "cxr-study-001",
        "modality": "CR",
        "studyDescription": "CHEST AP",
    },
    "patient": {"fhirPatientId": "Patient/2"},
    "order": {"priority": "stat", "reasonCode": ["J93.1"]},
    "meta": {"traceId": "trc_y", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


async def test_pneumothorax_reason_code_records_evidence_but_stays_stubbed():
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    validate_skill_output("interpretation.runTools", out)
    ptx = next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")
    # STUBBED, not COMPLETE: a referral reason code is not pixel-level evidence, and COMPLETE
    # gates the pre-sign fhir2 chart write -- see the comment in handler.py.
    assert ptx["status"] == "STUBBED"
    assert "J93.1" in ptx["label"]
    assert ptx["evidenceRef"] == "order.reasonCode=J93.1"
    assert out["overallStatus"] == "STUBBED"


async def test_pneumothorax_without_matching_reason_code_stays_stubbed():
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["R05"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    ptx = next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")
    assert ptx["status"] == "STUBBED"
    assert ptx["evidenceRef"] is None
    assert out["overallStatus"] == "STUBBED"


async def test_no_reason_code_at_all_stays_stubbed():
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat"}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    ptx = next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")
    assert ptx["status"] == "STUBBED"


async def test_postprocedural_pneumothorax_code_records_evidence():
    # J95.811 (postprocedural pneumothorax, e.g. r/o PTX post-line film) added per lead review.
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["J95.811"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    ptx = next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")
    assert ptx["status"] == "STUBBED"
    assert ptx["evidenceRef"] == "order.reasonCode=J95.811"


async def test_tools_selected_version_reflects_referral_rule_hit():
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    ptx = next(t for t in out["toolsSelected"] if t["toolId"] == "pneumothorax-detect")
    assert ptx["version"] == "referral-rule-1"
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["R05"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    ptx = next(t for t in out["toolsSelected"] if t["toolId"] == "pneumothorax-detect")
    assert ptx["version"] == "stub-0"


# Second real slice (#27): pe-detect cross-checks the referral reason code, table-driven off the
# same _reason_finding rule as pneumothorax-detect (per lead review).
# NOTE: studyDescription is "CT CHEST" rather than the real-world "CTPA" protocol name on purpose
# -- the registry's alias/word-boundary matching that would resolve "CTPA" to the chest region
# lives on a separate, not-yet-merged branch (#63). Keep that dependency out of this branch's
# tests; once both merge, "CTPA" will resolve the same way.
CTPA_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_ctpa_test",
    "study": {
        "studyInstanceUID": "1.2.3.5",
        "orthancStudyId": "ctpa-study-001",
        "modality": "CT",
        "studyDescription": "CT CHEST W CONTRAST",
    },
    "patient": {"fhirPatientId": "Patient/3"},
    "order": {"priority": "stat", "reasonCode": ["I26.99"]},
    "meta": {"traceId": "trc_z", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


async def test_pe_reason_code_records_evidence_but_stays_stubbed():
    out = await handle("interpretation.runTools", {"studyContext": CTPA_CONTEXT})
    validate_skill_output("interpretation.runTools", out)
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["status"] == "STUBBED"
    assert "I26.99" in pe["label"]
    assert pe["evidenceRef"] == "order.reasonCode=I26.99"
    assert out["overallStatus"] == "STUBBED"


async def test_pe_with_acute_cor_pulmonale_code_records_evidence():
    ctx = {**CTPA_CONTEXT, "order": {"priority": "stat", "reasonCode": ["I26.02"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["evidenceRef"] == "order.reasonCode=I26.02"


async def test_pe_obstetric_thromboembolism_code_records_evidence():
    # O88.2x (obstetric thromboembolism) sits outside I26 for PE in pregnancy/puerperium --
    # added per lead review.
    ctx = {**CTPA_CONTEXT, "order": {"priority": "stat", "reasonCode": ["O88.212"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["evidenceRef"] == "order.reasonCode=O88.212"


async def test_pe_without_matching_reason_code_stays_stubbed():
    ctx = {**CTPA_CONTEXT, "order": {"priority": "stat", "reasonCode": ["R91.8"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["status"] == "STUBBED"
    assert pe["evidenceRef"] is None
    assert out["overallStatus"] == "STUBBED"


# #27 follow-up (Saptarshi/Pranathi): worklist-triage normalises order.reasonCode to a 3-char
# ICD-10 prefix and escalated "I26"/"I2699" as urgent PE while the old exact-string list here
# stayed silent on the identical code -- the two agents disagreed about the same order.
async def test_pe_bare_category_code_records_evidence():
    ctx = {**CTPA_CONTEXT, "order": {"priority": "stat", "reasonCode": ["I26"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["evidenceRef"] == "order.reasonCode=I26"


async def test_pe_code_without_dot_records_evidence():
    ctx = {**CTPA_CONTEXT, "order": {"priority": "stat", "reasonCode": ["I2699"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["evidenceRef"] == "order.reasonCode=I2699"


async def test_pe_other_obstetric_embolism_code_stays_unmatched():
    # O88.0 (air embolism) sits in the same O88 family as O88.2 (thromboembolism) but is not PE --
    # the prefix must stay the 4-char "O882", not widen to the 3-char "O88" family.
    ctx = {**CTPA_CONTEXT, "order": {"priority": "stat", "reasonCode": ["O88.0"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["evidenceRef"] is None


# Pins "O882" as a PREFIX, not an exact match: the billable obstetric-PE children (O88.211,
# O88.219, O88.22, O88.23) must keep matching. Without this, the O88.0-stays-unmatched test above
# alone would pass just as well if O882 were tightened to an exact match -- and every billable
# child would go silent while the suite stayed green (Pranathi, MR review).
async def test_pe_obstetric_embolism_child_code_records_evidence():
    ctx = {**CTPA_CONTEXT, "order": {"priority": "stat", "reasonCode": ["O88.211"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["evidenceRef"] == "order.reasonCode=O88.211"


async def test_pneumothorax_other_intrathoracic_injury_code_stays_unmatched():
    # S27.1 (traumatic hemothorax) sits in the same S27 family as S27.0XXA (traumatic
    # pneumothorax) but is not pneumothorax -- S27.0XXA must stay an explicit full code.
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["S27.1"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    ptx = next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")
    assert ptx["evidenceRef"] is None

# --- selection on the descriptions departments ACTUALLY send (#63) -----------------------------
#
# The tests above all feed a description that spells the anatomy out ("CT CHEST", "CT HEAD"), which
# is what let the registry look correct while selecting the generic screen on half of real traffic:
# a DICOM StudyDescription carries the PROTOCOL name, not the anatomy. Every row below returned the
# modality's generic screen before the alias table went in.
@pytest.mark.parametrize("modality,description,expected", [
    # the tool is literally named after the study, and the study did not select it
    ("CT", "CTPA",                       ["lung-nodule-detect", "pe-detect"]),
    ("CT", "CT PULMONARY ANGIOGRAM",     ["lung-nodule-detect", "pe-detect"]),
    ("CT", "CTA PULMONARY ARTERIES",     ["lung-nodule-detect", "pe-detect"]),
    ("CT", "CT THORAX WITH CONTRAST",    ["lung-nodule-detect", "pe-detect"]),
    ("CT", "CT LUNG CANCER SCREENING",   ["lung-nodule-detect", "pe-detect"]),
    # CT says "head", MR says "brain" -- so each is the other's alias, or both fall through
    ("CT", "CT BRAIN",                   ["ich-detect", "stroke-detect"]),
    ("CT", "NCCT BRAIN",                 ["ich-detect", "stroke-detect"]),
    ("CT", "CT ANGIO CIRCLE OF WILLIS",  ["ich-detect", "stroke-detect"]),
    ("MR", "MRI HEAD WITHOUT CONTRAST",  ["brain-tumor-screen", "ms-lesion-detect"]),
    ("MR", "MR CEREBRAL ANGIOGRAM",      ["brain-tumor-screen", "ms-lesion-detect"]),
    # abbreviations
    ("CT", "CT ABD PELVIS",              ["liver-lesion-detect", "pelvic-fracture-detect"]),
    ("CT", "CT ANGIO AORTIC ARCH",       ["aortic-dissection-detect"]),
    ("CR", "CXR",                        ["cxr-screen", "pneumothorax-detect"]),
    ("CR", "CXR PORTABLE",               ["cxr-screen", "pneumothorax-detect"]),
    ("DX", "THORAX PA",                  ["cxr-screen", "pneumothorax-detect"]),
    ("US", "RUQ ULTRASOUND",             ["gallstone-detect"]),
    ("US", "US LIVER",                   ["gallstone-detect"]),
])
def test_real_study_descriptions_select_their_body_region_tools(modality, description, expected):
    assert select_tools(modality, description) == expected


@pytest.mark.parametrize("description", [
    "US OB DELIVERY PLANNING",
    "US OBSTETRIC 3RD TRIMESTER DELIVERY",
])
def test_an_alias_does_not_match_inside_another_word(description):
    """The word boundary on aliases is load-bearing, not stylistic.

    "liver" (an abdomen alias) sits inside "deLIVERy". Match aliases as plain substrings and an
    obstetric ultrasound picks up the abdomen region and runs `gallstone-detect` on a delivery scan.
    Widening the match must not also loosen it -- take `\\b` out of registry._ALIAS_RE and this
    fails."""
    assert select_tools("US", description) == ["generic-us-screen"]


def test_thoracic_spine_is_a_spine_study_not_a_chest_study():
    """A T-spine CT gets the spine tool and NOT the lung tools.

    This is why "thoracic" is not an alias of chest. (The word boundary is not what saves this one
    -- "thorax" is not a substring of "thoracic" either way; the alias table simply must not grow a
    "thoracic" entry, and this pins that.)"""
    assert select_tools("CT", "CT THORACIC SPINE") == ["vertebral-fracture-detect"]


def test_an_unrelated_study_still_falls_back_to_the_generic_screen():
    """Aliases must widen the match, not dissolve it: a study that names no region we know still
    belongs on the generic screen, not on a body-region tool it never earned."""
    assert select_tools("CT", "CT MISC PROTOCOL") == ["generic-ct-screen"]
    assert select_tools("MR", "MR RESEARCH SEQUENCE") == ["generic-mr-screen"]


# --- a region can be NAMED without being the study's subject (#63, review) ---------------------
#
# The mirror image of the misses above: a description that says "head" or "vertebral" and means
# something else entirely. These are inert while every tool is STUBBED -- a wrong selection is a
# stubbed finding with a wrong toolId, and the pre-sign fhir2 write gates on a COMPLETE finding a
# stub never reaches -- but they become real the moment live CAD wires in behind this registry, and
# that is the expensive time to find them.
@pytest.mark.parametrize("modality,description,expected", [
    # a bone's "head" is a joint, not a brain. `CT FEMORAL HEAD` selects ich-detect on main TODAY:
    # the `head` key is a plain substring, so this predates the alias table.
    ("CT", "CT FEMORAL HEAD",              ["generic-ct-screen"]),
    ("CT", "CT HIP FEMORAL HEAD AVN",      ["generic-ct-screen"]),
    ("MR", "MR FEMORAL HEAD",              ["generic-mr-screen"]),
    ("MR", "MR HUMERAL HEAD",              ["generic-mr-screen"]),
    # requiring a leading modality token (`MRI HEAD`) would NOT catch this one -- the bone does
    ("MR", "MRI HEAD OF FEMUR",            ["generic-mr-screen"]),
    # and the plural must behave the same: `\bhead\b` does not match "heads", so before the
    # exclusion the same study selected different tools depending on how the tech typed it
    ("MR", "MR FEMORAL HEADS",             ["generic-mr-screen"]),
    # the vertebral ARTERY is a neck vessel, not a spine fracture
    ("CT", "CT ANGIO VERTEBRAL ARTERIES",  ["generic-ct-screen"]),
    ("CT", "CTA VERTEBRAL ARTERY",         ["generic-ct-screen"]),
    ("MR", "MRA VERTEBRAL ARTERIES",       ["generic-mr-screen"]),
    # the uterine cervix is not the cervical spine
    ("MR", "MR CERVICAL CANCER STAGING",   ["generic-mr-screen"]),
    ("CT", "CT CERVICAL CANCER",           ["generic-ct-screen"]),
    # the exclusion matches ANY bone ADJACENT to "head", not a hand-listed few: a fibular head
    # (knee) or a mandibular head (TMJ) is refused too, not just femoral/humeral.
    ("CT", "CT FIBULAR HEAD",              ["generic-ct-screen"]),
    ("CT", "CT MANDIBULAR HEAD",           ["generic-ct-screen"]),
    ("MR", "MR RADIAL HEAD",               ["generic-mr-screen"]),
    # a TAVR-planning CT is a valve/annulus sizing study, not a dissection study (#64, PI ruling
    # 2026-07-15). "aortic" is in the name for access planning only; delete registry's aorta
    # exclusion and this selects aortic-dissection-detect.
    ("CT", "CT AORTIC VALVE TAVR PLANNING", ["generic-ct-screen"]),
    ("CT", "CT AORTIC VALVE TAVR-PLANNING PROTOCOL", ["generic-ct-screen"]),
])
def test_a_region_named_but_not_the_subject_does_not_select_its_tools(modality, description, expected):
    assert select_tools(modality, description) == expected


@pytest.mark.parametrize("modality,description,expected", [
    ("CT", "CT HEAD",                    ["ich-detect", "stroke-detect"]),
    ("CT", "CT BRAIN",                   ["ich-detect", "stroke-detect"]),
    ("CT", "CT HEAD WITHOUT CONTRAST",   ["ich-detect", "stroke-detect"]),
    ("MR", "MRI HEAD WITHOUT CONTRAST",  ["brain-tumor-screen", "ms-lesion-detect"]),
    ("MR", "MRI BRAIN WITH CONTRAST",    ["brain-tumor-screen", "ms-lesion-detect"]),
    ("MR", "MRI CERVICAL SPINE",         ["cord-compression-detect"]),
    ("MR", "MRI C-SPINE",                ["cord-compression-detect"]),
    ("CT", "CT LUMBAR SPINE",            ["vertebral-fracture-detect"]),
    ("MR", "MRI LUMBAR",                 ["cord-compression-detect"]),
    # a bone named ELSEWHERE in the description (not adjacent to "head") must NOT suppress the
    # brain region: a polytrauma head CT that also scans a long bone is a real brain scan and must
    # keep ich-detect. Excluding on any bone anywhere silently dropped hemorrhage screening here.
    ("CT", "CT HEAD ABDOMEN PELVIS FEMUR", [
        "ich-detect", "stroke-detect", "liver-lesion-detect", "pelvic-fracture-detect",
    ]),
    ("CT", "CT HEAD AND FEMUR TRAUMA",     ["ich-detect", "stroke-detect"]),
    ("CT", "CT BRAIN AND SHOULDER",        ["ich-detect", "stroke-detect"]),
    # a dedicated aortic-root study keeps the dissection screen (#64, PI ruling: a type-A
    # dissection genuinely involves the root -- "leave it")
    ("CT", "CT CARDIAC AORTIC ROOT",       ["aortic-dissection-detect"]),
    # the TAVR exclusion is the "tavr planning" bigram, not the bare acronym: post-TAVR
    # surveillance of the aorta is a population that CAN dissect and keeps its screen
    ("CT", "CT AORTA POST TAVR",           ["aortic-dissection-detect"]),
])
def test_the_exclusions_do_not_cost_the_real_studies(modality, description, expected):
    """The exclusion refuses a region; it must not refuse the studies the region exists for."""
    assert select_tools(modality, description) == expected


# --- the real corpus: every distinct study name the showcase PACS actually sends (#64) ---------
#
# Captured from the deployed Orthanc by scripts/audit_registry_selection.py, not written from
# memory -- inventing these names is how #63 and the !53 over-matches got in. The fixture pins the
# selection for every distinct (modality, StudyDescription) in the #68 cohort, so the next naming
# convention that appears in the PACS shows up here as a diff, caught by CI instead of by review.
# CXR-only cohort, so this validates the chest/CXR matching; the CT rows above stay the coverage
# for CT names until real CT studies land (PI's #64 caveat).

_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "orthanc_studydescription_corpus.json").read_text()
)


def test_corpus_fixture_is_not_empty():
    assert _CORPUS["rows"], "an empty corpus would pass every test below by vacuity"


@pytest.mark.parametrize(
    "row", _CORPUS["rows"],
    ids=[f"{r['modality']}-{r['studyDescription']}" for r in _CORPUS["rows"]],
)
def test_every_real_study_name_selects_exactly_what_the_corpus_pins(row):
    assert select_tools(row["modality"], row["studyDescription"]) == row["tools"]


@pytest.mark.parametrize(
    "row", _CORPUS["rows"],
    ids=[f"{r['modality']}-{r['studyDescription']}" for r in _CORPUS["rows"]],
)
def test_no_real_study_falls_to_the_generic_screen_or_to_nothing(row):
    """Both #63 directions over the real distribution: every study the showcase PACS sends gets a
    regional tool -- none is generic-only, none selects nothing. In particular every row keeps
    pneumothorax-detect, the one live CAD (#71): lose the chest match for a real name and the
    only non-stubbed tool in the pipeline silently stops running on that study."""
    tools = select_tools(row["modality"], row["studyDescription"])
    assert tools, "selected nothing: modality missing from the registry"
    assert any(t not in {"generic-ct-screen", "generic-mr-screen", "generic-xr-screen",
                         "generic-us-screen", "mammo-screen"} for t in tools)
    assert "pneumothorax-detect" in tools
