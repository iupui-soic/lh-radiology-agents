"""Tool registry — maps study type to AI tools. v1 returns names only (stubs)."""
from __future__ import annotations
import re

_REGISTRY: dict[str, dict[str, list[str]]] = {
    "CT": {
        "chest":   ["lung-nodule-detect", "pe-detect"],
        "head":    ["ich-detect", "stroke-detect"],
        "abdomen": ["liver-lesion-detect"],
        "pelvis":  ["pelvic-fracture-detect"],
        "spine":   ["vertebral-fracture-detect"],
        "aorta":   ["aortic-dissection-detect"],
        "*":       ["generic-ct-screen"],
    },
    "MR": {
        "brain":   ["brain-tumor-screen", "ms-lesion-detect"],
        "spine":   ["cord-compression-detect"],
        "breast":  ["breast-mri-screen"],
        "knee":    ["knee-mri-screen"],
        "shoulder": ["shoulder-mri-screen"],
        "*":       ["generic-mr-screen"],
    },
    "CR": {
        "chest":   ["cxr-screen", "pneumothorax-detect", "effusion-detect",
                    "consolidation-detect", "edema-detect"],
        "*":       ["generic-xr-screen"],
    },
    "DX": {
        "chest":   ["cxr-screen", "pneumothorax-detect", "effusion-detect",
                    "consolidation-detect", "edema-detect"],
        "*":       ["generic-xr-screen"],
    },
    "MG": {
        "*":       ["mammo-screen"],
    },
    "US": {
        "abdomen": ["gallstone-detect"],
        "thyroid": ["thyroid-nodule-detect"],
        "pelvis":  ["pelvic-us-screen"],
        "*":       ["generic-us-screen"],
    },
    "PT": {
        "whole body": ["fdg-uptake-screen"],
        "brain":      ["brain-pet-screen"],
        "*":          ["generic-pet-screen"],
    },
    "NM": {
        "bone":       ["bone-scan-screen"],
        "myocardial": ["myocardial-perfusion-screen"],
        "*":          ["generic-nm-screen"],
    },
    "XA": {
        "coronary": ["coronary-stenosis-detect"],
        "cerebral": ["cerebral-aneurysm-detect"],
        "*":        ["generic-xa-screen"],
    },
}

# How radiologists actually name the region a key stands for (#63).
#
# The registry keys are anatomical ("chest", "head"), but a DICOM StudyDescription carries the
# PROTOCOL name the department typed -- "CTPA", "CXR", "CT BRAIN". Matching the key alone means a
# study whose description never spells out the anatomy falls through to the modality's generic
# screen: `CT BRAIN` and `NCCT BRAIN` missed `ich-detect`, `MRI HEAD` missed the brain tools, `CXR`
# missed `pneumothorax-detect`, and `CTPA` missed the very tool named after it. Half the real
# descriptions we can name selected the wrong tool set.
#
# Keyed by REGION, not by modality, on purpose: CT calls it "head" and MR calls it "brain", so each
# is the other's alias and a `CT BRAIN` / `MRI HEAD` both land correctly.
#
# Only regions that need department-specific synonyms are listed. Registry keys without aliases
# still match their literal name in a study description.
_REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "chest":   ("cxr", "ctpa", "thorax", "lung", "lungs", "pulmonary"),
    "head":    ("brain", "cerebral", "cranial", "circle of willis"),
    "brain":   ("head", "cerebral", "cranial"),
    "abdomen": ("abd", "ruq", "luq", "liver", "hepatic", "gallbladder", "biliary"),
    # "vertebral" and "cervical" are deliberately NOT spine aliases. A `CT ANGIO VERTEBRAL ARTERIES`
    # is a neck-vessel study and a `CT CERVICAL CANCER` is a uterine-cervix study; neither is a
    # spine study, and both matched the spine tools when those words were aliases. They also buy
    # nothing: a real spine study says so (`CT LUMBAR SPINE`, `MRI C-SPINE`), which the `spine` key
    # already catches.
    "spine":   ("lumbar",),
    "aorta":   ("aortic",),
}

# A region can be NAMED in a description and still not be the study's subject.
#
# "Head" is the case that bites: a `CT FEMORAL HEAD`, `MRI HEAD OF FEMUR` or `MR HUMERAL HEAD` is a
# joint, not a brain. The `head` key is matched as a plain substring, so `CT FEMORAL HEAD` selects
# `ich-detect` on main TODAY -- this predates the alias table, which merely extends the same flaw to
# MR through the head<->brain cross-alias.
#
# Requiring a leading modality token (`CT HEAD`, `MRI HEAD`) does not fix it: `MRI HEAD OF FEMUR` is
# a real way to name the study and still matches "MRI HEAD". What separates the two is the BONE --
# but the bone must sit NEXT TO "head" (`<bone> head` / `head of <bone>`), NOT anywhere in the
# description. Refusing the region on any bone word anywhere is too blunt: a polytrauma
# `CT HEAD ABDOMEN PELVIS FEMUR` is a real brain scan, and dropping `ich-detect` because "femur"
# appears elsewhere silently removes intracranial-hemorrhage screening from exactly the studies
# that need it. Matching the adjacency also lets the bone list be complete without that risk, so
# `fibular head`, `mandibular head` (TMJ) etc. are covered too, not just femoral/humeral. `heads?`
# keeps the plural consistent (`MR FEMORAL HEADS`).
_MSK_BONE = (
    "femoral|femur|humeral|humerus|radial|radius|ulnar|ulna|fibular|fibula|tibial|tibia|"
    "mandibular|mandible|condylar|condyle|patellar|patella|metacarpal|metatarsal|hip|shoulder"
)
_MSK_JOINT_HEAD = re.compile(
    rf"\b(?:{_MSK_BONE})\s+heads?\b|\bheads?\s+of\s+(?:the\s+)?(?:{_MSK_BONE})\b"
)

# A TAVR-planning CT names the aorta without being a dissection study (#64, PI ruling
# 2026-07-15): it is an elective aortic-stenosis valve/annulus sizing study, imaging the aorta
# only for access planning, and that population is not the acute-dissection population -- running
# a dissection detector on it is off-indication. `CT CARDIAC AORTIC ROOT` stays matched by the
# same ruling: a type-A dissection genuinely involves the root.
#
# The exclusion is the adjacency bigram "tavr planning", NOT the bare device acronym -- same
# lesson as the bone-head exclusion above: refusing the region on a word ANYWHERE is too blunt.
# A post-TAVR surveillance CT of the aorta is aorta imaging in a population that CAN dissect,
# and excluding on "tavr" alone would silently remove its dissection screen. A wrong exclusion
# deletes screening; over-narrow beats over-broad here.
# The PI REVERSED the aortic-root half of the 2026-07-15 ruling on #64 (2026-08-06): "TAVR
# planning and aortic-root CTs are valve and annulus sizing studies, ordered on a known workup,
# not on suspicion of dissection. A screening detector firing there adds noise where a specialist
# is already looking at the root." The earlier ruling kept the root matched because a type-A
# dissection involves it; the reversal's point is that a dedicated root study is not ordered on
# that question. The comment above still records the original reasoning, because knowing the call
# was revisited matters more than a tidy file -- but the TAVR-only pattern it described is gone,
# folded into the alternation below rather than left defined and referenced by nothing.
#
# The reversal's stated safety net does not exist, so do not rely on it. It was justified with
# "if a dissection question is real the order carries it, and the reasonCode path selects the tool
# anyway" -- that is not what the reasonCode path does. `aortic-dissection-detect` is not a key in
# handler._REASON_CODE_RULES (only pneumothorax/pe/effusion are), and that path is a per-tool
# ENRICHMENT loop over the tools this function already returned (handler.py `for tool in tools:`),
# so it cannot add back a tool an exclusion removed. Verified by running it: an excluded
# description carrying reasonCode I71.00 selects ['generic-ct-screen'] and nothing else. The same
# is true of the TAVR exclusion that shipped before this one.
#
# So this exclusion SUBTRACTS a screen outright, with no fallback underneath it. The clinical call
# is the PI's and stands; the point here is that the negative lookahead below is the only thing
# keeping a real dissection study screened, which is why it is not optional.
#
# Two negative lookaheads, both the over-narrow-beats-over-broad rule made literal, because a
# wrong exclusion DELETES a screen while a wrong inclusion only adds noise.
#
#   1. "dissection" anywhere -- a study that says it is a dissection study is one whatever else it
#      names, so "CT AORTIC ROOT DISSECTION PROTOCOL" keeps its screen.
#   2. post-operative / hardware cues -- a root that has been replaced, repaired or grafted is
#      surveillance in a population that can dissect, and at higher risk than an unoperated one
#      (anastomotic pseudoaneurysm, new dissection at the suture line). This is the SAME reason
#      `CT AORTA POST TAVR` keeps its screen, pinned in
#      test_the_exclusions_do_not_cost_the_real_studies -- "the exclusion refuses a region; it must
#      not refuse the studies the region exists for". Without this, `CT POST AORTIC ROOT REPAIR`
#      lost its screen while `CT AORTA POST TAVR` kept one, which is the same clinical situation
#      described two ways.
#
# The cues are deliberately specific ("post-op", not a bare "post") so a sizing study written as
# `AORTIC ROOT POST CONTRAST` is not rescued by the word "post". Where they do over-rescue, that
# is the safe direction: keeping a screen costs noise, losing one costs a finding.
_AORTA_SIZING_STUDY = re.compile(
    r"^(?!.*\bdissection\b)"
    r"(?!.*\b(?:post[\s-]?op(?:erative)?|status\s+post|s/p|replacement|repair|graft|stent"
    r"|surveillance|follow[\s-]?up)\b)"
    r".*(?:\btavr[\s-]+planning\b|\baortic\s+root\b|\broot\s+of\s+(?:the\s+)?aorta\b)"
)

_REGION_EXCLUSIONS: dict[str, re.Pattern[str]] = {
    "head":  _MSK_JOINT_HEAD,
    "brain": _MSK_JOINT_HEAD,
    "aorta": _AORTA_SIZING_STUDY,
}

# Aliases match on WORD BOUNDARIES, unlike the plain-substring match on the key itself.
#
# They have to. An alias is short and clinical where a key is anatomical, so it turns up inside
# unrelated words: "liver" sits inside "deLIVERy", which means a plain substring match hands an
# obstetric `US OB DELIVERY PLANNING` the abdomen region and runs `gallstone-detect` on it. Widening
# the match must not also loosen it -- guarded in tests/test_handler.py.
#
# Related: "thoracic" is deliberately NOT an alias of chest, because a `CT THORACIC SPINE` is a spine
# study. ("thorax" is safe -- it is not a substring of "thoracic".)
#
# The boundary costs nothing on real descriptions: `CT L-SPINE` and `MRI C-SPINE` still match,
# because a hyphen is a word boundary too.
_ALIAS_RE: dict[str, re.Pattern[str]] = {
    region: re.compile(r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\b")
    for region, aliases in _REGION_ALIASES.items()
}


def _matches_region(desc: str, key: str) -> bool:
    """Does this study description name the given body region — under any of its names?

    Additive over the old `key in desc` test in every case EXCEPT an excluded one: a description
    that matched a region before still matches it unless the exclusion says the region is not what
    the study is about. That single subtraction is deliberate and is the point of `_REGION_EXCLUSIONS`
    -- `CT FEMORAL HEAD` selects `ich-detect` today and should not.
    """
    excluded = _REGION_EXCLUSIONS.get(key)
    if excluded and excluded.search(desc):
        return False
    if key in desc:
        return True
    pattern = _ALIAS_RE.get(key)
    return bool(pattern and pattern.search(desc))


def select_tools(modality: str, description: str) -> list[str]:
    """Return the tool list for a given modality and study description.

    Collects tools from every matching body-part key (deduped, in registry
    order) so multi-region studies run all applicable regional tools, not
    just the first match. A region matches on its key or any of its aliases
    (#63). Falls back to "*" when no body-part key matches.
    """
    desc = (description or "").lower()
    by_mod = _REGISTRY.get(modality, {})
    matched: list[str] = []
    for key, tools in by_mod.items():
        if key != "*" and _matches_region(desc, key):
            for tool in tools:
                if tool not in matched:
                    matched.append(tool)
    if matched:
        return matched
    return by_mod.get("*", [])
