"""Atomic enqueue helper for Hope's voice-out queue.

Producers (the ``hope-speak`` shim, Python callers, other subprocesses)
call :func:`append_speech_line` which:

  1. Builds a wire-format dict (id, text, voice?, priority?, created_at, status).
  2. Serializes it to a single JSON line (no embedded newlines — we
     escape any control chars so ``readline`` always gets one record).
  3. Appends under an ``fcntl.flock`` exclusive lock so concurrent
     writers can't interleave bytes mid-line.

The queue file lives at ``<HOPE_ROOT>/.hope-io/tts-out.jsonl``. If the
parent directory is missing we create it — that way a fresh checkout
still works without a manual ``mkdir`` step.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import pathlib
import uuid
from typing import Optional

# Resolve the Hope root from the module location. ``voice_bridge`` lives
# at ``<HOPE_ROOT>/src/voice_bridge`` so the root is two parents up.
_HOPE_ROOT = pathlib.Path(__file__).resolve().parents[2]
_IO_DIR = _HOPE_ROOT / ".hope-io"
_QUEUE_PATH = _IO_DIR / "tts-out.jsonl"


def queue_path() -> pathlib.Path:
    """Return the absolute path to the voice-out queue file."""
    return _QUEUE_PATH


def append_speech_line(
    text: str,
    *,
    voice: Optional[str] = None,
    priority: Optional[int] = None,
) -> str:
    """Append a single speech record to the queue and return its uuid.

    Raises ``ValueError`` if *text* is empty after stripping — empty
    utterances would just waste a consumer wakeup.
    """
    if text is None:
        raise ValueError("hope-speak: text is required")
    stripped = text.strip()
    if not stripped:
        raise ValueError("hope-speak: text cannot be empty")

    record_id = str(uuid.uuid4())
    record = {
        "id": record_id,
        "text": stripped,
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "status": "pending",
    }
    if voice:
        record["voice"] = voice
    if priority is not None:
        record["priority"] = int(priority)

    # ensure_ascii=True so non-ASCII bytes can't sneak in an embedded
    # newline and desync the reader's ``readline`` loop.
    line = json.dumps(record, ensure_ascii=True)
    if "\n" in line:  # pragma: no cover — defensive; ensure_ascii guarantees this
        line = line.replace("\n", " ")

    _IO_DIR.mkdir(parents=True, exist_ok=True)

    # O_APPEND + flock is belt-and-suspenders. O_APPEND gives atomic
    # appends on POSIX when writes are <= PIPE_BUF (~4 KiB on darwin),
    # flock covers larger records and multi-step operations.
    fd = os.open(_QUEUE_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

    return record_id


__all__ = ["append_speech_line", "queue_path"]
