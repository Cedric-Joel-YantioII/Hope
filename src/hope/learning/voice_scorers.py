"""Voice-turn scorers.

Five cheap heuristics that convert a window of :class:`VoiceTurn` records
into a 0-1 score per turn. Kept simple and local — no LLM judge required
in the hot path; the :class:`TraceJudge` in ``optimize/feedback/judge.py``
can layer on top for ambiguous turns.

Each scorer returns an ``(delta, reason)`` tuple:
  * ``delta`` is in ``[-1, 1]``; positive = good, negative = bad.
  * ``reason`` is a short human-readable string used in ``score_reason``.

The final score is ``clamp(0.5 + sum(deltas), 0, 1)`` — starts neutral,
moves with evidence.
"""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

from hope.traces.voice_trace import VoiceTurn

Scorer = Tuple[float, str]


# ---------------------------------------------------------------------------
# Individual scorers
# ---------------------------------------------------------------------------


_NEGATIVE_PHRASES = (
    "no that's not",
    "not right",
    "never mind",
    "nevermind",
    "cancel that",
    "stop",
    "wrong",
    "that's wrong",
    "nope",
)
_POSITIVE_PHRASES = (
    "thanks",
    "thank you",
    "perfect",
    "got it",
    "great",
    "awesome",
    "nice",
    "love that",
    "exactly",
)


def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def score_repetition(turn: VoiceTurn, next_turn: VoiceTurn | None) -> Scorer:
    """Negative if the user re-issued the same request within 30s.

    Token-overlap > 60% against the NEXT turn's transcript, arriving
    within 30s, means the user didn't get a useful answer and tried again.
    """
    if next_turn is None:
        return (0.0, "")
    gap = next_turn.started_at - turn.ended_at
    if gap < 0 or gap > 30.0:
        return (0.0, "")
    a = set(_tokens(turn.user_transcript))
    b = set(_tokens(next_turn.user_transcript))
    if len(a) < 2 or len(b) < 2:
        return (0.0, "")
    overlap = len(a & b) / max(len(a), 1)
    if overlap > 0.6:
        return (-0.4, f"user-repeated (overlap={overlap:.2f})")
    return (0.0, "")


def score_correction(turn: VoiceTurn, next_turn: VoiceTurn | None) -> Scorer:
    """Negative if the user's next turn is an explicit correction."""
    if next_turn is None:
        return (0.0, "")
    if (next_turn.started_at - turn.ended_at) > 60.0:
        return (0.0, "")
    lowered = next_turn.user_transcript.lower()
    for phrase in _NEGATIVE_PHRASES:
        if phrase in lowered:
            return (-0.5, f"user-corrected ({phrase!r})")
    return (0.0, "")


def score_satisfaction(turn: VoiceTurn, next_turn: VoiceTurn | None) -> Scorer:
    """Positive if the user's next utterance thanks Hope / signals approval."""
    if next_turn is None:
        return (0.0, "")
    if (next_turn.started_at - turn.ended_at) > 60.0:
        return (0.0, "")
    lowered = next_turn.user_transcript.lower().strip(" .,!?")
    if len(_tokens(lowered)) > 6:
        return (0.0, "")  # too long to be a pure ack
    for phrase in _POSITIVE_PHRASES:
        if phrase in lowered:
            return (+0.4, f"user-thanked ({phrase!r})")
    return (0.0, "")


def score_tts_shape(turn: VoiceTurn, next_turn: VoiceTurn | None) -> Scorer:
    """Penalize replies where the spoken first-sentence missed the answer.

    Heuristic: if the FULL reply is >2 sentences AND the spoken head is
    very short (<25 chars) or contains no content word the full reply
    emphasises, the head probably didn't carry the answer.
    """
    full = turn.brain_reply_full.strip()
    head = turn.brain_reply_head.strip()
    if not full or not head:
        return (0.0, "")
    # crude sentence count
    sentences = [s for s in re.split(r"[.!?]+", full) if s.strip()]
    if len(sentences) <= 2:
        return (0.0, "")
    if len(head) < 25:
        return (-0.2, "tts-head-too-short")
    full_tokens = set(_tokens(full)) - set(_tokens(head))
    head_tokens = set(_tokens(head))
    # If the first sentence shares very little vocabulary with the rest of
    # the reply, it's probably a greeting / preamble and missed the answer.
    if full_tokens and head_tokens:
        shared = len(full_tokens & head_tokens) / max(len(head_tokens), 1)
        if shared < 0.1:
            return (-0.15, "tts-head-preamble")
    return (0.0, "")


def score_error(turn: VoiceTurn, next_turn: VoiceTurn | None) -> Scorer:
    """Large negative if the brain call errored or timed out."""
    if turn.error:
        return (-0.6, f"error:{turn.error[:60]}")
    return (0.0, "")


ALL_SCORERS = (
    score_repetition,
    score_correction,
    score_satisfaction,
    score_tts_shape,
    score_error,
)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def score_turn(
    turn: VoiceTurn,
    next_turn: VoiceTurn | None = None,
) -> Tuple[float, str]:
    """Run all scorers on *turn* and return ``(score_0_to_1, combined_reason)``."""
    total = 0.0
    reasons: List[str] = []
    for fn in ALL_SCORERS:
        delta, reason = fn(turn, next_turn)
        if delta == 0.0 and not reason:
            continue
        total += delta
        if reason:
            reasons.append(reason)
    score = max(0.0, min(1.0, 0.5 + total))
    return score, "; ".join(reasons) if reasons else "neutral"


def score_window(turns: Sequence[VoiceTurn]) -> List[Tuple[str, float, str]]:
    """Score a chronological window of turns.

    ``turns`` MUST be sorted ascending by ``started_at``. Returns a list of
    ``(turn_id, score, reason)`` suitable for
    :meth:`VoiceTraceStore.update_score`.
    """
    ordered = sorted(turns, key=lambda t: t.started_at)
    results: List[Tuple[str, float, str]] = []
    for i, turn in enumerate(ordered):
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        score, reason = score_turn(turn, nxt)
        results.append((turn.turn_id, score, reason))
    return results


__all__ = [
    "ALL_SCORERS",
    "score_turn",
    "score_window",
    "score_repetition",
    "score_correction",
    "score_satisfaction",
    "score_tts_shape",
    "score_error",
]
