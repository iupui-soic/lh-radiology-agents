# M4 showcase run-book: the step-by-step demo script (#76)

The day-of script. Every step names the exact location (URL, screen, or command) and the
expected on-screen result. Arcs and rationale come from the #76 draft; backend beats were
live-verified in the #72 restart drill, the click path in the #73 browser drill, and the
viewer arc on the built !100 image.

Throughout, `https://demo.example.org` stands for `https://$DEMO_DOMAIN` — the single public
origin the #75 Caddy overlay serves. Nothing else is reachable off-box.

## 0. The location map

| What | Exact location | Auth |
|---|---|---|
| Reading worklist | `https://demo.example.org/reading` | proxy login (`DEMO_PROXY_USER`) |
| Viewer (reading mode) | `https://demo.example.org/read?...` — reached ONLY by clicking a worklist row | same origin, same login |
| RIS / OpenMRS login | `https://demo.example.org/openmrs/login.htm` | radiologist's own OpenMRS account |
| RIS order page (read-only) | `https://demo.example.org/openmrs/module/radiology/radiologyOrder.form?orderId=<uuid>` — reached via the viewer's **Report this study** action. Shows the order and any pre-sign draft. It carries **no claim button** on this build (#109), and its "View Study" link points at a `localhost:8081` Weasis URL that is dead from any reviewer's browser: ignore it, the viewer is the `/read` route | OpenMRS session |
| RIS report form (claim, author, sign) | `https://demo.example.org/openmrs/module/radiology/radiologyReport.form?orderId=<uuid>` — **opening this URL is the claim**: it creates the draft and redirects to `?reportId=<n>` | OpenMRS session |
| Patient chart (referring MD) | `https://demo.example.org/openmrs` → find patient → chart shows the **AI critical result notification** entry | physician's own OpenMRS account |
| Critical-result ack (phone) | `https://demo.example.org/reading-api/ack/<taskId>?sig=…` — the signed link inside the chart notification | physician's OpenMRS account (live session if the link sits under `/openmrs`, else an HTTP Basic prompt) |
| Sign-off override (phone) | `https://demo.example.org/ingress/signoff/<workflowId>/override` — the link inside the escalation page | `SIGNOFF_OVERRIDE_TOKEN` |
| Jaeger (choreography visual) | presenter laptop: `ssh -L 16686:127.0.0.1:16686 demo@<host>` → `http://localhost:16686` | SSH only (loopback-bound on the host) |
| Temporal UI (backstage only) | tunnel `8088` the same way → `http://localhost:8088` | SSH only |
| Restage/seeder commands | SSH shell on the demo host, repo root | host account |

## 1. Prerequisites (verify the morning of; details in the #76 comment)

1. Stack up under the overlay:
   `SIGNOFF_OVERRIDE_TOKEN=… A2A_CALLBACK_TOKEN=… docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d`
   (plus `--profile otel` for the Jaeger visual). Compose refuses dev-default secrets.
2. #68 cohort loaded (FHIR → DICOM → `link_radiology_studies.py`), referring physicians seeded (!97).
3. Flags on, each with its recorded sign-off: `ORTHANC_PRESIGN_WRITE_ENABLED=1`,
   `EHR_INBOX_WRITE_ENABLED=1`, `PATCH_PRESIGN_IMPRESSION`, `CRITCOM_ACK_HMAC_SECRET` set and
   `CRITCOM_ACK_BASE_URL=https://demo.example.org/reading-api`, LLM keys for impression/comms
   prose (both degrade to deterministic text if unset). `CRITCOM_ACK_HMAC_SECRET` must be the
   SAME value on `communications` (which signs the link) and `worklist-api` (which verifies it);
   both read it, and verification fails closed when it is empty. The `/reading-api` base URL
   means the ack asks for a login rather than reusing the physician's OpenMRS session: see arc
   2 step 6 for why, and what to change if you want the one-click path.
4. Accounts: each radiologist has their own OpenMRS user; the referring-physician demo account
   password is known; the ack/override phone is on wifi that can reach the demo origin.
4a. Referring-physician access (#85) is bootstrapped, not hand-built: the
   `referring-role-bootstrap` one-shot (runs on every `up`) fills the module's zero-privilege
   `Radiology: Referring physician` role with the proven read-only set. The `dr.reyes` /
   `dr.okafor` / `dr.novak` logins (password: `MIMIC_REFERRER_PASSWORD`) come from the cohort
   ETL at load time; `BOOTSTRAP_DEMO_REFERRERS=1` makes the bootstrap provision and converge
   them too, but that half needs user-admin-capable creds on the bootstrap service (the host's
   least-privilege service account 403s; see the compose comment). Entry path for these logins
   is a `patientDashboard.form?patientId=<id>` DEEP LINK (the notification link in arc 2 is
   one): the legacy home page 500s for them (upstream errorhandler.jsp serialization bug), while
   the patient dashboard, its Radiology tab included, renders fine. Do not "fix" a referrer
   login by granting admin; re-run the bootstrap instead and check its log line
   (`role 'Radiology: Referring physician': N granted, ...`).
5. OpenMRS seed captured once (`scripts/dump_openmrs_seed.sh`) so recovery never costs the
   16-minute boot.
5a. Radiology-module vendor assets fetched once per host (`docker/caddy/fetch-radiology-vendor.sh`,
   network required): the omod ships without them and every RIS page dies on "jQuery is not
   defined" until Caddy can serve them (#75 overlay; real fix is the o3 omod build).
5b. `ris-sign-bridge` is up (`docker compose ps ris-sign-bridge`): the module's sign emit is
   broken (ServiceNotFoundException, swallowed), so without the bridge a signed report never
   reaches fhir2/the poller and every read parks at the gate (workaround for #70; real fix o3).
6. Smoke: `https://demo.example.org/` → 401 without the proxy login; `/reading` lists the
   cohort after login; one seeded `report_seeder.py finalize` releases a test study end to end.

## 2. Arc 1 — routine clear CXR (~3 min): the fast path

1. **Restage** (SSH shell, repo root): re-push one normal cohort study (or reset its workflow via
   the seeder). Say out loud: "nothing below is a typed URL; the pipeline drives the screens."
2. **Browser →** `https://demo.example.org/reading`. Expected: the study appears within one
   refresh cycle, tier **ROUTINE**, no AI badge.
3. **Click the row.** Expected: URL changes to `/read?...&hangingProtocolId=lhrad.cxr.two-view`,
   PA + lateral hang side by side automatically, right panel open, findings banner shows no
   COMPLETE finding.
4. **Report this study** (right panel / toolbar). Expected: popup lands on
   `/openmrs/module/radiology/radiologyOrder.form?orderId=<uuid>`, which shows the order.
   **There is no Claim Report button on this build (#109).** Claim by changing `radiologyOrder`
   to `radiologyReport` in that URL, keeping the same `orderId`: that creates the draft and
   redirects to `?reportId=<n>`. Author a normal report (FINDINGS + IMPRESSION sections), set
   **Results Interpreter** to yourself, then **Complete**, which is the sign. Expected: the page
   returns with "Report completed" and status **Completed**.
5. **Narrate the silence:** poller joins the final DiagnosticReport within one cycle,
   verification runs post-sign and PASSes, **no page goes out** — the alert-fatigue point.
6. **Jaeger** (`http://localhost:16686` over the tunnel): pick the study's trace, show the
   ingest → triage → worklist → sign → verify choreography as one waterfall.

## 3. Arc 2 — pneumothorax, the full closed loop (~7 min): the centerpiece

1. **Restage** a pneumothorax-positive cohort study whose order carries the J93*/J95.811 reason
   code (STAT). Expected on `/reading`: it lands at the **top**, tier STAT.
2. **Before anyone reads**, RIS window at
   `/openmrs/module/radiology/radiologyOrder.form?orderId=<uuid>`: the pre-sign **preliminary**
   DiagnosticReport (authorship-stamped draft impression) is already there. Point at it: the AI
   drafted before the human opened the study, and it can only ever overwrite its own draft.
3. **Worklist row click →** `/read?...`: PA + lateral hang, right panel already open, banner
   reads "Pneumothorax screening signal (not a read): positive at p=…" with zero clicks; show
   the CAD evidence overlay.
4. **Report this study →** switch `radiologyOrder.form` to `radiologyReport.form` in the URL,
   which is the claim on this build (#109, and arc 1 step 4) → author, accepting or editing the
   draft impression → set **Results Interpreter** → **Complete**, which is the sign.
5. **The page goes out.** Chart of the ordering patient (`/openmrs`, logged in as the referring
   physician): the **AI critical result notification** entry is on the chart — finding label +
   accession + the signed ack link, never the narrative.
6. **Phone on camera:** tap the ack link
   (`https://demo.example.org/reading-api/ack/<taskId>?sig=…`). Two taps, by design (!114):
   the link opens a **confirmation page** naming who the acknowledgement will be attributed
   to, and only the **Acknowledge** button on it attests. Opening the link never acknowledges
   anything, so a preloading browser, a restored tab or a scanner cannot attest on the
   physician's behalf. Re-tap: idempotent.
   - **Identity comes first, and how depends on the route.** The signature is checked before
     any credential is solicited, so a forged link 403s without ever prompting. Then, if the
     browser sends a live OpenMRS `JSESSIONID`, the page is one click with no login. It only
     sends that cookie when the ack URL rides under the cookie's `/openmrs` path, and
     `CRITCOM_ACK_BASE_URL` currently points at `/reading-api`, so **on the host today expect
     the HTTP Basic prompt first**, then the confirmation page. That is the supported
     fallback, not a fault. To demo the one-click path instead, route the ack under
     `/openmrs` and point `CRITCOM_ACK_BASE_URL` there.
7. **Close the loop verbally:** the ledger Task is COMPLETED with the acknowledger's identity on
   it, `comms.checkAck` reads COMPLETED, no escalation fires. (Backstage proof if asked:
   Temporal UI over the tunnel, the workflow's `ackStatus`.)

**Where the AI actually ran** (radiologists will ask): not in the viewer. The banner renders a
finding computed server-side by the interpretation-assistant agent — TorchXRayVision's
DenseNet-121, Pneumothorax head, CPU, weights baked into the agent image — the moment the study
was ingested, before anyone opened it. Say the caveats out loud: it is a screening signal, not a
diagnosis; it scores anything handed to it (the registry's study selection is the only guard);
and only a positive screen ever becomes a COMPLETE finding. Full detail: `docs/cad-inference.md`.

## 4. Arc 3 — sloppy dictation and the override (~4 min)

1. **Restage** a cohort study; sign a report in the RIS **without an IMPRESSION section**.
2. Expected: verification **WARN**, the sign-off gate holds the workflow, the tier timer arms.
3. **Escalation page arrives** (comms channel per the rota) carrying the override link.
4. **Phone on camera:** open
   `https://demo.example.org/ingress/signoff/<workflowId>/override` — the confirm page shows
   the held verdict (status + rule IDs; never report text). Enter name, reason, and the
   override token; **Release the gate** → the "study released" receipt renders and the
   workflow proceeds. Narrate: authenticated, audited, who-and-why on the record.

## 5. Arc 4 — pre-read EHR value (~2 min, coda)

1. Pick the cohort patient with real MIMIC-IV labs/meds (creatinine, IV heparin).
2. Show the assembled context the agents used (the EHR packet for that study: labs, med flags,
   problems) next to the chart in `/openmrs`.
3. Land the lean-reference principle: only IDs crossed the agent wire; PHI stayed in fhir2.

## 6. Reset between takes / sessions

- **Never** `docker compose down` the OpenMRS stack mid-day (documented wedge).
- Restage a study: `python scripts/mimic/report_seeder.py finalize <study_id>` for the
  flip-to-final rehearsal path; delete probe artifacts per the worked examples in the drills.
- **`restage` is what makes a study re-readable, and it must be run before a second RIS sign.**
  The radiology module refuses to create a report when the order already carries a COMPLETED
  one, and its check ignores the voided flag, so a study signed once is otherwise unsignable
  forever (the form returns `cannot.create.already.completed`). `restage` clears that status as
  well as voiding the row, and it also returns the row to the unread worklist (#108). Hit live
  on 2026-08-07 while re-running an arc on an already-signed study.
- Full reset (between sessions only): selective `docker volume rm <project>_mariadb-data` +
  seed reload; ledger and ingress volumes untouched.
- **After any `openmrs` container recreate, rebuild the Lucene search index** or every
  name/identifier search in the RIS (find patient, find provider) silently returns nothing,
  and an ETL re-run would duplicate the cohort (the REST q-search rides the same index).
  Live-hit 2026-08-07. Fix: `DELETE FROM global_property WHERE property='search.indexVersion';`
  then restart the container; core rebuilds the index at boot (~1 min warm) and re-stamps the
  property. Verify: `patient?q=<subject_id>` returns the patient.

## 6a. Deploy window: updating the app services (#98)

The app services run CI-published images (#97), so a deploy is a pull, never a host build --
the drift class behind #83 cannot recur. Only in an announced window, never mid-review:

1. Announce the window; finish or reset any in-flight arc.
2. On the host (repo root, with the SAME `-f` overlay chain the stack was started with --
   a bare `docker compose` command against an overlay-started stack recreates services and
   can land the OpenMRS volume wedge):
   ```bash
   git pull                                   # compose + configs only; images come from CI
   docker compose -f docker-compose.yml -f docker-compose.tls.yml pull \
     orchestrator worklist-triage ehr-assistant interpretation-assistant \
     impression-generation report-verification communications worklist-api ohif \
     ris-sign-bridge ris-presign-bridge
   docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d --no-deps \
     orchestrator worklist-triage ehr-assistant interpretation-assistant \
     impression-generation report-verification communications worklist-api ohif \
     ris-sign-bridge ris-presign-bridge
   ```
   Both bridges are named because both are app services on the pull path; they share one
   published image (`ris-sign-bridge`) and differ only in the command, so a deploy that
   lists one and forgets the other leaves half the RIS bridge on an old build.
   `--no-deps` is load-bearing: the stateful set (openmrs, mariadb, orthanc and its volume,
   temporal's postgres, the comms ledger) is never touched by a deploy.
3. `ohif` no longer needs a routine restart after a deploy. Since #104 its nginx resolves
   `worklist-api`, `orthanc` and the orchestrator per request through Docker's embedded DNS
   (variable `proxy_pass` + `resolver` in `docker/ohif/default.conf`), so a recreated upstream
   is picked up within seconds. One deliberate exception, because that conf is volume-mounted
   from the checkout: when the `git pull` in step 2 changed `docker/ohif/default.conf` itself,
   run `docker compose -f docker-compose.yml -f docker-compose.tls.yml restart ohif` once so
   nginx loads the new conf. Before #104 a stale pinned IP cost four and a half hours of
   `/reading` 502 (2026-08-06) while every API-level check passed; the browser smoke in step 5
   is the guard that keeps that class of failure visible.
4. Pin what reviewers see: `APP_IMAGE_TAG=vX.Y.Z` (or a short SHA for an urgent fix) in the
   host env steers which published tag the pull fetches; unset means `main`. Pulls are
   anonymous -- the project registry is public, the host needs no token.
5. Smoke: §1.6 (proxy 401, `/reading` lists the cohort, one seeder `finalize` releases a
   study end to end), then §2's arc 1 once, quickly. **Load `/reading` in a browser** -- an API
   check is not a substitute, see step 3.

   **Confirm the browser is running the build you just deployed**, not a cached one. Since #111
   the entry point is served `Cache-Control: no-cache`, so an ordinary reload is enough; this
   check is what tells you it worked. In the browser console on `/reading`:

   ```js
   [...document.querySelectorAll('script[src]')].map(s => s.src.split('/').pop())
   ```

   and compare the `app.bundle.<hash>.js` it names against the host's:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.tls.yml exec -T ohif \
     grep -oE 'app.bundle.[a-f0-9]+.js' /usr/share/nginx/html/index.html
   ```

   The two must match. If they do not, the browser is on a stale `index.html`: hard-reload
   once and re-check. Twice on 2026-08-07 (pre-#111) a correct deploy showed the OLD UI this
   way while every server-side check passed, which is how the whole class of problem hides.
6. Log the deploy (date, tag, operator) in the demo diary next to the rehearsal notes.

**Bumping the o3 image pin is a different animal from an app deploy.** It recreates `openmrs`,
so the RIS is down for the whole boot and the app services above ride out an unavailable fhir2
(the orchestrator's poller logs `sign-off detection is stalled` and recovers on its own). Budget
**about 4 minutes** on a pin at or after `o3-010548c5`, measured 195s to `session` 200 on the
demo host. Older pins cost ~25 minutes, because the reference demo-data generator ran on every
boot and built 50 demo patients before the server would answer (#101); if a recreate is still
taking twenty minutes, the pin predates that fix. `docker compose ... pull openmrs` first so the
image is local and the outage is just the boot, and take a DB dump before the recreate
(`scripts/dump_openmrs_seed.sh ~/backups/openmrs-predeploy-<date>.sql.gz`).

## 7. Recording plan

One continuous 1920×1080 capture per arc, browser only, no dev tools; the phone on camera for
the ack tap (arc 2) and the override (arc 3). Film order: arc 1 condensed (30 s), arc 2 full,
arc 3, arc 4 as coda — ~12 min raw, cut to ~6 for the public version. Two seeder-driven dry
runs first, record the third.
