import { describe, expect, it } from 'vitest';

import {
  fetchWorklist,
  isWorklistResponse,
  sortByPriority,
  WorklistApiError,
  WORKLIST_API_PATH,
} from '../api/worklistClient';
import type { WorklistItem, WorklistResponse } from '../types';

// --- test helpers -----------------------------------------------------------

const item = (overrides: Partial<WorklistItem> = {}): WorklistItem => ({
  orthancStudyId: 'o-1',
  studyInstanceUID: 'uid-1',
  accessionNumber: 'ACC-1',
  modality: 'CT',
  studyDescription: 'CT CHEST',
  studyDate: '20260710',
  numberOfInstances: 100,
  priorityTier: 'ROUTINE',
  priorityScore: 50,
  workflowId: null,
  assignment: null,
  ...overrides,
});

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

// --- sortByPriority ---------------------------------------------------------

describe('sortByPriority', () => {
  it('sinks a read study below every unread one, whatever its tier (#108)', () => {
    // The live miss this pins: #108 shipped the server-side sort alone, and this function
    // re-sorted it away, leaving a signed study at position 26 of 100 on the demo host, above
    // 74 studies nobody had read.
    const items = [
      item({ studyInstanceUID: 'read-stat', priorityTier: 'STAT', priorityScore: 100,
             readState: 'ARCHIVED' }),
      item({ studyInstanceUID: 'unread-routine', priorityTier: 'ROUTINE', priorityScore: 45 }),
      item({ studyInstanceUID: 'unread-stat', priorityTier: 'STAT', priorityScore: 90 }),
    ];
    expect(sortByPriority(items).map((i) => i.studyInstanceUID)).toEqual([
      'unread-stat',
      'unread-routine',
      'read-stat',
    ]);
  });

  it('orders STAT above URGENT above ROUTINE', () => {
    const items = [
      item({ studyInstanceUID: 'r', priorityTier: 'ROUTINE', priorityScore: 40 }),
      item({ studyInstanceUID: 's', priorityTier: 'STAT', priorityScore: 95 }),
      item({ studyInstanceUID: 'u', priorityTier: 'URGENT', priorityScore: 70 }),
    ];
    const sorted = sortByPriority(items).map((i) => i.studyInstanceUID);
    expect(sorted).toEqual(['s', 'u', 'r']);
  });

  it('within a tier, higher score comes first', () => {
    const items = [
      item({ studyInstanceUID: 'lo', priorityTier: 'STAT', priorityScore: 80 }),
      item({ studyInstanceUID: 'hi', priorityTier: 'STAT', priorityScore: 95 }),
    ];
    expect(sortByPriority(items).map((i) => i.studyInstanceUID)).toEqual(['hi', 'lo']);
  });

  it('within a tier and score, older studyDate comes first', () => {
    const items = [
      item({ studyInstanceUID: 'newer', priorityTier: 'STAT', priorityScore: 95, studyDate: '20260710' }),
      item({ studyInstanceUID: 'older', priorityTier: 'STAT', priorityScore: 95, studyDate: '20260701' }),
    ];
    expect(sortByPriority(items).map((i) => i.studyInstanceUID)).toEqual(['older', 'newer']);
  });

  it('is defensive: unknown tier gets ranked last', () => {
    const items = [
      item({ studyInstanceUID: 'weird', priorityTier: 'CRITICAL', priorityScore: 100 }),
      item({ studyInstanceUID: 'stat', priorityTier: 'STAT', priorityScore: 50 }),
    ];
    expect(sortByPriority(items).map((i) => i.studyInstanceUID)).toEqual(['stat', 'weird']);
  });

  it('does not mutate the input array', () => {
    const items = [item({ studyInstanceUID: 'a' }), item({ studyInstanceUID: 'b' })];
    const snapshot = [...items];
    sortByPriority(items);
    expect(items).toEqual(snapshot);
  });
});

// --- isWorklistResponse -----------------------------------------------------

describe('isWorklistResponse', () => {
  it('accepts a valid shape', () => {
    expect(isWorklistResponse({ items: [], generatedAt: 't' })).toBe(true);
  });

  it('rejects null and non-object', () => {
    expect(isWorklistResponse(null)).toBe(false);
    expect(isWorklistResponse('nope')).toBe(false);
    expect(isWorklistResponse(42)).toBe(false);
  });

  it('rejects when items is not an array', () => {
    expect(isWorklistResponse({ items: 'nope', generatedAt: 't' })).toBe(false);
  });

  it('rejects when generatedAt is missing', () => {
    expect(isWorklistResponse({ items: [] })).toBe(false);
  });
});

// --- fetchWorklist ----------------------------------------------------------

describe('fetchWorklist', () => {
  it('GETs the default path with Accept: application/json', async () => {
    let seenUrl = '';
    let seenAccept = '';
    const fetchImpl = async (url: RequestInfo | URL, init?: RequestInit) => {
      seenUrl = String(url);
      seenAccept = (init?.headers as Record<string, string> | undefined)?.Accept ?? '';
      return jsonResponse({ items: [], generatedAt: 't' });
    };
    await fetchWorklist({ fetchImpl });
    expect(seenUrl).toBe(WORKLIST_API_PATH);
    expect(seenAccept).toBe('application/json');
  });

  it('returns the parsed WorklistResponse', async () => {
    const body: WorklistResponse = { items: [item()], generatedAt: '2026-07-10T00:00Z' };
    const fetchImpl = async () => jsonResponse(body);
    const got = await fetchWorklist({ fetchImpl });
    expect(got).toEqual(body);
  });

  it('throws WorklistApiError on 503 (Orthanc down)', async () => {
    const fetchImpl = async () => jsonResponse({ detail: 'Orthanc unreachable' }, 503);
    await expect(fetchWorklist({ fetchImpl })).rejects.toMatchObject({
      name: 'WorklistApiError',
      status: 503,
    });
  });

  it('throws WorklistApiError on unexpected response shape (bad nginx route → HTML)', async () => {
    // an HTML error page from a misconfigured proxy would deserialize as unexpected JSON
    const fetchImpl = async () =>
      new Response('<html><body>404</body></html>', {
        status: 200,
        headers: { 'Content-Type': 'text/html' },
      });
    // response.json() will throw on non-JSON; that surfaces up
    await expect(fetchWorklist({ fetchImpl })).rejects.toBeDefined();
  });

  it('respects the url override (deployments may reverse-proxy differently)', async () => {
    let seenUrl = '';
    const fetchImpl = async (url: RequestInfo | URL) => {
      seenUrl = String(url);
      return jsonResponse({ items: [], generatedAt: 't' });
    };
    await fetchWorklist({ fetchImpl, url: '/custom/worklist' });
    expect(seenUrl).toBe('/custom/worklist');
  });

  it('passes the AbortSignal through so callers can cancel', async () => {
    let seenSignal: AbortSignal | undefined;
    const fetchImpl = async (_url: RequestInfo | URL, init?: RequestInit) => {
      seenSignal = init?.signal ?? undefined;
      return jsonResponse({ items: [], generatedAt: 't' });
    };
    const controller = new AbortController();
    await fetchWorklist({ fetchImpl, signal: controller.signal });
    expect(seenSignal).toBe(controller.signal);
  });
});
