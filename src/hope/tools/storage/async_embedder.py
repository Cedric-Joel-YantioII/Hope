"""Queue-based background embedding worker.

Embedding a batch of text on an M2 CPU with ``all-MiniLM-L6-v2`` takes
tens of milliseconds; with BGE-small-en-v1.5 it's longer.  That's fine
for an indexing job but unacceptable on the hot path of ``store()`` —
it makes the whole turn wait for a CPU tensor op before it can ACK.

This module keeps ``store()`` O(micro) by:

1. Enqueueing ``(doc_id, backend_ref, content)`` tuples.
2. Stamping the backend with a **zero placeholder vector** so the index
   shape stays consistent (queries against un-embedded rows just won't
   match anything until the worker catches up — cheaper than crashing).
3. A single background thread pulls up to ``batch_size`` items with a
   ``max_wait_ms`` deadline, calls ``embedder.embed(contents)`` once,
   and calls back into the backend to replace the placeholder rows.

The queue is process-global so MiniLM is only loaded once even if the
app spins up multiple ``DenseMemory`` instances.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from hope.core.events import EventType, get_event_bus
from hope.tools.storage.embeddings import Embedder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


# The update callback takes (doc_id, vector) — the backend is responsible
# for locating the row (by id) and patching it in place.  We keep it as a
# callable rather than a MemoryBackend reference so backends that don't
# store vectors directly (e.g. FAISS) can provide a thin adapter.
UpdateVectorFn = Callable[[str, Any], None]


@dataclass(slots=True)
class _EmbedJob:
    """One queued item."""

    doc_id: str
    content: str
    update_fn: UpdateVectorFn
    enqueued_at: float


# ---------------------------------------------------------------------------
# AsyncEmbedderQueue
# ---------------------------------------------------------------------------


class AsyncEmbedderQueue:
    """Single-threaded background embedder.

    Thread-safe.  Safe to instantiate multiple times but for efficiency
    prefer :func:`get_async_embedder` which keeps a per-process singleton
    so the model is only loaded once.

    Parameters
    ----------
    embedder:
        Any :class:`Embedder` implementation.  The queue calls
        ``embedder.embed(batch_of_strings)`` and expects a 2D numpy-ish
        array back.
    batch_size:
        Max items pulled off the queue per embed call.
    max_wait_ms:
        How long to wait for a batch to fill before embedding what we
        have.  Trades off latency vs. throughput.
    """

    def __init__(
        self,
        embedder: Embedder,
        *,
        batch_size: int = 16,
        max_wait_ms: int = 200,
    ) -> None:
        self._embedder = embedder
        self._batch_size = max(1, batch_size)
        self._max_wait_s = max(0.001, max_wait_ms / 1000.0)
        self._queue: "queue.Queue[Optional[_EmbedJob]]" = queue.Queue()
        self._shutdown = threading.Event()
        self._idle = threading.Event()
        self._idle.set()  # idle until we enqueue
        self._thread: Optional[threading.Thread] = None
        # For .flush() — we can't rely on queue.join() alone because a
        # batch may already be dequeued but mid-embed.
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._drained = threading.Condition(self._pending_lock)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Spawn the worker thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="hope-async-embedder",
            daemon=True,
        )
        self._thread.start()

    def shutdown(self, *, timeout: float = 10.0) -> None:
        """Stop the worker after draining the queue."""
        self.flush(timeout=timeout)
        self._shutdown.set()
        # Send sentinel to unblock the get() loop
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -- public API --------------------------------------------------------

    def enqueue(
        self,
        doc_id: str,
        content: str,
        update_fn: UpdateVectorFn,
    ) -> None:
        """Schedule *content* for background embedding.

        The backend should already have inserted a placeholder zero
        vector for *doc_id*; ``update_fn`` will be called with the real
        vector when the worker completes.
        """
        if self._thread is None:
            self.start()
        with self._pending_lock:
            self._pending += 1
            self._idle.clear()
        self._queue.put(
            _EmbedJob(
                doc_id=doc_id,
                content=content,
                update_fn=update_fn,
                enqueued_at=time.time(),
            )
        )

    def flush(self, *, timeout: float = 30.0) -> bool:
        """Block until the queue drains.  Returns True if drained in time."""
        deadline = time.time() + timeout
        with self._drained:
            while self._pending > 0:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._drained.wait(timeout=min(remaining, 1.0))
        return True

    def pending(self) -> int:
        """Number of jobs not yet embedded."""
        with self._pending_lock:
            return self._pending

    # -- worker -----------------------------------------------------------

    def _run(self) -> None:
        while not self._shutdown.is_set():
            batch = self._collect_batch()
            if not batch:
                continue
            self._process_batch(batch)

    def _collect_batch(self) -> List[_EmbedJob]:
        """Pull up to ``batch_size`` jobs, waiting at most ``max_wait_s``."""
        batch: List[_EmbedJob] = []
        # Blocking wait for the first item — we have no reason to spin.
        try:
            first = self._queue.get(timeout=1.0)
        except queue.Empty:
            return batch
        if first is None:  # shutdown sentinel
            return batch
        batch.append(first)

        deadline = time.time() + self._max_wait_s
        while len(batch) < self._batch_size:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                nxt = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            if nxt is None:  # shutdown sentinel — stop filling
                # Put it back so the outer loop also sees it.
                self._queue.put(None)
                break
            batch.append(nxt)
        return batch

    def _process_batch(self, batch: List[_EmbedJob]) -> None:
        contents = [j.content for j in batch]
        t0 = time.time()
        try:
            vectors = self._embedder.embed(contents)
        except Exception:  # pragma: no cover - defensive
            logger.exception("async embed batch failed; marking jobs done")
            self._mark_done(len(batch))
            return

        for job, vec in zip(batch, vectors):
            try:
                job.update_fn(job.doc_id, vec)
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "update_fn failed for doc_id=%s", job.doc_id,
                )

        # Emit a single aggregate event for observability.
        try:
            bus = get_event_bus()
            bus.publish(
                EventType.MEMORY_EMBEDDED,
                {
                    "count": len(batch),
                    "batch_embed_ms": int((time.time() - t0) * 1000),
                    "queue_age_ms": int(
                        (time.time() - batch[0].enqueued_at) * 1000,
                    ),
                },
            )
        except Exception:  # pragma: no cover - bus is best-effort
            pass

        self._mark_done(len(batch))

    def _mark_done(self, n: int) -> None:
        with self._drained:
            self._pending = max(0, self._pending - n)
            if self._pending == 0:
                self._idle.set()
                self._drained.notify_all()


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_GLOBAL_QUEUE: Optional[AsyncEmbedderQueue] = None
_GLOBAL_LOCK = threading.Lock()


def get_async_embedder(
    embedder: Optional[Embedder] = None,
    *,
    batch_size: int = 16,
    max_wait_ms: int = 200,
) -> AsyncEmbedderQueue:
    """Return the process-global async embedder queue, creating it lazily.

    Pass *embedder* on the first call to pick the model; subsequent
    calls ignore it so we don't spawn multiple worker threads.
    """
    global _GLOBAL_QUEUE
    with _GLOBAL_LOCK:
        if _GLOBAL_QUEUE is None:
            if embedder is None:
                raise RuntimeError(
                    "get_async_embedder() needs an embedder on its first call",
                )
            _GLOBAL_QUEUE = AsyncEmbedderQueue(
                embedder,
                batch_size=batch_size,
                max_wait_ms=max_wait_ms,
            )
            _GLOBAL_QUEUE.start()
        return _GLOBAL_QUEUE


def reset_async_embedder() -> None:
    """Tear down the process-global queue (for tests)."""
    global _GLOBAL_QUEUE
    with _GLOBAL_LOCK:
        if _GLOBAL_QUEUE is not None:
            try:
                _GLOBAL_QUEUE.shutdown(timeout=5.0)
            except Exception:  # pragma: no cover
                pass
            _GLOBAL_QUEUE = None


__all__ = [
    "AsyncEmbedderQueue",
    "UpdateVectorFn",
    "get_async_embedder",
    "reset_async_embedder",
]
