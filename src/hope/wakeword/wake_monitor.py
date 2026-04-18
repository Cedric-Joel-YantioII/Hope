"""Coordinator that unifies clap + spoken-phrase detection into WAKE_TRIGGER events.

The daemon / CLI binds to this class via the handshake contract:

* ``WakeMonitor(bus, *, config=None)``
* ``.start()`` — non-blocking, idempotent, subscribes detectors.
* ``.stop()`` — idempotent, clean teardown.
* ``.is_monitoring`` — bool property.
* Publishes :data:`hope.core.events.EventType.WAKE_TRIGGER` on either
  detection path with payload ``{"source": "voice"|"clap",
  "text": Optional[str], "timestamp": float}``.

A post-fire refractory window (default 3 s, configurable via
``WakeConfig.refractory_sec``) suppresses retriggers, so a single user
event never publishes two WAKE_TRIGGERs (even if both the clap and voice
paths would have fired).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from hope.core.config import WakeConfig
from hope.core.events import EventBus, EventType

from .clap_detector import ClapDetector, ClapDetectorConfig
from .phrase_matcher import PhraseMatcher


logger = logging.getLogger(__name__)


class WakeMonitor:
    """Orchestrates clap + voice wake detection, publishes WAKE_TRIGGER.

    The monitor owns no mic hardware on its own — it attaches to an
    externally-owned :class:`hope.capture.mic.MicCapture` (typically the
    one already spun up by the always-on whisper-cpp backend) via the
    capture's ``subscribe`` fan-out hook. If the caller does not provide a
    capture, the monitor will discover one lazily on :meth:`start` by
    importing :class:`hope.capture.mic.MicCapture` and spinning its own —
    but the expected deployment path is to share the STT's capture to
    avoid double-opening the PortAudio device.

    Parameters
    ----------
    bus:
        Event bus to subscribe against and publish to.
    config:
        Optional :class:`WakeConfig`. Defaults are taken from
        :class:`WakeConfig` if omitted.
    mic_capture:
        Optional pre-existing :class:`MicCapture` to attach the clap
        detector to. When provided, :meth:`start`/:meth:`stop` will *not*
        start/stop the capture itself — only subscribe/unsubscribe the
        clap detector. Pass ``None`` to have the monitor manage its own
        capture lifecycle.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        config: Optional[WakeConfig] = None,
        mic_capture: Optional[object] = None,  # MicCapture; kept loose to avoid import
    ) -> None:
        self._bus = bus
        self._cfg = config or WakeConfig()
        self._mic_capture = mic_capture
        self._owns_capture = mic_capture is None
        self._clap: Optional[ClapDetector] = None
        self._phrase: Optional[PhraseMatcher] = None
        self._monitoring = False
        self._lock = threading.Lock()
        self._last_fire_at: float = 0.0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Wire up detectors. Non-blocking, idempotent."""
        with self._lock:
            if self._monitoring:
                return
            if not self._cfg.enabled:
                logger.info("WakeMonitor disabled via config; skipping start")
                return

            # --- clap path ---------------------------------------------------
            if self._cfg.clap_enabled:
                clap_cfg = ClapDetectorConfig(
                    peak_dbfs=self._cfg.clap_min_peak_dbfs,
                    quiet_floor_dbfs=self._cfg.clap_quiet_floor_dbfs,
                    min_gap_ms=self._cfg.clap_min_gap_ms,
                    max_gap_ms=self._cfg.clap_max_gap_ms,
                )
                self._clap = ClapDetector(on_clap=self._on_clap, config=clap_cfg)
                capture = self._get_or_create_capture()
                if capture is not None:
                    capture.subscribe(self._on_mic_frame)

            # --- voice path --------------------------------------------------
            self._phrase = PhraseMatcher(
                bus=self._bus,
                on_match=self._on_phrase,
                phrases=self._cfg.phrases,
                min_confidence=self._cfg.voice_min_confidence,
            )
            self._phrase.start()

            self._monitoring = True
            logger.info(
                "WakeMonitor started (clap=%s, phrases=%d)",
                self._cfg.clap_enabled,
                len(self._cfg.phrases),
            )

    def stop(self) -> None:
        """Unsubscribe everything. Idempotent."""
        with self._lock:
            if not self._monitoring:
                return
            if self._phrase is not None:
                self._phrase.stop()
                self._phrase = None
            if self._mic_capture is not None and self._clap is not None:
                try:
                    self._mic_capture.unsubscribe(self._on_mic_frame)  # type: ignore[attr-defined]
                except Exception as exc:  # pragma: no cover — best effort
                    logger.debug("mic unsubscribe failed: %s", exc)
            if self._owns_capture and self._mic_capture is not None:
                try:
                    self._mic_capture.stop()  # type: ignore[attr-defined]
                except Exception as exc:  # pragma: no cover — best effort
                    logger.debug("mic stop failed: %s", exc)
                self._mic_capture = None
            self._clap = None
            self._monitoring = False
            logger.info("WakeMonitor stopped")

    @property
    def is_monitoring(self) -> bool:
        return self._monitoring

    # -- internals ----------------------------------------------------------

    def _get_or_create_capture(self) -> Optional[object]:
        """Return the provided capture, or lazily create one if we own it."""
        if self._mic_capture is not None:
            return self._mic_capture
        try:
            # Lazy import — pulls sounddevice in. Tests inject a capture.
            from hope.capture.mic import MicCapture

            capture = MicCapture()
            capture.start()
            self._mic_capture = capture
            self._owns_capture = True
            return capture
        except Exception as exc:
            logger.warning(
                "WakeMonitor could not open MicCapture (%s); clap detection disabled",
                exc,
            )
            return None

    def _on_mic_frame(self, frame: object) -> None:
        """Mic callback-thread entry: feed the clap detector."""
        if self._clap is None:
            return
        pcm = getattr(frame, "pcm", None)
        ts = getattr(frame, "timestamp", None)
        if pcm is None:
            return
        self._clap.process_frame(pcm, timestamp=ts)

    # -- detector callbacks -------------------------------------------------

    def _on_clap(self) -> None:
        self._fire(source="clap", text=None)

    def _on_phrase(self, text: str) -> None:
        self._fire(source="voice", text=text)

    def _fire(self, *, source: str, text: Optional[str]) -> None:
        now = time.time()
        if (now - self._last_fire_at) < self._cfg.refractory_sec:
            # Suppress retrigger (either detector firing twice in a row, or
            # the two paths firing near-simultaneously on the same event).
            return
        self._last_fire_at = now
        payload = {"source": source, "text": text, "timestamp": now}
        try:
            self._bus.publish(EventType.WAKE_TRIGGER, payload)
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("WAKE_TRIGGER publish failed: %s", exc)


__all__ = ["WakeMonitor"]
