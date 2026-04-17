"""Always-on, on-device STT using distil-large-v3.5 via faster-whisper.

The backend is named ``whisper-cpp`` for brand parity with the upstream
OpenJarvis config surface, but under the hood it uses the CTranslate2
faster-whisper runtime, which gives us Q5-equivalent int8 quantization
with zero extra native deps on Apple Silicon. Anywhere in config where
``whisper-cpp`` appears, we load the ``distil-whisper/distil-large-v3.5``
English-only weights.

Two modes are exposed:

1. **One-shot transcription** — :meth:`WhisperCppSTT.transcribe` matches the
   :class:`~hope.speech.SpeechBackend` contract used by ``faster-whisper``,
   ``openai_whisper``, and ``deepgram`` (``bytes → TranscriptionResult``).
2. **Always-on mode** — :meth:`WhisperCppSTT.start` opens the mic + silero-VAD
   gate in a background thread and publishes a ``SPEECH_TRANSCRIPT`` event
   on :data:`hope.core.events.EventBus` for every finalized utterance.
   :meth:`stop` tears everything down and frees the model.

Heavy imports (faster-whisper, silero-vad, sounddevice, torch, numpy) are
deferred to :meth:`_ensure_model` / :meth:`start` so importing this module
stays lightweight — pytest collection must not force a model download.
"""

from __future__ import annotations

import io
import logging
import tempfile
import threading
import time
import wave
from typing import List, Optional

from hope.core.events import EventType, get_event_bus
from hope.core.registry import SpeechRegistry
from hope.speech._stubs import Segment, SpeechBackend, TranscriptionResult

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None  # type: ignore[assignment, misc]


logger = logging.getLogger(__name__)


# distil-whisper is English-only and identifies as "en"; we hard-pin it.
_DISTIL_LANG = "en"
_DEFAULT_MODEL = "distil-whisper/distil-large-v3.5"
_SAMPLE_RATE = 16_000


def _pcm16_to_wav_bytes(pcm: bytes, sample_rate: int = _SAMPLE_RATE) -> bytes:
    """Wrap a raw int16 mono PCM buffer in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # int16 = 2 bytes
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


@SpeechRegistry.register("whisper-cpp")
class WhisperCppSTT(SpeechBackend):
    """Always-on STT backend powered by distil-large-v3.5.

    The model loads lazily on the first call to :meth:`transcribe` or
    :meth:`start`, and is fully released on :meth:`stop` so the engine is
    cheap to recycle between user sessions. The class is re-entrant: calling
    ``start()`` or ``stop()`` multiple times is a no-op when already in the
    requested state.
    """

    backend_id = "whisper-cpp"

    def __init__(
        self,
        model_name: str = _DEFAULT_MODEL,
        *,
        device: str = "auto",
        compute_type: str = "int8",
        vad_threshold: float = 0.5,
        min_silence_ms: int = 500,
        input_device: Optional[str] = None,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._vad_threshold = vad_threshold
        self._min_silence_ms = min_silence_ms
        self._input_device = input_device

        self._model: Optional[WhisperModel] = None
        self._model_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._capture = None  # type: ignore[assignment] — hope.capture.mic.MicCapture
        self._segmenter = None  # type: ignore[assignment] — VADGatedSegmenter
        self._running = False

    # -- model management ---------------------------------------------------

    def _ensure_model(self) -> "WhisperModel":
        """Lazy-load distil-large-v3.5 under a lock so concurrent starts are safe."""
        with self._model_lock:
            if self._model is not None:
                return self._model
            if WhisperModel is None:
                raise ImportError(
                    "faster-whisper is not installed. "
                    "Install with: uv sync --extra speech"
                )
            logger.info(
                "Loading whisper-cpp backend (model=%s, compute_type=%s, device=%s)",
                self._model_name,
                self._compute_type,
                self._device,
            )
            self._model = WhisperModel(
                self._model_name,
                device=self._device,
                compute_type=self._compute_type,
            )
            return self._model

    def _release_model(self) -> None:
        with self._model_lock:
            self._model = None

    # -- SpeechBackend contract ---------------------------------------------

    def transcribe(
        self,
        audio: bytes,
        *,
        format: str = "wav",
        language: Optional[str] = None,
    ) -> TranscriptionResult:
        """Synchronously transcribe *audio* bytes to a TranscriptionResult.

        Distil-large-v3.5 is English-only, so any non-``en`` language hint is
        silently coerced to ``en`` — passing the real target keeps
        faster-whisper from paying the auto-detect cost.
        """
        model = self._ensure_model()
        lang = language or _DISTIL_LANG
        if lang != _DISTIL_LANG:
            logger.debug(
                "distil-large-v3.5 is English-only; coercing language=%r to 'en'",
                lang,
            )
            lang = _DISTIL_LANG

        suffix = f".{format}" if not format.startswith(".") else format
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
            tmp.write(audio)
            tmp.flush()
            segments_iter, info = model.transcribe(tmp.name, language=lang)
            segments_list = list(segments_iter)

        text = "".join(seg.text for seg in segments_list).strip()
        segments = [
            Segment(
                text=seg.text.strip(),
                start=seg.start,
                end=seg.end,
                confidence=getattr(seg, "avg_logprob", None),
            )
            for seg in segments_list
        ]
        return TranscriptionResult(
            text=text,
            language=getattr(info, "language", _DISTIL_LANG),
            confidence=getattr(info, "language_probability", None),
            duration_seconds=getattr(info, "duration", 0.0),
            segments=segments,
        )

    def health(self) -> bool:
        """``True`` when the backend is loaded or loadable."""
        if self._model is not None:
            return True
        return WhisperModel is not None

    def supported_formats(self) -> List[str]:
        """Formats faster-whisper (via ffmpeg) accepts."""
        return ["wav", "mp3", "m4a", "ogg", "flac", "webm"]

    # -- always-on lifecycle ------------------------------------------------

    def start(self) -> None:
        """Open the mic + VAD loop and load the model. Re-entrant safe."""
        with self._lifecycle_lock:
            if self._running:
                return
            # Import capture lazily — it pulls in sounddevice/silero/torch.
            from hope.capture.mic import (
                MicCapture,
                VADConfig,
                VADGatedSegmenter,
            )

            self._ensure_model()  # Fail fast if faster-whisper is missing.
            self._capture = MicCapture(device=self._input_device)
            self._segmenter = VADGatedSegmenter(
                capture=self._capture,
                on_segment=self._handle_segment,
                vad_config=VADConfig(
                    threshold=self._vad_threshold,
                    min_silence_ms=self._min_silence_ms,
                ),
            )
            self._capture.start()
            try:
                self._segmenter.start()
            except Exception:
                self._capture.stop()
                raise
            self._running = True
            logger.info("whisper-cpp always-on STT started")

    def stop(self) -> None:
        """Stop the mic loop, free silero + whisper, return to idle. Idempotent."""
        with self._lifecycle_lock:
            if not self._running:
                return
            try:
                if self._segmenter is not None:
                    self._segmenter.stop()
                if self._capture is not None:
                    self._capture.stop()
            finally:
                self._segmenter = None
                self._capture = None
                self._release_model()
                self._running = False
            logger.info("whisper-cpp always-on STT stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # -- internal: VAD segment → transcript → event -------------------------

    def _handle_segment(self, pcm: bytes, duration_s: float) -> None:
        """Called from the VAD thread for every finalized speech segment."""
        t0 = time.time()
        try:
            wav_bytes = _pcm16_to_wav_bytes(pcm, sample_rate=_SAMPLE_RATE)
            result = self.transcribe(wav_bytes, format="wav", language=_DISTIL_LANG)
        except Exception as exc:
            logger.exception("whisper-cpp transcription failed: %s", exc)
            return

        text = result.text.strip()
        if not text:
            return  # VAD triggered on non-speech; nothing to publish.

        payload = {
            "text": text,
            "confidence": result.confidence,
            "lang": result.language or _DISTIL_LANG,
            "timestamp": t0,
            "duration_ms": int(duration_s * 1000),
        }
        # EventBus.publish is fast (synchronous fan-out in-lock), so calling it
        # from the VAD thread stays non-blocking for the mic callback, which
        # is producing into a separate bounded queue.
        try:
            get_event_bus().publish(EventType.SPEECH_TRANSCRIPT, payload)
        except Exception as exc:  # pragma: no cover — subscribers must not kill us
            logger.exception("SPEECH_TRANSCRIPT publish failed: %s", exc)


__all__ = ["WhisperCppSTT"]
