"""Tests for ``hope status`` (:mod:`hope.cli.status_cmd`)."""

from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

from hope.cli import cli


class TestStatusCmd:
    def test_not_running(self) -> None:
        with patch("hope.daemon.core.read_pid", return_value=None):
            result = CliRunner().invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "not running" in result.output

    def test_json_not_running(self) -> None:
        with patch("hope.daemon.core.read_pid", return_value=None):
            result = CliRunner().invoke(cli, ["status", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["running"] is False
        assert payload["pid"] is None
        assert payload["state"] is None

    def test_running_happy_path(self) -> None:
        state = {
            "pid": 1234,
            "started_at": 0.0,
            "orchestrator_started": True,
            "hope_main_pane_id": "hope-abc",
            "specialist_count": 2,
            "queued_spawn_count": 1,
            "wake_monitor_available": True,
            "wake_monitor_active": True,
            "bus_socket": "/tmp/bus.sock",
            "control_socket": "/tmp/ctrl.sock",
        }
        with (
            patch("hope.daemon.core.read_pid", return_value=1234),
            patch(
                "hope.daemon.core.send_control",
                return_value={"ok": True, "state": state},
            ),
        ):
            result = CliRunner().invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "running" in result.output
        assert "1234" in result.output
        assert "hope-abc" in result.output

    def test_running_json_returns_state_shape(self) -> None:
        state = {
            "pid": 1234,
            "started_at": 10.0,
            "orchestrator_started": True,
            "hope_main_pane_id": None,
            "specialist_count": 0,
            "queued_spawn_count": 0,
            "wake_monitor_available": False,
            "wake_monitor_active": False,
            "bus_socket": "",
            "control_socket": "/tmp/ctrl.sock",
        }
        with (
            patch("hope.daemon.core.read_pid", return_value=1234),
            patch(
                "hope.daemon.core.send_control",
                return_value={"ok": True, "state": state},
            ),
        ):
            result = CliRunner().invoke(cli, ["status", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["running"] is True
        assert payload["pid"] == 1234
        assert payload["state"]["orchestrator_started"] is True
        assert "hope_main_pane_id" in payload["state"]
        assert "wake_monitor_available" in payload["state"]

    def test_running_socket_missing_still_reports(self) -> None:
        with (
            patch("hope.daemon.core.read_pid", return_value=5678),
            patch(
                "hope.daemon.core.send_control",
                side_effect=FileNotFoundError("no socket"),
            ),
        ):
            result = CliRunner().invoke(cli, ["status"])
        # Not a hard failure — we print a warning and exit 0.
        assert result.exit_code == 0
        assert "5678" in result.output
