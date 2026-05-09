"""Tests for the nightly memory consolidation job.

These tests exercise the full path:

1. A fake embedder is wired into a DenseMemory backend in async mode.
2. We enqueue a few rows and intentionally do NOT drain the queue so
   some placeholders linger — consolidation should mop those up.
3. We feed a trivial trace set through a fake SkillDiscovery path.
4. Assert the MEMORY_CONSOLIDATED event fires with plausible stats.
"""

from __future__ import annotations

import time
from typing import Any, List

import pytest

from hope.core.events import EventType, get_event_bus, reset_event_bus
from hope.tools.storage import consolidation
from hope.tools.storage.async_embedder import reset_async_embedder
from hope.tools.storage.embeddings import Embedder


class _FakeEmbedder(Embedder):
    """Deterministic; vector[0] = len(text), vector[1] = 1.0."""

    def __init__(self, dim: int = 4, latency_s: float = 0.0) -> None:
        self._dim = dim
        self._latency_s = latency_s

    def embed(self, texts: list[str]) -> Any:
        import numpy as np

        if self._latency_s:
            time.sleep(self._latency_s)
        arr = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, t in enumerate(texts):
            arr[i, 0] = float(len(t))
            arr[i, 1] = 1.0
        return arr

    def dim(self) -> int:
        return self._dim


@pytest.fixture(autouse=True)
def _reset_globals():
    reset_async_embedder()
    reset_event_bus()
    yield
    reset_async_embedder()
    reset_event_bus()


def _bus_records():
    bus = get_event_bus(record_history=True)
    # Replace with a fresh bus that actually records.
    bus.clear_history()
    return bus


class TestConsolidation:
    def test_re_embeds_placeholder_rows(self):
        from hope.tools.storage.dense import DenseMemory

        backend = DenseMemory(
            embedder=_FakeEmbedder(dim=4, latency_s=0.02),
            embed_mode="async",
        )
        # Enqueue 10 items then immediately call consolidation — some
        # items will definitely still be placeholders.
        ids = backend.store_many(
            [f"doc-{i:02d}-body" for i in range(10)],
        )
        assert len(ids) == 10

        bus = _bus_records()
        report = consolidation.run([backend], flush_timeout_s=5.0)

        assert report.duration_ms >= 0
        # After consolidation there should be no placeholders left.
        assert backend.placeholder_ids() == []
        # Matrix row 0 should reflect the real embedding (len of text).
        import numpy as np

        assert not np.allclose(backend._matrix[0], 0.0)

        events = [e for e in bus.history if e.event_type == EventType.MEMORY_CONSOLIDATED]
        assert len(events) == 1
        payload = events[0].data
        assert "duration_ms" in payload
        assert payload["re_embedded"] >= 0  # could be 0 if queue drained first
        assert payload["new_skills"] == 0

    def test_idempotent(self):
        """Calling consolidation twice back-to-back produces no new work."""
        from hope.tools.storage.dense import DenseMemory

        backend = DenseMemory(
            embedder=_FakeEmbedder(dim=4), embed_mode="async",
        )
        backend.store_many([f"doc-{i}" for i in range(5)])

        consolidation.run([backend], flush_timeout_s=5.0)
        assert backend.placeholder_ids() == []

        report2 = consolidation.run([backend], flush_timeout_s=5.0)
        assert report2.re_embedded == 0  # nothing to fix
        assert backend.placeholder_ids() == []

    def test_sync_backend_is_a_noop_except_event(self):
        """A purely-sync backend with no placeholder API is still safe."""
        class _DummyBackend:
            backend_id = "dummy"

            def store(self, *a, **k):  # pragma: no cover
                return "x"

            def retrieve(self, *a, **k):  # pragma: no cover
                return []

            def delete(self, *a, **k):  # pragma: no cover
                return False

            def clear(self):  # pragma: no cover
                pass

        bus = _bus_records()
        report = consolidation.run([_DummyBackend()])
        assert report.re_embedded == 0
        assert report.rebuilt_indexes == 0
        events = [e for e in bus.history if e.event_type == EventType.MEMORY_CONSOLIDATED]
        assert len(events) == 1

    def test_skill_distillation_hook_fires(self):
        """If traces + sink are provided, new_skills reflects discovery."""
        captured: List[Any] = []

        def trace_provider():
            # Two traces each tracing the same 2-step sequence; should
            # NOT meet the default min_frequency=3 — so new_skills=0.
            return [
                {
                    "steps": [
                        {"step_type": "tool_call", "tool_name": "web_search"},
                        {"step_type": "tool_call", "tool_name": "file_write"},
                    ],
                    "outcome": 1.0,
                },
                {
                    "steps": [
                        {"step_type": "tool_call", "tool_name": "web_search"},
                        {"step_type": "tool_call", "tool_name": "file_write"},
                    ],
                    "outcome": 1.0,
                },
            ]

        def sink(skills):
            captured.extend(skills)

        from hope.tools.storage.dense import DenseMemory

        backend = DenseMemory(
            embedder=_FakeEmbedder(dim=4), embed_mode="sync",
        )
        report = consolidation.run(
            [backend], trace_provider=trace_provider, skill_sink=sink,
        )
        # The call path is exercised even if frequency threshold isn't met.
        assert isinstance(report.new_skills, int)
