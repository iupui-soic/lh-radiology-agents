"""The sign-off override confirm page: GET renders what is being waived, the form POST releases.

The #57 endpoint shipped as THE MISSING PRODUCER, but only for callers who could hand-build a
POST with a header on it -- the escalation payload's overrideUrl answered a browser with a 405.
These tests pin the clickable half: the GET/POST split (a bare GET must change nothing), the
token travelling as a form field (a browser form cannot set a header), the HTML error pages
keeping the API's status codes, and the JSON contract staying exactly as it was.

Fake-Temporal tier (the test_signoff_override.py posture): the client is a stub, so every
branch of the page -- unknown query, unreachable workflow, signal failure -- is reachable.
test_signoff_override_http.py drives the same page against a REAL parked workflow.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from httpx import ASGITransport

import orchestrator.ingress as ingress
from orchestrator.workflow import StudyWorkflow

_TOKEN = "page-secret"
WF = "wf_page_1"

# `message` present ON PURPOSE: the workflow-side query strips it, but the page must not render
# it even if a context somehow carries one -- it may quote report text (lean-reference).
CONTEXT = {
    "state": "AWAITING_SIGNOFF",
    "verificationStatus": "WARN",
    "requiresHumanReview": True,
    "issues": [{"ruleId": "critical_finding_unflagged", "severity": "WARN",
                "message": "NEVER-RENDER: body names pneumothorax"}],
}

GOOD_FORM = {"acknowledgedBy": "Wei Chen (reading radiologist)",
             "reason": "verification WARN acknowledged; findings reviewed, report stands",
             "token": _TOKEN}


class FakeHandle:
    def __init__(self, context=CONTEXT, state="AWAITING_SIGNOFF",
                 context_exc=None, state_exc=None, signal_exc=None):
        self.context, self.state = context, state
        self.context_exc, self.state_exc, self.signal_exc = context_exc, state_exc, signal_exc
        self.signals: list = []

    async def query(self, q, *args):
        if q is StudyWorkflow.signoff_context:
            if self.context_exc:
                raise self.context_exc
            return self.context
        if q is StudyWorkflow.current_state:
            if self.state_exc:
                raise self.state_exc
            return self.state
        raise AssertionError(f"unexpected query {q}")

    async def signal(self, s, payload):
        if self.signal_exc:
            raise self.signal_exc
        self.signals.append(payload)


class FakeClient:
    def __init__(self, handle):
        self.handle = handle

    def get_workflow_handle(self, workflow_id):
        return self.handle


def _drive(handle, method="GET", data=None, json=None, headers=None):
    async def go():
        saved_client, saved_token = ingress._client, ingress.SIGNOFF_OVERRIDE_TOKEN
        ingress._client, ingress.SIGNOFF_OVERRIDE_TOKEN = FakeClient(handle), _TOKEN
        try:
            transport = ASGITransport(app=ingress.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                url = f"/signoff/{WF}/override"
                if method == "GET":
                    return await c.get(url)
                return await c.post(url, data=data, json=json, headers=headers)
        finally:
            ingress._client, ingress.SIGNOFF_OVERRIDE_TOKEN = saved_client, saved_token
    return asyncio.run(go())


# --- GET: the page ---------------------------------------------------------------------------

def test_get_renders_the_form_with_the_verdict_and_never_issue_messages():
    r = _drive(FakeHandle())
    assert r.status_code == 200
    page = r.text
    # No absolute action: the form must post back to whatever URL served it (see the
    # prefix test below). Pinning "/signoff/..." here is what let the overlay bug ship.
    assert "<form method=\"post\">" in page
    assert "action=" not in page
    for field in ("acknowledgedBy", "reason", "token"):
        assert f"name=\"{field}\"" in page
    assert "WARN" in page and "critical_finding_unflagged" in page
    assert "requires human review" in page
    assert "NEVER-RENDER" not in page, "issue messages may quote report text; the page must not show them"
    assert "pneumothorax" not in page


def test_get_for_a_workflow_not_at_the_gate_offers_no_form():
    ctx = dict(CONTEXT, state="COMMUNICATE")
    r = _drive(FakeHandle(context=ctx))
    assert r.status_code == 200
    assert "<form" not in r.text
    assert "Nothing to release" in r.text and "COMMUNICATE" in r.text


def test_get_falls_back_to_current_state_for_a_worker_predating_the_query():
    handle = FakeHandle(context_exc=RuntimeError("unknown query"), state="AWAITING_SIGNOFF")
    r = _drive(handle)
    assert r.status_code == 200
    assert "<form" in r.text
    assert "Verification:" not in r.text  # no verdict available -- degrade, don't invent


def test_get_renders_the_bare_form_when_the_workflow_cannot_be_queried():
    """An on-call clinician following a paged overrideUrl must land on a working page even when
    Temporal is having a moment; the POST re-checks everything that matters."""
    boom = RuntimeError("temporal down")
    r = _drive(FakeHandle(context_exc=boom, state_exc=boom))
    assert r.status_code == 200
    assert "<form" in r.text
    assert "could not be queried" in r.text


# --- POST, form-encoded: the button ----------------------------------------------------------

def test_form_post_with_the_token_field_releases_and_renders_the_receipt():
    handle = FakeHandle()
    r = _drive(handle, method="POST", data=GOOD_FORM)
    assert r.status_code == 200
    assert "study released" in r.text
    assert "Wei Chen" in r.text
    assert len(handle.signals) == 1
    ack = handle.signals[0]
    assert ack["acknowledgedBy"] == GOOD_FORM["acknowledgedBy"]
    assert ack["reason"] == GOOD_FORM["reason"]
    assert ack["acknowledgedAt"]


def test_form_post_with_a_wrong_token_is_401_html_and_does_not_signal():
    handle = FakeHandle()
    r = _drive(handle, method="POST", data=dict(GOOD_FORM, token="wrong"))
    assert r.status_code == 401
    assert "still held" in r.text
    assert handle.signals == []


def test_form_post_missing_the_reason_is_422_html_and_does_not_signal():
    handle = FakeHandle()
    r = _drive(handle, method="POST", data={"acknowledgedBy": "Wei Chen", "token": _TOKEN})
    assert r.status_code == 422
    assert "still held" in r.text
    assert handle.signals == []


def test_form_post_when_the_signal_fails_is_502_and_says_the_gate_is_held():
    handle = FakeHandle(signal_exc=RuntimeError("no such workflow"))
    r = _drive(handle, method="POST", data=GOOD_FORM)
    assert r.status_code == 502
    assert "still held" in r.text


def test_a_header_token_still_wins_on_the_form_path():
    """Anything already POSTing forms with the header keeps working; the form field is the
    fallback for browsers, not a replacement for the header."""
    handle = FakeHandle()
    r = _drive(handle, method="POST",
               data={k: v for k, v in GOOD_FORM.items() if k != "token"},
               headers={"X-Signoff-Token": _TOKEN})
    assert r.status_code == 200
    assert len(handle.signals) == 1


# --- POST, JSON: the API contract is unchanged -----------------------------------------------

def test_json_post_contract_is_unchanged():
    handle = FakeHandle()
    r = _drive(handle, method="POST",
               json={"acknowledgedBy": "Practitioner/dr-rao", "reason": "reviewed"},
               headers={"X-Signoff-Token": _TOKEN})
    assert r.status_code == 202
    body = r.json()
    assert body["acknowledged"] is True and body["workflowId"] == WF
    assert body["acknowledgedBy"] == "Practitioner/dr-rao" and body["acknowledgedAt"]
    assert len(handle.signals) == 1


def test_json_post_with_a_wrong_token_stays_a_json_401():
    handle = FakeHandle()
    r = _drive(handle, method="POST", json={"acknowledgedBy": "x", "reason": "y"},
               headers={"X-Signoff-Token": "wrong"})
    assert r.status_code == 401
    assert r.json()["detail"] == "bad sign-off override token"
    assert handle.signals == []


def test_a_non_object_json_body_is_a_422_not_a_500():
    r = _drive(FakeHandle(), method="POST", json=["not", "a", "dict"],
               headers={"X-Signoff-Token": _TOKEN})
    assert r.status_code == 422


# --- the form must survive a path prefix (#76 arc 3 rehearsal, 2026-08-20) --------------------

def test_the_form_posts_to_its_own_url_so_a_path_prefix_survives():
    """The page is published to phones through the #75 Caddy overlay at /ingress/..., which
    STRIPS the prefix before proxying (`handle_path /ingress/*`). A form carrying the absolute
    in-cluster action posted to /signoff/... on the viewer origin instead, fell through to OHIF's
    nginx and came back 405 -- the one human step in the pipeline, broken in the only deployment
    a paged clinician can reach. An action-less form posts to the current URL, so it works under
    any prefix.
    """
    page = _drive(FakeHandle()).text
    assert "<form method=\"post\">" in page
    # the in-cluster path must not be baked in anywhere in the form
    assert f"/signoff/{WF}/override" not in page


def test_a_prefixed_post_reaches_the_same_handler_and_releases():
    """What the overlay actually does: strip the prefix, then proxy. Prove the stripped path is
    the one the app serves, so a POST that follows the form lands on the release endpoint."""
    handle = FakeHandle()
    r = _drive(handle, method="post", data=GOOD_FORM)
    assert r.status_code == 200          # the HTML path renders a receipt; 202 is the JSON contract
    assert handle.signals, "the form POST must reach the release endpoint and signal the workflow"
