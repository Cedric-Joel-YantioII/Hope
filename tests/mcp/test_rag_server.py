"""Tests for the stdio MCP server that the brain pane talks to.

The production transport is subprocess stdin/stdout, but we don't need
to actually fork here — :func:`_serve` reads from any iterable, writes
to any file-like object, so we feed it StringIO and assert the JSON-RPC
line that comes back. That covers:

  1. tools/list includes memory_store / memory_search / digest_collect.
  2. tools/call dispatches to the memory backend wired at build time.
  3. Mocked round-trip: store then search via the stdio loop.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional

import pytest

from hope.mcp import rag_server
from hope.mcp.protocol import MCPRequest
from hope.memory import reset_rag
from hope.tools.storage._stubs import MemoryBackend, RetrievalResult


class _FakeBackend(MemoryBackend):
    """Tiny in-memory backend — no SQLite, no FAISS, deterministic."""

    backend_id = "fake_rag"

    def __init__(self) -> None:
        self._docs: List[tuple] = []

    def store(
        self,
        content: str,
        *,
        source: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        self._docs.append((content, source, metadata or {}))
        return f"doc_{len(self._docs)}"

    def retrieve(
        self, query: str, *, top_k: int = 5, **kwargs: Any,
    ) -> List[RetrievalResult]:
        out = []
        for c, s, m in self._docs:
            if query.lower() in c.lower():
                out.append(RetrievalResult(content=c, score=1.0, source=s, metadata=m))
        return out[:top_k]

    def delete(self, doc_id: str) -> bool:  # pragma: no cover - unused
        return False

    def clear(self) -> None:
        self._docs.clear()


@pytest.fixture(autouse=True)
def _reset_rag():
    reset_rag()
    yield
    reset_rag()


@pytest.fixture()
def server(monkeypatch):
    """Build the stdio server bound to an in-memory fake backend."""
    from hope.memory.rag import RAGMemory

    fake = _FakeBackend()

    # ``build_server`` does a local ``from hope.memory import get_rag``
    # inside the function, so we patch the source module attribute that
    # the import resolves to.
    def _fake_get_rag():
        return RAGMemory(backend=fake, sparse=fake, dense=None)

    monkeypatch.setattr("hope.memory.get_rag", _fake_get_rag)
    monkeypatch.setattr("hope.memory.rag.get_rag", _fake_get_rag)
    return rag_server.build_server()


class TestToolsList:
    def test_memory_tools_registered(self, server):
        req = MCPRequest(method="tools/list", id=1)
        resp = server.handle(req)
        names = {t["name"] for t in resp.result["tools"]}
        assert "memory_store" in names
        assert "memory_search" in names
        assert "memory_retrieve" in names
        assert "memory_index" in names

    def test_digest_tool_registered(self, server):
        req = MCPRequest(method="tools/list", id=1)
        resp = server.handle(req)
        names = {t["name"] for t in resp.result["tools"]}
        assert "digest_collect" in names


class TestStdioRoundTrip:
    def test_store_then_search(self, server):
        """Pump two JSON-RPC calls through the stdio loop."""
        store_req = MCPRequest(
            method="tools/call",
            id=1,
            params={
                "name": "memory_store",
                "arguments": {"content": "Hope runs locally.", "source": "doc"},
            },
        )
        search_req = MCPRequest(
            method="tools/call",
            id=2,
            params={"name": "memory_search", "arguments": {"query": "locally"}},
        )
        stdin = io.StringIO(store_req.to_json() + "\n" + search_req.to_json() + "\n")
        stdout = io.StringIO()
        rag_server._serve(server, stdin, stdout)
        lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 2

        store_resp = json.loads(lines[0])
        assert store_resp["id"] == 1
        assert "Stored" in store_resp["result"]["content"][0]["text"]

        search_resp = json.loads(lines[1])
        assert search_resp["id"] == 2
        text = search_resp["result"]["content"][0]["text"]
        assert "locally" in text.lower()

    def test_notification_gets_no_reply(self, server):
        """Notifications (id=None) are one-way — no stdout output."""
        req = MCPRequest(method="tools/list", id=None)
        stdin = io.StringIO(req.to_json() + "\n")
        stdout = io.StringIO()
        rag_server._serve(server, stdin, stdout)
        assert stdout.getvalue() == ""
