"""Tests for ``hope start`` (:mod:`hope.cli.start_cmd`)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from hope.cli import cli


def _patched_daemon(
    *,
    orchestrator: MagicMock | None = None,
    wake_monitor: MagicMock | None = None,
    state_kwargs: dict | None = None,
) -> MagicMock:
    """Build a MagicMock standing in for :class:`hope.daemon.core.HopeDaemon`."""
    from hope.daemon.core import DaemonState

    orchestrator = orchestrator or MagicMock()
    orchestrator.hope_main_pane_id = "hope-abcd"
    orchestrator._started = True
    orchestrator.bus_socket_path = "/tmp/fake-bus.sock"
    orchestrator.queued_spawn_count.return_value = 0
    orchestrator.registry.specialist_count.return_value = 0

    wake_monitor = wake_monitor or MagicMock()
    wake_monitor.is_monitoring = True

    state = DaemonState(
        pid=9999,
        started_at=123456.0,
        orchestrator_started=True,
        hope_main_pane_id="hope-abcd",
        specialist_count=0,
        queued_spawn_count=0,
        wake_monitor_available=True,
        wake_monitor_active=True,
        bus_socket="/tmp/fake-bus.sock",
        control_socket="/tmp/fake-ctrl.sock",
    )
    for key, value in (state_kwargs or {}).items():
        setattr(state, key, value)

    daemon = MagicMock()
    daemon.start.return_value = state
    daemon.orchestrator = orchestrator
    daemon.wake_monitor = wake_monitor
    # run_forever is a blocking no-op in tests.
    daemon.run_forever.side_effect = lambda: None
    return daemon


class TestStartCmd:
    def test_refuses_when_already_running(self) -> None:
        with patch("hope.daemon.core.read_pid", return_value=42):
            result = CliRunner().invoke(cli, ["start"])
        assert result.exit_code != 0
        assert "already running" in result.output

    def test_foreground_start_spawns_orchestrator_and_wake(
        self, tmp_path: Path
    ) -> None:
        fake_daemon = _patched_daemon()
        with (
            patch("hope.daemon.core.read_pid", return_value=None),
            patch("hope.daemon.core.HopeDaemon", return_value=fake_daemon),
            patch("hope.daemon.core.PID_FILE", tmp_path / "daemon.pid"),
        ):
            result = CliRunner().invoke(cli, ["start", "--foreground"])
        assert result.exit_code == 0, result.output
        assert "Hope is ready" in result.output
        # daemon.start() was called exactly once.
        assert fake_daemon.start.call_count == 1
        # shutdown is always called on exit.
        assert fake_daemon.shutdown.called

    def test_no_wake_flag_disables_wake_monitor(self, tmp_path: Path) -> None:
        captured = {}

        def _factory(**kwargs):
            captured.update(kwargs)
            return _patched_daemon()

        with (
            patch("hope.daemon.core.read_pid", return_value=None),
            patch("hope.daemon.core.HopeDaemon", side_effect=_factory),
            patch("hope.daemon.core.PID_FILE", tmp_path / "daemon.pid"),
        ):
            result = CliRunner().invoke(cli, ["start", "--foreground", "--no-wake"])
        assert result.exit_code == 0, result.output
        assert captured.get("enable_wake") is False

    def test_detach_spawns_background_process(self, tmp_path: Path) -> None:
        fake_proc = MagicMock(pid=54321)
        with (
            patch("hope.daemon.core.read_pid", return_value=None),
            patch(
                "hope.cli.start_cmd.subprocess.Popen", return_value=fake_proc
            ) as mock_popen,
            patch("hope.daemon.core.LOG_FILE", tmp_path / "daemon.log"),
        ):
            result = CliRunner().invoke(cli, ["start", "--detach"])
        assert result.exit_code == 0, result.output
        assert "launching in the background" in result.output
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args.args[0]
        assert cmd[1:] == ["-m", "hope.cli", "start", "--foreground"]


class TestHopeDaemonIntegration:
    """Direct tests on :class:`hope.daemon.core.HopeDaemon` using mocks."""

    def test_daemon_start_writes_pid_and_subscribes_wake(
        self, tmp_path: Path
    ) -> None:
        import tempfile

        from hope.core.events import EventBus, EventType
        from hope.daemon.core import HopeDaemon

        bus = EventBus(record_history=True)
        orch = MagicMock()
        orch.hope_main_pane_id = "hope-x"
        orch.bus_socket_path = tmp_path / "bus.sock"
        orch._started = True
        orch.registry.specialist_count.return_value = 0
        orch.queued_spawn_count.return_value = 0

        wm = MagicMock()
        wm.is_monitoring = True

        # AF_UNIX paths cap at 104 chars on macOS — use the shortest
        # tmpdir available instead of pytest's deep nested tmp_path.
        short_dir = Path(tempfile.mkdtemp(prefix="hope_t_"))
        pid_file = short_dir / "d.pid"
        ctrl = short_dir / "d.sock"
        daemon = HopeDaemon(
            bus=bus,
            orchestrator=orch,
            wake_monitor=wm,
            enable_wake=True,
            pid_file=pid_file,
            control_socket=ctrl,
        )
        try:
            state = daemon.start()
            assert pid_file.exists()
            assert state.hope_main_pane_id == "hope-x"
            # Publishing WAKE_TRIGGER when already awake -> say("I'm already awake")
            with patch("hope.daemon.core.say") as mock_say:
                bus.publish(
                    EventType.WAKE_TRIGGER,
                    {"source": "manual", "text": None, "timestamp": 0.0},
                )
                mock_say.assert_called_once()
                assert "already awake" in mock_say.call_args.args[0].lower()
        finally:
            daemon.shutdown()
            import shutil

            shutil.rmtree(short_dir, ignore_errors=True)
        assert not pid_file.exists()
        orch.shutdown.assert_called()
        wm.stop.assert_called()
