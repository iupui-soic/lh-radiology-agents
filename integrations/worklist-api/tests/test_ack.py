"""The /ack/{task_id} surface (#79). TestClient + injected fakes, like test_api.py.

The ordering property is the load-bearing one: signature BEFORE identity (a forged link never
solicits credentials), identity BEFORE the ledger (an anonymous tap never reads the loop), and
an already-closed loop is never re-written.
"""
from __future__ import annotations

import httpx
import html as html_mod
import pathlib
import re
from urllib.parse import urljoin

import pytest
from fastapi.testclient import TestClient

from radagent_common.ack_link import sign_ack_task
from radagent_common.fhir_models import (
    Period,
    TaskRestriction,
    Communication,
    CommunicationPayload,
    Reference,
    Task,
    TaskStatus,
)

from ack import Caller
from main import create_app

_SECRET = "ack-test-secret"


class FakeLedger:
    def __init__(self, task: Task | None = None):
        self.task = task
        self.completed: list[tuple[str, str]] = []
        self.calls: list[dict] = []
        self.comm_reads: list[str] = []

    async def get_task(self, task_id: str) -> Task:
        if self.task is None:
            req = httpx.Request("GET", f"http://ledger/fhir/Task/{task_id}")
            raise httpx.HTTPStatusError(
                "404", request=req, response=httpx.Response(404, request=req))
        return self.task

    async def get_communication(self, comm_id: str) -> Communication:
        self.comm_reads.append(comm_id)
        return Communication(
            status="in-progress",
            payload=[CommunicationPayload(contentString="pneumothorax")])

    async def complete_ack_task(self, task_id: str, *, acknowledged_by: str,
                                at_iso: str, on_behalf_of: str | None = None,
                                late_deadline_iso: str | None = None) -> Task:
        self.completed.append((task_id, acknowledged_by))
        self.calls.append({"task_id": task_id, "acknowledged_by": acknowledged_by,
                           "on_behalf_of": on_behalf_of,
                           "late_deadline_iso": late_deadline_iso})
        done = self.task.model_copy(deep=True)
        # Mirrors the real ledger's status rule (#128): a Task the escalation already FAILED keeps
        # that status, so a late ack cannot erase the miss. A fake that always set COMPLETED would
        # pass a handler that reverted it, which is the #118 lesson about fakes narrower than the
        # contract they stand in for.
        if done.status != TaskStatus.FAILED:
            done.status = TaskStatus.COMPLETED
        return done


class FakeIdentity:
    """Accepts exactly dr-ref/refpass (Basic) and sess-live (session cookie); records every
    attempt on each path so tests can pin WHEN and BY WHICH proof identity is consulted."""

    def __init__(self, provider_uuid: str | None = "prov-ref",
                 provider_names: dict | None = None):
        self.attempts: list[str] = []
        self.session_attempts: list[str] = []
        # The Provider uuid OpenMRS would report in session.currentProvider. Defaults to the one
        # _open_task() addresses, so the default rig is the ordinary same-person acknowledgement.
        self.provider_uuid = provider_uuid
        # #130: what GET /provider/<uuid> would answer. Empty by default, so a test that does not
        # opt in sees the un-resolvable case rather than a name conjured by the fake.
        self.provider_names = provider_names or {}
        self.name_lookups: list[tuple] = []

    async def provider_name(self, provider_uuid: str, proof) -> str | None:
        # Records the PROOF as well as the uuid: the lookup being made with the caller's own
        # credential rather than a service account is a property worth pinning, not an accident.
        self.name_lookups.append((provider_uuid, proof))
        return self.provider_names.get(provider_uuid)

    async def whoami(self, username: str, password: str) -> "Caller | None":
        self.attempts.append(username)
        if (username, password) == ("dr-ref", "refpass"):
            return Caller("Dr Referrer (uuid-ref)", self.provider_uuid)
        return None

    async def whoami_session(self, jsessionid: str) -> "Caller | None":
        self.session_attempts.append(jsessionid)
        if jsessionid == "sess-live":
            return Caller("Dr Referrer (uuid-ref)", self.provider_uuid)
        return None


def _open_task(task_id: str = "task-7") -> Task:
    return Task(id=task_id, status=TaskStatus.REQUESTED,
                focus=Reference(reference="Communication/comm-1"))


@pytest.fixture()
def rig(monkeypatch):
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    ledger = FakeLedger(task=_open_task())
    identity = FakeIdentity()
    client = TestClient(create_app(
        orthanc=object(), assignment=object(),  # never touched by /ack
        store=_NullStore(), ledger=ledger, identity=identity))
    return client, ledger, identity


class _NullStore:
    def size(self) -> int:
        return 0

    def all(self) -> dict:
        return {}


def _sig(task_id: str = "task-7") -> str:
    return sign_ack_task(task_id, _SECRET)


def test_forged_signature_is_403_and_never_solicits_credentials(rig):
    client, ledger, identity = rig
    r = client.get("/ack/task-7", params={"sig": "not-a-real-signature"})
    assert r.status_code == 403
    assert identity.attempts == []          # no credential prompt for a forged link
    assert ledger.completed == []


def test_unconfigured_secret_fails_closed(rig, monkeypatch):
    client, ledger, identity = rig
    monkeypatch.delenv("CRITCOM_ACK_HMAC_SECRET", raising=False)
    r = client.get("/ack/task-7", params={"sig": _sig()})
    assert r.status_code == 403             # the surface does not exist without the secret


def test_missing_credentials_get_a_basic_challenge(rig):
    client, ledger, identity = rig
    r = client.get("/ack/task-7", params={"sig": _sig()})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")
    assert ledger.completed == []


def test_bad_credentials_are_rechallenged_and_touch_nothing(rig):
    client, ledger, identity = rig
    r = client.get("/ack/task-7", params={"sig": _sig()}, auth=("dr-ref", "wrong"))
    assert r.status_code == 401
    assert identity.attempts == ["dr-ref"]
    assert ledger.completed == []


def test_authenticated_tap_closes_the_loop_with_who(rig):
    client, ledger, identity = rig
    r = client.post("/ack/task-7", params={"sig": _sig()}, auth=("dr-ref", "refpass"))
    assert r.status_code == 200
    assert ledger.completed == [("task-7", "Dr Referrer (uuid-ref)")]
    assert "acknowledged" in r.text.lower()
    assert "Dr Referrer" in r.text
    assert "pneumothorax" in r.text         # the finding label from the Communication


def test_already_completed_is_idempotent(rig):
    client, ledger, identity = rig
    ledger.task = _open_task()
    ledger.task.status = TaskStatus.COMPLETED
    r = client.get("/ack/task-7", params={"sig": _sig()}, auth=("dr-ref", "refpass"))
    assert r.status_code == 200
    assert "already acknowledged" in r.text.lower()
    assert ledger.completed == []           # never re-written


def test_accepted_counts_as_acknowledged(rig):
    """ack_state treats ACCEPTED as acknowledged; the surface must agree or a tap would
    re-complete a loop the orchestrator already considers closed."""
    client, ledger, identity = rig
    ledger.task = _open_task()
    ledger.task.status = TaskStatus.ACCEPTED
    r = client.get("/ack/task-7", params={"sig": _sig()}, auth=("dr-ref", "refpass"))
    assert r.status_code == 200
    assert ledger.completed == []


def test_unknown_task_is_404(rig):
    client, ledger, identity = rig
    ledger.task = None
    r = client.get("/ack/task-404", params={"sig": _sig("task-404")},
                   auth=("dr-ref", "refpass"))
    assert r.status_code == 404


def test_signature_for_another_task_does_not_open_this_one(rig):
    """The prefix trap, at the surface: task-7's signature must not acknowledge task-70."""
    client, ledger, identity = rig
    r = client.get("/ack/task-70", params={"sig": _sig("task-7")}, auth=("dr-ref", "refpass"))
    assert r.status_code == 403
    assert ledger.completed == []


def test_live_openmrs_session_acks_in_one_click(rig):
    """The in-EHR path: the browser already holds an authenticated OpenMRS session, so the tap
    needs no second login -- identity comes from the forwarded JSESSIONID and Basic is never
    consulted."""
    client, ledger, identity = rig
    client.cookies.set("JSESSIONID", "sess-live")
    r = client.post("/ack/task-7", params={"sig": _sig()})
    assert r.status_code == 200
    assert ledger.completed == [("task-7", "Dr Referrer (uuid-ref)")]
    assert identity.session_attempts == ["sess-live"]
    assert identity.attempts == []          # no Basic prompt on the one-click path


def test_stale_session_falls_back_to_the_basic_challenge(rig):
    """A dead cookie is routine (sessions expire), not suspicious: the tap degrades to the
    pre-existing login prompt instead of a refusal."""
    client, ledger, identity = rig
    client.cookies.set("JSESSIONID", "sess-expired")
    r = client.get("/ack/task-7", params={"sig": _sig()})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")
    assert identity.session_attempts == ["sess-expired"]
    assert ledger.completed == []


def test_stale_session_with_valid_basic_still_acks_in_one_round_trip(rig):
    client, ledger, identity = rig
    client.cookies.set("JSESSIONID", "sess-expired")
    r = client.post("/ack/task-7", params={"sig": _sig()}, auth=("dr-ref", "refpass"))
    assert r.status_code == 200
    assert ledger.completed == [("task-7", "Dr Referrer (uuid-ref)")]
    assert identity.session_attempts == ["sess-expired"]
    assert identity.attempts == ["dr-ref"]  # fallback consulted only after the cookie missed


def test_forged_signature_never_consults_the_session_either(rig):
    """Ordering is unchanged by the new path: signature FIRST, so a forged link learns nothing
    from -- and sends nothing to -- the caller's live session."""
    client, ledger, identity = rig
    client.cookies.set("JSESSIONID", "sess-live")
    r = client.get("/ack/task-7", params={"sig": "not-a-real-signature"})
    assert r.status_code == 403
    assert identity.session_attempts == []
    assert identity.attempts == []
    assert ledger.completed == []


def test_finding_fetch_failure_never_costs_the_ack(rig):
    """The Communication read is garnish for the page; a ledger hiccup there must not fail the
    acknowledgement itself."""
    client, ledger, identity = rig

    async def boom(comm_id):
        raise RuntimeError("ledger hiccup")

    ledger.get_communication = boom
    r = client.post("/ack/task-7", params={"sig": _sig()}, auth=("dr-ref", "refpass"))
    assert r.status_code == 200
    assert ledger.completed == [("task-7", "Dr Referrer (uuid-ref)")]


# --- the GET/POST split: only a deliberate act attests -----------------------

def test_get_renders_a_confirmation_page_and_acknowledges_nothing(rig):
    """The prefetch-safety property. A browser preloading the link on hover, a restored tab, a
    mail client previewing it, or a security scanner following it all issue a GET -- and with
    the cookie path in play they can carry the physician's live session. None of them is a
    human acknowledging a critical result, so none of them may write one."""
    client, ledger, identity = rig
    client.cookies.set("JSESSIONID", "sess-live")

    r = client.get("/ack/task-7", params={"sig": _sig()})

    assert r.status_code == 200
    assert ledger.completed == []                 # nothing attested
    assert "<form" in r.text and 'method="post"' in r.text
    assert "Dr Referrer (uuid-ref)" in r.text     # says who it will be attributed to


def test_the_confirm_page_button_posts_back_to_the_same_signed_link(rig):
    """The page must carry the signature forward, or the button 403s and the one-click promise
    breaks."""
    client, _, _ = rig
    client.cookies.set("JSESSIONID", "sess-live")

    page = client.get("/ack/task-7", params={"sig": _sig()}).text

    assert f"sig={_sig()}" in page


def _form_action(page: str) -> str:
    m = re.search(r'<form[^>]*action="([^"]*)"', page)
    assert m, "the confirm page must carry a form action"
    return html_mod.unescape(m.group(1))


@pytest.mark.parametrize("served_at", [
    "http://host/ack/task-7",                 # in cluster
    "http://host/reading-api/ack/task-7",     # behind the #75 Caddy overlay
    "http://host/deep/prefix/ack/task-7",     # any other future prefix
])
def test_the_button_resolves_to_the_page_it_was_served_from(rig, served_at):
    """THE regression. A relative "ack/<id>?sig=..." resolves against the page's base directory
    (.../ack/), producing .../ack/ack/<id> -- a 404, so pressing Acknowledge silently did nothing
    and the escalation clock kept running. Resolve the action the way a browser does and require
    it to land back on the same path."""
    client, _, _ = rig
    client.cookies.set("JSESSIONID", "sess-live")
    page = client.get("/ack/task-7", params={"sig": _sig()}).text

    resolved = urljoin(served_at + f"?sig={_sig()}", _form_action(page))

    assert resolved.split("?")[0] == served_at, f"POST would go to {resolved}"
    assert f"sig={_sig()}" in resolved
    assert "/ack/ack/" not in resolved


def test_the_action_is_not_an_absolute_in_cluster_path(rig):
    """An absolute "/ack/<id>" would fix the doubling and reintroduce #122: behind the overlay
    the POST would leave the prefix and miss the route entirely."""
    client, _, _ = rig
    client.cookies.set("JSESSIONID", "sess-live")
    page = client.get("/ack/task-7", params={"sig": _sig()}).text

    assert not _form_action(page).startswith("/")


def test_post_still_refuses_a_forged_signature_before_any_identity_work(rig):
    """The ordering guarantee has to hold on BOTH methods, not just the one it was written on."""
    client, ledger, identity = rig
    client.cookies.set("JSESSIONID", "sess-live")

    r = client.post("/ack/task-7", params={"sig": "not-a-real-signature"})

    assert r.status_code == 403
    assert identity.session_attempts == []
    assert identity.attempts == []
    assert ledger.completed == []


def test_post_without_credentials_challenges_rather_than_acknowledging(rig):
    client, ledger, _ = rig

    r = client.post("/ack/task-7", params={"sig": _sig()})

    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")
    assert ledger.completed == []


def test_get_on_an_already_acknowledged_task_shows_the_done_page_not_a_button(rig):
    """No button to press twice: a re-tap lands on the already-acknowledged page."""
    client, ledger, _ = rig
    ledger.task.status = TaskStatus.COMPLETED
    client.cookies.set("JSESSIONID", "sess-live")

    r = client.get("/ack/task-7", params={"sig": _sig()})

    assert r.status_code == 200
    assert "<form" not in r.text
    assert ledger.completed == []


# --- #126: the deployment condition the session path depends on ------------------------------
#
# The four session tests above hand a JSESSIONID straight to the endpoint, so they pass whether
# or not a real browser would ever send one. On the demo host it never did: OpenMRS scopes the
# cookie to `Path=/openmrs` and the ack was routed at `/reading-api/ack/*`, so `whoami_session`
# was unreachable in production while fully covered in the suite. Every acknowledgement silently
# took the Basic path, which reads identity from the browser's credential store rather than from
# the clinician. These two tests pin the ROUTING, which is the part the mocks cannot see.

_CADDYFILE = (
    pathlib.Path(__file__).resolve().parents[3] / "docker" / "caddy" / "Caddyfile"
)
_RUNBOOK = (
    pathlib.Path(__file__).resolve().parents[3] / "docs" / "showcase-runbook.md"
)


def test_the_ack_route_is_served_under_the_jsessionid_cookie_path():
    """OpenMRS sets `JSESSIONID=...; Path=/openmrs`, so only a route under /openmrs receives it."""
    caddyfile = _CADDYFILE.read_text()
    assert "handle /openmrs/ack/*" in caddyfile, (
        "the ack surface must be served under /openmrs so the browser sends JSESSIONID; "
        "without it whoami_session is dead code in production and every ack falls back to Basic"
    )
    # and it has to be matched BEFORE the catch-all /openmrs/* proxy to OpenMRS, or the app
    # swallows it and the ack 404s -- the same ordering the vendor-assets override needs.
    assert caddyfile.index("handle /openmrs/ack/*") < caddyfile.index("handle /openmrs/* {"), (
        "/openmrs/ack/* must precede /openmrs/* or Caddy routes the ack to OpenMRS itself"
    )


def test_the_runbook_configures_the_ack_base_url_under_openmrs():
    """The route alone is not enough: the minted link has to point at it.

    `CRITCOM_ACK_BASE_URL` is what `communications` stamps into the chart notification, so a
    correct route with a /reading-api base URL still produces Basic-path links.
    """
    runbook = _RUNBOOK.read_text()
    assert "CRITCOM_ACK_BASE_URL=https://demo.example.org/openmrs" in runbook, (
        "the run-book's setup step must configure the ack base URL under /openmrs (#126)"
    )


# --- #127 (WHO) and #128 (WHEN): the endpoint used to trust both -------------------------------
#
# Reproduced live on 2026-08-23, one after the other, in a single #79 rehearsal. First the confirm
# page offered to record a physician uninvolved in the study, because identity had fallen back to
# HTTP Basic and Basic takes its credential from the browser's store. Then, on the next attempt,
# an ack landed after the window had lapsed and the escalation had fired, and the endpoint
# overwrote the FAILED task to COMPLETED while telling the physician the escalation clock was
# closed. On-call stayed paged.
#
# The rig's default identity provider is "prov-ref", so a task owned by Practitioner/prov-ref is
# the ordinary same-person case and everything above stays untouched.

def _addressed_task(task_id: str = "task-7", *, owner: str = "prov-ref",
                    deadline: str | None = None,
                    status: TaskStatus = TaskStatus.REQUESTED,
                    owner_display: str | None = "Dr Addressee") -> Task:
    """An ack Task with an addressee, and optionally a deadline and a terminal status.

    `owner_display=None` is the shape the LIVE ledger actually writes (#130): the comms agent
    carries the requester reference verbatim and never dereferences it, so there is no display on
    the resource. Both shapes are exercised on purpose. A fixture that only ever carried a display
    is what let a page ship that offered a covering physician a bare uuid to acknowledge on behalf
    of, which is the #118 lesson landing a second time on this surface.
    """
    owner_ref = Reference(reference=f"Practitioner/{owner}", display=owner_display) \
        if owner_display else Reference(reference=f"Practitioner/{owner}")
    kwargs: dict = {
        "id": task_id, "status": status,
        "focus": Reference(reference="Communication/comm-1"),
        "owner": owner_ref,
    }
    if deadline:
        kwargs["restriction"] = TaskRestriction(period=Period(end=deadline))
    return Task(**kwargs)


def _rig_for(task: Task, provider_uuid: str | None = "prov-ref",
             provider_names: dict | None = None):
    ledger = FakeLedger(task=task)
    identity = FakeIdentity(provider_uuid=provider_uuid, provider_names=provider_names)
    client = TestClient(create_app(
        orthanc=object(), assignment=object(),
        store=_NullStore(), ledger=ledger, identity=identity))
    return client, ledger, identity


_AUTH = ("dr-ref", "refpass")
_PAST = "2020-01-01T00:00:00+00:00"
_FUTURE = "2999-01-01T00:00:00+00:00"


# --- #127: the acknowledger is not the addressee ----------------------------------------------

def test_a_non_addressee_is_told_before_the_click_who_it_was_sent_to(monkeypatch):
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, _, _ident = _rig_for(_addressed_task(owner="prov-someone-else"))
    r = client.get(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    assert r.status_code == 200
    body = html_mod.unescape(r.text)
    assert "Dr Addressee" in body            # who it was actually sent to
    assert "on their behalf" in body         # and what pressing the button will mean
    assert "Acknowledge on their behalf" in body


def test_a_non_addressee_ack_is_recorded_naming_both_parties(monkeypatch):
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, ledger, _ident = _rig_for(_addressed_task(owner="prov-someone-else"))
    r = client.post(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    assert r.status_code == 200
    assert ledger.calls[0]["acknowledged_by"] == "Dr Referrer (uuid-ref)"
    assert ledger.calls[0]["on_behalf_of"] == "Dr Addressee"
    body = html_mod.unescape(r.text)
    assert "on behalf of Dr Addressee" in body


def test_the_addressee_acking_their_own_result_says_nothing_about_delegation(monkeypatch):
    """The ordinary case must not grow scary copy: prov-ref owns it and prov-ref is acking."""
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, ledger, _ident = _rig_for(_addressed_task(owner="prov-ref"))
    body = html_mod.unescape(client.get(f"/ack/task-7?sig={_sig()}", auth=_AUTH).text)
    assert "on their behalf" not in body
    client.post(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    assert ledger.calls[0]["on_behalf_of"] is None


def test_an_unresolvable_provider_is_not_treated_as_a_mismatch(monkeypatch):
    """#127's load-bearing degradation. `currentProvider` is null for a user with no Provider
    record, and the lookups that would map it 403 under the fleet's least-privilege credentials.
    Calling that a mismatch would mislabel every such clinician as acknowledging on someone
    else's behalf; refusing on it would lock them out entirely."""
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, ledger, _ident = _rig_for(_addressed_task(owner="prov-someone-else"), provider_uuid=None)
    r = client.post(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    assert r.status_code == 200                      # still lands
    assert ledger.calls[0]["on_behalf_of"] is None   # and is not claimed to be delegated


def test_a_task_with_no_owner_is_not_a_mismatch(monkeypatch):
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, ledger, _ident = _rig_for(_open_task())
    client.post(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    assert ledger.calls[0]["on_behalf_of"] is None


# --- #128: the window has lapsed ---------------------------------------------------------------

def test_a_late_ack_is_announced_as_late_before_the_click(monkeypatch):
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, _, _ident = _rig_for(_addressed_task(deadline=_PAST))
    body = html_mod.unescape(client.get(f"/ack/task-7?sig={_sig()}", auth=_AUTH).text)
    assert "closed at" in body and "late" in body
    assert "Record late acknowledgement" in body
    # the old unconditional promise must be gone
    assert "This closes the care team's escalation clock" not in body


def test_a_late_ack_after_escalation_says_on_call_remains_responsible(monkeypatch):
    """The sentence that made the live reproduction misleading rather than merely wrong."""
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, _, _ident = _rig_for(_addressed_task(deadline=_PAST, status=TaskStatus.FAILED))
    body = html_mod.unescape(client.get(f"/ack/task-7?sig={_sig()}", auth=_AUTH).text)
    assert "On-call has already been paged" in body
    assert "remains responsible" in body


def test_a_late_ack_does_not_erase_the_lapse(monkeypatch):
    """The core of #128: an escalated Task keeps FAILED, and the deadline it missed is recorded."""
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    task = _addressed_task(deadline=_PAST, status=TaskStatus.FAILED)
    client, ledger, _ident = _rig_for(task)
    r = client.post(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    assert r.status_code == 200
    assert ledger.calls[0]["late_deadline_iso"] is not None
    body = html_mod.unescape(r.text)
    assert "LATE" in body
    assert "does not stand them down" in body
    assert "escalation clock for this result is now closed" not in body


def test_an_ack_inside_the_window_is_not_late(monkeypatch):
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, ledger, _ident = _rig_for(_addressed_task(deadline=_FUTURE))
    body = html_mod.unescape(client.get(f"/ack/task-7?sig={_sig()}", auth=_AUTH).text)
    assert "late" not in body.lower()
    assert "This closes the care team's escalation clock" in body
    client.post(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    assert ledger.calls[0]["late_deadline_iso"] is None


def test_a_task_with_no_deadline_is_never_late(monkeypatch):
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, ledger, _ident = _rig_for(_addressed_task())
    client.post(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    assert ledger.calls[0]["late_deadline_iso"] is None


def test_both_anomalies_at_once_are_both_stated(monkeypatch):
    """A covering physician acknowledging a result that already escalated. The page has to carry
    both facts without either hiding the other."""
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, ledger, _ident = _rig_for(
        _addressed_task(owner="prov-someone-else", deadline=_PAST, status=TaskStatus.FAILED))
    body = html_mod.unescape(client.get(f"/ack/task-7?sig={_sig()}", auth=_AUTH).text)
    assert "Dr Addressee" in body and "on their behalf" in body
    assert "late" in body and "On-call has already been paged" in body
    client.post(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    assert ledger.calls[0]["on_behalf_of"] == "Dr Addressee"
    assert ledger.calls[0]["late_deadline_iso"] is not None


# --- #130: the addressee has to be readable, not a uuid ----------------------------------------
#
# Found by running !180 on the demo host, 2026-08-24. The delegation copy was correct and the
# ledger note was correct, but both named the addressee `Practitioner/718bc49a-...` because the
# LIVE Task.owner carries no display and the fixture above always did. A covering physician was
# asked to acknowledge on behalf of a uuid, which defeats the safeguard #127's decision rests on.

_LIVE_SHAPE = dict(owner="prov-someone-else", owner_display=None)   # what the ledger really writes
_NAMES = {"prov-someone-else": "dr.reyes - Marisol Reyes"}


def test_a_display_less_owner_is_named_from_the_provider_record(monkeypatch):
    """The #130 reproduction, inverted. The owner reference has no display, exactly as the live
    ledger writes it, and the page must still name a human."""
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, _, ident = _rig_for(_addressed_task(**_LIVE_SHAPE), provider_names=_NAMES)
    body = html_mod.unescape(client.get(f"/ack/task-7?sig={_sig()}", auth=_AUTH).text)
    assert "dr.reyes - Marisol Reyes" in body
    assert "Practitioner/prov-someone-else" not in body    # the raw reference is gone
    assert ident.name_lookups[0][0] == "prov-someone-else"  # looked up by the OWNER's uuid


def test_the_ledger_note_names_the_addressee_the_same_way_the_page_does(monkeypatch):
    """One resolution feeds both, so the audit record and the confirmation page cannot tell
    different stories about the same acknowledgement."""
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, ledger, _ = _rig_for(_addressed_task(**_LIVE_SHAPE), provider_names=_NAMES)
    page = html_mod.unescape(client.get(f"/ack/task-7?sig={_sig()}", auth=_AUTH).text)
    result = html_mod.unescape(client.post(f"/ack/task-7?sig={_sig()}", auth=_AUTH).text)
    assert ledger.calls[0]["on_behalf_of"] == "dr.reyes - Marisol Reyes"
    for surface in (page, result):
        assert "dr.reyes - Marisol Reyes" in surface
        assert "Practitioner/prov-someone-else" not in surface


def test_an_unresolvable_name_falls_back_to_the_reference_and_still_acks(monkeypatch):
    """Best-effort by construction: OpenMRS being slow, down or stingy with privileges costs a
    name, never the acknowledgement. This is the degradation #127 already insisted on, one field
    further in."""
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, ledger, _ = _rig_for(_addressed_task(**_LIVE_SHAPE), provider_names={})  # lookup misses
    r = client.post(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    assert r.status_code == 200                                          # the ack still lands
    assert ledger.calls[0]["on_behalf_of"] == "Practitioner/prov-someone-else"
    assert "on behalf of" in html_mod.unescape(r.text)                   # still declared delegated


def test_an_owner_that_already_has_a_display_is_not_looked_up(monkeypatch):
    """The resource answering the question already means no request is earned."""
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, ledger, ident = _rig_for(_addressed_task(owner="prov-someone-else"),
                                     provider_names=_NAMES)
    body = html_mod.unescape(client.get(f"/ack/task-7?sig={_sig()}", auth=_AUTH).text)
    assert "Dr Addressee" in body
    assert ident.name_lookups == []


def test_the_ordinary_same_person_ack_costs_no_lookup(monkeypatch):
    """The common path must not pay for the rare one: no mismatch, no request."""
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, _, ident = _rig_for(_addressed_task(owner="prov-ref", owner_display=None),
                                provider_names=_NAMES)
    client.get(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    client.post(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    assert ident.name_lookups == []


def test_the_name_is_read_with_the_callers_own_credential_not_a_service_account(monkeypatch):
    """worklist-api holds no OpenMRS credentials (#75 least privilege), and reading as the caller
    means this page can never surface what that clinician could not already read in the RIS."""
    monkeypatch.setenv("CRITCOM_ACK_HMAC_SECRET", _SECRET)
    client, _, ident = _rig_for(_addressed_task(**_LIVE_SHAPE), provider_names=_NAMES)

    client.get(f"/ack/task-7?sig={_sig()}", auth=_AUTH)
    _, basic_proof = ident.name_lookups[-1]
    assert basic_proof.auth == _AUTH and basic_proof.cookies is None

    client.cookies.set("JSESSIONID", "sess-live")
    client.get(f"/ack/task-7?sig={_sig()}")
    _, cookie_proof = ident.name_lookups[-1]
    assert cookie_proof.cookies == {"JSESSIONID": "sess-live"} and cookie_proof.auth is None
