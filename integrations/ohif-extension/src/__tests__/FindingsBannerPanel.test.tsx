/**
 * FindingsBannerPanel render tests.
 *
 * The banner is `finding.label`'s verbatim surface: the pixel tool's label carries the CAD
 * margin text ("raw 0.0298 vs op 0.0098", #86) and any surface that shows the label shows
 * the margin for free. This file pins that contract so a future banner rework -- say,
 * switching to a structured render or filtering "screening signal only" boilerplate --
 * cannot silently drop the margin substring and leave the reader with the compressed
 * p=0.50-0.53 signal alone.
 *
 * Rendering policy pinned here matches the file-header comment on FindingsBannerPanel:
 *   * COMPLETE  -> label rendered verbatim in the banner text
 *   * STUBBED   -> no banner (silence, no false marks)
 *   * ERROR     -> subdued "AI scan incomplete" banner (label NOT rendered)
 *   * loading / 404 / null -> subdued hint, no banner
 */
import * as React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';

import { FindingsBannerPanel } from '../components/FindingsBannerPanel';
import type { FindingsResponse } from '../api/findingsClient';

const finding = (overrides: Partial<FindingsResponse['findings'][number]> = {}) => ({
  toolId: 'pneumothorax-detect',
  label: 'Pneumothorax (screening p=0.51, raw 0.0298 vs op 0.0098); screening signal only, not a read',
  confidence: 0.51,
  rawScore: 0.0298,
  opThreshold: 0.0098,
  evidenceRef: 'orthanc:instance/i-1',
  status: 'COMPLETE',
  ...overrides,
});

const response = (findings: FindingsResponse['findings']): FindingsResponse => ({
  studyInstanceUID: 'uid-1',
  workflowId: 'wf-1',
  findings,
  overallStatus: findings.some((f) => f.status === 'COMPLETE') ? 'COMPLETE' : 'STUBBED',
  generatedAt: '2026-08-09T12:00:00Z',
  updatedAt:   '2026-08-09T12:00:00Z',
});

const fetchOk =
  (body: FindingsResponse | null) =>
  async (_uid: string, _signal?: AbortSignal): Promise<FindingsResponse | null> =>
    body;

describe('FindingsBannerPanel', () => {
  afterEach(cleanup);

  it('renders finding.label verbatim -- the #86 margin text must not be summarized away', async () => {
    render(
      <FindingsBannerPanel
        studyInstanceUID="uid-1"
        fetchImpl={fetchOk(response([finding()]))}
      />,
    );

    // Wait for the async load to settle.
    const banner = await waitFor(() => screen.getByTestId('lhrad-finding-complete'));

    // The full label -- including the margin substring "raw 0.0298 vs op 0.0098" -- must be
    // present verbatim in the banner. This is #86's contract: any rework of the banner that
    // parses label into pieces and rebuilds it MUST preserve the margin text, or a positive
    // that reads as calibrated p=0.51 (coin flip) loses the "raw 3x op" signal.
    expect(banner.textContent).toContain('raw 0.0298 vs op 0.0098');
    // Belt and suspenders: assert the whole label substring, not just the margin fragment,
    // so a partial regression (drops "screening signal only") also fails this test.
    expect(banner.textContent).toContain(
      'Pneumothorax (screening p=0.51, raw 0.0298 vs op 0.0098); screening signal only, not a read',
    );
  });

  it('appends confidence in the standard (p=X.XX) shape when non-null', async () => {
    render(
      <FindingsBannerPanel
        studyInstanceUID="uid-1"
        fetchImpl={fetchOk(response([finding({ confidence: 0.51 })]))}
      />,
    );
    const banner = await waitFor(() => screen.getByTestId('lhrad-finding-complete'));
    // The banner appends "(p=0.51)" after the label; the label itself already carries the
    // margin. The `(p=...)` piece is the pre-#86 display and stays for continuity, but
    // never on its own.
    expect(banner.textContent).toContain('(p=0.51)');
  });

  it('omits (p=...) when confidence is null (referral rule, stub, no-torch lane)', async () => {
    render(
      <FindingsBannerPanel
        studyInstanceUID="uid-1"
        fetchImpl={fetchOk(response([
          finding({ confidence: null, rawScore: null, opThreshold: null,
                    label: 'Suspected foo (referral reason)' }),
        ]))}
      />,
    );
    const banner = await waitFor(() => screen.getByTestId('lhrad-finding-complete'));
    expect(banner.textContent).not.toContain('p=');
    expect(banner.textContent).toContain('Suspected foo (referral reason)');
  });

  it('renders nothing complete for STUBBED (silence, no false marks)', async () => {
    render(
      <FindingsBannerPanel
        studyInstanceUID="uid-1"
        fetchImpl={fetchOk(response([finding({ status: 'STUBBED' })]))}
      />,
    );
    await waitFor(() => screen.getByTestId('lhrad-findings-empty'));
    expect(screen.queryByTestId('lhrad-finding-complete')).toBeNull();
  });

  it('renders subdued ERROR banner (label NOT rendered -- no margin claim under error)', async () => {
    render(
      <FindingsBannerPanel
        studyInstanceUID="uid-1"
        fetchImpl={fetchOk(response([finding({ status: 'ERROR' })]))}
      />,
    );
    const err = await waitFor(() => screen.getByTestId('lhrad-finding-error'));
    // Design rule: an ERROR banner carries a fixed "incomplete" message, not the label,
    // because a partial run's label can be misleading. Pins that the margin text does NOT
    // slip in via a copy-paste of the COMPLETE branch.
    expect(err.textContent).not.toContain('raw');
    expect(err.textContent).toContain('AI scan incomplete');
  });

  it('handles multiple COMPLETE findings by rendering each one label-verbatim', async () => {
    render(
      <FindingsBannerPanel
        studyInstanceUID="uid-1"
        fetchImpl={fetchOk(response([
          finding({ toolId: 'pneumothorax-detect', label: 'Pneumothorax (raw 0.030 vs op 0.010)' }),
          finding({ toolId: 'effusion-detect',    label: 'Effusion (raw 0.021 vs op 0.014)',
                    rawScore: 0.021, opThreshold: 0.014 }),
        ]))}
      />,
    );

    await waitFor(() => expect(screen.getAllByTestId('lhrad-finding-complete')).toHaveLength(2));
    const banners = screen.getAllByTestId('lhrad-finding-complete');
    expect(banners[0].textContent).toContain('raw 0.030 vs op 0.010');
    expect(banners[1].textContent).toContain('raw 0.021 vs op 0.014');
  });
});

// Ambient afterEach comes from Vitest globals config, same as WorkList.test.tsx.
declare const afterEach: (fn: () => void) => void;
