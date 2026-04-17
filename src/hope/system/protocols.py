"""Structural protocols for substituting fakes in place of HopeSystem."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional, Protocol

if TYPE_CHECKING:
    from hope.core.config import HopeConfig
    from hope.core.events import EventBus
    from hope.engine._stubs import InferenceEngine
    from hope.security.capabilities import CapabilityPolicy
    from hope.sessions.session import SessionStore
    from hope.tools._stubs import BaseTool
    from hope.tools.storage._stubs import MemoryBackend
    from hope.traces.collector import TraceCollector
    from hope.traces.store import TraceStore


class OrchestratorDeps(Protocol):
    """Minimum surface of HopeSystem that QueryOrchestrator depends on.

    Tests can satisfy this with a lightweight class — no need to construct
    the full HopeSystem dataclass or materialize every subsystem.
    """

    config: HopeConfig
    bus: EventBus
    engine: InferenceEngine
    engine_key: str
    model: str
    agent_name: str
    tools: List[BaseTool]
    memory_backend: Optional[MemoryBackend]
    capability_policy: Optional[CapabilityPolicy]
    session_store: Optional[SessionStore]
    trace_store: Optional[TraceStore]
    trace_collector: Optional[TraceCollector]  # written by _run_agent

    # Optional attribute (getattr with default) — declared for type clarity.
    _skill_few_shot_examples: Any
