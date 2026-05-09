"""Classify a transcript arriving MID-BRAIN into one of three actions.

Used by ``HopeDaemon._on_speech_transcript`` when ``_brain_busy`` is set —
previously this case was a single ``BUSY-DROP`` that silently lost the
user's speech. Now we route it:

- ``cancel`` → kill the brain's in-flight turn (user hit the off switch)
- ``backchannel`` → user said "yes", "mm-hmm", "go on" etc.; Hope heard
  them, no new turn is needed. We log/event them so the dashboard can
  show a flicker, but nothing reaches the brain.
- ``new_turn`` → substantive speech that should become a fresh turn
  after the current one finishes. The caller queues it.

Regex-based on purpose — 3 µs per classification, no LLM dependency,
deterministic, inspectable. Tuning surface is small and lives in this
file. If we ever need prosody or fuzzy matching, replace the two
token-sets with a small classifier while keeping the same return shape.
"""

from __future__ import annotations

import re

# --- Cancel / barge-in words ------------------------------------------------
# Any utterance whose FIRST token is here is a cancel. "wait a second",
# "stop that", "nevermind the joke" all match. Keep the list short and
# highly-specific so a general phrase like "waiting for you" doesn't
# accidentally cancel (note: that phrase would start with "waiting" not
# "wait" — the token match is word-bounded).
_CANCEL_STARTERS = frozenset({
    "stop", "cancel", "nevermind", "never",  # "never mind"
    "forget",  # "forget it", "forget that"
    "wait", "hold",  # "hold on"
    "shut",    # "shut up"
    "hush", "quiet", "enough",
    "pause", "abort", "scratch",  # "scratch that"
})

# --- Back-channel words -----------------------------------------------------
# These are what a listener MAKES as noise while still engaged. They don't
# mean "do something new". Must be short — an utterance of ≤3 words where
# ALL words are back-channels is treated as a back-channel; otherwise it's
# considered a substantive turn (e.g. "yes, play music" → new_turn).
_BACKCHANNEL_WORDS = frozenset({
    "yes", "yeah", "yep", "yup", "yah",
    "no", "nope", "nah",
    "ok", "okay", "kay",
    "right", "alright",
    "sure", "fine",
    "mm", "mmm", "mhm", "mmhm",  # mm-hmm gets punctuation stripped below
    "hmm", "hmmm",
    "uh", "huh",  # uh-huh gets punctuation stripped
    "ah", "oh",
    "gotcha",
    # two- or three-word continuations handled separately below
})

_BACKCHANNEL_PHRASES = frozenset({
    "mm hmm", "uh huh", "got it", "i see",
    "go on", "keep going", "carry on",
    "sounds good", "thank you", "thanks",
})


_WORD_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def classify_midturn(text: str) -> str:
    """Return 'cancel', 'backchannel', or 'new_turn' for *text*.

    Inputs are the raw STT transcript strings. Punctuation and casing are
    stripped before matching. Empty / noise-only inputs classify as
    ``backchannel`` (treat as nothing-heard rather than interrupt).
    """
    toks = _tokens(text or "")
    if not toks:
        return "backchannel"

    # Cancel: first token is a canonical canceller.
    if toks[0] in _CANCEL_STARTERS:
        # "stop" alone or "stop talking" / "cancel that" / "hold on" — all cancels.
        return "cancel"

    # Back-channel phrases (two words like "mm hmm" or "go on").
    joined_two = " ".join(toks[:2])
    joined_three = " ".join(toks[:3])
    if joined_three in _BACKCHANNEL_PHRASES and len(toks) <= 3:
        return "backchannel"
    if joined_two in _BACKCHANNEL_PHRASES and len(toks) <= 3:
        return "backchannel"

    # Back-channel: short utterance (≤3 words) and every word is on the list.
    if len(toks) <= 3 and all(t in _BACKCHANNEL_WORDS for t in toks):
        return "backchannel"

    # Otherwise it's a substantive new turn.
    return "new_turn"


__all__ = ["classify_midturn"]
