"""comms skills: #17 channel routing, #29's rung pass-through, and the real #52 closed loop.

The closed loop is the whole point of this agent: a critical finding produces a Communication (we
told someone) and a Task (did they acknowledge), and an unanswered Task escalates to on-call. These
drive it end to end against in-memory ledger/fhir2 doubles, so the state that ends up in the ledger
is what is asserted -- not just the shape of the reply.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from jsonschema import Draft202012Validator

import handler
from handler import handle
from radagent_common import paths
from radagent_common.fhir_models import TaskStatus
from radagent_common.validation import validate_skill_output

from fakes import FakeFhir2, FakeLedger

SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_test",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"fhirServiceRequestId": "ServiceRequest/sr-1"},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}

ESCALATION_RUNG = {
    "level": 2, "targetRole": "on-call-radiologist", "channels": ["pager", "sms"],
    "urgency": "critical", "attempt": 1,
    "reason": "sign-off gate timed out awaiting radiologist",
}

CRITICAL = {"criticalFlags": [{"label": "aortic dissection", "severity": "critical"}]}


@pytest.fixture(autouse=True)
def stores(monkeypatch, tmp_path):
    """Every test gets fresh doubles; no test ever reaches a real fhir2 or ledger.

    Routing is pinned to a hermetic table too (via _routing_table below): the no-requester and
    escalate paths read the routing config on every call, so without the pin these tests would
    read the shipped clinical YAML -- or whatever SPECIALTY_ROUTING_PATH happens to be set to in
    the developer's shell -- and a legitimate table edit could break tests about unrelated
    behaviour. SAMPLE_CONTEXT matches no rule in the pinned table, so non-#58 tests see the
    pre-#58 unnarrowed search; the #58 tests re-point the table per test."""
    _routing_table(monkeypatch, tmp_path, "any-on-call")
    fhir, ledger = FakeFhir2(), FakeLedger()
    handler._FHIR, handler._LEDGER = fhir, ledger
    yield fhir, ledger
    handler._FHIR = handler._LEDGER = None


def _input_validator(skill_id: str) -> Draft202012Validator:
    """The skill's $defs/input schema. Nothing validates skill INPUTS in the pipeline yet -- these
    tests are what keep the input schemas honest until it does."""
    schema = json.loads(paths.skill_schema(skill_id).read_text())
    return Draft202012Validator(schema["$defs"]["input"])


def test_dispatch_input_schema_admits_the_payloads_the_orchestrator_sends():
    """The input schema is additionalProperties:false, so every key the orchestrator actually
    sends must be declared -- including the #29 `escalation` slice."""
    v = _input_validator("comms.dispatch")
    v.validate({"studyContext": SAMPLE_CONTEXT})
    v.validate({"studyContext": SAMPLE_CONTEXT, "escalation": ESCALATION_RUNG})
    v.validate({
        "studyContext": SAMPLE_CONTEXT,
        "report": {"diagnosticReportId": "DiagnosticReport/1"},
        "impression": {"criticalFlags": []},
        "verification": {"verificationStatus": "PASS"},
    })


# --- routine: no ack clock ------------------------------------------------------------

async def test_routine_result_posts_to_the_inbox_and_opens_no_ack_clock(stores):
    """A normal study is not a critical result. Opening an ack clock on it is how alert fatigue
    starts -- and it would put a physician on a 60-minute timer for a clean chest X-ray."""
    _, ledger = stores
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("comms.dispatch", out)

    assert out["dispatchStatus"] == "SENT"
    assert out["acrCategory"] == "None"
    assert [c["channel"] for c in out["channelResults"]] == ["ehr-inbox"]
    assert "taskId" not in out                      # no clock
    assert ledger.communications == {} and ledger.tasks == {}   # nothing written


# --- critical: the closed loop --------------------------------------------------------

async def test_critical_finding_records_a_communication_and_opens_the_ack_clock(stores):
    fhir, ledger = stores
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)

    assert out["acrCategory"] == "Cat1"
    assert [c["channel"] for c in out["channelResults"]] == ["ehr-inbox", "oncall-pager"]
    assert out["recipient"] == "Practitioner/dr-order"       # the ordering physician
    assert fhir.orders_read == ["ServiceRequest/sr-1"]

    # The Communication says what we told, and about which order.
    comm = ledger.communications[out["communicationId"]]
    assert comm.subject.reference == "Patient/1"
    assert comm.basedOn[0].reference == "ServiceRequest/sr-1"
    assert comm.recipient[0].reference == "Practitioner/dr-order"
    assert comm.finding_summary == "aortic dissection"

    # The Task is the open loop: it points AT that Communication and is owned by the recipient.
    task = ledger.tasks[out["taskId"]]
    assert task.focus.reference == f"Communication/{comm.id}"
    assert task.owner.reference == "Practitioner/dr-order"
    assert task.status is TaskStatus.REQUESTED
    # Cat1 = contact within 60 minutes.
    assert task.restriction.period.end - task.restriction.period.start == timedelta(minutes=60)
    assert out["deadline"] == task.restriction.period.end.isoformat()


async def test_failed_verification_is_cat2_with_the_slower_clock(stores):
    _, ledger = stores
    out = await handle("comms.dispatch", {
        "studyContext": SAMPLE_CONTEXT,
        "verification": {"verificationStatus": "FAIL", "requiresHumanReview": True},
    })
    validate_skill_output("comms.dispatch", out)
    assert out["acrCategory"] == "Cat2"
    task = ledger.tasks[out["taskId"]]
    assert task.restriction.period.end - task.restriction.period.start == timedelta(minutes=1440)


async def test_no_requester_falls_back_to_on_call_rather_than_dropping_it(stores):
    """A study ingested with an unresolved order (#11) has no requester. A critical finding with
    nobody to tell must not be silently dropped."""
    fhir, ledger = stores
    fhir.requester = None
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)
    assert out["dispatchStatus"] == "SENT"
    assert out["recipient"] == "Practitioner/dr-oncall"


async def test_nobody_to_tell_reports_skipped_not_sent(stores):
    """No requester AND no on-call. SENT would claim a page that never happened -- and a claimed
    page is worse than an admitted failure, because nobody goes looking for it."""
    fhir, ledger = stores
    fhir.requester = None
    ledger.on_call = None
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)
    assert out["dispatchStatus"] == "SKIPPED"
    assert out["channelResults"] == []
    assert ledger.communications == {}       # nothing recorded, because nothing was sent


# --- the OTHER gate: #29's sign-off rung ----------------------------------------------

async def test_signoff_rung_dispatches_its_channels_and_opens_NO_ack_clock(stores):
    """#29's ladder pages the radiologist whose SIGNED report sits at the verification hold.
    The ladder owns its own cadence, so a Communication/Task here would put the same human on
    two clocks at once."""
    _, ledger = stores
    payload = {"studyContext": SAMPLE_CONTEXT, "escalation": ESCALATION_RUNG}
    _input_validator("comms.dispatch").validate(payload)
    out = await handle("comms.dispatch", payload)
    validate_skill_output("comms.dispatch", out)

    assert [c["channel"] for c in out["channelResults"]] == ["pager", "sms"]
    assert "taskId" not in out
    assert ledger.communications == {} and ledger.tasks == {}   # no ack loop opened


# --- comms.checkAck -------------------------------------------------------------------

async def test_check_ack_reports_pending_before_the_deadline(stores):
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    out = await handle("comms.checkAck",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.checkAck", out)
    assert out["ackStatus"] == "REQUESTED"
    assert out["overdue"] is False


async def test_check_ack_reports_overdue_once_the_deadline_passes(stores):
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    task = ledger.tasks[sent["taskId"]]
    task.restriction.period.end = datetime.now(tz=timezone.utc) - timedelta(minutes=1)

    out = await handle("comms.checkAck",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.checkAck", out)
    assert out["ackStatus"] == "OVERDUE"
    assert out["overdue"] is True


async def test_an_acknowledged_task_is_never_overdue(stores):
    """A physician who acknowledged is done, deadline or not. Reporting OVERDUE here would
    escalate a result that already landed."""
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    task = ledger.tasks[sent["taskId"]]
    task.status = TaskStatus.COMPLETED
    task.restriction.period.end = datetime.now(tz=timezone.utc) - timedelta(minutes=1)

    out = await handle("comms.checkAck",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.checkAck", out)
    assert out["ackStatus"] == "COMPLETED"
    assert out["overdue"] is False


# --- comms.escalate -------------------------------------------------------------------

async def test_escalate_fails_the_open_loop_and_opens_a_new_one_on_call(stores):
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.escalate", out)

    assert out["escalated"] is True
    # The original loop is closed as FAILED -- the audit fact a chart review needs: this
    # notification was never acknowledged by the person it was sent to.
    assert ledger.tasks[sent["taskId"]].status is TaskStatus.FAILED
    # A new loop is open on the on-call provider, carrying the SAME urgency and finding.
    new_comm = ledger.communications[out["newCommunicationId"]]
    assert new_comm.recipient[0].reference == "Practitioner/dr-oncall"
    assert new_comm.category[0].coding[0].code == "Cat1"
    assert "aortic dissection" in new_comm.finding_summary
    assert new_comm.finding_summary.startswith("[ESCALATED]")
    new_task = ledger.tasks[out["newTaskId"]]
    assert new_task.owner.reference == "Practitioner/dr-oncall"
    assert new_task.status is TaskStatus.REQUESTED


async def test_an_escalated_cat2_gets_the_SHORT_window_not_another_24_hours(stores):
    """The escalation window is deliberately NOT the category's window (#52, @sunbiz).

    A Cat2 result must be communicated within 24h. If nobody acknowledges and we escalate, handing
    on-call a fresh 24h clock would mean the finding could take 48 hours to land -- the escalation
    would make the deadline WORSE. An escalated result is already late, so it runs on one short
    window regardless of category.

    This is also the fix for a dead read: _escalate used to take its window from
    payload.get("ackMinutes"), but comms.escalate's input schema admits only studyContext + taskId
    with additionalProperties false, so that read could never receive data and every escalated loop
    silently took a 60-minute fallback nobody had chosen.
    """
    _, ledger = stores
    # FAIL + requiresHumanReview -> Cat2, whose own ack window is 24 hours.
    sent = await handle("comms.dispatch", {
        "studyContext": SAMPLE_CONTEXT,
        "verification": {"verificationStatus": "FAIL", "requiresHumanReview": True, "issues": []},
    })
    assert sent["acrCategory"] == "Cat2"
    original = ledger.tasks[sent["taskId"]].restriction.period.end

    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.escalate", out)
    escalated = ledger.tasks[out["newTaskId"]].restriction.period.end

    # The original clock really was the Cat2 window (~24h), and the escalated one is ~1h.
    assert original - datetime.now(timezone.utc) > timedelta(hours=20)
    assert escalated - datetime.now(timezone.utc) < timedelta(hours=2)
    assert escalated < original, "escalation must not push the deadline further out"


async def test_the_escalation_window_is_configurable(stores, monkeypatch):
    """Same knob shape as the per-category windows (CRITCOM_CAT*_ACK_TIMEOUT_MINUTES): a deployment
    that chases harder or softer sets it, rather than editing a hardcoded 60."""
    monkeypatch.setenv("CRITCOM_ESCALATION_ACK_TIMEOUT_MINUTES", "15")
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})

    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    escalated = ledger.tasks[out["newTaskId"]].restriction.period.end

    assert escalated - datetime.now(timezone.utc) < timedelta(minutes=16)


async def test_escalate_with_nobody_on_call_says_so_instead_of_claiming_success(stores):
    """An unescalatable critical result is exactly the thing a human must hear about. Returning
    escalated=true with no recipient would bury it."""
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    ledger.on_call = None

    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.escalate", out)
    assert out["escalated"] is False
    assert "on-call" in out["reason"]
    assert ledger.tasks[sent["taskId"]].status is TaskStatus.FAILED   # still not acknowledged


async def test_unexpected_skill_raises():
    with pytest.raises(ValueError):
        await handle("comms.sendCarrierPigeon", {"studyContext": SAMPLE_CONTEXT})


# --- #58: specialty-routed on-call paging ----------------------------------------------

NEURO_CONTEXT = {**SAMPLE_CONTEXT,
                 "study": {**SAMPLE_CONTEXT["study"], "studyDescription": "CT head w/o contrast"}}

OUT_OF_SPECIALTY = "out-of-specialty"


def _routing_table(monkeypatch, tmp_path, fallback: str):
    """A hermetic routing table: 'head' -> neuro, dial as given. The handler tests must not
    couple to the shipped table's keyword list -- that is clinical config under PI review."""
    p = tmp_path / "routing.yaml"
    p.write_text(
        'schemaVersion: "1.0.0"\n'
        f"outOfSpecialtyFallback: {fallback}\n"
        "rules:\n"
        "  - specialty: neuro\n"
        "    keywords: [head]\n"
    )
    monkeypatch.setenv("SPECIALTY_ROUTING_PATH", str(p))


def _marker_codes(comm) -> list[str]:
    return [c.code for cc in comm.category for c in cc.coding
            if c.system == "http://critcom/routing"]


async def test_on_call_fallback_pages_the_studys_own_specialty(stores, monkeypatch, tmp_path):
    """A neuro study with no requester pages NEURO call -- and the test asserts the search was
    actually narrowed, not that the right person happened to come back first."""
    _routing_table(monkeypatch, tmp_path, "any-on-call")
    fhir, ledger = stores
    fhir.requester = None
    ledger.on_call, ledger.on_call_specialty = "Practitioner/dr-neuro", "neuro"

    out = await handle("comms.dispatch", {"studyContext": NEURO_CONTEXT, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)
    assert out["recipient"] == "Practitioner/dr-neuro"
    assert ledger.on_call_searches == ["neuro"]
    # In-specialty: the record carries no routing marker.
    assert _marker_codes(ledger.communications[out["communicationId"]]) == []


async def test_nobody_in_specialty_pages_any_on_call_and_stamps_the_record(stores, monkeypatch,
                                                                            tmp_path):
    """The any-on-call direction: someone hears it, and the Communication says it was the wrong
    someone. The marker is APPENDED -- the ACR category must stay at category[0], where readers
    (and _escalate's re-derive) take it from."""
    _routing_table(monkeypatch, tmp_path, "any-on-call")
    fhir, ledger = stores
    fhir.requester = None           # on-call fallback; the general rota is untagged

    out = await handle("comms.dispatch", {"studyContext": NEURO_CONTEXT, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)
    assert out["dispatchStatus"] == "SENT"
    assert out["recipient"] == "Practitioner/dr-oncall"
    assert ledger.on_call_searches == ["neuro", None]      # narrowed miss, then the fallback
    comm = ledger.communications[out["communicationId"]]
    assert comm.category[0].coding[0].code == "Cat1"
    assert _marker_codes(comm) == [OUT_OF_SPECIALTY]


async def test_fallback_none_pages_nobody_and_reports_skipped(stores, monkeypatch, tmp_path):
    """The other direction of the dial: the policy says never page out of specialty, so the
    dispatch reports the miss honestly (SKIPPED, nothing written). Nothing re-pages in response
    -- under `none` the miss is only as loud as this record."""
    _routing_table(monkeypatch, tmp_path, "none")
    fhir, ledger = stores
    fhir.requester = None

    out = await handle("comms.dispatch", {"studyContext": NEURO_CONTEXT, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)
    assert out["dispatchStatus"] == "SKIPPED"
    assert ledger.on_call_searches == ["neuro"]            # no unnarrowed second search
    assert ledger.communications == {} and ledger.tasks == {}


async def test_an_unmapped_study_searches_unnarrowed_with_no_marker(stores, monkeypatch,
                                                                    tmp_path):
    """No rule matches SAMPLE_CONTEXT (CT, no description): the search runs exactly as before
    #58 and the record carries no marker. A site with one general rota is unaffected."""
    _routing_table(monkeypatch, tmp_path, "any-on-call")
    fhir, ledger = stores
    fhir.requester = None

    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    assert out["recipient"] == "Practitioner/dr-oncall"
    assert ledger.on_call_searches == [None]
    assert _marker_codes(ledger.communications[out["communicationId"]]) == []


async def test_the_ordering_provider_path_never_consults_routing(stores, monkeypatch, tmp_path):
    """#58 touches ONLY the two on-call paths. With a requester on the order, the notification
    goes to them: no directory search, no marker -- whatever the study maps to."""
    _routing_table(monkeypatch, tmp_path, "any-on-call")
    _, ledger = stores

    out = await handle("comms.dispatch", {"studyContext": NEURO_CONTEXT, "impression": CRITICAL})
    assert out["recipient"] == "Practitioner/dr-order"
    assert ledger.on_call_searches == []
    assert _marker_codes(ledger.communications[out["communicationId"]]) == []


async def test_escalation_pages_the_studys_specialty_first(stores, monkeypatch, tmp_path):
    _routing_table(monkeypatch, tmp_path, "any-on-call")
    _, ledger = stores
    ledger.on_call, ledger.on_call_specialty = "Practitioner/dr-neuro", "neuro"
    sent = await handle("comms.dispatch", {"studyContext": NEURO_CONTEXT, "impression": CRITICAL})

    out = await handle("comms.escalate", {"studyContext": NEURO_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.escalate", out)
    assert out["escalated"] is True
    assert ledger.on_call_searches == ["neuro"]
    new_comm = ledger.communications[out["newCommunicationId"]]
    assert new_comm.recipient[0].reference == "Practitioner/dr-neuro"
    assert _marker_codes(new_comm) == []


async def test_escalation_out_of_specialty_stamps_the_record_and_keeps_the_category(
        stores, monkeypatch, tmp_path):
    _routing_table(monkeypatch, tmp_path, "any-on-call")
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": NEURO_CONTEXT, "impression": CRITICAL})

    out = await handle("comms.escalate", {"studyContext": NEURO_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.escalate", out)
    assert out["escalated"] is True
    assert "out of specialty" in out["reason"]
    new_comm = ledger.communications[out["newCommunicationId"]]
    assert new_comm.recipient[0].reference == "Practitioner/dr-oncall"
    assert new_comm.category[0].coding[0].code == "Cat1"   # the re-derived urgency, still first
    assert _marker_codes(new_comm) == [OUT_OF_SPECIALTY]
    assert new_comm.finding_summary.startswith("[ESCALATED]")


async def test_escalation_with_an_empty_directory_names_the_directory_not_the_policy(
        stores, monkeypatch, tmp_path):
    """Under any-on-call, an empty directory is an empty-directory miss. The reason must not
    blame a policy that never declined anything -- the two are different audit facts, and this
    is the only test that can tell escalate's two failure reasons apart."""
    _routing_table(monkeypatch, tmp_path, "any-on-call")
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": NEURO_CONTEXT, "impression": CRITICAL})
    ledger.on_call = None

    out = await handle("comms.escalate", {"studyContext": NEURO_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.escalate", out)
    assert out["escalated"] is False
    assert ledger.on_call_searches == ["neuro", None]   # narrowed miss, then the fallback tried
    assert "configured in the ledger" in out["reason"]
    assert "does not page out of specialty" not in out["reason"]


async def test_escalation_under_fallback_none_reports_the_miss_honestly(stores, monkeypatch,
                                                                         tmp_path):
    """escalated=false with a reason that names the policy: "nobody was in specialty and we chose
    not to page out of it" is a different audit fact from "the directory is empty"."""
    _routing_table(monkeypatch, tmp_path, "none")
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": NEURO_CONTEXT, "impression": CRITICAL})

    out = await handle("comms.escalate", {"studyContext": NEURO_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.escalate", out)
    assert out["escalated"] is False
    assert "neuro" in out["reason"] and "out of specialty" in out["reason"]
    assert ledger.tasks[sent["taskId"]].status is TaskStatus.FAILED   # still not acknowledged


# --- #79: the ehr-inbox channel goes real behind the flag -----------------------------

async def test_inbox_write_disabled_keeps_the_stub_semantics(stores, monkeypatch):
    """Default-off: channelResults byte-identical to the pre-#79 stub, and NO chart write. The
    flag flip is a deployment decision gated on the PI sign-off recorded on #79 -- until then this
    change must be invisible."""
    monkeypatch.delenv("EHR_INBOX_WRITE_ENABLED", raising=False)   # hermetic vs the dev shell
    fhir, _ = stores
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)
    assert [(c["channel"], c["status"]) for c in out["channelResults"]] == [
        ("ehr-inbox", "SENT"), ("oncall-pager", "SENT")]
    assert fhir.notifications_written == []


async def test_inbox_write_enabled_delivers_the_notification_into_the_chart(stores, monkeypatch):
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    fhir, _ = stores
    ctx = {**SAMPLE_CONTEXT, "study": {**SAMPLE_CONTEXT["study"], "accessionNumber": "ACC-1"}}
    out = await handle("comms.dispatch", {"studyContext": ctx, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)

    assert [(c["channel"], c["status"]) for c in out["channelResults"]] == [
        ("ehr-inbox", "SENT"), ("oncall-pager", "SENT")]
    (written,) = fhir.notifications_written
    assert written["ack_task_id"] == out["taskId"]     # the chart entry names ITS ack loop
    assert written["finding"] == "aortic dissection"
    assert written["accession"] == "ACC-1"             # the order correlation (no basedOn on obs)
    assert written["patient_ref"] == "Patient/1"
    assert written["sent_iso"] == out["dispatchedAt"]


async def test_inbox_write_failure_reports_failed_and_never_costs_the_page(stores, monkeypatch):
    """Best-effort is load-bearing: a chart-write failure must not raise past dispatch (the
    orchestrator would retry the whole activity and page the same human twice) and must not be
    reported as a delivered notification."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    fhir, ledger = stores
    fhir.fail_notification_write = True
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)

    assert out["dispatchStatus"] == "SENT"             # the page went out
    assert out["taskId"] in ledger.tasks               # the ack clock is still open
    assert [(c["channel"], c["status"]) for c in out["channelResults"]] == [
        ("ehr-inbox", "FAILED"), ("oncall-pager", "SENT")]


async def test_routine_result_never_writes_to_the_chart_even_when_enabled(stores, monkeypatch):
    """#79 is CRITICAL-result delivery. A routine study writes nothing -- its signed report
    already lives in the EHR, and notifying every normal would be chart noise (alert fatigue in
    new clothes)."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    fhir, _ = stores
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("comms.dispatch", out)
    assert [c["channel"] for c in out["channelResults"]] == ["ehr-inbox"]
    assert fhir.notifications_written == []


async def test_escalation_rung_never_writes_to_the_chart_even_when_enabled(stores, monkeypatch):
    """A #29 sign-off rung is the OTHER gate: its channels are dispatched verbatim and it has no
    ack task for a chart entry to name. The #79 write must not leak into that path."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    fhir, _ = stores
    out = await handle("comms.dispatch",
                       {"studyContext": SAMPLE_CONTEXT, "escalation": ESCALATION_RUNG})
    validate_skill_output("comms.dispatch", out)
    assert [c["channel"] for c in out["channelResults"]] == ["pager", "sms"]
    assert fhir.notifications_written == []


# --- the escalated loop must be acknowledgeable (#123) -------------------------------------
#
# Found driving arc 2 in a browser: after a Cat1 escalation the chart entry still named the task
# the escalation had just FAILED, and the escalated page's whole payload was "[ESCALATED]
# <finding>" with no link. Between them, an escalated critical result could not be acknowledged
# by anyone: the first recipient's link was dead and the second never got one.

async def test_escalation_redelivers_the_chart_entry_naming_the_new_task(stores, monkeypatch):
    """The chart entry is anchored on the accession, so escalating must UPDATE it to name the
    LIVE ack task. Without this the physician's only link pointed at the failed one."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    fhir, _ = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    first = fhir.notifications_written[-1]
    assert first["ack_task_id"] == sent["taskId"]

    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.escalate", out)

    assert len(fhir.notifications_written) == 2, "escalation must re-deliver to the chart"
    latest = fhir.notifications_written[-1]
    assert latest["ack_task_id"] == out["newTaskId"]    # the LIVE loop, not the failed one
    assert latest["ack_task_id"] != sent["taskId"]
    # same accession => write_critical_result_notification updates the entry in place
    assert latest["accession"] == first["accession"]


async def test_the_escalated_chart_entry_keeps_the_plain_finding(stores, monkeypatch):
    """The Communication is prefixed "[ESCALATED] " for the pager. The chart entry is the record
    of THIS critical result, one per accession, so rewriting its text every rung would churn the
    patient's chart to say nothing new."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    fhir, _ = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})

    escalated = [n for n in fhir.notifications_written if n["ack_task_id"] == out["newTaskId"]]
    assert escalated, "no chart entry was written for the escalated task"
    assert not escalated[-1]["finding"].startswith("[ESCALATED]")


async def test_a_failed_escalation_writes_no_chart_entry(stores, monkeypatch):
    """Nobody on call means no new loop, so there is no live task to point the chart at. Leave
    the entry alone rather than blanking the link the physician still has."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    fhir, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    before = len(fhir.notifications_written)
    ledger.on_call = None                       # empty directory

    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})

    assert out["escalated"] is False
    assert len(fhir.notifications_written) == before


async def test_a_chart_write_failure_never_costs_the_escalation(stores, monkeypatch):
    """Best-effort, exactly like dispatch's: by this point the page and the ledger record exist,
    so raising would fail the activity and Temporal would page on-call twice."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    fhir, _ = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    fhir.fail_notification_write = True

    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})

    validate_skill_output("comms.escalate", out)
    assert out["escalated"] is True
    assert out["newTaskId"]


# --- the escalation marker must not compound (#123) ----------------------------------------

def test_plain_finding_strips_accumulated_markers():
    from handler import _plain_finding
    assert _plain_finding("pneumothorax") == "pneumothorax"
    assert _plain_finding("[ESCALATED] pneumothorax") == "pneumothorax"
    assert _plain_finding("[ESCALATED] [ESCALATED] pneumothorax") == "pneumothorax"
    assert _plain_finding("  [ESCALATED]   [ESCALATED]  aortic dissection") == "aortic dissection"
    assert _plain_finding("") == ""
    # only a LEADING marker is decoration; the word inside the finding text is left alone
    assert _plain_finding("pneumothorax [ESCALATED]") == "pneumothorax [ESCALATED]"


async def test_a_second_escalation_does_not_double_the_marker(stores):
    """Seen live on the demo host: the acknowledgement receipt a physician read said
    "[ESCALATED] [ESCALATED] pneumothorax". Each rung re-reads the previous Communication's
    finding and prepends again, so the marker compounded once per rung."""
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})

    first = await handle("comms.escalate",
                         {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    second = await handle("comms.escalate",
                          {"studyContext": SAMPLE_CONTEXT, "taskId": first["newTaskId"]})
    validate_skill_output("comms.escalate", second)

    summary = ledger.communications[second["newCommunicationId"]].finding_summary
    assert summary.count("[ESCALATED]") == 1, summary
    assert "aortic dissection" in summary


async def test_the_chart_entry_carries_no_marker_even_after_two_rungs(stores, monkeypatch):
    """The #123 chart write passes the finding straight through, so an accumulating marker would
    have landed in the patient's chart too."""
    monkeypatch.setenv("EHR_INBOX_WRITE_ENABLED", "1")
    fhir, _ = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    first = await handle("comms.escalate",
                         {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    second = await handle("comms.escalate",
                          {"studyContext": SAMPLE_CONTEXT, "taskId": first["newTaskId"]})

    entry = [n for n in fhir.notifications_written if n["ack_task_id"] == second["newTaskId"]][-1]
    assert "[ESCALATED]" not in entry["finding"]
