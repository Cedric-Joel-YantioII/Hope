"""Engine primitive — InferenceEngine ABC and shared response types.

The concrete engine implementations (Ollama, cloud, vLLM, etc.) were
removed during the voice-arch cleanup. The ABC + runtime dataclasses in
``_stubs.py`` are still live — subclassed by ``GuardrailsEngine``, test
fakes, and referenced as runtime types by specialist agents (``rlm``,
``monitor_operative``, ``deep_research``) and sandbox/security helpers.

If you need a concrete engine, wire one through ``hope.core.registry``.
"""

from __future__ import annotations

from hope.engine._stubs import InferenceEngine, ResponseFormat, StreamChunk

__all__ = ["InferenceEngine", "ResponseFormat", "StreamChunk"]
