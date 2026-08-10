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
/**
 * Summary of AI findings joined onto a worklist row (#107).
 *
 * Passed through from the /findings surface; the shape mirrors what the viewer's
 * FindingsBannerPanel receives, minus the studyInstanceUID key (that's the row's UID).
 * The row-side UI filters to COMPLETE for its margin badge and ignores STUBBED negatives
 * (they signal nothing worth flagging on a queue view).
 *
 * `null` on the row means findings have not been published yet for this study; an object
 * with an empty `findings` array means published but the tools produced no COMPLETE.
 * Both fall back to silence on the row.
 */
export interface AiFindingsSummary {
  findings: WorklistFinding[];
  overallStatus: string;
}

/** One finding on a worklist row; same shape as FindingItem in findingsClient but kept
 *  duplicated so the row rendering does not import the viewer's client. */
export interface WorklistFinding {
  toolId: string;
  label: string;
  status: string;
  confidence: number | null;
  /** The head's raw sigmoid (op-norm inverted from confidence). Null on tools without
   *  an operating point (referral rules, stubs) and in the no-torch lane. */
  rawScore: number | null;
  /** The head's raw-sigmoid operating point. Same nullability. */
  opThreshold: number | null;
  evidenceRef: string | null;
}

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

  /** #107: AI findings joined onto the row so the reading worklist can render a CAD
   *  margin badge (raw-to-op ratio) rather than a bare "positive exists" indicator.
   *  `null` when the workflow has not published findings yet for this study; an
   *  empty `findings` array means published but nothing COMPLETE. See the type's
   *  own docstring for the fallback contract. Optional so the field is safe to omit
   *  in older API responses (schema-forward, not a required breaking change). */
  aiFindings?: AiFindingsSummary | null;
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
