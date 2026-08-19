"""Tests for the DICOM evidence-capture wiring in ``handler._maybe_write_evidence_capture`` (#59
item 2 of the "Then" list).

Uses in-process fakes rather than an httpx mock so the tests exercise the helper's decision logic
directly: which findings get the write, how instance-id → SOPInstanceUID resolution behaves, and
how failures at each stage flow through the best-effort contract. The write client's own guards
(feature flag, transport, DICOM construction) are exercised by
``libs/radagent-common/tests/test_orthanc_client_write.py`` -- not re-tested here.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

import handler


_ORTHANC_STUDY_ID = "abc-123"
_ORTHANC_INSTANCE_ID = "inst-xyz-99"
_SOP_INSTANCE_UID = "1.2.840.113619.2.55.3.111.999"


class _FakeOrthancClient:
    """Records what the helper called us with, plus configurable failure injection at each step
    so a single test can drive one failure mode without touching the others."""

    def __init__(
        self,
        tags: Optional[dict] = None,
        tags_raises: Optional[Exception] = None,
        write_returns: Optional[str] = None,
        write_raises: Optional[Exception] = None,
    ):
        self._tags = tags or {"SOPInstanceUID": _SOP_INSTANCE_UID}
        self._tags_raises = tags_raises
        self._write_returns = write_returns or "2.25.999999"
        self._write_raises = write_raises
        # Recorded calls.
        self.tags_calls: list[str] = []
        self.write_calls: list[dict] = []

    async def get_instance_tags(self, orthanc_instance_id: str) -> dict:
        self.tags_calls.append(orthanc_instance_id)
        if self._tags_raises is not None:
            raise self._tags_raises
        return dict(self._tags)

    async def write_ai_evidence_capture(self, **kwargs) -> Optional[str]:
        self.write_calls.append(dict(kwargs))
        if self._write_raises is not None:
            raise self._write_raises
        return self._write_returns


# ===========================================================================
# Happy path: COMPLETE + orthanc:instance/ → resolve → write
# ===========================================================================

def test_complete_with_orthanc_instance_ref_triggers_the_write():
    """The primary contract: a pixel-tool COMPLETE finding whose evidenceRef names an Orthanc
    instance results in a write_ai_evidence_capture call with the resolved SOPInstanceUID and
    the finding's label/confidence."""
    client = _FakeOrthancClient()
    finding = {
        "toolId": "pneumothorax-detect",
        "label": "Pneumothorax (screening p=0.87); screening signal only, not a read",
        "confidence": 0.87,
        "evidenceRef": f"orthanc:instance/{_ORTHANC_INSTANCE_ID}",
        "status": "COMPLETE",
    }
    asyncio.run(handler._maybe_write_evidence_capture(finding, _ORTHANC_STUDY_ID, client))

    assert client.tags_calls == [_ORTHANC_INSTANCE_ID]
    assert len(client.write_calls) == 1
    call = client.write_calls[0]
    assert call["target_sop_instance_uid"] == _SOP_INSTANCE_UID
    assert call["orthanc_study_id"] == _ORTHANC_STUDY_ID
    assert call["tool_id"] == "pneumothorax-detect"
    assert call["confidence"] == 0.87
    assert "Pneumothorax" in call["label"]


# ===========================================================================
# Caller-level skips: status gate + evidenceRef-shape gate
# ===========================================================================

@pytest.mark.parametrize("status", ["STUBBED", "ERROR", "PARTIAL"])
def test_non_complete_findings_are_skipped(status):
    """Only COMPLETE findings are evidence-capture-eligible. A STUBBED (referral-rule) or ERROR
    (model reached the instance but threw) finding must not trigger a write, even if its
    evidenceRef points at a real instance -- STUBBED/ERROR is exactly the signal that we should
    NOT be authoring imaging evidence into the archive."""
    client = _FakeOrthancClient()
    finding = {
        "toolId": "pneumothorax-detect",
        "label": "something",
        "confidence": None,
        "evidenceRef": f"orthanc:instance/{_ORTHANC_INSTANCE_ID}",
        "status": status,
    }
    asyncio.run(handler._maybe_write_evidence_capture(finding, _ORTHANC_STUDY_ID, client))

    assert client.tags_calls == []  # no Orthanc IO at all
    assert client.write_calls == []


@pytest.mark.parametrize("ev", [
    "order.reasonCode=J93.1",  # referral-rule locator
    "",                        # empty
    None,                      # null (contract allows it)
    "orthanc-instance/abc",    # nearly-right prefix, but not our contract
    "sop:1.2.3.4",             # different shape entirely
])
def test_non_orthanc_evidence_refs_are_skipped(ev):
    """The wiring keys off the exact ``orthanc:instance/`` prefix. Anything else -- a
    referral-rule locator, a null, or a differently-shaped ref -- is not evidence-capture-eligible.
    Skip silently: the write client already has no idea what to do with it, and a warning here
    would fire on every STUBBED-with-order-reason finding."""
    client = _FakeOrthancClient()
    finding = {
        "toolId": "pneumothorax-detect",
        "label": "x",
        "confidence": 0.9,
        "evidenceRef": ev,
        "status": "COMPLETE",
    }
    asyncio.run(handler._maybe_write_evidence_capture(finding, _ORTHANC_STUDY_ID, client))

    assert client.tags_calls == []
    assert client.write_calls == []


# ===========================================================================
# Best-effort failure modes: tag-resolve failure, missing tag, write failure
# ===========================================================================

def test_tag_resolve_failure_skips_write_and_does_not_raise(caplog):
    """Orthanc is down or the instance is 404 -- ``get_instance_tags`` raises. The helper
    logs and returns without invoking the write. Other findings processed after this one must
    not be blocked."""
    client = _FakeOrthancClient(tags_raises=RuntimeError("orthanc HTTP 500"))
    finding = {
        "toolId": "pneumothorax-detect",
        "label": "x", "confidence": 0.9,
        "evidenceRef": f"orthanc:instance/{_ORTHANC_INSTANCE_ID}",
        "status": "COMPLETE",
    }
    asyncio.run(handler._maybe_write_evidence_capture(finding, _ORTHANC_STUDY_ID, client))

    assert client.tags_calls == [_ORTHANC_INSTANCE_ID]
    assert client.write_calls == []
    assert any("could not read tags" in r.message for r in caplog.records)


def test_missing_sop_instance_uid_tag_skips_write(caplog):
    """The instance exists in Orthanc but its /simplified-tags response has no SOPInstanceUID
    field (malformed source data or a non-DICOM instance). The write needs that UID for the SC's
    SourceImageSequence, so we skip and log rather than write an SC with an empty reference."""
    client = _FakeOrthancClient(tags={"PatientName": "DOE^JANE"})  # no SOPInstanceUID
    finding = {
        "toolId": "pneumothorax-detect",
        "label": "x", "confidence": 0.9,
        "evidenceRef": f"orthanc:instance/{_ORTHANC_INSTANCE_ID}",
        "status": "COMPLETE",
    }
    asyncio.run(handler._maybe_write_evidence_capture(finding, _ORTHANC_STUDY_ID, client))

    assert client.write_calls == []
    assert any("has no SOPInstanceUID tag" in r.message for r in caplog.records)


def test_write_raising_is_swallowed_and_logged(caplog):
    """The write client's own contract is best-effort (returns None on outage / disabled), BUT
    a transport-policy refusal RE-RAISES ``InsecureWriteTransportError`` deliberately. The wiring
    must not let that escape and stop other findings from being processed -- catch, log, and move
    on. A misconfigured deployment gets a per-finding warning rather than an aborted runTools."""
    from radagent_common.orthanc_client import InsecureWriteTransportError
    client = _FakeOrthancClient(
        write_raises=InsecureWriteTransportError("refusing plaintext write"),
    )
    finding = {
        "toolId": "pneumothorax-detect",
        "label": "x", "confidence": 0.9,
        "evidenceRef": f"orthanc:instance/{_ORTHANC_INSTANCE_ID}",
        "status": "COMPLETE",
    }
    asyncio.run(handler._maybe_write_evidence_capture(finding, _ORTHANC_STUDY_ID, client))

    assert len(client.write_calls) == 1
    assert any("evidence-capture write raised" in r.message for r in caplog.records)


def test_write_returning_none_is_not_treated_as_error():
    """The write client returns None when the feature flag is off or the target is missing.
    That is the write path's normal no-op, NOT a failure -- the helper must return cleanly
    without logging a warning."""
    client = _FakeOrthancClient(write_returns=None)
    finding = {
        "toolId": "pneumothorax-detect",
        "label": "x", "confidence": 0.9,
        "evidenceRef": f"orthanc:instance/{_ORTHANC_INSTANCE_ID}",
        "status": "COMPLETE",
    }
    # Must not raise.
    asyncio.run(handler._maybe_write_evidence_capture(finding, _ORTHANC_STUDY_ID, client))
    assert len(client.write_calls) == 1


# ===========================================================================
# Missing orthanc_study_id: guard against a partial ctx
# ===========================================================================

def test_missing_orthanc_study_id_skips_write():
    """The write client needs an orthanc_study_id to stamp into the SC's study identifiers. If the
    ctx doesn't carry one (probably a bug upstream, but tolerated), skip silently rather than
    write an SC with an empty study id."""
    client = _FakeOrthancClient()
    finding = {
        "toolId": "pneumothorax-detect",
        "label": "x", "confidence": 0.9,
        "evidenceRef": f"orthanc:instance/{_ORTHANC_INSTANCE_ID}",
        "status": "COMPLETE",
    }
    asyncio.run(handler._maybe_write_evidence_capture(finding, "", client))  # empty study id
    assert client.tags_calls == []
    assert client.write_calls == []


# ===========================================================================
# Handle() integration: caller-side loop calls the helper for each finding
# ===========================================================================

def test_handle_iterates_findings_and_writes_for_each_eligible_one(monkeypatch):
    """Full handle() flow with select_tools stubbed: two tools, one returns a COMPLETE +
    orthanc:instance/ finding, one returns STUBBED. The wiring must fire for the first and skip
    the second, and one Orthanc outage must not stop the other write from being attempted.

    Also monkeypatches OrthancClient so the wiring block's `OrthancClient is not None` gate
    passes -- in the agent-tests CI lane the [imaging] extra is intentionally absent, so
    OrthancClient is None at module scope. Substituting a dummy class here exercises the wiring
    logic without pulling in pydicom + torch."""
    # Stub the tool selection so we don't drag PIXEL_TOOLING branches in.
    monkeypatch.setattr(handler, "select_tools", lambda modality, desc: ["a", "b"])

    # In the extras-less test lane, OrthancClient is None; substitute a dummy that constructs
    # without side effects, so the wiring block's guard passes and the loop runs.
    monkeypatch.setattr(handler, "OrthancClient", lambda *a, **kw: object())

    # "a" is a pixel-tool row whose (faked) shared scoring pass came back positive -> a COMPLETE
    # with an orthanc:instance/ evidenceRef via the real _head_finding; "b" is in no table ->
    # falls through to the reason rules (no hit), then a bare stub.
    monkeypatch.setattr(handler, "_PIXEL_TOOL_SPECS", {"a": {"head": "A", "display": "A"}})

    async def _fake_score_study(ctx):
        return ("scored", {"A": 0.9}, _ORTHANC_INSTANCE_ID)

    monkeypatch.setattr(handler, "_score_study", _fake_score_study)

    # Capture the wiring calls without hitting Orthanc.
    calls: list[dict] = []
    async def _fake_write(finding, study_id, client):
        calls.append({"toolId": finding["toolId"], "status": finding["status"]})
    monkeypatch.setattr(handler, "_maybe_write_evidence_capture", _fake_write)

    ctx = {
        "workflowId": "wf_test",
        "study": {"modality": "CT", "studyDescription": "CXR", "orthancStudyId": _ORTHANC_STUDY_ID},
        "order": {"reasonCode": []},
    }
    out = asyncio.run(handler.handle("interpretation.runTools", {"studyContext": ctx}))

    # Helper was invoked ONCE PER FINDING (including STUBBED ones) -- the STATUS gate lives in
    # the helper, not the caller, so STUBBED-fed calls are the helper's decision to skip.
    assert len(calls) == 2
    assert {c["toolId"] for c in calls} == {"a", "b"}
    # And the output still has both findings.
    assert len(out["findings"]) == 2
