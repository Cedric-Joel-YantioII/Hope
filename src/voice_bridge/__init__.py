"""Voice-out bridge — Claude Code brain -> Hope TTS via a shared jsonl queue.

The ``voice_bridge`` package implements the writer/consumer pair that
lets any process (CC subprocesses, shell shims, the daemon itself)
queue speech for Hope without holding a reference to her TTS stack.

Components:
    * ``hope_speak_append`` — atomic enqueue helper (used by the
      ``/opt/homebrew/bin/hope-speak`` shim and by Python callers).
    * ``tts_consumer`` — long-running tailer that drains the queue and
      calls :func:`hope.audio.say.say_sync` per line.

Wire format (``~/Documents/Github/Hope/.hope-io/tts-out.jsonl``):
    {"id": uuid, "text": str, "voice": str?, "priority": int?,
     "created_at": iso, "status": "pending"}

Completion is recorded in a companion append-only file
(``tts-out.done.jsonl``) so the consumer never has to rewrite the
primary queue file — no contention with concurrent producers.
"""

from voice_bridge.hope_speak_append import append_speech_line

__all__ = ["append_speech_line"]
