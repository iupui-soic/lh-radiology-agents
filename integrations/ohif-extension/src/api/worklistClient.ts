/**
 * Client for the Worklist API's `GET /worklist` endpoint.
 *
 * The Worklist API is proxied by nginx at `/reading-api/*` (see
 * `docker/ohif/default.conf` in this MR) so requests are same-origin. This means
 * no CORS to configure and no bearer-token juggling from the browser — the browser
 * fires against its own origin, nginx forwards to `worklist-api:8107` on the
 * docker-compose network.
 *
 * Failure semantics mirror the Worklist API's own philosophy:
 *   - HTTP 503 (Orthanc down): surface as a real error to the caller. The
 *     component renders an error banner instead of a fake-empty list. See
 *     `test_worklist_503_when_orthanc_down` on the Worklist API side for the
 *     "silent empty list is dangerous" rationale.
 *   - Network error / non-JSON response: also surfaces as an error. Better to
 *     tell the radiologist "worklist unavailable" than to lie with an empty list.
 */
import type { WorklistResponse, WorklistItem } from '../types';

/** Same-origin URL — nginx routes /reading-api/* to the Worklist API container. */
export const WORKLIST_API_PATH = '/reading-api/worklist';

/** Options for {@link fetchWorklist}; `fetchImpl` overridable for tests. */
export interface FetchWorklistOptions {
  /** Signal for AbortController; used by React components to cancel in-flight
   *  requests when the component unmounts or the polling interval re-fires. */
  signal?: AbortSignal;
  /** Override the URL — useful for tests, or if a deployment reverse-proxies
   *  the worklist at a different path. */
  url?: string;
  /** Injectable for tests; defaults to global `fetch`. */
  fetchImpl?: typeof fetch;
}

/**
 * GET the worklist. Throws on any non-2xx response or network failure — the
 * caller is expected to render an error banner rather than degrade to an empty
 * list.
 */
export async function fetchWorklist(
  options: FetchWorklistOptions = {},
): Promise<WorklistResponse> {
  const { signal, url = WORKLIST_API_PATH, fetchImpl = fetch } = options;

  const response = await fetchImpl(url, { signal, headers: { Accept: 'application/json' } });
  if (!response.ok) {
    throw new WorklistApiError(
      `Worklist API returned HTTP ${response.status}`,
      response.status,
    );
  }

  const body = (await response.json()) as unknown;
  if (!isWorklistResponse(body)) {
    throw new WorklistApiError(
      'Worklist API returned an unexpected response shape',
      response.status,
    );
  }

  return body;
}

/** Narrow error the UI can distinguish from other Errors, so it can display the
 *  HTTP status if available. */
export class WorklistApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'WorklistApiError';
    this.status = status;
  }
}

/**
 * Duck-typed runtime check on the response shape.
 * NOT a full schema validation — we trust the Worklist API server-side to shape
 * items correctly (its own unit tests pin the shape). This guard exists so a
 * mistyped route or an unrelated 200 (e.g. an HTML error page from a bad nginx
 * config) still fails loudly here rather than crashing the WorkList render.
 */
export function isWorklistResponse(x: unknown): x is WorklistResponse {
  if (!x || typeof x !== 'object') return false;
  const obj = x as Record<string, unknown>;
  return Array.isArray(obj.items) && typeof obj.generatedAt === 'string';
}

/**
 * Client-side re-sort as a defensive redundancy — the Worklist API server sorts
 * already, but a browser cache, an old response, or a slow scroll during a
 * re-fetch could show an out-of-order state briefly. This makes the UI's
 * priority order the source of truth from the user's perspective.
 *
 * Sort key mirrors `integrations/worklist-api/main.py`:
 *   readState (unread before read, #108)
 *   → priorityTier bucket (STAT < URGENT < ROUTINE)
 *   → priorityScore descending
 *   → studyDate ascending (older stat cases float above newer)
 *
 * The read-state term is FIRST, and it must stay in step with the server. #108 shipped the
 * server half alone and this function silently undid it: a signed study still rendered at
 * position 26 of 100, above 74 studies nobody had read, because "defensive redundancy" re-sorted
 * the correct order away. A divergence here is invisible server-side, so treat the two sort keys
 * as one change.
 */
export function sortByPriority(items: WorklistItem[]): WorklistItem[] {
  const tierRank: Record<string, number> = { STAT: 0, URGENT: 1, ROUTINE: 2 };
  return [...items].sort((a, b) => {
    // Finished work sinks below every unread study whatever its tier: the reading list is a
    // queue of what is left to do.
    const readDiff = (a.readState ? 1 : 0) - (b.readState ? 1 : 0);
    if (readDiff !== 0) return readDiff;
    const tierDiff = (tierRank[a.priorityTier] ?? 99) - (tierRank[b.priorityTier] ?? 99);
    if (tierDiff !== 0) return tierDiff;
    const scoreDiff = b.priorityScore - a.priorityScore; // higher first
    if (scoreDiff !== 0) return scoreDiff;
    return (a.studyDate || '').localeCompare(b.studyDate || ''); // older first
  });
}
