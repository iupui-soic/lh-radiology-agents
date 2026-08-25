# ARCHITECTURE

Diagrams for the LH-Radiology multi-agent system. Contracts: `/contracts`. Decisions + glossary: `CLAUDE.md`. Backlog: GitLab issues.

## Components

```mermaid
flowchart LR
  subgraph Edge["Imaging + EHR"]
    ORTHANC[(Orthanc PACS)]
    OHIF[OHIF Viewer]
    OPENMRS[OpenMRS / LH-Radiology RIS + fhir2]
  end

  subgraph Core["Orchestration"]
    INGRESS[Ingress: Orthanc webhook + RIS poller]
    TEMPORAL[(Temporal)]
    WF[StudyWorkflow: state machine]
    WLAPI[Worklist API]
  end

  subgraph Agents["A2A Agents"]
    TRIAGE[Worklist Triage]
    EHR[EHR Assistant]
    INTERP[Interpretation Assistant]
    IMPR[Impression Generation]
    VERIFY[Report Verification]
    COMMS[Communications - existing]
  end

  ORTHANC -- OnStableStudy webhook --> INGRESS
  OPENMRS -- DiagnosticReport=final poll --> INGRESS
  INGRESS --> TEMPORAL --> WF
  WF -- A2A skill calls --> TRIAGE & EHR & INTERP & IMPR & VERIFY & COMMS
  EHR -. read-only .-> OPENMRS
  INTERP -. metadata .-> ORTHANC
  WF -- priority --> WLAPI --> OHIF
  OHIF -. opens study .-> ORTHANC
  Radiologist[Radiologist] -- authors & signs --> OPENMRS
  OHIF --> Radiologist
```

## State machine (one workflow instance per study)

```mermaid
stateDiagram-v2
  [*] --> RECEIVED: Orthanc OnStableStudy
  RECEIVED --> READY_FOR_READ: fan-out triage / ehr / interpretation
  READY_FOR_READ --> AWAITING_RADIOLOGIST: publish priority to Worklist API
  AWAITING_RADIOLOGIST --> IMPRESSION: RIS report status=final (signal)
  IMPRESSION --> VERIFY: impression.generate
  VERIFY --> COMMUNICATE: PASS
  VERIFY --> AWAITING_SIGNOFF: WARN/FAIL needs human review
  AWAITING_SIGNOFF --> VERIFY: addendum / ack (or escalate on timeout)
  COMMUNICATE --> ARCHIVED: comms.dispatch
  ARCHIVED --> [*]

  note right of AWAITING_RADIOLOGIST: human-gated (durable wait + signal)
  note right of AWAITING_SIGNOFF: human-gated (escalation timer)
```

## Sequence — happy path

```mermaid
sequenceDiagram
  autonumber
  participant O as Orthanc PACS
  participant I as Ingress
  participant W as StudyWorkflow
  participant A as A2A agents
  participant WL as Worklist API
  participant Rad as Radiologist
  participant F as RIS and fhir2
  participant C as Communications

  O->>I: OnStableStudy webhook
  I->>F: resolve patient and order by accession number
  I->>W: start workflow, one instance per study

  Note over W,A: RECEIVED — parallel pre-read fan-out
  par
    W->>A: triage.score
  and
    W->>A: ehr.assembleContext
  and
    W->>A: interpretation.runTools
  end

  Note over W,WL: READY_FOR_READ
  W->>WL: publish priority tier and score
  W->>WL: publish AI findings

  opt at least one COMPLETE finding
    W->>A: impression.generate
    A-->>W: draft impression text
    W->>F: write preliminary DiagnosticReport, authorship-stamped
  end

  Note over W: AWAITING_RADIOLOGIST — durable wait
  Rad->>WL: open the reading worklist
  Rad->>O: view images
  Rad->>F: author and sign the report

  I->>F: poll DiagnosticReport status=final since cursor
  F-->>I: finalized report
  I-->>W: signal report_finalized

  Note over W: IMPRESSION
  W->>A: impression.generate
  Note over W: VERIFY
  W->>A: report.verify
  A-->>W: PASS

  Note over W,C: COMMUNICATE
  W->>C: comms.dispatch
  C->>F: notification into the chart
  C-->>W: ack Task and deadline
  W->>C: comms.checkAck
  C-->>W: acknowledged

  Note over W: ARCHIVED
  W->>WL: publish read state
```

## Trigger map
Summary: **Orthanc** Python plugin → ingress webhook starts the workflow;
the **RIS poller** (`fhir2 DiagnosticReport?status=final&_lastUpdated=gt{cursor}`) signals the
waiting workflow; **OHIF** reads the **Worklist API** for the priority-ordered reading list
(M2: emits `StudyOpenedEvent` for pre-sign assist).

## Deployment (dev)
`docker-compose.yml` brings up Orthanc, OHIF, OpenMRS + MariaDB, and the Temporal stack
(server + Postgres + UI). Orchestrator (ingress + worker) and the six agents run as services
in M1 (Dockerfiles added then). Temporal hosting is self-hosted for dev; Temporal Cloud is a
prod option. Image tags in compose are starting points — pin them per environment.
