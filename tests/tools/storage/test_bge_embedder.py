"""Tests for the BGE-small-en-v1.5 embedder.

We gate the real model load behind an ``st_available`` check — CI or
hermetic environments without ``sentence-transformers``/``torch``
installed skip cleanly.  The dim sanity check is the most important
assertion: a regression in the wrapper (or a stale HuggingFace cache)
would surface as a wrong-dim output.
"""

from __future__ import annotations

import importlib.util

import pytest


def _st_available() -> bool:
    return (
        importlib.util.find_spec("sentence_transformers") is not None
        and importlib.util.find_spec("torch") is not None
    )


st_required = pytest.mark.skipif(
    not _st_available(),
    reason="sentence-transformers and torch not installed",
)


@st_required
class TestBGESmallEnV15Embedder:
    def test_embed_returns_384_dim_vector(self):
        from hope.tools.storage.bge_embedder import BGESmallEnV15Embedder

        emb = BGESmallEnV15Embedder()
        assert emb.dim() == 384

        vecs = emb.embed(["the quick brown fox jumps over the lazy dog"])
        assert vecs.shape == (1, 384)

    def test_embed_batch(self):
        from hope.tools.storage.bge_embedder import BGESmallEnV15Embedder

        emb = BGESmallEnV15Embedder()
        texts = [
            "machine learning is fun",
            "cats are small carnivorous mammals",
            "python is a programming language",
        ]
        vecs = emb.embed(texts)
        assert vecs.shape == (3, 384)

    def test_embed_empty_input(self):
        from hope.tools.storage.bge_embedder import BGESmallEnV15Embedder

        emb = BGESmallEnV15Embedder()
        vecs = emb.embed([])
        assert vecs.shape == (0, 384)

    def test_embed_query_applies_instruction(self):
        from hope.tools.storage.bge_embedder import BGESmallEnV15Embedder

        emb = BGESmallEnV15Embedder(query_instruction="QUERY: ")
        raw = emb.embed(["hello world"])
        query = emb.embed_query(["hello world"])
        # Adding a prefix should produce a different vector.
        assert raw.shape == query.shape == (1, 384)
        # Dot product < 1.0 (not identical) on normalized unit vectors.
        import numpy as np

        sim = float(np.dot(raw[0], query[0]))
        assert sim < 0.9999
