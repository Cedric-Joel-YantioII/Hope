import { create } from 'zustand';
import type {
  BrainState,
  BridgeEvent,
  EchoGuardState,
  MemoryEntry,
  SpecialistPane,
  TranscriptLine,
} from '../types';

const MAX_TRANSCRIPT = 64;
const MAX_MEMORY = 20;

function uid(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function pushCapped<T>(arr: T[], item: T, max: number): T[] {
  const next = [...arr, item];
  if (next.length <= max) return next;
  return next.slice(next.length - max);
}

interface DashboardStore {
  bridgeConnected: boolean;
  bridgeError: string | null;

  brainState: BrainState;
  brainMainPaneId: string | null;
  lastWake: { source: string; timestamp: number } | null;
  lastSleep: number | null;

  transcripts: TranscriptLine[];
  specialists: SpecialistPane[];
  memory: MemoryEntry[];
  echo: EchoGuardState;

  ragLastSync: number | null;
  ragInFlight: number;

  listeningPaused: boolean;

  ingest: (event: BridgeEvent) => void;
  setBridgeStatus: (connected: boolean, error?: string | null) => void;
  clearTranscripts: () => void;
  setBrainState: (state: BrainState) => void;
  setListeningPaused: (paused: boolean) => void;
}

export const useDashboardStore = create<DashboardStore>((set) => ({
  bridgeConnected: false,
  bridgeError: null,

  brainState: 'sleeping',
  brainMainPaneId: null,
  lastWake: null,
  lastSleep: null,

  transcripts: [],
  specialists: [],
  memory: [],
  echo: { speaking: false, echoWindowSize: 0, brainBusy: false },

  ragLastSync: null,
  ragInFlight: 0,

  listeningPaused: false,

  setBridgeStatus: (connected, error = null) =>
    set(() => ({ bridgeConnected: connected, bridgeError: error })),

  clearTranscripts: () => set(() => ({ transcripts: [] })),

  setBrainState: (brainState) => set(() => ({ brainState })),

  setListeningPaused: (paused) => set(() => ({ listeningPaused: paused })),

  ingest: (event) =>
    set((state) => {
      const ts = event.timestamp * 1000;
      const data = event.data ?? {};
      switch (event.type) {
        case 'wake_trigger': {
          const source = String(data.source ?? 'unknown');
          // Bring the window forward on wake — but only if listening is
          // NOT paused. When the user has muted Hope, wake events should
          // never yank focus out of whatever app they're working in.
          // (This still lets a manually-triggered wake — tray menu,
          // programmatic — pull the dashboard forward when listening is
          // active, which is the expected affordance.)
          if (!state.listeningPaused) {
            (async () => {
              try {
                const { getCurrentWindow } = await import(
                  '@tauri-apps/api/window'
                );
                const w = getCurrentWindow();
                await w.unminimize();
                await w.show();
                await w.setFocus();
              } catch {
                /* not running inside Tauri, or window API unavailable */
              }
            })();
          }
          return {
            lastWake: { source, timestamp: ts },
            brainState: 'idle',
            transcripts: pushCapped(
              state.transcripts,
              { id: uid(), kind: 'system', text: `wake (${source})`, timestamp: ts },
              MAX_TRANSCRIPT,
            ),
          };
        }
        case 'speech_transcript': {
          const text = String(data.text ?? '').trim();
          if (!text) return {};
          return {
            transcripts: pushCapped(
              state.transcripts,
              { id: uid(), kind: 'heard', text, timestamp: ts },
              MAX_TRANSCRIPT,
            ),
          };
        }
        case 'speaking_started': {
          // Hope's TTS started. Override any other state so the orb
          // immediately switches to the green speaking palette.
          return { brainState: 'speaking' };
        }
        case 'speaking_ended': {
          // Drop back to idle so the orb stops the speaking envelope.
          return { brainState: 'idle' };
        }
        case 'agent_turn_start':
        case 'inference_start':
          return { brainState: 'thinking' };
        case 'agent_turn_end':
        case 'inference_end':
          return { brainState: 'idle' };
        case 'pane_spawned': {
          const paneId = String(data.pane_id ?? data.paneId ?? uid());
          const role = String(data.role ?? data.pane_name ?? 'specialist');
          const pane: SpecialistPane = { paneId, role, spawnedAt: ts };
          const isMain = role === 'hope-main';
          return {
            brainMainPaneId: isMain ? paneId : state.brainMainPaneId,
            brainState: isMain ? 'idle' : state.brainState,
            specialists: isMain
              ? state.specialists
              : [...state.specialists.filter((p) => p.paneId !== paneId), pane],
          };
        }
        case 'pane_killed': {
          const paneId = String(data.pane_id ?? data.paneId ?? '');
          const role = String(data.role ?? data.pane_name ?? '');
          if (role === 'hope-main' || paneId === state.brainMainPaneId) {
            return {
              brainState: 'sleeping',
              brainMainPaneId: null,
              lastSleep: ts,
              specialists: [],
            };
          }
          return {
            specialists: state.specialists.filter((p) => p.paneId !== paneId),
          };
        }
        case 'pane_message': {
          const paneId = String(data.pane_id ?? data.paneId ?? '');
          const text = String(data.text ?? data.message ?? '').trim();
          if (!paneId && !text) return {};
          const updated = state.specialists.map((p) =>
            p.paneId === paneId ? { ...p, lastMessageAt: ts } : p,
          );
          if (text) {
            return {
              specialists: updated,
              transcripts: pushCapped(
                state.transcripts,
                { id: uid(), kind: 'brain', text, timestamp: ts },
                MAX_TRANSCRIPT,
              ),
            };
          }
          return { specialists: updated };
        }
        case 'memory_store': {
          const content = String(data.content ?? data.value ?? '').trim();
          if (!content) return {};
          const entry: MemoryEntry = {
            id: uid(),
            content,
            namespace:
              typeof data.namespace === 'string' ? data.namespace : undefined,
            timestamp: ts,
          };
          return { memory: pushCapped(state.memory, entry, MAX_MEMORY) };
        }
        case 'listening_paused':
          return { listeningPaused: true };
        case 'listening_resumed':
          return { listeningPaused: false };
        case 'state_snapshot': {
          // Sent by the daemon right after the websocket handshake so the
          // store reflects backend reality on every (re)connect — not the
          // React defaults. Replaces the listed fields; ignores keys we
          // don't yet bind UI to.
          const update: Record<string, unknown> = {};
          if (typeof data.listening_paused === 'boolean') {
            update.listeningPaused = data.listening_paused;
          }
          if (typeof data.brain_state === 'string') {
            update.brainState = data.brain_state;
          }
          if (
            data.hope_main_pane_id === null ||
            typeof data.hope_main_pane_id === 'string'
          ) {
            update.brainMainPaneId = data.hope_main_pane_id ?? null;
          }
          // Hydrate live specialists. Without this the panel only fills
          // from pane_spawned events that arrive AFTER the WS connects —
          // anything spawned before connect would be invisible until it
          // sends a pane_message.
          if (Array.isArray(data.specialists)) {
            update.specialists = data.specialists
              .filter((s): s is Record<string, unknown> =>
                s !== null && typeof s === 'object',
              )
              .map((s) => ({
                paneId: String(s.pane_id ?? s.paneId ?? uid()),
                role: String(s.role ?? 'specialist'),
                spawnedAt:
                  typeof s.spawned_at === 'number'
                    ? s.spawned_at * 1000
                    : typeof s.spawnedAt === 'number'
                      ? s.spawnedAt
                      : Date.now(),
              })) as SpecialistPane[];
          }
          return update;
        }
        case 'scheduler_task_start':
          return { ragInFlight: state.ragInFlight + 1 };
        case 'scheduler_task_end':
          return {
            ragInFlight: Math.max(0, state.ragInFlight - 1),
            ragLastSync: ts,
          };
        default:
          return {};
      }
    }),
}));
