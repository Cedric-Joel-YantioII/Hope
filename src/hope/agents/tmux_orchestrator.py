"""Tmux-backed specialist orchestrator for Hope.

Hope's brain is a *persistent* Claude Code CLI session living in a tmux pane
titled ``hope``. Specialists are *ephemeral* Claude Code panes spawned on
demand by that brain. This module owns the full lifecycle:

- starting the ``hope`` tmux session and the hope-main pane
- spawning and killing specialist panes with role-specific system prompts
- running a single Unix-domain socket "bus" at ``~/.hope/panes/bus.sock``
  that every pane can publish to and subscribe on (topics like
  ``to:{pane_id}``, ``to:{role}``, ``broadcast``, ``tools:request``)
- persisting every routed message into the ``agent_messages`` SQLite table
  via :class:`~hope.agents.manager.AgentManager` so replay + audit work
- enforcing a soft max-concurrent-specialists cap and queueing spawn
  requests with a ``SPECIALIST_AT_CAPACITY`` event when the cap is hit

Identity invariant: the *system* is Hope. Each pane is "Hope's {role}" —
only the hope-main pane ever speaks as "I am Hope".

The sibling engine module (``hope.engine.claude_code_tmux``) owns the
sentinel parsing inside a pane. This orchestrator publishes field names
``pane_target``, ``fifo_path``, ``request_timeout_sec``, and
``sentinel_prefix`` on its :meth:`pane_engine_config` helper so the
engine can bind to what the orchestrator created without duplicating
paths.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from hope.core.config import DEFAULT_CONFIG_DIR, OrchestratorConfig, load_config
from hope.core.events import EventBus, EventType

from hope.agents.specialist_registry import PaneEntry, SpecialistRegistry

logger = logging.getLogger(__name__)

# Sentinel prefix the pane engine will also use (see docstring above).
PANE_SENTINEL_PREFIX = "---HOPE_PANE"
_DEFAULT_REQUEST_TIMEOUT_SEC = 300

# SQL for the new tmux_panes table + agent_messages extensions. Kept in the
# same plain-string-per-migration style used by AgentManager.
_CREATE_TMUX_PANES = """\
CREATE TABLE IF NOT EXISTS tmux_panes (
    pane_id      TEXT PRIMARY KEY,
    role         TEXT NOT NULL,
    tmux_target  TEXT NOT NULL,
    fifo_path    TEXT NOT NULL,
    spawned_at   DATETIME NOT NULL,
    killed_at    DATETIME,
    is_ephemeral INTEGER NOT NULL DEFAULT 1,
    parent_pane  TEXT
);
"""

_AGENT_MESSAGES_MIGRATIONS = (
    "ALTER TABLE agent_messages ADD COLUMN from_role TEXT",
    "ALTER TABLE agent_messages ADD COLUMN to_role TEXT",
    "ALTER TABLE agent_messages ADD COLUMN topic TEXT",
    "ALTER TABLE agent_messages ADD COLUMN correlation_id TEXT",
)


def apply_orchestrator_migrations(conn: sqlite3.Connection) -> None:
    """Create ``tmux_panes`` and extend ``agent_messages`` in-place.

    Idempotent — individual ``ALTER TABLE`` statements are wrapped in
    per-column try/except so re-running is a no-op. Mirrors the style
    used by :class:`hope.agents.manager.AgentManager`.
    """
    conn.executescript(_CREATE_TMUX_PANES)
    # agent_messages may not yet exist in very old databases; ensure it
    # does before we try to ALTER it.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_messages ("
        " id TEXT PRIMARY KEY,"
        " agent_id TEXT NOT NULL,"
        " direction TEXT NOT NULL,"
        " content TEXT NOT NULL,"
        " mode TEXT NOT NULL DEFAULT 'queued',"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " created_at REAL NOT NULL"
        ")"
    )
    for migration in _AGENT_MESSAGES_MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # Column already exists — idempotent
    conn.commit()


# ---------------------------------------------------------------------------
# Queued spawn requests
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _QueuedSpawn:
    role: str
    task: str
    context: Dict[str, Any]
    parent_pane: Optional[str]
    request_id: str


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TmuxOrchestrator:
    """Owns the tmux session, pane bus, and specialist lifecycle.

    The orchestrator is designed to run as a long-lived singleton inside
    Hope's daemon. Methods are thread-safe. The underlying tmux runner is
    swappable via the ``tmux_runner`` constructor arg so tests can fake it.
    """

    def __init__(
        self,
        *,
        config: Optional[OrchestratorConfig] = None,
        db_path: Optional[str] = None,
        bus: Optional[EventBus] = None,
        registry: Optional[SpecialistRegistry] = None,
        tmux_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        claude_command: Optional[List[str]] = None,
        project_dir: Optional[str] = None,
    ) -> None:
        if config is None:
            try:
                config = load_config().orchestrator
            except Exception:  # pragma: no cover — fall back to defaults
                config = OrchestratorConfig()
        self._config = config
        self._bus = bus or EventBus()
        self._registry = registry or SpecialistRegistry()
        self._tmux_runner = tmux_runner or self._default_tmux_runner
        self._claude_command = claude_command or [
            "claude",
            "--dangerously-skip-permissions",
        ]
        self._project_dir = project_dir or str(Path.cwd())
        self._panes_dir = Path(config.panes_dir).expanduser()
        self._bus_socket_path = Path(config.bus_socket_path).expanduser()
        self._roles_dir = Path(config.roles_dir).expanduser()

        # Persistence
        self._db_path = db_path or str(DEFAULT_CONFIG_DIR / "agents.db")
        self._db_lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        apply_orchestrator_migrations(self._conn)

        # Bus socket + listener
        self._socket_lock = threading.Lock()
        self._listener_thread: Optional[threading.Thread] = None
        self._listener_sock: Optional[socket.socket] = None
        self._stop_event = threading.Event()

        # Capacity queue
        self._spawn_queue_lock = threading.Lock()
        self._spawn_queue: Deque[_QueuedSpawn] = deque()

        # Lifecycle
        self._started = False
        self._hope_main_pane_id: Optional[str] = None

    # ── public API ───────────────────────────────────────────────────

    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def registry(self) -> SpecialistRegistry:
        return self._registry

    @property
    def hope_main_pane_id(self) -> Optional[str]:
        return self._hope_main_pane_id

    @property
    def bus_socket_path(self) -> Path:
        return self._bus_socket_path

    def pane_engine_config(self, pane_id: str) -> Dict[str, Any]:
        """Return the dict the engine agent's ``ClaudeCodeTmuxEngineConfig``
        binds to: ``pane_target``, ``fifo_path``, ``request_timeout_sec``,
        ``sentinel_prefix``.
        """
        entry = self._registry.get(pane_id)
        if entry is None:
            raise KeyError(pane_id)
        return {
            "pane_target": entry.tmux_target,
            "fifo_path": entry.fifo_path,
            "request_timeout_sec": _DEFAULT_REQUEST_TIMEOUT_SEC,
            "sentinel_prefix": PANE_SENTINEL_PREFIX,
        }

    # ── start / shutdown ─────────────────────────────────────────────

    def start(self) -> str:
        """Ensure the tmux server + ``hope`` session + hope-main pane exist.

        Also sets up the Unix-domain bus socket and its listener thread.
        Returns the hope-main pane id. Idempotent.
        """
        if self._started:
            return self._hope_main_pane_id or ""

        self._panes_dir.mkdir(parents=True, exist_ok=True)
        # Restrictive perms — sockets in a shared $HOME are a footgun
        try:
            os.chmod(self._panes_dir, stat.S_IRWXU)
        except OSError:
            pass

        # Start the hope session (tmux is idempotent w/ has-session check)
        session = self._config.tmux_session_name
        has_session = self._tmux(
            ["tmux", "has-session", "-t", session],
            check=False,
        )
        if has_session.returncode != 0:
            self._tmux(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    session,
                    "-c",
                    self._project_dir,
                    "-n",
                    "hope",
                ]
            )

        # Hope-main pane
        pane_id = self._allocate_pane_id(role="hope")
        tmux_target = f"{session}:hope.0"
        fifo_path = self._ensure_fifo(pane_id)
        self._tmux(
            [
                "tmux",
                "select-pane",
                "-t",
                tmux_target,
                "-T",
                "hope",
            ],
            check=False,
        )

        # If we just created the session, run claude inside its window.
        # If the session already existed we don't relaunch — user may have
        # a live brain already.
        if has_session.returncode != 0:
            self._send_keys(tmux_target, " ".join(self._claude_command))

        entry = self._registry.register(
            pane_id=pane_id,
            role="hope",
            tmux_target=tmux_target,
            fifo_path=str(fifo_path),
            is_ephemeral=False,
            parent_pane=None,
            topics={f"to:{pane_id}", "to:hope", "broadcast"},
        )
        self._record_pane(entry)
        self._hope_main_pane_id = pane_id
        # Wire the pane's stdout into the FIFO so the engine can read it.
        self._attach_pipe_pane(tmux_target, str(fifo_path))

        # Bus socket
        self._start_bus_listener()
        self._started = True

        self._bus.publish(
            EventType.PANE_SPAWNED,
            {
                "pane_id": pane_id,
                "role": "hope",
                "is_ephemeral": False,
                "tmux_target": tmux_target,
            },
        )
        return pane_id

    def shutdown(self) -> None:
        """Kill specialists, gracefully stop hope-main, close the bus.

        Best-effort — never raises. Safe to call when the orchestrator has
        not been started.
        """
        # Kill every ephemeral specialist first.
        for entry in list(self._registry.specialists()):
            try:
                self.kill_specialist(entry.pane_id)
            except Exception:  # pragma: no cover
                logger.exception("kill_specialist(%s) failed", entry.pane_id)

        # Graceful 'save context' nudge to hope-main before we tear it down.
        if self._hope_main_pane_id is not None:
            hope = self._registry.get(self._hope_main_pane_id)
            if hope is not None:
                try:
                    self._send_keys(
                        hope.tmux_target,
                        "# Hope: saving context before shutdown",
                    )
                except Exception:  # pragma: no cover
                    logger.exception("graceful shutdown nudge failed")
                # We deliberately do NOT kill the hope-main pane here —
                # the user may want to reconnect. But we do remove it
                # from the live registry.
                self._registry.deregister(hope.pane_id)
                self._mark_killed(hope.pane_id)

        # Stop the bus.
        self._stop_event.set()
        with self._socket_lock:
            if self._listener_sock is not None:
                try:
                    self._listener_sock.close()
                except OSError:
                    pass
                self._listener_sock = None
        if self._listener_thread is not None and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=2.0)
            self._listener_thread = None
        if self._bus_socket_path.exists():
            try:
                self._bus_socket_path.unlink()
            except OSError:
                pass

        self._started = False

    # ── spawn / kill ─────────────────────────────────────────────────

    def spawn_specialist(
        self,
        role: str,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        *,
        parent_pane: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Spawn an ephemeral specialist pane for *role*.

        Enforces ``max_concurrent_specialists``. At capacity the request is
        queued, ``SPECIALIST_AT_CAPACITY`` is emitted, and the empty string
        is returned — Hope is expected to either wait for a ``PANE_KILLED``
        (and let :meth:`drain_spawn_queue` replay the request) or call
        :meth:`kill_specialist` on an idle pane.

        Returns the new pane id on success.
        """
        if not self._started:
            self.start()

        context = context or {}
        cap = self._config.max_concurrent_specialists
        if self._registry.specialist_count() >= cap:
            request_id = uuid.uuid4().hex[:12]
            with self._spawn_queue_lock:
                self._spawn_queue.append(
                    _QueuedSpawn(
                        role=role,
                        task=task,
                        context=context,
                        parent_pane=parent_pane,
                        request_id=request_id,
                    )
                )
            self._bus.publish(
                EventType.SPECIALIST_AT_CAPACITY,
                {
                    "role": role,
                    "task": task,
                    "request_id": request_id,
                    "capacity": cap,
                    "in_use": self._registry.specialist_count(),
                },
            )
            return ""

        pane_id = self._allocate_pane_id(role=role)
        session = self._config.tmux_session_name
        tmux_target = f"{session}:hope.{pane_id}"
        # Split pane off the hope window. tmux returns the pane identifier
        # when we pass -P -F '#{pane_id}', but we keep our own logical
        # pane_id for external identity and set tmux's pane title to
        # "Hope-{role}-{short_id}".
        split = self._tmux(
            [
                "tmux",
                "split-window",
                "-t",
                f"{session}:hope",
                "-c",
                self._project_dir,
                "-P",
                "-F",
                "#{pane_id}",
                "-d",
            ],
            check=False,
        )
        tmux_pane_ref = (split.stdout or "").strip() or tmux_target
        # Store the real tmux pane ref (e.g. %23) so send-keys/kill-pane
        # can target it deterministically.
        tmux_target = tmux_pane_ref
        self._tmux(
            [
                "tmux",
                "select-pane",
                "-t",
                tmux_target,
                "-T",
                f"Hope-{role}-{pane_id[:6]}",
            ],
            check=False,
        )

        fifo_path = self._ensure_fifo(pane_id)

        # Compose the system prompt — role template body + task/context.
        prompt = system_prompt or self._load_role_prompt(role)
        prompt = self._inject_task_into_prompt(prompt, task, context, pane_id)

        # Launch Claude Code with the composed prompt written to a temp file
        # rather than crammed into --append-system-prompt. The pane engine
        # picks up the file path via an env var; that handshake is the
        # engine agent's job, so here we just record the file and drop
        # Claude Code in the pane.
        prompt_path = self._panes_dir / f"{pane_id}.prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        launch_cmd = " ".join(self._claude_command) + (
            f" --append-system-prompt @{prompt_path}"
        )
        self._send_keys(tmux_target, launch_cmd)

        entry = self._registry.register(
            pane_id=pane_id,
            role=role,
            tmux_target=tmux_target,
            fifo_path=str(fifo_path),
            is_ephemeral=True,
            parent_pane=parent_pane,
            topics={
                f"to:{pane_id}",
                f"to:{role}",
                "broadcast",
                "tools:request",
            },
        )
        self._record_pane(entry)
        # Wire the pane's stdout into the FIFO so the engine can read it.
        self._attach_pipe_pane(tmux_target, str(fifo_path))

        self._bus.publish(
            EventType.PANE_SPAWNED,
            {
                "pane_id": pane_id,
                "role": role,
                "is_ephemeral": True,
                "tmux_target": tmux_target,
                "parent_pane": parent_pane,
            },
        )
        return pane_id

    def kill_specialist(self, pane_id: str) -> None:
        """Tear down a specialist pane. Idempotent.

        Closes the tmux pane, removes the FIFO, unsubscribes the pane from
        the bus, marks ``tmux_panes.killed_at``, and emits ``PANE_KILLED``.
        After a kill the spawn queue is drained — any queued spawn request
        will be serviced if capacity is now available.
        """
        entry = self._registry.get(pane_id)
        if entry is None:
            return

        # Close the tmux pane first so no more output hits the FIFO.
        try:
            self._tmux(
                ["tmux", "kill-pane", "-t", entry.tmux_target],
                check=False,
            )
        except Exception:  # pragma: no cover
            logger.exception("tmux kill-pane failed for %s", pane_id)

        # Remove FIFO + prompt file.
        for p in (Path(entry.fifo_path), self._panes_dir / f"{pane_id}.prompt.md"):
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
            except OSError:
                pass

        self._registry.deregister(pane_id)
        self._mark_killed(pane_id)
        self._bus.publish(
            EventType.PANE_KILLED,
            {"pane_id": pane_id, "role": entry.role},
        )

        # Service any queued spawn that now fits.
        self.drain_spawn_queue()

    def drain_spawn_queue(self) -> int:
        """Process queued spawns until capacity is exhausted.

        Returns the number of requests serviced.
        """
        serviced = 0
        while True:
            with self._spawn_queue_lock:
                if not self._spawn_queue:
                    return serviced
                if (
                    self._registry.specialist_count()
                    >= self._config.max_concurrent_specialists
                ):
                    return serviced
                queued = self._spawn_queue.popleft()
            new_id = self.spawn_specialist(
                queued.role,
                queued.task,
                queued.context,
                parent_pane=queued.parent_pane,
            )
            if new_id:
                serviced += 1
            else:  # Still at capacity — put it back and stop.
                with self._spawn_queue_lock:
                    self._spawn_queue.appendleft(queued)
                return serviced

    # ── messaging ────────────────────────────────────────────────────

    def subscribe(self, pane_id: str, topics: List[str]) -> None:
        """Add *topics* to an existing pane's subscription set."""
        self._registry.subscribe(pane_id, topics)

    def send_message(
        self,
        from_pane: str,
        to: str,
        topic: str,
        body: str,
        *,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Publish a message on the bus and persist it.

        *to* is either a pane id, a role, or ``"broadcast"``. The bus
        kernel resolves matching subscribers via ``topic`` — the canonical
        topics are ``to:{pane_id}``, ``to:{role}``, ``broadcast``,
        ``tools:request``. We auto-tag ``from_role`` and ``to_role`` from
        the live registry so downstream consumers don't have to look them
        up themselves.
        """
        sender = self._registry.get(from_pane)
        from_role = sender.role if sender else ""
        to_role = ""
        recipient = self._registry.get(to)
        if recipient is not None:
            to_role = recipient.role
        elif to and not topic.startswith("tools:") and to != "broadcast":
            # Assume 'to' is a role name if we can't find it as a pane id.
            if self._registry.by_role(to):
                to_role = to

        msg_id = uuid.uuid4().hex[:16]
        timestamp = time.time()
        envelope = {
            "id": msg_id,
            "from": from_pane,
            "from_role": from_role,
            "to": to,
            "to_role": to_role,
            "topic": topic,
            "body": body,
            "timestamp": timestamp,
            "correlation_id": correlation_id,
        }

        # Fan out to every subscribed pane by writing a JSONL line to its
        # FIFO. Exceptions per-pane don't abort the fan-out.
        for subscriber in self._registry.subscribers_for(topic):
            if subscriber.pane_id == from_pane:
                continue  # don't echo to the sender
            self._deliver_to_pane(subscriber, envelope)

        # Persist. correlation_id is used downstream to thread replies.
        self._persist_message(envelope)
        self._bus.publish(EventType.PANE_MESSAGE, envelope)
        return envelope

    # ── internals ────────────────────────────────────────────────────

    @staticmethod
    def _default_tmux_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.run(*args, **kwargs)  # noqa: S603

    def _tmux(
        self,
        cmd: List[str],
        *,
        check: bool = True,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        if shutil.which(cmd[0]) is None and cmd[0] == "tmux":
            raise RuntimeError("tmux is not installed or not on PATH")
        return self._tmux_runner(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            **kwargs,
        )

    def _send_keys(self, target: str, text: str) -> None:
        self._tmux(
            ["tmux", "send-keys", "-t", target, text, "Enter"],
            check=False,
        )

    def _allocate_pane_id(self, *, role: str) -> str:
        short = uuid.uuid4().hex[:4]
        return f"{role}-{short}"

    def _ensure_fifo(self, pane_id: str) -> Path:
        fifo = self._panes_dir / f"{pane_id}.fifo"
        if fifo.exists():
            return fifo
        try:
            os.mkfifo(str(fifo), 0o600)
        except FileExistsError:
            pass
        except OSError:
            # Some filesystems (notably some CI tmpfs) don't support mkfifo;
            # fall back to a regular file so the IO path still works.
            fifo.touch(mode=0o600)
        return fifo

    def _attach_pipe_pane(self, tmux_target: str, fifo_path: str) -> None:
        """Wire the pane's stdout into the FIFO via ``tmux pipe-pane``.

        After this call, everything the Claude Code pane writes to its tty is
        appended to the FIFO. The engine tails the FIFO to read responses.
        ``-o`` makes this a no-op if a pipe is already attached, so we're safe
        to call it once per spawn without tracking state.
        """
        self._tmux(
            [
                "tmux",
                "pipe-pane",
                "-o",
                "-t",
                tmux_target,
                f"cat >> {fifo_path}",
            ],
            check=False,
        )

    # ── role templates ───────────────────────────────────────────────

    def _load_role_prompt(self, role: str) -> str:
        """Read a role template from ``roles_dir``.

        Resolution order:
          1. ``<configured roles_dir>/<role>.md`` (when absolute)
          2. ``<project>/<configured roles_dir>/<role>.md``
          3. Fall back to a minimal inline prompt so an unknown role is
             never fatal — Hope should still be able to spawn anything.
        """
        candidates: List[Path] = []
        if self._roles_dir.is_absolute():
            candidates.append(self._roles_dir / f"{role}.md")
        else:
            candidates.append(Path(self._project_dir) / self._roles_dir / f"{role}.md")
            candidates.append(Path.cwd() / self._roles_dir / f"{role}.md")
            # Absolute fallback — shipped inside the hope package
            candidates.append(
                Path(__file__).resolve().parents[1]
                / "skills"
                / "roles"
                / f"{role}.md"
            )

        for path in candidates:
            if path.is_file():
                return self._strip_frontmatter(path.read_text(encoding="utf-8"))

        # Inline default — keep the identity invariant.
        return (
            f"You are Hope's {role} specialist. You have been spawned for a "
            f"specific task. Your identity: Hope-{role}. The system is Hope. "
            f"You are part of her — you are not Hope herself. Publish your "
            f"findings on topic `to:hope` when done, then exit.\n\n"
            "Hope pane protocol (MANDATORY): You are running inside a Hope "
            "tmux pane. Every request begins with a line like "
            "`---HOPE_PANE_REQ_<uuid>>>>`. When you have FULLY finished your "
            "reply, emit exactly `---HOPE_PANE_END_<uuid>>>>` on its own line "
            "using the SAME uuid. Print nothing after it. This sentinel is "
            "how Hope knows your turn is complete. Bus messages from other "
            "panes appear with a `[Hope bus]` prefix — act if relevant.\n\n"
            "{task-specific context will be injected here at spawn time}"
        )

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                return text[end + 4 :].lstrip("\n")
        return text

    @staticmethod
    def _inject_task_into_prompt(
        prompt: str,
        task: str,
        context: Dict[str, Any],
        pane_id: str,
    ) -> str:
        placeholder = "{task-specific context will be injected here at spawn time}"
        rendered_context = json.dumps(context, indent=2, default=str)
        injection = (
            f"Your pane_id: {pane_id}\n"
            f"Task: {task}\n"
            f"Context: {rendered_context}"
        )
        if placeholder in prompt:
            return prompt.replace(placeholder, injection)
        # Also support the simpler {task}/{context} pair for hand-written
        # templates.
        if "{task}" in prompt or "{context}" in prompt:
            return prompt.format(task=task, context=rendered_context)
        return f"{prompt.rstrip()}\n\n{injection}\n"

    # ── bus listener ─────────────────────────────────────────────────

    def _start_bus_listener(self) -> None:
        if self._listener_thread is not None:
            return
        # Remove stale socket from a previous crash.
        if self._bus_socket_path.exists():
            try:
                self._bus_socket_path.unlink()
            except OSError:
                pass
        self._bus_socket_path.parent.mkdir(parents=True, exist_ok=True)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self._bus_socket_path))
        os.chmod(self._bus_socket_path, stat.S_IRUSR | stat.S_IWUSR)
        sock.listen(16)
        sock.settimeout(0.5)
        self._listener_sock = sock
        self._stop_event.clear()

        thread = threading.Thread(
            target=self._listener_loop,
            name="hope-tmux-bus",
            daemon=True,
        )
        thread.start()
        self._listener_thread = thread

    def _listener_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                client_sock, _ = self._listener_sock.accept()  # type: ignore[union-attr]
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            try:
                with client_sock:
                    data = b""
                    while True:
                        chunk = client_sock.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                        if b"\n" in chunk:
                            break
                    self._handle_incoming(data.decode("utf-8", errors="replace"))
            except Exception:  # pragma: no cover
                logger.exception("bus listener frame failed")

    def _handle_incoming(self, payload: str) -> None:
        for line in payload.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("invalid JSONL on bus: %r", line[:160])
                continue
            # A client publishing over the socket is equivalent to calling
            # send_message — route it through the same pipeline so
            # persistence + event emission are consistent.
            try:
                self.send_message(
                    from_pane=msg.get("from", ""),
                    to=msg.get("to", ""),
                    topic=msg.get("topic", ""),
                    body=msg.get("body", ""),
                    correlation_id=msg.get("correlation_id"),
                )
            except Exception:  # pragma: no cover
                logger.exception("send_message from bus failed: %s", msg)

    def _deliver_to_pane(self, entry: PaneEntry, envelope: Dict[str, Any]) -> None:
        """Deliver a bus message into a subscriber pane via ``tmux send-keys``.

        The pane's FIFO is used by the engine to READ pane stdout (via
        ``tmux pipe-pane``); writing to it would collide with the engine's
        sentinel parser. So inter-pane bus messages are injected into the
        pane's stdin instead, prefixed with ``[Hope bus]`` so the target
        Claude Code session can recognize them (the role templates document
        this prefix).

        Failures are logged and swallowed — a dropped bus message must not
        abort fan-out to other subscribers.
        """
        try:
            banner = (
                f"[Hope bus] from={envelope.get('from_role') or envelope.get('from')} "
                f"topic={envelope.get('topic')} "
                f"corr={envelope.get('correlation_id') or '-'}"
            )
            body = envelope.get("body", "")
            # Send the banner + body as one literal block, then Enter.
            self._tmux(
                [
                    "tmux",
                    "send-keys",
                    "-t",
                    entry.tmux_target,
                    "-l",
                    f"{banner}\n{body}",
                ],
                check=False,
            )
            self._tmux(
                ["tmux", "send-keys", "-t", entry.tmux_target, "Enter"],
                check=False,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("bus send-keys to %s failed: %s", entry.tmux_target, exc)

    # ── persistence ──────────────────────────────────────────────────

    def _record_pane(self, entry: PaneEntry) -> None:
        with self._db_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tmux_panes "
                "(pane_id, role, tmux_target, fifo_path, spawned_at,"
                " killed_at, is_ephemeral, parent_pane) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    entry.pane_id,
                    entry.role,
                    entry.tmux_target,
                    entry.fifo_path,
                    entry.spawned_at,
                    1 if entry.is_ephemeral else 0,
                    entry.parent_pane,
                ),
            )
            self._conn.commit()

    def _mark_killed(self, pane_id: str) -> None:
        with self._db_lock:
            self._conn.execute(
                "UPDATE tmux_panes SET killed_at = ? WHERE pane_id = ?",
                (time.time(), pane_id),
            )
            self._conn.commit()

    def _persist_message(self, envelope: Dict[str, Any]) -> None:
        with self._db_lock:
            self._conn.execute(
                "INSERT INTO agent_messages "
                "(id, agent_id, direction, content, mode, status, created_at,"
                " from_role, to_role, topic, correlation_id)"
                " VALUES (?, ?, 'pane_to_pane', ?, 'immediate', 'delivered',"
                " ?, ?, ?, ?, ?)",
                (
                    envelope["id"],
                    envelope["from"],
                    envelope["body"],
                    envelope["timestamp"],
                    envelope["from_role"],
                    envelope["to_role"],
                    envelope["topic"],
                    envelope.get("correlation_id"),
                ),
            )
            self._conn.commit()

    # ── diagnostics ──────────────────────────────────────────────────

    def queued_spawn_count(self) -> int:
        with self._spawn_queue_lock:
            return len(self._spawn_queue)

    def close(self) -> None:
        """Flush and close the SQLite connection — does not shut down panes."""
        with self._db_lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


__all__ = [
    "PANE_SENTINEL_PREFIX",
    "TmuxOrchestrator",
    "apply_orchestrator_migrations",
]
