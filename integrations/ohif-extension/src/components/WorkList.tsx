/**
 * WorkList -- the priority-ordered reading list surfaced at /reading.
 *
 * Rendered by the customRoutes entry that the extension's preRegistration hook
 * injects into OHIF's router (see src/index.ts); there is no custom mode. It
 * replaces the built-in OHIF Study List with our own list that hits the
 * Worklist API and orders by priorityTier / priorityScore instead of just
 * StudyDate desc.
 *
 * Data flow:
 *   1. mount → fetchWorklist() from /reading-api/worklist
 *   2. sortByPriority defensively client-side
 *   3. render rows; click → emit StudyOpenedEvent + navigate to OHIF viewer
 *   4. auto-refresh every REFRESH_MS to pick up new studies + updated priorities
 *
 * Navigation: uses react-router-dom's useNavigate hook (client-side) rather
 * than window.location.assign (full page reload). The full-page path caused
 * a real UX bug -- after a Back from /viewer the browser landed on / (Study
 * List) rather than /reading, because the /viewer full-page load re-booted
 * OHIF and its startup routing pushed / to history before react-router
 * resolved /viewer. Client-side navigation stays inside the same SPA lifetime
 * and history behaves as expected. Our route is mounted inside OHIF's
 * BrowserRouter (via customRoutes), so useNavigate resolves correctly.
 *
 * Styling: intentionally plain HTML. `@ohif/ui` is React-17 + tightly coupled to
 * OHIF's Redux services, so importing its Table components requires being loaded
 * inside the OHIF app context. Rather than fight that from a standalone extension,
 * we render our own table with minimal inline styles. OHIF's app shell wraps
 * the route so we inherit its dark background.
 */
import * as React from 'react';
import { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';

import type { WorklistItem, WorklistResponse } from '../types';
import {
  fetchWorklist,
  sortByPriority,
  WorklistApiError,
} from '../api/worklistClient';
import { emitStudyOpenedEvent, buildViewerUrl } from '../api/eventClient';

/** How often to re-fetch the worklist (ms). Fresh data matters â€" a new stat
 *  case may have landed while the radiologist was reading the previous study. */
const REFRESH_MS = 30_000;

export interface WorkListProps {
  /** Radiologist identity if available from OHIF's user context. Passed to the
   *  StudyOpenedEvent so the orchestrator can track who opened what.
   *  M2 leaves this optional â€" dev stack has no auth yet. */
  radiologistId?: string;
  /** Overridable for tests. */
  onOpenStudy?: (studyInstanceUID: string) => void;
}

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; data: WorklistResponse }
  | { kind: 'error'; message: string; status?: number };

export const WorkList: React.FC<WorkListProps> = ({
  radiologistId,
  onOpenStudy,
}) => {
  const [state, setState] = useState<LoadState>({ kind: 'loading' });
  const navigate = useNavigate();

  const load = useCallback(async (signal: AbortSignal) => {
    try {
      const data = await fetchWorklist({ signal });
      // preserve `generatedAt`, replace items with locally-sorted list
      setState({
        kind: 'ready',
        data: { ...data, items: sortByPriority(data.items) },
      });
    } catch (err) {
      if ((err as Error).name === 'AbortError') return; // component unmounted / re-fetching
      const status = err instanceof WorklistApiError ? err.status : undefined;
      setState({
        kind: 'error',
        message: (err as Error).message || 'Worklist unavailable',
        status,
      });
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    const timer = setInterval(() => {
      // fire-and-forget; each interval uses its own AbortController so the
      // outer teardown only abandons the one in flight, not future ones.
      const c = new AbortController();
      load(c.signal);
    }, REFRESH_MS);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [load]);

  const openStudy = useCallback(
    (uid: string, accession: string, study?: WorklistItem) => {
      // Fire-and-forget event, then navigate. Never await â€" a slow event POST
      // should not delay the viewer opening.
      void emitStudyOpenedEvent(uid, { radiologistId });
      if (onOpenStudy) {
        onOpenStudy(uid);
      } else {
        // Client-side navigation via react-router: single history entry,
        // Back returns to /reading (see file header comment). Accession rides in the
        // URL so ReportActionsPanel can build the RIS deep link (#73 item 2);
        // modality/description let buildViewerUrl pick the CXR two-view hanging
        // protocol for chest radiographs (#73 item 4).
        navigate(buildViewerUrl(uid, accession, study));
      }
    },
    [radiologistId, onOpenStudy, navigate],
  );

  if (state.kind === 'loading') {
    return (
      <div data-testid="lhrad-worklist-loading" style={styles.centered}>
        Loading reading worklist…
      </div>
    );
  }

  if (state.kind === 'error') {
    return (
      <div data-testid="lhrad-worklist-error" style={styles.error} role="alert">
        <strong>Worklist unavailable.</strong>{' '}
        {state.status ? `HTTP ${state.status}: ` : ''}
        {state.message}
        <div style={{ marginTop: 8, fontSize: '0.9em', opacity: 0.8 }}>
          Retrying automatically every {REFRESH_MS / 1000}s.
        </div>
      </div>
    );
  }

  const { items, generatedAt } = state.data;

  return (
    <div data-testid="lhrad-worklist" style={styles.container}>
      <header style={styles.header}>
        <h2 style={styles.title}>Reading Worklist</h2>
        <span style={styles.meta}>
          {items.length} studies · updated {formatGeneratedAt(generatedAt)}
        </span>
      </header>
      {items.length === 0 ? (
        <div data-testid="lhrad-worklist-empty" style={styles.centered}>
          No studies pending read.
        </div>
      ) : (
        <table style={styles.table}>
          <thead>
            <tr>
              <th style={styles.th}>Priority</th>
              <th style={styles.th}>Score</th>
              <th style={styles.th}>Modality</th>
              <th style={styles.th}>Description</th>
              <th style={styles.th}>Study Date</th>
              <th style={styles.th}>Accession</th>
              <th style={styles.th}>Status</th>
              <th style={styles.th}>Assigned To</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <WorklistRow
                key={item.studyInstanceUID}
                item={item}
                onOpen={openStudy}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
};

const WorklistRow: React.FC<{
  item: WorklistItem;
  onOpen: (uid: string, accession: string, study?: WorklistItem) => void;
}> = ({ item, onOpen }) => {
  return (
    <tr
      data-testid={`lhrad-row-${item.studyInstanceUID}`}
      data-priority-tier={item.priorityTier}
      data-read-state={item.readState ?? ''}
      onClick={() => onOpen(item.studyInstanceUID, item.accessionNumber, item)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpen(item.studyInstanceUID, item.accessionNumber, item);
        }
      }}
      tabIndex={0}
      style={item.readState ? styles.readRow : styles.row}
    >
      <td style={{ ...styles.td, ...tierBadgeStyle(item.priorityTier) }}>
        {item.priorityTier}
      </td>
      <td style={styles.td}>{item.priorityScore}</td>
      <td style={styles.td}>{item.modality || 'â€"'}</td>
      <td style={styles.td}>{item.studyDescription || 'â€"'}</td>
      <td style={styles.td}>{formatDicomDate(item.studyDate)}</td>
      <td style={styles.td}>{item.accessionNumber || 'â€"'}</td>
      <td style={styles.td}>
        {item.readState ? (
          <span style={styles.readBadge}>Read</span>
        ) : (
          <em style={styles.unassigned}>to read</em>
        )}
      </td>
      <td style={styles.td}>
        {item.assignment?.radiologistId ?? <em style={styles.unassigned}>unassigned</em>}
      </td>
    </tr>
  );
};

// --- format helpers ----------------------------------------------------------

/** DICOM YYYYMMDD -> YYYY-MM-DD for readability. Non-8-char input passes through. */
export function formatDicomDate(dicomDate: string): string {
  if (!dicomDate || dicomDate.length !== 8) return dicomDate || 'â€"';
  return `${dicomDate.slice(0, 4)}-${dicomDate.slice(4, 6)}-${dicomDate.slice(6, 8)}`;
}

export function formatGeneratedAt(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return iso;
  }
}

// --- inline styles ----------------------------------------------------------
// Inlined to avoid CSS-loader integration with OHIF's webpack â€" we're a single
// route with a small surface, and OHIF's webpack config is opinionated about
// CSS-modules. Trading a lint concern for a build simplicity win.
const styles: Record<string, React.CSSProperties> = {
  container: { padding: 16, color: '#e8eef3', fontFamily: 'sans-serif' },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'baseline',
    marginBottom: 12,
  },
  title: { margin: 0, fontSize: '1.5em' },
  meta: { fontSize: '0.9em', opacity: 0.7 },
  table: {
    width: '100%',
    borderCollapse: 'collapse',
    background: 'transparent',
  },
  th: {
    textAlign: 'left',
    padding: '8px 12px',
    borderBottom: '1px solid #37424c',
    fontWeight: 600,
    fontSize: '0.85em',
    textTransform: 'uppercase',
    letterSpacing: 0.5,
    opacity: 0.75,
  },
  td: { padding: '10px 12px', borderBottom: '1px solid #2a323b' },
  row: { cursor: 'pointer' },
  centered: { padding: 32, textAlign: 'center', opacity: 0.7 },
  error: {
    padding: 16,
    background: '#5c1f1f',
    border: '1px solid #a04040',
    borderRadius: 4,
    color: '#ffdada',
  },
  unassigned: { opacity: 0.5, fontSize: '0.9em' },
  // #108: a finished read stays visible (vanishing rows read as data loss during a demo) but
  // must never compete with work still to do, so the row is dimmed and the badge is calm.
  // Colour alone is not the signal: the Status column carries the word "Read" too.
  readBadge: { color: '#8fd9a8', fontWeight: 600 },
  readRow: { cursor: 'pointer', opacity: 0.55 },
};

function tierBadgeStyle(tier: string): React.CSSProperties {
  switch (tier) {
    case 'STAT':
      return { color: '#ffb3b3', fontWeight: 700 };
    case 'URGENT':
      return { color: '#ffd28a', fontWeight: 600 };
    default:
      return { color: '#a9b6c2' };
  }
}
