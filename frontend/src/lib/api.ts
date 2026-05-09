// -----------------------------------------------------------------------
// api.ts — thin wrapper around the Tauri `invoke` bridge.
//
// The frontend no longer talks to an HTTP server. Instead it:
//   * receives live EventBus events via `listen('hope:event')`
//   * invokes the daemon's Unix control socket via `send_daemon_control`
//   * tails the daemon log via `tail_daemon_log`
// -----------------------------------------------------------------------

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

export const isTauri = (): boolean =>
  typeof window !== 'undefined' && !!window.__TAURI_INTERNALS__;

/** Payload shape of the `hope:event` Tauri event — mirrors the WS envelope. */
export interface BridgeEvent {
  type: string;
  timestamp: number;
  data: Record<string, unknown>;
}

export interface BridgeStatusEvent {
  connected: boolean;
  error?: string;
}

/** Send a JSON-RPC-ish message to ~/.hope/daemon.sock. */
export async function sendDaemonControl<T = Record<string, unknown>>(
  cmd: string,
  payload?: Record<string, unknown>,
): Promise<T> {
  if (!isTauri()) {
    throw new Error('daemon control is only available in the Tauri shell');
  }
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke<T>('send_daemon_control', { cmd, payload });
}

/** Tail the last N bytes of ~/.hope/daemon.log. */
export async function tailDaemonLog(maxBytes = 32 * 1024): Promise<string> {
  if (!isTauri()) return '';
  const { invoke } = await import('@tauri-apps/api/core');
  try {
    return await invoke<string>('tail_daemon_log', { maxBytes });
  } catch {
    return '';
  }
}

/** Manually show / hide the dashboard window. */
export async function showDashboardWindow(): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import('@tauri-apps/api/core');
  await invoke('show_window');
}

export async function hideDashboardWindow(): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import('@tauri-apps/api/core');
  await invoke('hide_window');
}

/**
 * Search the daemon's memory via the control socket. Relies on the daemon
 * handling a `memory_search` command — if it isn't wired up yet, the call
 * will surface as a {ok: false, error: ...} response and callers can fall
 * back gracefully.
 */
export async function searchMemory(
  query: string,
  topK: number = 10,
): Promise<{ ok: boolean; results?: Array<Record<string, unknown>>; error?: string }> {
  try {
    return await sendDaemonControl('memory_search', { query, top_k: topK });
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

/** Return the WS endpoint info the bridge-client uses (debug only). */
export async function dashboardEndpoint(): Promise<{
  host: string;
  port: number;
  url: string;
} | null> {
  if (!isTauri()) return null;
  const { invoke } = await import('@tauri-apps/api/core');
  try {
    return await invoke('dashboard_endpoint');
  } catch {
    return null;
  }
}
