"""Daemon boot test: verify the scheduler comes up and evolution gating.

Covers the ``HopeDaemon.start()`` contract:

* When ``[scheduler] enabled = true`` (the default), start() spins up a
  :class:`TaskScheduler`, binds ``daemon._scheduler`` to it, and the
  ``hope_consolidate`` cron job is registered with a populated
  ``next_run`` timestamp.
* When ``[evolution] enabled = true``, the ``evolution_run_cycle`` job
  from ``hope.evolution.default_jobs`` is seeded alongside it.
* When ``[evolution] enabled = false`` (the default), the evolution job
  is NOT registered — this matters because the evolution loop runs
  untrusted generated code in a sandbox and we refuse to install it
  silently.
* ``shutdown()`` tears the scheduler down cleanly (thread dies).

The daemon is instantiated with fake collaborators (orchestrator, wake
monitor, bus) so nothing touches tmux or the mic. The SQLite scheduler
store is redirected to ``tmp_path`` so the test never writes to
``~/.hope``.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hope.core.config import HopeConfig
from hope.daemon.core import HopeDaemon


def _short_sock_dir() -> Path:
    """Return a short-path directory for AF_UNIX sockets.

    macOS caps ``sun_path`` at 104 bytes; ``tmp_path`` under
    ``/var/folders/...`` is already ~60 chars and runs over once we
    append ``daemon.sock``. We stash the socket in ``/tmp/<short-uuid>``
    which is always ≤20 chars.
    """
    d = Path(tempfile.gettempdir()) / f"hope-test-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_orchestrator() -> MagicMock:
    """A tmux orchestrator that looks started-but-dormant to the daemon."""
    orch = MagicMock()
    orch._started = False
    orch.hope_main_pane_id = None
    orch.registry = MagicMock()
    orch.registry.specialist_count.return_value = 0
    orch.queued_spawn_count.return_value = 0
    orch.bus_socket_path = "/tmp/fake.sock"
    return orch


@pytest.fixture()
def stub_config(monkeypatch, tmp_path) -> HopeConfig:
    """Patch ``load_config`` to return a config isolated to tmp_path.

    Returns the config so each test can flip a flag before calling
    ``daemon.start()``.
    """
    cfg = HopeConfig()
    # Point the scheduler store at tmp_path so we never touch ~/.hope.
    cfg.scheduler.db_path = str(tmp_path / "scheduler.db")
    # Keep the poll interval short so stop() returns fast if a thread
    # does end up started during the test.
    cfg.scheduler.poll_interval = 5
    # Dashboard / speech / wake off — otherwise start() opens sockets or
    # mics we don't want in unit tests.
    cfg.dashboard.enabled = False
    cfg.speech.always_on = False
    cfg.learning.enabled = False

    monkeypatch.setattr("hope.daemon.core.load_config", lambda: cfg)
    return cfg


@pytest.fixture()
def sock_dir() -> Path:
    d = _short_sock_dir()
    yield d
    # Best-effort cleanup; sockets get unlinked by the daemon itself.
    try:
        for p in d.iterdir():
            p.unlink(missing_ok=True)
        d.rmdir()
    except OSError:
        pass


@pytest.fixture()
def isolated_daemon(
    tmp_path, sock_dir, monkeypatch, fake_orchestrator, stub_config,
) -> HopeDaemon:
    """Build a daemon that won't touch the user's tmux, mic, or ~/.hope."""
    pid_file = tmp_path / "daemon.pid"
    control_socket = sock_dir / "daemon.sock"

    # Block the RAG singleton from spinning up FAISS/SQLite on the real
    # memory backend path — it's irrelevant to these tests and would
    # write to ~/.hope/rag.
    monkeypatch.setattr(
        "hope.memory.get_rag",
        lambda: MagicMock(count=lambda: 0),
    )

    daemon = HopeDaemon(
        orchestrator=fake_orchestrator,
        wake_monitor=None,
        enable_wake=False,  # no mic
        pid_file=pid_file,
        control_socket=control_socket,
    )
    yield daemon
    # Always tear down so orphan threads don't bleed into the next test.
    try:
        daemon.shutdown()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Scheduler boot
# ---------------------------------------------------------------------------


class TestSchedulerStartsOnDaemonBoot:
    def test_scheduler_attribute_bound_after_start(
        self, isolated_daemon, stub_config,
    ) -> None:
        assert stub_config.scheduler.enabled is True  # defaults-on contract
        isolated_daemon.start()
        assert isolated_daemon._scheduler is not None

    def test_scheduler_thread_is_alive(self, isolated_daemon) -> None:
        isolated_daemon.start()
        sched = isolated_daemon._scheduler
        assert sched is not None
        assert sched._thread is not None
        assert sched._thread.is_alive()

    def test_hope_consolidate_job_registered_with_next_run(
        self, isolated_daemon,
    ) -> None:
        isolated_daemon.start()
        sched = isolated_daemon._scheduler
        tasks = sched.list_tasks()
        consolidate = [
            t for t in tasks
            if (t.metadata or {}).get("default_job_id") == "hope_consolidate"
        ]
        assert len(consolidate) == 1, (
            f"expected one hope_consolidate job, got {len(consolidate)}"
        )
        task = consolidate[0]
        assert task.schedule_type == "cron"
        assert task.schedule_value == "0 3 * * *"
        assert task.next_run is not None and task.next_run != ""

    def test_connector_sync_job_registered(self, isolated_daemon) -> None:
        isolated_daemon.start()
        sched = isolated_daemon._scheduler
        tasks = sched.list_tasks()
        conn = [
            t for t in tasks
            if (t.metadata or {}).get("default_job_id") == "connector_sync"
        ]
        assert len(conn) == 1


class TestSchedulerDisabledOptsOut:
    def test_scheduler_not_started_when_disabled(
        self, isolated_daemon, stub_config,
    ) -> None:
        stub_config.scheduler.enabled = False
        isolated_daemon.start()
        assert isolated_daemon._scheduler is None


# ---------------------------------------------------------------------------
# Evolution gating
# ---------------------------------------------------------------------------


class TestEvolutionJobGating:
    """Evolution is opt-in. The daemon must not register
    ``evolution_run_cycle`` unless ``[evolution] enabled = true``."""

    def test_evolution_job_NOT_registered_by_default(
        self, isolated_daemon, stub_config,
    ) -> None:
        assert stub_config.evolution.enabled is False
        isolated_daemon.start()
        sched = isolated_daemon._scheduler
        assert sched is not None
        tasks = sched.list_tasks()
        ids = {(t.metadata or {}).get("default_job_id") for t in tasks}
        assert "evolution_run_cycle" not in ids

    def test_evolution_job_registered_when_enabled(
        self, isolated_daemon, stub_config,
    ) -> None:
        stub_config.evolution.enabled = True
        isolated_daemon.start()
        sched = isolated_daemon._scheduler
        assert sched is not None
        tasks = sched.list_tasks()
        evo = [
            t for t in tasks
            if (t.metadata or {}).get("default_job_id") == "evolution_run_cycle"
        ]
        assert len(evo) == 1, (
            f"expected one evolution_run_cycle job, got {len(evo)}"
        )
        task = evo[0]
        assert task.schedule_type == "cron"
        # Job runs at 4 AM per default_jobs.py.
        assert task.schedule_value == "0 4 * * *"
        assert task.agent == "evolution"
        assert task.next_run is not None and task.next_run != ""


# ---------------------------------------------------------------------------
# Clean shutdown
# ---------------------------------------------------------------------------


class TestShutdownStopsScheduler:
    def test_shutdown_stops_scheduler_thread(self, isolated_daemon) -> None:
        isolated_daemon.start()
        sched = isolated_daemon._scheduler
        assert sched is not None
        thread = sched._thread
        assert thread is not None and thread.is_alive()
        isolated_daemon.shutdown()
        # Daemon clears its handle; the actual thread has been joined.
        assert isolated_daemon._scheduler is None
        assert not thread.is_alive()

    def test_shutdown_is_idempotent(self, isolated_daemon) -> None:
        isolated_daemon.start()
        isolated_daemon.shutdown()
        # Second shutdown must not raise.
        isolated_daemon.shutdown()

    def test_jobs_persist_across_restart(
        self, isolated_daemon, stub_config, tmp_path, monkeypatch,
        fake_orchestrator,
    ) -> None:
        """Restarting the daemon must NOT create duplicate jobs — the
        dedupe on ``default_job_id`` is what makes that safe."""
        isolated_daemon.start()
        before = len(isolated_daemon._scheduler.list_tasks())
        assert before >= 2  # consolidate + connector_sync
        isolated_daemon.shutdown()

        # Spin up a second daemon over the same sqlite store.
        monkeypatch.setattr(
            "hope.memory.get_rag",
            lambda: MagicMock(count=lambda: 0),
        )
        second_sock = _short_sock_dir()
        d2 = HopeDaemon(
            orchestrator=fake_orchestrator,
            wake_monitor=None,
            enable_wake=False,
            pid_file=tmp_path / "daemon2.pid",
            control_socket=second_sock / "daemon.sock",
        )
        try:
            d2.start()
            after = len(d2._scheduler.list_tasks())
            assert after == before, (
                "default_job_id dedupe failed — jobs duplicated across restart"
            )
        finally:
            d2.shutdown()
            try:
                for p in second_sock.iterdir():
                    p.unlink(missing_ok=True)
                second_sock.rmdir()
            except OSError:
                pass
