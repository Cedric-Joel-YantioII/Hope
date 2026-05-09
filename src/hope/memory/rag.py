"""RAGMemory — process-wide hybrid (SQLite + FAISS/BGE) memory singleton.

Hope's long-term memory is assembled here. We intentionally build a small
stack with no knobs users have to think about:

* **Keyword** side: :class:`SQLiteMemory` under ``~/.hope/rag/memory.db``
  (FTS5-backed, handled by the Rust bridge).
* **Dense** side: :class:`FAISSMemory` wrapping a :class:`BGESmallEnV15Embedder`
  (384-dim, ~130 MB on disk) behind an :class:`AsyncEmbedderQueue`, so
  ``store()`` returns immediately and embedding happens off the hot path.
* Fused with :class:`HybridMemory` (Reciprocal Rank Fusion).

The singleton is created lazily on first :func:`get_rag` call. The daemon
calls ``get_rag()`` from ``HopeDaemon.start()`` so the brain is already
online by the time a user asks it anything — no first-query stall.

Config keys (``~/.hope/config.toml`` ``[memory]`` section, all optional):

    [memory]
    default_backend = "hybrid"          # or "sqlite" (dense off)
    db_path = "~/.hope/rag/memory.db"   # sqlite location
    embedder = "bge-small-en-v1.5"      # only BGE supported in RAG path
    embed_mode = "async"                # "sync" | "async"

Tests should call :func:`reset_rag` in teardown — the singleton is
process-wide and would otherwise leak between tests.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hope.core.config import DEFAULT_CONFIG_DIR, load_config
from hope.tools.storage._stubs import MemoryBackend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Singleton state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RAGMemory:
    """A bundle of the backends wired together plus a handle to the fused one.

    Most callers only need ``.backend`` — it satisfies :class:`MemoryBackend`
    and delegates to the fused Hybrid instance. ``.sparse`` and ``.dense``
    are exposed so the nightly consolidation job can reach them by name.
    """

    backend: MemoryBackend
    sparse: MemoryBackend
    dense: Optional[MemoryBackend]

    def count(self) -> int:
        """Return the best-effort entry count for logging / health checks.

        We count SQLite rows because it's the keyword side and it's the one
        backend that has a real persistent row count; the FAISS index
        mirrors it. If the count call fails for any reason we return 0 —
        the caller uses this only for a boot-log line.
        """
        counter = getattr(self.sparse, "count", None)
        if callable(counter):
            try:
                return int(counter())
            except Exception:  # pragma: no cover - defensive
                return 0
        return 0


_SINGLETON: Optional[RAGMemory] = None
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def _resolve_db_path(cfg_db_path: str) -> Path:
    """Return the SQLite path, defaulting to ``~/.hope/rag/memory.db``."""
    if cfg_db_path and cfg_db_path != str(DEFAULT_CONFIG_DIR / "memory.db"):
        # User set an explicit override — honour it.
        return Path(cfg_db_path).expanduser()
    return (DEFAULT_CONFIG_DIR / "rag" / "memory.db").expanduser()


def _build_sparse(db_path: Path) -> MemoryBackend:
    """Build the keyword (SQLite/FTS5) side."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from hope.tools.storage.sqlite import SQLiteMemory

    return SQLiteMemory(db_path=str(db_path))


def _build_dense(embed_mode: str) -> Optional[MemoryBackend]:
    """Build the dense (FAISS + BGE) side, or ``None`` if FAISS is absent.

    Returning ``None`` on missing FAISS lets Hope boot on a fresh machine
    without the extra ~80 MB wheel — the daemon degrades to sparse-only.
    """
    try:
        from hope.tools.storage.bge_embedder import BGESmallEnV15Embedder
        from hope.tools.storage.faiss_backend import FAISSMemory
    except ImportError as exc:
        logger.warning(
            "dense RAG disabled — FAISS/BGE unavailable: %s", exc,
        )
        return None

    try:
        embedder = BGESmallEnV15Embedder()
    except Exception:
        logger.exception("BGE embedder failed to load; dense RAG disabled")
        return None

    # AsyncEmbedder wrapping happens inside FAISSMemory when embed_mode="async".
    # This keeps store() off the embedding path even for large ingestion runs.
    dense = FAISSMemory(embedder=embedder, embed_mode=embed_mode)

    # Prime the process-global async queue so the worker thread is up
    # before the first store() lands on it.
    if embed_mode == "async":
        from hope.tools.storage.async_embedder import get_async_embedder

        get_async_embedder(embedder)

    return dense


def _build_hybrid(
    sparse: MemoryBackend, dense: MemoryBackend,
) -> MemoryBackend:
    """Fuse sparse + dense via RRF."""
    from hope.tools.storage.hybrid import HybridMemory

    return HybridMemory(sparse=sparse, dense=dense)


def _assemble(cfg: Any | None = None) -> RAGMemory:
    """Load config and construct the RAG bundle. No singleton effects."""
    if cfg is None:
        cfg = load_config()

    storage = cfg.tools.storage
    backend_override = (storage.default_backend or "hybrid").lower()
    embed_mode = storage.embed_mode or "async"

    db_path = _resolve_db_path(storage.db_path)
    sparse = _build_sparse(db_path)

    # If the user explicitly asked for the keyword-only backend, skip
    # loading BGE/FAISS entirely — it saves ~150 MB of RAM.
    if backend_override == "sqlite":
        return RAGMemory(backend=sparse, sparse=sparse, dense=None)

    dense = _build_dense(embed_mode)
    if dense is None:
        # Degrade: sparse satisfies MemoryBackend so everything still works.
        return RAGMemory(backend=sparse, sparse=sparse, dense=None)

    hybrid = _build_hybrid(sparse, dense)
    return RAGMemory(backend=hybrid, sparse=sparse, dense=dense)


def get_rag(cfg: Any | None = None) -> RAGMemory:
    """Return the process-wide RAGMemory, building it on first call.

    ``cfg`` is accepted for tests and for callers that already loaded a
    HopeConfig; production code can pass nothing.
    """
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = _assemble(cfg)
            logger.info(
                "RAG backbone assembled (backend=%s, dense=%s, entries=%d)",
                type(_SINGLETON.backend).__name__,
                type(_SINGLETON.dense).__name__ if _SINGLETON.dense else "none",
                _SINGLETON.count(),
            )
        return _SINGLETON


def reset_rag() -> None:
    """Tear down the singleton (tests only).

    Also tears down the process-global async embedder queue so a fresh
    build on the next ``get_rag()`` starts from a clean slate.
    """
    global _SINGLETON
    with _LOCK:
        _SINGLETON = None
    try:
        from hope.tools.storage.async_embedder import reset_async_embedder

        reset_async_embedder()
    except Exception:  # pragma: no cover - best-effort
        pass


__all__ = ["RAGMemory", "get_rag", "reset_rag"]
