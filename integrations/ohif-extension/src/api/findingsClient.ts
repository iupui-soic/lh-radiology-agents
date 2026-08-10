/**
 * findingsClient — fetch AI findings for a study from the Worklist API's /findings
 * surface (#89, client-side CAD evidence rendering).
 *
 * Read-only client: the orchestrator publishes; OHIF fetches; no write path from the
 * viewer side. Contrast with the archive-write path (#59) which produces DICOM SC
 * objects that OHIF picks up via the imaging series list — that is a separate
 * long-term concern gated on PI sign-off. This client is the demo-safe alternative
 * that renders findings from a small API without touching the archive.
 *
 * 404 handling: the endpoint returns 404 when the workflow has not yet published
 * findings for the study (interpretation still running, or worker down, or workflow
 * hasn't reached READY_FOR_READ yet). The client resolves 404 to `null` so the
 * caller can distinguish "not yet" (null) from "published empty" (an object with
 * an empty or all-STUBBED findings array). Both render as "no banner", but the
 * null case may render a subtle "AI still analyzing" hint whereas the empty case
 * renders nothing.
 *
 * Path lives under /reading-api/, same nginx proxy path as the reading worklist,
 * so the extension needs no cross-origin config or additional proxy line.
 */

export interface FindingItem {
  toolId: string;
  label: string;
  confidence: number | null;
  /** The CAD margin (#86): rawScore is the head's raw-sigmoid recovery from confidence,
   *  opThreshold is the head's operating point. Together they distinguish a 3.1x-the-op
   *  positive from a 1.01x one; the calibrated p alone compresses this cohort's positives
   *  into 0.50-0.53, which reads as a coin flip on any surface that shows p by itself.
   *  Null on tools without an operating point (referral rules, stubs) and in the
   *  no-torch lane; the banner's label carries the same margin text verbatim already, so
   *  these fields are for downstream consumers (e.g., the worklist row's margin badge). */
  rawScore: number | null;
  opThreshold: number | null;
  evidenceRef: string | null;
  /** One of "COMPLETE" | "STUBBED" | "ERROR" | "PARTIAL" per interpretation.runTools. */
  status: string;
}

export interface FindingsResponse {
  studyInstanceUID: string;
  workflowId: string;
  findings: FindingItem[];
  overallStatus: string;
  generatedAt: string;
  updatedAt: string;
}

/**
 * Fetch findings for a study. Returns `null` when the endpoint returns 404 (workflow
 * hasn't published yet) or on any error (network, non-2xx). Never throws: rendering
 * a subdued state on error is always safer than crashing the panel.
 */
export async function fetchFindings(
  studyInstanceUID: string,
  signal?: AbortSignal,
): Promise<FindingsResponse | null> {
  const url = `/reading-api/findings/${encodeURIComponent(studyInstanceUID)}`;
  try {
    const resp = await fetch(url, { signal });
    if (resp.status === 404) return null;
    if (!resp.ok) {
      // Non-404 error (500, 502, etc.) — log for diagnostics but degrade to null so
      // the panel shows a subdued "unavailable" state rather than throwing red.
      console.warn(`fetchFindings: HTTP ${resp.status} for ${studyInstanceUID}`);
      return null;
    }
    return (await resp.json()) as FindingsResponse;
  } catch (e) {
    // AbortError on unmount is normal — no log.
    if ((e as { name?: string }).name !== 'AbortError') {
      console.warn(`fetchFindings: ${e}`);
    }
    return null;
  }
}
