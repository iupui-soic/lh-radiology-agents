# CLAUDE.md — Communications Agent

**Owner:** Pranathi (lead) · **Skills:** `comms.dispatch` / `comms.checkAck` / `comms.escalate` · **Port:** 8106 · **Stage:** Communicate

## What this is
The existing LH Communications service, being grown into the real **CritCom** (Critical-Results
Communication) agent (#52). The orchestrator hands off at COMMUNICATE and drives the
notify → check-ack → escalate loop. Three skills:
- **`comms.dispatch`** — classify the finding (ACR), page the right provider, open an ack clock.
- **`comms.checkAck`** — poll the acknowledgement Task; reports status + `overdue`.
- **`comms.escalate`** — escalate an unacknowledged critical result to the on-call provider.

## Contract
Per-skill schemas: `contracts/skills/comms.dispatch.schema.json`,
`comms.checkAck.schema.json`, `comms.escalate.schema.json` (each: top-level = output,
`$defs/input` = input). Card: `contracts/cards/communications.json`. The orchestrator acts on
`taskId` + `deadline` + escalate's `escalated` boolean; `acrCategory` is optional/informational.

## The two stores
Clinical context is READ from **fhir2** (`radagent_common.fhir_client`, read-only). The
notification and its ack are WRITTEN to the **comms ledger** (`radagent_common.comms_ledger`,
a separate HAPI FHIR server) — fhir2 implements neither `Communication` nor `PractitionerRole`.
See `comms_ledger.py` for the full reasoning.

`resolve_ordering_provider` carries `ServiceRequest.requester` **verbatim** and never dereferences
it: the order lives in fhir2 and the on-call directory in the ledger, so following that reference
across stores would be a guess about matching ids. Recording who we notified does not require
dialling them. The on-call provider IS dereferenced — `PractitionerRole` is ledger-native.

## The closed loop (#52 MR 3, real)
`comms.dispatch` classifies (ACR), and for a **critical** result writes a `Communication` ("we told
someone") + an ack `Task` ("did they answer"), returning `communicationId` / `taskId` / `deadline` /
`recipient`. A **routine** result posts to the EHR inbox and opens **no ack clock** — a timer on a
clean chest X-ray is how alert fatigue starts. `comms.checkAck` reads the Task; `comms.escalate`
marks it FAILED and opens a fresh loop on the on-call provider.

Ack windows: Cat1 = 60 min, Cat2 = 24 h (`CRITCOM_CAT{1,2}_ACK_TIMEOUT_MINUTES`).

**The agent never self-fires a timer.** It opens the clock and reports the deadline; the
orchestrator owns the durable wait and calls back (#52 MR 4).

## Specialty-routed on-call paging (#58)
Both on-call paths (dispatch's no-requester fallback and `comms.escalate`) derive a subspecialty
from `StudyContext.study` (modality + description) via `routing.py` and narrow the directory
search to it, so a critical intracranial finding pages neuro call — not whoever the directory
lists first. The mapping and the **out-of-specialty fallback dial** live in
`specialty-routing.yaml` beside the agent (CI-validated against
`contracts/specialty-routing.schema.json`; `SPECIALTY_ROUTING_PATH` overrides the path):
`any-on-call` pages whoever is on call and stamps the Communication with an
`http://critcom/routing|out-of-specialty` category; `none` pages nobody — the miss is recorded
(`SKIPPED` / `escalated: false`) and the study archives, with **no automatic human backstop**
(the #29 ladder pages only at the post-sign verification hold and does not react to dispatch
outcomes). The two fail in opposite directions — the dial is a safety call (issue #58 item 3),
so change its default only with PI sign-off. An unmapped study searches unnarrowed (pre-#58
behaviour); the ordering-provider path never consults routing.

## v1 vs later
- **v1:** the ACR classifier is deterministic — it reads Impression's `criticalFlags` and
  Verification's status, not the narrative (`classifier.py`). Channel delivery is still stubbed
  (no real EHR/pager/SMS I/O); what is real is the FHIR record of it.
- **#79 (behind `EHR_INBOX_WRITE_ENABLED`, default off):** the `ehr-inbox` channel for a
  CRITICAL result is real — `tools.deliver_critical_result_to_chart` writes an Observation into
  fhir2 (dedicated authorship concept, ack-task-marker idempotency, best-effort: a failure is a
  `FAILED` channel result, never a raise that would retry the dispatch and double-page). Routine
  results and #29 rung pass-throughs never write. The flag stays off until the PI write-path
  sign-off is recorded on #79; see `docs/ehr-inbox-notification.md`.
- **M3:** real pager/SMS delivery + the real Gemini ACR classifier behind the same `classify()`
  signature (the pattern `interpretation-assistant/registry.py` uses).

> ## Two gates. Do not double-page.
> **#29's ladder** = "the radiologist hasn't RESOLVED the verification hold on their signed
> report". Its fired rung arrives as an `escalation` input slice on `comms.dispatch`; the ladder
> already chose who/how and owns the cadence, so those channels are dispatched verbatim and
> **no ack clock is opened** — a comms-side Task would double-clock the same human.
> **checkAck/escalate** = "the ordering physician didn't ACK a communicated critical result".

## Optional LLM prose (`composer.py`)
The deterministic layer **decides** (category, recipient, deadline, escalation — the #78
model-free-trigger thesis); the composer only **writes** the physician-facing message in the
CritCom protocol format, with the category pre-decided. Invariants, all pinned in
`tests/test_composer.py`:
- `COMMS_LLM_COMPOSER` defaults **off** and is the only switch on either backend.
- **Two backends, one behaviour.** Unset `COMMS_LLM_BASE_URL` → the Gemini path
  (`COMMS_LLM_MODEL` defaults `gemini-2.5-flash-lite`, `GEMINI_API_KEY` required). Set it → any
  OpenAI-compatible `/chat/completions` endpoint; `COMMS_LLM_MODEL` is then required
  (half-set warns and falls back — it never silently reverts to Gemini) and `COMMS_LLM_API_KEY` is
  optional, since a local model wants none.
- Keys come from the operator's environment only — never a file, never a log line. `GEMINI_API_KEY`
  rides `x-goog-api-key`, `COMMS_LLM_API_KEY` rides `Authorization`; neither touches a URL.
- **Egress transport guard** (`COMMS_LLM_ALLOW_INSECURE`, same truthy set): `https` or a genuine
  loopback host is fine; plaintext to anything else is refused, because the finding label is
  cohort-derived. Note from a **container** nothing on the docker host is loopback, so a local
  model reached as `host.docker.internal` needs the opt-in — that is the real deployment case.
- **Fallback always**: flag off / no key / timeout (`COMMS_LLM_TIMEOUT_SECONDS`, default 5) /
  any error → the deterministic one-liner. The composer cannot fail or delay a page.
- **The 5s bound is a paging decision, not a backend one** — so the model is chosen to fit the
  bound, never the bound widened to fit the model. Measured on an ollama host, this prompt, all
  four replies accepted by the category check in every case:

  | model | cold (s) | warm p50 (s) | warm max (s) |
  |---|---|---|---|
  | `llama3.2:3b` | 5.2 | **2.8** | **3.1** |
  | `phi3` (3.8B) | 17.4 | 4.5 | 4.6 |
  | `qwen2.5:7b` | 10.3 | 5.0 | 5.4 |

  So a **3B-class model composes inside the bound with real headroom**, and a 7B one straddles
  it. Prose quality is not the discriminator here — every model's reply passed the
  contradicts-the-category check — so pick on latency.

  The **cold** column is a load, not an inference: ollama evicts an idle model (default ~5 min)
  and the next call pays to page it back in. So the first critical result after a quiet spell
  falls back to the deterministic one-liner even on the fastest model. That is the design working
  — nobody waits on a model — but it means a demo should warm the model at stack start (or pin it
  resident) rather than discover the cold path live. Note `phi3` costs more cold than `qwen2.5:7b`
  despite being half the size; size predicts warm latency, not load time.

  The default timeout stays **5**. Raising it is a safety change (a Cat1 page would wait that long
  on a model) and needs a PI call, not a config tweak.
- **Lean-reference prompt**: category + finding label + ack window. Never the report narrative,
  never patient/order identifiers — widening this sends PHI to an external API and needs a
  #30-style review first.
- Only the critical dispatch path composes; the #29 rung and routine results never consult it.
- **Prose cannot contradict the decision** (the #77 consistency precedent): composed text that
  names a different ACR category — or none — is rejected and the deterministic one-liner pages.
- The truthy set is byte-for-byte the family's (`{1,true,yes}` — !73 item 3). On the **Gemini**
  path `COMMS_LLM_MODEL` must be a plain model token (it rides the URL path; anything else falls
  back); on the OpenAI-compatible path it is a body field, so names like `qwen2.5:7b` are legal
  and that constraint is deliberately **not** applied.
- Wired: 7 env pass-throughs on the compose `communications` service, all default-empty (off).
- The chart write (#79's ehr-inbox) always carries the deterministic LABEL, never the composed
  prose — the Observation stays minimal-content whatever the composer produced.

## Run / test
`cd agents/communications && python -m pytest -q`
Run as a server: `uvicorn server:asgi_app --port 8106`

## Do NOT touch
Other agents, `orchestrator/`, the shared envelope, the A2A factory (`radagent_common/a2a.py`).
