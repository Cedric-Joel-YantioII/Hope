"""Dashboard autolaunch test suite.

Covers :meth:`HopeDaemon._launch_dashboard_app` and the companion
shutdown path that terminates the child process.

What's mocked
-------------
* ``subprocess.Popen`` — so we never actually spawn ``open`` or
  ``npm run tauri dev``. Each test asserts on the *argv* the daemon
  would have used.
* ``pathlib.Path.exists`` — toggled per-test so we can simulate:
  "the .app is installed", "only the home bundle exists", "nothing is
  installed".
* ``shutil.which`` — lets us simulate npm-on-PATH vs. missing-npm.

No mic, no tmux, no real config file — we build a
:class:`DashboardConfig` in code and call the method directly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from hope.core.config import DashboardConfig
from hope.daemon.core import HopeDaemon


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_daemon(tmp_path: Path) -> HopeDaemon:
    """Bare daemon instance — no start(), no sockets, no mic."""
    return HopeDaemon(
        orchestrator=MagicMock(),
        wake_monitor=None,
        enable_wake=False,
        pid_file=tmp_path / "daemon.pid",
        control_socket=tmp_path / "daemon.sock",
    )


def _exists_only(paths: set[str]):
    """Build a ``Path.exists`` substitute that returns True iff the
    instance's string matches one of *paths*."""

    def _exists(self: Path) -> bool:  # noqa: ARG001 — self IS the Path
        return str(self) in paths

    return _exists


# ---------------------------------------------------------------------------
# Bundle path priority
# ---------------------------------------------------------------------------


class TestBundleLaunch:
    def test_override_bundle_path_wins(self, tmp_path, monkeypatch) -> None:
        """``app_bundle_path`` should take precedence over both
        ``/Applications`` and ``~/Applications``."""
        daemon = _make_daemon(tmp_path)
        override = "/opt/custom/Hope.app"
        # Pretend both the override AND /Applications/Hope.app exist —
        # we want to prove the override wins.
        monkeypatch.setattr(
            Path, "exists", _exists_only({override, "/Applications/Hope.app"}),
        )
        popen = MagicMock()
        monkeypatch.setattr("hope.daemon.core.subprocess.Popen", popen)

        cfg = DashboardConfig(
            autolaunch=True, app_bundle_path=override, dev_fallback=True,
        )
        daemon._launch_dashboard_app(cfg)

        popen.assert_called_once()
        argv = popen.call_args.args[0]
        assert argv == ["open", "-g", "-a", override]
        assert daemon._dashboard_app_proc is popen.return_value

    def test_falls_back_to_applications_dir(self, tmp_path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path)
        monkeypatch.setattr(
            Path, "exists", _exists_only({"/Applications/Hope.app"}),
        )
        popen = MagicMock()
        monkeypatch.setattr("hope.daemon.core.subprocess.Popen", popen)

        daemon._launch_dashboard_app(DashboardConfig(autolaunch=True))

        popen.assert_called_once()
        assert popen.call_args.args[0] == [
            "open", "-g", "-a", "/Applications/Hope.app",
        ]

    def test_falls_back_to_home_applications(self, tmp_path, monkeypatch) -> None:
        daemon = _make_daemon(tmp_path)
        home_bundle = str(Path.home() / "Applications" / "Hope.app")
        monkeypatch.setattr(Path, "exists", _exists_only({home_bundle}))
        popen = MagicMock()
        monkeypatch.setattr("hope.daemon.core.subprocess.Popen", popen)

        daemon._launch_dashboard_app(DashboardConfig(autolaunch=True))

        popen.assert_called_once()
        assert popen.call_args.args[0] == ["open", "-g", "-a", home_bundle]


# ---------------------------------------------------------------------------
# Dev fallback (npm run tauri dev)
# ---------------------------------------------------------------------------


class TestDevFallback:
    def test_dev_fallback_spawns_npm_when_no_bundle(
        self, tmp_path, monkeypatch,
    ) -> None:
        daemon = _make_daemon(tmp_path)
        # No bundle at any of the candidate paths, but frontend/package.json
        # in the repo root DOES exist (we don't negate that one — rely on
        # the real repo checkout).
        monkeypatch.setattr(
            Path,
            "exists",
            lambda self: str(self).endswith("frontend/package.json"),
        )
        monkeypatch.setattr(
            "hope.daemon.core.shutil.which",
            lambda name: "/usr/local/bin/npm" if name == "npm" else None,
        )
        popen = MagicMock()
        monkeypatch.setattr("hope.daemon.core.subprocess.Popen", popen)

        cfg = DashboardConfig(autolaunch=True, dev_fallback=True)
        daemon._launch_dashboard_app(cfg)

        popen.assert_called_once()
        argv = popen.call_args.args[0]
        assert argv == ["/usr/local/bin/npm", "run", "tauri", "dev"]
        # Must have a cwd pointing at frontend/
        kwargs = popen.call_args.kwargs
        assert kwargs.get("cwd", "").endswith("frontend")
        assert kwargs.get("start_new_session") is True

    def test_skips_dev_fallback_when_disabled(
        self, tmp_path, monkeypatch,
    ) -> None:
        daemon = _make_daemon(tmp_path)
        # No bundle, no frontend — dev_fallback=False should short-circuit
        # before even looking for npm.
        monkeypatch.setattr(Path, "exists", lambda self: False)
        popen = MagicMock()
        monkeypatch.setattr("hope.daemon.core.subprocess.Popen", popen)

        cfg = DashboardConfig(autolaunch=True, dev_fallback=False)
        daemon._launch_dashboard_app(cfg)

        popen.assert_not_called()
        assert daemon._dashboard_app_proc is None

    def test_dev_fallback_skipped_when_npm_missing(
        self, tmp_path, monkeypatch,
    ) -> None:
        daemon = _make_daemon(tmp_path)
        monkeypatch.setattr(Path, "exists", lambda self: False)
        monkeypatch.setattr("hope.daemon.core.shutil.which", lambda name: None)
        popen = MagicMock()
        monkeypatch.setattr("hope.daemon.core.subprocess.Popen", popen)

        daemon._launch_dashboard_app(
            DashboardConfig(autolaunch=True, dev_fallback=True),
        )

        popen.assert_not_called()


# ---------------------------------------------------------------------------
# Autolaunch respect the config flag
# ---------------------------------------------------------------------------


class TestAutolaunchFlag:
    def test_try_start_skips_launch_when_autolaunch_false(
        self, tmp_path, monkeypatch,
    ) -> None:
        """With ``autolaunch=False`` the bridge starts but no child spawns."""
        daemon = _make_daemon(tmp_path)
        # Patch load_config to return a DashboardConfig with autolaunch off.
        from hope.core.config import HopeConfig

        cfg = HopeConfig()
        cfg.dashboard.enabled = True
        cfg.dashboard.autolaunch = False
        monkeypatch.setattr("hope.daemon.core.load_config", lambda: cfg)

        # Stub DashboardBridge so we don't open real ports.
        bridge = MagicMock()
        bridge.port = 8765
        monkeypatch.setattr(
            "hope.daemon.core.DashboardBridge",
            lambda *a, **kw: bridge,
        )
        launch = MagicMock()
        monkeypatch.setattr(daemon, "_launch_dashboard_app", launch)

        daemon._try_start_dashboard_bridge()

        bridge.start.assert_called_once()
        launch.assert_not_called()

    def test_try_start_invokes_launch_when_autolaunch_true(
        self, tmp_path, monkeypatch,
    ) -> None:
        daemon = _make_daemon(tmp_path)
        from hope.core.config import HopeConfig

        cfg = HopeConfig()
        cfg.dashboard.enabled = True
        cfg.dashboard.autolaunch = True
        monkeypatch.setattr("hope.daemon.core.load_config", lambda: cfg)

        bridge = MagicMock()
        bridge.port = 8765
        monkeypatch.setattr(
            "hope.daemon.core.DashboardBridge",
            lambda *a, **kw: bridge,
        )
        launch = MagicMock()
        monkeypatch.setattr(daemon, "_launch_dashboard_app", launch)

        daemon._try_start_dashboard_bridge()

        bridge.start.assert_called_once()
        launch.assert_called_once()


# ---------------------------------------------------------------------------
# Shutdown terminates the child
# ---------------------------------------------------------------------------


class TestShutdownTerminatesChild:
    def test_shutdown_sigterms_running_child(self, tmp_path) -> None:
        daemon = _make_daemon(tmp_path)
        proc = MagicMock()
        proc.poll.return_value = None  # still running
        proc.wait.return_value = 0
        daemon._dashboard_app_proc = proc

        daemon.shutdown()

        proc.terminate.assert_called_once()
        proc.wait.assert_called()
        # Should NOT have escalated to SIGKILL — wait() returned cleanly.
        proc.kill.assert_not_called()
        assert daemon._dashboard_app_proc is None

    def test_shutdown_sigkills_on_grace_timeout(self, tmp_path) -> None:
        daemon = _make_daemon(tmp_path)
        proc = MagicMock()
        proc.poll.return_value = None
        # First wait() (after SIGTERM) times out; second wait() (after
        # SIGKILL) returns cleanly.
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="tauri", timeout=5.0),
            0,
        ]
        daemon._dashboard_app_proc = proc

        daemon.shutdown()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_shutdown_skips_already_exited_child(self, tmp_path) -> None:
        """If the user closed the window, ``poll()`` returns a status —
        shutdown must not try to SIGTERM a dead process."""
        daemon = _make_daemon(tmp_path)
        proc = MagicMock()
        proc.poll.return_value = 0  # already exited
        daemon._dashboard_app_proc = proc

        daemon.shutdown()

        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
