"""Long-running consumer that drains Hope's voice-out queue.

The brain (Claude Code CLI subprocesses, shells, skills) appends speech
requests to ``<HOPE_ROOT>/.hope-io/tts-out.jsonl``. This consumer tails
that file, calls Hope's TTS per record, and records completions in a
companion append-only log ``tts-out.done.jsonl``.

Design notes:
  * We never rewrite ``tts-out.jsonl`` in place. That would race with
    the flock-protected producer appends. Completion state lives in the
    separate ``.done.jsonl`` so append-only semantics hold on both files.
  * Recovered state on restart: read the done log, build the set of
    already-processed ids, then replay the queue file and only speak
    records whose id isn't in the set. Crash-safe by construction.
  * Priority: within each poll cycle, sort the newly-seen pending
    records by ``priority`` (higher first, missing = 0) before speaking.
    Across cycles order is FIFO — we don't reorder already-scheduled work.
  * TTS call: ``hope.audio.say.say_sync`` (blocking) is the documented
    entry point. Blocking is what we want — otherwise a burst of queued
    utterances would overlap in the speakers.

Runtime shape is a simple poll loop (default 250 ms). No inotify/kqueue
dependency — macOS's inotify surface is weak and the queue is low-rate.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import signal
import sys
import time
from typing import Iterable

# Hope is installed as an editable package in her venv, so a plain
# import works when the consumer runs under that venv's python.
from hope.audio.say import say_sync

logger = logging.getLogger("hope.voice_bridge.tts_consumer")

_HOPE_ROOT = pathlib.Path(__file__).resolve().parents[2]
_IO_DIR = _HOPE_ROOT / ".hope-io"
_QUEUE_PATH = _IO_DIR / "tts-out.jsonl"
_DONE_PATH = _IO_DIR / "tts-out.done.jsonl"
_DEFAULT_POLL_SEC = 0.25
_DEFAULT_MAX_LINE_BYTES = 1 << 20  # 1 MiB hard cap; TTS text should be tiny

_shutdown_requested = False


def _handle_signal(signum, _frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("tts_consumer: signal %d received; draining and exiting", signum)


def _load_done_ids(done_path: pathlib.Path) -> set[str]:
    """Read the completion log and return the set of already-handled ids."""
    done: set[str] = set()
    if not done_path.exists():
        return done
    try:
        with done_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    # Tolerate a partial line from a crash — skip.
                    continue
                rid = rec.get("id")
                if isinstance(rid, str):
                    done.add(rid)
    except OSError as exc:  # pragma: no cover — defensive
        logger.warning("tts_consumer: failed to read done log: %s", exc)
    return done


def _record_done(done_path: pathlib.Path, record_id: str, status: str, *, error: str | None = None) -> None:
    """Append a completion marker so we never re-speak on restart."""
    payload = {
        "id": record_id,
        "status": status,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if error:
        payload["error"] = error
    line = json.dumps(payload, ensure_ascii=True) + "\n"
    try:
        with done_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:  # pragma: no cover — defensive
        logger.warning("tts_consumer: failed to append done record: %s", exc)


def _parse_new_records(
    queue_path: pathlib.Path,
    offset: int,
    done_ids: set[str],
) -> tuple[list[dict], int]:
    """Read any new bytes since *offset* and return (records, new_offset).

    The queue is recreated if it was deleted out from under us (e.g. a
    log rotation) — in that case we start from zero.
    """
    if not queue_path.exists():
        return [], 0

    try:
        size = queue_path.stat().st_size
    except OSError:
        return [], offset

    if size < offset:
        # File was truncated/rotated. Reread from start.
        logger.info("tts_consumer: queue shrank (%d -> %d); rereading", offset, size)
        offset = 0

    if size == offset:
        return [], offset

    records: list[dict] = []
    try:
        with queue_path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read(size - offset)
            new_offset = fh.tell()
    except OSError as exc:  # pragma: no cover — defensive
        logger.warning("tts_consumer: failed to read queue: %s", exc)
        return [], offset

    # Only advance past the last full line — a producer mid-write would
    # leave a trailing partial. The next poll will pick it up.
    if not chunk.endswith(b"\n"):
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return [], offset  # no complete line yet
        new_offset = offset + last_nl + 1
        chunk = chunk[: last_nl + 1]

    for raw in chunk.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if len(raw) > _DEFAULT_MAX_LINE_BYTES:
            logger.warning("tts_consumer: dropping oversized line (%d bytes)", len(raw))
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("tts_consumer: malformed line skipped: %s", exc)
            continue
        rid = rec.get("id")
        if not isinstance(rid, str) or not rid:
            logger.warning("tts_consumer: record missing id; skipping: %r", rec)
            continue
        if rid in done_ids:
            continue
        records.append(rec)

    return records, new_offset


def _sort_by_priority(records: Iterable[dict]) -> list[dict]:
    """Higher priority first; stable for equal priorities (FIFO fallback)."""
    indexed = list(enumerate(records))
    indexed.sort(key=lambda pair: (-int(pair[1].get("priority") or 0), pair[0]))
    return [rec for _, rec in indexed]


def _speak(record: dict) -> tuple[str, str | None]:
    """Invoke Hope's TTS. Returns (status, error_message_or_None)."""
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return "error", "empty text"

    # ``hope.audio.say`` reads HOPE_VOICE at import time, so per-record
    # voice overrides need a scoped env swap. Keep this tight.
    voice = record.get("voice")
    prior_voice = None
    try:
        if voice:
            prior_voice = os.environ.get("HOPE_VOICE")
            os.environ["HOPE_VOICE"] = str(voice)
            # Reimport path is heavy; just poke the module global the
            # ``say_sync`` function closes over.
            import hope.audio.say as _say_mod
            _say_mod._DEFAULT_VOICE = str(voice)  # noqa: SLF001 — controlled override

        say_sync(text)
        return "spoken", None
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("tts_consumer: say_sync raised: %s", exc)
        return "error", str(exc)
    finally:
        if voice:
            import hope.audio.say as _say_mod
            _say_mod._DEFAULT_VOICE = prior_voice or os.environ.get("HOPE_VOICE", "Samantha")
            if prior_voice is None:
                os.environ.pop("HOPE_VOICE", None)
            else:
                os.environ["HOPE_VOICE"] = prior_voice


def run(
    queue_path: pathlib.Path = _QUEUE_PATH,
    done_path: pathlib.Path = _DONE_PATH,
    poll_sec: float = _DEFAULT_POLL_SEC,
) -> int:
    """Main loop. Returns 0 on clean shutdown."""
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.touch(exist_ok=True)
    done_path.touch(exist_ok=True)

    done_ids = _load_done_ids(done_path)
    offset = 0  # always replay from zero so crash-recovery is honest

    logger.info(
        "tts_consumer: starting queue=%s done=%s poll=%.2fs already_done=%d",
        queue_path,
        done_path,
        poll_sec,
        len(done_ids),
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while not _shutdown_requested:
        records, offset = _parse_new_records(queue_path, offset, done_ids)
        if records:
            for rec in _sort_by_priority(records):
                if _shutdown_requested:
                    break
                rid = rec["id"]
                if rid in done_ids:
                    continue
                logger.info("tts_consumer: speaking id=%s len=%d", rid, len(rec.get("text", "")))
                status, err = _speak(rec)
                _record_done(done_path, rid, status, error=err)
                done_ids.add(rid)
        else:
            time.sleep(poll_sec)

    logger.info("tts_consumer: clean shutdown")
    return 0


def _cli() -> int:
    parser = argparse.ArgumentParser(
        prog="hope-voice-bridge",
        description="Hope voice-out consumer: drains the TTS jsonl queue.",
    )
    parser.add_argument(
        "--queue",
        default=str(_QUEUE_PATH),
        help="Path to the pending queue jsonl (default: %(default)s)",
    )
    parser.add_argument(
        "--done",
        default=str(_DONE_PATH),
        help="Path to the completion log jsonl (default: %(default)s)",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=_DEFAULT_POLL_SEC,
        help="Poll interval in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("HOPE_VOICE_BRIDGE_LOG_LEVEL", "INFO"),
        help="Python logging level (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    return run(
        queue_path=pathlib.Path(args.queue),
        done_path=pathlib.Path(args.done),
        poll_sec=args.poll,
    )


if __name__ == "__main__":
    sys.exit(_cli())
