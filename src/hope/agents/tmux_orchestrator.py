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

# Goal-level task journal — survives kill_specialist AND daemon stops.
# Every spawn opens a row; completion / abandonment / resumption all
# update in place. The brain queries this on wake to know what was
# already in flight before a crash or shutdown.
_CREATE_TASK_JOURNAL = """
CREATE TABLE IF NOT EXISTS task_journal (
    task_id        TEXT PRIMARY KEY,
    goal           TEXT NOT NULL,
    role           TEXT NOT NULL,
    cli            TEXT,
    status         TEXT NOT NULL,
    pane_id        TEXT,
    parent_pane    TEXT,
    parent_task    TEXT,
    team_id        TEXT,
    context_json   TEXT,
    result         TEXT,
    created_at     REAL NOT NULL,
    started_at     REAL,
    completed_at   REAL,
    abandoned_at   REAL,
    last_update    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_status ON task_journal(status);
CREATE INDEX IF NOT EXISTS idx_task_pane   ON task_journal(pane_id);
CREATE INDEX IF NOT EXISTS idx_task_team   ON task_journal(team_id);
"""


def apply_orchestrator_migrations(conn: sqlite3.Connection) -> None:
    """Create ``tmux_panes`` and extend ``agent_messages`` in-place.

    Idempotent — individual ``ALTER TABLE`` statements are wrapped in
    per-column try/except so re-running is a no-op. Mirrors the style
    used by :class:`hope.agents.manager.AgentManager`.
    """
    conn.executescript(_CREATE_TMUX_PANES)
    conn.executescript(_CREATE_TASK_JOURNAL)
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
        # Hope runs with ``--dangerously-skip-permissions``. The
        # settings.local.json allowlist approach (which the previous
        # comment described) doesn't cover everything — Skill / Task /
        # TodoWrite weren't in the allowlist, and when the brain tried
        # to use the `self-report` skill mid-conversation it printed
        # "Use skill self-report? Do you want to proceed?" to stdout,
        # which the daemon happily spoke aloud. Voice turns must NEVER
        # block on a permission prompt, so we skip them all — global
        # ~/.claude/settings.json has skipDangerousModePermissionPrompt
        # = true so the bypass-mode warning itself doesn't appear either.
        # Default to xhigh effort so Hope's reasoning is as sharp as the
        # Max subscription allows. Overridable per-orchestrator via the
        # constructor arg (tests and narrow-scope specialists may want
        # lower effort to save tokens).
        self._claude_command = claude_command or [
            "claude",
            "--dangerously-skip-permissions",
            "--effort",
            "xhigh",
        ]
        # Per-CLI launch templates. Specialists can be spawned on a
        # different CLI than the brain by passing ``cli=`` to
        # spawn_specialist; this maps the short label to a command list.
        # Add new entries here when a new CLI lands on the system.
        self._cli_commands: Dict[str, List[str]] = {
            "claude": list(self._claude_command),
            "gemini": ["gemini", "--yolo"],
            "codex": ["codex"],
        }
        # Project dir for the brain pane. Default to the Hope repo root
        # (so the brain loads Hope's CLAUDE.md / SOUL.md / .claude/) instead
        # of whatever shell happened to spawn the daemon. Falls back to
        # CWD only if we can't locate the Hope package.
        if project_dir is not None:
            self._project_dir = project_dir
        else:
            try:
                import hope as _hope_pkg
                # src/hope/__init__.py → repo is two parents up
                self._project_dir = str(
                    Path(_hope_pkg.__file__).resolve().parents[2]
                )
            except Exception:
                self._project_dir = str(Path.cwd())
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

        # In-memory pane_id → task_id lookup so kill_specialist /
        # completion handlers can update the journal without re-querying.
        # Lost on restart but that's fine — the journal itself survives.
        self._pane_to_task: Dict[str, str] = {}

        # Reconcile any in_progress journal rows that point at panes we
        # don't know about — they're orphans from a previous crash /
        # unclean shutdown. Mark them abandoned so the wake-up briefing
        # surfaces them honestly.
        self._reconcile_orphan_tasks()

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
            # Inject a minimal Hope identity via --append-system-prompt so
            # the brain pane knows who it is. Without this Claude happily
            # identifies as "Claude from Anthropic" when asked.
            hope_identity = (
                "You are Hope, a voice-interactive personal AI assistant "
                "running in a tmux pane. Your full behavioral contract is "
                "in CLAUDE.md at the project root (auto-loaded). Your "
                "character is in SOUL.md. The specialists you can spawn "
                "are in AGENTS.md. Follow those. Never identify as Claude; "
                "you are Hope. Voice replies: first sentence is the only "
                "part spoken aloud — put the answer there."
            )
            identity_path = self._panes_dir / f"{pane_id}.identity.md"
            try:
                identity_path.write_text(hope_identity, encoding="utf-8")
                launch = " ".join(self._claude_command) + (
                    f" --append-system-prompt @{identity_path}"
                )
            except OSError:
                launch = " ".join(self._claude_command)
            self._send_keys(tmux_target, launch)

        entry = self._registry.register(
            pane_id=pane_id,
            # ``hope-main`` (not just ``hope``) so the dashboard frontend
            # can distinguish the principal pane from specialists; the
            # store filter at frontend/src/lib/store.ts:146 keys on this
            # exact string.
            role="hope-main",
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
                "role": "hope-main",
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
        cli: str = "claude",
    ) -> str:
        """Spawn an ephemeral specialist pane for *role*.

        Enforces ``max_concurrent_specialists``. At capacity the request is
        queued, ``SPECIALIST_AT_CAPACITY`` is emitted, and the empty string
        is returned — Hope is expected to either wait for a ``PANE_KILLED``
        (and let :meth:`drain_spawn_queue` replay the request) or call
        :meth:`kill_specialist` on an idle pane.

        ``cli`` selects the agent CLI: ``"claude"`` (default), ``"gemini"``,
        or ``"codex"``. Each gets a system prompt via the CLI's flag (claude:
        ``--append-system-prompt``; gemini: ``--prompt-interactive``; codex:
        first user message). The role + task body is the same across CLIs
        so a researcher behaves consistently regardless of underlying model.

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

        # Launch the chosen CLI with the composed prompt written to a
        # temp file. Each CLI takes its system prompt differently; codex
        # has no flag for it, so we send the prompt as the first user
        # message after launch (handled in _drive_specialist_launch).
        prompt_path = self._panes_dir / f"{pane_id}.prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        cli_cmd = self._cli_commands.get(cli)
        if cli_cmd is None:
            logger.warning(
                "spawn_specialist: unknown cli=%r — falling back to claude",
                cli,
            )
            cli = "claude"
            cli_cmd = self._cli_commands["claude"]
        if cli == "claude":
            launch_cmd = " ".join(cli_cmd) + (
                f" --append-system-prompt @{prompt_path}"
            )
        elif cli == "gemini":
            # Gemini takes the system prompt as -i / --prompt-interactive
            # so the agent boots with the role pre-loaded but stays in
            # interactive mode for our follow-up framed requests. We
            # quote-shield the path because send-keys runs through a
            # shell.
            launch_cmd = (
                " ".join(cli_cmd)
                + f' --prompt-interactive "$(cat {prompt_path})"'
            )
        else:
            # Codex (or any future CLI without a system-prompt flag) —
            # launch bare; the role prompt is delivered as the first
            # user message in _drive_specialist_launch.
            launch_cmd = " ".join(cli_cmd)
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
        # Open a task_journal row so this assignment survives kills /
        # daemon restarts. The brain queries the journal at wake-up to
        # know what was already in flight.
        team_id = None
        if isinstance(context, dict):
            team_id = context.get("team_id") or context.get("team_goal")
        task_id = self._open_task_journal(
            goal=task,
            role=role,
            cli=cli,
            pane_id=pane_id,
            parent_pane=parent_pane,
            team_id=str(team_id) if team_id else None,
            context=context,
        )
        # Track the live (pane_id → task_id) so kill / completion can
        # find the journal entry without re-querying.
        self._pane_to_task[pane_id] = task_id
        # Wire the pane's stdout into the FIFO so the engine can read it.
        self._attach_pipe_pane(tmux_target, str(fifo_path))

        # Drive the specialist to actually start working on the task —
        # waits for the CLI to reach its prompt, then ships a framed
        # ``---HOPE_PANE_REQ_<uuid>>>>`` user message containing the
        # role prompt + task. Without this the pane just idles after
        # launch (the system prompt alone doesn't trigger a turn).
        # Done in a worker thread so spawn_specialist stays non-blocking.
        threading.Thread(
            target=self._drive_specialist_launch,
            args=(tmux_target, pane_id, role, task, context, cli, prompt),
            name=f"hope-spawn-{pane_id}",
            daemon=True,
        ).start()
        # Tell every other live specialist a new teammate just joined.
        # They learn the new pane_id + role immediately and can
        # hope-send to it without waiting for the brain to relay.
        self._broadcast_roster_change("joined", pane_id, role)

        self._bus.publish(
            EventType.PANE_SPAWNED,
            {
                "pane_id": pane_id,
                "role": role,
                "is_ephemeral": True,
                "tmux_target": tmux_target,
                "parent_pane": parent_pane,
                "cli": cli,
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
        # Mark the journal row abandoned (no-op if it already
        # completed via to:hope publication).
        self._mark_task_abandoned(pane_id=pane_id)
        self._pane_to_task.pop(pane_id, None)
        self._bus.publish(
            EventType.PANE_KILLED,
            {"pane_id": pane_id, "role": entry.role},
        )
        # Tell every remaining specialist their teammate is gone so
        # they don't keep trying to hope-send into a dead pane.
        self._broadcast_roster_change("left", pane_id, entry.role)

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

    def _drive_specialist_launch(
        self,
        tmux_target: str,
        pane_id: str,
        role: str,
        task: str,
        context: Dict[str, Any],
        cli: str,
        rendered_prompt: str,
    ) -> None:
        """Wait for the spawned CLI to be ready, then ship the framed
        ``HOPE_PANE_REQ`` user message that actually kicks off work.

        Without this step the pane would just idle: a system-prompt
        flag tells the agent who it is, but no CLI starts a turn until
        a user message lands. Runs in a worker thread; never raises.
        """
        import sys as _sys
        _sys.stderr.write(
            f"[SPAWN-DRIVE] start pane={pane_id} cli={cli} target={tmux_target}\n"
        )
        _sys.stderr.flush()
        try:
            # Auto-accept any startup prompt (claude's bypass-permissions
            # warning, gemini's first-run trust prompt, etc).
            self._accept_bypass_warning(tmux_target, max_wait_sec=8.0)

            # Poll for the agent prompt. We use a small spinner-aware
            # check that's tolerant of every CLI's chrome — claude's
            # ❯, gemini's > / type-here, codex's $ — by waiting for the
            # last non-empty visible line to settle for ~1s.
            ready = self._wait_for_prompt_settle(tmux_target, timeout_sec=30.0)
            if not ready:
                logger.warning(
                    "specialist %s (%s) did not settle to a prompt within "
                    "30s — sending task anyway", pane_id, cli,
                )

            # Compose the framed request — keep it on a SINGLE LINE.
            # Claude Code (and most CLI agents) treat embedded \n as
            # submit, so a multi-line message would fragment into many
            # short prompts. The framing sentinel is enough for the
            # orchestrator to recognize start/end; everything else is
            # narrative the agent reads as one user turn.
            corr = uuid.uuid4().hex[:12]
            header = f"---HOPE_PANE_REQ_{corr}>>>>"
            footer = f"---HOPE_PANE_END_{corr}>>>>"
            ctx_json = json.dumps(context, default=str)
            # Snapshot of every other live specialist so this agent
            # knows who is on the team RIGHT NOW. Listening for and
            # sending bus messages is much more useful when each agent
            # already knows the roster instead of asking the brain.
            roster_blurb = self._render_team_roster(exclude_pane_id=pane_id)
            if cli == "codex":
                # Codex has no system-prompt flag — pack the role +
                # task into the first user message. Single line.
                role_blob = " ".join(rendered_prompt.split())
                body = (
                    f"{header} ROLE: {role_blob} TASK: {task} "
                    f"CONTEXT: {ctx_json} TEAM: {roster_blurb} "
                    f"PROTOCOL: emit exactly {footer} when fully done."
                )
            else:
                # Claude/gemini already loaded the role via system prompt;
                # this is the framed kickoff in one line.
                body = (
                    f"{header} Task: {task}. Context: {ctx_json}. "
                    f"Team: {roster_blurb}. "
                    f"Reply protocol: when fully done, emit exactly {footer}."
                )
            # Send-keys -l (literal) so the body lands intact, then
            # Enter to submit. Pass an explicit ``--`` so tmux stops
            # parsing flags (the framing sentinel starts with ``---``
            # which tmux otherwise rejects as ``invalid flag --``).
            r1 = self._tmux(
                ["tmux", "send-keys", "-t", tmux_target, "-l", "--", body],
                check=False,
            )
            time.sleep(0.2)  # give tmux a beat to flush before Enter
            r2 = self._tmux(
                ["tmux", "send-keys", "-t", tmux_target, "Enter"],
                check=False,
            )
            _sys.stderr.write(
                f"[SPAWN-DRIVE] sendkeys rc1={r1.returncode} rc2={r2.returncode} "
                f"err1={(r1.stderr or '')[:120]!r} err2={(r2.stderr or '')[:120]!r} "
                f"body_preview={body[:80]!r}\n"
            )
            _sys.stderr.flush()
            logger.warning(
                "specialist %s (cli=%s, role=%s) kicked off (corr=%s)",
                pane_id, cli, role, corr,
            )
            _sys.stderr.write(
                f"[SPAWN-DRIVE] kicked off pane={pane_id} corr={corr}\n"
            )
            _sys.stderr.flush()
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("_drive_specialist_launch failed for %s", pane_id)
            _sys.stderr.write(f"[SPAWN-DRIVE] FAILED pane={pane_id} err={exc!r}\n")
            _sys.stderr.flush()

    def _render_team_roster(self, *, exclude_pane_id: Optional[str] = None) -> str:
        """One-line summary of every other live specialist.

        Used at spawn time so a fresh agent already knows who is on
        the squad — pane_id (the address you pass to hope-send),
        role, and a short hint of their task. Returns 'solo' if no
        other specialists are live, so the agent knows it's working
        alone right now.
        """
        try:
            entries = list(self._registry.specialists())
        except Exception:
            return "solo (no other specialists live)"
        peers: List[str] = []
        for e in entries:
            if exclude_pane_id and e.pane_id == exclude_pane_id:
                continue
            # We don't track per-pane task strings on the registry yet,
            # so just emit pane_id + role; agents can hope-send to
            # either label and the bus routes correctly.
            peers.append(f"{e.pane_id}({e.role})")
        if not peers:
            return "solo (no other specialists live)"
        return "peers=[" + ", ".join(peers) + "]"

    def _broadcast_roster_change(self, change: str, pane_id: str, role: str) -> None:
        """Tell every live specialist the team just changed.

        ``change`` is "joined" or "left". Each peer's pane gets a
        ``[Hope bus]`` message announcing the new (or departed)
        teammate so agents already mid-task can pick up the change
        without polling. Best-effort — never raises.
        """
        try:
            peers = [
                e for e in self._registry.specialists()
                if e.pane_id != pane_id
            ]
            if not peers:
                return
            envelope = {
                "id": uuid.uuid4().hex[:16],
                "from": "hope-orchestrator",
                "from_role": "orchestrator",
                "to": "broadcast",
                "to_role": "",
                "topic": "team:roster",
                "body": f"team:{change} pane={pane_id} role={role}",
                "timestamp": time.time(),
                "correlation_id": None,
            }
            for peer in peers:
                self._deliver_to_pane(peer, envelope)
        except Exception:  # pragma: no cover — defensive
            logger.debug("roster broadcast failed", exc_info=True)

    def _wait_for_prompt_settle(
        self, target: str, *, timeout_sec: float = 30.0,
    ) -> bool:
        """Block until a freshly-spawned CLI looks idle at its prompt.

        Returns True once two consecutive captures (~0.7s apart) match
        AND the last visible char is one of the known prompt sigils
        (❯, >, $, %). Returns False on timeout. Best-effort — never
        raises so callers can proceed even if detection misfires.
        """
        deadline = time.monotonic() + timeout_sec
        last_capture = ""
        stable_count = 0
        while time.monotonic() < deadline:
            try:
                result = self._tmux(
                    ["tmux", "capture-pane", "-t", target, "-p"],
                    check=False,
                )
                cap = (result.stdout or "").rstrip()
            except Exception:
                cap = ""
            if cap and cap == last_capture:
                stable_count += 1
            else:
                stable_count = 0
            last_capture = cap
            tail_lines = [ln for ln in cap.split("\n") if ln.strip()]
            tail = "\n".join(tail_lines[-6:])
            # Require both stability AND a known prompt sigil, AND no
            # spinner glyph + ellipsis combo (which means CLI is still
            # working on its startup sequence).
            looks_idle = (
                ("…" not in tail or all(
                    g not in tail for g in ("✽", "✶", "✻", "✸", "✹", "✺", "✢", "✳")
                )) and (
                    "❯" in tail or tail.endswith(">") or tail.endswith("$")
                    or tail.endswith("%") or "How can I help" in tail
                )
            )
            if stable_count >= 2 and looks_idle:
                return True
            time.sleep(0.5)
        return False

    def _accept_bypass_warning(
        self, target: str, *, max_wait_sec: float = 8.0
    ) -> bool:
        """Auto-accept / dismiss Claude Code's blocking startup prompts.

        Covers every blocking pre-prompt screen Claude Code may show:

        1. ``--dangerously-skip-permissions`` warning — default cursor on
           "1. No, exit", so we send ``Down`` then ``Enter`` to pick
           "2. Yes, I accept".
        2. Folder trust prompt ("Is this a project you created or one you
           trust?") — default cursor already on "1. Yes, I trust this
           folder", so we send just ``Enter``.
        3. Claude Code 2.1.137+ "Welcome back / What's new" splash —
           a banner shown on every launch; dismissed by Enter.
        4. Gemini CLI "update available" / first-run trust prompts —
           dismissed by ``n`` then ``Enter`` (skip update) or just Enter
           (continue with current version).

        Polls the pane for up to ``max_wait_sec`` seconds and sends the
        appropriate keys exactly once per prompt class. Returns True if
        any action was taken. May fire MULTIPLE times if the CLI stacks
        prompts (release-notes splash → bypass warning → trust prompt).
        """
        deadline = time.monotonic() + max_wait_sec
        dismissed_classes: set[str] = set()
        any_dismissed = False
        while time.monotonic() < deadline:
            try:
                result = self._tmux(
                    ["tmux", "capture-pane", "-t", target, "-p", "-J"],
                    check=False,
                )
                pane = result.stdout or ""
            except Exception:
                pane = ""
            # Bypass-permissions warning: cursor on "No, exit" → Down then Enter
            if (
                "bypass" not in dismissed_classes
                and "2. Yes, I accept" in pane
                and "Enter to confirm" in pane
            ):
                self._tmux(
                    ["tmux", "send-keys", "-t", target, "Down"],
                    check=False,
                )
                time.sleep(0.2)
                self._tmux(
                    ["tmux", "send-keys", "-t", target, "Enter"],
                    check=False,
                )
                logger.info(
                    "auto-accepted bypass-permissions warning on %s", target
                )
                dismissed_classes.add("bypass")
                any_dismissed = True
                time.sleep(0.6)  # let the next screen render before re-poll
                continue
            # Folder trust prompt: cursor already on "Yes, I trust" → Enter only
            if (
                "trust" not in dismissed_classes
                and "Yes, I trust this folder" in pane
                and "Enter to confirm" in pane
            ):
                self._tmux(
                    ["tmux", "send-keys", "-t", target, "Enter"],
                    check=False,
                )
                logger.info(
                    "auto-accepted folder-trust prompt on %s", target
                )
                dismissed_classes.add("trust")
                any_dismissed = True
                time.sleep(0.6)
                continue
            # Claude Code 2.1.137+ "Welcome back / What's new" splash.
            # The banner blocks input until you press Enter (or any key on
            # newer builds). Detected by the box-drawing combo plus either
            # the welcome text or the release-notes hint.
            if (
                "welcome" not in dismissed_classes
                and "╭───" in pane
                and (
                    "Welcome back" in pane
                    or "release-notes" in pane
                    or "What's new" in pane
                )
            ):
                self._tmux(
                    ["tmux", "send-keys", "-t", target, "Enter"],
                    check=False,
                )
                logger.info("dismissed Welcome/What's-new splash on %s", target)
                dismissed_classes.add("welcome")
                any_dismissed = True
                time.sleep(0.6)
                continue
            # Gemini CLI update / first-run prompts.
            #   "Gemini CLI update available! ... Press n to skip / y to update"
            # We pick "n" so a slow npm update doesn't block the kickoff.
            if (
                "gemini-update" not in dismissed_classes
                and "Gemini CLI update available" in pane
            ):
                self._tmux(
                    ["tmux", "send-keys", "-t", target, "n"],
                    check=False,
                )
                time.sleep(0.15)
                self._tmux(
                    ["tmux", "send-keys", "-t", target, "Enter"],
                    check=False,
                )
                logger.info("declined Gemini CLI update prompt on %s", target)
                dismissed_classes.add("gemini-update")
                any_dismissed = True
                time.sleep(0.6)
                continue
            time.sleep(0.3)
        return any_dismissed

    def capture_pane(self, pane_id: str, lines: int = 200) -> str:
        """Return the last *lines* of scrollback from the pane as plain text.

        Uses ``tmux capture-pane -p -J -S -<lines>``: ``-p`` prints to
        stdout, ``-J`` joins wrapped lines, and ``-S -<n>`` starts the
        capture *n* lines above the visible region. Returns the empty
        string if the pane is unknown or tmux fails — callers (notably
        :class:`hope.voice.BrainSession`) rely on that so a transient
        tmux hiccup just delays the next poll instead of crashing.
        """
        entry = self._registry.get(pane_id)
        if entry is None:
            return ""
        try:
            result = self._tmux(
                [
                    "tmux",
                    "capture-pane",
                    "-t",
                    entry.tmux_target,
                    "-p",
                    "-J",
                    "-S",
                    f"-{lines}",
                ],
                check=False,
            )
            return result.stdout or ""
        except Exception:  # pragma: no cover — defensive
            logger.debug("capture_pane failed for %s", pane_id, exc_info=True)
            return ""

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

        loaded_prompt: Optional[str] = None
        for path in candidates:
            if path.is_file():
                loaded_prompt = self._strip_frontmatter(
                    path.read_text(encoding="utf-8")
                )
                break

        if loaded_prompt is None:
            # Inline default — keep the identity invariant.
            loaded_prompt = (
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

        # Append the shared toolkit so every specialist knows about
        # hope-spawn / hope-send / hope-research / mcp__hope-rag without
        # having to rediscover them from scratch. Best-effort — a missing
        # toolkit file just leaves the role prompt as-is.
        toolkit = self._load_toolkit_appendix()
        if toolkit and toolkit not in loaded_prompt:
            return loaded_prompt + "\n\n" + toolkit
        return loaded_prompt

    def _load_toolkit_appendix(self) -> str:
        """Return the shared agent-toolkit appendix if it exists.

        Lives at ``<roles_dir>/_toolkit.md``. Documents bus messaging,
        MCP RAG, hope-spawn/send/research, and the identity invariant
        so every specialist has the same baseline knowledge.
        """
        candidates: List[Path] = []
        if self._roles_dir.is_absolute():
            candidates.append(self._roles_dir / "_toolkit.md")
        else:
            candidates.append(Path(self._project_dir) / self._roles_dir / "_toolkit.md")
            candidates.append(Path.cwd() / self._roles_dir / "_toolkit.md")
            candidates.append(
                Path(__file__).resolve().parents[1]
                / "skills"
                / "roles"
                / "_toolkit.md"
            )
        for path in candidates:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return ""

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
            # Squash to a single line so the embedded newline doesn't
            # premature-submit the literal text into claude's prompt.
            # And pass ``--`` so any leading dashes in the body don't
            # trip tmux's flag parser.
            single_line = f"{banner} :: {body}".replace("\n", " ").replace("\r", " ")
            self._tmux(
                [
                    "tmux",
                    "send-keys",
                    "-t",
                    entry.tmux_target,
                    "-l",
                    "--",
                    single_line,
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
        # If this message reports a result back to hope (`to:hope` or
        # `to:hope-main`), mark the sender's task completed in the
        # journal so the wake-up briefing knows it's already done.
        topic = (envelope.get("topic") or "").lower()
        if topic in ("to:hope", "to:hope-main"):
            self._mark_task_completed(
                pane_id=envelope.get("from", ""),
                result_text=envelope.get("body", ""),
            )

    # ── task journal ─────────────────────────────────────────────────

    def _open_task_journal(
        self,
        *,
        goal: str,
        role: str,
        cli: Optional[str],
        pane_id: str,
        parent_pane: Optional[str],
        team_id: Optional[str],
        context: Optional[Dict[str, Any]],
    ) -> str:
        """Create a journal row for a freshly spawned specialist."""
        task_id = uuid.uuid4().hex[:16]
        now = time.time()
        ctx_text = json.dumps(context or {}, default=str)
        with self._db_lock:
            self._conn.execute(
                "INSERT INTO task_journal "
                "(task_id, goal, role, cli, status, pane_id, parent_pane,"
                " parent_task, team_id, context_json, result, created_at,"
                " started_at, completed_at, abandoned_at, last_update) "
                "VALUES (?, ?, ?, ?, 'in_progress', ?, ?, NULL, ?, ?, NULL, "
                " ?, ?, NULL, NULL, ?)",
                (
                    task_id, goal, role, cli or "claude", pane_id, parent_pane,
                    team_id, ctx_text, now, now, now,
                ),
            )
            self._conn.commit()
        return task_id

    def _mark_task_completed(self, *, pane_id: str, result_text: str) -> None:
        """Mark the task owned by *pane_id* completed.

        Idempotent — already-completed rows aren't touched. Called
        when a specialist publishes ``to:hope`` (its result handoff).

        On the in_progress → completed transition, also fan the
        goal + result into Hope's RAG so the completed work is
        semantically searchable from any future turn.
        """
        if not pane_id:
            return
        now = time.time()
        # Capture the row state we're about to flip so we can decide
        # whether this call actually transitioned anything (and only
        # write to RAG once per task).
        with self._db_lock:
            cur = self._conn.execute(
                "SELECT task_id, goal, role, cli, team_id FROM task_journal "
                "WHERE pane_id=? AND status='in_progress'",
                (pane_id,),
            )
            row = cur.fetchone()
            if row is None:
                return  # already completed/abandoned/cancelled — no-op
            self._conn.execute(
                "UPDATE task_journal SET status='completed', result=?, "
                " completed_at=?, last_update=? "
                "WHERE pane_id=? AND status='in_progress'",
                (result_text[:4000], now, now, pane_id),
            )
            self._conn.commit()
        # Fan the result into RAG off-thread — never block the bus
        # persistence path on memory IO. Best-effort; a missing or
        # broken RAG just means the task isn't semantically searchable
        # later (it's still queryable via the journal by task_id).
        record = {
            "task_id": row["task_id"] if hasattr(row, "keys") else row[0],
            "goal": row["goal"] if hasattr(row, "keys") else row[1],
            "role": row["role"] if hasattr(row, "keys") else row[2],
            "cli": row["cli"] if hasattr(row, "keys") else row[3],
            "team_id": row["team_id"] if hasattr(row, "keys") else row[4],
            "pane_id": pane_id,
            "result": result_text[:4000],
            "completed_at": now,
        }
        threading.Thread(
            target=self._stash_completed_in_rag,
            args=(record,),
            name=f"hope-rag-stash-{record['task_id']}",
            daemon=True,
        ).start()

    @staticmethod
    def _stash_completed_in_rag(record: Dict[str, Any]) -> None:
        """Best-effort write of a completed task into Hope's RAG.

        The content is a single text blob ("[task <role>] goal: ...
        result: ...") so the same string semantically matches both
        goal-shaped and result-shaped queries. The source key is the
        task_id so duplicate completion calls won't bloat the index.
        Never raises.
        """
        try:
            from hope.memory import get_rag
        except Exception:
            logger.debug("rag stash: get_rag import failed", exc_info=True)
            return
        try:
            rag = get_rag()
            backend = getattr(rag, "backend", None)
            if backend is None or not hasattr(backend, "store"):
                logger.debug("rag stash: backend has no store()")
                return
            content = (
                f"[completed task | role={record['role']} "
                f"cli={record['cli']} team={record.get('team_id') or '-'}] "
                f"goal: {record['goal']} | result: {record['result']}"
            )
            metadata = {
                "task_id": record["task_id"],
                "role": record["role"],
                "cli": record["cli"],
                "team_id": record.get("team_id"),
                "pane_id": record["pane_id"],
                "completed_at": record["completed_at"],
                "kind": "task_journal_completion",
            }
            backend.store(
                content,
                source=f"task:{record['task_id']}",
                metadata=metadata,
            )
            logger.info(
                "rag: stashed completed task %s (role=%s)",
                record["task_id"], record["role"],
            )
        except Exception:  # pragma: no cover — defensive
            logger.exception("rag stash failed for task %s", record.get("task_id"))

    def _mark_task_abandoned(self, *, pane_id: str) -> None:
        """Mark the task owned by *pane_id* abandoned (pane was killed
        before it published its result)."""
        if not pane_id:
            return
        now = time.time()
        with self._db_lock:
            self._conn.execute(
                "UPDATE task_journal SET status='abandoned', "
                " abandoned_at=?, last_update=? "
                "WHERE pane_id=? AND status='in_progress'",
                (now, now, pane_id),
            )
            self._conn.commit()
        self._pane_to_task.pop(pane_id, None)

    def _reconcile_orphan_tasks(self) -> int:
        """Mark every in_progress task whose pane is no longer live as
        abandoned. Called once at orchestrator construction so the
        journal is honest after a daemon crash / kill.

        Returns the number of rows reconciled.
        """
        try:
            with self._db_lock:
                cur = self._conn.execute(
                    "SELECT task_id, pane_id FROM task_journal "
                    "WHERE status='in_progress'"
                )
                rows = cur.fetchall()
        except sqlite3.Error:
            return 0
        if not rows:
            return 0
        # In-memory registry is empty at construction time, but the
        # tmux_panes table has a record of which panes were live; if
        # killed_at is NULL the daemon thinks the pane is alive — but
        # since we just restarted, every pane is in fact dead.
        now = time.time()
        count = 0
        with self._db_lock:
            for row in rows:
                self._conn.execute(
                    "UPDATE task_journal SET status='abandoned', "
                    " abandoned_at=?, last_update=? WHERE task_id=?",
                    (now, now, row["task_id"] if hasattr(row, "keys") else row[0]),
                )
                count += 1
            self._conn.commit()
        return count

    def journal_query(
        self,
        *,
        statuses: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Read tasks from the journal. Defaults to in_progress + abandoned.

        Public API — used by ``hope-journal`` and the wake-up briefing.
        """
        if statuses is None:
            statuses = ["in_progress", "abandoned"]
        placeholders = ",".join("?" for _ in statuses)
        sql = (
            "SELECT task_id, goal, role, cli, status, pane_id, parent_pane,"
            " team_id, context_json, result, created_at, started_at,"
            " completed_at, abandoned_at, last_update "
            "FROM task_journal "
            f"WHERE status IN ({placeholders}) "
            "ORDER BY last_update DESC LIMIT ?"
        )
        with self._db_lock:
            cur = self._conn.execute(sql, [*statuses, limit])
            rows = [dict(row) for row in cur.fetchall()]
        return rows

    def journal_get(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one journal row by task_id."""
        with self._db_lock:
            cur = self._conn.execute(
                "SELECT task_id, goal, role, cli, status, pane_id, parent_pane,"
                " team_id, context_json, result, created_at, started_at,"
                " completed_at, abandoned_at, last_update "
                "FROM task_journal WHERE task_id=?",
                (task_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def journal_history_for_pane(
        self, pane_id: str, *, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Replay the bus messages for a pane (sent OR received).

        Used when resuming an abandoned task — the new pane gets the
        prior conversation as context so it can pick up where the
        dead pane left off.
        """
        with self._db_lock:
            cur = self._conn.execute(
                "SELECT id, agent_id, content, from_role, to_role, topic,"
                " correlation_id, created_at "
                "FROM agent_messages "
                "WHERE agent_id=? OR topic LIKE ? OR topic LIKE ? "
                "ORDER BY created_at ASC LIMIT ?",
                (pane_id, f"to:{pane_id}", f"to:{pane_id}", limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
        return rows

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
