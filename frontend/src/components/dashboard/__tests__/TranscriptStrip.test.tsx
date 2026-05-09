import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TranscriptStrip } from '../TranscriptStrip';
import type { TranscriptLine } from '../../../types';

/**
 * Synthetic stream mirroring what would arrive on the EventBus WebSocket:
 *   - "heard" → user audio transcribed
 *   - "brain" → hope-main pane output (PANE_MESSAGE)
 *   - "speaking" → Hope's TTS going out
 * The component should render the last N entries color-coded and preserve
 * chronological order.
 */
const FAKE_STREAM: TranscriptLine[] = [
  { id: 'a', kind: 'system',   text: 'wake (clap)',   timestamp: 1_700_000_000_000 },
  { id: 'b', kind: 'heard',    text: 'hey hope',       timestamp: 1_700_000_001_000 },
  { id: 'c', kind: 'brain',    text: 'hello',          timestamp: 1_700_000_002_000 },
  { id: 'd', kind: 'speaking', text: 'hello',          timestamp: 1_700_000_002_500 },
  { id: 'e', kind: 'heard',    text: 'what time is it', timestamp: 1_700_000_003_000 },
  { id: 'f', kind: 'brain',    text: '12:34 PM',       timestamp: 1_700_000_004_000 },
];

describe('TranscriptStrip', () => {
  it('renders empty state when given no events', () => {
    render(<TranscriptStrip transcripts={[]} />);
    expect(screen.getByTestId('transcript-strip')).toHaveAttribute('data-state', 'empty');
    expect(screen.getByText(/waiting for voice activity/i)).toBeInTheDocument();
  });

  it('renders the last 5 entries from a fake event stream', () => {
    render(<TranscriptStrip transcripts={FAKE_STREAM} limit={5} />);
    const strip = screen.getByTestId('transcript-strip');
    expect(strip).toHaveAttribute('data-state', 'populated');

    // The oldest entry ("wake (clap)") is outside the 5-line window.
    expect(screen.queryByText('wake (clap)')).not.toBeInTheDocument();

    // All remaining 5 should be rendered.
    expect(screen.getByText('hey hope')).toBeInTheDocument();
    expect(screen.getByText('12:34 PM')).toBeInTheDocument();
  });

  it('color-codes each kind of event with its own label', () => {
    render(<TranscriptStrip transcripts={FAKE_STREAM} limit={5} />);
    expect(screen.getAllByTestId('transcript-line-heard').length).toBe(2);
    expect(screen.getAllByTestId('transcript-line-brain').length).toBe(2);
    expect(screen.getByTestId('transcript-line-speaking')).toBeInTheDocument();
    expect(screen.getAllByText('HEARD').length).toBe(2);
    expect(screen.getAllByText('BRAIN ←').length).toBe(2);
    expect(screen.getByText('SPEAKING')).toBeInTheDocument();
  });

  it('respects a custom limit', () => {
    render(<TranscriptStrip transcripts={FAKE_STREAM} limit={2} />);
    const lines = screen.getAllByTestId(/transcript-line-/);
    expect(lines.length).toBe(2);
    expect(screen.getByText('what time is it')).toBeInTheDocument();
    expect(screen.getByText('12:34 PM')).toBeInTheDocument();
  });
});
