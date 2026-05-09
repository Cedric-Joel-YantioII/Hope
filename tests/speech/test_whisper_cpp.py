"""Tests for the whisper-cpp (distil-large-v3.5) STT backend.

These tests never touch a real microphone or ship weights to disk — all
heavy collaborators (``WhisperModel``, ``sounddevice``, ``silero-vad``) are
monkey-patched. The goal is to exercise:

1. Registry wiring under the ``whisper-cpp`` key.
2. The one-shot ``transcribe(bytes) → TranscriptionResult`` contract.
3. The always-on pipeline end-to-end: a canned PCM buffer is handed to the
   segmenter callback, and the test verifies that the backend publishes a
   ``SPEECH_TRANSCRIPT`` event on the global :class:`EventBus` with the
   fields listed in the Hope spec (``text``, ``confidence``, ``lang``,
   ``timestamp``, ``duration_ms``).
"""

from __future__ import annotations

import io
import wave
from unittest.mock import MagicMock, patch

import pytest

from hope.core.events import EventType, get_event_bus, reset_event_bus
from hope.core.registry import SpeechRegistry
from hope.speech._stubs import TranscriptionResult
from hope.speech.whisper_cpp import WhisperCppSTT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wav_bytes(duration_s: float = 1.0, sample_rate: int = 16_000) -> bytes:
    """Return a silent mono int16 WAV blob (header + samples)."""
    n_samples = int(duration_s * sample_rate)
    pcm = b"\x00\x00" * n_samples
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _make_pcm_bytes(duration_s: float = 1.0, sample_rate: int = 16_000) -> bytes:
    """Return a silent raw int16 PCM buffer (no container)."""
    return b"\x00\x00" * int(duration_s * sample_rate)


def _mock_whisper_model(text: str = "Hello Hope") -> MagicMock:
    """Build a faster-whisper-shaped mock that yields one segment."""
    seg = MagicMock()
    seg.text = f" {text}"
    seg.start = 0.0
    seg.end = 1.0
    seg.avg_logprob = -0.25

    info = MagicMock()
    info.language = "en"
    info.language_probability = 0.97
    info.duration = 1.0

    model = MagicMock()
    model.transcribe.return_value = ([seg], info)
    return model


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_bus():
    """Each test gets a fresh EventBus singleton with history recording on."""
    reset_event_bus()
    bus = get_event_bus(record_history=True)
    yield bus
    reset_event_bus()


@pytest.fixture(autouse=True)
def _register_backend():
    """Keep the backend registered even if an earlier test cleared the registry."""
    if not SpeechRegistry.contains("whisper-cpp"):
        SpeechRegistry.register_value("whisper-cpp", WhisperCppSTT)


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_whisper_cpp_registers() -> None:
    assert SpeechRegistry.contains("whisper-cpp")
    assert SpeechRegistry.get("whisper-cpp") is WhisperCppSTT


def test_supported_formats_includes_wav() -> None:
    # Bypass __init__ — we just want to exercise the pure-method surface.
    backend = WhisperCppSTT.__new__(WhisperCppSTT)
    formats = backend.supported_formats()
    assert "wav" in formats
    assert "mp3" in formats


def test_backend_id() -> None:
    assert WhisperCppSTT.backend_id == "whisper-cpp"


# ---------------------------------------------------------------------------
# One-shot transcription
# ---------------------------------------------------------------------------


def test_transcribe_returns_transcription_result() -> None:
    mock_model = _mock_whisper_model("hello world")
    with patch(
        "hope.speech.whisper_cpp.WhisperModel", return_value=mock_model
    ):
        backend = WhisperCppSTT(compute_type="int8", device="cpu")
        result = backend.transcribe(_make_wav_bytes())
    assert isinstance(result, TranscriptionResult)
    assert result.text == "hello world"
    assert result.language == "en"
    assert result.duration_seconds == 1.0
    # Distil-large-v3.5 is English-only — the backend must force language='en'.
    mock_model.transcribe.assert_called_once()
    _, kwargs = mock_model.transcribe.call_args
    assert kwargs.get("language") == "en"


def test_transcribe_coerces_non_english_language() -> None:
    mock_model = _mock_whisper_model("bonjour")
    with patch(
        "hope.speech.whisper_cpp.WhisperModel", return_value=mock_model
    ):
        backend = WhisperCppSTT()
        backend.transcribe(_make_wav_bytes(), language="fr")
    _, kwargs = mock_model.transcribe.call_args
    assert kwargs["language"] == "en"


def test_health_reflects_dep_availability() -> None:
    with patch("hope.speech.whisper_cpp.WhisperModel", new=None):
        backend = WhisperCppSTT.__new__(WhisperCppSTT)
        backend._model = None
        assert backend.health() is False

    with patch("hope.speech.whisper_cpp.WhisperModel", new=object):
        backend = WhisperCppSTT.__new__(WhisperCppSTT)
        backend._model = None
        assert backend.health() is True


# ---------------------------------------------------------------------------
# Always-on pipeline: VAD segment → SPEECH_TRANSCRIPT event
# ---------------------------------------------------------------------------


def test_handle_segment_publishes_speech_transcript_event() -> None:
    """Feed a canned PCM buffer into the segment callback and verify the event.

    This bypasses the mic / silero stack entirely — the VAD gate's only job
    in production is to call ``_handle_segment``, and this test proves that
    once it does, the backend correctly transcribes and publishes an event
    with every field required by the Hope spec.
    """
    mock_model = _mock_whisper_model("good morning sir")
    received: list = []

    bus = get_event_bus()
    bus.subscribe(EventType.SPEECH_TRANSCRIPT, received.append)

    with patch(
        "hope.speech.whisper_cpp.WhisperModel", return_value=mock_model
    ):
        backend = WhisperCppSTT()
        # Directly invoke the segment callback with raw PCM, exactly like
        # VADGatedSegmenter would after finalizing a speech segment.
        backend._handle_segment(_make_pcm_bytes(duration_s=1.5), duration_s=1.5)

    assert len(received) == 1
    event = received[0]
    assert event.event_type == EventType.SPEECH_TRANSCRIPT
    payload = event.data
    assert payload["text"] == "good morning sir"
    assert payload["lang"] == "en"
    assert payload["confidence"] == pytest.approx(0.97)
    assert payload["duration_ms"] == 1500
    assert isinstance(payload["timestamp"], float)


def test_empty_transcript_is_not_published() -> None:
    """Silence-like segments must not leak empty events onto the bus."""
    mock_model = _mock_whisper_model(text="")
    # faster-whisper returns an empty iterator when it finds no speech;
    # overwrite the transcribe mock to reflect that precisely.
    mock_model.transcribe.return_value = ([], MagicMock(
        language="en", language_probability=0.0, duration=0.5
    ))
    bus = get_event_bus()

    with patch(
        "hope.speech.whisper_cpp.WhisperModel", return_value=mock_model
    ):
        backend = WhisperCppSTT()
        backend._handle_segment(_make_pcm_bytes(duration_s=0.5), duration_s=0.5)

    events = [e for e in bus.history if e.event_type == EventType.SPEECH_TRANSCRIPT]
    assert events == []


# ---------------------------------------------------------------------------
# Lifecycle: start/stop is re-entrant and releases the model
# ---------------------------------------------------------------------------


def test_start_stop_is_reentrant_and_releases_model() -> None:
    """start() twice is idempotent; stop() frees the model handle."""
    mock_model = _mock_whisper_model()
    fake_capture = MagicMock()
    fake_segmenter = MagicMock()

    with patch(
        "hope.speech.whisper_cpp.WhisperModel", return_value=mock_model
    ), patch(
        "hope.capture.mic.MicCapture", return_value=fake_capture
    ), patch(
        "hope.capture.mic.VADGatedSegmenter", return_value=fake_segmenter
    ):
        backend = WhisperCppSTT()
        backend.start()
        assert backend.is_running is True
        # Second start must be a no-op — not re-instantiate capture.
        backend.start()
        fake_capture.start.assert_called_once()
        fake_segmenter.start.assert_called_once()

        backend.stop()
        assert backend.is_running is False
        fake_segmenter.stop.assert_called_once()
        fake_capture.stop.assert_called_once()
        # Model handle released so RAM is reclaimed.
        assert backend._model is None

        # Second stop is also a no-op.
        backend.stop()
        assert fake_segmenter.stop.call_count == 1
