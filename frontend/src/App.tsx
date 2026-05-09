import { useEffect } from 'react';
import { Routes, Route } from 'react-router';
import { Layout } from './components/Layout';
import { DashboardPage } from './pages/DashboardPage';
import { LogsPage } from './pages/LogsPage';
import { isTauri } from './lib/api';
import { useDashboardStore } from './lib/store';
import type { BridgeEvent } from './types';

/**
 * Root of the wake-triggered dashboard.
 *
 * Responsibilities:
 *   * Subscribe to Tauri events ``hope:event`` (EventBus forward) +
 *     ``hope:bridge-status`` (connection health).
 *   * Dispatch incoming events into the zustand store.
 */
export default function App() {
  const ingest = useDashboardStore((s) => s.ingest);
  const setBridgeStatus = useDashboardStore((s) => s.setBridgeStatus);

  useEffect(() => {
    // Light/dark toggle driven by OS preference; no user-facing setting.
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const apply = () => {
      const root = document.documentElement;
      if (media.matches) {
        root.classList.add('dark');
        root.classList.remove('light');
      } else {
        root.classList.add('light');
        root.classList.remove('dark');
      }
    };
    apply();
    media.addEventListener('change', apply);
    return () => media.removeEventListener('change', apply);
  }, []);

  useEffect(() => {
    if (!isTauri()) return;
    let unEvent: (() => void) | undefined;
    let unStatus: (() => void) | undefined;
    (async () => {
      const { listen } = await import('@tauri-apps/api/event');
      unEvent = await listen<BridgeEvent>('hope:event', (e) => ingest(e.payload));
      unStatus = await listen<{ connected: boolean; error?: string }>(
        'hope:bridge-status',
        (e) => setBridgeStatus(e.payload.connected, e.payload.error ?? null),
      );
    })();
    return () => {
      unEvent?.();
      unStatus?.();
    };
  }, [ingest, setBridgeStatus]);

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="logs" element={<LogsPage />} />
      </Route>
    </Routes>
  );
}
