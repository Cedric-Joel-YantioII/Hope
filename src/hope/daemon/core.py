"""Hope brain daemon — long-lived process owning the tmux orchestrator.

Responsibilities
----------------
* Spawn and hold the :class:`~hope.agents.tmux_orchestrator.TmuxOrchestrator`
  singleton (which in turn brings up the hope-main pane running
  ``claude --dangerously-skip-permissions``).
* Hold the :class:`~hope.wakeword.WakeMonitor` if the module is importable.
  The wake-word module is built by a sibling agent; we therefore guard the
  import and fall back to "no mic monitor" rather than crashing the daemon.
* Expose a Unix-domain JSON-RPC-ish control socket at ``~/.hope/daemon.sock``
  so ``hope wake`` / ``hope sleep`` / ``hope status`` can talk to a running
  daemon without having to dlopen the whole brain.
* Handle SIGTERM / SIGINT → graceful shutdown: orchestrator.shutdown(),
  wake_monitor.stop(), remove PID file.
* Write ``~/.hope/daemon.pid`` on start, remove it on clean exit.

The daemon is intentionally built from primitives the rest of the codebase
already ships (the orchestrator, the event bus) — it does not reimplement
any of them. It is the integration seam between "Hope's brain runs in tmux"
and "a Unix daemon process the CLI can manage".
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time

# Wake-prefix gate. Every utterance must either start with one of these
# phrases or fall inside the open conversation window after Hope's last
# reply. Without this, ambient music / TV / nearby conversation pours
# straight into the brain and burns turns.
#
# Pattern allows: "Hope ...", "Hope, ...", "Hey Hope ...", "OK Hope ...",
# "Wake up Hope ...", "Hope wake up ...". The capture group is whatever
# follows the prefix — empty if the user just said the wake phrase alone.
_WAKE_PREFIX_RE = re.compile(
    # Optional prefix word, separated from "hope" by whitespace AND/OR
    # punctuation. Whisper often transcribes "Hey Hope" as "Hey, Hope."
    # — the old `\s+` separator silently failed on the comma and the
    # whole utterance fell through as no-prefix.
    r"^\s*(?:(?:hey|ok(?:ay)?|alright|yo|wake\s+up)[\s,.:;!?\-]+)?"
    r"hope"
    r"(?:[\s,.:;!?\-]+wake\s+up)?\b"
    r"[\s,.:;!?\-]*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _strip_wake_prefix(text: str) -> tuple[str, bool]:
    """Return (cleaned_text, had_prefix).

    If the text starts with a Hope wake phrase, return whatever followed.
    If the wake phrase was the entire utterance, ``cleaned_text`` is "".
    If there's no wake prefix at all, return the input unchanged with
    ``had_prefix=False``.
    """
    m = _WAKE_PREFIX_RE.match(text)
    if not m:
        return text, False
    return m.group(1).strip(), True
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any, Dict, List, Optional

from hope.audio import say, say_sync
from hope.core.config import DEFAULT_CONFIG_DIR, load_config
from hope.core.events import EventBus, EventType, get_event_bus
from hope.daemon.dashboard_bridge import DashboardBridge, DashboardBridgeConfig
from hope.learning.voice_learning_loop import (
    DEFAULT_ACKS,
    DEFAULT_WAKE_PHRASES,
    LoopConfig,
    VoiceLearningLoop,
    default_skill_optimize_hook,
    load_acks,
    load_wake_phrases,
)
from hope.traces.voice_trace import VoiceTraceStore, VoiceTurn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PID_FILE = DEFAULT_CONFIG_DIR / "daemon.pid"
CONTROL_SOCKET = DEFAULT_CONFIG_DIR / "daemon.sock"
LOG_FILE = DEFAULT_CONFIG_DIR / "daemon.log"

_CONTROL_BACKLOG = 8
_CONTROL_TIMEOUT_SEC = 2.0


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def read_pid(pid_file: Path = PID_FILE) -> Optional[int]:
    """Return the PID in *pid_file* iff the process is still alive.

    Stale PID files (no such process) are removed in-place and ``None``
    is returned — so callers can safely use this as a liveness probe.
    """
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        pid_file.unlink(missing_ok=True)
        return None
    return pid


def write_pid(pid: int, pid_file: Path = PID_FILE) -> None:
    """Write *pid* to *pid_file*, creating the parent dir 0o700 if needed."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(pid_file.parent, stat.S_IRWXU)
    except OSError:
        pass
    pid_file.write_text(str(pid))


def clear_pid(pid_file: Path = PID_FILE) -> None:
    """Remove *pid_file* if present. Idempotent."""
    pid_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Control socket protocol
# ---------------------------------------------------------------------------


def send_control(
    cmd: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    socket_path: Path = CONTROL_SOCKET,
    timeout: float = _CONTROL_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """Send a single JSON line to the daemon and read one JSON line back.

    Returns the decoded response dict. Raises :class:`FileNotFoundError` if
    the control socket doesn't exist (i.e. the daemon is not running).
    """
    if not Path(socket_path).exists():
        raise FileNotFoundError(
            f"daemon control socket missing: {socket_path}"
        )
    req = {"cmd": cmd}
    if payload:
        req["payload"] = payload

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        # Shutdown write so the daemon sees EOF on read.
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in chunk:
                break
    line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    if not line:
        return {"ok": False, "error": "empty response"}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"ok": False, "error": "malformed response", "raw": line}


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DaemonState:
    """Lightweight snapshot for ``hope status`` / the control protocol."""

    pid: int
    started_at: float
    orchestrator_started: bool
    hope_main_pane_id: Optional[str]
    specialist_count: int
    queued_spawn_count: int
    wake_monitor_available: bool
    wake_monitor_active: bool
    bus_socket: str
    control_socket: str
    listening_paused: bool = False
    # Live ephemeral specialists. Sent in state_snapshot so the dashboard
    # hydrates the panel correctly on (re)connect — without this, the
    # frontend depends on catching pane_spawned events live and any
    # specialist already running before WS connect is invisible.
    specialists: List[Dict[str, Any]] = field(default_factory=list)
    # Coarse brain phase derived from internal flags. Mirrors the
    # frontend store's BrainState union so a state_snapshot event can
    # hydrate the dashboard on connect.
    #   "sleeping" → no hope-main pane spawned yet
    #   "speaking" → TTS in flight
    #   "thinking" → brain dispatched, awaiting reply
    #   "idle"     → pane up, nothing in flight
    brain_state: str = "sleeping"
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "orchestrator_started": self.orchestrator_started,
            "hope_main_pane_id": self.hope_main_pane_id,
            "specialist_count": self.specialist_count,
            "specialists": self.specialists,
            "queued_spawn_count": self.queued_spawn_count,
            "wake_monitor_available": self.wake_monitor_available,
            "wake_monitor_active": self.wake_monitor_active,
            "bus_socket": self.bus_socket,
            "control_socket": self.control_socket,
            "listening_paused": self.listening_paused,
            "brain_state": self.brain_state,
            **self.extras,
        }


class HopeDaemon:
    """Long-lived owner of the orchestrator, wake monitor, and control socket.

    The daemon is instantiable without ``start()`` being called so tests can
    exercise it with mocked collaborators. Call :meth:`start` to actually
    bring the system up and :meth:`shutdown` to tear it down.
    """

    def __init__(
        self,
        *,
        bus: Optional[EventBus] = None,
        orchestrator: Any = None,
        wake_monitor: Any = None,
        enable_wake: bool = True,
        pid_file: Path = PID_FILE,
        control_socket: Path = CONTROL_SOCKET,
    ) -> None:
        self._bus = bus or get_event_bus()
        self._orchestrator = orchestrator
        self._wake_monitor = wake_monitor
        self._enable_wake = enable_wake
        self._pid_file = pid_file
        self._control_socket_path = control_socket
        self._started_at: float = 0.0
        self._stop_event = threading.Event()
        self._control_thread: Optional[threading.Thread] = None
        self._control_sock: Optional[socket.socket] = None
        self._shutdown_requested = threading.Event()
        self._speech_backend: Any = None
        self._speech_unsub: Any = None
        # Listening-paused flag — when set, the daemon drops every
        # SPEECH_TRANSCRIPT and WAKE_TRIGGER before any downstream
        # handler fires. Useful when a phone/TV is playing back media
        # Hope would otherwise interpret as user input. The mic stream
        # stays warm so resume is instant.
        self._listening_paused = threading.Event()
        self._wake_lock = threading.Lock()
        # Single-slot executor so concurrent transcripts queue rather
        # than racing into the same pane. Lazily built on first speech
        # event — the daemon can still boot in pure-sleep mode without
        # spinning the worker thread.
        self._brain_executor: Optional[ThreadPoolExecutor] = None
        # Cached BrainSession, rebuilt whenever the hope-main pane id
        # changes (i.e. after sleep → wake spawns a fresh pane).
        self._brain_session: Any = None
        self._brain_session_pane_id: Optional[str] = None
        # Echo guard: set while Hope is speaking via say_sync so incoming
        # transcripts (her own voice bouncing through the mic) get dropped
        # instead of triggering another brain call.
        self._speaking = threading.Event()
        # Separate from _wake_lock so the wake handler can call
        # _speak_blocking() from inside its own critical section without
        # self-deadlocking.
        self._speak_lock = threading.Lock()
        # Ring buffer of what Hope recently spoke (text, expires_at_sec).
        # When a transcript lands within the 15s window, we check for
        # token-overlap > 50% against each entry and drop it if so.
        # This catches the classic case where whisper finalizes +
        # transcribes Hope's own voice 3-5s AFTER TTS stops and
        # ``_speaking`` has already cleared.
        self._echo_log: list[tuple[str, float]] = []
        self._echo_lock = threading.Lock()
        self._echo_window_sec = 15.0
        # Brain-busy flag: set while a ``session.send()`` is in flight.
        # Prevents queueing a backlog of transcripts behind a Claude turn
        # that's still processing (which was causing 60s timeouts).
        self._brain_busy = threading.Event()
        # Wake-prefix gate. Updated in two places: (a) when a bare wake
        # phrase fires (opens the window so the user can immediately ask
        # without re-prefixing), and (b) when the brain finishes a reply
        # (keeps the window open for natural follow-ups). Outside the
        # window, transcripts without a "Hope ..." prefix are dropped.
        self._last_brain_turn_at: float = 0.0
        self._conversation_window_sec: float = 30.0
        # Rolling log of recent acks so the categorical picker avoids
        # repeating herself turn-to-turn.
        self._recent_acks: list[str] = []
        # Pending-turn queue — transcripts that arrived mid-brain and
        # were substantive (not cancels, not back-channels). Processed
        # FIFO in the turn-done finally block. Capped at 2.
        self._pending_transcripts: list[str] = []
        self._pending_lock = threading.Lock()
        # Voice-turn trace store + opt-in learning loop. Built lazily in
        # ``start()`` so tests can construct the daemon without writing to
        # ``~/.hope``.
        self._voice_store: Optional[VoiceTraceStore] = None
        self._voice_loop: Optional[VoiceLearningLoop] = None
        self._last_turn: Optional[VoiceTurn] = None  # for skill_tags fill-in
        # Dashboard WebSocket bridge — forwards EventBus events to the
        # Tauri frontend so the wake-triggered window can render live
        # transcript/pane state without re-opening the old HTTP API.
        self._dashboard_bridge: Optional[DashboardBridge] = None
        # Optional child process holding the Tauri dashboard (either the
        # production `.app` launched via ``open -g -a`` or ``npm run tauri
        # dev``). We track it so shutdown() can terminate a dev-mode child
        # gracefully; a production ``open`` child exits immediately after
        # handing the bundle to LaunchServices, so ``.poll()`` will return
        # a status by the time we try to tear it down — that's expected.
        self._dashboard_app_proc: Optional[subprocess.Popen] = None
        self._dashboard_app_log: Any = None  # open file handle for dev mode
        # RAG memory + scheduler are populated in ``start()``; declared
        # here so ``shutdown()`` can safely no-op when called on a daemon
        # that never started (tests do this).
        self._rag: Any = None
        self._scheduler: Any = None

    def _speak_blocking(self, text: str) -> None:
        """Speak *text* synchronously, serialized, with echo guard.

        Only one TTS invocation runs at a time. Two layers of echo
        defense:

        1. ``_speaking`` is True during playback + 2.5s tail — transcripts
           arriving in this window are dropped outright.
        2. ``_echo_log`` records the text for 15s — transcripts that
           token-overlap >50% with any recent spoken phrase are also
           dropped, which catches the case where whisper lags behind TTS
           and delivers Hope-hearing-herself after the guard clears.
        """
        if not text:
            return
        with self._speak_lock:
            # Record what we're about to say BEFORE speaking so the echo
            # rejection window opens immediately (whisper might transcribe
            # it while TTS is still playing).
            with self._echo_lock:
                self._echo_log.append((text.lower(), time.time() + self._echo_window_sec))
                # Trim expired entries.
                now = time.time()
                self._echo_log = [(t, exp) for t, exp in self._echo_log if exp > now]
            self._speaking.set()
            # Tell the dashboard orb (and any other subscribers) that
            # Hope is now actually speaking. Best-effort: a missing or
            # broken bus must never block TTS.
            try:
                self._bus.publish(
                    EventType.SPEAKING_STARTED,
                    {
                        "text": text,
                        "char_count": len(text),
                        # ~5 chars/sec at HOPE_VOICE_RATE=200 wpm; the
                        # frontend uses this to scale the synthesised
                        # envelope's duration.
                        "estimated_duration_sec": max(0.5, len(text) / 14.0),
                    },
                )
            except Exception:
                logger.debug("speaking_started publish failed", exc_info=True)
            try:
                say_sync(text)
            except Exception:
                logger.exception("say_sync failed")
            finally:
                try:
                    self._bus.publish(EventType.SPEAKING_ENDED, {"text": text})
                except Exception:
                    logger.debug("speaking_ended publish failed", exc_info=True)
                time.sleep(2.5)
                self._speaking.clear()

    @staticmethod
    def _tokenize_for_echo(text: str) -> list[str]:
        """Punctuation-stripped, lowercased token list."""
        cleaned = "".join(
            c if c.isalnum() or c.isspace() else " " for c in text.lower()
        )
        return [t for t in cleaned.split() if t]

    def _is_hope_own_echo(self, text: str) -> bool:
        """Return True if *text* is likely Hope's own voice re-captured.

        Token-overlap check with punctuation stripped from BOTH sides so
        ``"tools,"`` still matches ``"tools"``. Threshold 30% because
        whisper's word-error rate on Hope's TTS is high — an echo often
        shares only a third of its tokens verbatim with the original.
        Short transcripts (≤4 tokens) fall back to a stricter 50% bar
        to avoid false positives on brief user utterances.
        """
        incoming = self._tokenize_for_echo(text)
        if not incoming:
            return False
        now = time.time()
        with self._echo_lock:
            active_texts = [t for t, exp in self._echo_log if exp > now]
        threshold = 0.5 if len(incoming) <= 4 else 0.3
        for spoken in active_texts:
            spoken_tokens = set(self._tokenize_for_echo(spoken))
            if not spoken_tokens:
                continue
            overlap = sum(1 for t in incoming if t in spoken_tokens)
            if overlap / len(incoming) >= threshold:
                return True
        return False

    # ── properties ───────────────────────────────────────────────────

    @property
    def orchestrator(self) -> Any:
        return self._orchestrator

    @property
    def wake_monitor(self) -> Any:
        return self._wake_monitor

    @property
    def bus(self) -> EventBus:
        return self._bus

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> DaemonState:
        """Bring the daemon up in *sleeping* mode. Idempotent.

        Hope starts asleep: the daemon, wake monitor, and control socket
        come up, but the tmux brain pane is NOT spawned. The first
        ``WAKE_TRIGGER`` (from clap, phrase, or ``hope wake``) spawns the
        Claude Code pane.

        Order of operations:
          1. Lazily construct a :class:`TmuxOrchestrator` (but do NOT
             start it — the brain is dormant until wake).
          2. Subscribe the wake-trigger handler that spawns the brain on
             demand.
          3. Best-effort start a :class:`WakeMonitor`. Import failure →
             log a warning and continue.
          4. Write the daemon PID file and bind the control socket.
          5. Install SIGTERM/SIGINT handlers.
        """
        # Orchestrator — instantiate but leave dormant. Wake handler
        # calls .start() on first WAKE_TRIGGER.
        if self._orchestrator is None:
            from hope.agents.tmux_orchestrator import TmuxOrchestrator

            self._orchestrator = TmuxOrchestrator(bus=self._bus)

        # Wake-trigger handler (always subscribed so manual CLI wake works
        # even if the mic-based WakeMonitor is unavailable).
        self._bus.subscribe(EventType.WAKE_TRIGGER, self._on_wake)

        # Start the always-on speech backend FIRST so its MicCapture
        # can be shared with the WakeMonitor. Two separate MicCapture
        # instances competing for the same audio device produce
        # ``PaMacCore err=-50`` on macOS and no mic indicator ever
        # appears — whisper hears nothing, claps don't register.
        self._try_start_speech_backend()
        self._speech_unsub = self._bus.subscribe(
            EventType.SPEECH_TRANSCRIPT, self._on_speech_transcript
        )

        # Wake monitor (optional, best-effort) — shares MicCapture with
        # the speech backend when one is available.
        if self._enable_wake and self._wake_monitor is None:
            self._wake_monitor = self._try_start_wake_monitor()
        elif self._wake_monitor is not None:
            try:
                self._wake_monitor.start()
            except Exception:
                logger.exception("pre-injected wake_monitor.start() failed")

        # Dashboard bridge — best-effort, must never block daemon boot.
        self._try_start_dashboard_bridge()
        self._try_start_memory_watcher()
        self._try_start_proactive_recall()

        # PID file + control socket
        write_pid(os.getpid(), self._pid_file)
        self._start_control_socket()

        # Signal handlers — only when running in the main thread (tests
        # spin this up on worker threads where signal handlers aren't
        # legal; skip silently there).
        try:
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)
        except ValueError:
            logger.debug("skipping signal handler install — not on main thread")

        # Voice-turn trace store + learning loop (opt-in).
        self._init_voice_learning()

        # RAG memory backbone + scheduler — best-effort; neither is
        # allowed to tank the daemon if it fails to initialize.
        self._rag = None
        self._scheduler = None
        self._try_start_rag()
        self._try_start_scheduler()

        # Pre-warm the local Gemma model that powers Hope's vision
        # AND the personality-aware ack generator. Without this the
        # very first ack after wake pays the full ~7 s cold-load
        # penalty, blows the 3 s ack budget, and falls back to the
        # canned pool — which was the user-visible "acks feel odd"
        # symptom. Done in a worker so daemon boot stays snappy.
        threading.Thread(
            target=self._prewarm_gemma,
            name="hope-gemma-prewarm",
            daemon=True,
        ).start()

        self._started_at = time.time()
        logger.info(
            "Hope daemon started (sleeping) pid=%d wake_monitor=%s learning=%s",
            os.getpid(),
            self._wake_monitor is not None,
            self._voice_loop is not None,
        )
        return self.snapshot()

    def _try_start_rag(self) -> None:
        """Initialise the RAG memory singleton. Never raises."""
        try:
            from hope.memory import get_rag

            self._rag = get_rag()
            logger.info(
                "RAG backbone online (%d entries)", self._rag.count(),
            )
        except Exception:
            logger.exception("RAG backbone failed to initialize")
            self._rag = None

    def _prewarm_gemma(self) -> None:
        """Send a tiny generate request so Ollama loads gemma3:4b.

        gemma3:4b is dual-purpose for Hope: vision (eyes) AND
        personality-aware acks. Cold-load is ~7 s on M2; once
        resident in Ollama with ``keep_alive=-1`` it stays loaded
        across calls. Best-effort — never raises; if Ollama isn't
        running, the ack path will retry on each turn.
        """
        try:
            from hope.learning.acks_gemma import gen_ack
        except Exception:
            return
        try:
            # 30 s budget for the cold load — reasonable on M2 even
            # under heavy claude-CLI memory pressure.
            phrase = gen_ack("warmup ping", timeout=30.0)
            logger.info(
                "gemma pre-warm complete (sample ack=%r)", phrase,
            )
        except Exception:
            logger.debug("gemma pre-warm failed", exc_info=True)

    def _try_start_scheduler(self) -> None:
        """Start the TaskScheduler if ``[scheduler] enabled`` is set.

        Registers two built-in jobs the first time Hope boots:
          * ``hope_consolidate`` — nightly ``0 3 * * *`` memory consolidation.
          * ``connector_sync`` — hourly skeleton sync job (real connector
            wiring happens per-user via ``hope connect``; the job is a
            no-op until at least one connector is configured).
        """
        try:
            cfg = load_config()
        except Exception:
            logger.exception("scheduler: failed to load config; skipping")
            return
        sched_cfg = getattr(cfg, "scheduler", None)
        # Default to enabled — Hope's memory + connector story needs the
        # scheduler. Users can set ``[scheduler] enabled = false`` to opt out.
        enabled = (
            True if sched_cfg is None else getattr(sched_cfg, "enabled", True)
        )
        if not enabled:
            logger.info("scheduler disabled via config")
            return
        try:
            from hope.scheduler.scheduler import TaskScheduler
            from hope.scheduler.store import SchedulerStore

            db_path = (
                sched_cfg.db_path
                if sched_cfg and sched_cfg.db_path
                else str(DEFAULT_CONFIG_DIR / "scheduler.db")
            )
            poll_interval = sched_cfg.poll_interval if sched_cfg else 60
            store = SchedulerStore(db_path)
            scheduler = TaskScheduler(
                store, poll_interval=poll_interval, bus=self._bus,
            )
            self._register_default_jobs(scheduler)
            self._register_evolution_job(scheduler, cfg)
            scheduler.start()
            self._scheduler = scheduler
            logger.info(
                "scheduler started (db=%s, poll=%ds)",
                db_path,
                poll_interval,
            )
        except Exception:
            logger.exception("scheduler failed to start")
            self._scheduler = None

    @staticmethod
    def _register_default_jobs(scheduler: Any) -> None:
        """Register the nightly consolidate + hourly sync jobs if absent.

        Jobs are keyed by a stable metadata tag (``default_job_id``) so we
        don't duplicate them on daemon restart.
        """
        existing = {
            t.metadata.get("default_job_id")
            for t in scheduler.list_tasks()
            if t.metadata
        }
        if "hope_consolidate" not in existing:
            scheduler.create_task(
                prompt="hope_consolidate",
                schedule_type="cron",
                schedule_value="0 3 * * *",
                metadata={
                    "default_job_id": "hope_consolidate",
                    "handler": "scripts.hope_consolidate:main",
                    "description": "Nightly memory consolidation",
                },
            )
        if "connector_sync" not in existing:
            scheduler.create_task(
                prompt="connector_sync",
                schedule_type="interval",
                schedule_value=str(60 * 60),  # hourly
                metadata={
                    "default_job_id": "connector_sync",
                    "handler": "hope.connectors.sync_engine:run_all",
                    "description": (
                        "Hourly incremental sync across configured connectors"
                    ),
                },
            )

    @staticmethod
    def _register_evolution_job(scheduler: Any, cfg: Any) -> None:
        """Register the 4 AM evolution cycle iff ``[evolution] enabled``.

        Pulls the job spec from :mod:`hope.evolution.default_jobs` so the
        evolution module owns its own schedule. No-op when the user has
        not opted in — the evolution loop runs untrusted code in a sandbox
        container and we refuse to install it silently.
        """
        try:
            from hope.evolution.default_jobs import (
                DEFAULT_JOBS,
                should_install,
            )
        except Exception:
            logger.exception("evolution default_jobs import failed")
            return
        if not should_install(cfg):
            return
        existing = {
            t.metadata.get("default_job_id")
            for t in scheduler.list_tasks()
            if t.metadata
        }
        for job in DEFAULT_JOBS:
            job_id = job.get("id")
            if not job_id or job_id in existing:
                continue
            metadata = dict(job.get("metadata") or {})
            metadata["default_job_id"] = job_id
            scheduler.create_task(
                prompt=job["prompt"],
                schedule_type=job["schedule_type"],
                schedule_value=job["schedule_value"],
                agent=job.get("agent", "simple"),
                tools=job.get("tools", ""),
                metadata=metadata,
            )

    def _init_voice_learning(self) -> None:
        """Build the voice trace store + learning loop if enabled in config.

        Trace store is ALWAYS built (observability is free); the loop only
        runs when ``learning.enabled`` is true.
        """
        try:
            self._voice_store = VoiceTraceStore()
        except Exception:
            logger.exception("failed to open voice trace store; tracing disabled")
            self._voice_store = None
            return
        try:
            cfg = load_config()
            learning = getattr(cfg, "learning", None)
            enabled = bool(getattr(learning, "enabled", False))
        except Exception:
            enabled = False
        if not enabled or self._voice_store is None:
            return
        try:
            loop_cfg = LoopConfig(enabled=True)
            self._voice_loop = VoiceLearningLoop(
                self._voice_store,
                config=loop_cfg,
                skill_optimize_hook=default_skill_optimize_hook(self._voice_store),
            )
            logger.info("voice learning loop active (interval=%d turns)",
                        loop_cfg.turns_between_runs)
        except Exception:
            logger.exception("failed to start voice learning loop")
            self._voice_loop = None

    def pause_listening(self) -> bool:
        """Stop acting on mic input. Returns True if state changed.

        Fully shuts the STT pipeline down (mic capture + whisper
        + VAD segmenter) so no CPU is spent transcribing background
        audio while paused. Trade-off: voice wake ('Hope, wake up')
        cannot fire while paused — un-mute via the dashboard button
        or ``hope wake`` from the CLI. Cold-restart on resume is
        ~1 s. Publishes ``LISTENING_PAUSED`` on the event bus.
        """
        if self._listening_paused.is_set():
            return False
        self._listening_paused.set()
        # Shut STT down so whisper isn't burning CPU on TV/podcast
        # audio. Best-effort — if the backend is already stopped or
        # absent, the flag alone keeps the dispatch path quiet.
        if self._speech_backend is not None:
            try:
                self._speech_backend.stop()
                logger.info("listening paused — STT stopped")
            except Exception:
                logger.exception("speech_backend.stop() during pause failed")
        else:
            logger.info("listening paused")
        try:
            self._bus.publish(EventType.LISTENING_PAUSED, {"timestamp": time.time()})
        except Exception:
            logger.exception("failed to publish LISTENING_PAUSED")
        return True

    def resume_listening(self) -> bool:
        """Start acting on mic input again. Returns True if state changed.

        Re-spins the STT pipeline that ``pause_listening`` shut down.
        Idempotent — calling on an already-listening daemon is a
        no-op.
        """
        if not self._listening_paused.is_set():
            return False
        self._listening_paused.clear()
        # Bring STT back online. The backend's ``start()`` is
        # re-entrant per WhisperCppSTT's lifecycle test.
        if self._speech_backend is not None:
            try:
                self._speech_backend.start()
                logger.info("listening resumed — STT restarted")
            except Exception:
                logger.exception("speech_backend.start() on resume failed")
        else:
            logger.info("listening resumed")
        try:
            self._bus.publish(EventType.LISTENING_RESUMED, {"timestamp": time.time()})
        except Exception:
            logger.exception("failed to publish LISTENING_RESUMED")
        return True

    def toggle_listening(self) -> bool:
        """Flip the pause state. Returns the NEW paused state (True = paused)."""
        if self._listening_paused.is_set():
            self.resume_listening()
            return False
        self.pause_listening()
        return True

    @property
    def listening_paused(self) -> bool:
        return self._listening_paused.is_set()

    def sleep_brain(self) -> None:
        """Put the brain to sleep; keep the daemon + wake monitor alive.

        Kills hope-main and every live specialist pane but leaves the
        daemon process running so a subsequent ``WAKE_TRIGGER`` can spawn
        a fresh brain without re-running ``hope start``. Idempotent.

        Differs from :meth:`shutdown` in that the Python process
        continues and the wake monitor keeps listening.
        """
        orch = self._orchestrator
        if orch is None or not bool(getattr(orch, "_started", False)):
            try:
                say("Hope is already sleeping")
            except Exception:
                pass
            return
        # TmuxOrchestrator.shutdown() deliberately leaves the hope-main
        # pane alive (so you can tmux-attach after the daemon exits). For
        # brain-sleep we want it gone so the next wake starts a fresh
        # Claude Code CLI.
        main_id = getattr(orch, "hope_main_pane_id", None)
        if main_id:
            try:
                entry = orch.registry.get(main_id)
                if entry is not None:
                    orch._tmux(
                        ["tmux", "kill-pane", "-t", entry.tmux_target],
                        check=False,
                    )
            except Exception:
                logger.exception("kill hope-main pane failed during sleep_brain")
        try:
            orch.shutdown()
        except Exception:
            logger.exception("orchestrator.shutdown() failed during sleep_brain")
        # The cached BrainSession is bound to the now-dead pane id —
        # drop it so the next wake picks up the fresh pane.
        self._brain_session = None
        self._brain_session_pane_id = None
        try:
            say("Hope is sleeping")
        except Exception:
            pass
        logger.info("Hope brain sleeping; daemon still listening for wake")

    def shutdown(self) -> None:
        """Graceful shutdown. Safe to call repeatedly."""
        if self._shutdown_requested.is_set():
            return
        self._shutdown_requested.set()

        # Stop wake monitor first so no new WAKE_TRIGGERs land mid-teardown.
        if self._wake_monitor is not None:
            try:
                self._wake_monitor.stop()
            except Exception:
                logger.exception("wake_monitor.stop() failed")

        # Stop the always-on speech backend.
        if self._speech_backend is not None:
            try:
                self._speech_backend.stop()
            except Exception:
                logger.exception("speech_backend.stop() failed")
            self._speech_backend = None

        # Flush the brain worker. cancel_futures=True drops queued
        # transcripts — the daemon is going away, they're moot.
        if self._brain_executor is not None:
            try:
                self._brain_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                logger.exception("brain_executor.shutdown() failed")
            self._brain_executor = None
        self._brain_session = None
        self._brain_session_pane_id = None

        # Scheduler — stop the poll loop before orchestrator teardown so
        # no scheduler-triggered agent spawns arrive after the bus closes.
        if self._scheduler is not None:
            try:
                self._scheduler.stop()
            except Exception:
                logger.exception("scheduler.stop() failed")
            self._scheduler = None

        # Orchestrator — kills specialists, closes the bus socket.
        if self._orchestrator is not None:
            try:
                self._orchestrator.shutdown()
            except Exception:
                logger.exception("orchestrator.shutdown() failed")

        # Dashboard bridge.
        if self._dashboard_bridge is not None:
            try:
                self._dashboard_bridge.stop()
            except Exception:
                logger.exception("dashboard bridge stop failed")
            self._dashboard_bridge = None

        # Dashboard child process — SIGTERM with a 5s grace window, then
        # SIGKILL. If the user already closed the window, ``.poll()``
        # will return a non-None status and we skip teardown entirely.
        if self._dashboard_app_proc is not None:
            proc = self._dashboard_app_proc
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        logger.warning(
                            "dashboard child pid=%s did not exit in 5s — "
                            "sending SIGKILL",
                            proc.pid,
                        )
                        proc.kill()
                        try:
                            proc.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            logger.exception(
                                "dashboard child pid=%s ignored SIGKILL",
                                proc.pid,
                            )
            except Exception:
                logger.exception("dashboard child shutdown failed")
            self._dashboard_app_proc = None
        if self._dashboard_app_log is not None:
            try:
                self._dashboard_app_log.close()
            except Exception:
                pass
            self._dashboard_app_log = None

        # Control socket shutdown.
        self._stop_event.set()
        if self._control_sock is not None:
            try:
                self._control_sock.close()
            except OSError:
                pass
            self._control_sock = None
        if self._control_thread is not None and self._control_thread.is_alive():
            self._control_thread.join(timeout=2.0)
        try:
            if self._control_socket_path.exists():
                self._control_socket_path.unlink()
        except OSError:
            pass

        # Voice trace store — best-effort close. Loop is thread-only, no
        # teardown needed beyond dropping the reference.
        if self._voice_store is not None:
            try:
                self._voice_store.close()
            except Exception:
                logger.exception("voice_store.close() failed")
            self._voice_store = None
        self._voice_loop = None

        clear_pid(self._pid_file)
        try:
            say("Hope is sleeping")
        except Exception:
            pass
        logger.info("Hope daemon shutdown complete")

    def run_forever(self) -> None:
        """Block until shutdown is requested (signal or control socket)."""
        try:
            while not self._shutdown_requested.is_set():
                self._shutdown_requested.wait(timeout=1.0)
        finally:
            self.shutdown()

    # ── wake handling ────────────────────────────────────────────────

    def _on_wake(self, event: Any) -> None:
        """Handle a :attr:`EventType.WAKE_TRIGGER` event.

        The wake lock prevents a classic check-then-act race: if two
        triggers fire in quick succession (e.g. a clap plus a phrase
        match both within the wake monitor's refractory window, or two
        parallel event dispatches), only one of them spawns the brain
        and speaks the greeting.
        """
        payload = getattr(event, "data", {}) or {}
        source = payload.get("source", "unknown")
        logger.info("WAKE_TRIGGER received source=%s", source)

        # Listening paused? A wake from VOICE or CLAP is deliberate
        # user intent ("Hope, wake up" / two claps) — auto-resume
        # listening AND fall through. Mirrors what
        # ``_on_speech_transcript`` already does for wake-prefix
        # transcripts during pause. Only an "unknown" source (or any
        # source we explicitly want to gate) is ignored.
        was_paused = self._listening_paused.is_set()
        if was_paused:
            if source in ("voice", "clap", "manual"):
                if source != "manual":
                    self.resume_listening()
                    logger.info(
                        "wake while paused — auto-resuming "
                        "(source=%s, deliberate user intent)",
                        source,
                    )
                # When the user wakes Hope after a pause, any queued
                # transcripts are almost certainly stale background
                # noise (TV, podcast, the conversation we were trying
                # to filter out). Drop them so the brain doesn't
                # process minutes-old garbage right after the wake.
                with self._pending_lock:
                    dropped = len(self._pending_transcripts)
                    self._pending_transcripts.clear()
                if dropped:
                    logger.info(
                        "wake-from-pause: cleared %d stale queued transcript(s)",
                        dropped,
                    )
                # Fall through into the wake flow.
            else:
                logger.info(
                    "wake ignored — listening paused (source=%s)", source,
                )
                return

        orch = self._orchestrator
        if orch is None:
            self._speak_blocking("Hope is not ready")
            return

        # Serialize wake handling so two triggers can't both call
        # orchestrator.start() + say() concurrently.
        with self._wake_lock:
            if bool(getattr(orch, "_started", False)):
                if was_paused:
                    # The user wasn't asleep — the daemon was just
                    # muted. Confirm we're back so they don't talk
                    # into a black hole. Short greeting only; the
                    # full "Hi sir, I am online..." is reserved for
                    # cold wakes.
                    self._last_brain_turn_at = time.monotonic()
                    self._speak_blocking("I'm listening.")
                    logger.info(
                        "wake while already-awake — un-paused with "
                        "greeting (was muted)",
                    )
                    return
                # Truly already awake and not paused — silent. The
                # "I'm already awake" chatter was annoying and caused
                # more echo feedback when mic re-captured it.
                logger.info("wake ignored — already awake")
                return
            try:
                orch.start()
            except Exception:
                logger.exception("orchestrator.start() on wake failed")
                self._speak_blocking("Hope failed to wake up")
                return
            # Open the conversation window now — the user's next utterance
            # shouldn't need a wake prefix. Critical when whisper mishears
            # "Hope" as "hook"/"hoe" — fuzzy PhraseMatcher fires the wake
            # but the strict regex would otherwise reject the follow-up.
            self._last_brain_turn_at = time.monotonic()
            self._speak_blocking("Hi sir, I am online. What can I do for you?")
            logger.info(
                "Hope is awake — hope-main pane=%s",
                getattr(orch, "hope_main_pane_id", None),
            )
            # Brief the brain on any work that was in flight before the
            # restart — abandoned/in_progress tasks the previous daemon
            # left behind. Done in a worker thread so the user-facing
            # wake greeting isn't gated on the brief landing.
            threading.Thread(
                target=self._brief_brain_on_unfinished,
                name="hope-wake-brief",
                daemon=True,
            ).start()

    def _brief_brain_on_unfinished(self) -> None:
        """Send the brain a wake-up briefing of unfinished tasks.

        Reads :class:`TmuxOrchestrator.journal_query` for in_progress
        and abandoned tasks, formats them into a single-line bus
        message, and delivers it to the hope-main pane. Abandoned
        tasks are the ones a previous daemon owned but never closed
        out — Hope sees them and decides whether to resume.

        Best-effort — never raises. A missing journal or empty result
        means we just skip the brief.
        """
        orch = self._orchestrator
        if orch is None:
            return
        main_id = getattr(orch, "hope_main_pane_id", None)
        if not main_id:
            return
        try:
            tasks = orch.journal_query(
                statuses=["in_progress", "abandoned"], limit=12,
            )
        except Exception:
            logger.exception("wake-brief: journal_query failed")
            return
        if not tasks:
            logger.info("wake-brief: no unfinished tasks in the journal")
            return
        lines = []
        for t in tasks:
            ago = max(0.0, time.time() - float(t.get("last_update", 0)))
            ago_str = (
                f"{int(ago)}s" if ago < 90 else
                f"{int(ago / 60)}m" if ago < 90 * 60 else
                f"{int(ago / 3600)}h"
            )
            goal = (t.get("goal") or "")[:120]
            lines.append(
                f"{t.get('status')} {t.get('role')}({t.get('task_id')}) "
                f"goal={goal!r} {ago_str}-ago"
            )
        body = (
            "Wake-up briefing — tasks left over from before the restart. "
            f"{len(tasks)} unfinished: " + " | ".join(lines)
            + ". Use hope-journal to inspect or resume."
        )
        try:
            orch.send_message(
                from_pane="hope-orchestrator",
                to=main_id,
                topic=f"to:{main_id}",
                body=body,
            )
            logger.info("wake-brief: delivered %d tasks to brain", len(tasks))
        except Exception:
            logger.exception("wake-brief: send_message failed")

    # ── speech backend + brain bridge ─────────────────────────────────

    def _try_start_dashboard_bridge(self) -> None:
        """Start the WebSocket bridge if ``dashboard.enabled`` is True.

        Never raises — the bridge is a nice-to-have for the UI and must
        not block the core wake/speech loop.
        """
        try:
            cfg = load_config()
            dash_cfg = getattr(cfg, "dashboard", None)
            if dash_cfg is None or not getattr(dash_cfg, "enabled", False):
                logger.info("dashboard.enabled is False — skipping bridge")
                return
            bridge = DashboardBridge(
                DashboardBridgeConfig(
                    enabled=True,
                    host=getattr(dash_cfg, "host", "127.0.0.1"),
                    port=int(getattr(dash_cfg, "port", 8765)),
                ),
                bus=self._bus,
                # Lambda so the bridge calls back at every accept and gets
                # *current* state, not whatever was true at startup.
                state_provider=lambda: self.snapshot().to_dict(),
            )
            bridge.start()
            self._dashboard_bridge = bridge
            logger.info(
                "dashboard bridge up on ws://%s:%s",
                getattr(dash_cfg, "host", "127.0.0.1"),
                bridge.port,
            )
        except Exception:
            logger.exception("dashboard bridge failed to start; continuing without it")
            return
        # Bridge is up — try to bring the Tauri window online (hidden).
        # This runs only when the bridge actually started: no point firing
        # up the UI if the socket it talks to isn't listening.
        if self._dashboard_bridge is not None and getattr(
            dash_cfg, "autolaunch", True,
        ):
            self._launch_dashboard_app(dash_cfg)

    def _launch_dashboard_app(self, dash_cfg: Any) -> None:
        """Best-effort bring up the Tauri dashboard window (hidden).

        Preference order:

        1. Explicit ``dash_cfg.app_bundle_path`` override.
        2. ``/Applications/Hope.app``.
        3. ``~/Applications/Hope.app``.
        4. If ``dash_cfg.dev_fallback`` is True: ``npm run tauri dev`` in
           the repo's ``frontend/`` dir, with stdout/stderr teed to
           ``~/.hope/dashboard.log``.

        Never raises — a missing ``.app``, missing ``npm``, or a spawn
        failure is logged and the daemon carries on. The window itself
        starts hidden (handled by the Tauri app); this just makes sure
        it's *running* so the WS bridge can later tell it to show on
        ``wake_trigger``.
        """
        # (1)/(2)/(3): try an installed `.app` bundle first.
        candidates: list[Path] = []
        override = getattr(dash_cfg, "app_bundle_path", None)
        if override:
            candidates.append(Path(override).expanduser())
        candidates.append(Path("/Applications/Hope.app"))
        candidates.append(Path.home() / "Applications" / "Hope.app")
        for bundle in candidates:
            try:
                if bundle.exists():
                    proc = subprocess.Popen(
                        ["open", "-g", "-a", str(bundle)],
                    )
                    self._dashboard_app_proc = proc
                    logger.info(
                        "dashboard app launched (hidden) from %s", bundle,
                    )
                    return
            except Exception:
                logger.exception(
                    "dashboard app launch via `open` failed for %s", bundle,
                )

        # (4): dev fallback — only if explicitly allowed.
        if not getattr(dash_cfg, "dev_fallback", True):
            logger.warning(
                "dashboard autolaunch: no .app bundle found and "
                "dev_fallback=False — skipping. Run "
                "`./scripts/build_dashboard.sh` to install the .app.",
            )
            return
        try:
            npm = shutil.which("npm")
            if npm is None:
                logger.warning(
                    "dashboard autolaunch: `npm` not found on PATH — "
                    "skipping dev fallback",
                )
                return
            # Locate frontend/ relative to the repo root. This file lives
            # at src/hope/daemon/core.py, so the repo root is three
            # parents up from __file__.
            repo_root = Path(__file__).resolve().parents[3]
            frontend_dir = repo_root / "frontend"
            if not (frontend_dir / "package.json").exists():
                logger.warning(
                    "dashboard autolaunch: frontend/package.json not "
                    "found at %s — skipping dev fallback",
                    frontend_dir,
                )
                return
            log_path = DEFAULT_CONFIG_DIR / "dashboard.log"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_fh = open(log_path, "ab", buffering=0)
            except OSError:
                logger.exception(
                    "dashboard autolaunch: could not open %s for writing",
                    log_path,
                )
                log_fh = subprocess.DEVNULL  # type: ignore[assignment]
            # Tauri dev needs cargo on PATH; the daemon's inherited env
            # often doesn't have ~/.cargo/bin (rustup default install
            # location), so the spawn fails silently with "cargo metadata
            # not found". Prepend known cargo locations to PATH for the
            # child only.
            child_env = dict(os.environ)
            extra_paths = [
                str(Path.home() / ".cargo" / "bin"),
                "/opt/homebrew/bin",
                "/usr/local/bin",
            ]
            existing_path = child_env.get("PATH", "")
            child_env["PATH"] = ":".join(
                p for p in extra_paths + [existing_path] if p
            )
            proc = subprocess.Popen(
                [npm, "run", "tauri", "dev"],
                cwd=str(frontend_dir),
                stdout=log_fh,
                stderr=log_fh,
                stdin=subprocess.DEVNULL,
                env=child_env,
                # start_new_session detaches from the daemon's PGID so a
                # Ctrl-C in the daemon's foreground shell doesn't also
                # blow up the Tauri dev window. We still own the PID and
                # will SIGTERM it in shutdown().
                start_new_session=True,
            )
            self._dashboard_app_proc = proc
            if hasattr(log_fh, "close"):
                self._dashboard_app_log = log_fh
            logger.info(
                "dashboard dev server spawned (pid=%s) in %s → %s",
                proc.pid, frontend_dir, log_path,
            )
        except Exception:
            logger.exception(
                "dashboard autolaunch: dev fallback failed; continuing",
            )

    def _try_start_speech_backend(self) -> None:
        """Start the always-on speech backend if ``speech.always_on`` is set.

        Never raises — a mic permission error or a missing dep must not
        kill the daemon. If the backend cannot start, voice wake just
        won't work; clap wake and manual ``hope wake`` still function.
        """
        try:
            cfg = load_config()
            speech_cfg = getattr(cfg, "speech", None)
            if speech_cfg is None or not getattr(speech_cfg, "always_on", False):
                logger.info(
                    "speech.always_on is False — skipping always-on whisper"
                )
                return
        except Exception:
            logger.exception("failed to read speech config")
            return
        try:
            from hope.speech.whisper_cpp import WhisperCppSTT

            backend = WhisperCppSTT(
                model_name=getattr(
                    speech_cfg, "model_name", "distil-whisper/distil-large-v3.5"
                ),
                vad_threshold=getattr(speech_cfg, "vad_threshold", 0.5),
                min_silence_ms=getattr(speech_cfg, "min_silence_ms", 500),
                input_device=getattr(speech_cfg, "input_device", None),
            )
            backend.start()
            self._speech_backend = backend
            logger.info("always-on speech backend started")
        except Exception:
            logger.exception(
                "always-on speech backend failed to start; voice wake disabled"
            )

    def _on_speech_transcript(self, event: Any) -> None:
        """Forward spoken transcripts into the hope-main pane and speak the reply.

        Pre-filters (brain awake? non-empty? not a wake phrase?) run on
        the event dispatch thread so cheap rejections stay cheap. The
        actual send → poll → extract → TTS cycle is dispatched to a
        single-slot :class:`ThreadPoolExecutor` — Claude takes 2-30s per
        turn and we MUST NOT block ``EventBus`` dispatch for that long.
        A single-slot executor also serializes overlapping transcripts so
        two rapid utterances don't race into the same pane.
        """
        import sys as _sys
        payload = getattr(event, "data", {}) or {}
        text = (payload.get("text") or "").strip()
        logger.info("SPEECH_TRANSCRIPT received text=%r", text[:120])
        _sys.stderr.write(f"[HEARD] {text!r}\n")
        _sys.stderr.flush()

        # Listening paused? Drop the transcript — UNLESS it carries the
        # wake prefix, in which case interpret it as "I want you back",
        # auto-resume listening, and fall through to the normal handler
        # (the wake-prefix gate below will re-detect the prefix and open
        # the conversation window cleanly).
        if self._listening_paused.is_set():
            _, has_wake_prefix = _strip_wake_prefix(text)
            if has_wake_prefix:
                self.resume_listening()
                _sys.stderr.write(f"[WAKE-UNMUTE] {text!r}\n")
                _sys.stderr.flush()
                # fall through — don't return
            else:
                _sys.stderr.write(f"[PAUSED-DROP] {text!r}\n")
                _sys.stderr.flush()
                return

        # Echo guard: if Hope is currently speaking, this transcript is
        # almost certainly her own voice bouncing back through the mic.
        if self._speaking.is_set():
            logger.info(
                "speech bridge: ignored (Hope is speaking) text=%r", text[:60]
            )
            _sys.stderr.write(f"[ECHO-DROP speaking] {text!r}\n")
            _sys.stderr.flush()
            return
        # Second layer: token-overlap check against recently spoken phrases.
        # Whisper often delivers Hope's own voice 3-5s after TTS ends, by
        # which time ``_speaking`` has cleared — this catches those.
        if self._is_hope_own_echo(text):
            logger.info("speech bridge: ignored (echo match) text=%r", text[:60])
            _sys.stderr.write(f"[ECHO-DROP match] {text!r}\n")
            _sys.stderr.flush()
            return
        # Third layer — mid-brain speech triage. Classify the incoming
        # transcript into cancel / backchannel / new_turn:
        #   - cancel   → barge in, kill the in-flight brain turn
        #   - backchannel → user said "yes"/"mm-hmm"/"go on"; log, emit
        #     event for dashboard, don't start a new turn
        #   - new_turn → queue for after the current turn finishes
        if self._brain_busy.is_set():
            try:
                from hope.learning.turn_classifier import classify_midturn
                action = classify_midturn(text)
            except Exception:
                action = "new_turn"
            if action == "cancel":
                logger.info("speech bridge: CANCEL mid-turn: %r", text[:60])
                _sys.stderr.write(f"[CANCEL] {text!r}\n")
                _sys.stderr.flush()
                self._handle_cancel_midturn()
                return
            if action == "backchannel":
                logger.info("speech bridge: back-channel: %r", text[:60])
                _sys.stderr.write(f"[BACK-CHANNEL] {text!r}\n")
                _sys.stderr.flush()
                try:
                    self._bus.publish(
                        EventType.BACK_CHANNEL_HEARD
                        if hasattr(EventType, "BACK_CHANNEL_HEARD")
                        else "back_channel_heard",
                        {"text": text, "timestamp": time.time()},
                    )
                except Exception:
                    pass
                return
            # new_turn — queue it. Hard cap at 2 pending so a monologue
            # doesn't build a huge backlog.
            with self._pending_lock:
                if len(self._pending_transcripts) >= 2:
                    logger.info("speech bridge: queue full, dropping: %r", text[:60])
                    _sys.stderr.write(f"[QUEUE-FULL] {text!r}\n")
                    _sys.stderr.flush()
                    return
                self._pending_transcripts.append(text)
                _sys.stderr.write(f"[QUEUED] {text!r}\n")
                _sys.stderr.flush()
            return

        orch = self._orchestrator
        if orch is None or not bool(getattr(orch, "_started", False)):
            logger.debug("speech bridge: brain not awake — skipping")
            return
        if not text:
            return
        # cortexOS parity: drop transcripts shorter than 3 chars — these
        # are almost always whisper hallucinations on breath/keyboard noise.
        if len(text) < 3:
            logger.info("speech bridge: transcript too short — skipping: %r", text)
            return
        # Wake-prefix gate. Three outcomes:
        #   1. Has prefix + content → strip prefix, forward content.
        #   2. Has prefix + nothing else → swallow, open conversation window.
        #   3. No prefix → forward only if we're still inside the open
        #      conversation window from Hope's last reply; otherwise drop.
        cleaned, had_prefix = _strip_wake_prefix(text)
        now_mono = time.monotonic()
        in_window = (
            self._last_brain_turn_at > 0
            and (now_mono - self._last_brain_turn_at)
            <= self._conversation_window_sec
        )
        if had_prefix:
            if not cleaned:
                # Bare wake phrase like "Hope" or "Hey Hope" — treat as a
                # system-level wake (no brain turn) and open the window so
                # the next utterance doesn't need re-prefixing.
                self._last_brain_turn_at = now_mono
                logger.info("speech bridge: bare wake phrase: %r", text[:60])
                _sys.stderr.write(f"[WAKE-BARE] {text!r}\n")
                _sys.stderr.flush()
                return
            text = cleaned  # strip "Hope, " before forwarding
        elif not in_window:
            logger.info(
                "speech bridge: no wake prefix, outside conversation window — dropped: %r",
                text[:60],
            )
            _sys.stderr.write(f"[NO-WAKE-PREFIX] {text!r}\n")
            _sys.stderr.flush()
            return
        # else: no prefix but inside window → forward as-is (follow-up turn)
        main_id = getattr(orch, "hope_main_pane_id", None)
        if not main_id or orch.registry.get(main_id) is None:
            logger.warning(
                "speech bridge: no hope-main pane entry; main_id=%r", main_id
            )
            return

        executor = self._ensure_brain_executor()
        if executor is None:  # pragma: no cover — shutdown race
            return
        try:
            executor.submit(self._process_transcript, text, main_id)
        except RuntimeError:
            # Executor was shut down mid-dispatch — drop the transcript
            # rather than crashing the event bus.
            logger.debug("speech bridge: executor shut down — dropping transcript")

    def _ensure_brain_executor(self) -> Optional[ThreadPoolExecutor]:
        """Build the single-slot brain worker on demand."""
        if self._brain_executor is None:
            self._brain_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="hope-brain"
            )
        return self._brain_executor

    def _get_brain_session(self, main_id: str) -> Any:
        """Return a cached :class:`BrainSession` for *main_id*, rebuilding
        it whenever the pane id changes (sleep → wake cycles).
        """
        if (
            self._brain_session is not None
            and self._brain_session_pane_id == main_id
        ):
            return self._brain_session
        from hope.voice import BrainSession

        self._brain_session = BrainSession(self._orchestrator, main_id)
        self._brain_session_pane_id = main_id
        return self._brain_session

    # Ack phrases are resolved dynamically via :func:`load_acks` so the
    # voice-learning loop can prune / extend them in place at
    # ``~/.hope/learning/acks.json`` without a daemon restart. The tuple
    # below is the baked-in fallback used when the overlay file is absent.
    _ACK_PHRASES = DEFAULT_ACKS

    @staticmethod
    def _ack_phrases() -> tuple[str, ...]:
        try:
            phrases = load_acks()
        except Exception:  # pragma: no cover — file IO edge case
            return DEFAULT_ACKS
        return tuple(phrases) if phrases else DEFAULT_ACKS

    @staticmethod
    def _truncate_for_speech(reply: str, max_chars: int = 280) -> str:
        """Shorten Claude's reply before TTS.

        Long technical replies (code, lists, multi-paragraph explanations)
        take 30-60s to speak via ``say``, which vastly expands the echo
        window. We speak the first sentence (or up to *max_chars*), which
        is usually Claude's summary. Full reply is still in the tmux
        pane and the log for anyone who wants it.
        """
        reply = (reply or "").strip()
        if not reply:
            return ""
        import re as _re

        # Step 1 — strip Claude Code's own activity-marker lines that
        # sometimes leak into the extracted reply. They sit on their own
        # line(s) BEFORE the agent's actual sentence, so we nuke the
        # whole line. Examples:
        #   "Read 1 file (ctrl+o to expand)"
        #   "Called claude-flow 2 times (ctrl+o to expand)"
        #   "Searched for 1 pattern (ctrl+o to expand)"
        #   "Listed 2 directories"
        #   "Running… (33s)"
        marker_line = _re.compile(
            r"^\s*(?:Read|Reading|Listed|Listing|Searched|Searching|"
            r"Wrote|Writing|Called|Calling|Running|Ran)\b[^\n]*$",
            _re.IGNORECASE | _re.MULTILINE,
        )
        reply = marker_line.sub("", reply)
        # Also strip classic `hope-app` / `hope-dismiss` output lines that
        # describe a state change rather than speak to the user:
        #   "Calculator (launched)"
        #   "JMP (focused)"
        #   "Spotify (quit)"
        #   "Calculator (already running, focused)"
        #   "Calculator (launched, resolved from 'Calc')"
        tool_line = _re.compile(
            r"^\s*\S[^\n]*\((?:launched|quit|focused|already running[^)]*|"
            r"launched, resolved[^)]*|clicked [^)]*|closed[^)]*)\)\s*$",
            _re.IGNORECASE | _re.MULTILINE,
        )
        reply = tool_line.sub("", reply)
        # Collapse the now-empty lines back down.
        reply = _re.sub(r"\n{3,}", "\n\n", reply).strip()

        # Step 2 — pick the paragraph that holds the agent's actual reply.
        # Claude Code renders the final agent statement LAST; earlier
        # paragraphs are usually tool output, shell fragments, or thinking.
        # We scan paragraphs bottom-up and pick the last one that looks
        # like natural language (starts with a letter, has a sentence
        # terminator, not obviously shell/path/tool-dump noise).
        paragraphs = [p.strip() for p in reply.split("\n\n") if p.strip()]

        def _looks_like_reply(p: str) -> bool:
            if len(p) < 3:
                return False
            first = p.lstrip()[:1]
            if not first.isalpha():
                return False
            # Shell-ish signals: lots of `|`, `&&`, `;`, backticks, pipes,
            # or lines that are mostly a path/pid dump.
            if _re.search(r"&&|\|\||;\s*\w|`[^`]+`", p):
                return False
            if _re.search(r"^\d+\s+/\S+", p):  # "35115 /System/..."
                return False
            # Must contain a terminator — real replies end in . ! or ?
            return bool(_re.search(r"[.!?]", p))

        head = ""
        for p in reversed(paragraphs):
            if _looks_like_reply(p):
                head = p
                break
        if not head:
            # Last-resort: try the last paragraph verbatim, then the first.
            head = paragraphs[-1] if paragraphs else reply
        # First sentence of the chosen paragraph.
        m = _re.search(r"[.!?](?:\s|$)", head)
        if m:
            head = head[: m.end()].strip()

        # Step 3 — inline pruning of things macOS `say` reads as gibberish:
        #   file paths, very-long digit runs (timestamps), inline `code`.
        head = _re.sub(r"(?:~|/)[\w./\-]{2,}", "", head)
        head = _re.sub(r"\b\d[\w_\-]*\d{3,}[\w_\-]*\b", "", head)
        head = _re.sub(r"`[^`]+`", "", head)
        head = _re.sub(r"\(ctrl\+o[^\)]*\)", "", head)
        # Claude Code TUI artifact: "(... 544 more lines, ctrl+o to expand)"
        # or just "with 544 more lines" leaking into the spoken text.
        # Strip both the parenthesised and bare forms.
        head = _re.sub(
            r"\(?\s*(?:\.\.\.\s*)?(?:with\s+)?\d+\s+more\s+lines?[^)]*\)?",
            "",
            head,
            flags=_re.IGNORECASE,
        )

        # Collapse whitespace (including embedded newlines) left behind
        # so `say` doesn't pause mid-sentence.
        head = _re.sub(r"\s+", " ", head).strip(" ,.—-–")
        return head[:max_chars].strip()

    def _process_transcript(self, text: str, main_id: str) -> None:
        """Worker-thread body: ack IN PARALLEL with brain processing.

        Launches the ack on a sibling thread and IMMEDIATELY calls
        ``session.send()``. By the time the ack finishes speaking, Claude
        is already producing tokens, so perceived latency is
        ``max(ack_duration, brain_duration)`` rather than their sum.
        """
        import random as _random
        import sys as _sys
        self._brain_busy.set()
        # Publish INFERENCE_START so the dashboard orb can flip to
        # `thinking`. The Tauri side maps this event → brainState.
        # The actual brain is the tmux/claude CLI subprocess (it doesn't
        # publish the event itself), so the daemon does it on its behalf.
        try:
            self._bus.publish(
                EventType.INFERENCE_START,
                {"engine": "claude-cli", "input": text[:200]},
            )
        except Exception:
            logger.debug("INFERENCE_START publish failed", exc_info=True)
        turn = VoiceTurn(user_transcript=text, brain_request=text)
        reply: str = ""
        try:
            session = self._get_brain_session(main_id)
            logger.info(
                "brain worker: sending to pane=%s text=%r",
                main_id,
                text[:80],
            )
            _sys.stderr.write(f"[→ BRAIN] {text!r}\n")
            _sys.stderr.flush()
            # Fire-and-forget ack — parallel with brain.send(). _speak_lock
            # ensures it can't overlap with the reply's TTS later.
            # --- Ack policy (three-tier) ------------------------------
            #  1. SKIP ack entirely on short follow-ups inside the
            #     conversation window. Mid-dialogue an ack feels like
            #     the assistant interrupting herself.
            #  2. Otherwise delay the ack by ``_ACK_DELAY_SEC``; if the
            #     brain replies first, cancel it — no ack for fast turns.
            #  3. When the ack does fire, try to generate one with the
            #     small local model (contextual, in Hope's voice). Fall
            #     back to the categorical pool on any failure.
            # Tighter than the original 0.6 s — short directives (<3s
            # of speech) need an audible "I heard you" within a beat
            # of the user's voice ending or it feels like Hope's not
            # listening. 0.2 s is roughly the floor where the ack
            # doesn't step on top of the user's last word and is
            # short enough to feel responsive.
            _ACK_DELAY_SEC = 0.2
            ack_cancel = threading.Event()
            skip_ack = False
            try:
                from hope.learning.acks_bank import categorize as _cat
                if _cat(text) == "followup" and self._last_brain_turn_at > 0 and \
                   (time.monotonic() - self._last_brain_turn_at) \
                   <= self._conversation_window_sec:
                    skip_ack = True
            except Exception:
                logger.debug("ack categorize failed", exc_info=True)

            ack_thread = None
            if not skip_ack:
                def _ack_worker():
                    # Wait before speaking — if brain is fast, this window
                    # expires with cancel set and we say nothing.
                    if ack_cancel.wait(timeout=_ACK_DELAY_SEC):
                        return
                    # Try the small-model ack generator. Tight timeout —
                    # we want the ack in flight well before the brain
                    # typical 2–5 s reply.
                    phrase = None
                    try:
                        from hope.learning.acks_gemma import gen_ack
                        # Gemma3:4b warm-eval is ~3s on M2; brain
                        # typically takes 5+s. With keep_alive=-1
                        # in acks_gemma, the model stays loaded so
                        # this is the eval-only budget, not load+eval.
                        phrase = gen_ack(text, timeout=3.0)
                    except Exception:
                        logger.debug("acks_gemma failed", exc_info=True)
                    if ack_cancel.is_set():
                        return
                    if not phrase:
                        try:
                            from hope.learning.acks_bank import pick_ack
                            phrase = pick_ack(text, recent=self._recent_acks[-3:])
                        except Exception:
                            phrase = _random.choice(self._ack_phrases())
                    if ack_cancel.is_set():
                        return
                    self._recent_acks.append(phrase)
                    if len(self._recent_acks) > 8:
                        self._recent_acks = self._recent_acks[-8:]
                    turn.ack_spoken = phrase
                    self._speak_blocking(phrase)
                ack_thread = threading.Thread(
                    target=_ack_worker, name="hope-ack", daemon=True,
                )
                ack_thread.start()

            reply = session.send(text) or ""
            # Brain returned — tell the ack worker to abort if it hasn't
            # spoken yet. If it *has* spoken, _speak_lock will serialize
            # the reply TTS cleanly after.
            ack_cancel.set()
            turn.brain_reply_full = reply
            # Make sure the ack finished before we try to speak the reply,
            # otherwise _speak_lock serializes and we get an awkward gap.
            if ack_thread is not None:
                ack_thread.join(timeout=10.0)
            if not reply:
                logger.info("brain worker: empty reply — not speaking")
                _sys.stderr.write("[BRAIN ←] (empty reply)\n")
                _sys.stderr.flush()
                return
            logger.info("brain worker: reply=%r", reply[:120])
            _sys.stderr.write(f"[BRAIN ←] {reply!r}\n")
            _sys.stderr.flush()
            # Speak only the first sentence — full reply is still in
            # the pane. Long TTS = long echo window = broken loop.
            spoken = self._truncate_for_speech(reply)
            turn.brain_reply_head = spoken
            if spoken:
                _sys.stderr.write(f"[SPEAKING] {spoken!r}\n")
                _sys.stderr.flush()
                turn.tts_spoken = spoken
                self._speak_blocking(spoken)
            logger.debug("brain worker: say_sync done")
        except Exception as exc:
            turn.error = f"{type(exc).__name__}: {exc}"[:200]
            logger.exception("brain worker: _process_transcript failed")
        finally:
            self._brain_busy.clear()
            # Pair with the INFERENCE_START above so the dashboard orb
            # drops back out of `thinking` once the brain returns (or
            # errors). Wrapped — never let a publish failure mask the
            # real brain error in the worker.
            try:
                self._bus.publish(
                    EventType.INFERENCE_END,
                    {"engine": "claude-cli", "had_reply": bool(reply)},
                )
            except Exception:
                logger.debug("INFERENCE_END publish failed", exc_info=True)
            # Open the conversation window so the user's next utterance
            # doesn't require re-prefixing with "Hope".
            self._last_brain_turn_at = time.monotonic()
            turn.ended_at = time.time()
            turn.duration_seconds = max(0.0, turn.ended_at - turn.started_at)
            self._record_voice_turn(turn)
            # Drain any transcripts that arrived during this turn. Each
            # runs as its own full turn — the executor is single-slot so
            # they serialize cleanly after the current one returns.
            self._drain_pending_turns(main_id)

    # ----------------------------------------------------------------
    # Mid-brain actions — cancel (knob 1) + queue drain (knob 3).
    # ----------------------------------------------------------------

    def _try_start_proactive_recall(self) -> None:
        """Periodically surface pending commitments that are due soon.

        Quiet-hour aware (22:00–08:00 by default), presence aware (only
        speaks if the user said something in the last 30 minutes —
        otherwise Hope would be talking to an empty room), speak-aware
        (waits for _speaking / _brain_busy to clear).

        Runs every 15 min. Never raises. Disable by setting
        ``HOPE_PROACTIVE_RECALL=0`` in the daemon env.
        """
        if os.environ.get("HOPE_PROACTIVE_RECALL", "1") == "0":
            logger.info("proactive recall: disabled via HOPE_PROACTIVE_RECALL=0")
            return
        if getattr(self, "_recall_thread", None) is not None:
            return
        import sys as _sys

        self._recall_stop = threading.Event()
        interval = float(os.environ.get("HOPE_RECALL_INTERVAL_SEC", "900"))
        quiet_start = int(os.environ.get("HOPE_QUIET_START_HOUR", "22"))
        quiet_end = int(os.environ.get("HOPE_QUIET_END_HOUR", "8"))
        presence_window = float(
            os.environ.get("HOPE_RECALL_PRESENCE_WINDOW_SEC", "1800")
        )

        def _in_quiet_hours() -> bool:
            h = time.localtime().tm_hour
            if quiet_start > quiet_end:
                # Overnight window (22 → 08)
                return h >= quiet_start or h < quiet_end
            return quiet_start <= h < quiet_end

        def _user_present() -> bool:
            # Negative window = force-present (testing / dev mode).
            if presence_window < 0:
                return True
            # User said something within the presence window.
            if self._last_brain_turn_at <= 0:
                return False
            return (time.monotonic() - self._last_brain_turn_at) <= presence_window

        def _phrase(commitment) -> str:
            """Short, Hope-voiced reminder. Kept template-based — no LLM
            call — so the loop is cheap and predictable."""
            who = commitment.who or "self"
            what = commitment.what or commitment.content
            due = commitment.due
            # Humanise the due
            due_human = {
                "today": "today",
            }.get(due.lower() if isinstance(due, str) else "", due)
            if who.lower() == "self":
                return f"Reminder, sir — you wanted to {what} by {due_human}."
            return f"Reminder, sir — you said you'd {what} for {who} by {due_human}."

        def _loop() -> None:
            from hope.memory.commitments import (
                mark_reminded,
                next_recall_candidate,
            )
            while not self._recall_stop.wait(timeout=interval):
                try:
                    if self._listening_paused.is_set():
                        continue
                    if self._speaking.is_set() or self._brain_busy.is_set():
                        continue
                    if _in_quiet_hours():
                        continue
                    if not _user_present():
                        continue
                    cand = next_recall_candidate()
                    if cand is None:
                        continue
                    phrase = _phrase(cand)
                    _sys.stderr.write(
                        f"[PROACTIVE-RECALL] surfacing {cand.key}: {phrase!r}\n"
                    )
                    _sys.stderr.flush()
                    try:
                        self._speak_blocking(phrase)
                    except Exception:
                        logger.exception("recall: speak failed")
                        continue
                    mark_reminded(cand.key)
                except Exception:
                    logger.exception("proactive recall loop body")

        t = threading.Thread(
            target=_loop, name="hope-proactive-recall", daemon=True,
        )
        t.start()
        self._recall_thread = t
        _sys.stderr.write(
            f"[RECALL] proactive loop armed "
            f"(interval={interval}s quiet={quiet_start}-{quiet_end} "
            f"presence_window={presence_window}s)\n"
        )
        _sys.stderr.flush()

    @staticmethod
    def _memory_watcher_seed_ts(db_path: str) -> int:
        """Pick the cursor seed for the memory watcher.

        Strategy: look back to the newest row currently in the DB and
        subtract 1 ms — first poll then catches that row plus everything
        newer. If the DB has no rows yet (or doesn't exist), seed at
        ``now - 7 days`` so any history written in the last week
        backfills the dashboard on connect. The per-poll ``LIMIT 50``
        in ``_loop`` drains gradually without flooding the bus.
        """
        import sqlite3 as _sqlite
        now_ms = int(time.time() * 1000)
        seven_days_ms = 7 * 86_400_000
        try:
            if not os.path.exists(db_path):
                return now_ms - seven_days_ms
            conn = _sqlite.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=1.0,
            )
            try:
                row = conn.execute(
                    "SELECT MAX(created_at) FROM memory_entries "
                    "WHERE status='active'"
                ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return max(int(row[0]) - 1, now_ms - seven_days_ms)
        except Exception:
            logger.debug("memory watcher seed failed — falling back to 7d",
                         exc_info=True)
        return now_ms - seven_days_ms

    def _try_start_memory_watcher(self) -> None:
        """Tail the pinned memory DB and republish MEMORY_STORE events.

        Hope's brain writes memories via an MCP subprocess that doesn't
        share our event bus, so the dashboard would otherwise be blind
        to every memory_store call. This watcher polls the canonical DB
        (``CLAUDE_FLOW_MEMORY_DB`` or the CWD default) every few seconds,
        detects rows newer than we last saw, and republishes one
        ``MEMORY_STORE`` event per new row on OUR bus — which the
        dashboard bridge already forwards to the Tauri frontend.

        Never raises. Failure = dashboard memory panel stays empty;
        everything else keeps working.
        """
        if getattr(self, "_memory_watcher_thread", None) is not None:
            return
        db_path = os.environ.get("CLAUDE_FLOW_MEMORY_DB") \
            or os.environ.get("HOPE_MEMORY_DB") \
            or os.path.join(os.path.expanduser("~"), ".hope", "memory.db")
        if not os.path.exists(db_path):
            logger.info("memory watcher: %s doesn't exist yet — still starting",
                        db_path)
        self._memory_watcher_stop = threading.Event()
        # Seed the cursor BEFORE the latest existing row so the dashboard
        # backfills history on connect, not from the moment the daemon
        # last booted. Otherwise every restart skips past every memory
        # ever written and the panel sticks on "no recent memory writes."
        self._memory_watcher_last_ts = self._memory_watcher_seed_ts(db_path)
        self._memory_watcher_db = db_path

        def _loop() -> None:
            import sqlite3 as _sqlite
            while not self._memory_watcher_stop.wait(timeout=3.0):
                try:
                    if not os.path.exists(self._memory_watcher_db):
                        continue
                    conn = _sqlite.connect(
                        f"file:{self._memory_watcher_db}?mode=ro",
                        uri=True, timeout=1.0,
                    )
                    conn.row_factory = _sqlite.Row
                    cur = conn.execute(
                        "SELECT id, key, namespace, content, created_at, "
                        "       tags, metadata "
                        "FROM memory_entries "
                        "WHERE status='active' AND created_at > ? "
                        "ORDER BY created_at ASC LIMIT 50",
                        (self._memory_watcher_last_ts,),
                    )
                    rows = cur.fetchall()
                    conn.close()
                except Exception:
                    logger.debug("memory watcher: query failed",
                                 exc_info=True)
                    continue
                if not rows:
                    continue
                import sys as _sys
                _sys.stderr.write(
                    f"[MEM-WATCH] {len(rows)} new memory rows "
                    f"(ns={rows[0]['namespace']}, key={rows[0]['key'][:40]})\n"
                )
                _sys.stderr.flush()
                for row in rows:
                    try:
                        self._bus.publish(
                            EventType.MEMORY_STORE,
                            {
                                "key": row["key"],
                                "namespace": row["namespace"],
                                "content": (row["content"] or "")[:500],
                                "created_at": row["created_at"],
                                "tags": row["tags"],
                            },
                        )
                    except Exception:
                        logger.debug("memory watcher: publish failed",
                                     exc_info=True)
                self._memory_watcher_last_ts = max(
                    r["created_at"] for r in rows
                )

        t = threading.Thread(target=_loop, name="hope-memory-watcher",
                             daemon=True)
        t.start()
        self._memory_watcher_thread = t
        import sys as _sys
        _sys.stderr.write(
            f"[MEM-WATCH] tailing {db_path} from ts={self._memory_watcher_last_ts}\n"
        )
        _sys.stderr.flush()
        logger.info("memory watcher: tailing %s from ts=%s",
                    db_path, self._memory_watcher_last_ts)

    # ----------------------------------------------------------------
    # Mid-brain actions — cancel (knob 1) + queue drain (knob 3).
    # ----------------------------------------------------------------

    def _handle_cancel_midturn(self) -> None:
        """User said 'stop' / 'cancel' while the brain was generating.

        Sends Ctrl+C to the brain tmux pane, drains it, speaks a short
        confirmation. Never raises — a failed cancel should leave the
        caller's state untouched rather than crash the event bus.
        """
        orch = self._orchestrator
        if orch is None:
            return
        main_id = getattr(orch, "hope_main_pane_id", None)
        if not main_id:
            return
        entry = orch.registry.get(main_id)
        target = entry.tmux_target if entry is not None else None
        if not target:
            return
        # SIGINT-equivalent to the pane. Twice to clear any prompt waiting
        # for a decision plus interrupt the current generation.
        try:
            orch._tmux(  # type: ignore[attr-defined]
                ["tmux", "send-keys", "-t", target, "C-c"], check=False,
            )
            time.sleep(0.1)
            orch._tmux(  # type: ignore[attr-defined]
                ["tmux", "send-keys", "-t", target, "C-c"], check=False,
            )
        except Exception:
            logger.debug("cancel: tmux send-keys failed", exc_info=True)
        # Drop the pending queue — the cancel intent covers them too.
        with self._pending_lock:
            dropped = len(self._pending_transcripts)
            self._pending_transcripts.clear()
        if dropped:
            logger.info("cancel: dropped %d pending transcripts", dropped)
        # Let the user hear that the cancel landed.
        cancel_acks = (
            "Stopped, sir.",
            "Got it — scrapping that.",
            "Right, dropping it.",
            "Cancelled. Standing by.",
            "Done — all stopped.",
        )
        try:
            import random as _r
            self._speak_blocking(_r.choice(cancel_acks))
        except Exception:
            logger.debug("cancel: speak failed", exc_info=True)
        # Publish for the dashboard.
        try:
            self._bus.publish(
                EventType.BRAIN_CANCELLED
                if hasattr(EventType, "BRAIN_CANCELLED")
                else "brain_cancelled",
                {"timestamp": time.time()},
            )
        except Exception:
            pass
        # Clear brain_busy so subsequent transcripts aren't trapped in
        # the mid-brain path. The _process_transcript worker thread will
        # also clear it in its finally block, but that happens after
        # session.send() times out / returns — we don't want to wait.
        self._brain_busy.clear()
        # Drop the orb out of `thinking` immediately on cancel — without
        # this it would stay until the worker's finally block fires.
        try:
            self._bus.publish(
                EventType.INFERENCE_END,
                {"engine": "claude-cli", "cancelled": True},
            )
        except Exception:
            logger.debug("INFERENCE_END (cancel) publish failed", exc_info=True)
        # Keep the conversation window open so the user's next utterance
        # doesn't need re-prefixing ("Hope, actually do X") — a cancel
        # usually leads immediately into a redirect.
        self._last_brain_turn_at = time.monotonic()

    def _drain_pending_turns(self, main_id: str) -> None:
        """After a turn ends, process any queued mid-brain transcripts.

        Each queued transcript becomes its own full turn (ack + brain
        send + reply TTS). We're already running INSIDE the single-slot
        brain executor (this is called from ``_process_transcript``'s
        ``finally`` block), so we invoke ``_process_transcript``
        directly instead of submitting back into the executor — that
        would deadlock because the only worker slot is occupied by us.
        """
        while True:
            with self._pending_lock:
                if not self._pending_transcripts:
                    return
                nxt = self._pending_transcripts.pop(0)
            logger.info("draining queued transcript: %r", nxt[:80])
            try:
                # Direct call — already on the brain worker thread.
                # New SPEECH_TRANSCRIPT events arriving during this
                # call will be queued (brain_busy is back to set
                # because _process_transcript flips it inside) and
                # picked up by the next iteration of this loop.
                self._process_transcript(nxt, main_id)
            except Exception:
                logger.exception("drain: pending turn failed")

    def _record_voice_turn(self, turn: VoiceTurn) -> None:
        """Persist *turn* + poke the learning loop. Never raises."""
        store = self._voice_store
        if store is None:
            return
        try:
            store.save(turn)
        except Exception:
            logger.exception("voice_trace save failed")
            return
        self._last_turn = turn
        loop = self._voice_loop
        if loop is not None:
            try:
                loop.note_turn()
            except Exception:
                logger.exception("voice learning note_turn failed")

    @staticmethod
    def _is_wake_phrase(text: str) -> bool:
        t = text.lower().strip(" .,!?")
        try:
            phrases = load_wake_phrases()
        except Exception:  # pragma: no cover
            phrases = list(DEFAULT_WAKE_PHRASES)
        for phrase in phrases:
            if phrase in t:
                return True
        return False

    def _try_start_wake_monitor(self) -> Any:
        """Attempt to import + start the sibling wake-word module.

        Returns the monitor instance on success, ``None`` if the module
        isn't available or fails to start. Never raises — we refuse to
        let the wake monitor tank the whole daemon.
        """
        try:
            from hope.wakeword import WakeMonitor  # type: ignore[import-not-found]
        except Exception as exc:
            logger.warning(
                "hope.wakeword not available (%s); "
                "wake monitor disabled — daemon will rely on manual 'hope wake'",
                exc,
            )
            return None
        try:
            # Share the speech backend's MicCapture if one is running —
            # opening a second PortAudio stream on the same device fails
            # with err=-50 on macOS.
            shared_mic = getattr(self._speech_backend, "_capture", None)
            if shared_mic is not None:
                monitor = WakeMonitor(self._bus, mic_capture=shared_mic)
            else:
                monitor = WakeMonitor(self._bus)
            monitor.start()
            return monitor
        except Exception:
            logger.exception("WakeMonitor.start() failed; continuing without it")
            return None

    # ── control socket ───────────────────────────────────────────────

    def _start_control_socket(self) -> None:
        path = self._control_socket_path
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        path.parent.mkdir(parents=True, exist_ok=True)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        sock.listen(_CONTROL_BACKLOG)
        sock.settimeout(0.5)
        self._control_sock = sock
        self._control_thread = threading.Thread(
            target=self._control_loop,
            name="hope-daemon-control",
            daemon=True,
        )
        self._control_thread.start()

    def _control_loop(self) -> None:
        while not self._stop_event.is_set():
            sock = self._control_sock
            if sock is None:
                return
            try:
                conn, _ = sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            try:
                with conn:
                    conn.settimeout(_CONTROL_TIMEOUT_SEC)
                    data = b""
                    while True:
                        try:
                            chunk = conn.recv(4096)
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        data += chunk
                        if b"\n" in chunk:
                            break
                    response = self._handle_control(data)
                    try:
                        conn.sendall((json.dumps(response) + "\n").encode())
                    except OSError:
                        pass
            except Exception:
                logger.exception("control-socket frame failed")

    def _handle_control(self, raw: bytes) -> Dict[str, Any]:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return {"ok": False, "error": "empty request"}
        try:
            req = json.loads(text.split("\n", 1)[0])
        except json.JSONDecodeError:
            return {"ok": False, "error": "malformed json"}
        cmd = req.get("cmd", "")
        payload = req.get("payload") or {}
        if cmd == "ping":
            return {"ok": True, "pong": True}
        if cmd == "wake":
            # Subscribers (notably the orchestrator) spawn tmux + claude
            # synchronously inside their handler, which can take 5–15s.
            # The control socket has a 2s timeout, so an inline publish
            # makes `hope wake` always report "Wake failed: timed out"
            # even when the wake completes successfully. Dispatch the
            # publish on a daemon thread so we can ack immediately.
            event_payload = {
                "source": payload.get("source", "manual"),
                "text": payload.get("text"),
                "timestamp": time.time(),
            }
            threading.Thread(
                target=self._bus.publish,
                args=(EventType.WAKE_TRIGGER, event_payload),
                name="hope-wake-publish",
                daemon=True,
            ).start()
            return {"ok": True}
        if cmd == "sleep":
            # Brain-only sleep — daemon keeps running so the next
            # WAKE_TRIGGER can spawn a fresh brain.
            threading.Thread(
                target=self.sleep_brain,
                name="hope-daemon-sleep-brain",
                daemon=True,
            ).start()
            return {"ok": True, "brain_sleeping": True}
        if cmd == "stop":
            # Full daemon teardown — Python process exits.
            threading.Thread(
                target=self.shutdown,
                name="hope-daemon-stop",
                daemon=True,
            ).start()
            return {"ok": True, "shutting_down": True}
        if cmd == "status":
            return {"ok": True, "state": self.snapshot().to_dict()}
        if cmd == "pause_listening":
            changed = self.pause_listening()
            return {"ok": True, "paused": True, "changed": changed}
        if cmd == "resume_listening":
            changed = self.resume_listening()
            return {"ok": True, "paused": False, "changed": changed}
        if cmd == "toggle_listening":
            now_paused = self.toggle_listening()
            return {"ok": True, "paused": now_paused}
        if cmd == "speech_transcript":
            # Inject a SPEECH_TRANSCRIPT event into the bus. This is the
            # textual side of the voice pipeline — exactly what the STT
            # backend would publish when it finalizes an utterance. Used
            # for end-to-end tests, scripts that pipe text into Hope, and
            # the dashboard's "type to Hope" affordance.
            text = str(payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "speech_transcript needs 'text'"}
            event_payload = {
                "text": text,
                "confidence": float(payload.get("confidence", 1.0)),
                "timestamp": time.time(),
                "source": payload.get("source", "control_socket"),
            }
            threading.Thread(
                target=self._bus.publish,
                args=(EventType.SPEECH_TRANSCRIPT, event_payload),
                name="hope-transcript-publish",
                daemon=True,
            ).start()
            return {"ok": True}
        if cmd == "send_message":
            # Route a message between panes via the orchestrator's
            # pub/sub bus. Used by the brain to ship context to a
            # specialist mid-flight (the whole reason for tmux-pane
            # CLIs over inline Task subagents).
            from_pane = str(payload.get("from") or "hope-main").strip()
            to = str(payload.get("to") or "").strip()
            topic = str(payload.get("topic") or "").strip()
            body = str(payload.get("body") or "")
            corr = payload.get("correlation_id")
            if not to or not topic or not body:
                return {"ok": False,
                        "error": "send_message needs 'to', 'topic', 'body'"}
            orch = self._orchestrator
            if orch is None:
                return {"ok": False, "error": "orchestrator not started"}
            # Resolve "hope-main" into the actual pane id.
            if from_pane == "hope-main":
                from_pane = getattr(orch, "hope_main_pane_id", None) or "hope-main"
            try:
                env = orch.send_message(
                    from_pane=from_pane,
                    to=to,
                    topic=topic,
                    body=body,
                    correlation_id=corr,
                )
                return {"ok": True, "id": env.get("id")}
            except Exception as exc:
                return {"ok": False, "error": f"send_message failed: {exc}"}
        if cmd == "kill_specialist":
            pane = str(payload.get("pane_id") or "").strip()
            if not pane:
                return {"ok": False, "error": "kill_specialist needs 'pane_id'"}
            orch = self._orchestrator
            if orch is None:
                return {"ok": False, "error": "orchestrator not started"}
            try:
                orch.kill_specialist(pane)
                return {"ok": True, "killed": pane}
            except Exception as exc:
                return {"ok": False, "error": f"kill_specialist failed: {exc}"}
        if cmd == "list_specialists":
            orch = self._orchestrator
            if orch is None:
                return {"ok": False, "error": "orchestrator not started"}
            try:
                specs = [
                    {
                        "pane_id": e.pane_id,
                        "role": e.role,
                        "tmux_target": e.tmux_target,
                        "spawned_at": e.spawned_at,
                    }
                    for e in orch.registry.specialists()
                ]
                return {"ok": True, "specialists": specs}
            except Exception as exc:
                return {"ok": False, "error": f"list failed: {exc}"}
        if cmd == "journal_query":
            orch = self._orchestrator
            if orch is None:
                return {"ok": False, "error": "orchestrator not started"}
            statuses = payload.get("statuses")
            if not isinstance(statuses, list) or not statuses:
                statuses = None
            try:
                limit = int(payload.get("limit", 50))
            except Exception:
                limit = 50
            try:
                tasks = orch.journal_query(statuses=statuses, limit=limit)
                return {"ok": True, "tasks": tasks}
            except Exception as exc:
                return {"ok": False, "error": f"journal_query failed: {exc}"}
        if cmd == "journal_get":
            orch = self._orchestrator
            if orch is None:
                return {"ok": False, "error": "orchestrator not started"}
            tid = str(payload.get("task_id") or "").strip()
            if not tid:
                return {"ok": False, "error": "journal_get needs 'task_id'"}
            try:
                task = orch.journal_get(tid)
                history = orch.journal_history_for_pane(
                    task.get("pane_id") or "",
                ) if task else []
                return {"ok": True, "task": task, "history": history}
            except Exception as exc:
                return {"ok": False, "error": f"journal_get failed: {exc}"}
        if cmd == "journal_resume":
            # Resume an abandoned task by spawning a fresh specialist
            # and feeding it the prior pane's bus history as context.
            orch = self._orchestrator
            if orch is None:
                return {"ok": False, "error": "orchestrator not started"}
            tid = str(payload.get("task_id") or "").strip()
            if not tid:
                return {"ok": False, "error": "journal_resume needs 'task_id'"}
            try:
                task = orch.journal_get(tid)
            except Exception as exc:
                return {"ok": False, "error": f"resume failed: {exc}"}
            if not task:
                return {"ok": False, "error": f"task {tid!r} not found"}
            if task.get("status") not in ("abandoned", "completed"):
                return {
                    "ok": False,
                    "error": f"task is {task.get('status')!r} — only "
                             "abandoned/completed tasks are resumable",
                }
            history = orch.journal_history_for_pane(
                task.get("pane_id") or "",
            )
            history_blurb = " | ".join(
                f"{h.get('agent_id')}->{h.get('topic')}: "
                f"{(h.get('content') or '')[:80]}"
                for h in history[-8:]
            ) or "(no prior bus messages)"
            resume_task = (
                f"RESUMING task {tid} (was {task.get('status')}). "
                f"Original goal: {task.get('goal')}. "
                f"Prior bus history: {history_blurb}. "
                f"Pick up where the previous specialist left off."
            )
            ctx_orig: Dict[str, Any] = {}
            try:
                if task.get("context_json"):
                    ctx_orig = json.loads(task["context_json"])
            except Exception:
                ctx_orig = {}
            ctx_orig["resumed_from_task"] = tid
            ctx_orig["prior_status"] = task.get("status")
            cli_for_resume = str(payload.get("cli") or task.get("cli") or "claude")

            def _do_resume() -> None:
                try:
                    orch.spawn_specialist(
                        role=task.get("role") or "researcher",
                        task=resume_task,
                        context=ctx_orig,
                        cli=cli_for_resume,
                    )
                except Exception:
                    logger.exception("journal_resume spawn failed")

            threading.Thread(
                target=_do_resume, name=f"hope-resume-{tid}", daemon=True,
            ).start()
            return {"ok": True, "resumed_task_id": tid, "role": task.get("role")}
        if cmd == "journal_cancel":
            orch = self._orchestrator
            if orch is None:
                return {"ok": False, "error": "orchestrator not started"}
            tid = str(payload.get("task_id") or "").strip()
            if not tid:
                return {"ok": False, "error": "journal_cancel needs 'task_id'"}
            try:
                with orch._db_lock:
                    orch._conn.execute(
                        "UPDATE task_journal SET status='cancelled', "
                        " last_update=? WHERE task_id=?",
                        (time.time(), tid),
                    )
                    orch._conn.commit()
                return {"ok": True, "cancelled_task_id": tid}
            except Exception as exc:
                return {"ok": False, "error": f"cancel failed: {exc}"}
        if cmd == "spawn_specialist":
            # The brain calls this through ``bin/hope-spawn`` (which
            # shells into the control socket) instead of the native
            # ``Task`` tool, which is denied per .claude/settings.json.
            # Spawning takes ~3-5s for the Claude Code boot, so do it on
            # a worker thread and ack immediately with the queued/spawned
            # marker — the actual pane_id will surface via PANE_SPAWNED
            # on the dashboard bus.
            role = str(payload.get("role") or "").strip()
            task = str(payload.get("task") or "").strip()
            if not role or not task:
                return {"ok": False,
                        "error": "spawn_specialist needs 'role' and 'task'"}
            context = payload.get("context") if isinstance(
                payload.get("context"), dict) else None
            system_prompt = payload.get("system_prompt") if isinstance(
                payload.get("system_prompt"), str) else None
            cli = str(payload.get("cli") or "claude").strip().lower()
            if cli not in ("claude", "gemini", "codex"):
                return {"ok": False,
                        "error": f"unknown cli={cli!r} "
                                 "(supported: claude, gemini, codex)"}
            orch = self._orchestrator
            if orch is None:
                return {"ok": False, "error": "orchestrator not started"}

            def _do_spawn() -> None:
                try:
                    orch.spawn_specialist(
                        role=role,
                        task=task,
                        context=context,
                        system_prompt=system_prompt,
                        cli=cli,
                    )
                except Exception:
                    logger.exception("spawn_specialist via control failed")

            threading.Thread(
                target=_do_spawn,
                name="hope-daemon-spawn-specialist",
                daemon=True,
            ).start()
            return {"ok": True, "queued": True, "role": role}
        return {"ok": False, "error": f"unknown cmd: {cmd!r}"}

    # ── introspection ────────────────────────────────────────────────

    def snapshot(self) -> DaemonState:
        orch = self._orchestrator
        reg = getattr(orch, "registry", None)
        specialist_count = 0
        specialists: List[Dict[str, Any]] = []
        if reg is not None:
            try:
                specialist_count = reg.specialist_count()
            except Exception:
                specialist_count = 0
            try:
                specialists = [
                    {
                        "pane_id": e.pane_id,
                        "role": e.role,
                        "spawned_at": e.spawned_at,
                        "tmux_target": e.tmux_target,
                        "parent_pane": e.parent_pane,
                    }
                    for e in reg.specialists()
                ]
            except Exception:
                specialists = []
        queued = 0
        if orch is not None:
            try:
                queued = orch.queued_spawn_count()
            except Exception:
                queued = 0
        wm = self._wake_monitor
        wm_active = False
        if wm is not None:
            try:
                wm_active = bool(getattr(wm, "is_monitoring", False))
            except Exception:
                wm_active = False
        bus_socket_path = ""
        if orch is not None:
            try:
                bus_socket_path = str(orch.bus_socket_path)
            except Exception:
                bus_socket_path = ""
        return DaemonState(
            pid=os.getpid(),
            started_at=self._started_at,
            orchestrator_started=bool(getattr(orch, "_started", False)),
            hope_main_pane_id=getattr(orch, "hope_main_pane_id", None),
            specialist_count=specialist_count,
            specialists=specialists,
            queued_spawn_count=queued,
            wake_monitor_available=wm is not None,
            wake_monitor_active=wm_active,
            bus_socket=bus_socket_path,
            control_socket=str(self._control_socket_path),
            listening_paused=self._listening_paused.is_set(),
            brain_state=self._compute_brain_state(),
        )

    def _compute_brain_state(self) -> str:
        """Derive a coarse brain phase from internal flags. Used by the
        dashboard state_snapshot so the orb shows the right colour on
        connect/reconnect, not the React store default."""
        if self._speaking.is_set():
            return "speaking"
        if self._brain_busy.is_set():
            return "thinking"
        orch = self._orchestrator
        main_id = getattr(orch, "hope_main_pane_id", None) if orch else None
        if not main_id:
            return "sleeping"
        return "idle"

    # ── signals ──────────────────────────────────────────────────────

    def _handle_signal(self, signum: int, frame: Optional[FrameType]) -> None:
        logger.info("received signal %d; shutting down", signum)
        self._shutdown_requested.set()


__all__ = [
    "CONTROL_SOCKET",
    "DaemonState",
    "HopeDaemon",
    "LOG_FILE",
    "PID_FILE",
    "clear_pid",
    "read_pid",
    "send_control",
    "write_pid",
]
