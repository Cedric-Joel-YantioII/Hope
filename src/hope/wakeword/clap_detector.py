"""Pure-DSP double-clap detector.

Runs entirely on the mic callback thread, so :meth:`process_frame` MUST
stay non-blocking (target: sub-millisecond per 32 ms frame).

Detection pipeline
------------------

1. Per-frame RMS → dBFS. A "transient" is declared when the running quiet
   floor estimate is below ``quiet_floor_dbfs`` *and* the current frame
   peak exceeds ``peak_dbfs``. This hysteresis stops sustained loud audio
   (music, speech) from continuously tripping the detector.
2. When a transient fires, we remember its timestamp and enter an
   "awaiting second clap" state. If a second transient arrives between
   ``min_gap_ms`` and ``max_gap_ms`` later, we call the callback. Outside
   that window we reset and treat the new transient as a fresh candidate.
3. After a successful double-clap, we enter a refractory period
   (``refractory_ms``) during which all transients are ignored — this
   suppresses echoes / rapid hand-clap bursts from retriggering.

Only dependencies: ``numpy`` and the stdlib.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Optional, Union

try:
    import numpy as np
except ImportError:  # pragma: no cover — numpy is part of the speech extra
    np = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


SAMPLE_RATE_HZ = 16_000
_INT16_FULL_SCALE = 32768.0
_EPS = 1e-12


@dataclass(slots=True)
class ClapDetectorConfig:
    """Tunables for :class:`ClapDetector`."""

    peak_dbfs: float = -20.0  # transient must exceed this (loud)
    quiet_floor_dbfs: float = -50.0  # room silence baseline (quiet)
    min_gap_ms: int = 150  # min spacing between claps (1st -> 2nd)
    max_gap_ms: int = 600  # max spacing between claps
    refractory_ms: int = 1500  # post-fire cooldown to swallow echoes
    # Peaks must be separated by >= this much "quiet" (< quiet_floor_dbfs
    # average) to count as distinct claps. Stops one long sound from
    # registering twice.
    inter_clap_silence_ms: int = 40
    sample_rate: int = SAMPLE_RATE_HZ


def _rms_dbfs(samples: "np.ndarray") -> float:
    """Return RMS of *samples* (float in [-1, 1]) expressed in dBFS."""
    if samples.size == 0:
        return -math.inf
    rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2) + _EPS))
    if rms <= 0.0:
        return -math.inf
    return 20.0 * math.log10(rms)


class ClapDetector:
    """Amplitude-transient double-clap detector.

    Parameters
    ----------
    on_clap:
        Zero-arg callable invoked when a valid double-clap fires. Called
        from whichever thread feeds :meth:`process_frame` (usually the
        PortAudio callback thread), so it MUST be non-blocking — bounce
        via a queue / event bus if it does real work.
    config:
        Tunable thresholds. ``None`` picks sane defaults.
    """

    def __init__(
        self,
        on_clap: Callable[[], None],
        config: Optional[ClapDetectorConfig] = None,
    ) -> None:
        if np is None:  # pragma: no cover — guarded by the speech extra
            raise ImportError(
                "numpy is required for ClapDetector. "
                "Install with: uv sync --extra speech"
            )
        self._on_clap = on_clap
        self._cfg = config or ClapDetectorConfig()
        # State
        self._first_clap_at: Optional[float] = None
        self._last_fire_at: float = 0.0
        self._quiet_ms_since_last_peak: float = 0.0
        # Precomputed
        self._frame_ms_fallback = 32.0  # 512 samples @ 16 kHz

    # -- API ----------------------------------------------------------------

    @property
    def config(self) -> ClapDetectorConfig:
        return self._cfg

    def reset(self) -> None:
        """Clear detector state (useful between tests / after .stop())."""
        self._first_clap_at = None
        self._last_fire_at = 0.0
        self._quiet_ms_since_last_peak = 0.0

    def process_frame(
        self,
        frame: Union[bytes, bytearray, memoryview, "np.ndarray"],
        *,
        timestamp: Optional[float] = None,
    ) -> None:
        """Feed one 32 ms int16 mono frame.

        *timestamp* is the monotonic capture time; when omitted we fall
        back to :func:`time.time`. Providing the capture time improves
        gap-measurement accuracy when frames are processed in a batch.
        """
        assert np is not None  # for type-checkers
        if isinstance(frame, (bytes, bytearray, memoryview)):
            samples = np.frombuffer(bytes(frame), dtype=np.int16)
        else:
            samples = np.asarray(frame, dtype=np.int16)
        if samples.size == 0:
            return
        now = timestamp if timestamp is not None else time.time()

        # Refractory: swallow everything (including quiet) for the first
        # `refractory_ms` after firing. This matters because a real double
        # clap in a room will produce 100-300 ms of echo ringing.
        refractory_s = self._cfg.refractory_ms / 1000.0
        if self._last_fire_at and (now - self._last_fire_at) < refractory_s:
            return

        # Normalise to float in [-1, 1] for dBFS math.
        norm = samples.astype(np.float32) / _INT16_FULL_SCALE
        peak = float(np.max(np.abs(norm)))
        if peak <= 0.0:
            peak_dbfs = -math.inf
        else:
            peak_dbfs = 20.0 * math.log10(peak + _EPS)
        rms_dbfs_val = _rms_dbfs(norm)

        # Frame duration in ms. Derived from actual sample count so the
        # detector Just Works even when fed sub-frame buffers in tests.
        frame_ms = (samples.size / float(self._cfg.sample_rate)) * 1000.0
        if frame_ms <= 0:
            frame_ms = self._frame_ms_fallback

        # A "loud transient" is a frame whose peak exceeds the peak
        # threshold *and* whose body isn't sustained loud audio. We use
        # the RMS of the frame itself as a cheap proxy for "body": a real
        # hand-clap is mostly transient so frame-RMS is typically 10-20 dB
        # below peak, whereas sustained music has peak ~= RMS.
        is_transient = (
            peak_dbfs >= self._cfg.peak_dbfs
            and (peak_dbfs - rms_dbfs_val) >= 6.0
        )

        if is_transient:
            self._handle_transient(now)
            # A transient frame cannot contribute to the silence gap used
            # to separate claps; reset the silence counter.
            self._quiet_ms_since_last_peak = 0.0
        else:
            if rms_dbfs_val <= self._cfg.quiet_floor_dbfs:
                self._quiet_ms_since_last_peak += frame_ms
            else:
                # Loud but non-transient (music, speech, room noise).
                # This breaks "is there silence between the two claps?".
                self._quiet_ms_since_last_peak = 0.0

        # Age out an unconfirmed first clap once the gap window expires.
        if self._first_clap_at is not None:
            max_gap_s = self._cfg.max_gap_ms / 1000.0
            if (now - self._first_clap_at) > max_gap_s:
                self._first_clap_at = None
                self._quiet_ms_since_last_peak = 0.0

    # -- internals ----------------------------------------------------------

    def _handle_transient(self, now: float) -> None:
        cfg = self._cfg
        if self._first_clap_at is None:
            # First clap of a potential pair.
            self._first_clap_at = now
            return

        gap_ms = (now - self._first_clap_at) * 1000.0
        if gap_ms < cfg.min_gap_ms:
            # Too close — same physical clap / bouncy echo. Keep the
            # earlier timestamp so a legitimate second clap later in the
            # window can still pair with it.
            return
        if gap_ms > cfg.max_gap_ms:
            # Too far — treat this transient as the new first-clap.
            self._first_clap_at = now
            self._quiet_ms_since_last_peak = 0.0
            return
        if self._quiet_ms_since_last_peak < cfg.inter_clap_silence_ms:
            # Two transients but no silence between them — probably one
            # sustained loud event, not two claps. Reset.
            self._first_clap_at = now
            self._quiet_ms_since_last_peak = 0.0
            return

        # Valid double-clap.
        self._first_clap_at = None
        self._last_fire_at = now
        self._quiet_ms_since_last_peak = 0.0
        try:
            self._on_clap()
        except Exception as exc:  # pragma: no cover — callback discipline
            logger.exception("clap callback raised: %s", exc)


__all__ = ["ClapDetector", "ClapDetectorConfig"]
