"""Tools primitive — BaseTool ABC + the survivors after the voice-arch cleanup.

Only the knowledge/retrieval/storage tool surface remains (the sibling
RAG wiring depends on it). The legacy code-interpreter / shell / git /
browser / PDF / image / channel tools were deleted.
"""

from __future__ import annotations

from hope.tools._stubs import BaseTool, ToolExecutor, ToolSpec

# Import built-in tools to trigger @ToolRegistry.register() decorators.
# Each wrapped in try/except so the package loads even if individual
# modules are being refactored by sibling agents.
for _mod in (
    "hope.tools.retrieval",
    "hope.tools.storage_tools",
    "hope.tools.digest_collect",
    "hope.tools.knowledge_tools",
    "hope.tools.knowledge_search",
    "hope.tools.knowledge_sql",
    "hope.tools.scan_chunks",
):
    try:
        __import__(_mod)
    except ImportError:
        pass

__all__ = ["BaseTool", "ToolExecutor", "ToolSpec"]
