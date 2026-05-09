"""Non-blocking text-to-speech acknowledgment via macOS ``/usr/bin/say``.

On non-Darwin hosts this is a no-op with a debug log — Hope's daemon
can run on Linux for CI / dev without a crashing ``say`` dependency.
The call is fully non-blocking: we :func:`subprocess.Popen` the
``say`` binary and return immediately. Errors (missing binary,
permission denied, …) are swallowed so a TTS hiccup can never tear
down the wake/sleep handlers that call this.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess

logger = logging.getLogger(__name__)

_SAY_BINARY = "/usr/bin/say"

# Default macOS voice — Samantha is smoother than the stock voice used
# when no ``-v`` is given. Users can override via the ``HOPE_VOICE``
# env var (e.g. ``HOPE_VOICE=Ava`` once the premium voice is installed
# via System Settings → Accessibility → Spoken Content).
import os as _os  # noqa: E402 — keep import close to its use

_DEFAULT_VOICE = _os.environ.get("HOPE_VOICE", "Shelley")
# Words-per-minute rate. ``say`` default is ~175; we nudge a bit higher
# so Hope sounds alert instead of droning.
_DEFAULT_RATE = _os.environ.get("HOPE_VOICE_RATE", "200")


def say(text: str) -> None:
    """Speak *text* aloud on macOS; log-only elsewhere.

    Non-blocking. Safe to call from any thread. Never raises.
    """
    if not text:
        return

    system = platform.system()
    if system != "Darwin":
        logger.debug("say() no-op on %s: %r", system, text)
        return

    binary = _SAY_BINARY if shutil.which(_SAY_BINARY) else shutil.which("say")
    if binary is None:
        logger.debug("say() skipped — 'say' binary not on PATH: %r", text)
        return

    try:
        subprocess.Popen(  # noqa: S603 — fixed binary path, text is a literal
            [binary, "-v", _DEFAULT_VOICE, "-r", _DEFAULT_RATE, text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:  # pragma: no cover — defensive
        logger.debug("say() Popen failed: %s", exc)


def say_sync(text: str, timeout: float = 120.0) -> None:
    """Speak *text* and BLOCK until playback exits.

    Two-tier backend:
      1. :mod:`hope.audio.kokoro_tts` — local neural TTS (~1.5x real-time on
         Apple Silicon). The quality path. Used when the model files exist
         under ``~/.hope/kokoro/`` and ``kokoro-onnx`` is importable.
      2. macOS ``/usr/bin/say`` — the robotic-but-always-there fallback.

    Behaviour on non-Darwin (or without ``say`` installed) matches
    :func:`say`: a debug log and a silent return, so Hope's daemon can run
    for CI / dev on Linux without crashing.
    """
    if not text:
        return

    system = platform.system()
    if system != "Darwin":
        logger.debug("say_sync() no-op on %s: %r", system, text)
        return

    # Preferred path: neural Kokoro. Returns False on any failure; we fall
    # through to `say` below.
    try:
        from hope.audio.kokoro_tts import speak_blocking_neural
        if speak_blocking_neural(text, timeout=timeout):
            return
    except Exception:  # noqa: BLE001 — TTS must never tear down the daemon
        logger.debug("say_sync: neural backend errored, falling back to say",
                     exc_info=True)

    binary = _SAY_BINARY if shutil.which(_SAY_BINARY) else shutil.which("say")
    if binary is None:
        logger.debug("say_sync() skipped — 'say' binary not on PATH: %r", text)
        return

    try:
        subprocess.run(  # noqa: S603 — fixed binary path, text is a literal
            [binary, "-v", _DEFAULT_VOICE, "-r", _DEFAULT_RATE, text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("say_sync() timed out after %.1fs: %r", timeout, text[:60])
    except OSError as exc:  # pragma: no cover — defensive
        logger.debug("say_sync() run failed: %s", exc)


__all__ = ["say", "say_sync"]
