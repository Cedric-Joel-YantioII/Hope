"""End-to-end tests for :class:`hope.wakeword.wake_monitor.WakeMonitor`.

MicCapture is mocked — we drive clap frames straight into the monitor's
registered listener.  SPEECH_TRANSCRIPT events are published on the bus
to exercise the voice path.
"""

from __future__ import annotations

import time
from typing import Callable, List

import numpy as np
import pytest

from hope.core.config import WakeConfig
from hope.core.events import Event, EventBus, EventType
from hope.wakeword import WakeMonitor, WakeSource

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512
FRAME_MS = 32


# ---------------------------------------------------------------------------
# Fake MicCapture — minimal subscribe/unsubscribe contract.
# ---------------------------------------------------------------------------


class _FakeMicFrame:
    __slots__ = ("pcm", "timestamp")

    def __init__(self, pcm: bytes, timestamp: float) -> None:
        self.pcm = pcm
        self.timestamp = timestamp


class _FakeMicCapture:
    """Implements the subscribe/unsubscribe frame-fanout contract only."""

    def __init__(self) -> None:
        self._listeners: List[Callable[[_FakeMicFrame], None]] = []

    def subscribe(self, cb: Callable[[_FakeMicFrame], None]) -> None:
        if cb not in self._listeners:
            self._listeners.append(cb)

    def unsubscribe(self, cb: Callable[[_FakeMicFrame], None]) -> None:
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    def emit(self, pcm: bytes, timestamp: float) -> None:
        frame = _FakeMicFrame(pcm=pcm, timestamp=timestamp)
        for cb in list(self._listeners):
            cb(frame)

    @property
    def listener_count(self) -> int:
        return len(self._listeners)


# ---------------------------------------------------------------------------
# Audio synthesis helpers (mirrors test_clap_detector)
# ---------------------------------------------------------------------------


def _silence_frame() -> bytes:
    rng = np.random.default_rng(42)
    rms = 10 ** (-70.0 / 20.0)
    return (rng.standard_normal(FRAME_SAMPLES) * rms * 32768.0).astype(np.int16).tobytes()


def _clap_frame(peak_dbfs: float = -6.0) -> bytes:
    peak = min(int(32767 * (10 ** (peak_dbfs / 20.0))), 32767)
    samples = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    impulse_len = 32
    pattern = np.array([peak, -peak] * (impulse_len // 2), dtype=np.int16)
    samples[:impulse_len] = pattern
    return samples.tobytes()


def _feed_clap_sequence(mic: _FakeMicCapture, *, gap_ms: int, start_ts: float) -> float:
    """Emit silence -> clap -> silence -> clap -> silence. Returns final timestamp."""
    ts = start_ts
    step = FRAME_MS / 1000.0
    # leading silence
    for _ in range(5):
        mic.emit(_silence_frame(), ts)
        ts += step
    # first clap
    mic.emit(_clap_frame(), ts)
    ts += step
    # gap silence
    for _ in range(max(1, gap_ms // FRAME_MS)):
        mic.emit(_silence_frame(), ts)
        ts += step
    # second clap
    mic.emit(_clap_frame(), ts)
    ts += step
    # trailing silence
    for _ in range(3):
        mic.emit(_silence_frame(), ts)
        ts += step
    return ts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bus() -> EventBus:
    return EventBus(record_history=True)


@pytest.fixture
def mic() -> _FakeMicCapture:
    return _FakeMicCapture()


@pytest.fixture
def triggers(bus: EventBus) -> List[Event]:
    collected: List[Event] = []

    def _subscriber(event: Event) -> None:
        collected.append(event)

    bus.subscribe(EventType.WAKE_TRIGGER, _subscriber)
    return collected


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_clap_path_fires_wake_trigger(
    bus: EventBus, mic: _FakeMicCapture, triggers: List[Event]
) -> None:
    monitor = WakeMonitor(bus, config=WakeConfig(), mic_capture=mic)
    monitor.start()
    assert monitor.is_monitoring
    assert mic.listener_count == 1

    _feed_clap_sequence(mic, gap_ms=300, start_ts=1000.0)

    assert len(triggers) == 1
    evt = triggers[0]
    assert evt.event_type == EventType.WAKE_TRIGGER
    assert evt.data["source"] == WakeSource.CLAP.value
    assert evt.data["text"] is None
    assert isinstance(evt.data["timestamp"], float)
    monitor.stop()


def test_voice_path_fires_wake_trigger(
    bus: EventBus, mic: _FakeMicCapture, triggers: List[Event]
) -> None:
    monitor = WakeMonitor(bus, config=WakeConfig(), mic_capture=mic)
    monitor.start()

    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {
            "text": "Hey Hope, what's up?",
            "confidence": 0.92,
            "lang": "en",
            "timestamp": 0.0,
            "duration_ms": 700,
        },
    )

    assert len(triggers) == 1
    evt = triggers[0]
    assert evt.data["source"] == WakeSource.VOICE.value
    assert evt.data["text"] == "Hey Hope, what's up?"
    assert isinstance(evt.data["timestamp"], float)
    monitor.stop()


def test_refractory_suppresses_second_trigger_within_window(
    bus: EventBus, mic: _FakeMicCapture, triggers: List[Event]
) -> None:
    cfg = WakeConfig(refractory_sec=3.0)
    monitor = WakeMonitor(bus, config=cfg, mic_capture=mic)
    monitor.start()

    # Fire once via voice.
    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {"text": "hey hope", "confidence": 0.9, "lang": "en", "timestamp": 0.0},
    )
    assert len(triggers) == 1

    # Immediately follow with a clap — should be suppressed by refractory.
    _feed_clap_sequence(mic, gap_ms=300, start_ts=time.time())
    assert len(triggers) == 1

    # And another voice phrase — still suppressed.
    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {"text": "wake up hope", "confidence": 0.9, "lang": "en", "timestamp": 0.0},
    )
    assert len(triggers) == 1
    monitor.stop()


def test_start_stop_idempotent(bus: EventBus, mic: _FakeMicCapture) -> None:
    monitor = WakeMonitor(bus, config=WakeConfig(), mic_capture=mic)
    monitor.start()
    monitor.start()  # should no-op, not double-subscribe
    assert mic.listener_count == 1
    monitor.stop()
    monitor.stop()  # should no-op
    assert not monitor.is_monitoring
    assert mic.listener_count == 0


def test_stop_unsubscribes_voice_path(
    bus: EventBus, mic: _FakeMicCapture, triggers: List[Event]
) -> None:
    monitor = WakeMonitor(bus, config=WakeConfig(), mic_capture=mic)
    monitor.start()
    monitor.stop()

    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {"text": "hey hope", "confidence": 0.9, "lang": "en", "timestamp": 0.0},
    )
    # Nothing should fire post-stop.
    assert triggers == []


def test_disabled_config_does_not_start(
    bus: EventBus, mic: _FakeMicCapture, triggers: List[Event]
) -> None:
    cfg = WakeConfig(enabled=False)
    monitor = WakeMonitor(bus, config=cfg, mic_capture=mic)
    monitor.start()

    # No subscriber wired up.
    assert mic.listener_count == 0
    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {"text": "hey hope", "confidence": 0.9, "lang": "en", "timestamp": 0.0},
    )
    assert triggers == []


def test_clap_disabled_leaves_voice_working(
    bus: EventBus, mic: _FakeMicCapture, triggers: List[Event]
) -> None:
    cfg = WakeConfig(clap_enabled=False)
    monitor = WakeMonitor(bus, config=cfg, mic_capture=mic)
    monitor.start()

    # No clap listener wired.
    assert mic.listener_count == 0
    _feed_clap_sequence(mic, gap_ms=300, start_ts=1000.0)
    assert triggers == []

    # Voice still works.
    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {"text": "ok hope", "confidence": 0.9, "lang": "en", "timestamp": 0.0},
    )
    assert len(triggers) == 1
    assert triggers[0].data["source"] == WakeSource.VOICE.value
    monitor.stop()


def test_refractory_releases_eventually(
    bus: EventBus, mic: _FakeMicCapture, triggers: List[Event]
) -> None:
    # Use a tiny refractory so the test is fast.
    cfg = WakeConfig(refractory_sec=0.05)
    monitor = WakeMonitor(bus, config=cfg, mic_capture=mic)
    monitor.start()

    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {"text": "hey hope", "confidence": 0.9, "lang": "en", "timestamp": 0.0},
    )
    time.sleep(0.1)  # clear the 50 ms refractory
    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {"text": "wake up hope", "confidence": 0.9, "lang": "en", "timestamp": 0.0},
    )
    assert len(triggers) == 2
    monitor.stop()


def test_payload_shape_contract(
    bus: EventBus, mic: _FakeMicCapture, triggers: List[Event]
) -> None:
    """Pin the handshake contract: exactly these keys on WAKE_TRIGGER."""
    monitor = WakeMonitor(bus, config=WakeConfig(), mic_capture=mic)
    monitor.start()

    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {"text": "wake up hope", "confidence": 0.9, "lang": "en", "timestamp": 0.0},
    )
    assert len(triggers) == 1
    data = triggers[0].data
    assert set(data.keys()) == {"source", "text", "timestamp"}
    assert data["source"] in ("voice", "clap")
    assert data["text"] is None or isinstance(data["text"], str)
    assert isinstance(data["timestamp"], float)
    monitor.stop()
