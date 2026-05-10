"""Neural TTS via Kokoro-ONNX — Hope's high-quality voice path.

Replaces the robotic macOS ``say`` formant synthesis with a locally-run
neural TTS (~300 MB ONNX model + 27 MB voice bank, runs on Apple Silicon
in ~1.5x real-time). The macOS ``say`` backend stays as a fallback so a
missing model file, a bad voice id, or an import failure never silences
Hope — she always speaks, even if less prettily.

Usage pattern (drop-in for :func:`hope.audio.say.say_sync`):

    from hope.audio.kokoro_tts import speak_blocking_neural
    speak_blocking_neural("Calculator's open, sir.")

Configuration (env vars, evaluated at import time for default; each call
can override):

    HOPE_VOICE           default ``bf_isabella`` (British female, composed)
    HOPE_VOICE_SPEED     default ``1.0`` (0.5–2.0)
    HOPE_VOICE_LANG      default ``en-gb``
    HOPE_KOKORO_MODEL    default ``~/.hope/kokoro/kokoro-v1.0.onnx``
    HOPE_KOKORO_VOICES   default ``~/.hope/kokoro/voices-v1.0.bin``

Model assets live at ``~/.hope/kokoro/`` so they survive daemon restarts
and aren't re-downloaded on every ``hope start``.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_HOME = Path(os.path.expanduser("~"))
_MODEL_PATH = Path(
    os.environ.get("HOPE_KOKORO_MODEL",
                   str(_HOME / ".hope" / "kokoro" / "kokoro-v1.0.onnx"))
)
_VOICES_PATH = Path(
    os.environ.get("HOPE_KOKORO_VOICES",
                   str(_HOME / ".hope" / "kokoro" / "voices-v1.0.bin"))
)
_DEFAULT_VOICE = os.environ.get("HOPE_VOICE", "bf_isabella")
_DEFAULT_SPEED = float(os.environ.get("HOPE_VOICE_SPEED", "1.0"))
_DEFAULT_LANG = os.environ.get("HOPE_VOICE_LANG", "en-gb")


# ---------------------------------------------------------------------------
# Singleton model loader — first synth is slow (ONNX session init), so we
# load once per process and hold.
# ---------------------------------------------------------------------------

_kokoro = None
_kokoro_lock = threading.Lock()
_kokoro_load_failed = False  # sticky — don't retry every call once we've failed


def _get_kokoro():
    """Return the shared :class:`Kokoro` instance or ``None`` if unusable.

    Load errors (missing model file, broken ONNX runtime, stale voice
    bank) are logged once and then the backend is marked unavailable so
    ``say_sync_neural`` falls back to ``say`` without thrashing.
    """
    global _kokoro, _kokoro_load_failed
    if _kokoro is not None:
        return _kokoro
    if _kokoro_load_failed:
        return None
    with _kokoro_lock:
        if _kokoro is not None:
            return _kokoro
        if _kokoro_load_failed:
            return None
        if not _MODEL_PATH.exists() or not _VOICES_PATH.exists():
            logger.warning(
                "kokoro: model or voices missing (model=%s, voices=%s) — "
                "falling back to macOS say",
                _MODEL_PATH, _VOICES_PATH,
            )
            _kokoro_load_failed = True
            return None
        try:
            from kokoro_onnx import Kokoro  # type: ignore[import-not-found]
            _kokoro = Kokoro(str(_MODEL_PATH), str(_VOICES_PATH))
            logger.info("kokoro: loaded model from %s", _MODEL_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.warning("kokoro: load failed (%s) — falling back to say", exc)
            _kokoro_load_failed = True
            return None
        return _kokoro


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def speak_blocking_neural(
    text: str,
    *,
    voice: Optional[str] = None,
    speed: Optional[float] = None,
    lang: Optional[str] = None,
    timeout: float = 60.0,
    on_audio_start: "Optional[Callable[[], None]]" = None,
) -> bool:
    """Synthesise *text* via Kokoro and play it through ``afplay``.

    Returns True on success (audio played end-to-end), False on any
    failure. Callers should then fall back to ``say_sync``.

    This is a BLOCKING call — returns only after playback completes, so
    the daemon's echo guard can treat the playback window as over.
    """
    if not text or not text.strip():
        return True  # trivially "done"
    if platform.system() != "Darwin":
        return False  # afplay is macOS-only
    if shutil.which("afplay") is None:
        return False

    k = _get_kokoro()
    if k is None:
        return False

    voice = voice or _DEFAULT_VOICE
    speed = speed if speed is not None else _DEFAULT_SPEED
    lang = lang or _DEFAULT_LANG

    try:
        import soundfile as sf  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.warning("kokoro: soundfile missing (%s) — falling back", exc)
        return False

    tmp = None
    try:
        samples, sr = k.create(text, voice=voice, speed=speed, lang=lang)
        # Write to a short-lived temp WAV — afplay wants a file.
        with tempfile.NamedTemporaryFile(
            suffix=".wav", prefix="hope-tts-", delete=False,
        ) as fh:
            tmp = fh.name
        sf.write(tmp, samples, sr)
        # Fire on_audio_start right before afplay runs so the daemon's
        # orb transition to "speaking" lines up with audible output, not
        # with the synth window. Best-effort: a broken callback must
        # never block playback.
        if on_audio_start is not None:
            try:
                on_audio_start()
            except Exception:
                logger.debug("kokoro: on_audio_start callback raised", exc_info=True)
        subprocess.run(  # noqa: S603
            ["afplay", tmp],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return True
    except subprocess.TimeoutExpired:
        logger.warning("kokoro: afplay timed out after %.1fs", timeout)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.exception("kokoro: synth/play failed: %s", exc)
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def is_available() -> bool:
    """True if the neural backend is ready; safe to call at startup to warm."""
    return _get_kokoro() is not None


__all__ = ["speak_blocking_neural", "is_available"]
