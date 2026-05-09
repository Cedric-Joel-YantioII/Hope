// Shared types for the Hope wake-triggered dashboard. These are thin — the
// bulk of our runtime data comes off the EventBus WebSocket bridge as free-
// form JSON and is normalised into these shapes by the store.

/** Matches the JSON envelope emitted by `hope.daemon.dashboard_bridge`. */
export interface BridgeEvent {
  type: string;
  timestamp: number;
  data: Record<string, unknown>;
}

export type BrainState = 'sleeping' | 'idle' | 'thinking' | 'speaking';

export type TranscriptKind = 'heard' | 'brain' | 'speaking' | 'system';

export interface TranscriptLine {
  id: string;
  kind: TranscriptKind;
  text: string;
  timestamp: number;
}

export interface SpecialistPane {
  paneId: string;
  role: string;
  spawnedAt: number;
  lastMessageAt?: number;
}

export interface MemoryEntry {
  id: string;
  content: string;
  namespace?: string;
  timestamp: number;
}

export interface EchoGuardState {
  speaking: boolean;
  echoWindowSize: number;
  brainBusy: boolean;
}
