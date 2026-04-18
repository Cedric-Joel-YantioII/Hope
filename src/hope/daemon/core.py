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
import signal
import socket
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any, Dict, Optional

from hope.audio import say
from hope.core.config import DEFAULT_CONFIG_DIR, load_config
from hope.core.events import EventBus, EventType, get_event_bus

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
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "orchestrator_started": self.orchestrator_started,
            "hope_main_pane_id": self.hope_main_pane_id,
            "specialist_count": self.specialist_count,
            "queued_spawn_count": self.queued_spawn_count,
            "wake_monitor_available": self.wake_monitor_available,
            "wake_monitor_active": self.wake_monitor_active,
            "bus_socket": self.bus_socket,
            "control_socket": self.control_socket,
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

        # Wake monitor (optional, best-effort)
        if self._enable_wake and self._wake_monitor is None:
            self._wake_monitor = self._try_start_wake_monitor()
        elif self._wake_monitor is not None:
            try:
                self._wake_monitor.start()
            except Exception:
                logger.exception("pre-injected wake_monitor.start() failed")

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

        self._started_at = time.time()
        logger.info(
            "Hope daemon started (sleeping) pid=%d wake_monitor=%s",
            os.getpid(),
            self._wake_monitor is not None,
        )
        return self.snapshot()

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

        # Orchestrator — kills specialists, closes the bus socket.
        if self._orchestrator is not None:
            try:
                self._orchestrator.shutdown()
            except Exception:
                logger.exception("orchestrator.shutdown() failed")

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
        """Handle a :attr:`EventType.WAKE_TRIGGER` event."""
        payload = getattr(event, "data", {}) or {}
        source = payload.get("source", "unknown")
        logger.info("WAKE_TRIGGER received source=%s", source)

        orch = self._orchestrator
        if orch is None:
            # Extremely defensive — _on_wake is wired only after orchestrator
            # exists, but a late callback during shutdown could still land.
            say("Hope is not ready")
            return

        # _started is the authoritative signal — hope_main_pane_id can
        # linger after sleep_brain as a historical reference.
        if bool(getattr(orch, "_started", False)):
            say("I'm already awake")
            return
        try:
            orch.start()
        except Exception:
            logger.exception("orchestrator.start() on wake failed")
            say("Hope failed to wake up")
            return
        say("Hope is awake")

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
            self._bus.publish(
                EventType.WAKE_TRIGGER,
                {
                    "source": payload.get("source", "manual"),
                    "text": payload.get("text"),
                    "timestamp": time.time(),
                },
            )
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
        return {"ok": False, "error": f"unknown cmd: {cmd!r}"}

    # ── introspection ────────────────────────────────────────────────

    def snapshot(self) -> DaemonState:
        orch = self._orchestrator
        reg = getattr(orch, "registry", None)
        specialist_count = 0
        if reg is not None:
            try:
                specialist_count = reg.specialist_count()
            except Exception:
                specialist_count = 0
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
            queued_spawn_count=queued,
            wake_monitor_available=wm is not None,
            wake_monitor_active=wm_active,
            bus_socket=bus_socket_path,
            control_socket=str(self._control_socket_path),
        )

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
