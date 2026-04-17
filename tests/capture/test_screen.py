"""Tests for :mod:`hope.capture.screen`.

The real capture path requires macOS Screen Recording permission and the
``mss`` optional dep; those tests are skipped elsewhere. The remaining
tests patch the frame grabber so they run on any platform/CI.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from hope.capture.screen import (
    CaptureSummary,
    ScreenCapture,
    _FrameGrabber,
    _Session,
)
from hope.core.events import EventBus, EventType


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _FakeGrabber(_FrameGrabber):
    """Returns a tiny synthetic BGRA frame — exercises the writer path."""

    def __init__(self, size=(2, 2)) -> None:
        self.size = size
        w, h = size
        # Magenta BGRX pixels.
        self._bytes = bytes([0xFF, 0x00, 0xFF, 0xFF]) * (w * h)
        self.calls = 0

    def grab_raw(self):
        self.calls += 1
        return self._bytes, self.size

    def close(self) -> None:
        pass


@pytest.fixture
def bus() -> EventBus:
    return EventBus(record_history=True)


@pytest.fixture
def capture(tmp_path, bus):
    with patch("hope.capture.screen._verify_screen_recording_permission"):
        with patch(
            "hope.capture.screen._probe_backend", return_value="mss"
        ), patch(
            "hope.capture.screen._build_grabber",
            side_effect=lambda s: _FakeGrabber(),
        ):
            yield ScreenCapture(root_dir=tmp_path, bus=bus, auto_prune=False)


# --------------------------------------------------------------------------- #
# Core behavior — patched backend (runs everywhere)
# --------------------------------------------------------------------------- #


def test_start_then_stop_produces_expected_frame_count(capture, bus, tmp_path):
    """start() + ~1s + stop() at 2fps yields 2 frames (±1 for scheduling)."""
    frames: List[dict] = []
    bus.subscribe(EventType.SCREEN_FRAME, lambda e: frames.append(e.data))

    capture.start("s1", fps=2)
    time.sleep(1.05)
    summary = capture.stop("s1")

    assert isinstance(summary, CaptureSummary)
    assert summary.session_id == "s1"
    # At 2fps over ~1s we expect 2 frames; allow 1-3 for scheduler jitter.
    assert 1 <= summary.frames_captured <= 3
    assert summary.frames_captured == len(frames)
    assert summary.output_dir == tmp_path / "s1"
    assert summary.output_dir.exists()
    assert summary.last_frame_path and summary.last_frame_path.exists()
    # PNGs written with the frame_NNNNNN.png pattern
    pngs = sorted(summary.output_dir.glob("frame_*.png"))
    assert len(pngs) == summary.frames_captured


def test_screen_frame_event_payload_shape(capture, bus):
    events: List[dict] = []
    bus.subscribe(EventType.SCREEN_FRAME, lambda e: events.append(e.data))

    capture.start("payload", fps=4)
    time.sleep(0.55)
    capture.stop("payload")

    assert events, "at least one SCREEN_FRAME event should fire"
    first = events[0]
    assert set(first) == {"session_id", "frame_idx", "path", "timestamp"}
    assert first["session_id"] == "payload"
    assert first["frame_idx"] == 1
    assert Path(first["path"]).exists()


def test_event_driven_start_and_stop(capture, bus):
    """Publishing SCREEN_CAPTURE_START/STOP drives the capture."""
    assert not capture.is_active("evt")

    bus.publish(
        EventType.SCREEN_CAPTURE_START, {"session_id": "evt", "fps": 2}
    )
    # Give the worker thread a moment to spin up.
    time.sleep(0.2)
    assert capture.is_active("evt")

    bus.publish(EventType.SCREEN_CAPTURE_STOP, {"session_id": "evt"})
    time.sleep(0.1)
    assert not capture.is_active("evt")


def test_multiple_simultaneous_sessions(capture, bus):
    capture.start("a", fps=2)
    capture.start("b", fps=2)
    time.sleep(0.55)
    sum_a = capture.stop("a")
    sum_b = capture.stop("b")
    assert sum_a.frames_captured >= 1
    assert sum_b.frames_captured >= 1
    assert sum_a.output_dir != sum_b.output_dir


def test_start_rejects_duplicate_session(capture):
    capture.start("dup", fps=2)
    try:
        with pytest.raises(ValueError):
            capture.start("dup", fps=2)
    finally:
        capture.stop("dup")


def test_start_rejects_non_positive_fps(capture):
    with pytest.raises(ValueError):
        capture.start("bad", fps=0)


def test_stop_unknown_session_raises(capture):
    with pytest.raises(KeyError):
        capture.stop("nope")


def test_stop_all_shuts_down_all_sessions(capture):
    capture.start("x", fps=2)
    capture.start("y", fps=2)
    summaries = capture.stop_all()
    assert len(summaries) == 2
    assert not capture.is_active("x")
    assert not capture.is_active("y")


def test_prune_old_sessions_removes_stale_dirs(tmp_path, bus):
    old = tmp_path / "old"
    fresh = tmp_path / "fresh"
    old.mkdir()
    fresh.mkdir()
    # Backdate the old one by 48h.
    stale = time.time() - 48 * 3600
    import os
    os.utime(old, (stale, stale))

    with patch("hope.capture.screen._verify_screen_recording_permission"), patch(
        "hope.capture.screen._probe_backend", return_value="mss"
    ):
        cap = ScreenCapture(root_dir=tmp_path, bus=bus, auto_prune=False)
        removed = cap.prune_old_sessions()
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


# --------------------------------------------------------------------------- #
# Darwin-only live smoke test
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only live capture")
def test_live_darwin_capture_2fps_smoke(tmp_path):
    """Actually grab frames on macOS (requires Screen Recording permission).

    Skipped automatically when the permission probe fails, so CI without
    the grant doesn't falsely fail.
    """
    mss = pytest.importorskip("mss")
    from PIL import Image  # noqa: F401

    from hope.capture.screen import ScreenRecordingPermissionError

    bus = EventBus()
    try:
        cap = ScreenCapture(root_dir=tmp_path, bus=bus, auto_prune=False)
        cap.start("live", fps=2)
    except ScreenRecordingPermissionError:
        pytest.skip("Screen Recording permission not granted on this host")
    time.sleep(1.05)
    summary = cap.stop("live")

    assert 1 <= summary.frames_captured <= 3
    assert summary.avg_latency_ms < 50.0, (
        f"per-frame latency too high: {summary.avg_latency_ms:.1f}ms"
    )
