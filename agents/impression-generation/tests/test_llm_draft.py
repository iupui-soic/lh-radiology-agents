"""Tests for llm_draft.draft_impression (#77).

Uses httpx.MockTransport so every network call is intercepted -- never touches a real LLM
endpoint. Covers: the default-off gate, the half-set misconfiguration warn-once-and-degrade,
best-effort degrade-to-None on every failure mode (network, timeout, non-2xx, malformed JSON,
empty recommendations, prose that ignores a confirmed critical flag), and the happy path.
"""
from __future__ import annotations

import json as _json

import httpx

import llm_draft
from llm_draft import LLMDraft, draft_impression

_LLM_ENV = (
    "IMPRESSION_LLM_BASE_URL", "IMPRESSION_LLM_MODEL",
    "IMPRESSION_LLM_API_KEY", "IMPRESSION_LLM_TIMEOUT_SECONDS",
    "IMPRESSION_LLM_ALLOW_INSECURE",
)

# Capture the real AsyncClient before any monkeypatch replaces it -- tests patch
# llm_draft.httpx.AsyncClient, the same class object as this one (import httpx shares the ref).
_REAL_ASYNC_CLIENT = httpx.AsyncClient

CRITICAL_FLAGS = [{"label": "pneumothorax", "severity": "critical"}]


def _clear(monkeypatch) -> None:
    for k in _LLM_ENV:
        monkeypatch.delenv(k, raising=False)


def _configure(monkeypatch, base_url: str = "http://localhost:8000/v1", model: str = "test-model") -> None:
    # Default to a loopback base URL: the transport guard allows it, so the behavioural tests below
    # exercise the LLM path itself. The plaintext-remote guard is covered by its own tests.
    monkeypatch.setenv("IMPRESSION_LLM_BASE_URL", base_url)
    monkeypatch.setenv("IMPRESSION_LLM_MODEL", model)


def _install(monkeypatch, transport: httpx.MockTransport) -> None:
    monkeypatch.setattr(
        "llm_draft.httpx.AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
    )


def _responding(status_code: int = 200, content: str = "") -> tuple[httpx.MockTransport, list[dict]]:
    """A transport that answers every POST with a chat-completion envelope wrapping `content`,
    recording each intercepted request for post-hoc assertions."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "body": _json.loads(request.content or b"{}")})
        return httpx.Response(status_code, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handler), seen


async def test_disabled_when_unset(monkeypatch):
    _clear(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200)

    _install(monkeypatch, httpx.MockTransport(handler))
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out is None
    assert calls["n"] == 0  # unset is the default -- no network call attempted at all


async def test_misconfigured_half_set_warns_once_and_degrades(monkeypatch, caplog):
    _clear(monkeypatch)
    monkeypatch.setenv("IMPRESSION_LLM_BASE_URL", "http://llm-host:8000/v1")  # model left unset
    monkeypatch.setattr(llm_draft, "_warned_misconfigured", False)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out is None
    assert "must both be set" in caplog.text


async def test_model_down_degrades_to_none(monkeypatch):
    _clear(monkeypatch)
    _configure(monkeypatch)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install(monkeypatch, httpx.MockTransport(refuse))
    out = await draft_impression(
        conclusion="no acute findings", finding_labels="", critical_flags=[], ehr_context={}
    )
    assert out is None


async def test_timeout_degrades_to_none(monkeypatch):
    _clear(monkeypatch)
    _configure(monkeypatch)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    _install(monkeypatch, httpx.MockTransport(slow))
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out is None


async def test_http_5xx_degrades_to_none(monkeypatch):
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, _ = _responding(500, content="")
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out is None


async def test_malformed_json_degrades_to_none(monkeypatch, caplog):
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, _ = _responding(200, content="not json at all")
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out is None
    assert "JSONDecodeError" in caplog.text


async def test_empty_recommendations_accepted_without_critical_flags(monkeypatch):
    """A normal study warrants no recommendation, and the model says so with [] (#103).

    This used to be rejected as malformed, which discarded the LLM draft for most of a screening
    cohort -- 11 of 12 studies on the demo host -- while every deployment check said the feature
    was on, because the fallback to the template is silent by design.
    """
    _clear(monkeypatch)
    _configure(monkeypatch)
    content = '{"impressionText": "No acute cardiopulmonary abnormality.", "recommendations": []}'
    transport, _ = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out == LLMDraft(impression_text="No acute cardiopulmonary abnormality.", recommendations=[])


async def test_empty_recommendations_still_refused_beside_a_critical_flag(monkeypatch, caplog):
    """Where a recommendation is load-bearing, silence is not acceptable (#103).

    Prose that names a confirmed critical finding and then advises nothing reads as reassurance,
    so this keeps falling back to the deterministic template, which does carry an action.
    """
    _clear(monkeypatch)
    _configure(monkeypatch)
    content = '{"impressionText": "Pneumothorax is present.", "recommendations": []}'
    transport, _ = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(
        conclusion="", finding_labels="pneumothorax", critical_flags=CRITICAL_FLAGS, ehr_context={},
    )
    assert out is None
    assert "no recommendations alongside a confirmed critical flag" in caplog.text


async def test_non_string_recommendations_still_rejected(monkeypatch, caplog):
    _clear(monkeypatch)
    _configure(monkeypatch)
    content = '{"impressionText": "No acute findings.", "recommendations": [{"text": "Follow up."}]}'
    transport, _ = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out is None
    assert "malformed recommendations" in caplog.text


async def test_trailing_commentary_after_the_json_is_tolerated(monkeypatch):
    """Backends decorate the object despite response_format (#103): "Extra data" threw the draft away."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    content = (
        'Here is the impression:\n'
        '{"impressionText": "No acute findings.", "recommendations": ["Routine follow-up."]}\n'
        'Let me know if you would like it shortened.'
    )
    transport, _ = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out == LLMDraft(impression_text="No acute findings.", recommendations=["Routine follow-up."])


async def test_brace_inside_the_prose_does_not_unbalance_the_scan(monkeypatch):
    _clear(monkeypatch)
    _configure(monkeypatch)
    content = '{"impressionText": "Density measures 40 HU {sic}.", "recommendations": ["Correlate."]} trailing'
    transport, _ = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out == LLMDraft(impression_text="Density measures 40 HU {sic}.", recommendations=["Correlate."])


async def test_fallback_is_counted_and_logged(monkeypatch, caplog):
    """The #103 failure mode was silence, not error: every check looked healthy while the feature
    was inert. A running tally makes the rate visible without diffing outputs."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    monkeypatch.setattr(llm_draft, "_ATTEMPTS", 0)
    monkeypatch.setattr(llm_draft, "_FALLBACKS", 0)
    transport, _ = _responding(200, content="not json at all")
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out is None
    assert "fell back to the deterministic template (1 of 1 attempts since start)" in caplog.text


async def test_prose_not_mentioning_critical_flag_degrades_to_none(monkeypatch, caplog):
    _clear(monkeypatch)
    _configure(monkeypatch)
    content = '{"impressionText": "No acute findings identified.", "recommendations": ["Routine follow-up."]}'
    transport, _ = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(
        conclusion="large pneumothorax", finding_labels="", critical_flags=CRITICAL_FLAGS, ehr_context={}
    )
    assert out is None
    assert "prose does not assert 1 of 1 confirmed critical flag(s)" in caplog.text


async def test_success_returns_llm_draft(monkeypatch):
    _clear(monkeypatch)
    _configure(monkeypatch, model="test-model")
    content = (
        '{"impressionText": "Findings consistent with a right-sided pneumothorax.", '
        '"recommendations": ["Urgent clinical correlation recommended."]}'
    )
    transport, seen = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(
        conclusion="large right pneumothorax",
        finding_labels="pneumothorax",
        critical_flags=CRITICAL_FLAGS,
        ehr_context={"activeProblems": [{"display": "COPD"}]},
    )
    assert out == LLMDraft(
        impression_text="Findings consistent with a right-sided pneumothorax.",
        recommendations=["Urgent clinical correlation recommended."],
    )
    assert len(seen) == 1
    assert seen[0]["url"] == "http://localhost:8000/v1/chat/completions"
    assert seen[0]["body"]["model"] == "test-model"
    assert seen[0]["body"]["messages"][-1]["role"] == "user"


async def test_plaintext_remote_refused_by_default(monkeypatch, caplog):
    _clear(monkeypatch)
    _configure(monkeypatch, base_url="http://llm-host:8000/v1")  # non-loopback plaintext
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    _install(monkeypatch, httpx.MockTransport(handler))
    out = await draft_impression(
        conclusion="large pneumothorax", finding_labels="pneumothorax",
        critical_flags=CRITICAL_FLAGS, ehr_context={"activeProblems": [{"display": "COPD"}]},
    )
    assert out is None
    assert calls["n"] == 0  # refused BEFORE any request leaves the process -- no PHI on the wire
    assert "plaintext" in caplog.text.lower()


async def test_plaintext_remote_allowed_with_optin(monkeypatch):
    _clear(monkeypatch)
    _configure(monkeypatch, base_url="http://llm-host:8000/v1")
    monkeypatch.setenv("IMPRESSION_LLM_ALLOW_INSECURE", "1")
    content = ('{"impressionText": "Findings consistent with a pneumothorax.", '
               '"recommendations": ["Urgent clinical correlation."]}')
    transport, seen = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(
        conclusion="pneumothorax", finding_labels="pneumothorax",
        critical_flags=CRITICAL_FLAGS, ehr_context={},
    )
    assert out is not None and len(seen) == 1  # opt-in lets it proceed


async def test_optin_accepts_capitalised_true(monkeypatch):
    # truthy set matches FHIR2_ALLOW_INSECURE_WRITE: True/YES/Yes all work, not just "1"
    assert llm_draft._egress_transport_is_secure("https://remote/v1")
    for val in ("1", "true", "True", "YES", "Yes"):
        monkeypatch.setenv("IMPRESSION_LLM_ALLOW_INSECURE", val)
        assert llm_draft._egress_transport_is_secure("http://llm-host:8000/v1"), val
    monkeypatch.setenv("IMPRESSION_LLM_ALLOW_INSECURE", "no")
    assert not llm_draft._egress_transport_is_secure("http://llm-host:8000/v1")


async def test_https_remote_allowed_without_optin(monkeypatch):
    _clear(monkeypatch)
    _configure(monkeypatch, base_url="https://llm-host/v1")  # TLS -> always fine
    content = ('{"impressionText": "Pneumothorax noted.", "recommendations": ["Correlate."]}')
    transport, seen = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(
        conclusion="pneumothorax", finding_labels="", critical_flags=CRITICAL_FLAGS, ehr_context={},
    )
    assert out is not None and len(seen) == 1


# --- the flag-consistency check is negation-aware and exhaustive ----------------------

def _draft_json(impression: str, recommendations: list[str] | None = None) -> str:
    return _json.dumps({"impressionText": impression,
                        "recommendations": recommendations or ["clinical correlation"]})


async def test_negated_prose_is_rejected(monkeypatch):
    """The reassuring-prose case the consistency check exists for: the draft NAMES the confirmed
    flag only to negate it. A bare substring test accepts this; the negation-aware check must
    not -- reassuring prose next to a real critical finding is the worst failure shape."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, _ = _responding(
        content=_draft_json("No pneumothorax is identified. Lungs are clear."))
    _install(monkeypatch, transport)
    out = await draft_impression(
        conclusion="large right pneumothorax", finding_labels="pneumothorax",
        critical_flags=CRITICAL_FLAGS, ehr_context={})
    assert out is None


async def test_prose_must_assert_every_confirmed_flag(monkeypatch):
    """Two confirmed flags, prose asserting only one: silent about the other -> rejected. Prose
    asserting both -> accepted."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    two_flags = [{"label": "pneumothorax", "severity": "critical"},
                 {"label": "aortic dissection", "severity": "critical"}]

    transport, _ = _responding(content=_draft_json("Large pneumothorax on the right."))
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="",
                                 critical_flags=two_flags, ehr_context={})
    assert out is None

    transport2, _ = _responding(content=_draft_json(
        "Large pneumothorax on the right. Findings concerning for aortic dissection."))
    _install(monkeypatch, transport2)
    out2 = await draft_impression(conclusion="", finding_labels="",
                                  critical_flags=two_flags, ehr_context={})
    assert isinstance(out2, LLMDraft)


async def test_flag_asserted_after_a_negated_mention_is_accepted(monkeypatch):
    """The #78 matcher's any-occurrence-asserted semantics must carry over: prose that first
    negates a small pneumothorax but asserts the remaining one is consistent, not a reject --
    over-rejection would silently cost every nuanced draft."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, _ = _responding(content=_draft_json(
        "No small apical pneumothorax; however a large basal pneumothorax remains."))
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="",
                                 critical_flags=CRITICAL_FLAGS, ehr_context={})
    assert isinstance(out, LLMDraft)


# --- the never-raise contract holds without handler.py's backstop ---------------------

async def test_malformed_base_url_never_raises(monkeypatch):
    """urlparse raises ValueError on an invalid-IPv6 base URL, and that used to happen OUTSIDE
    the try -- escaping a module whose whole contract is None-never-raise. The handler backstop
    caught it in production, which made the backstop load-bearing; now the module honours its
    own contract."""
    _clear(monkeypatch)
    _configure(monkeypatch, base_url="http://[::1oops/v1")
    out = await draft_impression(conclusion="", finding_labels="",
                                 critical_flags=[], ehr_context={})
    assert out is None


async def test_unexpected_ehr_context_shape_never_raises(monkeypatch):
    """Prompt building sits under the ladder too: a list where a dict was expected degrades to
    the template like any other failure."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, _ = _responding(content=_draft_json("Clear lungs."))
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="",
                                 critical_flags=[], ehr_context=["not", "a", "dict"])
    assert out is None


# --- only CODED clinical context rides to the external endpoint -----------------------

async def test_uncoded_ehr_entries_never_reach_the_prompt(monkeypatch):
    """fhir_client's projector falls back to the CodeableConcept's free `text` for the display
    when nothing is coded -- clinician-typed narrative. An uncoded entry must therefore be
    dropped from the outbound prompt entirely; a coded entry's terminology display rides."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, seen = _responding(content=_draft_json("Clear lungs."))
    _install(monkeypatch, transport)
    await draft_impression(
        conclusion="", finding_labels="", critical_flags=[],
        ehr_context={
            "activeProblems": [
                {"code": "J45.909", "display": "Asthma"},
                {"code": "", "display": "pt anxious re spouse's diagnosis, see note"},
            ],
            "relevantLabs": [
                {"code": "2160-0", "display": "Creatinine", "value": 1.1, "unit": "mg/dL"},
                {"display": "free-text lab comment from the chart"},
            ],
        })
    (request,) = seen
    outbound = _json.dumps(request["body"])
    assert "Asthma" in outbound and "Creatinine" in outbound
    assert "anxious" not in outbound
    assert "free-text lab comment" not in outbound


async def test_json_fence_wrapped_response_is_accepted(monkeypatch):
    """response_format support varies across OpenAI-compatible backends; a fenced-but-valid
    draft must parse rather than fall back."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    fenced = "```json\n" + _draft_json("Large pneumothorax on the right.") + "\n```"
    transport, _ = _responding(content=fenced)
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="",
                                 critical_flags=CRITICAL_FLAGS, ehr_context={})
    assert isinstance(out, LLMDraft)
    assert out.impression_text == "Large pneumothorax on the right."


async def test_prompt_demands_an_action_when_a_critical_flag_is_present(monkeypatch):
    """The parser refuses an empty recommendation list beside a critical flag, so the prompt has
    to ask for one; otherwise the LLM path fails exactly where it is most valuable (#103)."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    content = '{"impressionText": "Pneumothorax is present.", "recommendations": ["Chest tube."]}'
    transport, seen = _responding(200, content=content)
    _install(monkeypatch, transport)
    await draft_impression(
        conclusion="", finding_labels="pneumothorax", critical_flags=CRITICAL_FLAGS, ehr_context={},
    )
    sent = seen[0]["body"]["messages"][1]["content"]
    assert "at least one concrete action" in sent


async def test_prompt_stays_quiet_about_actions_on_a_normal_study(monkeypatch):
    _clear(monkeypatch)
    _configure(monkeypatch)
    content = '{"impressionText": "No acute findings.", "recommendations": []}'
    transport, seen = _responding(200, content=content)
    _install(monkeypatch, transport)
    await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    sent = seen[0]["body"]["messages"][1]["content"]
    assert "at least one concrete action" not in sent


def _responding_sequence(contents):
    """A transport that answers each POST with the next content in `contents`."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_json.loads(request.content or b"{}"))
        body = contents[min(len(seen) - 1, len(contents) - 1)]
        return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})

    return httpx.MockTransport(handler), seen


async def test_unparseable_reply_is_retried_once_and_accepted(monkeypatch):
    """The model answered, it just answered badly (#103). Sampling is not deterministic, so one
    retry recovers the impression instead of spending the study on the template."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    good = '{"impressionText": "No acute findings.", "recommendations": ["Routine follow-up."]}'
    transport, seen = _responding_sequence(['{"impressionText": "broken", "recomm',  good])
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out == LLMDraft(impression_text="No acute findings.", recommendations=["Routine follow-up."])
    assert len(seen) == 2, "should have asked exactly twice"


async def test_two_bad_replies_fall_back_to_the_template(monkeypatch, caplog):
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, seen = _responding_sequence(["still not json"])
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out is None
    assert len(seen) == 2, "one retry, then give up"
    assert "fell back to the deterministic template" in caplog.text


async def test_transport_failure_is_not_retried(monkeypatch):
    """A timeout already cost the full budget in front of a radiologist's read; do not pay twice."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    calls = {"n": 0}

    def timing_out(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TimeoutException("timed out", request=request)

    _install(monkeypatch, httpx.MockTransport(timing_out))
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out is None
    assert calls["n"] == 1


# --- unfilled template placeholders in the drafted prose --------------------------------
# The two impression texts below are VERBATIM from a live local-model run (llama3.2:3b on the
# demo host, 2026-08-20): 2 of 30 normal-case drafts came back carrying one. They matter more
# than an invented string would, because the pre-sign path writes impressionText straight into
# the chart as a preliminary DiagnosticReport. The prompts behind them were synthetic textbook
# phrases with an empty ehr_context -- no cohort text reached the model, so this is model
# output about nobody.

async def test_observed_patient_name_placeholder_is_rejected(monkeypatch):
    _clear(monkeypatch)
    _configure(monkeypatch)
    leaked = ("The radiologic examination of [patient name] revealed no evidence of acute "
              "cardiopulmonary pathology. The study is normal.")
    content = _json.dumps({"impressionText": leaked, "recommendations": []})
    transport, _ = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out is None


async def test_observed_insert_context_placeholder_is_rejected(monkeypatch):
    _clear(monkeypatch)
    _configure(monkeypatch)
    leaked = ("The patient presented with [insert context], and no acute cardiopulmonary "
              "abnormalities were identified on imaging.")
    content = _json.dumps({"impressionText": leaked, "recommendations": []})
    transport, _ = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out is None


async def test_placeholder_in_a_recommendation_is_rejected(monkeypatch):
    """Same defect one field over -- and the recommendations are what a reader acts on."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    content = _json.dumps({
        "impressionText": "Findings consistent with a right-sided pneumothorax.",
        "recommendations": ["Contact [ordering physician] urgently."],
    })
    transport, _ = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(
        conclusion="", finding_labels="", critical_flags=CRITICAL_FLAGS, ehr_context={})
    assert out is None


async def test_a_placeholder_draft_is_retried_once_and_a_clean_retry_is_accepted(monkeypatch):
    """The reject rides the existing #103 retry ladder, which is what makes it cheap: sampling is
    not deterministic, so the second ask usually comes back filled in."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    leaked = _json.dumps({"impressionText": "Study of [patient name] is normal.",
                          "recommendations": []})
    clean = _json.dumps({"impressionText": "No acute cardiopulmonary abnormality.",
                         "recommendations": []})
    transport, seen = _responding_sequence([leaked, clean])
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out == LLMDraft(impression_text="No acute cardiopulmonary abnormality.", recommendations=[])
    assert len(seen) == 2


async def test_curly_braces_in_prose_are_still_accepted(monkeypatch):
    """Pinned alongside the guard: {sic} is legitimate prose, so the check stays square-only."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    content = _json.dumps({"impressionText": "Density measures 40 HU {sic}.",
                           "recommendations": ["Correlate."]})
    transport, _ = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out == LLMDraft(impression_text="Density measures 40 HU {sic}.", recommendations=["Correlate."])


async def test_comparison_operators_in_prose_are_not_placeholders(monkeypatch):
    """The other reason the check is square-only: radiology writes comparison operators."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    prose = "Subcentimetre nodule <5 mm; cardiothoracic ratio >0.5."
    content = _json.dumps({"impressionText": prose, "recommendations": []})
    transport, _ = _responding(200, content=content)
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels="", critical_flags=[], ehr_context={})
    assert out == LLMDraft(impression_text=prose, recommendations=[])


# --- confirmed NON-CRITICAL findings (#76 rehearsal, 2026-08-20) ---------------------------
#
# effusion-detect is the first non-critical producer. The prompt named only CRITICAL findings as
# authoritative, so a study whose one COMPLETE finding was a pleural effusion arrived as
# "critical findings: none" with no report conclusion, and the model wrote a NORMAL impression --
# which the pre-sign path then wrote to the chart. Reproduced live on 5 of 5 effusion-only
# studies, one at p=0.77.

EFFUSION_LABEL = ("Pleural effusion (screening p=0.65, raw 0.376 vs op 0.103); "
                  "screening signal only, not a read")


def test_finding_terms_keeps_only_the_pathology_head():
    """Matching the whole label would reject every draft: no prose repeats the calibration tail."""
    assert llm_draft._finding_terms([EFFUSION_LABEL]) == ["pleural effusion"]
    assert llm_draft._finding_terms(EFFUSION_LABEL) == ["pleural effusion"]
    assert llm_draft._finding_terms(["Pneumothorax (screening p=0.50)", EFFUSION_LABEL]) == [
        "pneumothorax", "pleural effusion"]
    assert llm_draft._finding_terms([]) == []
    assert llm_draft._finding_terms(["", None]) == []


def test_duplicate_finding_labels_dedupe():
    assert llm_draft._finding_terms([EFFUSION_LABEL, EFFUSION_LABEL]) == ["pleural effusion"]


async def test_normal_prose_next_to_a_confirmed_effusion_is_refused(monkeypatch):
    """THE regression: no critical flag, a confirmed effusion, and the model says the study is
    normal. Must degrade to None so the deterministic recital runs instead."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, _ = _responding(
        content=_draft_json("No acute cardiopulmonary abnormality."))
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels=[EFFUSION_LABEL],
                                 critical_flags=[], ehr_context={})
    assert out is None


async def test_negating_a_confirmed_non_critical_finding_is_refused(monkeypatch):
    """Naming the finding only to negate it, the non-critical twin of the critical-flag case."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, _ = _responding(content=_draft_json(
        "No pleural effusion or pneumothorax is identified."))
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels=[EFFUSION_LABEL],
                                 critical_flags=[], ehr_context={})
    assert out is None


async def test_prose_asserting_the_confirmed_effusion_is_accepted(monkeypatch):
    """The fix must not reject a correct draft: recommendations may still be empty for a
    non-critical finding (#103), so only the assertion matters here."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, _ = _responding(content=_draft_json(
        "Small left pleural effusion. Clinical correlation recommended."))
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels=[EFFUSION_LABEL],
                                 critical_flags=[], ehr_context={})
    assert out == LLMDraft(
        impression_text="Small left pleural effusion. Clinical correlation recommended.",
        recommendations=["clinical correlation"])


async def test_every_confirmed_finding_must_be_asserted_not_just_one(monkeypatch):
    """A study that fires both heads: prose naming only the pneumothorax is silent about the
    effusion. That is the shape of the 18 stale drafts found on the host."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    labels = ["Pneumothorax (screening p=0.53)", EFFUSION_LABEL]
    transport, _ = _responding(content=_draft_json(
        "Pneumothorax is present. No other acute cardiopulmonary abnormalities are identified."))
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="", finding_labels=labels,
                                 critical_flags=[], ehr_context={})
    assert out is None

    transport2, _ = _responding(content=_draft_json(
        "Pneumothorax is present with an accompanying pleural effusion."))
    _install(monkeypatch, transport2)
    out2 = await draft_impression(conclusion="", finding_labels=labels,
                                  critical_flags=[], ehr_context={})
    assert out2 is not None


async def test_a_study_with_no_confirmed_finding_may_still_read_normal(monkeypatch):
    """The check must not fire when nothing was confirmed: most of a screening cohort is normal,
    and post-sign this path runs over reports with no AI finding at all (#103)."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, _ = _responding(
        content=_draft_json("No acute cardiopulmonary abnormality.", recommendations=[]))
    _install(monkeypatch, transport)
    out = await draft_impression(conclusion="Lungs are clear.", finding_labels=[],
                                 critical_flags=[], ehr_context={})
    assert out is not None
    assert out.impression_text == "No acute cardiopulmonary abnormality."


async def test_the_prompt_names_confirmed_findings_as_authoritative(monkeypatch):
    """The prompt is the wire contract with the model: if it stops naming the confirmed findings,
    the model goes back to inferring normality and these tests still pass on canned replies."""
    _clear(monkeypatch)
    _configure(monkeypatch)
    transport, seen = _responding(content=_draft_json("Small left pleural effusion."))
    _install(monkeypatch, transport)
    await draft_impression(conclusion="", finding_labels=[EFFUSION_LABEL],
                           critical_flags=[], ehr_context={})
    prompt = seen[0]["body"]["messages"][1]["content"]
    assert "Confirmed AI screening findings (authoritative, do not contradict): pleural effusion" in prompt
    assert "MUST name each of them as present" in prompt
    assert "do NOT write that the study is normal" in prompt.replace("Do NOT", "do NOT")
