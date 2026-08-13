"""Optional LLM prose for the physician-facing notification (CritCom protocol format).

The deterministic layer stays authoritative for everything that pages: WHO is notified, the ACR
category, the ack deadline, and escalation are classifier/rule decisions (the #78 thesis -- the
safety trigger must be auditable without a model). This module upgrades exactly ONE string: the
Communication payload text a physician reads. The category is PRE-DECIDED and passed in; the
model is told not to re-classify.

Fail-safe by construction: flag off (the default), no key, timeout, transport error, HTTP error,
or an empty/malformed reply all return None, and the caller falls back to the deterministic
one-line summary. Paging never waits on and never fails because of the LLM -- the composer gets
one bounded attempt (COMMS_LLM_TIMEOUT_SECONDS, default 5) inside a dispatch that was going to
send either way.

Lean-reference prompt (golden rule 2 applied to an EXTERNAL model): the prompt carries the ACR
category, the finding label, and the ack window -- never the report narrative, never patient or
order identifiers. Widening it to the narrative would send PHI to an external API and needs a
#30-style review first.

TWO BACKENDS, one behaviour. Setting COMMS_LLM_BASE_URL points the composer at any
OpenAI-compatible /chat/completions endpoint (a locally hosted open-weights model, vLLM, ollama);
leaving it unset keeps the original Gemini path byte-for-byte. The #77 hosting decision --
local open-weights on the demo host, no third-party cloud -- could not be honoured while this
module could only reach generativelanguage.googleapis.com, and the finding label it sends is
cohort-derived, so "off" was the only DUA-safe setting. Now "local" is a setting too.

Keys come from the operator's environment only (compose passes ${...:-} through), never a file.
GEMINI_API_KEY rides the x-goog-api-key header and COMMS_LLM_API_KEY (optional -- a local model
usually wants none) rides Authorization, so no URL or log line ever carries either.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

import httpx

_log = logging.getLogger("agents.communications.composer")

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_DEFAULT_MODEL = "gemini-2.5-flash-lite"
# Byte-for-byte the family truthy set (FHIR2_ALLOW_INSECURE_WRITE / EHR_INBOX_WRITE_ENABLED /
# IMPRESSION_LLM_ALLOW_INSECURE): two switches with different token sets is an operator trap
# (!73 review, item 3) -- "on" deliberately does NOT enable this one either.
_TRUTHY = {"1", "true", "yes"}

# On the GEMINI path COMMS_LLM_MODEL rides into the URL PATH; a stray "/" or "?" in a typo'd value
# would rewrite the endpoint (same https host, wrong route). Constrain to plain model-name
# characters and fall back on anything else -- a bad model name must degrade like any other
# failure, not steer the URL. The OpenAI-compatible path deliberately does NOT apply this: there
# the model is a JSON body field and cannot steer anything, and the real names carry characters
# this pattern rejects ("qwen2.5:7b" -- a colon), so enforcing it there would silently fall back
# on exactly the models this backend exists to reach.
_MODEL_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")

# Same host set and same reasoning as impression-generation's llm_draft (#77): from a CONTAINER
# nothing on the docker host is loopback, so a local model reached as host.docker.internal or by
# compose service name is plaintext-remote and needs the explicit opt-in below.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Her CritCom protocol responder, adapted for a pre-decided category: the deterministic classifier
# already chose Cat1/Cat2 and the window, so the model composes the message AROUND that decision
# instead of making it. "Never ask questions" replaces the interactive prompt's clarifying-question
# rule -- there is no conversation here, only one message that must stand on its own.
_PROMPT = """You are CritCom, the radiology critical-results communication specialist.

The deterministic pipeline has ALREADY classified this finding and opened the acknowledgment
clock. Do not re-classify, do not change the category or the window, do not invent clinical
detail beyond the finding label given. You have no patient identifiers and must not fabricate
any. Never ask questions. Output only the message, in exactly this format:

**Critical Results Communication Protocol**

**Finding:** <one-sentence clinical restatement of the finding label>
**ACR Category:** {category} — <one-line reasoning for why this category fits the finding>

**Action plan:**
- The ordering physician has been notified via pager and EHR inbox.
- Acknowledge within {ack_minutes} minutes; an unacknowledged result escalates to the on-call
  provider on a shorter window.

Be concise, clinical, and decisive.

Finding label: {finding}
ACR category (pre-decided): {category}
Acknowledgment window: {ack_minutes} minutes"""


def _enabled() -> bool:
    return os.environ.get("COMMS_LLM_COMPOSER", "").strip().lower() in _TRUTHY


def _contradicts_category(text: str, category: str) -> bool:
    """Does the composed prose name a DIFFERENT ACR category, or fail to name the decided one?

    The category is paging semantics -- it decided the window and the escalation ladder -- so a
    message whose visible text says Cat3 while the clock runs on Cat1 misinforms the physician
    about the urgency of their own deadline. Same precedent as the impression module's
    flag-consistency check (#77): the deterministic layer decided; prose that contradicts the
    decision is rejected and the deterministic fallback goes out instead."""
    lowered = text.lower()
    named = {c for c in ("cat1", "cat2", "cat3") if c in lowered}
    return category.lower() not in named or bool(named - {category.lower()})


def _timeout_seconds() -> float:
    try:
        return float(os.environ.get("COMMS_LLM_TIMEOUT_SECONDS", "5"))
    except ValueError:
        return 5.0


def _egress_transport_is_secure(base_url: str) -> bool:
    """May we POST the finding label to this base URL?

    Mirrors llm_draft's guard (#77) and fhir_client's write guard before it, deliberately as a
    third local copy rather than a shared helper -- same precedent as the truthy set above. What
    goes out here is narrower than the impression module's (a finding label, no narrative, no
    identifiers), but a cohort-derived label on the wire is still a DUA exposure. `https` is fine,
    so is a genuine loopback target; plaintext to anything else needs the operator to accept the
    risk, which on a single demo host (docker bridge, never leaves the machine) is a real answer
    rather than a rubber stamp."""
    parsed = urlparse(base_url)
    if parsed.scheme == "https" or (parsed.hostname or "").lower() in _LOOPBACK_HOSTS:
        return True
    return os.environ.get("COMMS_LLM_ALLOW_INSECURE", "").strip().lower() in _TRUTHY


def _openai_compatible_target() -> tuple[str, str, str] | None:
    """(base_url, model, api_key) for the OpenAI-compatible backend, or None to use Gemini.

    Base URL unset -> Gemini, silently: that is the default and the pre-existing behaviour.
    Base URL set without a model is a misconfiguration, not a request to guess -- there is no
    sensible default model for an arbitrary endpoint, so warn and fall back (same half-set rule
    as IMPRESSION_LLM_BASE_URL/MODEL)."""
    base_url = os.environ.get("COMMS_LLM_BASE_URL", "").strip()
    if not base_url:
        return None
    model = os.environ.get("COMMS_LLM_MODEL", "").strip()
    if not model:
        _log.warning("composer fallback: COMMS_LLM_BASE_URL is set but COMMS_LLM_MODEL is not")
        return None
    if not _egress_transport_is_secure(base_url):
        # Host only -- the path may name a deployment, and the refusal must not echo it back.
        _log.warning("composer fallback: refusing plaintext egress to remote host %r; set "
                     "COMMS_LLM_ALLOW_INSECURE to accept the risk", urlparse(base_url).hostname)
        return None
    return base_url, model, os.environ.get("COMMS_LLM_API_KEY", "").strip()


async def _compose_openai_compatible(target: tuple[str, str, str], prompt: str) -> str | None:
    base_url, model, api_key = target
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=_timeout_seconds()) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        body = resp.json()
    return body["choices"][0]["message"]["content"]


async def _compose_gemini(prompt: str) -> str | None:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        # Flag on with no key is a config gap, but the dispatch must still page: fall back loudly.
        _log.warning("COMMS_LLM_COMPOSER is on but GEMINI_API_KEY is unset; using fallback text")
        return None
    model = os.environ.get("COMMS_LLM_MODEL", "").strip() or _DEFAULT_MODEL
    if not _MODEL_TOKEN.fullmatch(model):
        _log.warning("composer fallback: COMMS_LLM_MODEL is not a plain model name; ignoring it")
        return None
    async with httpx.AsyncClient(timeout=_timeout_seconds()) as client:
        resp = await client.post(
            _GEMINI_URL.format(model=model),
            headers={"x-goog-api-key": key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )
        resp.raise_for_status()
        body = resp.json()
    return body["candidates"][0]["content"]["parts"][0]["text"]


async def compose_notification(*, acr_category: str, finding: str,
                               ack_minutes: int | None) -> str | None:
    """The physician-facing notification text, or None meaning "use the deterministic fallback".

    None is the answer to EVERY failure mode -- this function must never raise into a dispatch.
    The backend choice changes only WHERE the prose comes from: the flag, the lean-reference
    prompt, and every acceptance check below are identical on both paths."""
    if not _enabled():
        return None
    prompt = _PROMPT.format(category=acr_category, finding=finding,
                            ack_minutes=ack_minutes if ack_minutes is not None else 60)
    try:
        target = _openai_compatible_target()
        if target is not None:
            composed = await _compose_openai_compatible(target, prompt)
        elif os.environ.get("COMMS_LLM_BASE_URL", "").strip():
            return None  # base URL set but unusable: _openai_compatible_target already warned.
        else:
            composed = await _compose_gemini(prompt)
        if composed is None:
            return None
        text = composed.strip()
        if not text:
            return None
        if _contradicts_category(text, acr_category):
            # Content-free by design: log the decided category (a classifier code, not clinical
            # text), never the model's prose.
            _log.warning("composer fallback: prose contradicts the decided ACR category %s",
                         acr_category)
            return None
        return text
    except Exception as exc:  # noqa: BLE001 -- any failure means "fall back", never "fail the page"
        # Exception type + message only; keys live in headers, never in a URL or str(exc).
        _log.warning("composer fallback: %s: %s", type(exc).__name__, exc)
        return None
