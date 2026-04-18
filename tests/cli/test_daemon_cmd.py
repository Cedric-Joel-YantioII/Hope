"""Tests for ``hope start|stop|restart|status`` daemon management commands.

Top-level ``start``/``status`` have moved to the brain-daemon module
(:mod:`hope.daemon.core` + :mod:`hope.cli.start_cmd` /
:mod:`hope.cli.status_cmd`). ``stop``/``restart`` still live in the
legacy :mod:`hope.cli.daemon_cmd` and target the API-server PID file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from hope.cli import cli
from hope.cli.daemon_cmd import _read_pid, _write_pid


class TestLegacyStopRestart:
    """The old server-daemon stop/restart verbs are still wired."""

    def test_stop_no_server(self) -> None:
        with patch("hope.cli.daemon_cmd._read_pid", return_value=None):
            result = CliRunner().invoke(cli, ["stop"])
        assert result.exit_code != 0
        assert "No running server" in result.output

    def test_read_pid_no_file(self, tmp_path: Path) -> None:
        with patch(
            "hope.cli.daemon_cmd._PID_FILE",
            tmp_path / "nonexistent.pid",
        ):
            assert _read_pid() is None

    def test_write_and_read_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        with (
            patch("hope.cli.daemon_cmd._PID_FILE", pid_file),
            patch("hope.cli.daemon_cmd.DEFAULT_CONFIG_DIR", tmp_path),
            patch("os.kill", return_value=None),
        ):
            _write_pid(12345)
            assert pid_file.exists()
            assert _read_pid() == 12345


class TestBrainDaemonCommands:
    """Top-level ``start``/``status`` now drive the brain daemon."""

    def test_start_command_exists(self) -> None:
        result = CliRunner().invoke(cli, ["start", "--help"])
        assert result.exit_code == 0
        out = result.output.lower()
        assert "start" in out

    def test_status_no_daemon(self) -> None:
        with patch("hope.daemon.core.read_pid", return_value=None):
            result = CliRunner().invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "not running" in result.output

    def test_start_already_running(self) -> None:
        with patch("hope.daemon.core.read_pid", return_value=42):
            result = CliRunner().invoke(cli, ["start"])
        assert result.exit_code != 0
        assert "already running" in result.output
