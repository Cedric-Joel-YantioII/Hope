"""Commitment storage + retrieval helpers.

Commitments live in their own namespace (``commitments``) in AgentDB and
follow a strict schema so the proactive-recall loop and the nightly
consolidator can reason about them without LLM cost:

    key        : "commit:<short-slug>"
    namespace  : "commitments"
    content    : human-readable one-liner — what Hope will say back
    metadata   : JSON {
        "who":     "<person name, or 'self'>",
        "what":    "<action phrase>",
        "due":     "<ISO date YYYY-MM-DD OR 'today'/'tomorrow'>",
        "status":  "pending" | "done" | "cancelled",
        "created_at": <unix ms>,
        "last_reminded_at": <unix ms or 0>,
        "source":  "user_said" | "consolidator" | "brain_inferred",
    }
    tags       : "commitment"

Readers can query by namespace alone — no vector search needed, since
commitment volume is low (tens to low hundreds per user per year).

This module is intentionally **sqlite-direct**, not via the claude-flow
MCP bridge: the proactive-recall thread runs inside the daemon process
and needs synchronous deterministic access to the pinned memory DB
(``CLAUDE_FLOW_MEMORY_DB``). Going through MCP would add a subprocess
hop per poll, which is overkill for a 15-min loop.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def _db_path() -> Path:
    """Resolve the canonical memory DB path same way memory-bridge does."""
    env = os.environ.get("CLAUDE_FLOW_MEMORY_DB") or os.environ.get("HOPE_MEMORY_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hope" / "memory.db"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class Commitment:
    key: str
    content: str
    who: str = "self"
    what: str = ""
    due: str = "today"  # ISO yyyy-mm-dd, or 'today' / 'tomorrow'
    status: str = "pending"
    created_at_ms: int = 0
    last_reminded_at_ms: int = 0
    source: str = "user_said"
    tags: str = "commitment"

    # -- parsing ------------------------------------------------------------
    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Commitment":
        md = {}
        try:
            md = json.loads(row["metadata"] or "{}")
        except Exception:
            pass
        return cls(
            key=row["key"],
            content=row["content"] or "",
            who=md.get("who", "self"),
            what=md.get("what", row["content"] or ""),
            due=md.get("due", "today"),
            status=md.get("status", "pending"),
            created_at_ms=int(md.get("created_at", row["created_at"] or 0)),
            last_reminded_at_ms=int(md.get("last_reminded_at", 0)),
            source=md.get("source", "user_said"),
            tags=row["tags"] or "commitment",
        )

    # -- due-date logic -----------------------------------------------------
    def due_date(self) -> Optional[datetime]:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        d = (self.due or "").strip().lower()
        if d == "today":
            return today
        if d == "tomorrow":
            return today + timedelta(days=1)
        try:
            return datetime.fromisoformat(d).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
        except Exception:
            return None

    def is_due_within(self, hours: float) -> bool:
        """True if the commitment is due within *hours* from now."""
        dd = self.due_date()
        if dd is None:
            return False
        delta = (dd - datetime.now()).total_seconds() / 3600
        # Allow slightly-past-due to still surface (up to 2 days overdue).
        return -48 <= delta <= hours


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def list_pending(db_path: Optional[Path] = None) -> List[Commitment]:
    """Return all pending commitments. Never raises — caller-safe."""
    path = db_path or _db_path()
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, key, namespace, content, metadata, tags, created_at "
            "FROM memory_entries "
            "WHERE status='active' AND namespace='commitments' "
            "ORDER BY created_at DESC LIMIT 500"
        ).fetchall()
        conn.close()
    except Exception:
        logger.debug("commitments: read failed", exc_info=True)
        return []
    out: List[Commitment] = []
    for r in rows:
        try:
            c = Commitment.from_row(r)
        except Exception:
            continue
        if c.status == "pending":
            out.append(c)
    return out


def next_recall_candidate(
    within_hours: float = 24.0,
    min_hours_since_last_reminder: float = 4.0,
    db_path: Optional[Path] = None,
) -> Optional[Commitment]:
    """Pick the most pressing commitment to surface, or None.

    Rules:
    - Must be ``pending`` and due within *within_hours*.
    - Must not have been reminded in the last
      *min_hours_since_last_reminder* hours.
    - Among candidates, prefer the one with the EARLIEST due_date, then
      the EARLIEST created_at (FIFO tie-break).
    """
    now_ms = int(time.time() * 1000)
    gate_ms = int(min_hours_since_last_reminder * 3600 * 1000)
    candidates = [
        c for c in list_pending(db_path)
        if c.is_due_within(within_hours)
        and (now_ms - c.last_reminded_at_ms) >= gate_ms
    ]
    if not candidates:
        return None
    # Earliest due first; within same due, oldest-created wins.
    candidates.sort(
        key=lambda c: (c.due_date() or datetime.max, c.created_at_ms)
    )
    return candidates[0]


# ---------------------------------------------------------------------------
# Write path — only for the last_reminded_at bump after surfacing.
# Live creates go through the MCP memory_store that the brain already uses;
# consolidator creates go via the bridge too.
# ---------------------------------------------------------------------------

def mark_reminded(key: str, db_path: Optional[Path] = None) -> bool:
    """Stamp ``last_reminded_at = now`` on an existing commitment.

    Returns True if a row was updated. Works directly on SQLite to avoid
    a bridge round-trip — the proactive-recall loop runs synchronously.
    """
    path = db_path or _db_path()
    if not path.exists():
        return False
    now_ms = int(time.time() * 1000)
    try:
        conn = sqlite3.connect(str(path), timeout=2.0)
        cur = conn.execute(
            "SELECT metadata FROM memory_entries "
            "WHERE namespace='commitments' AND key=?",
            (key,),
        )
        row = cur.fetchone()
        if row is None:
            conn.close()
            return False
        try:
            md = json.loads(row[0] or "{}")
        except Exception:
            md = {}
        md["last_reminded_at"] = now_ms
        conn.execute(
            "UPDATE memory_entries SET metadata=?, updated_at=? "
            "WHERE namespace='commitments' AND key=?",
            (json.dumps(md), now_ms, key),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        logger.debug("commitments: mark_reminded failed", exc_info=True)
        return False


__all__ = ["Commitment", "list_pending", "next_recall_candidate", "mark_reminded"]
