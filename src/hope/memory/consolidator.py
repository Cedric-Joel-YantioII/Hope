"""Nightly memory consolidator.

Walks the last ~26 h of entries in the ``claude-memories`` namespace,
asks a claude-brain subprocess to extract **commitments, themes, and
people**, and writes them back to structured namespaces where the
proactive-recall loop (commitments) and future UI surfaces (themes,
people) can find them.

Why a subprocess: the consolidator needs Claude-quality extraction but
shouldn't touch Hope's live brain tmux pane. ``claude -p`` gives us a
one-shot, non-interactive call on the Max subscription with a tight
system prompt. Mean wall-time on M-series ≈ 8–20 s per run depending on
corpus size.

Idempotence: we track ``last_run_ts`` in ``~/.hope/consolidator-state.json``
so each run only reads rows *newer* than the previous run (with a small
6 h overlap so nothing slips through). Commitments are deduped by a
content hash; themes/people are upserted by key.

Failures are logged and swallowed — a bad LLM response, a missing DB,
or a claude-CLI crash must never tear down the nightly launchd agent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_PATH = Path.home() / ".hope" / "consolidator-state.json"
DEFAULT_LOOKBACK_HOURS = 26.0
OVERLAP_HOURS = 6.0

# Claude-p subprocess timeout (walltime). Large enough for xhigh on a
# long corpus, small enough that a hung claude doesn't wedge launchd.
CLAUDE_TIMEOUT_SEC = 300


def _db_path() -> Path:
    env = os.environ.get("CLAUDE_FLOW_MEMORY_DB") or os.environ.get("HOPE_MEMORY_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hope" / "memory.db"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Corpus assembly
# ---------------------------------------------------------------------------

def _collect_recent_memories(since_ts_ms: int) -> List[sqlite3.Row]:
    path = _db_path()
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT key, namespace, content, tags, created_at "
        "FROM memory_entries "
        "WHERE status='active' "
        "  AND namespace IN ('claude-memories', 'default', 'auto-memory') "
        "  AND created_at > ? "
        "ORDER BY created_at ASC LIMIT 1000",
        (since_ts_ms,),
    ).fetchall()
    conn.close()
    return rows


def _existing_commitment_keys() -> set:
    """Keys already in the ``commitments`` namespace — used for dedupe."""
    path = _db_path()
    if not path.exists():
        return set()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    rows = conn.execute(
        "SELECT key FROM memory_entries "
        "WHERE status='active' AND namespace='commitments'"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are Hope's nightly memory consolidator.

You receive a list of raw memory entries that Hope stored throughout the
day (user remarks, observations, saved notes). Your job is to extract
three things and return them as STRICT JSON — no prose, no markdown
fences, no comments.

Extract ONLY from the evidence in the memories. Never invent.

Schema:

{
  "commitments": [
    {
      "who":  "<person name, or 'self' if it's something the user owes themselves>",
      "what": "<action, 2-8 words, lowercase-imperative where natural>",
      "due":  "<YYYY-MM-DD, or 'today'/'tomorrow' if the memory says exactly that>",
      "confidence": 0.0-1.0,
      "source_key": "<the memory key this came from>"
    }
  ],
  "themes": [
    {
      "name": "<short label, e.g. 'PRISM fundraise', 'Hope voice architecture'>",
      "summary": "<one sentence, what's happening in this theme this week>",
      "source_keys": ["<key>", ...]
    }
  ],
  "people": [
    {
      "name": "<person name>",
      "context": "<one-line who-they-are-to-user + most recent context>",
      "source_keys": ["<key>", ...]
    }
  ]
}

Rules:
- If none of a category is present, return an empty list.
- Skip vague intentions ("I should probably update the docs") — only include
  commitments with clear deadlines or real promises.
- Deduplicate: don't emit two items saying the same thing.
- `due` dates must be absolute (YYYY-MM-DD) when possible. Derive from
  phrases like "by Friday" using TODAY'S date (provided below).
- Confidence < 0.6 → don't emit.

Return ONLY the JSON object. No preamble, no epilogue.
"""


def _run_claude(prompt: str, timeout: int = CLAUDE_TIMEOUT_SEC) -> Optional[str]:
    """Invoke ``claude -p`` with the consolidator prompt. Returns stdout
    or None on any failure."""
    try:
        result = subprocess.run(
            [
                "claude", "-p",
                "--effort", "medium",
                "--append-system-prompt", _SYSTEM_PROMPT,
                prompt,
            ],
            capture_output=True, text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("consolidator: `claude` binary not on PATH")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("consolidator: claude -p timed out after %ds", timeout)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("consolidator: claude spawn failed: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning("consolidator: claude exited %s stderr=%r",
                       result.returncode, result.stderr[:300])
        return None
    return result.stdout


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_response(text: str) -> Optional[Dict[str, Any]]:
    """Be forgiving about extra prose around the JSON."""
    if not text:
        return None
    # Try whole text first.
    try:
        return json.loads(text)
    except Exception:
        pass
    # Extract the outermost {...} block.
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _build_user_prompt(rows: List[sqlite3.Row]) -> str:
    today = time.strftime("%Y-%m-%d", time.localtime())
    dow = time.strftime("%A", time.localtime())
    lines = [
        f"TODAY = {today} ({dow})",
        "",
        "MEMORIES (last 24 h, oldest first):",
        "",
    ]
    for i, r in enumerate(rows, 1):
        content = (r["content"] or "").replace("\n", " ").strip()
        tags = r["tags"] or ""
        lines.append(
            f"[{i}] key={r['key']}  tags={tags[:40]}\n    {content[:600]}"
        )
    lines.append("")
    lines.append("Return the JSON object now.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write path — uses claude-flow CLI so the normal pipeline (embedding,
# dashboard MEMORY_STORE event) fires without duplicating code here.
# ---------------------------------------------------------------------------

def _memory_store(
    key: str, value: str, namespace: str,
    tags: str = "", metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    env = os.environ.copy()
    # Make sure the child CLI writes to the same pinned DB we're reading.
    if "CLAUDE_FLOW_MEMORY_DB" not in env:
        env["CLAUDE_FLOW_MEMORY_DB"] = str(_db_path())
    try:
        result = subprocess.run(
            [
                "/opt/homebrew/bin/claude-flow", "memory", "store",
                "--key", key, "--value", value,
                "--namespace", namespace,
                "--tags", tags or namespace,
                "--upsert",
            ],
            env=env,
            capture_output=True, text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("consolidator: memory store spawn failed: %s", exc)
        return False
    if result.returncode != 0:
        logger.warning("consolidator: memory store %s/%s exited %s",
                       namespace, key, result.returncode)
        return False
    # Second step: if metadata supplied, stamp it onto the row via direct
    # SQL. The CLI doesn't accept --metadata flags reliably.
    if metadata:
        try:
            conn = sqlite3.connect(str(_db_path()), timeout=5.0)
            conn.execute(
                "UPDATE memory_entries SET metadata=?, updated_at=? "
                "WHERE namespace=? AND key=?",
                (json.dumps(metadata), int(time.time() * 1000),
                 namespace, key),
            )
            conn.commit()
            conn.close()
        except Exception:
            logger.debug("consolidator: metadata stamp failed", exc_info=True)
    return True


def _slug(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:maxlen] or "item"


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

@dataclass
class ConsolidationResult:
    memories_scanned: int
    commitments_added: int
    themes_added: int
    people_added: int
    error: Optional[str] = None


def run_once() -> ConsolidationResult:
    """Walk recent memories, extract commitments + themes + people, write
    them back. Idempotent — safe to call repeatedly."""
    state = _load_state()
    last_ts = int(state.get("last_run_ts", 0))
    # Re-scan with overlap so nothing slips through between runs.
    cutoff_ts = max(
        0,
        last_ts - int(OVERLAP_HOURS * 3600 * 1000),
        int((time.time() - DEFAULT_LOOKBACK_HOURS * 3600) * 1000),
    )
    if last_ts == 0:
        # Cold-start: scan the full default lookback window.
        cutoff_ts = int((time.time() - DEFAULT_LOOKBACK_HOURS * 3600) * 1000)

    rows = _collect_recent_memories(cutoff_ts)
    if not rows:
        state["last_run_ts"] = int(time.time() * 1000)
        _save_state(state)
        return ConsolidationResult(0, 0, 0, 0)

    user_prompt = _build_user_prompt(rows)
    raw = _run_claude(user_prompt)
    parsed = _parse_json_response(raw or "")
    if not parsed:
        return ConsolidationResult(
            memories_scanned=len(rows),
            commitments_added=0, themes_added=0, people_added=0,
            error="empty_or_invalid_json",
        )

    now_ms = int(time.time() * 1000)
    commitments_added = themes_added = people_added = 0

    # --- Commitments -----------------------------------------------------
    existing = _existing_commitment_keys()
    for c in (parsed.get("commitments") or []):
        who = str(c.get("who", "self")).strip()
        what = str(c.get("what", "")).strip()
        due = str(c.get("due", "today")).strip()
        conf = float(c.get("confidence", 0.0))
        src = str(c.get("source_key", ""))
        if not what or conf < 0.6:
            continue
        # Dedupe key: hash of (who, what, due) so identical commitments
        # across runs collapse to the same key.
        sig = hashlib.sha1(
            f"{who.lower()}|{what.lower()}|{due.lower()}".encode()
        ).hexdigest()[:10]
        key = f"commit:{_slug(what, 30)}-{sig}"
        if key in existing:
            continue
        value = f"{what} — for {who} by {due}"
        ok = _memory_store(
            key, value, namespace="commitments",
            tags="commitment,consolidated",
            metadata={
                "who": who, "what": what, "due": due,
                "status": "pending",
                "created_at": now_ms,
                "last_reminded_at": 0,
                "source": "consolidator",
                "source_key": src,
                "confidence": conf,
            },
        )
        if ok:
            commitments_added += 1
            existing.add(key)

    # --- Themes ----------------------------------------------------------
    for t in (parsed.get("themes") or []):
        name = str(t.get("name", "")).strip()
        summary = str(t.get("summary", "")).strip()
        sources = t.get("source_keys") or []
        if not name or not summary:
            continue
        key = f"theme:{_slug(name, 40)}"
        if _memory_store(
            key, f"{name}: {summary}",
            namespace="consolidated",
            tags="theme,consolidated",
            metadata={
                "name": name, "summary": summary,
                "source_keys": sources,
                "updated_at": now_ms,
            },
        ):
            themes_added += 1

    # --- People ----------------------------------------------------------
    for p in (parsed.get("people") or []):
        name = str(p.get("name", "")).strip()
        context = str(p.get("context", "")).strip()
        sources = p.get("source_keys") or []
        if not name or not context:
            continue
        key = f"person:{_slug(name, 40)}"
        if _memory_store(
            key, f"{name} — {context}",
            namespace="consolidated",
            tags="person,consolidated",
            metadata={
                "name": name, "context": context,
                "source_keys": sources,
                "updated_at": now_ms,
            },
        ):
            people_added += 1

    # Persist progress.
    state["last_run_ts"] = now_ms
    state["last_scanned"] = len(rows)
    state["last_commitments_added"] = commitments_added
    _save_state(state)

    return ConsolidationResult(
        memories_scanned=len(rows),
        commitments_added=commitments_added,
        themes_added=themes_added,
        people_added=people_added,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[consolidator] %(asctime)s %(levelname)s %(message)s",
    )
    try:
        r = run_once()
    except Exception:
        logger.exception("consolidator: unhandled failure")
        return 1
    msg = (f"scanned={r.memories_scanned} commitments+={r.commitments_added} "
           f"themes+={r.themes_added} people+={r.people_added}")
    if r.error:
        msg += f" error={r.error}"
    logger.info(msg)
    print(msg, file=sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
