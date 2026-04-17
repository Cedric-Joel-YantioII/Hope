"""Tests for the async embedder queue.

These tests use a tiny fake embedder so they don't depend on
sentence-transformers or torch.  The fake is deterministic: it returns
a vector of length ``dim`` whose first slot is the string's length.
That's enough to prove the queue actually called ``embed()`` on the
right content.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import pytest

from hope.tools.storage.async_embedder import (
    AsyncEmbedderQueue,
    reset_async_embedder,
)
from hope.tools.storage.embeddings import Embedder


class _FakeEmbedder(Embedder):
    """Deterministic embedder: vector[0] = len(text)."""

    def __init__(self, dim: int = 4, latency_s: float = 0.0) -> None:
        self._dim = dim
        self._latency_s = latency_s
        self.call_count = 0
        self.batch_sizes: List[int] = []
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> Any:
        import numpy as np

        with self._lock:
            self.call_count += 1
            self.batch_sizes.append(len(texts))
        if self._latency_s:
            time.sleep(self._latency_s)
        arr = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, t in enumerate(texts):
            arr[i, 0] = float(len(t))
        return arr

    def dim(self) -> int:
        return self._dim


@pytest.fixture(autouse=True)
def _reset_global_queue():
    yield
    reset_async_embedder()


def _make_sink():
    vectors: Dict[str, Any] = {}
    lock = threading.Lock()

    def update_vec(doc_id: str, vec: Any) -> None:
        with lock:
            vectors[doc_id] = vec

    return vectors, update_vec


class TestAsyncEmbedderQueue:
    def test_enqueue_and_flush_populates_vectors(self):
        embedder = _FakeEmbedder(dim=4)
        q = AsyncEmbedderQueue(embedder, batch_size=8, max_wait_ms=50)
        q.start()

        sink, update_fn = _make_sink()
        contents = [f"doc-{i:02d}-text" for i in range(20)]
        for i, text in enumerate(contents):
            q.enqueue(f"id-{i}", text, update_fn)

        assert q.flush(timeout=10.0) is True
        # All 20 should now have a vector.
        assert len(sink) == 20
        # Vector[0] should equal len(content) — proves the right string
        # ended up at the right doc id.
        for i, text in enumerate(contents):
            assert sink[f"id-{i}"][0] == pytest.approx(len(text))

        q.shutdown()

    def test_batching_respects_batch_size(self):
        """Enqueuing many items at once should batch up to batch_size."""
        embedder = _FakeEmbedder(dim=4)
        q = AsyncEmbedderQueue(embedder, batch_size=4, max_wait_ms=50)
        q.start()
        sink, update_fn = _make_sink()

        for i in range(16):
            q.enqueue(f"id-{i}", f"text-{i}", update_fn)
        assert q.flush(timeout=10.0) is True

        # Every batch should be <= 4 items, and sizes should sum to 16.
        assert sum(embedder.batch_sizes) == 16
        assert max(embedder.batch_sizes) <= 4
        q.shutdown()

    def test_flush_on_empty_queue_returns_true(self):
        embedder = _FakeEmbedder()
        q = AsyncEmbedderQueue(embedder)
        assert q.flush(timeout=1.0) is True

    def test_pending_count_tracks_outstanding_work(self):
        # latency_s makes the worker slow so we can see non-zero pending.
        embedder = _FakeEmbedder(dim=4, latency_s=0.05)
        q = AsyncEmbedderQueue(embedder, batch_size=2, max_wait_ms=10)
        q.start()
        sink, update_fn = _make_sink()

        for i in range(6):
            q.enqueue(f"id-{i}", f"t{i}", update_fn)
        # Right after enqueue at least some should still be pending.
        assert q.pending() > 0
        assert q.flush(timeout=10.0) is True
        assert q.pending() == 0
        q.shutdown()

    def test_shutdown_is_idempotent(self):
        embedder = _FakeEmbedder()
        q = AsyncEmbedderQueue(embedder)
        q.start()
        q.shutdown()
        q.shutdown()  # second call should not raise
