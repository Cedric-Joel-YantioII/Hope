"""Tests for :class:`hope.agents.tmux_orchestrator.TmuxOrchestrator`.

The orchestrator touches tmux, a Unix-domain socket, FIFOs, and SQLite. We
fake the tmux subprocess runner in unit tests so the full lifecycle can
run inside a tmp_path sandbox on any CI. A single integration test exercises
the real binaries — it is skipped automatically when ``tmux`` or ``claude``
are unavailable.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, List

import pytest

from hope.agents.specialist_registry import SpecialistRegistry
from hope.agents.tmux_orchestrator import (
    PANE_SENTINEL_PREFIX,
    TmuxOrchestrator,
    apply_orchestrator_migrations,
)
from hope.core.config import OrchestratorConfig
from hope.core.events import EventBus, EventType


# ---------------------------------------------------------------------------
# Helpers — fake tmux runner
# ---------------------------------------------------------------------------


class _FakeTmux:
    """Records every tmux invocation and returns canned ``CompletedProcess``.

    Returns a fresh tmux-style pane ref (``%N``) for ``split-window``
    invocations so the orchestrator records a unique tmux_target per pane.
    """

    def __init__(self) -> None:
        self.calls: List[List[str]] = []
        self._next_pane = 10

    def __call__(self, cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        argv = cmd[1:] if cmd and cmd[0] == "tmux" else cmd
        first = argv[0] if argv else ""

        if first == "has-session":
            # Pretend the session does not exist the first time so start()
            # walks through the new-session path.
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")
        if first == "split-window":
            self._next_pane += 1
            return subprocess.CompletedProcess(
                cmd,
                returncode=0,
                stdout=f"%{self._next_pane}\n",
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    def commands(self) -> List[str]:
        return [c[1] if c and c[0] == "tmux" else c[0] for c in self.calls]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def orchestrator_env(tmp_path, monkeypatch):
    """Build an isolated orchestrator + capture bus events."""
    panes_dir = tmp_path / "panes"
    # Keep socket path short — macOS sun_path is 104 chars.
    socket_dir = Path(f"/tmp/hope-test-{uuid.uuid4().hex[:6]}")
    socket_dir.mkdir(parents=True, exist_ok=True)
    bus_socket = socket_dir / "bus.sock"
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    # Copy the shipped roles for realistic prompt loading.
    src_roles = Path(__file__).resolve().parents[2] / "src" / "hope" / "skills" / "roles"
    for md in src_roles.glob("*.md"):
        shutil.copy2(md, roles_dir / md.name)

    cfg = OrchestratorConfig(
        max_concurrent_specialists=2,
        tmux_session_name="hope-test",
        bus_socket_path=str(bus_socket),
        panes_dir=str(panes_dir),
        roles_dir=str(roles_dir),
    )
    bus = EventBus(record_history=True)
    captured: List[Any] = []

    for event_type in (
        EventType.PANE_SPAWNED,
        EventType.PANE_KILLED,
        EventType.PANE_MESSAGE,
        EventType.SPECIALIST_AT_CAPACITY,
    ):
        bus.subscribe(event_type, captured.append)

    fake_tmux = _FakeTmux()
    orch = TmuxOrchestrator(
        config=cfg,
        db_path=str(tmp_path / "agents.db"),
        bus=bus,
        registry=SpecialistRegistry(),
        tmux_runner=fake_tmux,
        project_dir=str(tmp_path),
    )

    yield orch, fake_tmux, captured, bus_socket

    try:
        orch.shutdown()
    except Exception:
        pass
    try:
        orch.close()
    except Exception:
        pass
    shutil.rmtree(socket_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_start_creates_hope_session_and_bus_socket(orchestrator_env):
    orch, fake, captured, bus_socket = orchestrator_env
    pane_id = orch.start()
    assert pane_id.startswith("hope-")
    # Bus socket exists and is a real Unix socket.
    assert bus_socket.exists(), "bus socket was not created"
    import stat

    assert stat.S_ISSOCK(bus_socket.stat().st_mode)
    # tmux was invoked to create the session.
    cmds = [c[1] if len(c) > 1 else c[0] for c in fake.calls]
    assert "has-session" in cmds
    assert "new-session" in cmds
    # hope-main is registered and not ephemeral.
    entry = orch.registry.get(pane_id)
    assert entry is not None and entry.is_ephemeral is False
    # hope-main picks up its default subscriptions.
    assert "to:hope" in entry.subscribed_topics
    # PANE_SPAWNED event fired.
    assert any(
        isinstance(e, object) and getattr(e, "event_type", None) == EventType.PANE_SPAWNED
        for e in captured
    )


def test_spawn_specialist_registers_and_persists(orchestrator_env):
    orch, fake, captured, _ = orchestrator_env
    orch.start()
    pane_id = orch.spawn_specialist(
        role="coder",
        task="refactor auth module",
        context={"files": ["a.py", "b.py"]},
    )
    assert pane_id.startswith("coder-")
    entry = orch.registry.get(pane_id)
    assert entry is not None and entry.is_ephemeral
    # Canonical topics bound for routing.
    for topic in (f"to:{pane_id}", "to:coder", "broadcast", "tools:request"):
        assert topic in entry.subscribed_topics
    # Row landed in the tmux_panes table.
    conn = sqlite3.connect(orch._db_path)
    row = conn.execute(
        "SELECT role, is_ephemeral, killed_at FROM tmux_panes WHERE pane_id = ?",
        (pane_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "coder"
    assert row[1] == 1
    assert row[2] is None
    # Prompt file was materialised with the injected task.
    prompt_path = Path(orch._panes_dir) / f"{pane_id}.prompt.md"
    assert prompt_path.exists()
    text = prompt_path.read_text()
    assert "refactor auth module" in text
    assert "Hope-coder" not in text or "Hope's coder" in text
    # Engine config handshake matches the sibling engine agent's field names.
    ec = orch.pane_engine_config(pane_id)
    assert set(ec.keys()) == {
        "pane_target",
        "fifo_path",
        "request_timeout_sec",
        "sentinel_prefix",
    }
    assert ec["sentinel_prefix"] == PANE_SENTINEL_PREFIX


def test_kill_specialist_cleans_up(orchestrator_env):
    orch, _, captured, _ = orchestrator_env
    orch.start()
    pane_id = orch.spawn_specialist(role="coder", task="x", context={})
    fifo = Path(orch.registry.get(pane_id).fifo_path)

    orch.kill_specialist(pane_id)
    assert orch.registry.get(pane_id) is None
    assert not fifo.exists()

    conn = sqlite3.connect(orch._db_path)
    killed_at = conn.execute(
        "SELECT killed_at FROM tmux_panes WHERE pane_id = ?",
        (pane_id,),
    ).fetchone()[0]
    conn.close()
    assert killed_at is not None and killed_at > 0

    events = [getattr(e, "event_type", None) for e in captured]
    assert EventType.PANE_KILLED in events

    # Idempotent — a second kill does not raise.
    orch.kill_specialist(pane_id)


def test_send_message_routes_and_autotags_roles(orchestrator_env):
    orch, _, captured, _ = orchestrator_env
    hope = orch.start()
    coder = orch.spawn_specialist(role="coder", task="t", context={})
    architect = orch.spawn_specialist(role="architect", task="t", context={})

    envelope = orch.send_message(
        from_pane=coder,
        to=architect,
        topic=f"to:{architect}",
        body="design review please",
        correlation_id="corr-1",
    )
    assert envelope["from_role"] == "coder"
    assert envelope["to_role"] == "architect"
    assert envelope["correlation_id"] == "corr-1"

    conn = sqlite3.connect(orch._db_path)
    row = conn.execute(
        "SELECT from_role, to_role, topic, correlation_id FROM agent_messages"
        " WHERE id = ?",
        (envelope["id"],),
    ).fetchone()
    conn.close()
    assert row == ("coder", "architect", f"to:{architect}", "corr-1")

    # Routing by role also auto-tags to_role.
    env2 = orch.send_message(
        from_pane=hope,
        to="coder",
        topic="to:coder",
        body="status?",
    )
    assert env2["to_role"] == "coder"

    # PANE_MESSAGE event fired for every routed message.
    assert sum(
        1 for e in captured if getattr(e, "event_type", None) == EventType.PANE_MESSAGE
    ) >= 2


def test_max_capacity_queues_and_emits_at_capacity(orchestrator_env):
    orch, _, captured, _ = orchestrator_env
    orch.start()
    a = orch.spawn_specialist(role="coder", task="t1", context={})
    b = orch.spawn_specialist(role="coder", task="t2", context={})
    assert a and b

    # Capacity is 2 — third spawn should queue and emit SPECIALIST_AT_CAPACITY.
    queued = orch.spawn_specialist(role="coder", task="t3", context={})
    assert queued == ""
    assert orch.queued_spawn_count() == 1
    assert any(
        getattr(e, "event_type", None) == EventType.SPECIALIST_AT_CAPACITY
        for e in captured
    )

    # Killing one frees capacity and the queued request is serviced.
    orch.kill_specialist(a)
    assert orch.queued_spawn_count() == 0
    # The freshly spawned pane should be present and ephemeral.
    live = {p.role for p in orch.registry.specialists()}
    assert live == {"coder"}
    assert len(orch.registry.specialists()) == 2


def test_shutdown_kills_every_specialist(orchestrator_env):
    orch, _, captured, bus_socket = orchestrator_env
    orch.start()
    orch.spawn_specialist(role="coder", task="t", context={})
    orch.spawn_specialist(role="architect", task="t", context={})
    assert len(orch.registry.specialists()) == 2

    orch.shutdown()
    assert len(orch.registry.specialists()) == 0
    # Bus socket is cleaned up.
    assert not bus_socket.exists()


def test_apply_orchestrator_migrations_is_idempotent(tmp_path):
    db = tmp_path / "m.db"
    conn = sqlite3.connect(db)
    apply_orchestrator_migrations(conn)
    # Run again — must not raise.
    apply_orchestrator_migrations(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_messages)")}
    assert {"from_role", "to_role", "topic", "correlation_id"}.issubset(cols)
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tmux_panes'"
    ).fetchone() is not None
    conn.close()


def test_role_template_injection_uses_placeholder(orchestrator_env):
    orch, _, _, _ = orchestrator_env
    orch.start()
    pane_id = orch.spawn_specialist(
        role="architect",
        task="boundary design for tmux panes",
        context={"note": "keep kernel tiny"},
    )
    body = (Path(orch._panes_dir) / f"{pane_id}.prompt.md").read_text()
    assert "Hope's architect specialist" in body
    assert "boundary design for tmux panes" in body
    assert "keep kernel tiny" in body
    # Placeholder must be fully consumed.
    assert "{task-specific context will be injected here at spawn time}" not in body


# ---------------------------------------------------------------------------
# Integration test — real tmux + real claude CLI
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("claude") is None,
    reason="requires both tmux and the claude CLI",
)
def test_integration_real_tmux_spawn_and_kill(tmp_path):
    """Smoke test: spawn a real claude pane in a real tmux server, then kill it.

    We don't drive a full dialogue — Claude Code needs network + auth —
    so the assertion is just: the pane exists under tmux list-panes after
    spawn, and is gone after kill.
    """
    session_name = f"hope-it-{uuid.uuid4().hex[:6]}"
    panes_dir = tmp_path / "panes"
    bus_socket = Path(f"/tmp/hope-it-{uuid.uuid4().hex[:6]}.sock")
    roles_dir = Path(__file__).resolve().parents[2] / "src" / "hope" / "skills" / "roles"

    cfg = OrchestratorConfig(
        max_concurrent_specialists=2,
        tmux_session_name=session_name,
        bus_socket_path=str(bus_socket),
        panes_dir=str(panes_dir),
        roles_dir=str(roles_dir),
    )
    orch = TmuxOrchestrator(
        config=cfg,
        db_path=str(tmp_path / "agents.db"),
        bus=EventBus(),
        project_dir=str(tmp_path),
    )
    hope_pane = None
    spec_pane = None
    try:
        hope_pane = orch.start()
        # Let tmux settle briefly.
        time.sleep(0.3)
        spec_pane = orch.spawn_specialist(role="coder", task="noop", context={})
        time.sleep(0.3)

        list_panes = subprocess.run(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert list_panes.returncode == 0, list_panes.stderr
        # At least 2 panes — hope-main + the specialist.
        assert len(list_panes.stdout.strip().splitlines()) >= 2

        orch.kill_specialist(spec_pane)
        time.sleep(0.3)
        list_panes_after = subprocess.run(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
        after_panes = list_panes_after.stdout.strip().splitlines()
        # The specialist is gone; hope-main remains.
        assert len(after_panes) < 2
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True,
            check=False,
        )
        try:
            orch.close()
        except Exception:
            pass
        if bus_socket.exists():
            bus_socket.unlink()
