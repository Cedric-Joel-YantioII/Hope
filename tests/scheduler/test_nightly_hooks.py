"""Tests for the daemon-wired scheduler hooks.

Covers the three contracts the task specified:

  1. The scheduler starts when ``[scheduler] enabled`` is true.
  2. The default jobs (``hope_consolidate`` + ``connector_sync``) are
     registered on first boot and NOT duplicated on restart.
  3. ``daemon.shutdown()`` stops the scheduler cleanly.

We build a bare :class:`HopeDaemon` with orchestrator/wake injected as
MagicMocks so we don't spin up tmux or the mic backend.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hope.daemon.core import HopeDaemon
from hope.scheduler.scheduler import TaskScheduler
from hope.scheduler.store import SchedulerStore


@pytest.fixture()
def bare_daemon(tmp_path, monkeypatch):
    """Daemon with everything except the scheduler mocked out."""
    # Point the RAG + scheduler at tmp_path so tests never touch ~/.hope.
    from hope.core import config as _cfg_mod

    def _cfg_for_scheduler(enabled: bool = True):
        cfg = _cfg_mod.HopeConfig()
        cfg.scheduler.enabled = enabled
        cfg.scheduler.poll_interval = 1
        cfg.scheduler.db_path = str(tmp_path / "sched.db")
        cfg.tools.storage.default_backend = "sqlite"
        cfg.tools.storage.db_path = str(tmp_path / "mem.db")
        return cfg

    monkeypatch.setattr("hope.daemon.core.load_config", _cfg_for_scheduler)
    monkeypatch.setattr(
        "hope.memory.rag.load_config", lambda: _cfg_for_scheduler(True),
    )

    daemon = HopeDaemon(
        orchestrator=MagicMock(),
        wake_monitor=MagicMock(),
        enable_wake=False,
        pid_file=tmp_path / "daemon.pid",
        control_socket=tmp_path / "daemon.sock",
    )
    # Patch out heavy start-side-effects: control socket, signal handlers,
    # dashboard bridge, speech backend, voice learning. We only care about
    # the scheduler here.
    with (
        patch.object(daemon, "_try_start_speech_backend"),
        patch.object(daemon, "_try_start_dashboard_bridge", create=True),
        patch.object(daemon, "_init_voice_learning", create=True),
        patch.object(daemon, "_start_control_socket"),
    ):
        yield daemon
    try:
        daemon.shutdown()
    except Exception:
        pass


class TestSchedulerLifecycle:
    def test_scheduler_starts_when_enabled(self, bare_daemon):
        bare_daemon.start()
        assert isinstance(bare_daemon._scheduler, TaskScheduler)
        # Poll thread must have come up.
        assert bare_daemon._scheduler._thread is not None
        assert bare_daemon._scheduler._thread.is_alive()

    def test_scheduler_skipped_when_disabled(self, tmp_path, monkeypatch):
        from hope.core import config as _cfg_mod

        def _cfg():
            cfg = _cfg_mod.HopeConfig()
            cfg.scheduler.enabled = False
            cfg.tools.storage.default_backend = "sqlite"
            cfg.tools.storage.db_path = str(tmp_path / "mem.db")
            return cfg

        monkeypatch.setattr("hope.daemon.core.load_config", _cfg)
        monkeypatch.setattr("hope.memory.rag.load_config", _cfg)

        d = HopeDaemon(
            orchestrator=MagicMock(),
            wake_monitor=MagicMock(),
            enable_wake=False,
            pid_file=tmp_path / "d.pid",
            control_socket=tmp_path / "d.sock",
        )
        with (
            patch.object(d, "_try_start_speech_backend"),
            patch.object(d, "_try_start_dashboard_bridge", create=True),
            patch.object(d, "_init_voice_learning", create=True),
            patch.object(d, "_start_control_socket"),
        ):
            d.start()
            assert d._scheduler is None
            d.shutdown()


class TestDefaultJobs:
    def test_jobs_registered_on_first_boot(self, bare_daemon):
        bare_daemon.start()
        tasks = bare_daemon._scheduler.list_tasks()
        default_ids = {t.metadata.get("default_job_id") for t in tasks}
        assert "hope_consolidate" in default_ids
        assert "connector_sync" in default_ids

    def test_consolidate_is_cron_at_3am(self, bare_daemon):
        bare_daemon.start()
        tasks = bare_daemon._scheduler.list_tasks()
        consolidate = next(
            t for t in tasks
            if t.metadata.get("default_job_id") == "hope_consolidate"
        )
        assert consolidate.schedule_type == "cron"
        assert consolidate.schedule_value == "0 3 * * *"

    def test_no_duplicate_jobs_on_restart(self, tmp_path, monkeypatch):
        """Second daemon boot against the same DB should NOT add more jobs."""
        from hope.core import config as _cfg_mod

        db = tmp_path / "sched.db"
        mem_db = tmp_path / "mem.db"

        def _cfg():
            cfg = _cfg_mod.HopeConfig()
            cfg.scheduler.enabled = True
            cfg.scheduler.poll_interval = 1
            cfg.scheduler.db_path = str(db)
            cfg.tools.storage.default_backend = "sqlite"
            cfg.tools.storage.db_path = str(mem_db)
            return cfg

        monkeypatch.setattr("hope.daemon.core.load_config", _cfg)
        monkeypatch.setattr("hope.memory.rag.load_config", _cfg)

        store = SchedulerStore(str(db))
        scheduler = TaskScheduler(store, poll_interval=1)
        HopeDaemon._register_default_jobs(scheduler)
        first_count = len(scheduler.list_tasks())
        # Second call should be a no-op.
        HopeDaemon._register_default_jobs(scheduler)
        second_count = len(scheduler.list_tasks())
        assert first_count == second_count == 2


class TestShutdownCleanup:
    def test_shutdown_stops_scheduler_thread(self, bare_daemon):
        bare_daemon.start()
        sched = bare_daemon._scheduler
        assert sched._thread.is_alive()
        bare_daemon.shutdown()
        # Thread reference is cleared; should no longer be alive.
        assert bare_daemon._scheduler is None
        # The underlying thread should have been joined.
        assert sched._thread is None or not sched._thread.is_alive()
