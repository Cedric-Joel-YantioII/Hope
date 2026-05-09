"""Voice-turn trace store.

Every ``_process_transcript`` run in :mod:`hope.daemon.core` produces one
:class:`VoiceTurn` record here. This is the raw material the learning loop
scores + feeds into skill / ack / wake-phrase evolution.

Kept deliberately narrow:
  * One row per voice turn (user→Hope→TTS).
  * SQLite at ``~/.hope/traces.db`` (shared with the legacy ``TraceStore``
    but in its own table so nothing collides).
  * Rolling deletion once the DB file exceeds the size cap.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from hope.core.config import DEFAULT_CONFIG_DIR

logger = logging.getLogger(__name__)


DEFAULT_VOICE_TRACE_DB = DEFAULT_CONFIG_DIR / "traces.db"
DEFAULT_SIZE_CAP_BYTES = 100 * 1024 * 1024  # 100 MB


_CREATE = """\
CREATE TABLE IF NOT EXISTS voice_turns (
    turn_id          TEXT    PRIMARY KEY,
    started_at       REAL    NOT NULL,
    ended_at         REAL    NOT NULL DEFAULT 0.0,
    user_transcript  TEXT    NOT NULL DEFAULT '',
    ack_spoken       TEXT    NOT NULL DEFAULT '',
    brain_request    TEXT    NOT NULL DEFAULT '',
    brain_reply_full TEXT    NOT NULL DEFAULT '',
    brain_reply_head TEXT    NOT NULL DEFAULT '',
    tts_spoken       TEXT    NOT NULL DEFAULT '',
    duration_seconds REAL    NOT NULL DEFAULT 0.0,
    error            TEXT    NOT NULL DEFAULT '',
    score            REAL,
    score_reason     TEXT    NOT NULL DEFAULT '',
    skill_tags       TEXT    NOT NULL DEFAULT '[]',
    metadata         TEXT    NOT NULL DEFAULT '{}'
);
"""

_CREATE_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_voice_turns_started "
    "ON voice_turns(started_at DESC);"
)


@dataclass(slots=True)
class VoiceTurn:
    """One complete voice turn: user utterance → Hope reply → TTS."""

    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    user_transcript: str = ""
    ack_spoken: str = ""
    brain_request: str = ""
    brain_reply_full: str = ""
    brain_reply_head: str = ""
    tts_spoken: str = ""
    duration_seconds: float = 0.0
    error: str = ""
    score: Optional[float] = None
    score_reason: str = ""
    skill_tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> tuple:
        return (
            self.turn_id,
            self.started_at,
            self.ended_at,
            self.user_transcript,
            self.ack_spoken,
            self.brain_request,
            self.brain_reply_full,
            self.brain_reply_head,
            self.tts_spoken,
            self.duration_seconds,
            self.error,
            self.score,
            self.score_reason,
            json.dumps(self.skill_tags),
            json.dumps(self.metadata),
        )


class VoiceTraceStore:
    """Thread-safe append-only store for voice turns."""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_VOICE_TRACE_DB,
        *,
        size_cap_bytes: int = DEFAULT_SIZE_CAP_BYTES,
    ) -> None:
        self._db_path = str(db_path)
        self._size_cap = max(1_000_000, int(size_cap_bytes))
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe with WAL; inserts happen from the
        # brain-executor thread, queries from the scoring thread or CLI.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE)
        self._conn.execute(_CREATE_IDX)
        self._conn.commit()

    # ── write ────────────────────────────────────────────────────────

    def save(self, turn: VoiceTurn) -> None:
        """Insert *or* update a turn (keyed by turn_id)."""
        self._conn.execute(
            "INSERT OR REPLACE INTO voice_turns ("
            "turn_id, started_at, ended_at, user_transcript, ack_spoken, "
            "brain_request, brain_reply_full, brain_reply_head, tts_spoken, "
            "duration_seconds, error, score, score_reason, skill_tags, metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            turn.to_row(),
        )
        self._conn.commit()
        self._maybe_rotate()

    def update_score(
        self,
        turn_id: str,
        score: Optional[float],
        reason: str = "",
    ) -> bool:
        cur = self._conn.execute(
            "UPDATE voice_turns SET score = ?, score_reason = ? WHERE turn_id = ?",
            (score, reason, turn_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ── read ─────────────────────────────────────────────────────────

    def list_recent(
        self,
        *,
        since: Optional[float] = None,
        limit: int = 200,
        only_unscored: bool = False,
    ) -> List[VoiceTurn]:
        clauses: List[str] = []
        params: List[Any] = []
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since)
        if only_unscored:
            clauses.append("score IS NULL")
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = (
            f"SELECT * FROM voice_turns WHERE {where} "
            "ORDER BY started_at DESC LIMIT ?"
        )
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_turn(r) for r in rows]

    def get(self, turn_id: str) -> Optional[VoiceTurn]:
        row = self._conn.execute(
            "SELECT * FROM voice_turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return self._row_to_turn(row) if row else None

    def count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM voice_turns").fetchone()[0]
        )

    def stats_since(self, since: float) -> Dict[str, Any]:
        """Compact stats for the self-report skill."""
        row = self._conn.execute(
            "SELECT COUNT(*), AVG(duration_seconds), AVG(score), "
            "SUM(CASE WHEN error != '' THEN 1 ELSE 0 END) "
            "FROM voice_turns WHERE started_at >= ?",
            (since,),
        ).fetchone()
        count, avg_latency, avg_score, errors = row or (0, 0.0, None, 0)
        return {
            "turns": int(count or 0),
            "avg_latency_seconds": float(avg_latency or 0.0),
            "avg_score": float(avg_score) if avg_score is not None else None,
            "errors": int(errors or 0),
        }

    def close(self) -> None:
        self._conn.close()

    # ── maintenance ──────────────────────────────────────────────────

    def _maybe_rotate(self) -> None:
        """Drop the oldest 20% of rows if the DB file exceeds the cap."""
        if self._db_path == ":memory:":
            return
        try:
            size = os.path.getsize(self._db_path)
        except OSError:
            return
        if size < self._size_cap:
            return
        try:
            total = self.count()
            if total < 100:
                return
            cutoff_row = self._conn.execute(
                "SELECT started_at FROM voice_turns "
                "ORDER BY started_at ASC LIMIT 1 OFFSET ?",
                (total // 5,),
            ).fetchone()
            if cutoff_row is None:
                return
            self._conn.execute(
                "DELETE FROM voice_turns WHERE started_at < ?", (cutoff_row[0],)
            )
            self._conn.execute("VACUUM")
            self._conn.commit()
            logger.info(
                "voice_trace: rotated oldest 20%% of %d rows (cap=%d)",
                total, self._size_cap,
            )
        except Exception:
            logger.exception("voice_trace rotation failed")

    # ── internal ─────────────────────────────────────────────────────

    @staticmethod
    def _row_to_turn(row: tuple) -> VoiceTurn:
        return VoiceTurn(
            turn_id=row[0],
            started_at=row[1],
            ended_at=row[2],
            user_transcript=row[3],
            ack_spoken=row[4],
            brain_request=row[5],
            brain_reply_full=row[6],
            brain_reply_head=row[7],
            tts_spoken=row[8],
            duration_seconds=row[9],
            error=row[10],
            score=row[11],
            score_reason=row[12],
            skill_tags=json.loads(row[13] or "[]"),
            metadata=json.loads(row[14] or "{}"),
        )


def as_dict(turn: VoiceTurn) -> Dict[str, Any]:
    """Convenience — asdict that keeps JSON-serialisable fields."""
    return asdict(turn)


__all__ = [
    "DEFAULT_VOICE_TRACE_DB",
    "VoiceTurn",
    "VoiceTraceStore",
    "as_dict",
]
