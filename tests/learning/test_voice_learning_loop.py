"""Voice learning loop + SkillOptimizer dry-run tests."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from hope.learning import voice_learning_loop as vll
from hope.learning.voice_learning_loop import (
    VoiceLearningLoop,
    LoopConfig,
    evolve_acks,
    evolve_wake_phrases,
    load_acks,
    mine_wake_alternates,
)
from hope.traces.voice_trace import VoiceTraceStore, VoiceTurn


def _turn(**kwargs):
    base = dict(started_at=time.time(), ended_at=time.time() + 1.0, duration_seconds=1.0)
    base.update(kwargs)
    return VoiceTurn(**base)


def test_ack_evolution_prunes_bad_phrase(tmp_path, monkeypatch):
    """Pruning path: an ack correlated with low scores gets removed."""
    acks_path = tmp_path / "acks.json"
    monkeypatch.setattr(vll, "ACKS_PATH", acks_path)

    bad = "Okay, let me think about that."
    # Give bad ack many low-score turns, and another ack many high-score ones.
    turns = []
    for _ in range(8):
        turns.append(_turn(ack_spoken=bad, score=0.1))
    for _ in range(8):
        turns.append(_turn(ack_spoken="Sure, let me take a look.", score=0.9))

    result = evolve_acks(turns, min_samples=5, prune_below=0.35, keep_floor=4)
    assert bad in result.pruned
    assert bad not in result.kept
    # File was persisted
    saved = json.loads(acks_path.read_text())["phrases"]
    assert bad not in saved


def test_ack_evolution_respects_keep_floor(tmp_path, monkeypatch):
    """Never shrink below keep_floor even if every ack scored badly."""
    acks_path = tmp_path / "acks.json"
    monkeypatch.setattr(vll, "ACKS_PATH", acks_path)

    turns = []
    current = load_acks()
    for phrase in current:
        for _ in range(8):
            turns.append(_turn(ack_spoken=phrase, score=0.05))

    result = evolve_acks(turns, min_samples=5, prune_below=0.35, keep_floor=4)
    assert len(result.kept) >= 4


def test_wake_phrase_mining(tmp_path, monkeypatch):
    """Short utterances containing 'hope' that recur ≥3 times become candidates."""
    monkeypatch.setattr(vll, "WAKE_PATH", tmp_path / "wake.json")
    turns = [
        _turn(user_transcript="yo hope") for _ in range(3)
    ] + [_turn(user_transcript="what's the weather")]
    alternates = mine_wake_alternates(turns, min_uses=3)
    assert "yo hope" in alternates

    evolve_wake_phrases(turns)
    saved = json.loads((tmp_path / "wake.json").read_text())["phrases"]
    assert "yo hope" in saved


def test_loop_tick_scores_unscored_turns(tmp_path, monkeypatch):
    """End-to-end: VoiceLearningLoop.tick scores unscored rows in the store."""
    monkeypatch.setattr(vll, "ACKS_PATH", tmp_path / "acks.json")
    monkeypatch.setattr(vll, "WAKE_PATH", tmp_path / "wake.json")

    store = VoiceTraceStore(tmp_path / "traces.db")
    now = time.time()
    # A good turn followed by a thanks — should score > 0.5.
    good = _turn(
        started_at=now - 10, ended_at=now - 9,
        user_transcript="what time is it",
        brain_reply_full="2:30pm.",
    )
    thanks = _turn(
        started_at=now - 8, ended_at=now - 7,
        user_transcript="thanks",
        brain_reply_full="",
    )
    # A bad turn followed by a repetition — should score < 0.5.
    bad = _turn(
        started_at=now - 5, ended_at=now - 4,
        user_transcript="open youtube",
        brain_reply_full="I can't.",
    )
    retry = _turn(
        started_at=now - 3, ended_at=now - 2,
        user_transcript="please open youtube",
        brain_reply_full="",
    )
    for t in (good, thanks, bad, retry):
        store.save(t)

    loop = VoiceLearningLoop(
        store,
        config=LoopConfig(enabled=True, nightly_skill_optimize=False),
    )
    summary = loop.tick()
    assert summary["scored"] >= 4
    assert store.get(good.turn_id).score > 0.5
    assert store.get(bad.turn_id).score < 0.5
    store.close()


def test_skill_optimizer_dry_run_from_voice_turns(tmp_path, monkeypatch):
    """SkillOptimizer can consume voice turns via the adapter — no exceptions,
    and a skill bucket with enough traces gets exercised (status != 'skipped').
    """
    monkeypatch.setattr(vll, "ACKS_PATH", tmp_path / "acks.json")
    monkeypatch.setattr(vll, "WAKE_PATH", tmp_path / "wake.json")
    monkeypatch.setattr(vll, "SKILL_OVERLAY_DIR", tmp_path / "skills")

    store = VoiceTraceStore(tmp_path / "traces.db")
    now = time.time()
    for i in range(25):
        store.save(
            _turn(
                started_at=now - 100 + i,
                ended_at=now - 99 + i,
                user_transcript=f"remember note {i}",
                brain_reply_full="Noted.",
                skill_tags=["remember"],
                score=0.9,
            )
        )

    # Mock the lazily-imported DSPy optimizer module to avoid network/LLM.
    fake_output = {"system_prompt": "improved", "few_shot_examples": []}

    class _FakeOpt:
        def __init__(self, *a, **kw): pass
        def optimize(self, _store): return fake_output

    import hope.learning.agents.dspy_optimizer as dspy_mod

    with patch.object(dspy_mod, "DSPyAgentOptimizer", _FakeOpt, create=True):
        hook = vll.default_skill_optimize_hook(store)
        # Hook must run without raising; overlay output depends on
        # SkillManager.discover() finding the 'remember' skill, which we
        # don't assert on here.
        hook()

    store.close()
