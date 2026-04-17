"""Hope agent wrapping a persistent Claude Code tmux pane.

Unlike :class:`hope.agents.claude_code.ClaudeCodeAgent` (which spawns a
one-shot Node.js runner and exits), this agent is paired with a *live*
tmux pane managed by :class:`hope.agents.tmux_orchestrator.TmuxOrchestrator`.
The pane holds a long-lived Claude Code CLI session; each ``run()`` call
serialises the task and hands it to the pane via the engine agent's
``ClaudeCodeTmuxEngine`` (sibling file ``hope/engine/claude_code_tmux.py``).

We keep this shape minimal on purpose — the agent is a thin adapter over
an engine-managed pane.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from hope.agents._stubs import AgentContext, AgentResult, BaseAgent
from hope.core.events import EventBus
from hope.core.registry import AgentRegistry
from hope.engine._stubs import InferenceEngine

logger = logging.getLogger(__name__)


@AgentRegistry.register("claude_code_tmux")
class ClaudeCodeTmuxAgent(BaseAgent):
    """An agent backed by a live Claude Code tmux pane.

    Instances are constructed by the orchestrator after ``spawn_specialist``
    returns. The caller supplies the ``pane_id``, ``role``, and an
    ``engine`` which is the pane-bound ``ClaudeCodeTmuxEngine`` — that
    engine knows how to write a request into the pane's FIFO and parse
    the sentinel-framed response out of the pane's captured output.
    """

    agent_id = "claude_code_tmux"
    accepts_tools = False
    _default_temperature = 0.7
    _default_max_tokens = 1024

    def __init__(
        self,
        engine: InferenceEngine,
        model: str = "",
        *,
        pane_id: str,
        role: str,
        is_ephemeral: bool = True,
        bus: Optional[EventBus] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> None:
        super().__init__(
            engine,
            model,
            bus=bus,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.pane_id = pane_id
        self.role = role
        self.is_ephemeral = is_ephemeral

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        """Serialize the task, dispatch it to the pane, return the result.

        The engine is expected to expose a ``dispatch(request: dict) -> dict``
        method (the sibling engine implementation honours this). We keep
        the adapter tolerant: if the engine only exposes ``generate`` we
        fall back to the generic BaseAgent path so the wrapper is usable
        before the tmux engine lands.
        """
        self._emit_turn_start(input)

        payload = {
            "pane_id": self.pane_id,
            "role": self.role,
            "task": input,
            "context": (
                {"messages": [m.__dict__ for m in context.conversation.messages]}
                if context and context.conversation and context.conversation.messages
                else {}
            ),
        }

        dispatch = getattr(self._engine, "dispatch", None)
        if callable(dispatch):
            try:
                raw = dispatch(payload)
            except Exception as exc:
                logger.exception("tmux engine dispatch failed for %s", self.pane_id)
                self._emit_turn_end(turns=1, error=True)
                return AgentResult(
                    content=f"Hope's {self.role} failed: {exc}",
                    turns=1,
                    metadata={"error": True, "pane_id": self.pane_id},
                )
            content = raw.get("content", "") if isinstance(raw, dict) else str(raw)
            metadata = raw.get("metadata", {}) if isinstance(raw, dict) else {}
            self._emit_turn_end(turns=1)
            return AgentResult(
                content=content,
                turns=1,
                metadata={
                    "pane_id": self.pane_id,
                    "role": self.role,
                    **metadata,
                },
            )

        # Fallback — no dispatch API, treat the payload as a plain prompt.
        messages = self._build_messages(json.dumps(payload))
        result = self._generate(messages)
        self._emit_turn_end(turns=1)
        return AgentResult(
            content=result.get("content", ""),
            turns=1,
            metadata={"pane_id": self.pane_id, "role": self.role},
        )


__all__ = ["ClaudeCodeTmuxAgent"]
