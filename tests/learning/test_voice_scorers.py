"""Voice-turn scorers — synthetic good/bad turn tests."""

from __future__ import annotations

from hope.learning.voice_scorers import (
    score_correction,
    score_satisfaction,
    score_turn,
    score_window,
)
from hope.traces.voice_trace import VoiceTurn


def _make_turn(**kwargs):
    base = dict(started_at=1000.0, ended_at=1001.0, duration_seconds=1.0)
    base.update(kwargs)
    return VoiceTurn(**base)


def test_score_good_turn_with_thanks():
    """User says 'thanks' in the next turn → positive score."""
    t1 = _make_turn(user_transcript="what's the weather", brain_reply_full="Sunny, 70F.")
    t2 = _make_turn(
        started_at=1005.0, ended_at=1006.0,
        user_transcript="thanks",
    )
    score, reason = score_turn(t1, t2)
    assert score > 0.5, f"expected positive, got {score} ({reason})"
    assert "thanked" in reason


def test_score_bad_turn_repetition():
    """User repeats the same question within 30s → negative."""
    t1 = _make_turn(
        user_transcript="open youtube please",
        brain_reply_full="I can't open youtube.",
    )
    t2 = _make_turn(
        started_at=1010.0, ended_at=1011.0,
        user_transcript="please open youtube",
    )
    score, reason = score_turn(t1, t2)
    assert score < 0.5, f"expected negative, got {score} ({reason})"
    assert "repeated" in reason


def test_score_error_turn():
    """Error field populated → large negative."""
    t1 = _make_turn(
        user_transcript="do the thing",
        brain_reply_full="",
        error="TimeoutError: session.send timed out",
    )
    score, reason = score_turn(t1, None)
    assert score < 0.4
    assert "error" in reason


def test_score_correction():
    """Next turn is 'no that's not right' → negative."""
    t1 = _make_turn(user_transcript="send mom a text", brain_reply_full="Sent.")
    t2 = _make_turn(
        started_at=1002.0, ended_at=1003.0,
        user_transcript="no that's not right, send dad",
    )
    delta, reason = score_correction(t1, t2)
    assert delta < 0
    assert "corrected" in reason


def test_score_satisfaction_needs_short_ack():
    """Long replies that happen to contain 'thanks' should NOT score positive."""
    t1 = _make_turn(user_transcript="q", brain_reply_full="a")
    t2 = _make_turn(
        started_at=1002.0, ended_at=1003.0,
        user_transcript="thanks but I also want you to do seven other things and also",
    )
    delta, _reason = score_satisfaction(t1, t2)
    assert delta == 0.0


def test_score_window_chronological():
    """score_window processes turns in started_at order regardless of input order."""
    a = _make_turn(user_transcript="open youtube", started_at=1000, ended_at=1001)
    b = _make_turn(user_transcript="please open youtube", started_at=1010, ended_at=1011)
    # Pass b first — should still score a using b as the next turn.
    results = score_window([b, a])
    scored = {tid: (s, r) for tid, s, r in results}
    assert scored[a.turn_id][0] < 0.5  # a was repeated by b → bad
