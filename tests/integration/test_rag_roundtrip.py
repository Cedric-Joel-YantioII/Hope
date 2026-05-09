"""RAG round-trip + persistence test.

Validates two contracts:
  1. ``store → flush → search`` returns the stored content.
  2. A second :class:`RAGMemory` instance pointing at the same SQLite path
     still sees the entry (persistence across restarts).

Uses the sqlite-only backend so neither FAISS nor BGE is required.
"""

from __future__ import annotations

import pytest

from hope.core import config as _cfg_mod
from hope.memory import get_rag, reset_rag


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure each test starts with a cold RAG singleton."""
    reset_rag()
    yield
    reset_rag()


def _cfg_sqlite(db_path):
    cfg = _cfg_mod.HopeConfig()
    cfg.tools.storage.default_backend = "sqlite"
    cfg.tools.storage.db_path = str(db_path)
    cfg.tools.storage.embed_mode = "sync"
    return cfg


def test_store_then_search_roundtrip(tmp_path, monkeypatch):
    """memory_store → memory_search returns the stored value."""
    db = tmp_path / "rag_memory.db"
    monkeypatch.setattr(
        "hope.memory.rag.load_config", lambda: _cfg_sqlite(db),
    )

    rag = get_rag()
    assert rag.dense is None  # sqlite-only path

    doc_id = rag.backend.store(
        "Joel prefers brevity in Hope's replies.",
        source="user_pref",
        metadata={"tag": "preference"},
    )
    assert doc_id

    # Sparse SQLite/FTS5 is synchronous — no queue to flush — but the
    # flush-analog (commit) is inside store(). A search should return the row.
    hits = rag.backend.retrieve("brevity", top_k=5)
    contents = [h.content for h in hits]
    assert any("brevity" in c.lower() for c in contents), (
        f"'brevity' not found in search results: {contents!r}"
    )


def test_persistence_across_restart(tmp_path, monkeypatch):
    """A fresh RAGMemory pointing at the same DB sees prior writes."""
    db = tmp_path / "rag_persist.db"
    monkeypatch.setattr(
        "hope.memory.rag.load_config", lambda: _cfg_sqlite(db),
    )

    # First "instance" — store a single entry.
    first = get_rag()
    first.backend.store(
        "PRISM is country-agnostic; Nigeria is the test market.",
        source="project_prism",
    )
    assert first.count() >= 1

    # Simulate a restart by resetting the singleton. The NEXT get_rag() call
    # assembles a brand-new RAGMemory — but the SQLite file on disk persists.
    reset_rag()

    second = get_rag()
    # Different python-level object.
    assert second is not first
    # Same underlying data.
    assert second.count() >= 1
    hits = second.backend.retrieve("PRISM", top_k=5)
    assert any("PRISM" in h.content for h in hits), (
        f"persistence failed — PRISM entry not visible to second instance; "
        f"hits={[h.content for h in hits]!r}"
    )
