# Post-ack follow-up check (#139 step 2)

**Off by default.** Nothing below happens unless `FOLLOWUP_CHECK_HOURS` is set on the orchestrator.

## Why

Acknowledging a critical result is not acting on it (SIIM CAIMI26 feedback, #139). The ack loop
(#52) proves that someone saw the result. This check looks for evidence that someone did something
about it.

## What it does

When a study's critical-result loop ends **acknowledged**, the study archives exactly as before and
starts one `FollowUpCheckWorkflow` (workflow id `<study workflow id>-followup`). That workflow:

1. waits `FOLLOWUP_CHECK_HOURS`, counted from when the result was communicated;
2. asks fhir2 for the patient's resources of `FOLLOWUP_EVIDENCE_TYPES` last updated since then,
   leaving out the study's own order (the RIS updates it on sign-off);
3. if it finds any, ends with `FOUND` and the references; if it finds none, sends **one** reminder
   to the ordering physician through the same `comms.dispatch` escalation path the sign-off ladder
   uses, on `FOLLOWUP_REMINDER_CHANNELS`, and ends with `NOT_FOUND`;
4. if fhir2 cannot be searched, ends with `CHECK_FAILED` and sends **no** reminder: "could not
   look" is not "nothing found".

An unacknowledged result is not checked (the ack loop has already escalated it), and neither is a
routine one. The check runs in its own workflow so the study still finishes on time; a report
addendum arriving after archive keeps reaching the dead-letter path, as before.

## Settings (orchestrator)

| Variable | Default | Meaning |
|---|---|---|
| `FOLLOWUP_CHECK_HOURS` | unset (off) | Hours to wait. Must be more than 0 and at most 720 (a month); anything else leaves the check off. |
| `FOLLOWUP_EVIDENCE_TYPES` | `ServiceRequest,Encounter,Procedure` | Which new fhir2 resources count as action. Unknown names are ignored; none valid leaves the check off. |
| `FOLLOWUP_REMINDER_CHANNELS` | `in-app` | Reminder channels, from the escalation contract's list (`in-app`, `email`, `sms`, `pager`, `phone`). |

## What the reminder is today

The reminder rides the same `comms.dispatch` escalation path as the sign-off ladder's pages, and
that path does not yet deliver to a person: the Communications Agent records the page and answers
`SENT` per channel, as it does for the ladder. The workflow outcome (`NOT_FOUND`, reminded) and the
orchestrator's `ESCALATE` log line are the record. Real delivery (for example into the chart, the
way the critical result itself is delivered) is a Communications Agent change that both the ladder
and this check need.

## Decisions still open (PI)

- **The window**: how long after the result is "no action" a finding.
- **What counts as action**: the defaults are the three resource types #139 names. Check them
  against the live fhir2 first: an Encounter or ServiceRequest that the pipeline or the RIS itself
  touches would read as action, and a type this fhir2 build does not expose simply never counts.
- **The acknowledger's answer**: the #139 step 1 ack page records what was done on the Task note
  (for example "already known, no action needed"). This check does not read it yet; that needs
  `comms.checkAck` to return it, a contract change best made once step 1 is merged.
- **Who is reminded**: the ordering physician, once. Repeats or escalation beyond that are not built.
