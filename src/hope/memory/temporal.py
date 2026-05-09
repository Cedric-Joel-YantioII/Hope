"""Temporal-fact layer on top of Hope's existing RAG.

Solves the three pains a knowledge-graph would fix without standing up
a graph DB:

* Temporal facts (a person had role X 2023-24, role Y now) — stored
  as separate documents with non-overlapping ``valid_from`` /
  ``valid_to`` windows.
* Entity disambiguation — every fact is tagged with an entity id; a
  search for "Alex" can return distinct entities instead of a
  jumbled mix.
* Supersedence — when a fact becomes wrong, write a new fact and
  point its ``superseded_by`` at the old one (or call
  :func:`supersede_fact`).

Storage is the existing :class:`hope.memory.rag.RAGMemory` backend —
zero new RAM cost, zero new processes. Temporal metadata lives in the
RAG's per-document ``metadata`` JSON blob (already supported by
SQLiteMemory).

Schema in metadata:

  {
    "kind": "temporal_fact",
    "entity": "<canonical-id e.g. 'alex-chen-stripe'>",
    "valid_from": <unix-ts | null>,    # null = always-was
    "valid_to":   <unix-ts | null>,    # null = still-valid
    "superseded_by": "<doc_id | null>",
    "tags": [...],
  }

Reads filter on ``kind == "temporal_fact"`` and the validity window;
``current_facts`` returns one record per entity (most-recent valid).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class TemporalFact:
    """A temporal-fact view over a single RAG document."""

    doc_id: str
    entity: str
    content: str
    valid_from: Optional[float]
    valid_to: Optional[float]
    superseded_by: Optional[str]
    score: float
    source: str
    tags: List[str]


def _coerce_ts(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            # Best-effort parse for ISO-8601 like '2026-04-22T13:37:50'.
            from datetime import datetime
            try:
                return datetime.fromisoformat(value.rstrip("Z")).timestamp()
            except ValueError:
                return None
    return None


def store_fact(
    entity: str,
    content: str,
    *,
    valid_from: Any = None,
    valid_to: Any = None,
    source: str = "",
    tags: Optional[List[str]] = None,
    backend: Any = None,
) -> str:
    """Persist a temporal fact for *entity* and return its doc_id.

    ``backend`` defaults to the singleton RAG backend
    (``hope.memory.get_rag().backend``). Pass an explicit one for tests.
    """
    if backend is None:
        from hope.memory import get_rag
        backend = get_rag().backend
    if not entity or not content.strip():
        raise ValueError("store_fact: entity and content required")
    metadata: Dict[str, Any] = {
        "kind": "temporal_fact",
        "entity": entity,
        "valid_from": _coerce_ts(valid_from),
        "valid_to": _coerce_ts(valid_to),
        "superseded_by": None,
        "tags": list(tags or []),
        "stored_at": time.time(),
    }
    return backend.store(content, source=source or f"fact:{entity}", metadata=metadata)


def supersede_fact(
    old_doc_id: str,
    new_content: str,
    *,
    entity: Optional[str] = None,
    valid_from: Any = None,
    source: str = "",
    tags: Optional[List[str]] = None,
    backend: Any = None,
) -> str:
    """Write a new fact that supersedes *old_doc_id*.

    The old fact's ``valid_to`` is set to *valid_from* (or now), and
    its ``superseded_by`` points at the new doc. Returns the new
    doc_id. The old document is NOT deleted — provenance is preserved.
    """
    if backend is None:
        from hope.memory import get_rag
        backend = get_rag().backend
    cutoff = _coerce_ts(valid_from) or time.time()
    old = _fetch_meta(backend, old_doc_id)
    if old is None:
        raise KeyError(f"no fact with doc_id={old_doc_id!r}")
    inferred_entity = entity or old.get("entity") or ""
    new_doc_id = store_fact(
        inferred_entity, new_content,
        valid_from=cutoff, valid_to=None,
        source=source, tags=tags, backend=backend,
    )
    # Mutate the old fact's metadata in place. SQLiteMemory has no
    # update API, but the underlying document table does — go direct.
    _patch_meta(
        backend, old_doc_id,
        {"valid_to": cutoff, "superseded_by": new_doc_id},
    )
    return new_doc_id


def current_facts(
    entity: str,
    *,
    as_of: Any = None,
    top_k: int = 5,
    backend: Any = None,
) -> List[TemporalFact]:
    """Return the currently-valid facts for *entity*.

    ``as_of`` defaults to "now". A fact is valid at ``t`` iff
    ``valid_from is None or valid_from <= t`` AND
    ``valid_to is None or valid_to > t`` AND
    ``superseded_by is None``.
    """
    if backend is None:
        from hope.memory import get_rag
        backend = get_rag().backend
    moment = _coerce_ts(as_of) or time.time()
    rows = _query_by_entity(backend, entity)
    out: List[TemporalFact] = []
    for r in rows:
        meta = r["meta"]
        if meta.get("superseded_by"):
            continue
        vf, vt = meta.get("valid_from"), meta.get("valid_to")
        if vf is not None and float(vf) > moment:
            continue
        if vt is not None and float(vt) <= moment:
            continue
        out.append(_row_to_fact(r))
        if len(out) >= top_k:
            break
    return out


def fact_history(
    entity: str,
    *,
    top_k: int = 50,
    backend: Any = None,
) -> List[TemporalFact]:
    """Return EVERY recorded fact for *entity*, valid or not, sorted
    most-recent-first by ``valid_from`` (or ``stored_at``).
    """
    if backend is None:
        from hope.memory import get_rag
        backend = get_rag().backend
    rows = _query_by_entity(backend, entity)
    facts = [_row_to_fact(r) for r in rows]
    facts.sort(
        key=lambda f: (f.valid_from or 0.0, f.doc_id),
        reverse=True,
    )
    return facts[:top_k]


# ---------------------------------------------------------------------------
# Internals — go direct to SQLite for the few operations the backend
# doesn't expose (metadata patch, doc lookup by id).
# ---------------------------------------------------------------------------


def _query_by_entity(backend: Any, entity: str) -> List[Dict[str, Any]]:
    """Return all temporal-fact rows for *entity* via direct SQL.

    Bypasses FTS5 (which mis-tokenises hyphens) and goes straight to
    the documents table, filtering by ``json_extract(metadata,'$.entity')``.
    """
    db_path = getattr(backend, "_db_path", None)
    if not db_path:
        return []
    import sqlite3
    out: List[Dict[str, Any]] = []
    with sqlite3.connect(db_path) as conn:
        for row in conn.execute(
            "SELECT id, content, source, metadata, created_at "
            "FROM documents "
            "WHERE json_extract(metadata, '$.kind') = 'temporal_fact' "
            "  AND json_extract(metadata, '$.entity') = ? "
            "ORDER BY created_at DESC",
            (entity,),
        ):
            try:
                meta = json.loads(row[3]) if row[3] else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            out.append({
                "id": row[0],
                "content": row[1] or "",
                "source": row[2] or "",
                "meta": meta,
                "created_at": row[4],
            })
    return out


def _row_to_fact(r: Dict[str, Any]) -> TemporalFact:
    meta = r["meta"]
    return TemporalFact(
        doc_id=r["id"],
        entity=str(meta.get("entity", "")),
        content=r["content"],
        valid_from=_coerce_ts(meta.get("valid_from")),
        valid_to=_coerce_ts(meta.get("valid_to")),
        superseded_by=meta.get("superseded_by"),
        score=0.0,  # not from FTS retrieval, no score
        source=r["source"],
        tags=list(meta.get("tags") or []),
    )


def _fetch_meta(backend: Any, doc_id: str) -> Optional[Dict[str, Any]]:
    db_path = getattr(backend, "_db_path", None)
    if not db_path:
        return None
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT metadata FROM documents WHERE id = ?", (doc_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0]) if row[0] else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _patch_meta(backend: Any, doc_id: str, patch: Dict[str, Any]) -> None:
    db_path = getattr(backend, "_db_path", None)
    if not db_path:
        return
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT metadata FROM documents WHERE id = ?", (doc_id,),
        ).fetchone()
        if row is None:
            return
        try:
            meta = json.loads(row[0]) if row[0] else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        meta.update(patch)
        conn.execute(
            "UPDATE documents SET metadata = ? WHERE id = ?",
            (json.dumps(meta), doc_id),
        )
        conn.commit()


def _to_fact(hit: Any, meta: Dict[str, Any]) -> TemporalFact:
    return TemporalFact(
        doc_id=getattr(hit, "id", "") or getattr(hit, "doc_id", ""),
        entity=str(meta.get("entity", "")),
        content=getattr(hit, "content", "") or "",
        valid_from=_coerce_ts(meta.get("valid_from")),
        valid_to=_coerce_ts(meta.get("valid_to")),
        superseded_by=meta.get("superseded_by"),
        score=float(getattr(hit, "score", 0.0) or 0.0),
        source=getattr(hit, "source", "") or "",
        tags=list(meta.get("tags") or []),
    )


__all__ = [
    "TemporalFact",
    "current_facts",
    "fact_history",
    "store_fact",
    "supersede_fact",
]
