/**
 * WorkList component render tests.
 *
 * Verifies the pieces that don't depend on OHIF being present:
 *   * loads via injected fetch, renders rows in priority order
 *   * error state renders when Worklist API returns 503
 *   * empty state renders when Worklist API returns 0 items
 *   * clicking a row calls onOpenStudy with the right UID
 *   * `data-priority-tier` attribute is set so visual styling can key off it
 *
 * Not covered here (deferred to Docker smoke test):
 *   * OHIF's customRoutes extension point mounts this at /reading (see index.ts preRegistration)
 *   * The 30 s refresh interval — asserting on timers with happy-dom is finicky
 *     and the risk/reward isn't there for a first MR
 *
 * All renders are wrapped in <MemoryRouter> because WorkList calls
 * `useNavigate()` at render time (see WorkList.tsx file-header comment for why);
 * without a Router in the tree the hook throws. The renderWithRouter helper
 * keeps every render() call inside the same MemoryRouter context so the
 * tests match how the component is used in production (mounted inside OHIF's
 * BrowserRouter via customRoutes).
 */
import * as React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { WorkList } from '../components/WorkList';
import type { WorklistItem } from '../types';

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
  new Response(JSON.stringify(body), { status });

// Patch global fetch for each test.
const withFetch = (
  handler: (url: RequestInfo | URL, init?: RequestInit) => Promise<Response>,
) => {
  vi.stubGlobal('fetch', vi.fn(handler));
};

/** Mount the component inside a MemoryRouter so useNavigate() resolves.
 *  See file-header comment for rationale. */
const renderWithRouter = (ui: React.ReactElement) =>
  render(<MemoryRouter>{ui}</MemoryRouter>);

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

describe('<WorkList />', () => {
  it('marks a read study and leaves an unread one alone (#108)', async () => {
    // The #108 defect: before the read-state publish existed, a signed and archived study
    // rendered byte-identically to one still waiting to be read.
    withFetch(async () =>
      jsonResponse({
        generatedAt: '2026-08-07T19:11Z',
        items: [
          item({ studyInstanceUID: 'unread' }),
          item({ studyInstanceUID: 'done', readState: 'ARCHIVED', readAt: '2026-08-07T19:11:42Z' }),
        ],
      }),
    );
    renderWithRouter(<WorkList />);
    await waitFor(() =>
      expect(screen.queryByTestId('lhrad-worklist-loading')).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId('lhrad-row-done').getAttribute('data-read-state')).toBe('ARCHIVED');
    expect(screen.getByTestId('lhrad-row-unread').getAttribute('data-read-state')).toBe('');
    // The word, not just the styling: colour alone would not survive a screenshot in greyscale
    // and would tell a colour-blind reader nothing.
    expect(screen.getByTestId('lhrad-row-done').textContent).toContain('Read');
    expect(screen.getByTestId('lhrad-row-unread').textContent).toContain('to read');
  });

  it('renders loading state initially', () => {
    withFetch(async () => new Promise(() => {})); // never resolves
    renderWithRouter(<WorkList />);
    expect(screen.getByTestId('lhrad-worklist-loading')).toBeInTheDocument();
  });

  it('renders rows in priority order (STAT > URGENT > ROUTINE)', async () => {
    withFetch(async () =>
      jsonResponse({
        generatedAt: '2026-07-10T00:00Z',
        items: [
          item({ studyInstanceUID: 'routine', priorityTier: 'ROUTINE', priorityScore: 40 }),
          item({ studyInstanceUID: 'urgent', priorityTier: 'URGENT', priorityScore: 70 }),
          item({ studyInstanceUID: 'stat', priorityTier: 'STAT', priorityScore: 95 }),
        ],
      }),
    );
    renderWithRouter(<WorkList />);
    await waitFor(() => expect(screen.queryByTestId('lhrad-worklist-loading')).not.toBeInTheDocument());
    const rows = screen.getAllByRole('row').slice(1); // skip <thead>
    expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual([
      'lhrad-row-stat',
      'lhrad-row-urgent',
      'lhrad-row-routine',
    ]);
  });

  it('sets data-priority-tier so styling can key off it', async () => {
    withFetch(async () =>
      jsonResponse({
        generatedAt: 't',
        items: [item({ studyInstanceUID: 's', priorityTier: 'STAT', priorityScore: 90 })],
      }),
    );
    renderWithRouter(<WorkList />);
    await waitFor(() => screen.getByTestId('lhrad-row-s'));
    expect(screen.getByTestId('lhrad-row-s')).toHaveAttribute('data-priority-tier', 'STAT');
  });

  it('renders empty state for zero-item response', async () => {
    withFetch(async () => jsonResponse({ generatedAt: 't', items: [] }));
    renderWithRouter(<WorkList />);
    await waitFor(() => screen.getByTestId('lhrad-worklist-empty'));
    expect(screen.getByTestId('lhrad-worklist-empty')).toHaveTextContent(
      /no studies pending/i,
    );
  });

  it('renders error banner (loud, not empty list) on 503', async () => {
    withFetch(async () => jsonResponse({ detail: 'orthanc unreachable' }, 503));
    renderWithRouter(<WorkList />);
    await waitFor(() => screen.getByTestId('lhrad-worklist-error'));
    expect(screen.getByTestId('lhrad-worklist-error')).toHaveTextContent(/503/);
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  it('calls onOpenStudy with the row UID when the row is clicked', async () => {
    withFetch(async () =>
      jsonResponse({
        generatedAt: 't',
        items: [item({ studyInstanceUID: 'clickme' })],
      }),
    );
    const onOpenStudy = vi.fn();
    renderWithRouter(<WorkList onOpenStudy={onOpenStudy} />);
    await waitFor(() => screen.getByTestId('lhrad-row-clickme'));
    fireEvent.click(screen.getByTestId('lhrad-row-clickme'));
    expect(onOpenStudy).toHaveBeenCalledWith('clickme');
  });

  it('opens on keyboard Enter (accessibility)', async () => {
    withFetch(async () =>
      jsonResponse({
        generatedAt: 't',
        items: [item({ studyInstanceUID: 'kb' })],
      }),
    );
    const onOpenStudy = vi.fn();
    renderWithRouter(<WorkList onOpenStudy={onOpenStudy} />);
    await waitFor(() => screen.getByTestId('lhrad-row-kb'));
    fireEvent.keyDown(screen.getByTestId('lhrad-row-kb'), { key: 'Enter' });
    expect(onOpenStudy).toHaveBeenCalledWith('kb');
  });
});

// Ambient afterEach/beforeEach come from Vitest globals config.
declare function afterEach(fn: () => void): void;
