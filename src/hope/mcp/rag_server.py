"""Stdio MCP server exposing Hope's RAG memory + digest tools to the brain.

The Claude Code pane (the "brain") auto-loads ``.mcp.json`` from the
project root at boot; that file launches this module as a subprocess and
talks JSON-RPC over its stdin/stdout. The brain then sees
``memory_store`` / ``memory_retrieve`` / ``memory_search`` /
``memory_index`` and ``digest_collect`` as callable tools — no
orchestrator changes required.

Design notes
------------
* **Singleton backend.** The server binds every memory tool instance to
  the single :class:`RAGMemory` the daemon itself uses via
  :func:`hope.memory.get_rag`. That keeps the pane's writes visible to
  background jobs (nightly consolidation, digest) and vice-versa.
* **No orchestrator dependency.** We rebuild the tool list explicitly
  rather than calling :meth:`MCPServer._auto_discover_tools` because
  auto-discover includes heavy tools (``llm_tool``, ``code_interpreter``)
  the brain already has via the Claude Code harness.
* **Zero side effects on import.** Tool registration happens inside
  :func:`main`; importing the module doesn't touch ``~/.hope`` or load
  BGE. This matters for tests.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, List

from hope.mcp.protocol import MCPRequest
from hope.mcp.server import MCPServer
from hope.tools._stubs import BaseTool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool builders
# ---------------------------------------------------------------------------


def _build_memory_tools(backend: Any) -> List[BaseTool]:
    """Instantiate the four memory tools, each wired to *backend*."""
    from hope.tools.storage_tools import (
        MemoryIndexTool,
        MemoryRetrieveTool,
        MemorySearchTool,
        MemoryStoreTool,
    )

    return [
        MemoryStoreTool(backend=backend),
        MemoryRetrieveTool(backend=backend),
        MemorySearchTool(backend=backend),
        MemoryIndexTool(backend=backend),
    ]


def _build_digest_tool() -> List[BaseTool]:
    """Instantiate the digest_collect tool.

    Lives in this server so the voice path ("brief me" / "good morning")
    can reach the same digest implementation as ``hope digest``. Importing
    the module also registers every connector via
    ``hope.connectors.__init__``.
    """
    try:
        from hope.tools.digest_collect import DigestCollectTool

        return [DigestCollectTool()]
    except Exception:
        logger.exception("digest_collect tool unavailable")
        return []


def build_server() -> MCPServer:
    """Assemble the stdio MCP server with RAG + digest tools."""
    from hope.memory import get_rag

    rag = get_rag()
    tools: List[BaseTool] = []
    tools.extend(_build_memory_tools(rag.backend))
    tools.extend(_build_digest_tool())
    return MCPServer(tools=tools)


# ---------------------------------------------------------------------------
# stdio loop
# ---------------------------------------------------------------------------


def _serve(server: MCPServer, stdin: Any, stdout: Any) -> None:
    """Pump JSON-RPC requests from *stdin*, write responses to *stdout*.

    One JSON object per line. EOF on stdin terminates the loop (the brain
    has closed the pipe). Every response is flushed so the pane sees
    output promptly; Claude Code's stdio transport expects line-delimited
    output with no buffering.
    """
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            # Detect notifications (no "id" key) at the parse layer —
            # ``MCPRequest.from_json`` defaults missing ids to 0, which
            # would otherwise swallow the distinction.
            parsed = json.loads(line)
            is_notification = "id" not in parsed
            req = MCPRequest.from_json(line)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("invalid JSON-RPC request: %s", exc)
            continue
        resp = server.handle(req)
        if is_notification:
            continue
        stdout.write(resp.to_json() + "\n")
        stdout.flush()


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001 - argv unused
    """Entrypoint — serve forever on stdin/stdout."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        # Log to stderr so the stdio JSON-RPC channel stays clean.
        stream=sys.stderr,
    )
    try:
        server = build_server()
    except Exception:
        logger.exception("rag_server failed to build")
        return 1
    logger.info(
        "hope rag_server ready: tools=%s",
        sorted(t.spec.name for t in server.get_tools()),
    )
    try:
        _serve(server, sys.stdin, sys.stdout)
    except KeyboardInterrupt:  # pragma: no cover - signal path
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_server", "main"]
