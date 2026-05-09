import { useMemo } from 'react';
import type { TranscriptLine } from '../../types';

export interface TranscriptStripProps {
  /** The full transcript buffer — we render only the last `limit` entries. */
  transcripts: TranscriptLine[];
  /** How many lines to show. Defaults to 5 per spec. */
  limit?: number;
}

const KIND_STYLES: Record<TranscriptLine['kind'], { label: string; color: string }> = {
  heard:    { label: 'HEARD',    color: 'var(--color-accent-blue, #6ec6ff)' },
  brain:    { label: 'BRAIN ←',  color: 'var(--color-accent-purple, #c59cff)' },
  speaking: { label: 'SPEAKING', color: 'var(--color-success, #5fd39a)' },
  system:   { label: 'SYSTEM',   color: 'var(--color-text-tertiary, #8b8b8b)' },
};

function formatTimeHMS(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleTimeString('en-US', {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/**
 * Compact top-of-dashboard strip showing the latest voice loop events,
 * color-coded by direction. Pure/presentational — all state comes in via
 * props so the component is trivially testable.
 */
export function TranscriptStrip({ transcripts, limit = 5 }: TranscriptStripProps) {
  const tail = useMemo(() => transcripts.slice(-limit), [transcripts, limit]);

  if (tail.length === 0) {
    return (
      <div
        data-testid="transcript-strip"
        data-state="empty"
        className="w-full py-3 px-4 text-xs rounded-md font-mono"
        style={{
          background: 'var(--color-surface)',
          color: 'var(--color-text-tertiary)',
          border: '1px solid var(--color-border)',
        }}
      >
        waiting for voice activity…
      </div>
    );
  }

  return (
    <div
      data-testid="transcript-strip"
      data-state="populated"
      className="w-full rounded-md font-mono text-xs overflow-hidden"
      style={{
        background: 'var(--color-surface)',
        border: '1px solid var(--color-border)',
      }}
    >
      <ul className="flex flex-col divide-y" style={{ borderColor: 'var(--color-border)' }}>
        {tail.map((line) => {
          const style = KIND_STYLES[line.kind];
          return (
            <li
              key={line.id}
              data-testid={`transcript-line-${line.kind}`}
              className="flex items-baseline gap-2 px-3 py-1.5"
            >
              <span
                className="shrink-0 tabular-nums"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                {formatTimeHMS(line.timestamp)}
              </span>
              <span
                className="shrink-0 uppercase tracking-wide"
                style={{ color: style.color, fontWeight: 600 }}
              >
                {style.label}
              </span>
              <span
                className="truncate"
                style={{ color: 'var(--color-text)' }}
                title={line.text}
              >
                {line.text}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
