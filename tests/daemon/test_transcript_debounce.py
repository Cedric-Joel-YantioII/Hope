"""Tests for HopeDaemon's transcript debounce-and-merge.

The VAD chops at a fixed silence threshold; users routinely pause
mid-sentence longer than that. Without this layer, half-sentences land
in the brain and Hope replies confused. The debouncer holds dispatch
for a short window so two-or-three-segment thoughts arrive as one
brain turn.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hope.daemon.core import HopeDaemon


def _make_daemon(tmp_path: Path) -> HopeDaemon:
    return HopeDaemon(
        orchestrator=MagicMock(),
        wake_monitor=None,
        enable_wake=False,
        pid_file=tmp_path / "daemon.pid",
        control_socket=tmp_path / "daemon.sock",
    )


def test_single_transcript_dispatches_after_debounce(tmp_path, monkeypatch):
    """A lone segment ships unchanged once the timer fires."""
    daemon = _make_daemon(tmp_path)
    daemon._DEBOUNCE_SEC = 0.05

    captured: list[tuple[str, str]] = []

    class FakeExecutor:
        def submit(self, fn, *args):
            captured.append(args)
            return MagicMock()

    monkeypatch.setattr(daemon, "_ensure_brain_executor", lambda: FakeExecutor())
    daemon._orchestrator.registry.get = lambda _id: object()

    daemon._enqueue_debounced("what time is it", "hope-pane")
    time.sleep(0.15)

    assert captured == [("what time is it", "hope-pane")]


def test_two_transcripts_within_window_merge(tmp_path, monkeypatch):
    """Two segments arriving inside ``_DEBOUNCE_SEC`` ship as one."""
    daemon = _make_daemon(tmp_path)
    daemon._DEBOUNCE_SEC = 0.1

    captured: list[tuple] = []

    class FakeExecutor:
        def submit(self, fn, *args):
            captured.append(args)
            return MagicMock()

    monkeypatch.setattr(daemon, "_ensure_brain_executor", lambda: FakeExecutor())
    daemon._orchestrator.registry.get = lambda _id: object()

    daemon._enqueue_debounced("can you find me", "hope-pane")
    time.sleep(0.04)  # well inside the 100 ms window
    daemon._enqueue_debounced("the latest news", "hope-pane")
    time.sleep(0.2)

    assert captured == [("can you find me the latest news", "hope-pane")]


def test_three_segments_merge_into_one_turn(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path)
    daemon._DEBOUNCE_SEC = 0.08

    captured: list[tuple] = []

    class FakeExecutor:
        def submit(self, fn, *args):
            captured.append(args)
            return MagicMock()

    monkeypatch.setattr(daemon, "_ensure_brain_executor", lambda: FakeExecutor())
    daemon._orchestrator.registry.get = lambda _id: object()

    for chunk in ("Hope I want", "to play some", "Stevie Wonder"):
        daemon._enqueue_debounced(chunk, "hope-pane")
        time.sleep(0.03)
    time.sleep(0.2)

    assert captured == [("Hope I want to play some Stevie Wonder", "hope-pane")]


def test_transcripts_after_window_dispatch_separately(tmp_path, monkeypatch):
    """If the second segment lands AFTER the timer fired, it's a new turn."""
    daemon = _make_daemon(tmp_path)
    daemon._DEBOUNCE_SEC = 0.05

    captured: list[tuple] = []

    class FakeExecutor:
        def submit(self, fn, *args):
            captured.append(args)
            return MagicMock()

    monkeypatch.setattr(daemon, "_ensure_brain_executor", lambda: FakeExecutor())
    daemon._orchestrator.registry.get = lambda _id: object()

    daemon._enqueue_debounced("what time is it", "hope-pane")
    time.sleep(0.15)  # timer has fired
    daemon._enqueue_debounced("and what's the weather", "hope-pane")
    time.sleep(0.15)

    assert captured == [
        ("what time is it", "hope-pane"),
        ("and what's the weather", "hope-pane"),
    ]


def test_pane_vanishes_during_debounce_drops_silently(tmp_path, monkeypatch):
    """If the pane goes away mid-window, flush quietly drops the buffer."""
    daemon = _make_daemon(tmp_path)
    daemon._DEBOUNCE_SEC = 0.05

    captured: list[tuple] = []

    class FakeExecutor:
        def submit(self, fn, *args):  # pragma: no cover — must not be called
            captured.append(args)
            return MagicMock()

    monkeypatch.setattr(daemon, "_ensure_brain_executor", lambda: FakeExecutor())
    daemon._orchestrator.registry.get = lambda _id: None  # pane gone

    daemon._enqueue_debounced("hello", "hope-pane")
    time.sleep(0.15)

    assert captured == []
