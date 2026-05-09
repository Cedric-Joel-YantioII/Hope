"""Voice-turn trace roundtrip tests."""

from __future__ import annotations

import time

from hope.traces.voice_trace import VoiceTraceStore, VoiceTurn


def test_voice_trace_roundtrip(tmp_path):
    db_path = tmp_path / "traces.db"
    store = VoiceTraceStore(db_path)

    turn = VoiceTurn(
        started_at=time.time() - 1.0,
        ended_at=time.time(),
        user_transcript="Hey Hope, what time is it?",
        ack_spoken="One moment, looking into it now.",
        brain_request="Hey Hope, what time is it?",
        brain_reply_full="It's 2:30pm.\nThe full readout is below.",
        brain_reply_head="It's 2:30pm.",
        tts_spoken="It's 2:30pm.",
        duration_seconds=1.2,
        skill_tags=["daily-briefing"],
        metadata={"source": "test"},
    )
    store.save(turn)

    # Roundtrip by id
    fetched = store.get(turn.turn_id)
    assert fetched is not None
    assert fetched.user_transcript == turn.user_transcript
    assert fetched.brain_reply_head == "It's 2:30pm."
    assert fetched.skill_tags == ["daily-briefing"]
    assert fetched.metadata == {"source": "test"}
    assert fetched.score is None

    # list_recent returns it
    recent = store.list_recent(limit=10)
    assert len(recent) == 1
    assert recent[0].turn_id == turn.turn_id

    # update_score sticks
    assert store.update_score(turn.turn_id, 0.75, "good")
    fetched2 = store.get(turn.turn_id)
    assert fetched2 is not None
    assert fetched2.score == 0.75
    assert fetched2.score_reason == "good"

    # stats_since aggregates correctly
    stats = store.stats_since(turn.started_at - 10.0)
    assert stats["turns"] == 1
    assert stats["errors"] == 0
    assert stats["avg_score"] == 0.75

    store.close()


def test_voice_trace_unscored_filter(tmp_path):
    store = VoiceTraceStore(tmp_path / "traces.db")
    scored = VoiceTurn(user_transcript="a", brain_reply_full="r")
    unscored = VoiceTurn(user_transcript="b", brain_reply_full="r")
    store.save(scored)
    store.save(unscored)
    store.update_score(scored.turn_id, 0.5, "test")

    only_unscored = store.list_recent(only_unscored=True, limit=10)
    ids = {t.turn_id for t in only_unscored}
    assert unscored.turn_id in ids
    assert scored.turn_id not in ids
    store.close()
