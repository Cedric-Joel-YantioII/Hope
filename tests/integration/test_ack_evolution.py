"""Ack-phrase evolution integration test.

Seeds a :class:`VoiceTraceStore` with 50 turns where half correlate a
specific ack phrase with negative outcomes, then runs
:func:`evolve_acks` and asserts the bad phrase is pruned from the
overlay file ``~/.hope/learning/acks.json`` (redirected to a tmp path).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hope.learning import voice_learning_loop as vll
from hope.learning.voice_learning_loop import (
    DEFAULT_ACKS,
    VoiceLearningLoop,
    LoopConfig,
    evolve_acks,
    load_acks,
    save_acks,
)
from hope.traces.voice_trace import VoiceTraceStore, VoiceTurn


BAD_ACK = "Sounds good."
GOOD_ACK = "Sure, let me take a look."  # one of DEFAULT_ACKS


def _seed_store(store: VoiceTraceStore) -> None:
    """25 bad-ack turns (low score) + 25 good-ack turns (high score)."""
    base = time.time() - 3600.0
    for i in range(25):
        store.save(
            VoiceTurn(
                turn_id=f"bad_{i}",
                started_at=base + i,
                ended_at=base + i + 0.5,
                duration_seconds=0.5,
                user_transcript=f"query {i}",
                ack_spoken=BAD_ACK,
                brain_request=f"query {i}",
                brain_reply_full="sorry, that's not right",
                brain_reply_head="sorry, that's not right",
                tts_spoken="sorry, that's not right",
                score=0.1,
                score_reason="seeded-bad",
            )
        )
    for i in range(25):
        store.save(
            VoiceTurn(
                turn_id=f"good_{i}",
                started_at=base + 100 + i,
                ended_at=base + 100 + i + 0.5,
                duration_seconds=0.5,
                user_transcript=f"other query {i}",
                ack_spoken=GOOD_ACK,
                brain_request=f"other query {i}",
                brain_reply_full="done",
                brain_reply_head="done",
                tts_spoken="done",
                score=0.9,
                score_reason="seeded-good",
            )
        )


def test_evolve_acks_prunes_bad_phrase(tmp_path, monkeypatch):
    """BAD_ACK is correlated with negative outcomes and must be pruned."""
    # Redirect ACKS_PATH to tmp so we can assert file-level persistence.
    acks_path = tmp_path / "acks.json"
    monkeypatch.setattr(vll, "ACKS_PATH", acks_path)

    # Seed the overlay with defaults + BAD_ACK so there's something to prune.
    initial = list(DEFAULT_ACKS) + [BAD_ACK]
    save_acks(initial)
    assert BAD_ACK in load_acks()

    # Seed a trace store with 50 turns split 25/25 bad/good.
    store = VoiceTraceStore(db_path=str(tmp_path / "traces.db"))
    _seed_store(store)
    turns = store.list_recent(limit=100)
    assert len(turns) == 50

    result = evolve_acks(turns, min_samples=5, prune_below=0.35, keep_floor=4)
    assert BAD_ACK in result.pruned, (
        f"BAD_ACK={BAD_ACK!r} not pruned — ack_scores={result.ack_scores!r}"
    )
    assert BAD_ACK not in result.kept

    # Persistence: the overlay file no longer contains BAD_ACK.
    saved = json.loads(acks_path.read_text())["phrases"]
    assert BAD_ACK not in saved
    # Good acks survived.
    assert GOOD_ACK in saved


def test_voice_learning_loop_tick_runs_evolution(tmp_path, monkeypatch):
    """VoiceLearningLoop.tick() chains scoring + ack evolution end-to-end."""
    acks_path = tmp_path / "acks.json"
    monkeypatch.setattr(vll, "ACKS_PATH", acks_path)
    monkeypatch.setattr(vll, "WAKE_PATH", tmp_path / "wake.json")

    save_acks(list(DEFAULT_ACKS) + [BAD_ACK])

    store = VoiceTraceStore(db_path=str(tmp_path / "traces.db"))
    _seed_store(store)

    loop = VoiceLearningLoop(store, config=LoopConfig(enabled=True))
    result = loop.tick()

    assert result["window_turns"] >= 50
    # tick() triggers evolve_acks internally; BAD_ACK should be pruned.
    assert BAD_ACK in result["ack_pruned"], (
        f"evolve pass did not prune BAD_ACK; ack_pruned={result['ack_pruned']!r}"
    )
    saved = json.loads(acks_path.read_text())["phrases"]
    assert BAD_ACK not in saved

    store.close()
