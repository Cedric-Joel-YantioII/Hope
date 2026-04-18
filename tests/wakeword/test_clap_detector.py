"""Unit tests for :class:`hope.wakeword.clap_detector.ClapDetector`.

Audio is entirely synthesised — no mic, no files. Each test builds a
stream of 32 ms int16 mono frames (matching the real MicCapture contract)
and feeds them frame-by-frame into the detector.
"""

from __future__ import annotations

from typing import List

import numpy as np
import pytest

from hope.wakeword.clap_detector import ClapDetector, ClapDetectorConfig


SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512  # 32 ms at 16 kHz
FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE  # 32


# ---------------------------------------------------------------------------
# Synthesis helpers
# ---------------------------------------------------------------------------


def _silence_frame(amplitude_dbfs: float = -70.0) -> bytes:
    """Near-silent noise frame at the given RMS dBFS."""
    rms = 10 ** (amplitude_dbfs / 20.0)
    # Use a small deterministic seed so tests are reproducible.
    rng = np.random.default_rng(42)
    samples = (rng.standard_normal(FRAME_SAMPLES) * rms * 32768.0).astype(np.int16)
    return samples.tobytes()


def _clap_frame(peak_dbfs: float = -6.0) -> bytes:
    """Single-frame transient: big impulse surrounded by zeros.

    A real hand-clap lasts 2-10 ms (32-160 samples at 16 kHz), so an
    impulse occupying the first ~2 ms of a 32 ms frame gives us:
    * a very high peak (matches >= -20 dBFS threshold)
    * a much lower frame-wide RMS (peak-RMS gap > 6 dB, passing the
      transient check)
    """
    peak = min(int(32767 * (10 ** (peak_dbfs / 20.0))), 32767)
    samples = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    # 2 ms = 32 samples of bipolar impulse
    impulse_len = 32
    pattern = np.array([peak, -peak] * (impulse_len // 2), dtype=np.int16)
    samples[:impulse_len] = pattern
    return samples.tobytes()


def _sustained_loud_frame(rms_dbfs: float = -10.0) -> bytes:
    """Music-like: full-frame sinewave with peak ≈ RMS."""
    rng = np.random.default_rng(7)
    rms = 10 ** (rms_dbfs / 20.0)
    samples = (rng.standard_normal(FRAME_SAMPLES) * rms * 32768.0).astype(np.int16)
    return samples.tobytes()


def _feed_frames(det: ClapDetector, frames: List[bytes], start_ts: float = 1000.0) -> None:
    """Feed frames with evenly-spaced timestamps, 32 ms apart."""
    ts = start_ts
    for f in frames:
        det.process_frame(f, timestamp=ts)
        ts += FRAME_MS / 1000.0


def _double_clap_sequence(
    gap_ms: int,
    leading_silence_frames: int = 10,
    trailing_silence_frames: int = 10,
) -> List[bytes]:
    """Build: [silence...][CLAP][silence gap][CLAP][silence...]."""
    gap_frames = max(1, gap_ms // FRAME_MS)
    silence = [_silence_frame() for _ in range(leading_silence_frames)]
    between = [_silence_frame() for _ in range(gap_frames)]
    tail = [_silence_frame() for _ in range(trailing_silence_frames)]
    return silence + [_clap_frame()] + between + [_clap_frame()] + tail


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def fires() -> List[float]:
    """List the detector will append to on each clap."""
    return []


@pytest.fixture
def detector(fires: List[float]) -> ClapDetector:
    def _on_clap() -> None:
        fires.append(1.0)

    return ClapDetector(
        on_clap=_on_clap,
        config=ClapDetectorConfig(
            peak_dbfs=-20.0,
            quiet_floor_dbfs=-50.0,
            min_gap_ms=150,
            max_gap_ms=600,
            refractory_ms=1500,
        ),
    )


def test_double_clap_fires_once(detector: ClapDetector, fires: List[float]) -> None:
    _feed_frames(detector, _double_clap_sequence(gap_ms=300))
    assert len(fires) == 1


def test_single_clap_does_not_fire(detector: ClapDetector, fires: List[float]) -> None:
    frames = [_silence_frame() for _ in range(10)]
    frames.append(_clap_frame())
    frames.extend(_silence_frame() for _ in range(20))
    _feed_frames(detector, frames)
    assert fires == []


def test_claps_too_close_do_not_fire(
    detector: ClapDetector, fires: List[float]
) -> None:
    # 64 ms gap is below the 150 ms minimum — should not fire.
    _feed_frames(detector, _double_clap_sequence(gap_ms=64))
    assert fires == []


def test_claps_too_far_do_not_fire(
    detector: ClapDetector, fires: List[float]
) -> None:
    # 1000 ms gap is above the 600 ms maximum — should not fire.
    _feed_frames(detector, _double_clap_sequence(gap_ms=1000))
    assert fires == []


def test_refractory_suppresses_retrigger(
    detector: ClapDetector, fires: List[float]
) -> None:
    # Two back-to-back double-claps, 500 ms apart (well under 1500 ms refractory).
    seq = _double_clap_sequence(gap_ms=300, trailing_silence_frames=15)
    # 15 frames ≈ 480 ms — inside refractory window.
    seq.extend(_double_clap_sequence(gap_ms=300, leading_silence_frames=0))
    _feed_frames(detector, seq)
    # First double-clap fires; second is suppressed by refractory.
    assert len(fires) == 1


def test_refractory_releases_after_window(
    detector: ClapDetector, fires: List[float]
) -> None:
    # Feed two double-claps more than 1500 ms apart — both should fire.
    _feed_frames(detector, _double_clap_sequence(gap_ms=250, trailing_silence_frames=60))
    # 60 frames ≈ 1920 ms — past the 1500 ms refractory.
    _feed_frames(
        detector,
        _double_clap_sequence(gap_ms=250, leading_silence_frames=0),
        start_ts=1000.0 + (10 + 8 + 60) * FRAME_MS / 1000.0 + 3.0,
    )
    assert len(fires) == 2


def test_sustained_loud_audio_does_not_fire(
    detector: ClapDetector, fires: List[float]
) -> None:
    # Music / speech: peak ≈ RMS, so the transient gate rejects every frame.
    frames = [_sustained_loud_frame() for _ in range(60)]
    _feed_frames(detector, frames)
    assert fires == []


def test_latency_under_100ms(detector: ClapDetector) -> None:
    """Time from the second clap frame being fed to the callback firing.

    The detector is pure Python/numpy, so latency is dominated by feeding
    one frame (which is the natural mic cadence, 32 ms). We measure the
    *compute* delta between feeding the second-clap frame and the
    callback firing — this must be well under 100 ms per the spec.
    """
    import time

    fire_times: List[float] = []

    def _on_clap() -> None:
        fire_times.append(time.perf_counter())

    d = ClapDetector(on_clap=_on_clap)
    frames = _double_clap_sequence(gap_ms=300)
    # Feed all but the last clap frame.
    # Sequence is: 10 silent + clap + gap_silence + clap + 10 silent.
    # We stop one frame before the second clap and time that frame.
    stop_index = 10 + 1 + (300 // FRAME_MS)  # last index before the 2nd clap
    ts = 1000.0
    for f in frames[:stop_index]:
        d.process_frame(f, timestamp=ts)
        ts += FRAME_MS / 1000.0

    before = time.perf_counter()
    d.process_frame(frames[stop_index], timestamp=ts)  # the 2nd clap
    after = time.perf_counter()

    assert len(fire_times) == 1
    latency_ms = (after - before) * 1000
    # Compute latency on the clap frame itself must be way under 100 ms.
    assert latency_ms < 100, f"detector took {latency_ms:.2f}ms per frame"


def test_reset_clears_state(detector: ClapDetector, fires: List[float]) -> None:
    # First clap of a would-be pair.
    _feed_frames(detector, [_silence_frame(), _clap_frame()])
    detector.reset()
    # A lone clap after reset should not pair with the pre-reset clap.
    _feed_frames(detector, [_silence_frame() for _ in range(20)], start_ts=2000.0)
    _feed_frames(detector, [_clap_frame()], start_ts=2000.0 + 0.7)
    _feed_frames(detector, [_silence_frame() for _ in range(20)], start_ts=2000.0 + 0.8)
    assert fires == []
