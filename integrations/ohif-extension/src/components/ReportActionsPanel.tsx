/**
 * ReportActionsPanel — right-side viewer panel offering actions on the study
 * the radiologist is currently reading.
 *
 * Today: one action — "Report this study" — opens the RIS order page in a new
 * tab, so the radiologist lands directly on the order they were reading.
 * #73 item 2.
 *
 * CORRECTION (#109, measured on the deployed o3 pin o3-010548c5, 2026-08-07):
 * this comment used to say the order page "carries the Claim Report action into
 * radiologyReport.form". It does not. That page renders no claim affordance at
 * all — confirmed in the DOM, with an account holding `Add Radiology Reports`,
 * so it is not a privilege gap. The claim is implicit in navigating to
 * `radiologyReport.form?orderId=<uuid>`, which creates the draft and redirects
 * to `?reportId=<n>`. The run-book's step says so explicitly.
 *
 * The target below is deliberately NOT repointed at radiologyReport.form: that
 * navigation has a side effect (it claims), so a radiologist clicking through
 * merely to look would silently take the report. Whether the panel should offer
 * a second, explicitly-labelled claim action is a workflow call for this
 * extension's owner, tracked on #109.
 *
 * URL, live-verified against the running o3 stack (2026-07-22): report
 * authoring hangs off the order page,
 * `/openmrs/module/radiology/radiologyOrder.form?orderId={orderUuid}`, and the
 * form accepts the order UUID. There is no accession-parameterized RIS page,
 * so the accession the viewer URL carries is first resolved to the order UUID
 * through the radiology module's REST index
 * (`/ws/rest/v1/radiologyorder?accessionNumber=`), same-origin through the
 * nginx `/openmrs/` proxy so the radiologist's existing RIS session
 * authenticates the lookup. If the lookup fails (not logged in, unknown
 * accession, RIS down), the button falls back to the RIS orders dashboard —
 * never a dead click. A deployment can still override the target via
 * `window.LHRAD_RIS_REPORT_URL_TEMPLATE`; a template carrying `{accession}`
 * skips resolution entirely and substitutes directly.
 *
 * Why a global rather than an env var: the OHIF viewer is a static SPA served
 * by nginx; there is no build-time env pipe from the compose file into the
 * bundle. The convention across other extensions is to pin runtime config via
 * globals set on `app-config.js` before the SPA loads. Same pattern here.
 *
 * Rendered as a panel, not a toolbar button: (a) it keeps discoverability
 * distinct from the DICOM cine/measurement tools OHIF puts in the main
 * toolbar, and (b) it leaves room for follow-up actions (e.g., "escalate",
 * "flag for teaching") without cluttering the toolbar. This panel slot was
 * previously occupied by PriorsPanel — see #73 item 3 discussion for why
 * PriorsPanel is unregistered pending backing endpoint.
 */
import * as React from 'react';

/** Overridable at runtime via `window.LHRAD_RIS_REPORT_URL_TEMPLATE`. A
 *  `{orderUuid}` token triggers accession→order resolution first; an
 *  `{accession}` token substitutes directly with no lookup. When no accession
 *  is present in the URL (unusual — the WorkList row click passes it
 *  explicitly), the button is disabled so we never open a broken link. */
const DEFAULT_RIS_REPORT_URL_TEMPLATE =
  '/openmrs/module/radiology/radiologyOrder.form?orderId={orderUuid}';

/** The radiology module's accession index (same lookup the orchestrator's
 *  ingest resolver uses server-side). Same-origin via the nginx /openmrs/
 *  proxy; the RIS session cookie authenticates it. */
const RESOLVE_ORDER_PATH =
  '/openmrs/ws/rest/v1/radiologyorder?v=custom:(uuid)&accessionNumber=';

/** Where the button lands when resolution fails: the RIS orders dashboard.
 *  OpenMRS itself bounces an unauthenticated hit through login and back. */
const RIS_FALLBACK_URL =
  '/openmrs/module/radiology/radiologyDashboardOrdersTab.htm';

const ACCESSION_URL_PARAM = 'accession';

declare global {
  interface Window {
    LHRAD_RIS_REPORT_URL_TEMPLATE?: string;
  }
}

export interface ReportActionsPanelProps {
  /** Overridable for tests. Reads window.location.search by default. */
  accession?: string | null;
  /** Overridable for tests. Reads window.LHRAD_RIS_REPORT_URL_TEMPLATE or falls back. */
  urlTemplate?: string;
  /** Overridable for tests — normally window.open. */
  openImpl?: (url: string, target: string) => void;
  /** Overridable for tests — normally global fetch. */
  fetchImpl?: typeof fetch;
}

export const ReportActionsPanel: React.FC<ReportActionsPanelProps> = ({
  accession,
  urlTemplate,
  openImpl,
  fetchImpl,
}) => {
  const effectiveAccession =
    accession !== undefined ? accession : readAccessionFromLocation();
  const effectiveTemplate =
    urlTemplate ??
    ((typeof window !== 'undefined' && window.LHRAD_RIS_REPORT_URL_TEMPLATE) ||
      DEFAULT_RIS_REPORT_URL_TEMPLATE);
  const effectiveOpen =
    openImpl ??
    ((url: string, target: string) => {
      if (typeof window !== 'undefined') {
        window.open(url, target);
      }
    });
  const effectiveFetch =
    fetchImpl ?? (typeof fetch !== 'undefined' ? fetch : undefined);

  const disabled = !effectiveAccession;

  const onReport = async () => {
    if (disabled || !effectiveAccession) return;
    // Operator override carrying {accession}: substitute directly, no lookup.
    if (effectiveTemplate.includes('{accession}')) {
      effectiveOpen(
        effectiveTemplate.replace(
          /\{accession\}/g,
          encodeURIComponent(effectiveAccession),
        ),
        '_blank',
      );
      return;
    }
    // Default path: resolve accession -> order uuid, then open the order page.
    let target = RIS_FALLBACK_URL;
    if (effectiveFetch) {
      try {
        const res = await effectiveFetch(
          RESOLVE_ORDER_PATH + encodeURIComponent(effectiveAccession),
          { credentials: 'include' },
        );
        if (res.ok) {
          const body = await res.json();
          const uuid = body?.results?.[0]?.uuid;
          if (uuid) {
            target = effectiveTemplate.replace(
              /\{orderUuid\}/g,
              encodeURIComponent(uuid),
            );
          }
        }
      } catch {
        // fall through to the dashboard — never a dead click
      }
    }
    effectiveOpen(target, '_blank');
  };

  return (
    <div data-testid="lhrad-report-actions-panel" style={styles.panel}>
      <h3 style={styles.title}>Report Actions</h3>

      <button
        type="button"
        data-testid="lhrad-report-this-study"
        onClick={onReport}
        disabled={disabled}
        style={{
          ...styles.button,
          ...(disabled ? styles.buttonDisabled : {}),
        }}
      >
        Report this study
      </button>

      {disabled && (
        <p data-testid="lhrad-report-actions-hint" style={styles.hint}>
          Open a study from the Reading Worklist to enable reporting.
        </p>
      )}

      {!disabled && (
        <p style={styles.hint}>
          Opens the RIS report authoring page for accession{' '}
          <code style={styles.code}>{effectiveAccession}</code> in a new tab.
        </p>
      )}
    </div>
  );
};

function readAccessionFromLocation(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return new URLSearchParams(window.location.search).get(ACCESSION_URL_PARAM);
  } catch {
    return null;
  }
}

const styles: Record<string, React.CSSProperties> = {
  panel: {
    padding: 12,
    color: '#e8eef3',
    fontFamily: 'sans-serif',
    fontSize: 14,
  },
  title: {
    margin: '4px 0 12px',
    fontSize: '1.1em',
    borderBottom: '1px solid #37424c',
    paddingBottom: 6,
  },
  button: {
    display: 'block',
    width: '100%',
    padding: '10px 12px',
    background: '#2f6cb3',
    color: '#fff',
    border: 'none',
    borderRadius: 4,
    fontSize: '0.95em',
    fontWeight: 600,
    cursor: 'pointer',
  },
  buttonDisabled: {
    background: '#2f3841',
    color: '#8a95a0',
    cursor: 'not-allowed',
  },
  hint: {
    marginTop: 10,
    fontSize: '0.85em',
    opacity: 0.6,
    lineHeight: 1.4,
  },
  code: {
    fontFamily: 'monospace',
    background: '#232a31',
    padding: '1px 4px',
    borderRadius: 3,
  },
};
