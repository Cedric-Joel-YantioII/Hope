"""Tests for the Claude Code tmux-pane inference engine backend."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from hope.core.config import (
    ClaudeCodeTmuxEngineConfig,
    EngineConfig,
)
from hope.core.registry import EngineRegistry
from hope.core.types import Message, Role
from hope.engine.claude_code_tmux import ClaudeCodeTmuxEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fifo(tmp_path: Path) -> Path:
    """Create a real named FIFO in ``tmp_path`` and return its path."""
    fifo = tmp_path / "pane.fifo"
    os.mkfifo(str(fifo))
    return fifo


def _feed_fifo(fifo: Path, lines: List[str], delay: float = 0.0) -> threading.Thread:
    """Spawn a writer thread that pushes ``lines`` into ``fifo``.

    Returns the Thread so the test can ``.join()`` it.  Writing to a FIFO
    blocks until a reader shows up, so we must launch this concurrently.
    """

    def _run() -> None:
        # open-for-write blocks until reader attaches
        with open(fifo, "w", buffering=1) as fh:
            for ln in lines:
                if delay:
                    time.sleep(delay)
                fh.write(ln)
                fh.flush()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


class TestClaudeCodeTmuxEngineConfig:
    def test_default_values(self) -> None:
        cfg = ClaudeCodeTmuxEngineConfig()
        assert cfg.pane_target == "hope:0.0"
        assert cfg.fifo_path == "~/.hope/panes/hope-main.fifo"
        assert cfg.request_timeout_sec == 120.0
        assert cfg.sentinel_prefix == "---HOPE_PANE"

    def test_engine_config_has_claude_code_tmux_field(self) -> None:
        ec = EngineConfig()
        assert hasattr(ec, "claude_code_tmux")
        assert isinstance(ec.claude_code_tmux, ClaudeCodeTmuxEngineConfig)

    def test_registered_in_engine_registry(self) -> None:
        # The autouse ``_clean_registries`` conftest fixture wipes the
        # registry before every test.  Re-register here by value so we
        # can prove the class is the one the decorator would have bound.
        EngineRegistry.register_value("claude_code_tmux", ClaudeCodeTmuxEngine)
        assert EngineRegistry.contains("claude_code_tmux")
        assert EngineRegistry.get("claude_code_tmux") is ClaudeCodeTmuxEngine


# ---------------------------------------------------------------------------
# Message serialization — no tool_calls, no tool messages
# ---------------------------------------------------------------------------


class TestMessagesToPrompt:
    def test_single_user(self) -> None:
        engine = ClaudeCodeTmuxEngine()
        out = engine._messages_to_prompt([Message(role=Role.USER, content="Hi")])
        assert out == "[user]\nHi"

    def test_system_and_user(self) -> None:
        engine = ClaudeCodeTmuxEngine()
        out = engine._messages_to_prompt(
            [
                Message(role=Role.SYSTEM, content="You are helpful."),
                Message(role=Role.USER, content="Hello"),
            ]
        )
        assert out == "[system]\nYou are helpful.\n\n[user]\nHello"

    def test_multi_turn(self) -> None:
        engine = ClaudeCodeTmuxEngine()
        out = engine._messages_to_prompt(
            [
                Message(role=Role.USER, content="first"),
                Message(role=Role.ASSISTANT, content="ack"),
                Message(role=Role.USER, content="second"),
            ]
        )
        assert out == "[user]\nfirst\n\n[assistant]\nack\n\n[user]\nsecond"

    def test_tool_messages_dropped(self) -> None:
        """Tool messages must be stripped — Claude Code handles tools itself."""
        engine = ClaudeCodeTmuxEngine()
        out = engine._messages_to_prompt(
            [
                Message(role=Role.USER, content="go"),
                Message(role=Role.TOOL, content="should not appear"),
                Message(role=Role.ASSISTANT, content="done"),
            ]
        )
        assert "should not appear" not in out
        assert out == "[user]\ngo\n\n[assistant]\ndone"


# ---------------------------------------------------------------------------
# Framed prompt contains both sentinels
# ---------------------------------------------------------------------------


class TestFraming:
    def test_framed_prompt_has_sentinels(self) -> None:
        engine = ClaudeCodeTmuxEngine(sentinel_prefix="<<<TEST")
        framed = engine._framed_prompt("hello", rid="abc123")
        assert "<<<TEST_REQ_abc123>>>" in framed
        assert "<<<TEST_END_abc123>>>" in framed
        # end sentinel appears after the prompt body
        assert framed.index("<<<TEST_END_abc123>>>") > framed.index("hello")


# ---------------------------------------------------------------------------
# generate(): full round-trip with a real FIFO + mocked tmux
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_round_trip(self, tmp_path: Path) -> None:
        fifo = _make_fifo(tmp_path)
        engine = ClaudeCodeTmuxEngine(
            pane_target="hope-test:0.0",
            fifo_path=str(fifo),
            request_timeout_sec=5.0,
            sentinel_prefix="<<<T",
        )

        sent: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            sent.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with patch(
            "hope.engine.claude_code_tmux.subprocess.run", side_effect=fake_run
        ):
            # When send_to_pane is invoked we'll know the framed prompt
            # was serialized — so kick off the FIFO feeder first.  The
            # sentinel UUIDs are generated inside generate(), so we use
            # a permissive feeder that matches any UUID by writing
            # *both* sentinels before any body text.  We do this by
            # waiting for send to happen, then parsing the sent payload.

            def feeder() -> None:
                # Wait until the engine has sent its framed prompt.
                while not sent:
                    time.sleep(0.005)
                payload = sent[0][-1]  # last arg to tmux send-keys -l
                # Extract sentinels from the payload
                start_line = next(
                    ln for ln in payload.splitlines() if ln.startswith("<<<T_REQ_")
                )
                end_line = next(
                    ln for ln in payload.splitlines() if "<<<T_END_" in ln
                )
                end_sentinel = end_line.split()[-1]
                with open(fifo, "w", buffering=1) as fh:
                    fh.write(start_line + "\n")
                    fh.write("Hello, I am Hope.\n")
                    fh.write("Nice to meet you.\n")
                    fh.write(end_sentinel + "\n")

            t = threading.Thread(target=feeder, daemon=True)
            t.start()

            result = engine.generate(
                [Message(role=Role.USER, content="Who are you?")],
                model="claude-code",
                temperature=0.7,
                max_tokens=100,
            )
            t.join(timeout=5)

        assert result["model"] == "claude-code"
        assert result["finish_reason"] == "stop"
        assert "Hello, I am Hope." in result["content"]
        assert "Nice to meet you." in result["content"]
        assert result["usage"]["prompt_tokens"] > 0
        assert result["usage"]["completion_tokens"] > 0

        # First subprocess.run should be a tmux send-keys -l
        assert sent[0][0] == "tmux"
        assert sent[0][1] == "send-keys"
        assert "-t" in sent[0]
        assert "-l" in sent[0]
        # Followed by the Enter keypress
        assert any("Enter" in c for c in sent[1:])


# ---------------------------------------------------------------------------
# stream(): yields incremental chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_incremental(tmp_path: Path) -> None:
    fifo = _make_fifo(tmp_path)
    engine = ClaudeCodeTmuxEngine(
        pane_target="hope-test:0.0",
        fifo_path=str(fifo),
        request_timeout_sec=5.0,
        sentinel_prefix="<<<S",
    )

    sent: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        sent.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    with patch("hope.engine.claude_code_tmux.subprocess.run", side_effect=fake_run):
        # Feeder: wait for send, parse sentinels from payload, write body
        # lines with small gaps to verify incremental delivery.
        def feeder() -> None:
            while not sent:
                time.sleep(0.005)
            payload = sent[0][-1]
            start_line = next(
                ln for ln in payload.splitlines() if ln.startswith("<<<S_REQ_")
            )
            end_line = next(ln for ln in payload.splitlines() if "<<<S_END_" in ln)
            end_sentinel = end_line.split()[-1]
            with open(fifo, "w", buffering=1) as fh:
                fh.write(start_line + "\n")
                fh.flush()
                for word in ["one\n", "two\n", "three\n"]:
                    fh.write(word)
                    fh.flush()
                    time.sleep(0.01)
                fh.write(end_sentinel + "\n")

        t = threading.Thread(target=feeder, daemon=True)
        t.start()

        chunks: list[str] = []
        async for chunk in engine.stream(
            [Message(role=Role.USER, content="count")],
            model="claude-code",
        ):
            chunks.append(chunk)

        t.join(timeout=5)

    joined = "".join(chunks)
    assert "one" in joined and "two" in joined and "three" in joined
    # We expect the FIFO to emit at least one chunk per line written.
    assert len(chunks) >= 3


# ---------------------------------------------------------------------------
# health(): False when tmux session is missing
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_false_without_session(self, tmp_path: Path) -> None:
        fifo = _make_fifo(tmp_path)
        engine = ClaudeCodeTmuxEngine(
            pane_target="does-not-exist:0.0",
            fifo_path=str(fifo),
        )

        def fake_run(cmd, *args, **kwargs):
            # Simulate tmux has-session returning non-zero
            return subprocess.CompletedProcess(cmd, 1, b"", b"no session")

        with patch(
            "hope.engine.claude_code_tmux.subprocess.run", side_effect=fake_run
        ):
            assert engine.health() is False

    def test_health_false_without_fifo(self, tmp_path: Path) -> None:
        # FIFO path does not exist
        engine = ClaudeCodeTmuxEngine(
            pane_target="hope:0.0",
            fifo_path=str(tmp_path / "missing.fifo"),
        )

        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with patch(
            "hope.engine.claude_code_tmux.subprocess.run", side_effect=fake_run
        ):
            assert engine.health() is False

    def test_health_true_when_both_present(self, tmp_path: Path) -> None:
        fifo = _make_fifo(tmp_path)
        engine = ClaudeCodeTmuxEngine(
            pane_target="hope:0.0",
            fifo_path=str(fifo),
        )

        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with patch(
            "hope.engine.claude_code_tmux.subprocess.run", side_effect=fake_run
        ):
            assert engine.health() is True


# ---------------------------------------------------------------------------
# Per-pane locking: concurrent generate() calls must serialize
# ---------------------------------------------------------------------------


class TestPerPaneLock:
    def test_sync_lock_serializes_concurrent_generate(self, tmp_path: Path) -> None:
        fifo = _make_fifo(tmp_path)
        engine = ClaudeCodeTmuxEngine(
            pane_target="hope-test:0.0",
            fifo_path=str(fifo),
            request_timeout_sec=5.0,
            sentinel_prefix="<<<L",
        )

        # Track simultaneous entry into the critical section.
        concurrent_count = 0
        max_concurrent = 0
        cs_lock = threading.Lock()

        original_read = engine._read_until_sentinel_sync

        def tracked_read(start_sent: str, end_sent: str) -> str:
            nonlocal concurrent_count, max_concurrent
            with cs_lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            # Simulate work.  If the outer sync_lock does its job, only one
            # thread will ever be here at once.
            time.sleep(0.1)
            with cs_lock:
                concurrent_count -= 1
            return "served"

        engine._read_until_sentinel_sync = tracked_read  # type: ignore[assignment]

        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with patch(
            "hope.engine.claude_code_tmux.subprocess.run", side_effect=fake_run
        ):

            def call() -> str:
                out = engine.generate(
                    [Message(role=Role.USER, content="hi")],
                    model="claude-code",
                )
                return out["content"]

            threads = [threading.Thread(target=call) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        assert max_concurrent == 1, (
            f"Per-pane lock failed: {max_concurrent} concurrent readers saw"
        )


# ---------------------------------------------------------------------------
# Catalog: claude-code is registered with the expected shape
# ---------------------------------------------------------------------------


class TestModelCatalog:
    def test_claude_code_entry_present(self) -> None:
        from hope.intelligence.model_catalog import BUILTIN_MODELS

        entries = [s for s in BUILTIN_MODELS if s.model_id == "claude-code"]
        assert len(entries) == 1
        spec = entries[0]
        assert spec.supported_engines == ("claude_code_tmux",)
        assert spec.requires_api_key is False
        assert spec.provider == "anthropic"
        assert spec.context_length == 200000
