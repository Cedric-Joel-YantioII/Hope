"""Claude Code CLI (persistent tmux session) inference engine backend.

Hope's ``brain`` runs as a persistent ``claude --dangerously-skip-permissions``
CLI session inside a tmux pane.  Each pane inherits the project's CLAUDE.md,
MCP tools, and ``.claude/skills/`` automatically.  That gives Hope a stateful
reasoning backend that is *not* just ``claude -p``: conversation context,
cached tool state, and skill/MCP configuration persist between requests.

This engine pipes a prompt into the pane via ``tmux send-keys``, then tails a
FIFO (set up by the orchestrator; the pane's stdout is ``tee``'d into it) for
the response.  Each request is framed with unique UUID sentinels so
interleaved output can be disambiguated and the end of Claude's reply can be
detected reliably.

Lifecycle assumptions:
    - The orchestrator owns pane creation and teardown.  This engine does
      NOT spawn or kill panes.
    - The FIFO already exists and is being fed by the orchestrator's
      ``tee``/``pipe-pane`` setup.
    - One pane target serves one engine instance.  Per-pane locking prevents
      request interleaving; concurrent callers will serialize.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Dict, List

from hope.core.registry import EngineRegistry
from hope.core.types import Message, Role
from hope.engine._base import InferenceEngine, estimate_prompt_tokens

logger = logging.getLogger(__name__)


DEFAULT_MODEL_ID = "claude-code"


@EngineRegistry.register("claude_code_tmux")
class ClaudeCodeTmuxEngine(InferenceEngine):
    """Talk to a persistent Claude Code CLI session via tmux send-keys + FIFO.

    Parameters
    ----------
    pane_target:
        tmux target spec (``session:window.pane``) identifying the pane that
        hosts the Claude Code CLI session.
    fifo_path:
        Filesystem path to the FIFO that receives the pane's stdout.  The
        orchestrator is expected to create this FIFO and attach ``pipe-pane``
        to it before the engine is used.
    request_timeout_sec:
        Maximum wall-clock time to wait for the end-sentinel on a single
        request, including Claude's full think/tool-use cycle.
    sentinel_prefix:
        Prefix used for request framing.  A unique UUID is appended per
        request; the engine waits for ``{prefix}_END_{uuid}>>>`` to appear on
        the FIFO.
    """

    engine_id = "claude_code_tmux"
    is_cloud = False

    def __init__(
        self,
        pane_target: str = "hope:0.0",
        fifo_path: str = "~/.hope/panes/hope-main.fifo",
        request_timeout_sec: float = 120.0,
        sentinel_prefix: str = "---HOPE_PANE",
    ) -> None:
        self._pane_target = pane_target
        self._fifo_path = str(Path(fifo_path).expanduser())
        self._request_timeout = float(request_timeout_sec)
        self._sentinel_prefix = sentinel_prefix
        # Per-instance lock: panes are stateful; interleaving requests would
        # corrupt Claude's context.  Use a threading lock for sync generate()
        # and an asyncio lock for stream().  The asyncio lock is created
        # lazily so the engine is importable without a running event loop.
        self._sync_lock = threading.Lock()
        self._async_lock: asyncio.Lock | None = None

    # ------------------------------------------------------------------
    # Framing helpers
    # ------------------------------------------------------------------

    def _start_sentinel(self, rid: str) -> str:
        return f"{self._sentinel_prefix}_REQ_{rid}>>>"

    def _end_sentinel(self, rid: str) -> str:
        return f"{self._sentinel_prefix}_END_{rid}>>>"

    def _messages_to_prompt(self, messages: Sequence[Message]) -> str:
        """Serialize messages into a natural-language prompt for Claude Code.

        Claude Code handles tool-calling internally inside the pane, so we
        drop ``tool_calls`` and ``tool_call_id`` fields.  Role markers use a
        simple ``[role]`` tag that Claude reads as ordinary conversation.
        """
        parts: list[str] = []
        for msg in messages:
            if msg.role == Role.TOOL:
                # Claude Code handles tools inside the pane; skip tool
                # messages entirely to avoid confusing it with echoed
                # tool output from a previous turn of Hope's orchestrator.
                continue
            parts.append(f"[{msg.role.value}]\n{msg.content.strip()}")
        return "\n\n".join(parts)

    def _framed_prompt(self, prompt: str, rid: str) -> str:
        """Wrap the prompt with unique sentinels.

        The start sentinel is echoed to the FIFO stream (so tailers can pin
        the beginning of this request's output), and the end sentinel is
        appended as a standalone marker Claude is asked to echo verbatim
        after the assistant's reply.
        """
        start = self._start_sentinel(rid)
        end = self._end_sentinel(rid)
        # Instruct the pane to echo the end sentinel *after* its answer.
        # We also put the start sentinel on its own line so the tailer
        # can cleanly pin the request boundary.
        return (
            f"{start}\n"
            f"{prompt}\n\n"
            f"When you are fully done answering, print exactly this line "
            f"on its own with nothing else: {end}"
        )

    # ------------------------------------------------------------------
    # tmux / FIFO I/O
    # ------------------------------------------------------------------

    def _session_name(self) -> str:
        """Extract the tmux session name from ``session:window.pane``."""
        return self._pane_target.split(":", 1)[0]

    def _send_to_pane(self, payload: str) -> None:
        """Write *payload* into the pane via ``tmux send-keys`` + Enter.

        The payload is sent as a single literal argument to avoid shell
        quoting surprises.  A trailing ``Enter`` keypress submits it to the
        Claude Code REPL.
        """
        cmd = [
            "tmux",
            "send-keys",
            "-t",
            self._pane_target,
            "-l",  # literal: do not interpret payload as tmux keys
            payload,
        ]
        subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603
        subprocess.run(
            ["tmux", "send-keys", "-t", self._pane_target, "Enter"],  # noqa: S603, S607
            check=True,
            capture_output=True,
        )

    def _pane_exists(self) -> bool:
        """Return True if the tmux session/pane currently exists."""
        try:
            result = subprocess.run(  # noqa: S603
                ["tmux", "has-session", "-t", self._session_name()],  # noqa: S607
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def _fifo_writable(self) -> bool:
        """True if the configured FIFO exists and is open for reading.

        We don't try to write to it — we only *read* from it — but we do need
        the path to exist as a FIFO/file node the orchestrator can feed.
        """
        try:
            return os.path.exists(self._fifo_path)
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Blocking read of the FIFO until the end sentinel is seen
    # ------------------------------------------------------------------

    def _read_until_sentinel_sync(self, start_sentinel: str, end_sentinel: str) -> str:
        """Synchronously read the FIFO, returning text between sentinels.

        The FIFO is opened in read mode (blocks until a writer is present —
        which is fine because the orchestrator's ``tee`` keeps it hot).
        We scan for ``start_sentinel`` first, then accumulate until
        ``end_sentinel`` appears, then strip and return.
        """
        deadline = time.monotonic() + self._request_timeout
        # Line-buffered text read.  The FIFO is typically fed by ``tee``,
        # which writes line-at-a-time.
        with open(self._fifo_path, "r", buffering=1) as fh:
            saw_start = False
            buf: list[str] = []
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"claude_code_tmux: no end sentinel within "
                        f"{self._request_timeout}s on {self._fifo_path}"
                    )
                line = fh.readline()
                if not line:
                    # Writer not ready yet (EOF on FIFO briefly) — yield CPU
                    # without hot-looping.
                    time.sleep(0.01)
                    continue
                if not saw_start:
                    if start_sentinel in line:
                        saw_start = True
                    continue
                if end_sentinel in line:
                    # Capture anything before the sentinel on the final line
                    trailing = line.split(end_sentinel, 1)[0]
                    if trailing:
                        buf.append(trailing)
                    break
                buf.append(line)
        return "".join(buf).strip()

    async def _read_until_sentinel_async(
        self,
        start_sentinel: str,
        end_sentinel: str,
    ) -> AsyncIterator[str]:
        """Async generator: yield text fragments as the FIFO emits them.

        Yields content between the start and end sentinels in roughly
        line-sized chunks so callers can stream tokens.  Returns without
        yielding once the end sentinel is observed.  Offloads the blocking
        readline() to a thread via ``asyncio.to_thread``.
        """
        deadline = time.monotonic() + self._request_timeout
        # Opening the FIFO itself can block; push the open + reads off-loop.
        fh = await asyncio.to_thread(open, self._fifo_path, "r", buffering=1)
        try:
            saw_start = False
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"claude_code_tmux: no end sentinel within "
                        f"{self._request_timeout}s on {self._fifo_path}"
                    )
                line = await asyncio.to_thread(fh.readline)
                if not line:
                    await asyncio.sleep(0.01)
                    continue
                if not saw_start:
                    if start_sentinel in line:
                        saw_start = True
                    continue
                if end_sentinel in line:
                    trailing = line.split(end_sentinel, 1)[0]
                    if trailing:
                        yield trailing
                    return
                yield line
        finally:
            fh.close()

    # ------------------------------------------------------------------
    # InferenceEngine API
    # ------------------------------------------------------------------

    def generate(
        self,
        messages: Sequence[Message],
        *,
        model: str = DEFAULT_MODEL_ID,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Synchronously round-trip a prompt through the Claude Code pane.

        ``temperature`` and ``max_tokens`` are accepted for ABC compliance
        but ignored — the Claude Code CLI does not expose those knobs to
        external callers.  A debug log is emitted when they are set.
        """
        if temperature is not None or max_tokens is not None:
            logger.debug(
                "claude_code_tmux: ignoring temperature=%r / max_tokens=%r "
                "(Claude Code CLI does not expose these)",
                temperature,
                max_tokens,
            )

        rid = uuid.uuid4().hex
        prompt = self._framed_prompt(self._messages_to_prompt(messages), rid)

        with self._sync_lock:
            self._send_to_pane(prompt)
            content = self._read_until_sentinel_sync(
                self._start_sentinel(rid),
                self._end_sentinel(rid),
            )

        prompt_tokens = estimate_prompt_tokens(messages)
        completion_tokens = max(1, len(content.split()))
        return {
            "content": content,
            "model": DEFAULT_MODEL_ID,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "finish_reason": "stop",
        }

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str = DEFAULT_MODEL_ID,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream text chunks from the Claude Code pane incrementally."""
        if temperature is not None or max_tokens is not None:
            logger.debug(
                "claude_code_tmux: ignoring temperature=%r / max_tokens=%r "
                "(Claude Code CLI does not expose these)",
                temperature,
                max_tokens,
            )

        if self._async_lock is None:
            self._async_lock = asyncio.Lock()

        rid = uuid.uuid4().hex
        prompt = self._framed_prompt(self._messages_to_prompt(messages), rid)

        async with self._async_lock:
            await asyncio.to_thread(self._send_to_pane, prompt)
            async for chunk in self._read_until_sentinel_async(
                self._start_sentinel(rid),
                self._end_sentinel(rid),
            ):
                yield chunk

    # stream_full: we deliberately use the default base-class wrapper around
    # stream() so tool_calls stays None — Claude Code resolves tools itself
    # inside the pane, so exposing them here would be misleading.

    def list_models(self) -> List[str]:
        return [DEFAULT_MODEL_ID]

    def health(self) -> bool:
        """Pane must exist AND FIFO path must be present on disk."""
        return self._pane_exists() and self._fifo_writable()

    def close(self) -> None:
        """Release locks; leave pane and FIFO untouched.

        The orchestrator owns pane lifecycle, so we never kill the tmux
        session or remove the FIFO here.  We just drop our async lock
        reference so a fresh event loop can re-create it on next use.
        """
        self._async_lock = None


__all__ = ["ClaudeCodeTmuxEngine"]
