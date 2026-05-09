"""Agents primitive — specialist agents spawned by the voice daemon.

After the voice-arch cleanup the agent roster is lean:

- Persistent specialists (tmux_orchestrator, specialist_registry)
- Reference patterns kept as spawnable specialists:
  ``rlm``, ``rlm_repl``, ``monitor_operative``, ``deep_research``,
  ``morning_digest``, ``digest_store``.

All registry-populating imports below are wrapped in try/except so the
package still loads if a particular agent's optional deps are missing.
"""

from __future__ import annotations

import logging

from hope.agents._stubs import (
    AgentContext,
    AgentResult,
    BaseAgent,
    ToolUsingAgent,
)

logger = logging.getLogger(__name__)

# Import agent modules to trigger @AgentRegistry.register() decorators
for _mod in (
    "hope.agents.rlm",
    "hope.agents.rlm_repl",
    "hope.agents.monitor_operative",
    "hope.agents.deep_research",
    "hope.agents.morning_digest",
):
    try:
        __import__(_mod)
    except ImportError as _exc:
        logger.debug("Optional agent %s not loaded: %s", _mod, _exc)

__all__ = ["AgentContext", "AgentResult", "BaseAgent", "ToolUsingAgent"]
