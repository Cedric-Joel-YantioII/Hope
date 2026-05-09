"""End-to-end subprocess tests for the stdio MCP RAG server.

The other test module (``test_rag_server.py``) exercises :func:`_serve`
in-process against a fake backend. These tests fork the real
``python -m hope.mcp.rag_server`` process exactly the way Claude Code
will when it auto-loads ``.mcp.json`` at boot, pipe JSON-RPC into its
stdin, and assert that:

1. The MCP ``initialize`` handshake returns our server info.
2. ``tools/list`` advertises the expected 5 tools (``memory_store``,
   ``memory_retrieve``, ``memory_search``, ``memory_index``,
   ``digest_collect``).
3. A real ``memory_store`` → ``memory_search`` round-trip round-trips
   through the live :class:`RAGMemory` singleton that backs the
   production daemon.

All subprocess IO is line-delimited JSON with stderr going to PIPE so
the tests stay quiet on success.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_JSON = REPO_ROOT / ".mcp.json"


# ---------------------------------------------------------------------------
# Static .mcp.json schema check
# ---------------------------------------------------------------------------


class TestMcpJsonSchema:
    """Claude Code parses ``.mcp.json`` on startup; a malformed file
    silently strips the server. Validate the exact shape it expects."""

    def test_file_parses_as_json(self) -> None:
        assert MCP_JSON.exists(), f"missing {MCP_JSON}"
        data = json.loads(MCP_JSON.read_text())
        assert isinstance(data, dict)

    def test_has_mcpservers_root(self) -> None:
        data = json.loads(MCP_JSON.read_text())
        assert "mcpServers" in data
        assert isinstance(data["mcpServers"], dict)

    def test_hope_rag_entry_is_stdio_shape(self) -> None:
        data = json.loads(MCP_JSON.read_text())
        entry = data["mcpServers"].get("hope-rag")
        assert entry is not None, "hope-rag server missing"
        # Required fields per Claude Code stdio spec.
        assert "command" in entry and isinstance(entry["command"], str)
        assert "args" in entry and isinstance(entry["args"], list)
        # Must invoke our module.
        assert entry["args"][:2] == ["-m", "hope.mcp.rag_server"]
        # env is optional but must be dict[str,str] when present.
        if "env" in entry:
            assert isinstance(entry["env"], dict)
            for k, v in entry["env"].items():
                assert isinstance(k, str) and isinstance(v, str)


# ---------------------------------------------------------------------------
# Subprocess harness
# ---------------------------------------------------------------------------


def _spawn_server(env: Dict[str, str]) -> subprocess.Popen:
    """Launch ``python -m hope.mcp.rag_server`` as a child process."""
    return subprocess.Popen(
        [sys.executable, "-m", "hope.mcp.rag_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        bufsize=1,  # line-buffered
    )


def _exchange(
    requests: Iterable[Dict[str, Any]],
    env: Dict[str, str],
    *,
    wait: float = 5.0,
) -> List[Dict[str, Any]]:
    """Send *requests* to a fresh server, collect every reply JSON line.

    The server exits when stdin closes; we close stdin after writing all
    requests and ``communicate()`` drains stdout so no reply is lost to
    pipe-close race.
    """
    proc = _spawn_server(env)
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    try:
        out, err = proc.communicate(payload, timeout=wait)
    except subprocess.TimeoutExpired:  # pragma: no cover - diagnostic path
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"rag_server subprocess hung; stderr=\n{err}")
    replies: List[Dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            replies.append(json.loads(line))
        except json.JSONDecodeError:  # pragma: no cover - should never fire
            pytest.fail(f"non-JSON line on stdout: {line!r}\nstderr={err}")
    return replies


@pytest.fixture()
def isolated_hope_home(tmp_path, monkeypatch):
    """Point the subprocess at a throwaway ``~/.hope`` so tests don't
    touch the user's real RAG database."""
    hope_dir = tmp_path / "hope"
    hope_dir.mkdir()
    env = dict(os.environ)
    env["HOPE_HOME"] = str(hope_dir)
    env["HOME"] = str(tmp_path)
    env["PYTHONUNBUFFERED"] = "1"
    # Keep dense RAG off so tests don't have to load BGE (~130 MB).
    env["HOPE_MEMORY_DEFAULT_BACKEND"] = "sqlite"
    return env


# ---------------------------------------------------------------------------
# MCP handshake + tools/list
# ---------------------------------------------------------------------------


EXPECTED_TOOLS = {
    "memory_store",
    "memory_retrieve",
    "memory_search",
    "memory_index",
    "digest_collect",
}


class TestSubprocessHandshake:
    """Mirror exactly what Claude Code does when it boots the server."""

    def test_initialize_returns_server_info(self, isolated_hope_home) -> None:
        replies = _exchange(
            [{"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1}],
            env=isolated_hope_home,
        )
        assert len(replies) == 1
        r = replies[0]
        assert r["id"] == 1
        assert r["result"]["serverInfo"]["name"] == "hope"
        assert "protocolVersion" in r["result"]

    def test_tools_list_has_all_five(self, isolated_hope_home) -> None:
        replies = _exchange(
            [
                {"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1},
                {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2},
            ],
            env=isolated_hope_home,
        )
        assert len(replies) == 2
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        missing = EXPECTED_TOOLS - names
        extra = names - EXPECTED_TOOLS
        assert not missing, f"missing tools: {missing}"
        # Extras are OK (future tools) but we want to notice drift.
        assert not extra, f"unexpected extra tools: {extra}"


# ---------------------------------------------------------------------------
# Live round-trip through RAGMemory
# ---------------------------------------------------------------------------


class TestSubprocessRoundTrip:
    """memory_store + memory_search over a fresh SQLite backend."""

    def test_store_then_search_finds_entry(self, isolated_hope_home) -> None:
        store_req = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 10,
            "params": {
                "name": "memory_store",
                "arguments": {
                    "content": "hope knows this",
                    "source": "e2e-test",
                },
            },
        }
        search_req = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 11,
            "params": {
                "name": "memory_search",
                "arguments": {"query": "hope knows this"},
            },
        }
        replies = _exchange(
            [store_req, search_req], env=isolated_hope_home, wait=10.0
        )
        assert len(replies) == 2

        store_resp = replies[0]
        assert store_resp["id"] == 10
        assert store_resp["result"]["isError"] is False
        assert "stored" in store_resp["result"]["content"][0]["text"].lower()

        search_resp = replies[1]
        assert search_resp["id"] == 11
        assert search_resp["result"]["isError"] is False
        text = search_resp["result"]["content"][0]["text"].lower()
        assert "hope knows this" in text, f"search returned: {text!r}"


# ---------------------------------------------------------------------------
# Direct digest_collect import check (bug-watch on stub collisions)
# ---------------------------------------------------------------------------


class TestDigestCollectImport:
    """``hope.tools.digest_collect`` has to import + instantiate without
    touching the removed ``sdk.py`` / empty ``model_catalog.py`` /
    ``engine/_stubs.py`` bits — otherwise ``build_server()`` would fail
    silently and strip the tool from ``tools/list``."""

    def test_module_imports(self) -> None:
        import hope.tools.digest_collect as mod  # noqa: F401

    def test_tool_instantiates_with_correct_name(self) -> None:
        from hope.tools.digest_collect import DigestCollectTool

        tool = DigestCollectTool()
        assert tool.spec.name == "digest_collect"

    def test_tool_appears_in_subprocess_tools_list(
        self, isolated_hope_home
    ) -> None:
        # Same as the tools/list check above but specifically asserts the
        # digest tool is present — cleanup regressions would strip this.
        replies = _exchange(
            [{"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1}],
            env=isolated_hope_home,
        )
        names = {t["name"] for t in replies[0]["result"]["tools"]}
        assert "digest_collect" in names


# ---------------------------------------------------------------------------
# Cross-agent seam bug watch
# ---------------------------------------------------------------------------


class TestRagServerDoesNotDependOnRemovedStubs:
    """``rag_server.py`` should use ``RAGMemory`` directly — not go
    through the removed ``hope.sdk`` façade or the empty intelligence
    ``model_catalog``. Reading the source is enough; a stray import
    would show up here as a regression trigger for the cleanup agent."""

    def test_rag_server_source_has_no_sdk_import(self) -> None:
        src = Path(REPO_ROOT / "src/hope/mcp/rag_server.py").read_text()
        assert "from hope.sdk" not in src
        assert "import hope.sdk" not in src

    def test_rag_server_source_has_no_model_catalog_import(self) -> None:
        src = Path(REPO_ROOT / "src/hope/mcp/rag_server.py").read_text()
        assert "model_catalog" not in src
