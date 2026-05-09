"""Dry-run of :class:`ImproveAckPhrases` with a seeded proposal + traces DB.

We copy ``src/hope/daemon/core.py`` into a tmp workspace, pre-seed the
LLM proposal, and assert:

* ``apply()`` rewrites ``_ACK_PHRASES`` to the proposed tuple
* ``evaluate()`` returns success when (a) the tuple differs from the
  baseline, (b) the judge score ≥ 7, (c) tests pass
* ``evaluate()`` rejects when the proposal is identical to the baseline
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from hope.evolution.experiments.improve_ack_phrases import (
    CORE_PATH_REL,
    ImproveAckPhrases,
    _extract_ack_phrases,
    _rewrite_ack_phrases,
)

REPO = Path(__file__).resolve().parents[2]


_BASELINE_PHRASES = (
    "Okay, let me think about that.",
    "One moment, looking into it now.",
    "Right, give me just a second.",
    "Sure, let me take a look.",
    "Hmm, let me check that for you.",
    "Okay, working on it right now.",
    "Got it, just a moment please.",
    "Alright, thinking it through now.",
)


@pytest.fixture()
def seeded_workspace(tmp_path: Path) -> Path:
    """Minimal workspace: a tiny module holding a ``_ACK_PHRASES`` tuple.

    We deliberately do NOT copy real core.py — the current core.py has
    ``_ACK_PHRASES = DEFAULT_ACKS`` (a name ref, not a literal). The
    experiment handles that path by inserting a literal, but the unit
    tests want a predictable literal they can round-trip.
    """
    (tmp_path / "src/hope/daemon").mkdir(parents=True)
    baseline_src = (
        '"""Tiny stand-in for the real core.py during tests."""\n'
        "\n"
        "_ACK_PHRASES = (\n"
        + "\n".join(f'    "{p}",' for p in _BASELINE_PHRASES)
        + "\n)\n"
    )
    (tmp_path / CORE_PATH_REL).write_text(baseline_src)

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-A"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True,
    )
    return tmp_path


@pytest.fixture()
def seeded_traces(tmp_path: Path) -> Path:
    """A tiny traces.db with the schema the experiment expects."""
    db = tmp_path / "traces.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT UNIQUE,
                metadata TEXT,
                feedback REAL
            )
        """)
        conn.executemany(
            "INSERT INTO traces (trace_id, metadata, feedback) VALUES (?,?,?)",
            [
                ("t1", '{"ack_phrase": "Okay, let me think about that."}', 0.4),
                ("t2", '{"ack_phrase": "Okay, let me think about that."}', 0.3),
                ("t3", '{"ack_phrase": "One moment, looking into it now."}', 0.9),
            ],
        )
    return db


def test_apply_rewrites_ack_phrases(seeded_workspace: Path) -> None:
    proposal = (
        "Sure.", "One sec.", "On it.", "Got it.", "Okay.", "Mhm."
    )
    exp = ImproveAckPhrases(llm_proposal=proposal, judge_score=9.0)
    exp.apply(seeded_workspace)

    after = _extract_ack_phrases(
        (seeded_workspace / CORE_PATH_REL).read_text()
    )
    assert after == proposal


def test_evaluate_accepts_good_proposal(
    seeded_workspace: Path, seeded_traces: Path,
) -> None:
    proposal = (
        "Sure.", "One sec.", "On it.", "Got it.", "Okay.", "Mhm.",
    )
    exp = ImproveAckPhrases(
        traces_db=seeded_traces,
        llm_proposal=proposal,
        judge_score=9.0,
    )
    exp.apply(seeded_workspace)
    result = exp.evaluate(seeded_workspace)

    # No test suite exists in the seeded workspace → _run_pytest returns
    # "no test targets found" → passes.
    assert result.tests_passed is True
    assert result.success is True
    assert result.score == pytest.approx(9.0)
    assert result.details["different_from_baseline"] is True
    assert result.details["candidate"] == list(proposal)


def test_evaluate_rejects_identical_proposal(
    seeded_workspace: Path, seeded_traces: Path,
) -> None:
    """If the proposal equals the baseline, the experiment must reject."""
    baseline = _extract_ack_phrases(
        (seeded_workspace / CORE_PATH_REL).read_text()
    )
    exp = ImproveAckPhrases(
        traces_db=seeded_traces,
        llm_proposal=baseline,
        judge_score=9.0,
    )
    exp.apply(seeded_workspace)  # no-op because proposal == baseline
    result = exp.evaluate(seeded_workspace)

    assert result.success is False
    assert "identical_to_baseline" in result.details["reason"]


def test_evaluate_rejects_low_judge(
    seeded_workspace: Path, seeded_traces: Path,
) -> None:
    proposal = (
        "hmm", "uhh", "eh", "oh",
    )
    exp = ImproveAckPhrases(
        traces_db=seeded_traces,
        llm_proposal=proposal,
        judge_score=3.0,
    )
    exp.apply(seeded_workspace)
    result = exp.evaluate(seeded_workspace)
    assert result.success is False
    assert "judge_low" in result.details["reason"]


def test_trace_scores_loaded(seeded_traces: Path) -> None:
    exp = ImproveAckPhrases(traces_db=seeded_traces)
    scored = exp._load_ack_scores()
    assert set(scored) == {
        "Okay, let me think about that.",
        "One moment, looking into it now.",
    }
    bad = scored["Okay, let me think about that."]
    good = scored["One moment, looking into it now."]
    assert bad["n"] == 2
    assert bad["mean"] == pytest.approx(0.35)
    assert good["mean"] == pytest.approx(0.9)


def test_rewrite_preserves_file_parseable(seeded_workspace: Path) -> None:
    """After rewrite, the file must still parse (no syntax errors)."""
    import ast

    src = (seeded_workspace / CORE_PATH_REL).read_text()
    new = _rewrite_ack_phrases(src, ("a", "b", "c", "d"))
    ast.parse(new)  # would raise SyntaxError on failure
    assert '"a"' in new and '"b"' in new
