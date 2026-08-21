"""Interpretation Assistant handler — owner: Chaitra.

The tool REGISTRY selects by modality/study-type; each selected tool then reports at one of three
levels of reality, and the level is visible in the output rather than implied:

  * PIXELS (#71, slice of #27) -- the rows of _PIXEL_TOOL_SPECS run a real pretrained classifier
    over the study's image data (cxr_model.py), each row reading ONE head of the shared multi-label
    model; the study is fetched and scored once per call no matter how many rows are selected.
    A POSITIVE screen reports COMPLETE, with a real confidence and an evidenceRef naming the
    instance it scored; a NEGATIVE screen reports STUBBED (the model ran, but "draft only on
    positives" -- see _head_finding). When a row cannot look at pixels (extras absent / no Orthanc
    study / non-image instances) it DEGRADES to the referral reason below rather than fabricating
    a result.
  * REFERRAL REASON (#27) -- the order.reasonCode cross-check. `pe-detect` uses only this; it is
    also the degrade path for `pneumothorax-detect` when the pixel read cannot run. A genuine but
    narrow interim signal, not a CAD model, so it stays STUBBED.
  * STUBBED -- everything else, until it gets its own real implementation.

A tool that cannot run degrades to STUBBED (or ERROR) and NEVER invents a negative: "nothing found"
from a tool that never looked is the automation-bias trap the #26 COMPLETE-gate exists to prevent.

DICOM evidence capture (#59, item 2 of the "Then" list). After the tool loop completes, every
COMPLETE finding whose evidenceRef starts with "orthanc:instance/" gets handed to
`radagent_common.orthanc_client.write_ai_evidence_capture`, which writes a DICOM Secondary Capture
into Orthanc so OHIF picks it up as an AI evidence series alongside the source imaging. Held behind
the same feature-flag killswitch (ORTHANC_PRESIGN_WRITE_ENABLED, default False) that the write path
itself uses, so the wiring is inert on any deployment that hasn't opted in after the PI/lead
sign-off. Best-effort: any failure (Orthanc down, instance not found, disabled) logs and continues;
the human read is the safety net, and a failed evidence-capture write must never strand it. See
`docs/dicom-evidence-writeback.md`.

Input  : { studyContext }
Output : contracts/skills/interpretation.schema.json
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from radagent_common.tracing import now_iso
from registry import select_tools

log = logging.getLogger(__name__)

AGENT_VERSION = "0.6.0"

# Is this image built with the pixel/model extras? Decided ONCE, at import, by whether the imports
# succeed -- not discovered over the network mid-study. cxr_model imports torch eagerly for exactly
# this reason. The agent-tests CI lane installs neither extra, so PIXEL_TOOLING is False there and
# pneumothorax-detect falls back to its referral-reason rule -- which is why the pre-#71 suite still
# passes untouched.
#
# These are module-level names rather than function-local imports so a test can substitute a fake
# Orthanc and a fake model and exercise the pixel path WITHOUT torch. A seam that only exists in
# the presence of a 1.5GB dependency is a seam nobody tests.
try:
    from radagent_common.imaging import NotAnImage, dicom_to_greyscale
    from radagent_common.orthanc_client import OrthancClient

    from cxr_model import op_threshold, score

    PIXEL_TOOLING = True
except ImportError as _exc:  # pragma: no cover - only reachable in a lane without the extras installed
    log.info("interpretation: pixel/model extras absent (%s); pixel tools stay STUBBED", _exc)
    PIXEL_TOOLING = False

    class NotAnImage(Exception):  # type: ignore[no-redef]
        """Placeholder so the except-clause below is always a valid type."""

    OrthancClient = dicom_to_greyscale = score = op_threshold = None  # type: ignore[assignment]

# A pathology is REPORTED positive when its CALIBRATED score clears this. Lives here (not in
# cxr_model) because the decision is the handler's and the env read must work in the no-torch lane.
# 0.5 is the model's own nominal operating point -- with the op-norm calibration the `-all` weights
# apply, calibrated 0.5 IS the head's op threshold, so the default changes nothing. It is
# deliberately un-tuned in code: #86 measured 47/100 positives on the showcase cohort at the
# default, so a deployment MAY tighten it (calibrated domain, e.g. "0.55") once someone owns that
# call with cohort prevalence in hand -- making it deployable-without-a-release is exactly #86's
# ask, but shipping a different default is a clinical decision this module refuses to make (the
# same argument as #64's registry corpus).
# `or` (not a get default): the compose pass-through hands an EMPTY string when unset, and
# float("") at import time would keep the whole agent from booting.
POSITIVE_THRESHOLD = float(os.environ.get("CXR_POSITIVE_THRESHOLD") or "0.5")

# Referral-reason ICD-10 codes per real-slice tool, matched by FAMILY PREFIX rather than exact
# code. worklist-triage normalises the same order.reasonCode field to a 3-char ICD-10 category
# (agents/worklist-triage/handler.py:_reason_code_signals), so an exact-string list here silently
# disagreed with triage on the same order: triage escalated "I26" or "I2699" as urgent PE while
# this tool stayed silent on the identical code (#27 follow-up, Saptarshi/Pranathi). Prefixes
# confirmed with Pranathi (lead review):
#   - pneumothorax-detect: "J93" (spontaneous pneumothorax *and other air leak*, e.g. J93.82 --
#     that code already matched under the old exact-code list on main, so staying matched here
#     is not a widening; it's a chest study and the finding stays STUBBED regardless). S27.0XXA
#     (traumatic) and J95.811 (postprocedural, e.g. r/o PTX post-line film) stay explicit full
#     codes because their families -- S27 intrathoracic injury generally, J95 postprocedural
#     respiratory complications generally -- are NOT all pneumothorax.
#   - pe-detect: "I26" (parent + all billable children, with/without acute cor pulmonale --
#     all of I26 is pulmonary embolism, so the prefix can't over-match). "O882" (dot-normalised
#     O88.2, obstetric thromboembolism) stays a 4-char prefix rather than the 3-char "O88"
#     family, because O88 also covers air/amniotic-fluid/septic embolism, which are not PE.
#   - effusion-detect: "J90" (pleural effusion NEC -- the family is only pleural effusion) and
#     "J91" (pleural effusion in conditions classified elsewhere: J91.0 malignant, J91.8 other --
#     again wholly pleural effusion, so the prefix can't over-match). J94 (other pleural
#     conditions, e.g. chylous effusion, hemothorax) is deliberately NOT matched: the family is
#     not all effusion, and over-narrow beats over-broad here (same doctrine as the registry's
#     TAVR exclusion).
_REASON_CODE_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "pneumothorax-detect": (("J93", "S270XXA", "J95811"), "pneumothorax"),
    "pe-detect": (("I26", "O882"), "pulmonary embolism"),
    "effusion-detect": (("J90", "J91"), "pleural effusion"),
}


def _normalize_reason_code(code: str) -> str:
    """Same normalisation shape as worklist-triage's _reason_code_signals (dot-stripped,
    upper), so both agents read order.reasonCode the same way."""
    return code.upper().replace(".", "")


def _reason_finding(tool_id: str, reason_codes: list[str]) -> Optional[dict]:
    prefixes, condition = _REASON_CODE_RULES[tool_id]
    hit = next((code for code in reason_codes if _normalize_reason_code(code).startswith(prefixes)), None)
    if hit is None:
        return None
    return {
        "toolId": tool_id,
        "label": f"Referral reason coded {hit} ({condition}); no imaging-based result for this study",
        "confidence": None,
        # Text pointer to where the evidence lives, not an image-region ref: no pixel read ran for
        # this study (extras absent or no image), and writing a DICOM SC/overlay into Orthanc needs a
        # safety review we haven't done (#59). evidenceRef is `["string", "null"]` in the contract,
        # so a plain-text locator is a legitimate value here, not a placeholder for the image ref.
        "evidenceRef": f"order.reasonCode={hit}",
        # `status` stays STUBBED even though label/evidenceRef are populated: COMPLETE is reserved
        # for real pixel-level results, because it gates the pre-sign fhir2 write
        # (orchestrator/workflow.py:_has_complete_finding -> _presign_impression, before
        # AWAITING_RADIOLOGIST). A referral reason the ordering clinician typed is not imaging
        # evidence for the condition any more than a non-matching code is evidence against it -- so
        # it must not trip a pre-read critical-finding chart write. Do not flip this to COMPLETE
        # without also addressing the fhir2 write-back security/PHI review (#30).
        "status": "STUBBED",
    }


_MODEL_VERSION = "cxr-densenet121-res224-all"


def _tool_version(finding: dict) -> str:
    """What actually produced this finding. Visible in toolsSelected[].version so a consumer -- and
    anyone auditing why a chart says what it says -- can tell a real model from a referral-code rule
    from a stub. Three different things must not all report as "stub-0".

    The model version is claimed ONLY when the model actually SCORED an instance -- which is exactly
    when `evidenceRef` points at one: a COMPLETE positive, a STUBBED negative ("the model ran and
    found nothing"), or an ERROR that reached the model (which carries the instance it reached). A
    transport-stage failure (Orthanc down) degrades to the referral rule / stub in `_pixel_finding`
    and never lands here claiming the model. Claiming a model that never ran is the same lie as
    inventing a finding, so the check is on the instance ref, not on the status.
    """
    ev = finding.get("evidenceRef") or ""
    if finding["toolId"] in _PIXEL_TOOLS and ev.startswith("orthanc:instance/"):
        return _MODEL_VERSION
    if ev:
        return "referral-rule-1"
    return "stub-0"


def _overall_status(statuses: list[str]) -> str:
    unique = set(statuses)
    if not unique or unique == {"STUBBED"}:
        return "STUBBED"
    if unique == {"COMPLETE"}:
        return "COMPLETE"
    if unique == {"ERROR"}:
        return "ERROR"
    return "PARTIAL"


# Tools that read PIXELS -- the pluggable point: one row per screening finding. Everything else in
# the registry either cross-checks the referral reason (above) or is still a stub.
#
# A row maps a tool id to the head it reads out of the shared multi-label CXR model plus the
# clinical display name its labels carry. Every row reads the SAME forward pass -- score() returns
# every head at once -- so the study's pixels are fetched and scored ONCE per handle() call no
# matter how many rows are selected (_score_study), and adding a screening finding costs one row
# here plus its registry entry, not a second inference. pneumothorax-detect (#71) was the first
# real model in the system; this table is #71's special-casing generalised so the NEXT head is a
# row, not code.
_PIXEL_TOOL_SPECS: dict[str, dict[str, str]] = {
    "pneumothorax-detect": {"head": "Pneumothorax", "display": "Pneumothorax"},
    # The second row, and the first NON-CRITICAL one: "effusion" is deliberately NOT on
    # impression-generation's critical-keyword list, so a positive here surfaces on the worklist,
    # in OHIF, and in the pre-sign draft text -- but never trips a criticalFlag, so it cannot page
    # anyone or escalate sign-off. The display name says "Pleural effusion" (what the head means
    # clinically); the head is spelled exactly as TorchXRayVision names it in model.pathologies.
    "effusion-detect": {"head": "Effusion", "display": "Pleural effusion"},
    # Rows three and four, completing the set the PI approved on #27 ("all three: effusion,
    # consolidation and edema", same terms as the effusion row above). Same weights, same forward
    # pass, same threshold policy -- _score_study already runs the model ONCE per study and every
    # row reads its own head off that one result, so two more rows cost no extra inference.
    #
    # Both are NON-CRITICAL, like effusion and deliberately so. impression-generation scans each
    # COMPLETE finding LABEL against _CRITICAL_KEYWORDS (#26), so a display string is load-bearing
    # here: neither "Consolidation" nor "Pulmonary edema" contains a keyword from that list, so a
    # positive reaches the worklist, the viewer and the pre-sign draft without ever opening an ack
    # clock. Paging stays deterministic off report text (#78). Check that list before adding a row
    # whose display names a finding that IS critical.
    "consolidation-detect": {"head": "Consolidation", "display": "Consolidation"},
    "edema-detect": {"head": "Edema", "display": "Pulmonary edema"},
}

# The set view of the table, for the audit path (_tool_version): pixel tools are exactly its rows.
_PIXEL_TOOLS = frozenset(_PIXEL_TOOL_SPECS)

# ViewPosition values that mean the projection the model was trained on. Anything else -- LL,
# LATERAL, obliques -- is a side view the CXR heads were never meant to score.
_FRONTAL_VIEWS = frozenset({"PA", "AP"})


async def _frontal_first(client, instances: list[str]) -> list[str]:
    """Order candidate instances so a FRONTAL view is scored first.

    (SeriesNumber, InstanceNumber) acquisition order does not put the frontal first on real data:
    measured live on the 100-study MIMIC showcase cohort (2026-07-24), 17 studies lead with the
    LATERAL. The model is frontal-trained; scoring a lateral silently returns a confident number
    about the wrong projection (cxr_model.py's "it scores ANYTHING" warning made real). ViewPosition
    says which is which: known frontals first, unlabelled views next (a portable AP often ships a
    blank ViewPosition), known non-frontals last. The sort is stable, so acquisition order still
    breaks ties, and a study with no frontal at all keeps its old behaviour. Tag-fetch failures
    leave the instance where it was -- reordering is best-effort and never a new way to degrade.
    """
    def _rank(view_position) -> int:
        vp = str(view_position or "").strip().upper()
        if vp in _FRONTAL_VIEWS:
            return 0
        if not vp:
            return 1
        return 2

    ranked: list[tuple[int, int, str]] = []
    for position, instance_id in enumerate(instances):
        try:
            tags = await client.get_instance_tags(instance_id)
            view_position = (tags or {}).get("ViewPosition")
        except Exception:  # noqa: BLE001 - tags unavailable -> keep acquisition position
            view_position = None
        ranked.append((_rank(view_position), position, instance_id))
    ranked.sort()
    return [instance_id for _, _, instance_id in ranked]


def _raw_sigmoid(calibrated: float, op_thresh: Optional[float]) -> float:
    """Invert TorchXRayVision's op-norm to recover the raw sigmoid from a calibrated score (#86).

    The forward op-norm maps a raw sigmoid r with per-head operating threshold t piecewise-linearly
    so that t lands exactly at 0.5:  c = r/(2t) for r < t, else c = 1 - (1-r)/(2(1-t)).  Both
    branches are linear and meet at (t, 0.5), so the inverse is exact:
    r = 2tc for c < 0.5, else 1 - 2(1-t)(1-c).  Pure math, no torch -- which keeps it testable in
    the no-torch lane and usable on scores that were computed elsewhere.

    op_thresh None means the weights applied no op-norm, so the "calibrated" score IS the raw
    sigmoid and passes through unchanged.
    """
    if op_thresh is None:
        return calibrated
    if calibrated < 0.5:
        return 2.0 * op_thresh * calibrated
    return 1.0 - 2.0 * (1.0 - op_thresh) * (1.0 - calibrated)


def _head_finding(tool_id: str, probs: dict, instance_id: str) -> dict:
    """Turn the model's probability for this tool row's head into the contract's finding.

    THE "DRAFT ONLY ON POSITIVES" DECISION (#71's open question, resolved here and documented in
    CLAUDE.md). #71 asks whether a negative screen should emit COMPLETE (a draft on every study) or
    stay inert (a draft only on positives). It must be "only on positives", because the pre-sign
    write downstream is UNCONDITIONAL on any COMPLETE finding: orchestrator/workflow.py
    `_presign_impression` calls impression.generate and writes its `impressionText` to the chart
    regardless of whether anything was flagged. A COMPLETE *negative* would therefore write "No acute
    findings identified" -- a fixed negative impression authored by nobody -- into every normal
    patient's chart ahead of the read, which is exactly the automation-bias trap the #26 COMPLETE
    gate exists to prevent. The rule applies to EVERY row of _PIXEL_TOOL_SPECS, not just the first:

      * POSITIVE (p >= threshold) -> COMPLETE. Arms the pre-sign draft and, for pathologies on the
        critical-keyword list, the Cat1 critical-comm path: the label names the pathology, which is
        what impression-generation's keyword scan folds into criticalFlags.
      * NEGATIVE (p < threshold) -> STUBBED. The model ran (evidenceRef records which instance, and
        _tool_version still reports the model id), but it emits no COMPLETE, so the normal stays
        inert: no pre-sign chart write, no false Cat1 page. The label is negation-worded and is not
        scanned today anyway (impression-generation only folds COMPLETE labels), so it cannot trip a
        critical flag -- and stays correct once the scan becomes negation-aware (#78).

    One global POSITIVE_THRESHOLD across every head, deliberately (#86 policy): a per-pathology
    threshold is a clinical decision that needs a real cohort per head, and nobody has made it.
    """
    spec = _PIXEL_TOOL_SPECS[tool_id]
    p = probs[spec["head"]]  # KeyError if the model lacks the head -> caller turns it into ERROR
    # The margin fields (#86): the head's raw-sigmoid operating point and the raw sigmoid recovered
    # from the calibrated score. Carried on BOTH outcomes -- a negative's distance below the op
    # point is as informative as a positive's above it. op_threshold is None when the extras are
    # absent (this function is then only reachable through a test's fake) or the weights carry no
    # op point for the head; the payload degrades to null rather than a fabricated number.
    op_t = op_threshold(spec["head"]) if op_threshold is not None else None
    raw = _raw_sigmoid(p, op_t)
    # In the label, the raw sigmoid against its op point -- "raw 0.021 vs op 0.0098" -- because the
    # calibrated "p=0.51" alone reads as a coin flip on every surface that shows it (#86).
    margin_text = f", raw {raw:.3g} vs op {op_t:.3g}" if op_t is not None else ""
    if p >= POSITIVE_THRESHOLD:
        return {
            "toolId": tool_id,
            "label": (
                f"{spec['display']} (screening p={p:.2f}{margin_text}); "
                "screening signal only, not a read"
            ),
            "confidence": p,
            "rawScore": raw,
            "opThreshold": op_t,
            # Text locator: the instance the model actually scored, so a reader can pull up the exact
            # frame. Not a DICOM SC/overlay ref -- writing AI-made images into the record is deferred
            # (#59) and needs its own safety review.
            "evidenceRef": f"orthanc:instance/{instance_id}",
            "status": "COMPLETE",
        }
    return {
        "toolId": tool_id,
        "label": (
            f"{spec['display']} screening negative (p={p:.2f} < {POSITIVE_THRESHOLD:g}{margin_text}); "
            "model ran, no finding at threshold -- screening signal only, not a read"
        ),
        "confidence": None,
        "rawScore": raw,
        "opThreshold": op_t,
        "evidenceRef": f"orthanc:instance/{instance_id}",
        "status": "STUBBED",
    }


async def _score_study(ctx: dict) -> Optional[tuple]:
    """Fetch the study's pixels and run the model ONCE; every selected pixel-tool row then reads
    its own head from the same result (_pixel_result_finding). Returns:

      * ("scored", probs, instance_id) -- the model ran; probs carries every head;
      * ("model-error", exc_name, instance_id) -- the model threw once it had real pixels (a
        uniform frame, an inference error): an honest ERROR for every row, carrying the instance
        the model reached;
      * None -- could not LOOK: extras not installed / no Orthanc study id / no instances / no
        scoreable pixels / an Orthanc or decode failure. The study could not be screened, so every
        pixel row falls through to its referral rule, then a bare stub. An Orthanc outage costs the
        study its screen; it must not turn the tools red, and it must never be reported as the
        model "running".

    What this path must never do is invent a negative. A tool that cannot look at the image and
    reports "nothing found" is the automation-bias trap the #26 COMPLETE-gate exists to prevent,
    and it is worse here than in the stub, because this one carries a model's authority.
    """
    if not PIXEL_TOOLING:
        return None

    orthanc_study_id = (ctx.get("study") or {}).get("orthancStudyId")
    if not orthanc_study_id:
        return None

    # --- fetch stage: a failure here means we could not LOOK, so DEGRADE (fall through). Never an
    # ERROR (an Orthanc outage is not a model failure) and never the model version in the audit.
    try:
        client = OrthancClient()
        instances = await client.list_study_instances(orthanc_study_id)
    except Exception:  # noqa: BLE001 - Orthanc unreachable -> degrade, not error
        log.warning("pixel screen: could not list instances for %s; degrading", orthanc_study_id)
        return None
    if not instances:
        log.warning("pixel screen: study %s has no instances", orthanc_study_id)
        return None
    instances = await _frontal_first(client, instances)

    # Score the FIRST SCOREABLE instance, frontal views first. A study can
    # also carry non-image objects -- a Structured Report, a radiation-dose SR, a presentation
    # state -- that sort AHEAD of the image; skip those and score the first real image rather than
    # letting one of them fail the whole study. That is imaging.NotAnImage's contract: a caller
    # SKIPS such an instance, it does not abort -- a tool that errors out because a study happens
    # to contain an SR is a tool that never runs.
    instance_id = None
    pixels = None
    for candidate in instances:
        try:
            pixels = dicom_to_greyscale(await client.get_instance_dicom(candidate))
        except NotAnImage as exc:
            log.warning("pixel screen: %s skipping non-image instance %s (%s)",
                        orthanc_study_id, candidate, exc)
            continue
        except Exception:  # noqa: BLE001 - fetch/decode failure -> could not look -> degrade
            log.warning("pixel screen: %s could not fetch/decode instance %s; degrading",
                        orthanc_study_id, candidate)
            return None
        instance_id = candidate
        break
    if pixels is None:
        # No scoreable pixels anywhere in the study -> fall through, never a fabricated negative.
        log.warning("pixel screen: study %s has no scoreable image instance", orthanc_study_id)
        return None

    # --- model stage: the model now runs on a REAL instance, so a failure here is an honest ERROR
    # attributed to the model (evidenceRef records which instance it reached), not a degrade.
    # Inference is CPU-bound and blocking; keep it off the event loop so one study being screened
    # does not stall every other A2A request this agent is serving.
    try:
        probs = await asyncio.to_thread(score, pixels)
        return ("scored", probs, instance_id)
    except Exception as exc:  # noqa: BLE001 - model failure -> ERROR for every row, not a negative
        log.exception("pixel screen model failed for %s", orthanc_study_id)
        return ("model-error", type(exc).__name__, instance_id)


def _pixel_result_finding(tool_id: str, outcome: tuple) -> dict:
    """One tool row's finding from the shared scoring outcome. A model that threw is an honest
    ERROR for every row (the failure was the study's single forward pass); a model missing THIS
    row's head is an ERROR for this row only -- surfaced, never a crash and never a silent miss."""
    kind = outcome[0]
    if kind == "model-error":
        _, exc_name, instance_id = outcome
        return {
            "toolId": tool_id,
            "label": f"screening model failed: {exc_name}",
            "confidence": None,
            "evidenceRef": f"orthanc:instance/{instance_id}",  # the model reached this instance
            "status": "ERROR",
        }
    _, probs, instance_id = outcome
    try:
        return _head_finding(tool_id, probs, instance_id)
    except KeyError as exc:
        log.exception("%s: loaded weights lack head %s", tool_id, _PIXEL_TOOL_SPECS[tool_id]["head"])
        return {
            "toolId": tool_id,
            "label": f"screening model failed: {type(exc).__name__}",
            "confidence": None,
            "evidenceRef": f"orthanc:instance/{instance_id}",
            "status": "ERROR",
        }


# -----------------------------------------------------------------------------
# DICOM evidence capture wiring (#59 item 2)
# -----------------------------------------------------------------------------

# Prefix contract with the pixel-tool producer (#71): a COMPLETE finding from a pixel tool emits
# an evidenceRef starting with this exact string, and the substring after it is Orthanc's INTERNAL
# instance id (not the DICOM SOPInstanceUID -- the pixel walk uses Orthanc's own ids). If this
# prefix ever rotates, the wiring stops matching and the SC write silently no-ops -- safe default,
# but _tool_version's check uses the same string, so they must rotate together.
_ORTHANC_INSTANCE_PREFIX = "orthanc:instance/"


async def _maybe_write_evidence_capture(finding: dict, orthanc_study_id: str, client) -> None:
    """When a tool emits COMPLETE with an ``orthanc:instance/<id>`` evidenceRef, write an
    authorship-stamped Secondary Capture into Orthanc so OHIF picks it up as an AI evidence series
    alongside the source imaging (#59, item 2 of the "Then" list).

    Best-effort by contract: any failure -- disabled by the deployment flag, target not found,
    Orthanc down, DICOM build error -- logs and returns. The pre-sign impression text (#26 fhir2
    write) still carries the finding. The human read is the safety net. A failed evidence-capture
    write must never strand it.

    Held behind two gates by the write client itself (see radagent_common.orthanc_client and
    docs/dicom-evidence-writeback.md): the ORTHANC_PRESIGN_WRITE_ENABLED deployment flag and the
    plaintext-transport refusal. This helper adds the caller-level guards on top: a status gate
    (only COMPLETE findings) and an evidenceRef-shape gate (only "orthanc:instance/" refs). A
    finding that fails either of the caller-level guards is skipped silently -- STUBBED / ERROR /
    referral-rule findings are not evidence-capture-eligible.
    """
    if finding.get("status") != "COMPLETE":
        return
    ev = finding.get("evidenceRef") or ""
    if not ev.startswith(_ORTHANC_INSTANCE_PREFIX):
        return
    orthanc_instance_id = ev[len(_ORTHANC_INSTANCE_PREFIX):]
    if not orthanc_instance_id or not orthanc_study_id:
        return

    # The write client wants the DICOM SOPInstanceUID (which it stamps into the SC's
    # SourceImageSequence); the pixel producer gave us Orthanc's internal instance id. Resolve
    # via /simplified-tags on the same client.
    try:
        tags = await client.get_instance_tags(orthanc_instance_id)
    except Exception as e:  # noqa: BLE001 -- best-effort, outage or 404 both land here
        log.warning(
            "evidence-capture skip for %s: could not read tags for instance %s: %s",
            finding.get("toolId"), orthanc_instance_id, e,
        )
        return
    sop_instance_uid = tags.get("SOPInstanceUID")
    if not sop_instance_uid:
        log.warning(
            "evidence-capture skip for %s: instance %s has no SOPInstanceUID tag",
            finding.get("toolId"), orthanc_instance_id,
        )
        return

    try:
        new_sc_uid = await client.write_ai_evidence_capture(
            target_sop_instance_uid=sop_instance_uid,
            orthanc_study_id=orthanc_study_id,
            tool_id=finding.get("toolId") or "unknown",
            label=finding.get("label") or "",
            confidence=finding.get("confidence"),
        )
    except Exception as e:  # noqa: BLE001 -- best-effort, including the transport refusal re-raise
        log.warning(
            "evidence-capture write raised for %s (target %s): %s",
            finding.get("toolId"), sop_instance_uid, e,
        )
        return

    if new_sc_uid:
        log.info(
            "evidence-capture wrote SC %s for %s (target %s)",
            new_sc_uid, finding.get("toolId"), sop_instance_uid,
        )
    # new_sc_uid is None when the write no-op'd (flag off, target missing, best-effort failure).
    # Silence intentional -- the write client already logged the specific reason.


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "interpretation.runTools":
        raise ValueError(f"unexpected skill {skill_id}")
    ctx = payload["studyContext"]
    modality = ctx["study"].get("modality", "")
    desc = ctx["study"].get("studyDescription", "")
    reason_codes = ctx.get("order", {}).get("reasonCode") or []
    tools = select_tools(modality, desc)

    # ONE shared scoring pass for however many pixel-tool rows the registry selected: the model
    # scores every head in a single forward, so fetching/scoring per row would be the same numbers
    # at N times the cost. None when no pixel row is selected or the study could not be looked at.
    pixel_outcome = None
    if any(t in _PIXEL_TOOL_SPECS for t in tools):
        pixel_outcome = await _score_study(ctx)

    findings = []
    for tool in tools:
        # Pixel result wins when the model ran (COMPLETE positive, STUBBED negative, or ERROR); a
        # None outcome means it could not look, so fall back to the referral-reason cross-check,
        # then a stub.
        real = None
        if tool in _PIXEL_TOOL_SPECS and pixel_outcome is not None:
            real = _pixel_result_finding(tool, pixel_outcome)
        if real is None and tool in _REASON_CODE_RULES:
            real = _reason_finding(tool, reason_codes)
        findings.append(real or {
            "toolId": tool, "label": "", "confidence": None, "evidenceRef": None, "status": "STUBBED",
        })

    # #59 item 2: for each COMPLETE finding whose evidenceRef names an Orthanc instance, write an
    # authorship-stamped DICOM Secondary Capture into that study. Best-effort per finding: one
    # failure never stops the next. Held behind ORTHANC_PRESIGN_WRITE_ENABLED on the write client
    # (default False), so this is inert on any deployment that hasn't opted in.
    #
    # The OrthancClient-is-None branch matters when the [imaging] extra is not installed (agent-tests
    # CI lane, dev-stack without pydicom). In that case the pixel tools already stayed STUBBED and
    # nothing would have produced an "orthanc:instance/" evidenceRef anyway -- so skipping the whole
    # write block is correct AND avoids an AttributeError on the None module-level import.
    orthanc_study_id = ctx.get("study", {}).get("orthancStudyId") or ""
    if orthanc_study_id and OrthancClient is not None:
        orthanc = OrthancClient()
        for f in findings:
            await _maybe_write_evidence_capture(f, orthanc_study_id, orthanc)

    tools_selected = [
        {"toolId": f["toolId"], "version": _tool_version(f), "status": f["status"]}
        for f in findings
    ]

    return {
        # 1.1.0: findings gained the optional rawScore/opThreshold margin fields (#86).
        "schemaVersion": "1.1.0",
        "workflowId": ctx["workflowId"],
        "toolsSelected": tools_selected,
        "findings": findings,
        "overallStatus": _overall_status([f["status"] for f in findings]),
        "agentVersion": AGENT_VERSION,
        "ranAt": now_iso(),
    }
