"""The /ack/{task_id} surface (#79). TestClient + injected fakes, like test_api.py.

The ordering property is the load-bearing one: signature BEFORE identity (a forged link never
solicits credentials), identity BEFORE the ledger (an anonymous tap never reads the loop), and
an already-closed loop is never re-written.
"""
from __future__ import annotations

import httpx
import html as html_mod
import re
from urllib.parse import urljoin

import pytest
from fastapi.testclient import TestClient

from radagent_common.ack_link import sign_ack_task
from radagent_common.fhir_models import (
    Communication,
    CommunicationPayload,
    Reference,
    Task,
    TaskStatus,
)

from main import create_app

_SECRET = "ack-test-secret"


class FakeLedger:
    def __init__(self, task: Task | None = None):
        self.task = task
        self.completed: list[tuple[str, str]] = []
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
                                at_iso: str) -> Task:
        self.completed.append((task_id, acknowledged_by))
        done = self.task.model_copy(deep=True)
        done.status = TaskStatus.COMPLETED
        return done


class FakeIdentity:
    """Accepts exactly dr-ref/refpass (Basic) and sess-live (session cookie); records every
    attempt on each path so tests can pin WHEN and BY WHICH proof identity is consulted."""

    def __init__(self):
        self.attempts: list[str] = []
        self.session_attempts: list[str] = []

    async def whoami(self, username: str, password: str) -> str | None:
        self.attempts.append(username)
        if (username, password) == ("dr-ref", "refpass"):
            return "Dr Referrer (uuid-ref)"
        return None

    async def whoami_session(self, jsessionid: str) -> str | None:
        self.session_attempts.append(jsessionid)
        if jsessionid == "sess-live":
            return "Dr Referrer (uuid-ref)"
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
