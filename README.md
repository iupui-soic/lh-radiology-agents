# LH-Radiology Multi-Agent System

New contributor? See [CONTRIBUTING.md](./CONTRIBUTING.md) for setup and workflow

Five A2A agents + a Temporal orchestrator that drive a radiology study from PACS arrival
through result communication, on **LibreHealth Radiology** (OpenMRS) as EHR/RIS, **fhir2** as
the FHIR R4 data API, **Orthanc** PACS, and **OHIF** viewer.

- **Backlog (what is left to build):** GitLab issues; open work is under the `M4`/`M5` milestones
- **Architecture & decisions:** [`CLAUDE.md`](./CLAUDE.md) + [`ARCHITECTURE.md`](./ARCHITECTURE.md); contracts in [`contracts/`](./contracts)
- **Diagrams:** [`ARCHITECTURE.md`](./ARCHITECTURE.md)
- **Working in here with Claude Code:** [`CLAUDE.md`](./CLAUDE.md)

## Status

Current release: **v0.3.0** (M0–M3 complete).

- **M0** — contract freeze + runnable mock harness.
- **M1** — walking skeleton live: agents on Temporal, Orthanc arrival starts a workflow,
  RIS sign-off polling closes the loop.
- **M2** (v0.2.0) — Worklist API + OHIF data source, sign-off escalation, A2A push
  notifications, pre-sign impression assist, verification rule library, opt-in OTel tracing.
- **M3** (v0.3.0) — interpretation-registry selection hardening and the guarded fhir2
  write path for the pre-sign draft.
- **M4** (in progress) — the **MIMIC-CXR radiologist showcase**: a hosted demo running a
  ~100-study cohort through the full pipeline. Day-of script:
  [`docs/showcase-runbook.md`](docs/showcase-runbook.md).
- **M5** (next) — EMBED mammography flagship
  ([`docs/embed-mammography-mapping.md`](docs/embed-mammography-mapping.md)).

One real CAD tool (a pneumothorax classifier) is wired behind the Interpretation tool
registry; every other model is still a validated stub. The GitLab issue backlog is the
authoritative plan.

## Quickstart

```bash
# 1. Install the shared library + test deps (one venv for the monorepo)
python -m venv .venv && . .venv/bin/activate
pip install -e libs/radagent-common pytest pytest-asyncio

# 2. Verify the contracts hold together (CI runs this too)
python scripts/validate_contracts.py

# 3. Run the whole pipeline in-process — no Temporal / no servers needed.
#    Exercises all five handlers in workflow order and validates every hop.
python mocks/run_walking_skeleton.py

# 4. Run an agent's tests (agents are standalone roots — run from inside the dir)
cd agents/worklist-triage && python -m pytest -q
```

To run the full dev stack (Orthanc, OHIF, OpenMRS, Temporal), see `docker-compose.yml`.
The OpenMRS o3 backend has a slow first boot and a clean-boot-only recipe. See
[`docs/o3-dev-stack.md`](docs/o3-dev-stack.md). For the hosted-demo posture (TLS,
proxy auth), layer `docker-compose.tls.yml` on top — see
[`docs/hosted-tls-overlay.md`](docs/hosted-tls-overlay.md) — and follow the
prerequisites in [`docs/showcase-runbook.md`](docs/showcase-runbook.md).
For the live A2A + Temporal wiring, install the app extras and pin the SDKs:
`pip install -e . ` (see `pyproject.toml`; **pin `a2a-sdk` and `temporalio`**).

## Layout
| Path | What |
|------|------|
| `contracts/` | Source of truth: StudyContext, per-skill schemas, events, agent cards |
| `libs/radagent-common/` | Shared: StudyContext model, **A2A factory**, fhir2/Orthanc clients, validation |
| `orchestrator/` | Temporal workflow (state machine), activities, ingress (Orthanc rx + RIS poller) |
| `agents/<name>/` | One A2A agent each (standalone root) |
| `integrations/` | Orthanc plugin · Worklist API · OHIF extension (M2) |
| `mocks/` | Walking skeleton, mock agent, synthetic fixtures |

## Docs
| Doc | What |
|-----|------|
| [`showcase-runbook.md`](docs/showcase-runbook.md) | M4 demo day-of script: every step, URL, and expected result |
| [`mimic-cxr-mapping.md`](docs/mimic-cxr-mapping.md) | MIMIC-CXR showcase ETL design and write paths |
| [`o3-dev-stack.md`](docs/o3-dev-stack.md) | Booting the OpenMRS o3 backend (slow first boot, clean-boot recipe) |
| [`hosted-tls-overlay.md`](docs/hosted-tls-overlay.md) | TLS + proxy-auth overlay for the hosted showcase |
| [`signoff-link.md`](docs/signoff-link.md) | How a RIS sign reaches the orchestrator (and where it broke) |
| [`presign-concept.md`](docs/presign-concept.md) | The dedicated authorship concept behind the AI pre-sign draft |
| [`cad-inference.md`](docs/cad-inference.md) | Where CAD inference runs vs. what the viewer renders |
| [`ehr-inbox-notification.md`](docs/ehr-inbox-notification.md) | The in-EHR critical-result notification channel |
| [`dicom-evidence-writeback.md`](docs/dicom-evidence-writeback.md) | Safety case for writing AI evidence back into Orthanc |
| [`ohif-integration-approach.md`](docs/ohif-integration-approach.md) | OHIF integration decision record |
| [`embed-mammography-mapping.md`](docs/embed-mammography-mapping.md) | EMBED mammography mapping spike (M5 groundwork) |

## Ownership
See the [Ownership table in `CLAUDE.md`](./CLAUDE.md#ownership) for who owns which
workstream; the GitLab backlog carries per-issue assignment.

## Citing
BibTeX for this repo (machine-readable copy: [`CITATION.cff`](./CITATION.cff)):

```bibtex
@software{lh_radiology_agents_2026,
  author  = {Pulavarthy, Lalitha Pranathi and Naliyatthaliyazchayil, Parvati and
             Sammeta, Chaitra Sree and Gadeela, Viraj and Gichoya, Judy Wawira and
             Purkayastha, Saptarshi},
  title   = {LH-Radiology Multi-Agent System},
  year    = {2026},
  version = {0.3.0},
  url     = {https://gitlab.com/librehealth/radiology/lh-radiology-agents}
}
```
