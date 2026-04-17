"""Tools primitive — tool system with ABC interface and built-in tools."""

from __future__ import annotations

from hope.tools._stubs import BaseTool, ToolExecutor, ToolSpec

# Import built-in tools to trigger @ToolRegistry.register() decorators.
# Each is wrapped in try/except so the package loads even before the
# individual tool modules are created.
try:
    import hope.tools.calculator  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.think  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.retrieval  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.llm_tool  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.file_read  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.web_search  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.code_interpreter  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.code_interpreter_docker  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.repl  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.storage_tools  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.mcp_adapter  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.channel_tools  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.http_request  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.shell_exec  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.memory_manage  # noqa: F401
except ImportError:
    pass
try:
    import hope.tools.user_profile_manage  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.skill_manage  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.file_write  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.apply_patch  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.git_tool  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.db_query  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.pdf_tool  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.image_tool  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.audio_tool  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.knowledge_tools  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.text_to_speech  # noqa: F401
except ImportError:
    pass

try:
    import hope.tools.digest_collect  # noqa: F401
except ImportError:
    pass

__all__ = ["BaseTool", "ToolExecutor", "ToolSpec"]
