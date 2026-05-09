"""Tests for hope.voice.brain_session — the Python port of cortexOS's BrainSession."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional

import pytest

from hope.voice.brain_session import (
    BrainSession,
    has_ready_prompt,
    strip_ansi,
    strip_formatting_for_tts,
)

# ---------------------------------------------------------------------------
# Fakes — a minimal orchestrator stub with registry + tmux runner recording.
# ---------------------------------------------------------------------------


@dataclass
class _FakeEntry:
    tmux_target: str = "hope:hope.0"


class _FakeRegistry:
    def __init__(self, pane_id: str = "hope-abcd") -> None:
        self._entries = {pane_id: _FakeEntry()}

    def get(self, pane_id: str) -> Optional[_FakeEntry]:
        return self._entries.get(pane_id)


class FakeOrchestrator:
    """Stand-in for TmuxOrchestrator that records tmux calls and hands back
    pre-scripted capture_pane outputs.
    """

    def __init__(
        self,
        pane_id: str = "hope-abcd",
        pane_outputs: Optional[List[str]] = None,
    ) -> None:
        self.registry = _FakeRegistry(pane_id)
        self._pane_outputs = list(pane_outputs or [])
        self.tmux_calls: List[List[str]] = []
        self.capture_calls: int = 0

    def capture_pane(self, pane_id: str, lines: int = 200) -> str:
        self.capture_calls += 1
        if not self._pane_outputs:
            return ""
        if len(self._pane_outputs) == 1:
            return self._pane_outputs[0]
        return self._pane_outputs.pop(0)

    def _tmux(self, cmd, *, check: bool = True, **kwargs):
        self.tmux_calls.append(cmd)

        class _CP:
            stdout = ""
            returncode = 0

        return _CP()


# ---------------------------------------------------------------------------
# has_ready_prompt
# ---------------------------------------------------------------------------


def test_has_ready_prompt_true_when_prompt_visible():
    assert has_ready_prompt("hello\n\n❯ ") is True


def test_has_ready_prompt_false_on_esc_to_interrupt():
    # Claude renders ❯ in chrome even while thinking — the interrupt hint
    # must override.
    assert has_ready_prompt("❯ Working...\n(esc to interrupt)") is False


def test_has_ready_prompt_false_on_thinking():
    assert has_ready_prompt("❯\nThinking...") is False


def test_has_ready_prompt_false_on_empty():
    assert has_ready_prompt("") is False
    assert has_ready_prompt("   \n\n") is False


def test_has_ready_prompt_only_checks_last_six_nonempty_lines():
    # "esc to interrupt" earlier in history doesn't disqualify a current
    # ready prompt.
    old = "\n".join(["esc to interrupt"] + ["filler"] * 10 + ["❯ "])
    assert has_ready_prompt(old) is True


# ---------------------------------------------------------------------------
# strip_ansi
# ---------------------------------------------------------------------------


def test_strip_ansi_removes_csi_color_codes():
    colored = "\x1b[31mred text\x1b[0m plain"
    assert strip_ansi(colored) == "red text plain"


def test_strip_ansi_removes_osc8_hyperlinks():
    linked = "pre\x1b]8;;http://example.com\x1b\\label\x1b]8;;\x1b\\post"
    cleaned = strip_ansi(linked)
    assert "example.com" not in cleaned
    assert "label" in cleaned


def test_strip_ansi_leaves_plain_text_untouched():
    assert strip_ansi("hello world") == "hello world"


# ---------------------------------------------------------------------------
# strip_formatting_for_tts
# ---------------------------------------------------------------------------


def test_strip_formatting_removes_thinking_blocks():
    text = "before<thinking>secret plan</thinking>after"
    assert "secret plan" not in strip_formatting_for_tts(text)
    assert "before" in strip_formatting_for_tts(text)
    assert "after" in strip_formatting_for_tts(text)


def test_strip_formatting_removes_code_fences():
    text = "Here is code:\n```python\nprint('x')\n```\ndone"
    cleaned = strip_formatting_for_tts(text)
    assert "print" not in cleaned
    assert "done" in cleaned


def test_strip_formatting_strips_markdown_headers():
    text = "# Big Header\nbody text"
    cleaned = strip_formatting_for_tts(text)
    assert cleaned.startswith("Big Header")


def test_strip_formatting_removes_inline_code():
    text = "call `foo()` now"
    assert "foo" not in strip_formatting_for_tts(text)


def test_strip_formatting_unwraps_bold_italic():
    assert strip_formatting_for_tts("**bold** word") == "bold word"


def test_strip_formatting_collapses_triple_newlines():
    assert strip_formatting_for_tts("a\n\n\n\nb") == "a\n\nb"


# ---------------------------------------------------------------------------
# _extract_response — the load-bearing algorithm
# ---------------------------------------------------------------------------


def test_extract_response_happy_path():
    pane = (
        "> prior turn\n"
        "prior reply\n"
        "❯ what is two plus two\n"
        "⏺ Four.\n"
        "❯ "
    )
    got = BrainSession._extract_response(pane, "what is two plus two")
    assert got == "Four."


def test_extract_response_filters_tool_invocations():
    pane = (
        "❯ read the file\n"
        "⏺ Read(foo.py)\n"
        "  ⎿  Read 42 lines\n"
        "⏺ Here is what I found.\n"
        "─────────────────────\n"
        "? for shortcuts\n"
        "❯ "
    )
    got = BrainSession._extract_response(pane, "read the file")
    assert got == "Here is what I found."


def test_extract_response_returns_empty_when_message_missing():
    pane = "❯ some unrelated content\n❯ "
    assert BrainSession._extract_response(pane, "brand new message") == ""


def test_extract_response_strips_esc_to_interrupt_line():
    pane = (
        "❯ hello\n"
        "⏺ Hi there.\n"
        "(esc to interrupt)\n"
        "❯ "
    )
    got = BrainSession._extract_response(pane, "hello")
    assert got == "Hi there."


def test_extract_response_joins_multiple_prose_lines():
    pane = (
        "❯ tell me a story\n"
        "⏺ Once upon a time\n"
        "⏺ there was a robot.\n"
        "❯ "
    )
    got = BrainSession._extract_response(pane, "tell me a story")
    assert got == "Once upon a time\nthere was a robot."


# ---------------------------------------------------------------------------
# send() — full polling loop
# ---------------------------------------------------------------------------


def _stamp(message: str) -> str:
    """Mirror BrainSession.send's timestamp-prefix logic for fixtures."""
    import time
    return f"[{time.strftime('%Y-%m-%d %H:%M:%S %z', time.localtime())}] {message}"


def _pane_after_reply(message: str, reply: str) -> str:
    """Render a pane fixture as if Hope had sent ``message`` (with the
    timestamp prefix that production now adds) and the brain replied.
    """
    return f"❯ {_stamp(message)}\n⏺ {reply}\n❯ "


def test_send_returns_reply_when_pane_becomes_ready():
    # Poll 1: still thinking. Poll 2: done.
    outputs = [
        "❯ hi\n(esc to interrupt)\n",
        _pane_after_reply("hi", "Hello there."),
    ]
    orch = FakeOrchestrator(pane_outputs=outputs)
    session = BrainSession(
        orch,
        "hope-abcd",
        poll_interval_sec=0.01,
        send_timeout_sec=2.0,
    )
    reply = session.send("hi")
    assert reply == "Hello there."
    # send-keys -l + Enter were dispatched. The literal payload now
    # carries a leading timestamp prefix, so we check the last cmd
    # element ENDS WITH the original message rather than equals it.
    sent_cmds = [c for c in orch.tmux_calls if "send-keys" in c]
    assert any(
        "-l" in c and isinstance(c[-1], str) and c[-1].endswith("hi")
        for c in sent_cmds
    )
    assert any(c[-1] == "Enter" for c in sent_cmds)


def test_send_times_out_when_no_ready_prompt_appears():
    # Always thinking — never finishes.
    orch = FakeOrchestrator(
        pane_outputs=["❯ foo\n(esc to interrupt)\n"]
    )
    session = BrainSession(
        orch,
        "hope-abcd",
        poll_interval_sec=0.01,
        send_timeout_sec=0.05,
    )
    reply = session.send("foo")
    assert reply == BrainSession._TIMEOUT_REPLY


def test_send_returns_empty_when_pane_id_unknown():
    orch = FakeOrchestrator(pane_id="hope-abcd")
    session = BrainSession(orch, "does-not-exist", poll_interval_sec=0.01)
    assert session.send("hi") == ""


def test_send_empty_message_is_noop():
    orch = FakeOrchestrator()
    session = BrainSession(orch, "hope-abcd")
    assert session.send("") == ""
    # No tmux dispatch at all.
    assert orch.tmux_calls == []


def test_send_cleans_ansi_and_formatting_from_reply():
    raw_reply = "\x1b[32mHello **world**\x1b[0m"
    pane = _pane_after_reply("ping", raw_reply)
    orch = FakeOrchestrator(pane_outputs=[pane])
    session = BrainSession(
        orch,
        "hope-abcd",
        poll_interval_sec=0.01,
        send_timeout_sec=1.0,
    )
    reply = session.send("ping")
    # strip_ansi removes color; strip_formatting_for_tts unwraps **bold**.
    assert reply == "Hello world"


# ---------------------------------------------------------------------------
# Live integration — only when HOPE_LIVE_BRAIN=1 and claude+tmux are present.
# ---------------------------------------------------------------------------


def _live_enabled() -> bool:
    if os.environ.get("HOPE_LIVE_BRAIN") != "1":
        return False
    if shutil.which("tmux") is None or shutil.which("claude") is None:
        return False
    return True


@pytest.mark.skipif(
    not _live_enabled(),
    reason="set HOPE_LIVE_BRAIN=1 and install tmux+claude to run live test",
)
def test_send_live_against_real_claude(tmp_path):
    """End-to-end: spawn a real hope-main pane, send a prompt, assert a reply."""
    from hope.agents.tmux_orchestrator import TmuxOrchestrator
    from hope.core.config import OrchestratorConfig

    cfg = OrchestratorConfig(
        tmux_session_name=f"hope-live-test-{os.getpid()}",
        panes_dir=str(tmp_path / "panes"),
        bus_socket_path=str(tmp_path / "bus.sock"),
    )
    orch = TmuxOrchestrator(config=cfg, db_path=str(tmp_path / "agents.db"))
    try:
        pane_id = orch.start()
        assert pane_id
        # Let Claude finish booting.
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            cap = orch.capture_pane(pane_id)
            if "❯" in cap and "esc to interrupt" not in cap:
                break
            time.sleep(0.5)

        session = BrainSession(orch, pane_id, send_timeout_sec=45.0)
        reply = session.send("say hello in 5 words or fewer")
        assert reply.strip(), f"empty reply; pane dump: {orch.capture_pane(pane_id)!r}"
        assert "hello" in reply.lower() or "hi" in reply.lower()
    finally:
        # Best-effort teardown.
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", cfg.tmux_session_name],
                check=False,
                capture_output=True,
            )
        except Exception:
            pass
        try:
            orch.shutdown()
        except Exception:
            pass
        try:
            orch.close()
        except Exception:
            pass
