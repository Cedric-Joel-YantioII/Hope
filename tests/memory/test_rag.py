"""Tests for the RAG memory singleton.

We avoid loading BGE / FAISS / the Rust SQLite bridge in CI — those
pull in heavy deps that aren't available in every env. Instead the
tests inject fake sub-backends via monkeypatch and exercise the
assembly logic, idempotency, and the fused round-trip contract.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from hope.memory import get_rag, reset_rag
from hope.tools.storage._stubs import MemoryBackend, RetrievalResult


class _FakeBackend(MemoryBackend):
    """Tiny in-memory backend — no SQLite, no FAISS, deterministic."""

    backend_id = "fake"

    def __init__(self) -> None:
        self._rows: Dict[str, tuple] = {}
        self._next = 0

    def store(
        self,
        content: str,
        *,
        source: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        self._next += 1
        doc_id = f"doc_{self._next}"
        self._rows[doc_id] = (content, source, metadata or {})
        return doc_id

    def retrieve(
        self, query: str, *, top_k: int = 5, **kwargs: Any,
    ) -> List[RetrievalResult]:
        out = []
        for content, source, meta in self._rows.values():
            if query.lower() in content.lower():
                out.append(
                    RetrievalResult(
                        content=content, score=1.0, source=source, metadata=meta,
                    )
                )
        return out[:top_k]

    def delete(self, doc_id: str) -> bool:
        return self._rows.pop(doc_id, None) is not None

    def clear(self) -> None:
        self._rows.clear()

    def count(self) -> int:  # mirror SQLiteMemory for RAGMemory.count()
        return len(self._rows)


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    """Ensure each test starts with a cold RAG and fake sub-backends."""
    reset_rag()

    # Replace the two builders with fakes so neither Rust nor BGE is touched.
    monkeypatch.setattr(
        "hope.memory.rag._build_sparse", lambda _db_path: _FakeBackend(),
    )

    yield
    reset_rag()


def _make_cfg(cfg_mod: Any, db_path, backend: str):
    cfg = cfg_mod.HopeConfig()
    cfg.tools.storage.default_backend = backend
    cfg.tools.storage.db_path = str(db_path)
    cfg.tools.storage.embed_mode = "sync"
    return cfg


class TestRAGAssembly:
    def test_sqlite_only_when_override_is_sqlite(self, tmp_path, monkeypatch):
        """default_backend=sqlite should skip the dense path entirely."""
        from hope.core import config as _cfg_mod

        monkeypatch.setattr(
            "hope.memory.rag.load_config",
            lambda: _make_cfg(_cfg_mod, tmp_path / "mem.db", "sqlite"),
        )

        rag = get_rag()
        assert rag.dense is None
        assert rag.backend is rag.sparse

    def test_get_rag_is_singleton(self, tmp_path, monkeypatch):
        from hope.core import config as _cfg_mod

        monkeypatch.setattr(
            "hope.memory.rag.load_config",
            lambda: _make_cfg(_cfg_mod, tmp_path / "mem.db", "sqlite"),
        )
        first = get_rag()
        second = get_rag()
        assert first is second

    def test_degrades_to_sparse_when_dense_unavailable(
        self, tmp_path, monkeypatch,
    ):
        """If dense build returns None, backend should fall back to sparse."""
        from hope.core import config as _cfg_mod

        monkeypatch.setattr(
            "hope.memory.rag.load_config",
            lambda: _make_cfg(_cfg_mod, tmp_path / "mem.db", "hybrid"),
        )
        monkeypatch.setattr(
            "hope.memory.rag._build_dense", lambda embed_mode: None,
        )
        rag = get_rag()
        assert rag.dense is None
        assert rag.backend is rag.sparse


class TestRAGRoundTrip:
    def test_store_then_search_sqlite_only(self, tmp_path, monkeypatch):
        """End-to-end: store a note, search for a keyword, get it back."""
        from hope.core import config as _cfg_mod

        monkeypatch.setattr(
            "hope.memory.rag.load_config",
            lambda: _make_cfg(_cfg_mod, tmp_path / "mem.db", "sqlite"),
        )
        rag = get_rag()
        rag.backend.store(
            "Joel prefers brevity in Hope's replies.",
            source="user_pref",
        )
        rag.backend.store(
            "PRISM is tested in Nigeria but country-agnostic.",
            source="project_prism",
        )

        hits = rag.backend.retrieve("brevity", top_k=3)
        assert any("brevity" in r.content.lower() for r in hits)

        hits2 = rag.backend.retrieve("PRISM", top_k=3)
        assert any("PRISM" in r.content for r in hits2)

    def test_hybrid_fuses_results(self, tmp_path, monkeypatch):
        """Fused backend stores into both sides and returns fused hits."""
        from hope.core import config as _cfg_mod

        monkeypatch.setattr(
            "hope.memory.rag.load_config",
            lambda: _make_cfg(_cfg_mod, tmp_path / "mem.db", "hybrid"),
        )
        monkeypatch.setattr(
            "hope.memory.rag._build_dense", lambda embed_mode: _FakeBackend(),
        )

        rag = get_rag()
        assert rag.dense is not None
        rag.backend.store("hybrid wins when keyword and semantic agree")
        hits = rag.backend.retrieve("hybrid", top_k=3)
        assert hits, "fused backend returned no results"
