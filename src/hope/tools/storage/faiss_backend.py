"""FAISS dense retrieval memory backend.

Uses cosine similarity via inner-product search on L2-normalised
vectors.  Requires ``faiss-cpu`` (or ``faiss-gpu``) and ``numpy``.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import faiss
except ImportError as _faiss_exc:
    raise ImportError(
        "faiss is required for FAISSMemory. Install it with: "
        "pip install faiss-cpu  (or faiss-gpu)"
    ) from _faiss_exc

from hope.core.events import EventType, get_event_bus
from hope.core.registry import MemoryRegistry
from hope.tools.storage._stubs import MemoryBackend, RetrievalResult
from hope.tools.storage.embeddings import (
    Embedder,
    SentenceTransformerEmbedder,
)


@MemoryRegistry.register("faiss")
class FAISSMemory(MemoryBackend):
    """Dense retrieval backend powered by FAISS.

    Stores document embeddings in a ``faiss.IndexFlatIP`` index
    (inner-product, which equals cosine similarity when vectors
    are L2-normalised before insertion/search).
    """

    backend_id: str = "faiss"

    def __init__(
        self,
        *,
        embedder: Embedder | None = None,
        embed_mode: str = "sync",
    ) -> None:
        if embedder is None:
            embedder = SentenceTransformerEmbedder()
        self._embedder = embedder
        self._index = faiss.IndexFlatIP(self._embedder.dim())
        self._documents: Dict[str, Tuple[str, str, Dict[str, Any]]] = {}
        self._id_map: List[str] = []
        self._deleted: Set[str] = set()
        if embed_mode not in ("sync", "async"):
            raise ValueError(
                f"embed_mode must be 'sync' or 'async', got {embed_mode!r}",
            )
        self._embed_mode = embed_mode
        self._placeholder_ids: Set[str] = set()
        # FAISS IndexFlatIP has no in-place row update, so we keep a
        # parallel ``np.ndarray`` of vectors and swap in a fresh index
        # whenever placeholders are replaced.  Only used in async mode.
        self._vectors = None

    # ------------------------------------------------------------------
    # MemoryBackend interface
    # ------------------------------------------------------------------

    def store(
        self,
        content: str,
        *,
        source: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Embed and store *content*, returning a unique doc id."""
        import numpy as np

        doc_id = uuid.uuid4().hex
        meta = metadata if metadata is not None else {}

        if self._embed_mode == "async":
            # Placeholder zero vector; replaced by update_vector().
            vec = np.zeros((1, self._embedder.dim()), dtype=np.float32)
            self._placeholder_ids.add(doc_id)
        else:
            vec = self._embedder.embed([content])
            faiss.normalize_L2(vec)

        self._index.add(vec)

        # Track parallel numpy so we can rebuild the index on vector
        # updates without re-embedding existing rows.
        if self._vectors is None:
            self._vectors = vec.copy()
        else:
            self._vectors = np.concatenate([self._vectors, vec], axis=0)

        self._documents[doc_id] = (content, source, meta)
        self._id_map.append(doc_id)

        if self._embed_mode == "async":
            from hope.tools.storage.async_embedder import get_async_embedder

            aq = get_async_embedder(self._embedder)
            aq.enqueue(doc_id, content, self.update_vector)

        bus = get_event_bus()
        bus.publish(
            EventType.MEMORY_STORE,
            {
                "backend": self.backend_id,
                "doc_id": doc_id,
                "source": source,
            },
        )
        return doc_id

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        **kwargs: Any,
    ) -> List[RetrievalResult]:
        """Embed *query* and return the top-k most similar docs."""
        if not query.strip() or self._index.ntotal == 0:
            bus = get_event_bus()
            bus.publish(
                EventType.MEMORY_RETRIEVE,
                {
                    "backend": self.backend_id,
                    "query": query,
                    "num_results": 0,
                },
            )
            return []

        vec = self._embedder.embed([query])
        faiss.normalize_L2(vec)

        # Request more results to compensate for deleted docs
        k = min(
            top_k + len(self._deleted),
            self._index.ntotal,
        )
        scores, indices = self._index.search(vec, k)

        results: List[RetrievalResult] = []
        for score, idx in zip(scores[0].tolist(), indices[0].tolist()):
            if idx < 0:
                continue
            doc_id = self._id_map[idx]
            if doc_id in self._deleted:
                continue
            content, source, meta = self._documents[doc_id]
            results.append(
                RetrievalResult(
                    content=content,
                    score=float(score),
                    source=source,
                    metadata=dict(meta),
                )
            )
            if len(results) >= top_k:
                break

        bus = get_event_bus()
        bus.publish(
            EventType.MEMORY_RETRIEVE,
            {
                "backend": self.backend_id,
                "query": query,
                "num_results": len(results),
            },
        )
        return results

    # -- async embedder callback -----------------------------------------

    def update_vector(self, doc_id: str, vector: Any) -> None:
        """Replace the stored vector for *doc_id* and rebuild the index.

        Index-Flat has no mutable row, so we patch ``self._vectors`` and
        reconstruct the index from scratch.  Rebuild cost is O(n*dim),
        but it only fires on async embedding completion, not on the hot
        path.
        """
        import numpy as np

        if doc_id not in self._documents or self._vectors is None:
            return
        try:
            idx = self._id_map.index(doc_id)
        except ValueError:
            return
        vec = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        faiss.normalize_L2(vec)
        self._vectors[idx] = vec[0]
        # Rebuild the flat index (cheap: O(n) copy).
        new_index = faiss.IndexFlatIP(self._embedder.dim())
        new_index.add(self._vectors)
        self._index = new_index
        self._placeholder_ids.discard(doc_id)

    def placeholder_ids(self) -> List[str]:
        """Return the doc ids still awaiting a real embedding."""
        return list(self._placeholder_ids)

    def contents_for(self, doc_id: str) -> Optional[str]:
        """Return the stored content for *doc_id*, if any."""
        doc = self._documents.get(doc_id)
        return doc[0] if doc is not None else None

    def rebuild_index(self) -> None:
        """Reconstruct the FAISS index from the current vector matrix.

        Used by the nightly consolidation job.  Idempotent.
        """
        if self._vectors is None:
            return
        new_index = faiss.IndexFlatIP(self._embedder.dim())
        new_index.add(self._vectors)
        self._index = new_index

    def delete(self, doc_id: str) -> bool:
        """Soft-delete *doc_id*.  Return True if it existed."""
        if doc_id not in self._documents or doc_id in self._deleted:
            return False
        self._deleted.add(doc_id)
        self._placeholder_ids.discard(doc_id)
        return True

    def clear(self) -> None:
        """Reset the index and all internal storage."""
        self._index.reset()
        self._documents.clear()
        self._id_map.clear()
        self._deleted.clear()
        self._placeholder_ids.clear()
        self._vectors = None


__all__ = ["FAISSMemory"]
