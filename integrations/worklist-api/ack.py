"""The explicit ack surface (#79): the link a referring physician taps to close the loop.

The chart notification (the comms agent's ehr-inbox write) carries
`{CRITCOM_ACK_BASE_URL}/ack/{task_id}?sig={hmac}`. This module serves that route. Three checks,
in a deliberate order:

1. **Signature first**, before any auth challenge: a forged or enumerated task id is a 403 and
   never even gets a password prompt, so credentials are never solicited by an illegitimate link
   (`radagent_common.ack_link` holds the signing rationale).
2. **Identity is the human, not the link.** Possession of a URL is not "Dr X acknowledged": the
   caller's identity is resolved through `/ws/rest/v1/session`, the same identity OpenMRS
   itself would report, by whichever proof the request already carries, in order:
   a. the caller's EXISTING OpenMRS session (`JSESSIONID` cookie): the physician is reading the
      chart in an authenticated browser, so the ack is ONE CLICK with no second login. The
      browser only sends that cookie when the ack URL rides under the cookie's `/openmrs` path,
      i.e. a deployment fronting worklist-api at `/openmrs/ack/...` on the same host (a
      reverse-proxy route; the dev compose ports don't do this) and pointing
      CRITCOM_ACK_BASE_URL there. Anywhere else the cookie is absent and nothing changes.
   b. HTTP Basic (the fallback: a link opened outside the EHR, e.g. from a page), passed
      through to OpenMRS. No new accounts, no password handling beyond that pass-through.
   A cookie that no longer resolves to an authenticated session falls through to the Basic
   challenge rather than failing: stale sessions are routine, not suspicious.
3. **The acknowledgement is a deliberate act, so it is a POST.** `GET /ack/{id}` renders a
   confirmation page and changes nothing; the button on it POSTs. Once identity can come from
   an ambient cookie (a above), a GET that acknowledged would be reachable without any human
   act at all -- browsers prefetch on hover and refetch on tab restore, either of which would
   close the escalation clock with a physician's name on it. Under Basic alone that was
   impossible, because a prefetch carries no credentials. The physician still makes exactly
   one click; it is just a click on a button that says what it will attest.
4. **The ledger Task closes with WHO on it** (`complete_ack_task`: status COMPLETED + a note
   naming the acknowledger). `comms.checkAck` then reports COMPLETED and the orchestrator's
   escalation never fires, which is the run-book's "acknowledged in time" arc.
5. **WHO and WHEN are checked, and stated before the click** (#127, #128). The endpoint used to
   trust both. It recorded whoever authenticated, without comparing them to the Task's addressee,
   and it accepted an ack after the window had lapsed, overwriting the FAILED status the
   escalation had set. Both were reproduced live on 2026-08-23, one after the other, by a
   physician doing an ordinary thing with a real link.
   * **Non-addressee**: permitted, because covering physicians, night float and handover are
     normal and refusing them would push people to acknowledge from a colleague's logged-in
     browser. Recorded as on-behalf-of, naming BOTH parties, and the confirmation page says so
     before the button is pressed. Compared on the PROVIDER uuid (`session.currentProvider`), and
     an unresolvable provider is never treated as a mismatch, see `Caller`. The addressee is
     NAMED by a best-effort `/provider/<uuid>` read made as the caller, because the live
     `Task.owner` carries no display and a uuid is not something a covering physician can check
     (#130). See `_resolve_addressee_name`.
   * **Late**: permitted, because a physician who finally reads the notification has still
     received it and that is worth recording. But an escalation that already fired stands: the
     Task keeps its FAILED status, the note records the miss and the deadline, and the on-call
     Task is deliberately left open for on-call to close themselves. The page no longer claims
     the escalation clock is closed when it is not, which is what made the live reproduction
     misleading rather than merely wrong.

Still ONE tap on a paged phone: the link opens the confirmation page, and the button on it is
the tap. What the split buys is that everything which is not a tap (a prefetch, a tab restore,
a link-previewing mail client, a security scanner) now lands on a page instead of writing an
attestation. Re-tapping is idempotent either way: an already-acknowledged Task renders the
already-acknowledged page and is never re-written, so there is no duplicated loop.

Kept as a sibling module (the `assignment.py`/`store.py` pattern) so `main.py` stays the thin
app factory. Inert until a deployment sets CRITCOM_ACK_HMAC_SECRET: without it every signature
verification fails closed and no links are ever minted on the producer side.
"""
from __future__ import annotations

import base64
import html
import logging
from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from radagent_common.ack_link import verify_ack_task
from radagent_common.fhir_models import TaskStatus
from radagent_common.openmrs_rest import rest_base_url
from radagent_common.tracing import now_iso

_log = logging.getLogger("worklist-api.ack")

_ACKED = (TaskStatus.COMPLETED, TaskStatus.ACCEPTED)


class Caller(NamedTuple):
    """The resolved acknowledger: a display string for the audit note, plus the OpenMRS Provider
    uuid when the session carries one.

    Two identifier spaces meet here and they are NOT interchangeable. `who` is built from the USER
    uuid ("Marisol Reyes (29defed0-...)"), while `Task.owner` is a PRACTITIONER reference
    ("Practitioner/718bc49a-..."). Comparing the two directly always reports a mismatch, and
    comparing display strings instead would collide for clinicians who share a name (#127).

    `provider_uuid` is None when the session has no `currentProvider`, which is a real state on
    this deployment rather than an error: a user with no Provider record (admin, service accounts)
    returns null there, and the lookups that would map a USER to a PROVIDER -- `/provider?user=`
    and `/user/<uuid>` -- both 403 under the fleet's least-privilege credentials, because both
    need "Get Users". So None means "cannot determine", NEVER "not authorised": the ack still
    lands and the note says the provider was unknown. Refusing on None would lock out exactly the
    clinicians whose Provider records are missing.

    That 403 is about the USER->PROVIDER direction only. Reading a provider BY ITS OWN UUID
    (`/provider/<uuid>`) is a Provider read, needs no user privilege, and does work -- which is
    what lets `_resolve_addressee_name` put a name on the page (#130). The two were conflated in
    the first version of this comment, and the result was a page that offered a covering physician
    a bare uuid to acknowledge on behalf of.
    """
    who: str
    provider_uuid: str | None


class Proof(NamedTuple):
    """How this request proved who the caller is, kept for the lifetime of THIS request only.

    It exists so a follow-up read can be made AS THE CALLER rather than as a service account.
    That is deliberate on three counts. worklist-api holds no OpenMRS credentials at all, and
    giving it some to render a name would widen its blast radius against the #75 least-privilege
    hosting. Reading as the caller means this page can never surface anything the clinician could
    not already read in the RIS. And it keeps the module docstring's promise that identity here is
    the human, not the link.

    The Basic password is already in this request's headers, so holding it for one more call
    within the same request adds no exposure. It is never logged, never stored, and never leaves
    the request.
    """
    cookies: dict[str, str] | None = None
    auth: tuple[str, str] | None = None


class OpenmrsIdentity:
    """WHO is acknowledging, per OpenMRS. Both resolvers probe `/ws/rest/v1/session` and return
    a `Caller`, or None -- deliberately no distinction between unknown user, wrong password and
    dead session.

    `/ws/rest/v1/session` carries `currentProvider` alongside `user`, so the Provider uuid the
    owner check needs costs no extra request and no extra privilege (#127)."""

    def __init__(self, base_url: str | None = None, timeout: float = 10.0):
        self.base_url = (base_url or rest_base_url()).rstrip("/")
        self._timeout = timeout

    @staticmethod
    def _who(r: httpx.Response, fallback: str = "") -> "Caller | None":
        if r.status_code != 200:
            return None
        body = r.json()
        if not body.get("authenticated"):
            return None
        user = body.get("user") or {}
        display = user.get("display") or fallback
        uuid = user.get("uuid") or ""
        if not (display or uuid):
            return None
        # `currentProvider` is OpenMRS's own answer to "which Provider is this user", already in
        # this response. Absent/null for a user with no Provider record -- see Caller.
        provider = body.get("currentProvider") or {}
        provider_uuid = provider.get("uuid") or None
        if not display:
            # A uuid with no display would render as " (uuid-x)" -- a nameless audit line on
            # the one string that says WHO attested. Name the uuid instead of leading with a
            # blank.
            return Caller(f"unknown user ({uuid})", provider_uuid)
        return Caller(f"{display} ({uuid})" if uuid else display, provider_uuid)

    async def whoami(self, username: str, password: str) -> "Caller | None":
        async with httpx.AsyncClient(timeout=self._timeout, auth=(username, password)) as c:
            return self._who(await c.get(f"{self.base_url}/session"), fallback=username)

    async def whoami_session(self, jsessionid: str) -> "Caller | None":
        """The one-click path: resolve identity from the caller's EXISTING OpenMRS session
        cookie instead of soliciting credentials again. OpenMRS treats the forwarded JSESSIONID
        exactly like any in-app request, so the answer is the same identity the chart itself is
        rendered for."""
        async with httpx.AsyncClient(timeout=self._timeout,
                                     cookies={"JSESSIONID": jsessionid}) as c:
            return self._who(await c.get(f"{self.base_url}/session"))

    async def provider_name(self, provider_uuid: str, proof: "Proof") -> str | None:
        """The Provider's human name, read AS THE CALLER (#130). None on ANY failure.

        Best-effort by construction: this only ever decorates a page and a note that are already
        correct without it, so a slow or unhappy OpenMRS must cost a name, never the ack. Every
        exception is swallowed for that reason, and the caller falls back to the raw reference.

        `v=custom:(display)` keeps the response small; `display` comes back as
        "dr.reyes - Marisol Reyes", which is what a clinician actually recognises.
        """
        if not provider_uuid:
            return None
        try:
            async with httpx.AsyncClient(timeout=self._timeout, cookies=proof.cookies or {},
                                         auth=proof.auth) as c:
                r = await c.get(f"{self.base_url}/provider/{provider_uuid}",
                                params={"v": "custom:(display)"})
            if r.status_code != 200:
                return None
            return (r.json().get("display") or "").strip() or None
        except Exception:  # noqa: BLE001 -- a name is garnish; the ack must not depend on it
            _log.debug("could not resolve provider %s for the ack page", provider_uuid,
                       exc_info=True)
            return None


def _challenge() -> Response:
    """401 + a Basic challenge so a phone browser opens its native login prompt."""
    return Response(
        status_code=401,
        content="Sign in with your OpenMRS account to acknowledge this result.",
        headers={"WWW-Authenticate": 'Basic realm="LH-Radiology critical-result acknowledgement"'},
    )


def _page(title: str, lines: list[str]) -> HTMLResponse:
    body = "".join(f"<p>{html.escape(line)}</p>" for line in lines if line)
    return HTMLResponse(
        f"<!doctype html><html><head><meta name=\"viewport\" "
        f"content=\"width=device-width, initial-scale=1\"><title>{html.escape(title)}</title>"
        f"</head><body style=\"font-family: sans-serif; max-width: 30em; margin: 3em auto;\">"
        f"<h1 style=\"font-size:1.2em\">{html.escape(title)}</h1>{body}</body></html>"
    )


class Anomaly(NamedTuple):
    """What is irregular about this acknowledgement, worked out once and used by both methods.

    Both fields exist because the confirmation page has to state them BEFORE the click. The live
    #127 reproduction was a physician reading a page that named the wrong clinician and said
    nothing about it being wrong; the live #128 reproduction was a page promising the escalation
    clock was closed when on-call had already been paged and stayed paged.
    """
    on_behalf_of: str | None      # the addressee, when the acknowledger is somebody else
    late_deadline_iso: str | None  # the missed deadline, when the window has closed
    escalated: bool                # the escalation already fired (Task is FAILED)


def _owner_display(task) -> str:
    """A name for the Task's addressee from the resource ALONE, falling back to the bare
    reference. `_resolve_addressee_name` is what turns that fallback into a real name."""
    owner = getattr(task, "owner", None)
    if owner is None:
        return "the intended recipient"
    return getattr(owner, "display", None) or getattr(owner, "reference", None) or \
        "the intended recipient"


def _owner_provider_uuid(task) -> str:
    """The bare uuid out of a `Practitioner/<uuid>` owner reference, '' when it is not one."""
    ref = getattr(getattr(task, "owner", None), "reference", "") or ""
    head, _, tail = ref.partition("/")
    return tail if head == "Practitioner" and tail else ""


async def _resolve_addressee_name(task, identity: OpenmrsIdentity, proof: Proof) -> str | None:
    """Turn the addressee into something a clinician can read, or None to keep the fallback.

    #130: the live ledger writes `Task.owner` as a reference with NO `display`, because the comms
    agent carries the requester reference verbatim rather than dereferencing it across stores (see
    that module's docstring, and the note on #130 about why the producer side stays that way). So
    `_owner_display` returned `Practitioner/718bc49a-...` and the page asked a covering physician
    to acknowledge on behalf of a uuid. That defeats the safeguard #127's decision rests on: the
    whole reason a non-addressee ack is permitted is that the acknowledger READS who it was
    actually sent to first.

    Costs nothing on the ordinary path -- the caller only reaches here on a genuine mismatch --
    and costs nothing when the resource already carries a display.
    """
    owner = getattr(task, "owner", None)
    if owner is not None and getattr(owner, "display", None):
        return None  # the resource already names them; no lookup earned
    return await identity.provider_name(_owner_provider_uuid(task), proof)


def _assess(task, caller: "Caller", now: datetime) -> Anomaly:
    """Compare the caller and the clock against the Task. Pure, so both GET and POST agree.

    The owner comparison is on the PROVIDER uuid, never the display string (see `Caller`). When
    the caller's provider cannot be resolved we do NOT claim a mismatch: an unknown provider is
    not evidence of the wrong person, and treating it as one would refuse or mislabel every
    clinician whose Provider record is missing.
    """
    on_behalf_of = None
    owner_ref = getattr(getattr(task, "owner", None), "reference", "") or ""
    owner_uuid = owner_ref.split("/")[-1] if owner_ref else ""
    if caller.provider_uuid and owner_uuid and caller.provider_uuid != owner_uuid:
        on_behalf_of = _owner_display(task)

    late_deadline_iso = None
    period = getattr(getattr(task, "restriction", None), "period", None)
    end = getattr(period, "end", None)
    if end is not None:
        deadline = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        if now > deadline:
            late_deadline_iso = deadline.isoformat()

    return Anomaly(on_behalf_of, late_deadline_iso, task.status == TaskStatus.FAILED)


def _confirm_page(task_id: str, sig: str, who: str, finding: str | None,
                  anomaly: Anomaly) -> HTMLResponse:
    """The GET page: says what is about to be attested, who it will be attributed to, and what is
    irregular about it.

    The button POSTs, which is the whole point of the split -- see `acknowledge` below.
    """
    # Query-only action: resolves against the page's OWN url, so it keeps the path whatever
    # prefix served it and carries the signature forward.
    #
    # A relative "ack/<id>?sig=..." doubled the segment. The page lives at .../ack/<id>, whose
    # base directory is .../ack/, so the browser resolved the action to .../ack/ack/<id> and the
    # POST 404d -- in cluster AND behind the #75 overlay, so the acknowledgement was never
    # completable through the button at all. Found in the #76 arc 2 rehearsal, 2026-08-20, by a
    # referring physician pressing Acknowledge and nothing happening; the ledger Task stayed
    # `requested` and the escalation clock kept running. Same shape as #122 on the sign-off
    # override form, and the same blind spot: the tests POST the endpoint directly, so no test
    # ever resolved the action the browser resolves.
    action = f"?sig={quote(sig)}"

    # The attribution line, and then the anomalies. Each anomaly is stated in the terms the
    # clinician is deciding in -- who this was sent to, and whether anyone else has been paged --
    # rather than in the ledger's terms.
    if anomaly.on_behalf_of:
        attribution = (
            f"<p>This result was sent to <strong>{html.escape(anomaly.on_behalf_of)}</strong>. "
            f"You are signed in as <strong>{html.escape(who)}</strong>, so it will be recorded as "
            f"acknowledged on their behalf by you.</p>")
        button = "Acknowledge on their behalf"
    else:
        attribution = (f"<p>This will be recorded as acknowledged by "
                       f"<strong>{html.escape(who)}</strong>.</p>")
        button = "Acknowledge"

    if anomaly.late_deadline_iso:
        # NOT "closes the escalation clock". The old page said that unconditionally, which is how
        # a physician was told the loop was closed while on-call stayed paged (#128).
        paged = (" On-call has already been paged and remains responsible for this result."
                 if anomaly.escalated else "")
        timing = (
            f"<p>The acknowledgement window for this result closed at "
            f"<strong>{html.escape(anomaly.late_deadline_iso)}</strong>. Your acknowledgement "
            f"will be recorded as <strong>late</strong>.{paged}</p>")
        button = "Record late acknowledgement"
    else:
        timing = ("<p>This closes the care team's escalation clock for this result.</p>")

    return HTMLResponse(
        f"<!doctype html><html><head><meta name=\"viewport\" "
        f"content=\"width=device-width, initial-scale=1\">"
        f"<title>Acknowledge critical result</title></head>"
        f"<body style=\"font-family: sans-serif; max-width: 30em; margin: 3em auto;\">"
        f"<h1 style=\"font-size:1.2em\">Acknowledge critical result</h1>"
        + (f"<p>Finding: {html.escape(finding)}</p>" if finding else "")
        + attribution + timing
        + f"<form method=\"post\" action=\"{html.escape(action)}\">"
          f"<button type=\"submit\" style=\"font-size:1.1em; padding:0.6em 1.2em;\">"
          f"{html.escape(button)}</button></form></body></html>"
    )


def create_ack_router(ledger, identity: OpenmrsIdentity) -> APIRouter:
    router = APIRouter()

    async def _resolve_caller(task_id: str, request: Request, sig: str):
        """Signature, then identity. Returns `(caller, proof, early_response)`; `early_response`
        is a challenge to return as-is. Shared by both methods so the ordering guarantee cannot
        drift between them: the signature is checked BEFORE any credential is solicited or any
        session is consulted, on GET and POST alike.

        `proof` is whichever credential actually resolved the caller, so a follow-up read can be
        made as them rather than as a service account (see `Proof`)."""
        # 1. The link itself must be genuine -- BEFORE any credential prompt.
        if not verify_ack_task(task_id, sig):
            raise HTTPException(status_code=403, detail="invalid acknowledgement link")

        # 2a. The human, via their EXISTING OpenMRS session when the browser sent it -- the
        # one-click path (see the module docstring for the routing condition that makes the
        # cookie arrive). A cookie that no longer resolves falls through to Basic, not to a
        # refusal: stale sessions are routine.
        who = None
        proof = Proof()
        jsessionid = request.cookies.get("JSESSIONID", "")
        if jsessionid:
            who = await identity.whoami_session(jsessionid)
            if who is not None:
                proof = Proof(cookies={"JSESSIONID": jsessionid})

        # 2b. Fallback: HTTP Basic. fastapi's HTTPBasic dependency is skipped on purpose: it
        # cannot order itself after the signature check, and the challenge must not fire for
        # forged links.
        if who is None:
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("basic "):
                return None, None, _challenge()
            try:
                username, _, password = base64.b64decode(auth[6:]).decode().partition(":")
            except Exception:
                return None, None, _challenge()
            who = await identity.whoami(username, password)
            if who is None:
                return None, None, _challenge()
            proof = Proof(auth=(username, password))
        return who, proof, None

    async def _load(task_id: str):
        try:
            task = await ledger.get_task(task_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 410):
                raise HTTPException(status_code=404, detail="unknown acknowledgement task") from e
            raise

        finding = None
        if task.focus and task.focus.reference:
            try:
                comm = await ledger.get_communication(task.focus.reference.split("/")[-1])
                finding = comm.finding_summary
            except Exception:  # noqa: BLE001 -- the page must not fail over its garnish
                finding = None
        return task, finding

    async def _named(task, caller: Caller, proof: Proof) -> Anomaly:
        """`_assess` plus a readable addressee. Split so `_assess` stays pure and synchronous:
        deciding WHETHER this is a delegated or late ack is a rule, and must be testable without
        a network. Naming the addressee is I/O, is best-effort, and only happens on the mismatch
        path (#130).

        One resolution feeds BOTH the page and the ledger note, which is what stops the audit
        record and the confirmation page telling different stories about the same ack.
        """
        anomaly = _assess(task, caller, datetime.now(timezone.utc))
        if anomaly.on_behalf_of is None:
            return anomaly
        name = await _resolve_addressee_name(task, identity, proof)
        return anomaly._replace(on_behalf_of=name) if name else anomaly

    def _already(finding: str | None) -> HTMLResponse:
        return _page("Already acknowledged", [
            f"Finding: {finding}" if finding else "",
            "This critical result was already acknowledged; nothing further is needed.",
        ])

    @router.get("/ack/{task_id}")
    async def confirm(task_id: str, request: Request, sig: str = "") -> Response:
        """Renders the confirmation page. Deliberately changes NO state.

        An acknowledgement is a clinical attestation that a named physician saw a critical
        result, so it must follow a deliberate act. Once identity can come from an ambient
        cookie, a GET that acknowledges is reachable without one: browsers prefetch on hover
        and refetch on tab restore, and either would silently close the escalation clock with
        a physician's name on it. Under the older Basic-only flow that could not happen,
        because a prefetch carries no credentials. So the state change moved to POST and this
        GET only asks. The click on the button is still the only click the physician makes.
        """
        caller, proof, early = await _resolve_caller(task_id, request, sig)
        if early is not None:
            return early
        task, finding = await _load(task_id)
        if task.status in _ACKED:
            return _already(finding)
        anomaly = await _named(task, caller, proof)
        return _confirm_page(task_id, sig, caller.who, finding, anomaly)

    @router.post("/ack/{task_id}")
    async def acknowledge(task_id: str, request: Request, sig: str = "") -> Response:
        """The state change: submitted from the confirmation page's button."""
        caller, proof, early = await _resolve_caller(task_id, request, sig)
        if early is not None:
            return early
        task, finding = await _load(task_id)
        if task.status in _ACKED:
            return _already(finding)

        anomaly = await _named(task, caller, proof)
        await ledger.complete_ack_task(
            task_id, acknowledged_by=caller.who, at_iso=now_iso(),
            on_behalf_of=anomaly.on_behalf_of,
            late_deadline_iso=anomaly.late_deadline_iso)
        _log.info("ack task %s acknowledged by %s (on_behalf_of=%s late=%s escalated=%s)",
                  task_id, caller.who, anomaly.on_behalf_of,
                  bool(anomaly.late_deadline_iso), anomaly.escalated)

        # The result page mirrors the confirmation page's claims, so a screenshot of either tells
        # the same story. It must never assert the clock is closed when an escalation is running.
        if anomaly.on_behalf_of:
            attribution = (f"Recorded as acknowledged by {caller.who}, "
                           f"on behalf of {anomaly.on_behalf_of}.")
        else:
            attribution = f"Recorded as acknowledged by {caller.who}."
        if anomaly.late_deadline_iso:
            closing = (f"Recorded as LATE: the acknowledgement window closed at "
                       f"{anomaly.late_deadline_iso}.")
            if anomaly.escalated:
                closing += (" On-call was paged and remains responsible for this result; "
                            "this acknowledgement does not stand them down.")
        else:
            closing = "The care team's escalation clock for this result is now closed."
        return _page("Critical result acknowledged", [
            f"Finding: {finding}" if finding else "",
            attribution,
            closing,
        ])

    return router
