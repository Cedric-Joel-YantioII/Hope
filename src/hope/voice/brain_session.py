"""Tmux-backed Claude Code session I/O — direct port of cortexOS's BrainSession.

The daemon ships every voice transcript into the ``hope-main`` tmux pane.
Historically it was fire-and-forget — Hope never spoke back. This module
closes that gap by polling ``tmux capture-pane`` after the send, detecting
the Claude CLI's ready prompt (``❯``), and extracting the reply text
between the sent message and the prompt marker. The cleaned string is
what the daemon hands off to TTS.

Algorithms mirror
``/Users/joelc/Documents/Github/cortexOS/src/voice/brain-session.ts``
line-for-line so bug fixes stay portable across both projects.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type only
    from hope.agents.tmux_orchestrator import TmuxOrchestrator

logger = logging.getLogger(__name__)


# ANSI CSI (colors/cursor/erase), OSC-8 hyperlinks, OSC-07 (CWD report),
# and VT100 charset select. Identical set to the TS implementation.
_RE_ANSI_CSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_ANSI_OSC8 = re.compile(r"\x1b\]8;;[^\x1b]*\x1b\\")
_RE_ANSI_OSC07 = re.compile(r"\x1b\][^\x07]*\x07")
_RE_ANSI_CHARSET = re.compile(r"\x1b[()][AB012]")

_RE_THINKING = re.compile(r"<thinking>.*?</thinking>", re.IGNORECASE | re.DOTALL)
_RE_TOOL_USE = re.compile(r"<tool_use>.*?</tool_use>", re.IGNORECASE | re.DOTALL)
_RE_RESULT = re.compile(r"<result>.*?</result>", re.IGNORECASE | re.DOTALL)
_RE_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_RE_INLINE_CODE = re.compile(r"`[^`]+`")
_RE_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RE_BOLD_ITALIC = re.compile(r"\*{1,3}([^*]+)\*{1,3}")
_RE_TRIPLE_NEWLINE = re.compile(r"\n{3,}")

_RE_SEPARATOR = re.compile(r"^[─━\-=]{5,}$")

# Claude Code completion markers: "✻ Crunched for 3s",
# "✻ Worked for 6m 6s", "✻ Cooked for 4s", "✻ Baked for 2s",
# "✻ Churned for 12s", etc. These appear AFTER the brain finishes
# and are part of the chrome, not the response.
_RE_COMPLETION_MARKER = re.compile(
    r"^[\u2733\u2738\u2722-\u2730\u2732\u2734-\u2737\u2739-\u273F\u272F]\s+"
    r"[A-Z]\w+\s+for\s+\d+(?:[smh]|m|min|s)?\s*\d*\s*[smh]?\s*$"
)

# Claude Code session-rating options row: ``1: Bad    2: Fine   3: Good   0: Dismiss``
_RE_SESSION_RATING_OPTS = re.compile(
    r"^\s*\d\s*:\s*(?:Bad|Fine|Good|Dismiss)(?:\s+\d\s*:\s*(?:Bad|Fine|Good|Dismiss))+\s*$"
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from terminal output.

    Mirrors the 4-regex chain in the TS port — CSI (colors/moves/erase),
    OSC-8 hyperlinks, OSC-07 CWD reports, and VT100 charset select.
    """
    text = _RE_ANSI_CSI.sub("", text)
    text = _RE_ANSI_OSC8.sub("", text)
    text = _RE_ANSI_OSC07.sub("", text)
    text = _RE_ANSI_CHARSET.sub("", text)
    return text


def strip_formatting_for_tts(text: str) -> str:
    """Strip Claude Code formatting artifacts that shouldn't be spoken.

    Matches the TS implementation: thinking/tool_use/result blocks,
    fenced and inline code, markdown headers, bold/italic markers, and
    collapsed triple newlines.
    """
    cleaned = text
    cleaned = _RE_THINKING.sub("", cleaned)
    cleaned = _RE_TOOL_USE.sub("", cleaned)
    cleaned = _RE_RESULT.sub("", cleaned)
    cleaned = _RE_CODE_FENCE.sub("", cleaned)
    cleaned = _RE_INLINE_CODE.sub("", cleaned)
    cleaned = _RE_HEADER.sub("", cleaned)
    cleaned = _RE_BOLD_ITALIC.sub(r"\1", cleaned)
    cleaned = _RE_TRIPLE_NEWLINE.sub("\n\n", cleaned)
    return cleaned.strip()


# Spinner glyphs Claude Code 2.x cycles through while a turn is in flight.
# All variants observed in the wild — claude rotates these for the
# "thinking" indicator ("✽ Crunching…", "✻ Cooking…", "✢ Transfiguring…").
# Note ✻ overloads — present participle ("✻ Cooking…") is the spinner;
# past tense ("✻ Cooked for 3s") is a completion marker that lacks U+2026.
# That is why _is_thinking_line REQUIRES BOTH the glyph AND U+2026.
_SPINNER_GLYPHS = (
    "✽", "✶", "✸", "✹", "✺", "✻", "✢", "✣", "✤", "✥",
    "✦", "✩", "✪", "✫", "✬", "✭", "✮", "✯", "✰",
)
# The persistent status bar at the bottom of the pane carries the
# model label ("Opus 4.7 …") that uses U+2026 even when the brain is
# idle. Strip the trailing N lines before scanning for live spinners.
_STATUS_BAR_TAIL_LINES = 3


def _is_thinking_line(line: str) -> bool:
    """A line is a live spinner iff it (a) ends with U+2026 after
    rstrip, OR (b) carries U+2026 immediately after a present-
    participle verb that lives in the spinner-text vocabulary
    ("Perambulating…(running stop hook ...)").

    Past-tense markers like "✻ Crunched for 3s" have a glyph but
    no U+2026 — they are completion markers, not spinners.
    """
    stripped = line.rstrip()
    if stripped.endswith("\u2026"):
        return True
    # Spinner lines look like "<glyph> <CapVerb><lower>…<rest>" — the
    # ellipsis lives mid-line when claude appends progress/tool info
    # after the verb. Detect by: any U+2026 + a leading non-ASCII
    # glyph + present-participle verb (ends with "ing").
    if "\u2026" not in stripped:
        return False
    parts = stripped.split(None, 2)
    if len(parts) < 2:
        return False
    glyph, verb = parts[0], parts[1]
    if len(glyph) > 2 or glyph.isascii():
        return False
    return verb.endswith("\u2026") or verb.endswith("ing\u2026") or "\u2026" in verb


def has_ready_prompt(output: str) -> bool:
    """Return True iff the last 6 non-empty lines indicate Claude is idle.

    Claude Code renders ❯ in the UI chrome even while thinking, so a
    naive ``"❯" in output`` check would race. We require ❯ AND the
    absence of the "esc to interrupt" / "Ionizing" / "Thinking" hints
    that only appear while a turn is in flight.
    """
    lines = [line for line in output.split("\n") if line.strip()]
    if not lines:
        return False
    tail = "\n".join(lines[-6:])
    if "esc to interrupt" in tail:
        return False
    if "Ionizing" in tail or "Thinking" in tail:
        return False
    # Strip the persistent status bar — its model label
    # ("Opus 4.7 …") contains U+2026 even when claude is idle.
    # Real spinners only live in the conversation area, so scan the
    # body separately. A thinking line has BOTH a spinner glyph and
    # U+2026 (e.g. "✽ Crunching…"); a completion marker like
    # "✻ Crunched for 3s" has the glyph but no U+2026.
    if len(lines) > _STATUS_BAR_TAIL_LINES:
        body_lines = lines[:-_STATUS_BAR_TAIL_LINES]
    else:
        body_lines = lines
    for line in body_lines:
        if _is_thinking_line(line):
            return False
    # Subagent activity row: "(53s · … · thought for 1s) subagent".
    if " subagent" in tail and "·" in tail and "\u2026" in tail:
        return False
    return "❯" in tail


class BrainSession:
    """Sends a prompt to the hope-main tmux pane and extracts Claude's reply.

    Direct port of cortexOS's BrainSession — polls ``capture-pane`` for
    the ready prompt, then scrapes the text between the sent message
    marker and the prompt marker. Designed to be a throwaway per-send
    object OR cached on the daemon; both shapes are safe because the
    object holds no mutable state beyond construction args.
    """

    # 100 ms keeps end-of-turn detection near the floor of human-perceptible
    # delay (~80 ms) without flooring tmux capture-pane. The TS port runs
    # 500 ms; we diverge intentionally because the daemon is the one piece
    # of the pipeline that ALWAYS runs on the same box as the user. With
    # 100 ms polling, the natural "spinner gone → prompt back" window in
    # Claude Code 2.x already gives us streaming-like behaviour for free —
    # the brain's prose is captured within a beat of being final, so a
    # separate streaming path isn't worth its complexity.
    POLL_INTERVAL_SEC = 0.1
    # Multi-step actions (Chrome navigation + osascript + screenshot
    # verification) regularly run 5+ minutes. The old 60s ceiling caused
    # the daemon to silently bail with an empty reply while the brain
    # was still finishing — user got no audible confirmation even though
    # the work completed in the pane. Bumped to 8 min; tasks legitimately
    # longer than that should announce progress and resume separately.
    SEND_TIMEOUT_SEC = 480.0
    _CAPTURE_LINES = 200
    # Empty on timeout — the daemon checks for an empty reply and skips
    # TTS. Previously we spoke "I took too long on that. Try again."
    # which itself re-entered the mic and perpetuated the problem.
    _TIMEOUT_REPLY = ""

    def __init__(
        self,
        orchestrator: "TmuxOrchestrator",
        pane_id: str,
        *,
        poll_interval_sec: float = POLL_INTERVAL_SEC,
        send_timeout_sec: float = SEND_TIMEOUT_SEC,
    ) -> None:
        self._orchestrator = orchestrator
        self._pane_id = pane_id
        self._poll_interval_sec = poll_interval_sec
        self._send_timeout_sec = send_timeout_sec

    @property
    def pane_id(self) -> str:
        return self._pane_id

    def send(self, message: str) -> str:
        """Send *message* to the pane and return the cleaned reply.

        Algorithm:
          1. Look up the pane's tmux target via the orchestrator registry.
          2. ``send-keys -l`` the message, then ``Enter``.
          3. Every :attr:`POLL_INTERVAL_SEC`, capture the pane, strip
             ANSI, and if ``has_ready_prompt`` AND the message prefix is
             visible, attempt extraction.
          4. First non-empty extraction wins; timeout → friendly string.

        Never raises — wraps unexpected errors into a user-facing reply
        so a broken tmux pipe can never silence Hope.
        """
        if not message:
            return ""

        try:
            entry = self._orchestrator.registry.get(self._pane_id)
            if entry is None:
                logger.warning(
                    "BrainSession.send: no registry entry for pane_id=%r",
                    self._pane_id,
                )
                return ""
            target = entry.tmux_target

            # Prepend an absolute + ISO local timestamp so the brain
            # always knows when the user spoke (LLMs have no clock).
            # Format: "[2026-05-09 13:14:08 -0500] <message>".
            stamp = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime())
            stamped = f"[{stamp}] {message}"
            # send-keys -l (literal) so shell metachars in the transcript
            # don't get interpreted; followed by Enter to submit.
            r1 = self._orchestrator._tmux(
                ["tmux", "send-keys", "-t", target, "-l", "--", stamped],
                check=False,
            )
            r2 = self._orchestrator._tmux(
                ["tmux", "send-keys", "-t", target, "Enter"],
                check=False,
            )
            logger.info(
                "BrainSession.send: dispatched to pane=%s target=%s "
                "text_rc=%s enter_rc=%s text=%r",
                self._pane_id,
                target,
                r1.returncode,
                r2.returncode,
                message[:80],
            )
            if r1.returncode != 0 or r2.returncode != 0:
                logger.warning(
                    "BrainSession.send: tmux send-keys non-zero — "
                    "text_stderr=%r enter_stderr=%r",
                    (r1.stderr or "")[:200],
                    (r2.stderr or "")[:200],
                )

            deadline = time.monotonic() + self._send_timeout_sec
            response = ""
            poll_count = 0
            # Match against a stable substring of the actual sent text
            # (which now starts with the timestamp prefix) so the
            # response-extraction needle still anchors correctly.
            message_prefix = stamped[:40] if len(stamped) >= 40 else stamped
            # Heartbeat cadence is poll-rate dependent — log every ~10 s.
            heartbeat_every = max(1, int(round(10.0 / self._poll_interval_sec)))

            while time.monotonic() < deadline:
                time.sleep(self._poll_interval_sec)
                raw = self._orchestrator.capture_pane(
                    self._pane_id, lines=self._CAPTURE_LINES
                )
                cleaned = strip_ansi(raw)
                poll_count += 1

                # Heartbeat so logs show the loop is alive while Claude thinks.
                if poll_count % heartbeat_every == 0:
                    elapsed = int(
                        self._send_timeout_sec
                        - (deadline - time.monotonic())
                    )
                    logger.info(
                        "BrainSession: waiting for response... (%ds)", elapsed
                    )

                if has_ready_prompt(cleaned) and message_prefix in cleaned:
                    response = self._extract_response(cleaned, stamped)
                    if response:
                        break

            if not response:
                logger.warning(
                    "BrainSession.send: timeout after %.1fs pane=%s",
                    self._send_timeout_sec,
                    self._pane_id,
                )
                return self._TIMEOUT_REPLY

            return strip_formatting_for_tts(response)
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("BrainSession.send failed: %s", exc)
            return "Something went wrong processing that. Try again."

    @staticmethod
    def _extract_response(pane_output: str, sent_message: str) -> str:
        """Pull the assistant text out of the pane scrollback.

        Strategy (identical to the TS implementation):
          * Scan backward for the last line containing the first 50 chars
            of the sent message.
          * Scan backward for the last prompt line (``❯``, or a line that
            is/ends with ``>``) above that.
          * Slice lines between them (exclusive of both), drop Claude
            Code chrome (tool calls, separators, shortcut hints), and
            strip the leading ``⏺`` bullet from response lines.
        """
        lines = pane_output.split("\n")

        message_needle = sent_message[:50]
        message_line_index = -1
        for i in range(len(lines) - 1, -1, -1):
            if message_needle in lines[i]:
                message_line_index = i
                break

        if message_line_index == -1:
            return ""

        prompt_line_index = len(lines) - 1
        for i in range(len(lines) - 1, message_line_index, -1):
            trimmed = lines[i].strip()
            if "❯" in trimmed or trimmed == ">" or trimmed.endswith(">"):
                prompt_line_index = i
                break

        response_lines = lines[message_line_index + 1 : prompt_line_index]

        # A real agent turn always emits at least one ⏺-prefixed line.
        # Without this guard, when the brain hasn't started replying yet
        # AND the pane already shows a stale "❯" from the previous turn,
        # any wrap-continuation of the user message that lands below the
        # match line gets returned as if it were the agent reply.
        # Tightening the poll interval to 100 ms made this race far more
        # frequent, so we now require a positive signal from the agent
        # before considering the slice valid.
        if not any(rl.lstrip().startswith("⏺") for rl in response_lines):
            return ""

        kept: list[str] = []
        for raw_line in response_lines:
            line = raw_line.strip()
            if not line:
                # Preserve paragraph breaks so the daemon's bottom-up
                # last-paragraph scan in ``_truncate_for_speech`` can
                # find Claude's final answer instead of the "Let me
                # check…" / "Found it…" preamble that Claude Code 2.x
                # emits between tool calls.
                if kept and kept[-1] != "":
                    kept.append("")
                continue
            # ⏺ Bash(...) / ⏺ Read(...) — tool invocation headers.
            if line.startswith("⏺") and "(" in line:
                continue
            # ⎿ ...  — tool output rendering.
            if line.startswith("⎿"):
                continue
            if _RE_SEPARATOR.match(line):
                continue
            # Completion markers like "✻ Worked for 4s" — Claude
            # Code chrome printed AFTER the actual response, not
            # response prose.
            if _RE_COMPLETION_MARKER.match(line):
                continue
            if line.startswith("?") and "shortcut" in line:
                continue
            if "esc to interrupt" in line:
                continue
            if line == "⏺":
                continue
            # Claude Code session-rating prompt — periodically pops in
            # the pane and ends up being read aloud as "Black circle,
            # how is Claude doing this session" if not stripped. The
            # block looks like:
            #   ● How is Claude doing this session? (optional)
            #   1: Bad    2: Fine   3: Good   0: Dismiss
            # Note ● (U+25CF) is distinct from ⏺ (U+23FA) which prefixes
            # real agent prose.
            if line.startswith("●"):
                continue
            if "How is Claude doing this session" in line:
                continue
            if _RE_SESSION_RATING_OPTS.match(line):
                continue
            # Strip the leading ⏺ that Claude Code prefixes onto prose.
            if line.startswith("⏺"):
                line = line[1:].strip()
            kept.append(line)

        # Trim trailing paragraph break so split('\n\n') doesn't yield
        # an empty final chunk that fools the last-paragraph scan.
        while kept and kept[-1] == "":
            kept.pop()

        return "\n".join(kept)


__all__ = [
    "BrainSession",
    "has_ready_prompt",
    "strip_ansi",
    "strip_formatting_for_tts",
]
