import { useEffect, useMemo, useState } from 'react';
import { Loader2, MicOff, Mic } from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';
import { useDashboardStore } from '../lib/store';
import { TranscriptStrip } from '../components/dashboard/TranscriptStrip';
import { WaveformOrb } from '../components/dashboard/WaveformOrb';
import { searchMemory, sendDaemonControl } from '../lib/api';

const BRAIN_COLORS: Record<string, string> = {
  sleeping: 'var(--color-text-tertiary, #8b8b8b)',
  idle:     'var(--color-accent, #7cc3ff)',
  thinking: 'var(--color-accent-purple, #c59cff)',
  speaking: 'var(--color-success, #5fd39a)',
};

function formatAgo(ts: number | null | undefined): string {
  if (!ts) return '—';
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

export function DashboardPage() {
  const {
    transcripts,
    brainState,
    brainMainPaneId,
    lastWake,
    specialists,
    memory,
    echo,
    ragLastSync,
    ragInFlight,
  } = useDashboardStore(
    useShallow((s) => ({
      transcripts: s.transcripts,
      brainState: s.brainState,
      brainMainPaneId: s.brainMainPaneId,
      lastWake: s.lastWake,
      specialists: s.specialists,
      memory: s.memory,
      echo: s.echo,
      ragLastSync: s.ragLastSync,
      ragInFlight: s.ragInFlight,
      listeningPaused: s.listeningPaused,
    })),
  );
  const { listeningPaused } = useDashboardStore(
    useShallow((s) => ({ listeningPaused: s.listeningPaused })),
  );

  const onToggleListening = async () => {
    try {
      const resp = await sendDaemonControl<{ ok: boolean; paused?: boolean }>(
        'toggle_listening',
      );
      if (resp?.ok && typeof resp.paused === 'boolean') {
        useDashboardStore.getState().setListeningPaused(resp.paused);
      }
    } catch (err) {
      console.warn('toggle_listening failed', err);
    }
  };

  const [memoryQuery, setMemoryQuery] = useState('');
  const [memoryResults, setMemoryResults] = useState<Array<Record<string, unknown>> | null>(null);
  const [memoryPending, setMemoryPending] = useState(false);
  // Panels collapse to a chevron in idle/sleeping. User can force-open.
  // Auto-expands on thinking/speaking — see render below.
  const [panelsForceOpen, setPanelsForceOpen] = useState(false);

  useEffect(() => {
    const id = window.setInterval(() => {
      // Force a re-render for time-ago strings.
      setMemoryPending((p) => p);
    }, 5000);
    return () => window.clearInterval(id);
  }, []);

  const onMemorySearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!memoryQuery.trim()) {
      setMemoryResults(null);
      return;
    }
    setMemoryPending(true);
    const resp = await searchMemory(memoryQuery.trim(), 10);
    setMemoryPending(false);
    setMemoryResults(resp.results ?? []);
  };

  const brainColor = BRAIN_COLORS[brainState] ?? BRAIN_COLORS.idle;

  const sortedSpecialists = useMemo(
    () => [...specialists].sort((a, b) => a.spawnedAt - b.spawnedAt),
    [specialists],
  );

  return (
    <div className="h-full overflow-y-auto px-5 py-4">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 max-w-6xl mx-auto">
        {/* Hero: audio-reactive orb. Mic-driven bars in idle/listening,
            synthesised wave in speaking, orbiting arcs in thinking. */}
        <div className="lg:col-span-3 flex justify-center pt-6 pb-2">
          <WaveformOrb
            brainState={brainState}
            listeningPaused={listeningPaused}
            size={320}
          />
        </div>

        {/* Transcript strip — full width */}
        <div className="lg:col-span-3">
          <TranscriptStrip transcripts={transcripts} limit={5} />
        </div>

        {/* Idle/sleeping = hero-only aesthetic. The panels collapse to a
            thin chevron toggle so the orb dominates the canvas. They
            auto-expand the moment Hope wakes into thinking/speaking, and
            the user can tap the chevron to peek any time. */}
        {(() => {
          const autoExpand = brainState === 'thinking' || brainState === 'speaking';
          const expanded = panelsForceOpen || autoExpand;
          return (
            <div className="lg:col-span-3 flex justify-center">
              <button
                type="button"
                onClick={() => setPanelsForceOpen((v) => !v)}
                className="text-xs uppercase tracking-[0.25em] py-1 px-3 rounded-full transition-opacity"
                style={{
                  color: 'var(--color-text-tertiary)',
                  background: 'var(--color-bg-secondary)',
                  border: '1px solid var(--color-border)',
                  opacity: expanded ? 0.55 : 0.85,
                }}
                aria-expanded={expanded}
                title={expanded ? 'collapse panels' : 'expand panels'}
              >
                {expanded ? '▲ collapse' : '▼ details'}
              </button>
            </div>
          );
        })()}

        {/* Left column: brain state + echo-guard */}
        <section
          className="flex flex-col gap-4 transition-all duration-500 ease-out"
          style={{
            maxHeight:
              panelsForceOpen || brainState === 'thinking' || brainState === 'speaking'
                ? '2000px'
                : '0',
            opacity:
              panelsForceOpen || brainState === 'thinking' || brainState === 'speaking'
                ? 1
                : 0,
            overflow: 'hidden',
            pointerEvents:
              panelsForceOpen || brainState === 'thinking' || brainState === 'speaking'
                ? 'auto'
                : 'none',
          }}>
          <Panel title="Brain">
            <div className="flex items-center gap-3">
              <span
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium"
                style={{ background: 'var(--color-bg-secondary)', color: brainColor }}
                data-testid="brain-state"
              >
                {brainState === 'thinking' && (
                  <Loader2 size={12} className="animate-spin" />
                )}
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: brainColor }} />
                {brainState}
              </span>
              <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                wake {formatAgo(lastWake?.timestamp)}
                {lastWake?.source ? ` · ${lastWake.source}` : ''}
              </span>
            </div>
            <div className="mt-3 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              hope-main pane: <span style={{ color: 'var(--color-text)' }}>
                {brainMainPaneId ?? '(none)'}
              </span>
            </div>
            <button
              type="button"
              onClick={onToggleListening}
              className="mt-3 inline-flex items-center gap-2 px-3 py-1.5 rounded-md text-xs font-medium transition-colors"
              style={{
                background: listeningPaused
                  ? 'var(--color-warning, #f0b429)'
                  : 'var(--color-bg-secondary)',
                color: listeningPaused ? '#000' : 'var(--color-text)',
                border: '1px solid var(--color-border, rgba(255,255,255,0.1))',
              }}
              data-testid="toggle-listening"
            >
              {listeningPaused ? (
                <>
                  <MicOff size={14} /> Paused — tap to resume
                </>
              ) : (
                <>
                  <Mic size={14} /> Listening — tap to pause
                </>
              )}
            </button>
          </Panel>

          <Panel title="Echo guard">
            <div className="grid grid-cols-3 gap-2 text-xs">
              <Metric label="speaking" value={echo.speaking ? 'yes' : 'no'} />
              <Metric label="echo window" value={String(echo.echoWindowSize)} />
              <Metric label="brain busy" value={echo.brainBusy ? 'yes' : 'no'} />
            </div>
          </Panel>

          <Panel title="RAG ingestion">
            <div className="grid grid-cols-2 gap-2 text-xs">
              <Metric label="in flight" value={String(ragInFlight)} />
              <Metric label="last sync" value={formatAgo(ragLastSync)} />
            </div>
          </Panel>
        </section>

        {/* Middle: specialists grid */}
        <section
          className="lg:col-span-1 transition-all duration-500 ease-out"
          style={{
            maxHeight:
              panelsForceOpen || brainState === 'thinking' || brainState === 'speaking'
                ? '2000px'
                : '0',
            opacity:
              panelsForceOpen || brainState === 'thinking' || brainState === 'speaking'
                ? 1
                : 0,
            overflow: 'hidden',
            pointerEvents:
              panelsForceOpen || brainState === 'thinking' || brainState === 'speaking'
                ? 'auto'
                : 'none',
          }}
        >
          <Panel title={`Specialists (${sortedSpecialists.length})`}>
            {sortedSpecialists.length === 0 ? (
              <div className="text-xs py-6 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
                no specialists spawned
              </div>
            ) : (
              <ul className="flex flex-col divide-y" style={{ borderColor: 'var(--color-border)' }}>
                {sortedSpecialists.map((p) => (
                  <li key={p.paneId} className="py-1.5 flex items-baseline gap-2 text-xs">
                    <span
                      className="shrink-0 font-medium"
                      style={{ color: 'var(--color-text)' }}
                    >
                      {p.role}
                    </span>
                    <span
                      className="shrink-0 font-mono"
                      style={{ color: 'var(--color-text-tertiary)' }}
                    >
                      {p.paneId}
                    </span>
                    <span className="ml-auto" style={{ color: 'var(--color-text-tertiary)' }}>
                      {formatAgo(p.spawnedAt)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </section>

        {/* Right: memory panel */}
        <section
          className="lg:col-span-1 transition-all duration-500 ease-out"
          style={{
            maxHeight:
              panelsForceOpen || brainState === 'thinking' || brainState === 'speaking'
                ? '2000px'
                : '0',
            opacity:
              panelsForceOpen || brainState === 'thinking' || brainState === 'speaking'
                ? 1
                : 0,
            overflow: 'hidden',
            pointerEvents:
              panelsForceOpen || brainState === 'thinking' || brainState === 'speaking'
                ? 'auto'
                : 'none',
          }}
        >
          <Panel title="Memory">
            <form onSubmit={onMemorySearch} className="flex items-center gap-2 mb-2">
              <input
                type="text"
                placeholder="search memory…"
                value={memoryQuery}
                onChange={(e) => setMemoryQuery(e.target.value)}
                className="flex-1 px-2 py-1 rounded text-xs outline-none"
                style={{
                  background: 'var(--color-bg-secondary)',
                  color: 'var(--color-text)',
                  border: '1px solid var(--color-border)',
                }}
              />
              <button
                type="submit"
                disabled={memoryPending}
                className="px-2.5 py-1 rounded text-xs cursor-pointer disabled:opacity-60"
                style={{
                  background: 'var(--color-bg-secondary)',
                  color: 'var(--color-text-secondary)',
                  border: '1px solid var(--color-border)',
                }}
              >
                {memoryPending ? '…' : 'search'}
              </button>
            </form>

            {memoryResults ? (
              <ul className="flex flex-col gap-1">
                {memoryResults.length === 0 ? (
                  <li className="text-xs py-2" style={{ color: 'var(--color-text-tertiary)' }}>
                    no matches
                  </li>
                ) : (
                  memoryResults.slice(0, 8).map((r, i) => (
                    <li key={i} className="text-xs truncate" title={JSON.stringify(r)}>
                      {String((r as { content?: string }).content ?? JSON.stringify(r))}
                    </li>
                  ))
                )}
              </ul>
            ) : memory.length === 0 ? (
              <div className="text-xs py-6 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
                no recent memory writes
              </div>
            ) : (
              <ul className="flex flex-col gap-1">
                {memory.slice(-10).reverse().map((m) => (
                  <li key={m.id} className="text-xs flex items-baseline gap-2">
                    <span
                      className="shrink-0 tabular-nums"
                      style={{ color: 'var(--color-text-tertiary)' }}
                    >
                      {formatAgo(m.timestamp)}
                    </span>
                    <span className="truncate" style={{ color: 'var(--color-text)' }} title={m.content}>
                      {m.content}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </section>
      </div>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section
      className="rounded-lg p-3"
      style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}
    >
      <h2
        className="text-[10px] uppercase tracking-wider mb-2"
        style={{ color: 'var(--color-text-tertiary)' }}
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div
      className="rounded px-2 py-1.5"
      style={{ background: 'var(--color-bg-secondary)' }}
    >
      <div className="text-[10px] uppercase tracking-wide" style={{ color: 'var(--color-text-tertiary)' }}>
        {label}
      </div>
      <div className="text-sm tabular-nums" style={{ color: 'var(--color-text)' }}>
        {value}
      </div>
    </div>
  );
}
