import { useEffect, useRef, useState } from 'react';
import { RefreshCcw } from 'lucide-react';
import { tailDaemonLog } from '../lib/api';

/**
 * Minimal live tail of ``~/.hope/daemon.log``. The Tauri backend streams the
 * last 32 KiB and this page polls every 1.5s while visible.
 */
export function LogsPage() {
  const [body, setBody] = useState('');
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      try {
        const text = await tailDaemonLog(64 * 1024);
        if (!cancelled) {
          setBody(text);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    };
    pull();
    const id = window.setInterval(pull, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [body]);

  return (
    <div className="h-full flex flex-col overflow-hidden px-5 py-4">
      <header className="flex items-center justify-between mb-2 shrink-0">
        <h1 className="text-sm font-semibold" style={{ color: 'var(--color-text)' }}>
          daemon.log
        </h1>
        <div className="flex items-center gap-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          <RefreshCcw size={12} /> live tail
        </div>
      </header>

      {error && (
        <div
          className="text-xs px-3 py-2 mb-2 rounded"
          style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-error, #e76f6f)' }}
        >
          {error}
        </div>
      )}

      <pre
        className="flex-1 overflow-auto rounded-md p-3 font-mono text-[11px] leading-tight whitespace-pre-wrap"
        style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }}
      >
        {body || '(log file empty or not found)'}
        <div ref={bottomRef} />
      </pre>
    </div>
  );
}
