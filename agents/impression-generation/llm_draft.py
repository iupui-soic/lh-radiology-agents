"""LLM-authored impression prose (#77). Owner: Chaitra.

Prose ONLY: `impressionText` + `recommendations`. `criticalFlags` (and `structuredFindings`,
derived from it) stay a deterministic derivation from handler.py's keyword/negation scan --
never influenced by this module. That split is #78's whole point: criticality pages physicians
via CritCom and must stay auditable and testable without a model, so this file only ever
CONSUMES an already-computed `critical_flags` list -- it never derives or overrides one.

Config-gated + best-effort: unset IMPRESSION_LLM_BASE_URL/MODEL -> feature off, handler.py's
deterministic template is the default AND the fallback. ANY failure here -- network, timeout,
malformed output, a half-set misconfiguration, or prose that contradicts the confirmed critical
flags -- returns None rather than raising. The read must never be stranded on a hosting choice
the PI has not made yet.

Hosting-agnostic by design: speaks the OpenAI chat-completions HTTP shape that vLLM, Ollama, and
most cloud providers all implement, so the PI's hosting decision (local open-weights vs. a
DUA-compliant cloud service) becomes a config value -- base URL + model name -- not a code branch.

Never logs prompt or response CONTENT -- only exception class/message text and HTTP status
codes, matching fhir_client.py's "host only, never the clinical text" logging discipline. The
malformed-output ValueErrors raised below carry deliberately content-free reason strings (which
check failed, e.g. "empty impressionText"), so logging their message is safe and is what makes a
malformed-vs-empty-vs-flag-mismatch rejection distinguishable in production logs.
"""
from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from radagent_common.negation import find_asserted_terms

_log = logging.getLogger(__name__)
_warned_misconfigured = False

_DEFAULT_TIMEOUT_SECONDS = 12.0

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_plaintext_remote(base_url: str) -> bool:
    """Plaintext `http` to a non-loopback host: the transport that exposes anything sent on it."""
    parsed = urlparse(base_url)
    return parsed.scheme != "https" and (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS


def _egress_transport_is_secure(base_url: str) -> bool:
    """May we POST the report conclusion + EHR context to this LLM base URL (#77)?

    The chat-completions request body carries clinical text -- the report conclusion and the EHR
    problems/labs summary -- to whatever IMPRESSION_LLM_BASE_URL names. Over plaintext `http` to a
    remote host that (plus any Authorization key) is exposed on the wire, and for MIMIC content
    that is a DUA problem. So mirror fhir_client's write guard exactly: refuse plaintext-remote
    UNLESS the target is loopback (local model / unit tests) or the deployment has accepted the risk
    on a trusted internal network via IMPRESSION_LLM_ALLOW_INSECURE. `https` is always fine. The
    truthy set matches FHIR2_ALLOW_INSECURE_WRITE so an operator's opt-in behaves identically here.
    """
    if not _is_plaintext_remote(base_url):
        return True  # https, or a loopback host
    return os.environ.get("IMPRESSION_LLM_ALLOW_INSECURE", "").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class LLMDraft:
    impression_text: str
    recommendations: list[str]


def _timeout_seconds() -> float:
    raw = os.environ.get("IMPRESSION_LLM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        _log.warning(
            "IMPRESSION_LLM_TIMEOUT_SECONDS=%r is not a number; using the %.0fs default",
            raw, _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS


def _configured() -> tuple[str, str, str, float] | None:
    """(base_url, model, api_key, timeout), or None to fall back to the deterministic template.

    Unset (neither var) -> off, silently -- that's the default. Half-set -> a misconfiguration:
    warned ONCE (mirrors radagent_common.tracing's _warned_missing pattern) then treated as off,
    never raised -- this sits on the read path and must never strand it on a config typo."""
    base_url = os.environ.get("IMPRESSION_LLM_BASE_URL", "").strip()
    model = os.environ.get("IMPRESSION_LLM_MODEL", "").strip()
    if not base_url and not model:
        return None
    if bool(base_url) != bool(model):
        global _warned_misconfigured
        if not _warned_misconfigured:
            _warned_misconfigured = True
            _log.warning(
                "IMPRESSION_LLM_BASE_URL and IMPRESSION_LLM_MODEL must both be set; "
                "falling back to the deterministic template."
            )
        return None
    api_key = os.environ.get("IMPRESSION_LLM_API_KEY", "").strip()
    return base_url, model, api_key, _timeout_seconds()


def _labels_as_text(finding_labels: str | list[str] | tuple[str, ...] | set[str]) -> str:
    """`finding_labels` is a lowercased str today; #78 (in flight) changes its producer to return
    list[str]. This handler-side value is never touched by this module's caller (handler.py's
    scan block is #78's territory), so normalize defensively here instead of assuming either
    shape survives untouched across merge order."""
    if isinstance(finding_labels, str):
        return finding_labels
    return " ".join(str(v) for v in finding_labels if v)


def _summarize_ehr_context(ehr_context: dict) -> str:
    """An explicit allowlist -- never a blanket json.dumps(ehr_context). `contrastFlags` and
    `medicationFlags` are additionalProperties:true in contracts/skills/ehr.schema.json, so a raw
    dump would silently forward whatever a future EHR Assistant change adds there to an external
    endpoint, unreviewed.

    CODED entries only, for the same reason: fhir_client's projector (`_first_coding_value`)
    falls back to the CodeableConcept's free `text` for the display when no coding carries a
    code, so an UNCODED entry's display can be clinician-typed narrative -- which must not ride
    to an external endpoint ("a code is a code", the `_order_reason_codes` rule). When a code IS
    present the display is the coding's own terminology display, so it may travel."""
    problems = ", ".join(
        p.get("display") or p["code"]
        for p in ehr_context.get("activeProblems", []) if p and p.get("code")
    )
    labs = ", ".join(
        f"{lab.get('display') or lab['code']}: {lab.get('value', '')} {lab.get('unit', '')}".strip()
        for lab in ehr_context.get("relevantLabs", []) if lab and lab.get("code")
    )
    parts = []
    if problems:
        parts.append(f"Active problems: {problems}.")
    if labs:
        parts.append(f"Relevant labs: {labs}.")
    return " ".join(parts)


_SYSTEM_PROMPT = (
    "You are drafting the prose portion of a radiology impression. You will be given the "
    "confirmed critical findings -- already decided by a separate deterministic process -- plus "
    "supporting context. Write natural clinical prose that is consistent with, and does not "
    "contradict or invent findings beyond, the confirmed critical findings and the conclusion "
    "text given. Respond with ONLY a JSON object of the shape "
    '{"impressionText": "...", "recommendations": ["...", "..."]} -- no markdown, no commentary. '
    # Said explicitly because the omission cost us the whole feature (#103): a normal study
    # warrants no recommendation, the model correctly returned [], and the parser threw the draft
    # away as malformed. Most of a screening cohort is normal, so most drafts were discarded.
    "recommendations may be an empty array when none are warranted; do not invent one. "
    # The mirror of that, learned in the same session: told only that empty was acceptable, the
    # model returned [] next to a confirmed pneumothorax, which the parser refuses (prose naming a
    # critical finding and advising nothing reads as reassurance) -- so the LLM path failed exactly
    # where it is most valuable. Ask for the action wherever one is load-bearing.
    "When confirmed critical findings are listed, at least one recommendation is REQUIRED. "
    # And the JSON itself has to survive json.loads: raw newlines and unescaped quotes inside the
    # string values were the other live failure ("Expecting ',' delimiter").
    "Both fields must be valid JSON strings: escape any double quote, and use no literal newlines "
    "inside a string."
)


def _build_prompt(*, conclusion: str, labels_text: str, critical_flags: list[dict], ehr_summary: str) -> str:
    flag_labels = ", ".join(f["label"] for f in critical_flags if f.get("label")) or "none"
    lines = [
        f"Confirmed critical findings (authoritative, do not contradict): {flag_labels}",
        f"Report conclusion: {conclusion or '(none available)'}",
        f"AI finding labels: {labels_text or '(none)'}",
    ]
    if ehr_summary:
        lines.append(ehr_summary)
    if critical_flags:
        # Repeated per-request, not left to the system prompt alone: this is the case the parser
        # refuses outright, so it is worth spending a line on it where the findings are named.
        lines.append(
            "This study has confirmed critical findings, so recommendations must contain at "
            "least one concrete action."
        )
    return "\n".join(lines)


async def _chat_completion(base_url: str, model: str, api_key: str, timeout: float, prompt: str) -> str:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _first_json_object(text: str) -> str:
    """The first balanced {...} run in `text`, or `text` unchanged when there is none.

    Backends decorate the object despite `response_format` (#103): a ```json fence, a leading
    "Here is the impression:", a trailing sentence after the closing brace. `json.loads` on the
    whole string then dies with "Extra data" and a perfectly good draft is discarded. Scanning to
    the matching brace keeps the object and ignores the decoration. String-aware, so a brace
    inside the prose (a measurement, a quoted snippet) cannot unbalance the scan.
    """
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text


def _parse_draft(content: str, critical_flags: list[dict]) -> LLMDraft:
    """Raises on anything unusable; draft_impression() turns every raise into a None."""
    parsed = json.loads(_first_json_object(content.strip()))
    impression_text = parsed["impressionText"]
    recommendations = parsed["recommendations"]
    if not isinstance(impression_text, str) or not impression_text.strip():
        raise ValueError("empty impressionText")
    if not isinstance(recommendations, list) or not all(
        isinstance(r, str) and r.strip() for r in recommendations
    ):
        raise ValueError("malformed recommendations")
    # An EMPTY list is legitimate and must stay that way (#103). A normal study warrants no
    # recommendation, the model says so by returning [], and requiring one here threw away the
    # draft for most of a screening cohort -- silently, since the fallback is by design. Where a
    # recommendation IS load-bearing, next to a confirmed critical flag, an empty list is still
    # refused: prose that names a critical finding and then advises nothing reads as reassurance.
    if critical_flags and not recommendations:
        raise ValueError("no recommendations alongside a confirmed critical flag")
    # Criticality is never the LLM's call (#78 owns that derivation) -- so the prose must ASSERT
    # every confirmed critical flag, checked with the SAME negation-aware, word-boundary matcher
    # as the #78 scanners. Two failure shapes a bare substring test waves through, both rejected
    # here: "No pneumothorax is identified." names the flag only to NEGATE it -- exactly the
    # reassuring-prose-next-to-a-real-finding case this check exists for -- and prose naming one
    # of two confirmed flags is silent about the other. A conservative reject falls back to the
    # deterministic template (same bias as #78: an over-flag is tolerated, a silent miss is not).
    # Verbatim label match stays deliberate: prose that says "PTX" for a "pneumothorax" flag is
    # rejected -- a false reject costs deterministic prose, a false accept could read as
    # reassuring next to a real finding.
    flag_labels = [f["label"].lower() for f in critical_flags if f.get("label")]
    if flag_labels:
        asserted = set(find_asserted_terms(impression_text, flag_labels))
        missing = [label for label in flag_labels if label not in asserted]
        if missing:
            raise ValueError(
                f"prose does not assert {len(missing)} of {len(flag_labels)} confirmed critical flag(s)")
    return LLMDraft(
        impression_text=impression_text.strip(),
        recommendations=[r.strip() for r in recommendations],
    )


_ATTEMPTS = 0
_FALLBACKS = 0


def _note_fallback() -> None:
    """Count how often the LLM draft is discarded, and say so (#103).

    The fallback is safe by design, which is exactly why it needs a number: the feature can be
    configured, reach the model, and still be inert on nearly every study with nothing in the
    deployment looking wrong. #103 was 11 of 12 studies before anyone noticed. Counts only, no
    model output and no report text.
    """
    global _FALLBACKS
    _FALLBACKS += 1
    _log.warning(
        "impression LLM draft fell back to the deterministic template (%d of %d attempts since start)",
        _FALLBACKS, _ATTEMPTS,
    )


async def draft_impression(
    *, conclusion: str, finding_labels: str | list[str], critical_flags: list[dict], ehr_context: dict,
) -> LLMDraft | None:
    """The sole entry point. Returns None -- never raises -- when the LLM path is unset,
    misconfigured, or fails/produces something unusable for any reason; handler.py falls back to
    the deterministic template on None, exactly as it already does for a fhir2 fetch failure."""
    config = _configured()
    if config is None:
        return None
    base_url, model, api_key, timeout = config
    global _ATTEMPTS
    _ATTEMPTS += 1

    # EVERYTHING from here sits under the never-raise ladder, the transport guard and prompt
    # build included: urlparse raises ValueError on a malformed base URL (e.g. a junk port), and
    # an unexpectedly-shaped ehr_context/critical_flags must degrade to the template exactly like
    # a network failure would -- this module's contract is None, never a raise, and relying on
    # handler.py's backstop to honour it would make that backstop load-bearing.
    try:
        # No clinical text (report conclusion + EHR context) over plaintext HTTP to a remote host
        # (#77). Best-effort like every other failure here: skip to the deterministic template
        # rather than raise, so a transport misconfiguration never strands the read -- but the
        # PHI never leaves.
        if not _egress_transport_is_secure(base_url):
            _log.warning(
                "impression LLM draft skipped: refusing to POST report + EHR context over plaintext "
                "HTTP to non-loopback host %s; use an https base URL, or set "
                "IMPRESSION_LLM_ALLOW_INSECURE=1 for a trusted internal network",
                urlparse(base_url).hostname,
            )
            _note_fallback()
            return None
        if _is_plaintext_remote(base_url):
            # Proceeding only because of the insecure opt-in; leave an audit trail of what rides
            # this hop in cleartext. Host only -- never the report or EHR content.
            _log.warning(
                "impression LLM draft proceeding over PLAINTEXT http to %s under "
                "IMPRESSION_LLM_ALLOW_INSECURE: report conclusion, EHR context, and the "
                "Authorization key (when set) are in cleartext on this hop",
                urlparse(base_url).hostname,
            )

        prompt = _build_prompt(
            conclusion=conclusion,
            labels_text=_labels_as_text(finding_labels),
            critical_flags=critical_flags,
            ehr_summary=_summarize_ehr_context(ehr_context),
        )
        content = await _chat_completion(base_url, model, api_key, timeout, prompt)
        return _parse_draft(content, critical_flags)
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as e:
        _log.warning("impression LLM draft skipped: unusable IMPRESSION_LLM_BASE_URL (%s)", e.__class__.__name__)
    except httpx.HTTPStatusError as e:
        _log.warning("impression LLM draft failed: HTTP %s", e.response.status_code)
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        _log.warning("impression LLM draft failed: %s", e.__class__.__name__)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        # e's message is safe to log: JSONDecodeError describes a syntax position, KeyError names
        # a missing key, and the ValueErrors raised in _parse_draft are hand-written, content-free
        # reason strings -- none of them carry model output or report text.
        _log.warning("impression LLM draft malformed: %s: %s", e.__class__.__name__, e)
    except Exception as e:  # noqa: BLE001 - never-raises backstop; this path is advisory only
        _log.warning("impression LLM draft unexpected failure: %s", e.__class__.__name__)
    _note_fallback()
    return None
