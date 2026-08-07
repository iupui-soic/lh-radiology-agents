/**
 * Shared TypeScript contracts for the LH-Radiology OHIF extension.
 *
 * These types mirror the JSON shape emitted by the Worklist API's `GET /worklist`
 * endpoint (see `integrations/worklist-api/main.py` module docstring). The Worklist
 * API does NOT emit a formal JSON Schema for the response (deliberate — see the R2
 * doc `docs/ohif-integration-approach.md`), so these types are the consumer-side
 * source of truth. When the Worklist API changes, this file changes in lockstep.
 */

/**
 * A single row in the reading worklist.
 * All fields are always present from the API; optional fields are the ones where
 * the Worklist API returns `null` (see the module docstring on `main.py`).
 */
export interface WorklistItem {
  /** Orthanc's internal study identifier (opaque UUID). Used when we need to route
   *  through Orthanc, e.g. via `/dicom-web/studies/{orthancStudyId}`. */
  orthancStudyId: string;

  /** DICOM Study Instance UID — the correlation key across every system
   *  (Orthanc, fhir2, orchestrator, OHIF viewer URL). */
  studyInstanceUID: string;

  accessionNumber: string;
  modality: string;
  studyDescription: string;

  /** DICOM `YYYYMMDD`. Kept as a string here (not Date) because we display and
   *  sort it as-is; converting to Date would lose the DICOM canonical form. */
  studyDate: string;

  /** May be `null` if Orthanc doesn't report `Statistics.CountInstances`. */
  numberOfInstances: number | null;

  /** One of "STAT" | "URGENT" | "ROUTINE". Widened to string to tolerate any
   *  future tier the orchestrator introduces without needing a UI change here. */
  priorityTier: string;

  /** 0..100. Higher = read first. */
  priorityScore: number;

  /** Populated once triage has run; `null` for untriaged studies. */
  workflowId: string | null;

  /** #108: the study's terminal workflow state, published when the read finishes.
   *  `null` means still to read. Widened to string for the same reason as
   *  priorityTier: the orchestrator's state vocabulary must be free to change
   *  without a UI release. */
  readState?: string | null;

  /** ISO8601 instant the read finished; `null` while unread. */
  readAt?: string | null;

  /** Populated once LH-Radiology assignment is wired (M3); `null` in dev
   *  (see `NullAssignmentReader` in the Worklist API). */
  assignment: {
    radiologistId: string;
    /** ISO 8601 datetime. */
    assignedAt: string;
  } | null;
}

/** Top-level shape returned by `GET /worklist`. */
export interface WorklistResponse {
  items: WorklistItem[];
  /** ISO 8601 datetime; server-generated per response. */
  generatedAt: string;
}

/**
 * Priors + overlays surfaced next to the currently opened study.
 * Shape and source are placeholders for #21 — the priors panel reads the study
 * context via `?priorsRef=<studyContextRef>` in the URL when the mode is entered.
 * The exact backend that resolves `priorsRef` is a small follow-up (parallel to
 * the `orthanc_webhook` ingest surface in `orchestrator/ingress.py`).
 */
export interface PriorsPacket {
  studyInstanceUID: string;
  priorStudies: Array<{
    ref: string;
    modality?: string;
    date?: string;
  }>;
  relevantLabs: Array<{
    code: string;
    display?: string;
    value?: number | string;
    unit?: string;
    date?: string;
  }>;
  activeProblems: Array<{
    code: string;
    display?: string;
  }>;
  contrastFlags: {
    egfr: number | null;
    priorReaction: boolean;
    onMetformin: boolean;
  };
  allergies: Array<{
    code: string;
    criticality?: string;
  }>;
}

/**
 * The event fired when a radiologist opens a study — matches
 * `contracts/events/ohif-opened.schema.json`. This is emitted from the mode's
 * `onModeEnter` hook. It carries no PHI beyond the StudyInstanceUID.
 */
export interface StudyOpenedEvent {
  schemaVersion: '1.0.0';
  eventType: 'ohif.study.opened';
  studyInstanceUID: string;
  radiologistId?: string;
  /** ISO 8601 datetime. */
  openedAt: string;
}
