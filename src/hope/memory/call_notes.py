"""Call-note storage + retrieval helpers.

Call notes live in their own namespace (``call_notes``) in AgentDB. Each
note is one transcribed utterance from a video call — Hope's "watch this
call with me" mode captures both the user's mic track and the remote
participants' track (via BlackHole) as separate streams, and each stream
drops its utterances here:

    key        : "call:<session_id>:<counter>"   (counter is per-session,
                                                  1-indexed, monotonic)
    namespace  : "call_notes"
    content    : the transcribed utterance text
    metadata   : JSON {
        "speaker":  "me" | "remote" | null,
        "tag":      "action" | "question" | "decision" | null,
    }
    tags       : "call-note"

Because call-mode writes at high frequency (one row per utterance, many
per minute), this module talks directly to SQLite and bypasses the
claude-flow MCP bridge — same pattern as ``commitments.mark_reminded``.
The per-session monotonic counter is computed under a ``BEGIN IMMEDIATE``
transaction so two transcriber threads can write concurrently without
racing; on the rare UNIQUE collision we retry a few times.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def _db_path() -> Path:
    """Resolve the canonical memory DB path same way memory-bridge does."""
    env = os.environ.get("CLAUDE_FLOW_MEMORY_DB") or os.environ.get("HOPE_MEMORY_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hope" / "memory.db"


def _resolve_path(db_path: Optional[str]) -> Path:
    if db_path is None:
        return _db_path()
    return Path(db_path).expanduser()


def _now_iso() -> str:
    """ISO8601 timestamp, UTC, millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class CallNote:
    session_id: str
    counter: int
    timestamp: str          # ISO8601
    text: str
    speaker: Optional[str] = None    # "me" | "remote" | None
    tag: Optional[str] = None        # "action" | "question" | "decision" | None


# ---------------------------------------------------------------------------
# Schema bootstrap (cheap, idempotent)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT 'default',
    content TEXT,
    metadata TEXT,
    tags TEXT,
    status TEXT DEFAULT 'active',
    created_at INTEGER,
    updated_at INTEGER,
    UNIQUE(namespace, key)
);
CREATE INDEX IF NOT EXISTS idx_mem_ns_created
    ON memory_entries(namespace, created_at DESC);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def _parse_counter(key: str, prefix: str) -> Optional[int]:
    """Extract the trailing integer counter from a ``call:<sid>:<n>`` key."""
    if not key.startswith(prefix):
        return None
    suffix = key[len(prefix):]
    try:
        return int(suffix)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

_MAX_INSERT_RETRIES = 3


def save_note(
    session_id: str,
    text: str,
    speaker: Optional[str] = None,
    tag: Optional[str] = None,
    db_path: Optional[str] = None,
) -> CallNote:
    """Persist one call-note utterance and return the stored ``CallNote``.

    Counter is computed per ``session_id`` under ``BEGIN IMMEDIATE`` so
    concurrent writers (mic + BlackHole transcriber threads) stay
    monotonic. On a UNIQUE collision (another writer committed the same
    counter between our SELECT and INSERT), we retry up to 3 times.
    """
    if not session_id:
        raise ValueError("session_id is required")
    if text is None:
        raise ValueError("text is required")

    path = _resolve_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = _now_iso()
    now_ms = int(time.time() * 1000)
    metadata_json = json.dumps({"speaker": speaker, "tag": tag})
    prefix = f"call:{session_id}:"

    last_err: Optional[Exception] = None
    for attempt in range(_MAX_INSERT_RETRIES):
        conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        try:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            # Compute next counter for this session. substr is 1-indexed,
            # so the starting position is len(prefix) + 1.
            row = conn.execute(
                "SELECT COALESCE(MAX(CAST(substr(key, ?) AS INTEGER)), 0) + 1 "
                "FROM memory_entries "
                "WHERE namespace='call_notes' AND key LIKE ?",
                (len(prefix) + 1, f"{prefix}%"),
            ).fetchone()
            counter = int(row[0]) if row and row[0] is not None else 1
            key = f"{prefix}{counter}"

            conn.execute(
                "INSERT INTO memory_entries "
                "(key, namespace, content, metadata, tags, status, created_at, updated_at) "
                "VALUES (?, 'call_notes', ?, ?, 'call-note', 'active', ?, ?)",
                (key, text, metadata_json, now_ms, now_ms),
            )
            conn.execute("COMMIT")
            return CallNote(
                session_id=session_id,
                counter=counter,
                timestamp=timestamp,
                text=text,
                speaker=speaker,
                tag=tag,
            )
        except sqlite3.IntegrityError as e:
            last_err = e
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            logger.debug(
                "call_notes: UNIQUE collision on attempt %d (%s)", attempt + 1, e,
            )
            # Brief back-off so the racing writer can finish.
            time.sleep(0.01 * (attempt + 1))
            continue
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # Exhausted retries.
    raise RuntimeError(
        f"call_notes.save_note: failed after {_MAX_INSERT_RETRIES} attempts"
    ) from last_err


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def list_notes(
    session_id: str,
    db_path: Optional[str] = None,
) -> List[CallNote]:
    """Return all notes for ``session_id``, ordered by counter ascending."""
    if not session_id:
        return []
    path = _resolve_path(db_path)
    if not path.exists():
        return []

    prefix = f"call:{session_id}:"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT key, content, metadata, created_at FROM memory_entries "
            "WHERE status='active' AND namespace='call_notes' AND key LIKE ? "
            "ORDER BY CAST(substr(key, ?) AS INTEGER) ASC",
            (f"{prefix}%", len(prefix) + 1),
        ).fetchall()
        conn.close()
    except Exception:
        logger.debug("call_notes: list_notes read failed", exc_info=True)
        return []

    out: List[CallNote] = []
    for r in rows:
        counter = _parse_counter(r["key"], prefix)
        if counter is None:
            continue
        try:
            md = json.loads(r["metadata"] or "{}")
        except Exception:
            md = {}
        created_ms = r["created_at"] or 0
        try:
            ts = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            )
        except Exception:
            ts = _now_iso()
        out.append(
            CallNote(
                session_id=session_id,
                counter=counter,
                timestamp=ts,
                text=r["content"] or "",
                speaker=md.get("speaker"),
                tag=md.get("tag"),
            )
        )
    return out


def list_sessions(db_path: Optional[str] = None) -> List[str]:
    """Return distinct session_ids, most-recent-note-time first (no cap)."""
    path = _resolve_path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        # For each session, find its most-recent created_at, then order
        # sessions by that timestamp DESC. Session id is the text between
        # the "call:" prefix and the last ":".
        rows = conn.execute(
            "SELECT key, created_at FROM memory_entries "
            "WHERE status='active' AND namespace='call_notes' "
            "ORDER BY created_at DESC, id DESC"
        ).fetchall()
        conn.close()
    except Exception:
        logger.debug("call_notes: list_sessions read failed", exc_info=True)
        return []

    seen: dict[str, int] = {}
    for r in rows:
        key = r["key"] or ""
        if not key.startswith("call:"):
            continue
        last = key.rfind(":")
        if last <= len("call:"):
            continue
        sid = key[len("call:"):last]
        if not sid:
            continue
        # First occurrence wins because the SQL scan is DESC.
        if sid not in seen:
            seen[sid] = int(r["created_at"] or 0)
    return list(seen.keys())


__all__ = ["CallNote", "save_note", "list_notes", "list_sessions"]


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "memory.db")

        a1 = save_note("sess-A", "hello there", speaker="me", db_path=db)
        a2 = save_note("sess-A", "general kenobi", speaker="remote", tag="question", db_path=db)
        b1 = save_note("sess-B", "unrelated utterance", db_path=db)

        assert a1.counter == 1, f"expected counter 1, got {a1.counter}"
        assert a2.counter == 2, f"expected counter 2, got {a2.counter}"
        assert b1.counter == 1, f"expected fresh counter 1, got {b1.counter}"

        notes_a = list_notes("sess-A", db_path=db)
        assert [n.counter for n in notes_a] == [1, 2], f"bad order: {notes_a}"
        assert notes_a[0].speaker == "me"
        assert notes_a[1].tag == "question"

        sessions = list_sessions(db_path=db)
        assert len(sessions) == 2, f"expected 2 sessions, got {sessions}"
        assert set(sessions) == {"sess-A", "sess-B"}

        print("OK")
