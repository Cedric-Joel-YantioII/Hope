"""End-to-end daemon lifecycle tests with a fake tmux runner.

Skipped on CI by default — opt in with ``HOPE_RUN_DAEMON_INTEGRATION=1``.
Even off-CI, the entire suite avoids touching the user's real
``~/.hope`` directory by redirecting every relevant path at
``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

# Skipped unless the user explicitly opts in — the real tmux/claude
# combo isn't always available in CI.
_SKIP = os.environ.get("HOPE_RUN_DAEMON_INTEGRATION") != "1"


pytestmark = pytest.mark.skipif(
    _SKIP,
    reason="set HOPE_RUN_DAEMON_INTEGRATION=1 to run",
)


class _FakeTmuxRunner:
    """Stand-in for :func:`subprocess.run` that mimics tmux behavior."""

    def __init__(self) -> None:
        self.calls: List[List[str]] = []

    def __call__(self, cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        if cmd[:2] == ["tmux", "has-session"]:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")
        if cmd[:2] == ["tmux", "split-window"]:
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout="%42\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")


def test_full_daemon_lifecycle(tmp_path: Path) -> None:
    """Spin up daemon → wake → status → shutdown against a fake tmux."""
    from hope.agents.tmux_orchestrator import TmuxOrchestrator
    from hope.core.config import OrchestratorConfig
    from hope.core.events import EventBus
    from hope.daemon.core import HopeDaemon, send_control

    runner = _FakeTmuxRunner()
    bus = EventBus()
    orch_cfg = OrchestratorConfig(
        bus_socket_path=str(tmp_path / "bus.sock"),
        panes_dir=str(tmp_path / "panes"),
    )
    orch = TmuxOrchestrator(
        config=orch_cfg,
        db_path=str(tmp_path / "agents.db"),
        bus=bus,
        tmux_runner=runner,
    )
    daemon = HopeDaemon(
        bus=bus,
        orchestrator=orch,
        wake_monitor=MagicMock(is_monitoring=False),
        enable_wake=False,
        pid_file=tmp_path / "daemon.pid",
        control_socket=tmp_path / "daemon.sock",
    )
    state = daemon.start()
    try:
        assert (tmp_path / "daemon.pid").exists()
        assert state.orchestrator_started is True

        # Send a status probe via the real control socket.
        resp = send_control("status", socket_path=tmp_path / "daemon.sock")
        assert resp["ok"] is True
        assert resp["state"]["hope_main_pane_id"]

        # Wake-trigger round-trip.
        with patch("hope.daemon.core.say") as mock_say:
            resp = send_control(
                "wake",
                {"source": "manual"},
                socket_path=tmp_path / "daemon.sock",
            )
            assert resp["ok"] is True
            # Already awake → say "I'm already awake"
            # Give the handler a moment to run in the bus thread.
            time.sleep(0.1)
            assert mock_say.called
    finally:
        daemon.shutdown()
    assert not (tmp_path / "daemon.pid").exists()


def test_sleep_over_control_socket(tmp_path: Path) -> None:
    """Sleep control-socket cmd schedules shutdown in a background thread."""
    from hope.daemon.core import HopeDaemon, send_control

    daemon = HopeDaemon(
        orchestrator=MagicMock(
            hope_main_pane_id="hope-x",
            bus_socket_path=tmp_path / "bus.sock",
            _started=True,
            registry=MagicMock(specialist_count=MagicMock(return_value=0)),
            queued_spawn_count=MagicMock(return_value=0),
        ),
        wake_monitor=None,
        enable_wake=False,
        pid_file=tmp_path / "daemon.pid",
        control_socket=tmp_path / "daemon.sock",
    )
    daemon.start()
    try:
        resp = send_control("sleep", socket_path=tmp_path / "daemon.sock")
        assert resp["ok"] is True
        assert resp["shutting_down"] is True
        # Wait up to 2s for the shutdown to actually run.
        deadline = time.time() + 2.0
        while time.time() < deadline and (tmp_path / "daemon.pid").exists():
            time.sleep(0.05)
        assert not (tmp_path / "daemon.pid").exists()
    finally:
        # Defensive cleanup — shutdown is idempotent.
        daemon.shutdown()


def test_wake_monitor_import_failure_is_tolerated(tmp_path: Path) -> None:
    """If ``hope.wakeword`` import fails, daemon still comes up."""
    from hope.daemon.core import HopeDaemon

    # Ensure the module is not importable for this test.
    import sys

    original = sys.modules.pop("hope.wakeword", None)
    try:
        daemon = HopeDaemon(
            orchestrator=MagicMock(
                hope_main_pane_id="hope-x",
                bus_socket_path=tmp_path / "bus.sock",
                _started=True,
                registry=MagicMock(specialist_count=MagicMock(return_value=0)),
                queued_spawn_count=MagicMock(return_value=0),
            ),
            enable_wake=True,
            pid_file=tmp_path / "daemon.pid",
            control_socket=tmp_path / "daemon.sock",
        )
        state = daemon.start()
        try:
            assert state.wake_monitor_available is False
        finally:
            daemon.shutdown()
    finally:
        if original is not None:
            sys.modules["hope.wakeword"] = original
