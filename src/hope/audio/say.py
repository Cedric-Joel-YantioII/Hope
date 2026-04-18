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
            [binary, text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:  # pragma: no cover — defensive
        logger.debug("say() Popen failed: %s", exc)


__all__ = ["say"]
