"""composer: the LLM writes prose, the deterministic layer decides, and every failure falls back.

Two invariant families, both mutation-style:
  * FAIL-SAFE -- flag off (default), no key, timeout, HTTP error, malformed reply, empty text:
    every one returns None and the dispatch pages with the deterministic one-liner. The composer
    must be UNABLE to fail a page.
  * DECISIONS UNTOUCHED -- with the composer forced on and returning a message that CLAIMS a
    different category, the reply's acrCategory/deadline/recipient still come from the classifier.
    A model that could re-classify would defeat #78's model-free-trigger thesis.

The prompt is also pinned lean-reference: finding label + category + window go to the API;
patient/order identifiers and the workflow id must not (PHI to an external model needs a
#30-style review first).
"""
import json
import os
from unittest import mock

import pytest

import composer
import handler
from handler import handle

from fakes import FakeFhir2, FakeLedger

SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_test",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"fhirServiceRequestId": "ServiceRequest/sr-1"},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}

CRITICAL = {"criticalFlags": [{"label": "aortic dissection", "severity": "critical"}]}

ESCALATION_RUNG = {
    "level": 2, "targetRole": "on-call-radiologist", "channels": ["pager", "sms"],
    "urgency": "critical", "attempt": 1,
    "reason": "sign-off gate timed out awaiting radiologist",
}

COMPOSED = ("**Critical Results Communication Protocol**\n\n"
            "**Finding:** Acute aortic dissection.\n"
            "**ACR Category:** Cat1 — immediately life-threatening.\n\n"
            "**Action plan:**\n- Acknowledge within 60 minutes.")


@pytest.fixture(autouse=True)
def stores():
    fhir, ledger = FakeFhir2(), FakeLedger()
    handler._FHIR, handler._LEDGER = fhir, ledger
    yield fhir, ledger
    handler._FHIR = handler._LEDGER = None


@pytest.fixture(autouse=True)
def composer_env():
    """Every test starts with the composer's env untouched-by-default and restored afterward."""
    with mock.patch.dict(os.environ):
        for var in ("COMMS_LLM_COMPOSER", "COMMS_LLM_MODEL", "COMMS_LLM_TIMEOUT_SECONDS",
                    "GEMINI_API_KEY", "COMMS_LLM_BASE_URL", "COMMS_LLM_API_KEY",
                    "COMMS_LLM_ALLOW_INSECURE"):
            os.environ.pop(var, None)
        yield


class _FakeClient:
    """Programmable httpx.AsyncClient double. Class-level so tests read what the module sent."""

    requests: list[dict] = []          # {"url":…, "headers":…, "json":…}
    response_body: dict | None = None  # None -> raise `error` instead
    status_error: bool = False
    error: Exception | None = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.requests.append({"url": url, "headers": headers or {}, "json": json})
        if _FakeClient.error is not None:
            raise _FakeClient.error

        class _R:
            def raise_for_status(self):
                if _FakeClient.status_error:
                    raise RuntimeError("HTTP 500")

            def json(self):
                return _FakeClient.response_body

        return _R()


@pytest.fixture(autouse=True)
def fake_http():
    _FakeClient.requests = []
    _FakeClient.response_body = {
        "candidates": [{"content": {"parts": [{"text": COMPOSED}]}}]}
    _FakeClient.status_error = False
    _FakeClient.error = None
    with mock.patch.object(composer.httpx, "AsyncClient", _FakeClient):
        yield


def _on(key: str = "k-test"):
    os.environ["COMMS_LLM_COMPOSER"] = "1"
    os.environ["GEMINI_API_KEY"] = key


# --- fail-safe: every failure mode is None, and off means NO egress ---------------------

async def test_the_default_is_off_and_makes_no_network_attempt():
    """Key present, flag absent: the FLAG must be what stops the call (a keyless environment
    would mask a flag-default regression -- a mutation survived exactly that way once)."""
    os.environ["GEMINI_API_KEY"] = "k-test"
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None
    assert _FakeClient.requests == []


async def test_flag_on_without_a_key_falls_back_without_a_network_attempt():
    os.environ["COMMS_LLM_COMPOSER"] = "1"
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None
    assert _FakeClient.requests == []


async def test_a_successful_reply_returns_the_composed_text():
    _on()
    out = await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60)
    assert out == COMPOSED


@pytest.mark.parametrize("break_it", ["timeout", "http_error", "malformed", "empty_text"])
async def test_every_failure_mode_returns_none_not_an_exception(break_it):
    _on()
    if break_it == "timeout":
        _FakeClient.error = TimeoutError("deadline")
    elif break_it == "http_error":
        _FakeClient.status_error = True
    elif break_it == "malformed":
        _FakeClient.response_body = {"candidates": []}
    elif break_it == "empty_text":
        _FakeClient.response_body = {
            "candidates": [{"content": {"parts": [{"text": "   \n"}]}}]}
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None


# --- key + prompt hygiene ----------------------------------------------------------------

async def test_the_key_rides_the_header_and_the_model_rides_the_url():
    _on(key="k-secret")
    os.environ["COMMS_LLM_MODEL"] = "gemini-test-model"
    await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60)
    (req,) = _FakeClient.requests
    assert req["headers"]["x-goog-api-key"] == "k-secret"
    assert "k-secret" not in req["url"]
    assert "gemini-test-model:generateContent" in req["url"]


async def test_the_prompt_is_lean_reference_only():
    """Category + label + window go out; identifiers must not. Widening this is a #30 review."""
    _on()
    await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60)
    (req,) = _FakeClient.requests
    prompt = json.dumps(req["json"])
    assert "aortic dissection" in prompt and "Cat1" in prompt and "60" in prompt
    for phi_shaped in ("Patient/", "ServiceRequest/", "wf_test", "1.2.3"):
        assert phi_shaped not in prompt


# --- through the handler: prose upgraded, decisions untouched ----------------------------

async def test_flag_off_dispatch_pages_with_the_deterministic_one_liner(stores):
    _, ledger = stores
    os.environ["GEMINI_API_KEY"] = "k-test"   # key alone must not enable composition
    out = await handle("comms.dispatch",
                       {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    assert out["dispatchStatus"] == "SENT"
    comm = ledger.communications[out["communicationId"]]
    assert comm.payload[0].contentString == "aortic dissection"
    assert _FakeClient.requests == []


async def test_composed_prose_lands_in_the_communication_payload(stores):
    _, ledger = stores
    _on()
    out = await handle("comms.dispatch",
                       {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    comm = ledger.communications[out["communicationId"]]
    assert comm.payload[0].contentString == COMPOSED


async def test_the_model_cannot_move_the_category_or_the_deadline(stores):
    """Mutation-style: the fake model INSISTS the finding is routine. The reply's category, ack
    deadline, and recipient must still be the classifier's Cat1/60-minute decision."""
    _, ledger = stores
    _on()
    _FakeClient.response_body = {"candidates": [{"content": {"parts": [{
        "text": "**ACR Category:** Cat3 — routine, no communication required."}]}}]}
    out = await handle("comms.dispatch",
                       {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    assert out["acrCategory"] == "Cat1"
    assert out["taskId"]                       # the ack clock opened regardless
    task = ledger.tasks[out["taskId"]]
    deadline = task.restriction.period.end - task.restriction.period.start
    assert deadline.total_seconds() == 60 * 60  # the classifier's Cat1 window, not the model's
    assert out["recipient"] == "Practitioner/dr-order"


async def test_a_composer_failure_still_pages_with_the_fallback_text(stores):
    _, ledger = stores
    _on()
    _FakeClient.error = TimeoutError("deadline")
    out = await handle("comms.dispatch",
                       {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    assert out["dispatchStatus"] == "SENT"
    comm = ledger.communications[out["communicationId"]]
    assert comm.payload[0].contentString == "aortic dissection"


async def test_the_escalation_rung_and_routine_paths_never_consult_the_model(stores):
    """The #29 rung is dispatched verbatim (two-gate design) and a routine result opens nothing --
    neither path may spend a network call or a timeout budget on prose."""
    _on()
    await handle("comms.dispatch",
                 {"studyContext": SAMPLE_CONTEXT, "escalation": ESCALATION_RUNG})
    await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT})
    assert _FakeClient.requests == []


# --- the prose cannot contradict the decided category (the #77 consistency precedent) ---

async def test_prose_naming_a_different_category_falls_back():
    """The category is paging semantics: text that says Cat3 while the clock runs on Cat1 tells
    the physician the wrong urgency for their own deadline. Contradicting prose is rejected and
    the deterministic one-liner goes out instead."""
    _on()
    _FakeClient.response_body = {"candidates": [{"content": {"parts": [{"text":
        "**ACR Category:** Cat3 — routine, no urgency."}]}}]}
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None


async def test_prose_missing_the_decided_category_falls_back():
    """A protocol message that never states the category is malformed for its one job."""
    _on()
    _FakeClient.response_body = {"candidates": [{"content": {"parts": [{"text":
        "Please review this important finding at your convenience."}]}}]}
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None


# --- operator-surface parity + the model token stays a token ----------------------------

async def test_on_token_does_not_enable():
    """Truthy set is byte-for-byte the family's ({1,true,yes}): an operator whose "on" works
    here but not on the other switches -- or vice versa -- is the !73-review trap."""
    os.environ["COMMS_LLM_COMPOSER"] = "on"
    os.environ["GEMINI_API_KEY"] = "k-test"
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None
    assert _FakeClient.requests == []


async def test_model_name_with_path_characters_falls_back_without_a_request():
    """COMMS_LLM_MODEL rides the URL path; '../' or '?' in a typo'd value must degrade like any
    other failure, never steer the endpoint."""
    _on()
    os.environ["COMMS_LLM_MODEL"] = "../v1/other:endpoint?x=1"
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None
    assert _FakeClient.requests == []


# --- OpenAI-compatible backend: the #77 local-hosting path ------------------------------

def _local(base_url: str = "http://127.0.0.1:11434/v1", model: str = "qwen2.5:7b"):
    """Flag on, pointed at a local OpenAI-compatible endpoint. Deliberately NO Gemini key: the
    whole point of this backend is that it reaches a model without one."""
    os.environ["COMMS_LLM_COMPOSER"] = "1"
    os.environ["COMMS_LLM_BASE_URL"] = base_url
    os.environ["COMMS_LLM_MODEL"] = model
    _FakeClient.response_body = {"choices": [{"message": {"content": COMPOSED}}]}


async def test_a_local_model_composes_with_no_gemini_key_at_all():
    """The reason this backend exists: #77 chose local open-weights, and the Gemini key check was
    the gate that made 'local' unreachable."""
    _local()
    assert os.environ.get("GEMINI_API_KEY") is None
    out = await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60)
    assert out == COMPOSED


async def test_the_request_is_openai_shaped_and_never_touches_gemini():
    _local()
    await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60)
    (req,) = _FakeClient.requests
    assert req["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert "googleapis.com" not in req["url"]
    assert req["json"]["model"] == "qwen2.5:7b"
    assert req["json"]["messages"][0]["content"].count("Cat1") >= 1


async def test_a_model_name_with_a_colon_is_accepted_here():
    """The Gemini path's URL-safety regex rejects 'qwen2.5:7b'. Applying it to a JSON body field
    would silently fall back on exactly the models this backend exists to reach."""
    _local(model="qwen2.5:7b")
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) == COMPOSED
    assert _FakeClient.requests[0]["json"]["model"] == "qwen2.5:7b"


async def test_an_optional_key_rides_authorization_and_never_the_url():
    _local(base_url="https://llm.internal/v1")
    os.environ["COMMS_LLM_API_KEY"] = "k-secret"
    await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60)
    (req,) = _FakeClient.requests
    assert req["headers"]["Authorization"] == "Bearer k-secret"
    assert "k-secret" not in req["url"]


async def test_no_key_sends_no_authorization_header():
    _local()
    await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60)
    assert "Authorization" not in _FakeClient.requests[0]["headers"]


async def test_a_base_url_without_a_model_falls_back_without_a_request():
    """Half-set is a misconfiguration, not a request to guess: there is no sensible default model
    for an arbitrary endpoint. It must NOT silently fall through to Gemini either."""
    os.environ["COMMS_LLM_COMPOSER"] = "1"
    os.environ["COMMS_LLM_BASE_URL"] = "http://127.0.0.1:11434/v1"
    os.environ["GEMINI_API_KEY"] = "k-test"
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None
    assert _FakeClient.requests == []


# --- transport guard (mirrors llm_draft / fhir_client) ------------------------------------

@pytest.mark.parametrize("base_url", [
    "http://127.0.0.1:11434/v1",     # loopback plaintext: a local model
    "http://localhost:11434/v1",
    "https://llm.internal/v1",       # remote, but encrypted
])
async def test_a_safe_transport_needs_no_opt_in(base_url):
    _local(base_url=base_url)
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) == COMPOSED


async def test_plaintext_to_a_remote_host_is_refused_without_the_opt_in():
    """From a container nothing on the docker host is loopback, so this is the case an operator
    actually hits -- it must refuse rather than quietly ship a cohort-derived label on the wire."""
    _local(base_url="http://host.docker.internal:11434/v1")
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None
    assert _FakeClient.requests == []


async def test_the_opt_in_releases_plaintext_to_a_remote_host():
    _local(base_url="http://host.docker.internal:11434/v1")
    os.environ["COMMS_LLM_ALLOW_INSECURE"] = "1"
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) == COMPOSED


async def test_the_insecure_opt_in_uses_the_family_truthy_set():
    """Byte-for-byte the family's set (!73 item 3): 'on' does not enable this one either."""
    _local(base_url="http://host.docker.internal:11434/v1")
    os.environ["COMMS_LLM_ALLOW_INSECURE"] = "on"
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None
    assert _FakeClient.requests == []


async def test_the_refusal_log_names_the_host_but_not_the_path(caplog):
    _local(base_url="http://host.docker.internal:11434/v1/deployments/secret-name")
    with caplog.at_level("WARNING"):
        await composer.compose_notification(
            acr_category="Cat1", finding="aortic dissection", ack_minutes=60)
    logged = caplog.text
    assert "host.docker.internal" in logged and "secret-name" not in logged


# --- the shared acceptance checks apply on BOTH backends ----------------------------------

@pytest.mark.parametrize("break_it", ["timeout", "http_error", "malformed", "empty_text"])
async def test_every_failure_mode_is_none_on_the_local_backend_too(break_it):
    _local()
    if break_it == "timeout":
        _FakeClient.error = TimeoutError("deadline")
    elif break_it == "http_error":
        _FakeClient.status_error = True
    elif break_it == "malformed":
        _FakeClient.response_body = {"choices": []}
    elif break_it == "empty_text":
        _FakeClient.response_body = {"choices": [{"message": {"content": "  \n"}}]}
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None


async def test_the_local_backend_cannot_contradict_the_decided_category_either():
    _local()
    _FakeClient.response_body = {
        "choices": [{"message": {"content": COMPOSED.replace("Cat1", "Cat3")}}]}
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None


async def test_the_flag_still_gates_the_local_backend():
    """Base URL + model set, flag absent: the FLAG remains the only switch."""
    os.environ["COMMS_LLM_BASE_URL"] = "http://127.0.0.1:11434/v1"
    os.environ["COMMS_LLM_MODEL"] = "qwen2.5:7b"
    assert await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60) is None
    assert _FakeClient.requests == []


async def test_the_local_prompt_is_lean_reference_only():
    _local()
    await composer.compose_notification(
        acr_category="Cat1", finding="aortic dissection", ack_minutes=60)
    prompt = json.dumps(_FakeClient.requests[0]["json"])
    assert "aortic dissection" in prompt and "Cat1" in prompt and "60" in prompt
    for phi_shaped in ("Patient/", "ServiceRequest/", "wf_test", "1.2.3"):
        assert phi_shaped not in prompt
