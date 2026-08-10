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


// --- #107 additions: AI margin column ---

import type { AiFindingsSummary, WorklistFinding } from '../types';

const aiComplete = (
  overrides: Partial<WorklistFinding> = {},
): WorklistFinding => ({
  toolId: 'pneumothorax-detect',
  label: 'Pneumothorax (screening p=0.51, raw 0.0298 vs op 0.0098)',
  status: 'COMPLETE',
  confidence: 0.51,
  rawScore: 0.0298,
  opThreshold: 0.0098,
  evidenceRef: 'orthanc:instance/i-1',
  ...overrides,
});

const aiPayload = (findings: WorklistFinding[]): AiFindingsSummary => ({
  findings,
  overallStatus: findings.some((f) => f.status === 'COMPLETE') ? 'COMPLETE' : 'STUBBED',
});

describe('WorkList AI margin column', () => {
  it('renders a raw-to-op ratio badge for a COMPLETE finding with both margin fields', async () => {
    withFetch(async () =>
      jsonResponse({
        items: [item({ aiFindings: aiPayload([aiComplete()]) })],
        generatedAt: '2026-08-09T12:00:00Z',
      }),
    );
    renderWithRouter(<WorkList onOpenStudy={() => {}} />);
    const badge = await screen.findByTestId('lhrad-ai-margin-badge');
    // 0.0298 / 0.0098 = 3.04..., which formats as "3.0x".
    expect(badge.textContent).toBe('3.0x');
    // Tooltip is the full label so a reader on the row surface can see the same
    // marginal text the banner shows in the viewer.
    expect(badge.getAttribute('title')).toContain('raw 0.0298 vs op 0.0098');
  });

  it('picks the highest raw/op ratio when multiple COMPLETE findings are present (#27 multi-head)', async () => {
    const strong = aiComplete({ toolId: 'effusion-detect', label: 'Effusion label',
                                rawScore: 0.030, opThreshold: 0.010 });   // 3.0x
    const weak = aiComplete({ toolId: 'pneumothorax-detect', label: 'PTX label',
                              rawScore: 0.011, opThreshold: 0.010 });     // 1.1x
    withFetch(async () =>
      jsonResponse({
        items: [item({ aiFindings: aiPayload([weak, strong]) })],
        generatedAt: '2026-08-09T12:00:00Z',
      }),
    );
    renderWithRouter(<WorkList onOpenStudy={() => {}} />);
    const badge = await screen.findByTestId('lhrad-ai-margin-badge');
    // The higher-margin call wins the slot.
    expect(badge.textContent).toBe('3.0x');
    expect(badge.getAttribute('data-tool-id')).toBe('effusion-detect');
  });

  it('falls back to "AI+" when a COMPLETE finding has null margin (referral rule, stub, no-torch)', async () => {
    withFetch(async () =>
      jsonResponse({
        items: [item({ aiFindings: aiPayload([
          aiComplete({
            toolId: 'referral-rule-x',
            label: 'Suspected foo (referral reason)',
            confidence: null,
            rawScore: null,
            opThreshold: null,
          }),
        ]) })],
        generatedAt: '2026-08-09T12:00:00Z',
      }),
    );
    renderWithRouter(<WorkList onOpenStudy={() => {}} />);
    const badge = await screen.findByTestId('lhrad-ai-plus-badge');
    expect(badge.textContent).toBe('AI+');
    expect(screen.queryByTestId('lhrad-ai-margin-badge')).toBeNull();
  });

  it('prefers the ratio badge when at least one COMPLETE has a margin, ignoring null-margin COMPLETEs for ranking', async () => {
    const withMargin = aiComplete({ toolId: 'pneumothorax-detect',
                                    rawScore: 0.030, opThreshold: 0.010 }); // 3.0x
    const noMargin = aiComplete({ toolId: 'referral-rule-x', label: 'Referral',
                                  confidence: null, rawScore: null, opThreshold: null });
    withFetch(async () =>
      jsonResponse({
        items: [item({ aiFindings: aiPayload([noMargin, withMargin]) })],
        generatedAt: '2026-08-09T12:00:00Z',
      }),
    );
    renderWithRouter(<WorkList onOpenStudy={() => {}} />);
    const badge = await screen.findByTestId('lhrad-ai-margin-badge');
    expect(badge.textContent).toBe('3.0x');
  });

  it('renders nothing when aiFindings is null (workflow has not published yet)', async () => {
    withFetch(async () =>
      jsonResponse({
        items: [item({ aiFindings: null })],
        generatedAt: '2026-08-09T12:00:00Z',
      }),
    );
    renderWithRouter(<WorkList onOpenStudy={() => {}} />);
    // Wait for a row to render so the empty state is settled.
    await screen.findByTestId('lhrad-row-uid-1');
    expect(screen.queryByTestId('lhrad-ai-margin-badge')).toBeNull();
    expect(screen.queryByTestId('lhrad-ai-plus-badge')).toBeNull();
  });

  it('renders nothing when aiFindings is present but every finding is STUBBED (silence, per policy)', async () => {
    withFetch(async () =>
      jsonResponse({
        items: [item({ aiFindings: aiPayload([
          aiComplete({ status: 'STUBBED', confidence: null, rawScore: 0.005, opThreshold: 0.010 }),
        ]) })],
        generatedAt: '2026-08-09T12:00:00Z',
      }),
    );
    renderWithRouter(<WorkList onOpenStudy={() => {}} />);
    await screen.findByTestId('lhrad-row-uid-1');
    // A STUBBED finding must NEVER produce a badge on the row -- the automation-bias guard
    // the interpretation-agent's design comment calls out (see agents/interpretation-assistant/
    // handler.py: STUBBED = "the model ran, no finding at threshold" is not a positive claim).
    expect(screen.queryByTestId('lhrad-ai-margin-badge')).toBeNull();
    expect(screen.queryByTestId('lhrad-ai-plus-badge')).toBeNull();
  });

  it('renders nothing when the omitted aiFindings key is absent (backwards-compat with older API)', async () => {
    // Older worklist-api versions don't emit aiFindings at all; the field is optional in
    // the type. This must NOT crash and must not render a badge.
    const noField = item();
    delete (noField as any).aiFindings;
    withFetch(async () =>
      jsonResponse({
        items: [noField],
        generatedAt: '2026-08-09T12:00:00Z',
      }),
    );
    renderWithRouter(<WorkList onOpenStudy={() => {}} />);
    await screen.findByTestId('lhrad-row-uid-1');
    expect(screen.queryByTestId('lhrad-ai-margin-badge')).toBeNull();
    expect(screen.queryByTestId('lhrad-ai-plus-badge')).toBeNull();
  });

  it('column header "AI" is present between "Accession" and "Status"', async () => {
    withFetch(async () =>
      jsonResponse({
        items: [item()],
        generatedAt: '2026-08-09T12:00:00Z',
      }),
    );
    renderWithRouter(<WorkList onOpenStudy={() => {}} />);
    await screen.findByTestId('lhrad-row-uid-1');

    const headers = screen.getAllByRole('columnheader').map((th) => th.textContent);
    const acc = headers.indexOf('Accession');
    const ai = headers.indexOf('AI');
    const status = headers.indexOf('Status');
    expect(acc).toBeGreaterThanOrEqual(0);
    expect(ai).toBe(acc + 1);
    expect(status).toBe(ai + 1);
  });
});


// --- #116: the empty-value placeholder ---

describe('WorkList empty-value placeholder', () => {
  it('renders an em-dash, not a mojibake sequence, for every empty column', async () => {
    // The #116 defect: the placeholder literal in WorkList.tsx was a double-encoded em-dash
    // (U+00E2 U+20AC U+0022), so every row with an empty modality, description, accession or
    // study date showed "a-euro-quote" instead of a dash. On the showcase cohort that was all
    // 100 rows at once. The source now spells the character as a \u2014 escape, which is why
    // this test asserts on the RENDERED text: an escape cannot be mangled by an editor, and a
    // regression to a literal character would show up here rather than only in a browser.
    // Both expected values below are escapes too, for the same reason.
    withFetch(async () =>
      jsonResponse({
        items: [
          item({
            studyInstanceUID: 'blank',
            modality: '',
            studyDescription: '',
            accessionNumber: '',
            studyDate: '',
          }),
        ],
        generatedAt: '2026-08-09T12:00:00Z',
      }),
    );
    renderWithRouter(<WorkList onOpenStudy={() => {}} />);

    const row = await screen.findByTestId('lhrad-row-blank');
    const cells = [...row.querySelectorAll('td')].map((td) => td.textContent);
    // Modality, description, study date and accession all fall back to the same placeholder.
    const placeholders = cells.filter((c) => c === '\u2014');
    expect(placeholders).toHaveLength(4);
    // The specific broken bytes must never come back.
    expect(row.textContent).not.toContain('\u00e2\u20ac');
  });
});
