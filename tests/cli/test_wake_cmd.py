"""Tests for ``hope wake`` and ``hope sleep``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from hope.cli import cli


class TestWakeCmd:
    def test_sends_wake_payload_when_daemon_live(self) -> None:
        with (
            patch("hope.daemon.core.read_pid", return_value=4321),
            patch(
                "hope.daemon.core.send_control",
                return_value={"ok": True},
            ) as mock_send,
        ):
            result = CliRunner().invoke(cli, ["wake", "--source", "voice"])
        assert result.exit_code == 0, result.output
        assert "Wake trigger sent" in result.output
        mock_send.assert_called_once()
        cmd, payload = mock_send.call_args.args[:2]
        assert cmd == "wake"
        assert payload == {"source": "voice"}

    def test_sends_text_payload(self) -> None:
        with (
            patch("hope.daemon.core.read_pid", return_value=4321),
            patch(
                "hope.daemon.core.send_control",
                return_value={"ok": True},
            ) as mock_send,
        ):
            result = CliRunner().invoke(
                cli, ["wake", "--text", "hey hope"]
            )
        assert result.exit_code == 0, result.output
        payload = mock_send.call_args.args[1]
        assert payload["text"] == "hey hope"
        assert payload["source"] == "manual"

    def test_starts_daemon_if_not_running(self) -> None:
        fake_proc = MagicMock(pid=111)
        with (
            patch("hope.daemon.core.read_pid", return_value=None),
            patch(
                "hope.cli.start_cmd.subprocess.Popen", return_value=fake_proc
            ) as mock_popen,
        ):
            result = CliRunner().invoke(cli, ["wake"])
        assert result.exit_code == 0, result.output
        assert "Daemon not running" in result.output
        mock_popen.assert_called_once()

    def test_daemon_rejects_wake(self) -> None:
        with (
            patch("hope.daemon.core.read_pid", return_value=4321),
            patch(
                "hope.daemon.core.send_control",
                return_value={"ok": False, "error": "boom"},
            ),
        ):
            result = CliRunner().invoke(cli, ["wake"])
        assert result.exit_code != 0
        assert "boom" in result.output


class TestSleepCmd:
    def test_no_daemon_running(self) -> None:
        with patch("hope.daemon.core.read_pid", return_value=None):
            result = CliRunner().invoke(cli, ["sleep"])
        assert result.exit_code != 0
        assert "not running" in result.output

    def test_graceful_sleep_via_control_socket(self) -> None:
        # First read_pid returns live pid; subsequent os.kill(pid, 0) raises
        # to mark process as gone, so the sleep command returns cleanly.
        call_state = {"killed": False}

        def fake_kill(pid: int, sig: int) -> None:
            if call_state["killed"]:
                raise OSError("no such process")
            # The 0-signal probe after send_control — fake the exit.
            if sig == 0:
                raise OSError("gone")

        with (
            patch("hope.daemon.core.read_pid", return_value=9090),
            patch(
                "hope.daemon.core.send_control",
                return_value={"ok": True, "shutting_down": True},
            ) as mock_send,
            patch("hope.cli.sleep_cmd.os.kill", side_effect=fake_kill),
            patch("hope.daemon.core.clear_pid") as mock_clear,
        ):
            result = CliRunner().invoke(cli, ["sleep"])
        assert result.exit_code == 0, result.output
        assert "Hope stopped" in result.output
        mock_send.assert_called_once()
        mock_clear.assert_called()

    def test_force_flag_uses_sigterm(self) -> None:
        def fake_kill(pid: int, sig: int) -> None:
            if sig == 0:
                raise OSError("gone")

        with (
            patch("hope.daemon.core.read_pid", return_value=8080),
            patch("hope.cli.sleep_cmd.os.kill", side_effect=fake_kill) as mock_kill,
            patch("hope.daemon.core.clear_pid"),
        ):
            result = CliRunner().invoke(cli, ["sleep", "--force"])
        assert result.exit_code == 0, result.output
        # At least one SIGTERM and probe call.
        assert mock_kill.called
