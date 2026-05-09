"""Microphone capture with silero-VAD gating.

This module exposes two reusable primitives:

* :class:`MicCapture` — wraps PortAudio (``sounddevice``) to yield raw 16 kHz
  mono ``int16`` frames of a fixed size (default 32 ms / 512 samples, which is
  exactly what silero-VAD v5 expects).
* :class:`VADGatedSegmenter` — consumes frames from :class:`MicCapture` and
  emits finalized speech segments whenever silero-VAD detects a speech-onset
  followed by ``min_silence_ms`` of silence. A short pre-roll and post-roll
  buffer is retained around each segment so leading/trailing phonemes are
  not clipped.

Both primitives are designed to be driven from a background thread and to be
cheap to stop/restart — heavy model loading (silero) happens lazily inside
:meth:`VADGatedSegmenter.start`.

The module is intentionally decoupled from any specific STT backend; the
``whisper-cpp`` backend in :mod:`hope.speech.whisper_cpp` composes these
two primitives plus a faster-whisper decoder.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional

try:  # Heavy deps — both are optional, guarded at .start() time.
    import numpy as np
except ImportError:  # pragma: no cover - numpy is effectively mandatory
    np = None  # type: ignore[assignment]

try:
    import sounddevice as sd
except (ImportError, OSError):
    # OSError fires on Linux runners without the PortAudio shared lib
    # (CI, headless servers). Treat it like ImportError so module
    # import still succeeds; .start() will raise a clearer error if
    # mic capture is actually attempted.
    sd = None  # type: ignore[assignment]

try:
    # silero-vad>=5 ships a thin Python API returning a torch module.
    from silero_vad import load_silero_vad
except ImportError:
    load_silero_vad = None  # type: ignore[assignment]

try:
    import torch
except ImportError:
    torch = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — silero-vad v5 is trained on these exact numbers.
# ---------------------------------------------------------------------------

SAMPLE_RATE_HZ = 16_000
FRAME_SAMPLES = 512  # 32 ms @ 16 kHz — silero's canonical window size.
CHANNELS = 1
DTYPE = "int16"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MicFrame:
    """A single 32 ms frame of int16 mono PCM audio."""

    pcm: bytes  # Raw little-endian int16 samples, length == FRAME_SAMPLES * 2
    timestamp: float  # Monotonic capture time (seconds since epoch)


@dataclass(slots=True)
class VADConfig:
    """Tunables for the silero-VAD gate."""

    threshold: float = 0.5  # Speech probability cutoff (0..1)
    # 500 ms of trailing silence before a segment is finalized. The
    # user prefers this longer wait — it leaves room to add a clause
    # mid-thought without the segmenter chopping the utterance. End-
    # of-speech detection latency is the price; well worth it.
    min_silence_ms: int = 500
    pre_roll_ms: int = 200  # Audio retained BEFORE detected speech onset
    post_roll_ms: int = 500  # Audio retained AFTER speech offset
    min_speech_ms: int = 250  # Drop segments shorter than this (noise)
    # Hard cap on segment length. Without this, continuous background
    # audio (TV, podcast playing through the speakers, a long
    # monologue) keeps appending to the same segment for minutes —
    # whisper then chokes on the giant chunk and the user sees a wall
    # of text arrive seconds late. 6 s is enough room for a normal
    # spoken sentence and short enough that the user-perceived
    # latency stays under a second on M-series Macs.
    max_speech_ms: int = 6000


# ---------------------------------------------------------------------------
# Microphone capture
# ---------------------------------------------------------------------------


class MicCapture:
    """Background PortAudio reader that pushes 32 ms int16 frames to a queue.

    Parameters
    ----------
    device:
        PortAudio device name or index. ``None`` selects the system default
        input. Passed straight through to :class:`sounddevice.InputStream`.
    frame_queue:
        Optional externally-owned queue to publish frames on. A private queue
        is created when omitted.
    """

    def __init__(
        self,
        device: Optional[str | int] = None,
        *,
        frame_queue: Optional["queue.Queue[MicFrame]"] = None,
    ) -> None:
        self._device = device
        self._queue: queue.Queue[MicFrame] = frame_queue or queue.Queue(maxsize=256)
        self._stream: Optional["sd.RawInputStream"] = None
        self._lock = threading.Lock()
        self._running = False
        # Additive frame-tap: listeners registered via :meth:`subscribe` are
        # invoked synchronously from the PortAudio callback with every frame,
        # alongside the queue ``put``. Used by the wake-word subsystem so it
        # can peek at frames without racing ``VADGatedSegmenter`` for the
        # queue. Listeners MUST be non-blocking.
        self._frame_listeners: List[Callable[[MicFrame], None]] = []

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Open the mic stream. Idempotent + re-entrant safe."""
        with self._lock:
            if self._running:
                return
            if sd is None:
                raise ImportError(
                    "sounddevice is not installed. "
                    "Install with: uv sync --extra speech"
                )

            def _callback(indata, frames, time_info, status) -> None:  # type: ignore[no-untyped-def]
                if status:
                    logger.debug("sounddevice status: %s", status)
                frame = MicFrame(
                    pcm=bytes(indata), timestamp=time_info.inputBufferAdcTime
                )
                try:
                    self._queue.put_nowait(frame)
                except queue.Full:
                    # Drop oldest to keep latency bounded — VAD only needs ~30s.
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                # Fan out to any non-blocking listeners (e.g. clap detector).
                # Listeners own their error handling; we shield the audio
                # callback from exceptions so the mic stream never dies.
                for listener in list(self._frame_listeners):
                    try:
                        listener(frame)
                    except Exception as exc:  # pragma: no cover — defensive
                        logger.warning("mic frame listener raised: %s", exc)

            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE_HZ,
                blocksize=FRAME_SAMPLES,
                device=self._device,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=_callback,
            )
            self._stream.start()
            self._running = True

    def stop(self) -> None:
        """Close the mic stream. Idempotent."""
        with self._lock:
            if not self._running:
                return
            stream = self._stream
            self._stream = None
            self._running = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # pragma: no cover — best effort
                logger.warning("Error closing mic stream: %s", exc)

    # -- accessors ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def queue(self) -> "queue.Queue[MicFrame]":
        """Queue of captured frames (newest always appended to the right)."""
        return self._queue

    # -- frame fan-out ------------------------------------------------------

    def subscribe(self, callback: Callable[[MicFrame], None]) -> None:
        """Register a non-blocking *callback* to receive every frame.

        The callback runs synchronously on the PortAudio callback thread,
        so it MUST return quickly (no I/O, no heavy compute). Exceptions
        are caught and logged so a bad listener cannot kill the stream.
        Safe to call before or after :meth:`start`.
        """
        with self._lock:
            if callback not in self._frame_listeners:
                self._frame_listeners.append(callback)

    def unsubscribe(self, callback: Callable[[MicFrame], None]) -> None:
        """Remove a previously-registered frame listener. Idempotent."""
        with self._lock:
            try:
                self._frame_listeners.remove(callback)
            except ValueError:
                pass

    @staticmethod
    def list_devices() -> List[dict]:
        """Return PortAudio's enumerated input devices (empty if missing)."""
        if sd is None:
            return []
        return [d for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]


# ---------------------------------------------------------------------------
# VAD-gated segmenter
# ---------------------------------------------------------------------------


SegmentCallback = Callable[[bytes, float], None]
"""Callback signature: (pcm_bytes, duration_seconds) -> None."""


@dataclass
class _SegmenterState:
    """Ring-buffer + in-flight segment state for :class:`VADGatedSegmenter`."""

    pre_roll: List[bytes] = field(default_factory=list)  # last N frames of silence
    current: List[bytes] = field(default_factory=list)  # accumulating speech frames
    silence_ms: float = 0.0  # trailing silence inside an active segment
    in_speech: bool = False


class VADGatedSegmenter:
    """Run silero-VAD against a :class:`MicCapture` and emit speech segments.

    The segmenter spins its own thread on :meth:`start`, pulling frames from
    the :class:`MicCapture` queue, scoring each one with silero-VAD, and
    invoking ``on_segment(pcm, duration_s)`` whenever a finalized utterance
    is detected.

    This class owns no decoder — it is STT-backend agnostic.
    """

    def __init__(
        self,
        capture: MicCapture,
        on_segment: SegmentCallback,
        vad_config: Optional[VADConfig] = None,
    ) -> None:
        self._capture = capture
        self._on_segment = on_segment
        self._vad_cfg = vad_config or VADConfig()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._model = None  # lazy-loaded silero module
        self._pre_roll_frames = max(
            1, self._vad_cfg.pre_roll_ms // (FRAME_SAMPLES * 1000 // SAMPLE_RATE_HZ)
        )

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Load silero-vad (lazy) and spin the consumer thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        if load_silero_vad is None or torch is None or np is None:
            raise ImportError(
                "silero-vad + torch + numpy are required. "
                "Install with: uv sync --extra speech"
            )
        self._model = load_silero_vad()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="hope-vad-segmenter", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the consumer thread to exit and wait for it."""
        self._stop_event.set()
        t = self._thread
        self._thread = None
        if t is not None:
            t.join(timeout=timeout)
        # Release the torch model so RAM is reclaimed.
        self._model = None

    # -- core loop ----------------------------------------------------------

    def _run(self) -> None:
        state = _SegmenterState()
        frame_ms = FRAME_SAMPLES * 1000 / SAMPLE_RATE_HZ  # ≈ 32 ms
        while not self._stop_event.is_set():
            try:
                frame = self._capture.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                prob = self._score(frame.pcm)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("silero-vad scoring failed: %s", exc)
                continue

            if prob >= self._vad_cfg.threshold:
                self._on_speech(state, frame.pcm)
            else:
                self._on_silence(state, frame.pcm, frame_ms)

    # -- helpers ------------------------------------------------------------

    def _score(self, pcm: bytes) -> float:
        """Return silero-VAD's speech probability for one 32 ms frame."""
        assert np is not None and torch is not None and self._model is not None
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(samples)
        with torch.no_grad():
            return float(self._model(tensor, SAMPLE_RATE_HZ).item())

    def _on_speech(self, state: _SegmenterState, pcm: bytes) -> None:
        if not state.in_speech:
            state.in_speech = True
            # Prepend retained pre-roll so we don't clip the onset.
            state.current = list(state.pre_roll)
            state.pre_roll.clear()
        state.current.append(pcm)
        state.silence_ms = 0.0
        # Hard cap: if the current segment has been speech for more
        # than ``max_speech_ms``, force-finalize so whisper doesn't
        # choke on a multi-minute chunk. The next frame just starts
        # a fresh segment — no audio is lost.
        if self._vad_cfg.max_speech_ms > 0:
            current_bytes = sum(len(p) for p in state.current)
            current_ms = current_bytes / 2 / SAMPLE_RATE_HZ * 1000.0
            if current_ms >= self._vad_cfg.max_speech_ms:
                self._finalize(state)

    def _on_silence(
        self, state: _SegmenterState, pcm: bytes, frame_ms: float
    ) -> None:
        if state.in_speech:
            state.current.append(pcm)  # keep trailing audio for context
            state.silence_ms += frame_ms
            if state.silence_ms >= self._vad_cfg.min_silence_ms:
                self._finalize(state)
        else:
            # Rolling pre-roll buffer of recent silence.
            state.pre_roll.append(pcm)
            if len(state.pre_roll) > self._pre_roll_frames:
                state.pre_roll.pop(0)

    def _finalize(self, state: _SegmenterState) -> None:
        pcm = b"".join(state.current)
        duration_s = len(pcm) / 2 / SAMPLE_RATE_HZ  # 2 bytes per int16 sample
        state.current.clear()
        state.silence_ms = 0.0
        state.in_speech = False
        min_s = self._vad_cfg.min_speech_ms / 1000.0
        if duration_s < min_s:
            return  # Too short — treat as noise.
        try:
            self._on_segment(pcm, duration_s)
        except Exception as exc:  # pragma: no cover — callback must not crash us
            logger.exception("on_segment callback raised: %s", exc)


__all__ = [
    "CHANNELS",
    "DTYPE",
    "FRAME_SAMPLES",
    "MicCapture",
    "MicFrame",
    "SAMPLE_RATE_HZ",
    "SegmentCallback",
    "VADConfig",
    "VADGatedSegmenter",
]
