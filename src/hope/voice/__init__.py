"""Hope voice subsystem — tmux-backed brain session I/O.

Exposes :class:`BrainSession`, a thin Python port of cortexOS's
TypeScript BrainSession. It sends a prompt to the ``hope-main`` tmux
pane, polls ``tmux capture-pane`` for the Claude Code ready prompt,
and scrapes the reply text so the daemon can hand it to TTS.
"""

from hope.voice.brain_session import (
    BrainSession,
    has_ready_prompt,
    strip_ansi,
    strip_formatting_for_tts,
)

__all__ = [
    "BrainSession",
    "has_ready_prompt",
    "strip_ansi",
    "strip_formatting_for_tts",
]
