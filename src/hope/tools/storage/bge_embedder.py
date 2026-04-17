"""BGE-Small-EN-v1.5 embedder — small, high-quality English embeddings.

``BAAI/bge-small-en-v1.5`` is a 33M-parameter model producing 384-dim
L2-normalizable vectors.  On MTEB it beats ``all-MiniLM-L6-v2`` by a few
points on English retrieval at roughly the same latency and a small
(~130 MB on disk) footprint — a good default for an on-device 8 GB M2
like Hope's target.

The implementation is intentionally a thin subclass of
:class:`SentenceTransformerEmbedder` so the existing caching, device
selection, and numpy handling logic inside ``sentence-transformers`` is
reused.  We just pin the model id and the instruction prefix that BGE
recommends for queries (the ``query_instruction`` is optional — most
callers embed documents, not queries, so we default to an empty prefix
and expose :meth:`embed_query` for callers that want the instruction).
"""

from __future__ import annotations

from typing import Any, List

from hope.tools.storage.embeddings import (
    Embedder,
    SentenceTransformerEmbedder,
)


class BGESmallEnV15Embedder(SentenceTransformerEmbedder):
    """Embedder backed by ``BAAI/bge-small-en-v1.5``.

    Parameters
    ----------
    normalize:
        If True (default) L2-normalize output so callers can treat the
        dot-product as cosine similarity.
    query_instruction:
        Prefix prepended inside :meth:`embed_query` only.  Defaults to
        the BGE-recommended string; pass ``""`` to disable.
    """

    MODEL_ID = "BAAI/bge-small-en-v1.5"
    EMBED_DIM = 384

    def __init__(
        self,
        *,
        normalize: bool = True,
        query_instruction: str = (
            "Represent this sentence for searching relevant passages: "
        ),
    ) -> None:
        # Delegate model loading/caching to the parent class.
        super().__init__(model_name=self.MODEL_ID)
        self._normalize = normalize
        self._query_instruction = query_instruction
        # Sanity: BGE-small-en-v1.5 is 384-dim; guard against a HF cache
        # accident swapping in a variant.
        if self._dim != self.EMBED_DIM:
            raise RuntimeError(
                f"Expected {self.MODEL_ID} to produce {self.EMBED_DIM}-dim "
                f"vectors, got {self._dim}. Check the HuggingFace cache."
            )

    # -- Embedder ABC ------------------------------------------------------

    def embed(self, texts: list[str]) -> Any:
        """Return a numpy array of shape ``(len(texts), 384)``."""
        import numpy as np

        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        arr = self._model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=self._normalize,
        )
        # sentence-transformers returns float32 already but be defensive.
        return arr.astype(np.float32, copy=False)

    def embed_query(self, queries: List[str]) -> Any:
        """Embed *queries* with the BGE instruction prefix.

        Applying the prefix only on the query side is recommended by the
        BGE authors and gives a small retrieval-quality boost; documents
        should still be embedded via :meth:`embed`.
        """
        if self._query_instruction:
            queries = [self._query_instruction + q for q in queries]
        return self.embed(queries)

    def dim(self) -> int:  # pragma: no cover - trivial
        return self._dim


__all__ = ["BGESmallEnV15Embedder", "Embedder"]
