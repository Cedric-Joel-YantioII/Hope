"""Nightly memory consolidation job.

Responsibilities (idempotent):

* Finish any embeddings the async worker didn't drain.
* Rebuild dense indexes (FAISS flat, dense in-memory matrix) so the
  current generation of vectors is fully searchable.
* Mine recent traces via :class:`SkillDiscovery` and persist any newly
  discovered skills.
* Emit ``MEMORY_CONSOLIDATED`` with summary stats.

The entry point is :func:`run`.  It's safe to call multiple times a day;
empty inputs produce a zero-work run with no side effects beyond the
emitted event.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, List, Optional

from hope.core.events import EventType, get_event_bus
from hope.tools.storage._stubs import MemoryBackend

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConsolidationReport:
    """What a single consolidation pass did."""

    rebuilt_indexes: int = 0
    re_embedded: int = 0
    new_skills: int = 0
    duration_ms: int = 0
    backends: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Core primitives (pulled out so tests can exercise each piece)
# ---------------------------------------------------------------------------


def _re_embed_placeholders(backend: MemoryBackend) -> int:
    """Synchronously embed any rows the async worker missed.

    Relies on the duck-typed interface introduced by DenseMemory and
    FAISSMemory: ``placeholder_ids()``, ``contents_for()``, and
    ``update_vector()``.  Backends that don't implement these return 0.
    """
    placeholder_ids = getattr(backend, "placeholder_ids", None)
    contents_for = getattr(backend, "contents_for", None)
    update_vector = getattr(backend, "update_vector", None)
    embedder = getattr(backend, "_embedder", None) or getattr(
        backend, "_get_embedder", lambda: None,
    )()
    if not (placeholder_ids and contents_for and update_vector and embedder):
        return 0

    ids = list(placeholder_ids())
    if not ids:
        return 0

    # Resolve contents — skip any the backend no longer knows about.
    pairs: List[tuple] = []
    for doc_id in ids:
        content = contents_for(doc_id)
        if content is not None:
            pairs.append((doc_id, content))
    if not pairs:
        return 0

    texts = [p[1] for p in pairs]
    vectors = embedder.embed(texts)
    for (doc_id, _), vec in zip(pairs, vectors):
        update_vector(doc_id, vec)
    return len(pairs)


def _rebuild_index(backend: MemoryBackend) -> bool:
    """Kick any backend-specific index rebuild.  Returns True if rebuilt."""
    # DenseMemory is already always-current (the matrix IS the index).
    # FAISS needs an explicit rebuild after we patch vectors in place.
    rebuild_fn = getattr(backend, "rebuild_index", None)
    if callable(rebuild_fn):
        rebuild_fn()
        return True
    return False


def _distill_skills(
    trace_provider: Optional[Any] = None,
    skill_sink: Optional[Any] = None,
) -> int:
    """Run SkillDiscovery.analyze_traces over recent traces.

    Both *trace_provider* and *skill_sink* are injected so tests can stub
    them.  ``trace_provider()`` must return an iterable of trace objects;
    ``skill_sink(discovered)`` is called once with the resulting list.
    If either is missing we silently skip — skill mining is a nice-to-
    have, not a required part of a healthy run.
    """
    if trace_provider is None or skill_sink is None:
        return 0
    try:
        from hope.learning.agents.skill_discovery import SkillDiscovery
    except Exception:  # pragma: no cover - optional dep
        logger.debug("SkillDiscovery unavailable; skipping skill distillation")
        return 0

    traces = list(trace_provider())
    if not traces:
        return 0

    discovery = SkillDiscovery()
    discovered = discovery.analyze_traces(traces)
    if discovered:
        skill_sink(discovered)
    return len(discovered)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    backends: Iterable[MemoryBackend],
    *,
    trace_provider: Optional[Any] = None,
    skill_sink: Optional[Any] = None,
    flush_timeout_s: float = 30.0,
) -> ConsolidationReport:
    """Run a consolidation pass over *backends*.

    Parameters
    ----------
    backends:
        Memory backends to consolidate.  Backends that don't support
        async or index rebuilds are no-ops here.
    trace_provider:
        Optional zero-arg callable returning an iterable of traces.
    skill_sink:
        Optional callable invoked with ``List[DiscoveredSkill]``.
    flush_timeout_s:
        How long to wait for the async embed queue to drain before we
        give up and synchronously mop up any still-pending rows.
    """
    t0 = time.time()
    report = ConsolidationReport()

    # 1. Drain the async queue first so placeholder_ids() settles.
    try:
        from hope.tools.storage.async_embedder import get_async_embedder
        # We don't create a queue if none exists — embedder is None-safe.
        import hope.tools.storage.async_embedder as _aq_mod
        if _aq_mod._GLOBAL_QUEUE is not None:
            _aq_mod._GLOBAL_QUEUE.flush(timeout=flush_timeout_s)
    except Exception as exc:  # pragma: no cover - best-effort
        report.errors.append(f"flush_async_queue: {exc!r}")

    # 2. Per-backend: synchronously embed any stragglers, then rebuild.
    for backend in backends:
        bid = getattr(backend, "backend_id", type(backend).__name__)
        report.backends.append(bid)
        try:
            report.re_embedded += _re_embed_placeholders(backend)
            if _rebuild_index(backend):
                report.rebuilt_indexes += 1
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("consolidation failed for backend %s", bid)
            report.errors.append(f"{bid}: {exc!r}")

    # 3. Skill distillation.
    try:
        report.new_skills = _distill_skills(trace_provider, skill_sink)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("skill distillation failed")
        report.errors.append(f"skills: {exc!r}")

    report.duration_ms = int((time.time() - t0) * 1000)

    # 4. Emit event.
    try:
        bus = get_event_bus()
        bus.publish(
            EventType.MEMORY_CONSOLIDATED,
            {
                "rebuilt_indexes": report.rebuilt_indexes,
                "re_embedded": report.re_embedded,
                "new_skills": report.new_skills,
                "duration_ms": report.duration_ms,
                "backends": list(report.backends),
                "errors": list(report.errors),
            },
        )
    except Exception:  # pragma: no cover - best-effort
        logger.debug("event bus unavailable for MEMORY_CONSOLIDATED")

    return report


__all__ = [
    "ConsolidationReport",
    "run",
]
