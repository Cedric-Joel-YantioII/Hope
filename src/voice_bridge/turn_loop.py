"""Hope voice-in bridge — transcript → ``claude -p`` → TTS-out queue.

Daemon that watches for finalized speech transcripts from Hope's STT
pipeline, spawns a ``claude`` CLI subprocess (inheriting Joel's Max
subscription auth — never an Anthropic API key), and appends the reply
to the shared voice-out JSONL queue consumed by the TTS bridge agent.

Design
------
- **Input**: tail ``~/.hope/daemon.log`` for ``[HEARD] '...'`` stderr
  markers the Hope daemon writes on every ``SPEECH_TRANSCRIPT`` event.
  This is zero-touch on Hope's existing daemon — we never have to wire
  into her EventBus singleton from another process. A secondary source
  is ``~/Documents/Github/Hope/.hope-io/stt-in.jsonl`` if Hope starts
  emitting a dedicated transcript queue in the future.
- **Debounce**: 800ms quiescence window per-turn. Whisper-cpp ships
  final-only transcripts so partials are rare, but if a burst of
  ``[HEARD]`` lines arrives within the window they are coalesced into
  one turn using the last line's text.
- **Brain**: ``claude -p "<transcript>" --output-format text`` — a
  one-shot, non-interactive spawn. ``--print`` mode inherits Joel's
  logged-in Max auth from the CLI's cached creds.
- **Output**: append a JSON record to
  ``~/Documents/Github/Hope/.hope-io/tts-out.jsonl``. The voice-out
  bridge tails that file and speaks each record in order.
- **Trace log**: every turn (success or failure) gets one line in
  ``~/Documents/Github/Hope/.hope-io/turns.jsonl`` with timing and
  error info — the ``self-report`` skill can query this later.

Lifecycle is managed by ``hope-talk`` (start/stop/status/tail). A
pidfile at ``~/Documents/Github/Hope/.hope-io/turn_loop.pid`` serializes
singleton ownership.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Optional

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

HOPE_ROOT = Path.home() / "Documents" / "Github" / "Hope"
HOPE_IO = HOPE_ROOT / ".hope-io"
TTS_OUT_QUEUE = HOPE_IO / "tts-out.jsonl"
TURNS_LOG = HOPE_IO / "turns.jsonl"
STT_IN_QUEUE = HOPE_IO / "stt-in.jsonl"  # future source, optional
PID_FILE = HOPE_IO / "turn_loop.pid"
LOG_FILE = HOPE_IO / "turn_loop.log"

DAEMON_LOG = Path.home() / ".hope" / "daemon.log"

CLAUDE_BIN = "/Users/joelc/.local/bin/claude"
# Absolute fallbacks so the daemon works regardless of PATH state.
_CLAUDE_CANDIDATES = (
    CLAUDE_BIN,
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)

DEBOUNCE_SECONDS = 0.8
CLAUDE_TIMEOUT_SECONDS = 60.0
ERROR_REPLY = "Sorry, I hit an error"

# `[HEARD] '...'` — the Hope daemon stderrs this on every finalized
# transcript (see HopeDaemon._on_speech_transcript in
# src/hope/daemon/core.py). We pattern-match it to avoid touching the
# daemon while still staying in lockstep with the event bus.
_RE_HEARD = re.compile(r"\[HEARD\]\s+(['\"])(.*)\1\s*$")

logger = logging.getLogger("hope.voice_bridge")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Turn:
    """One user-turn, from transcript to queued reply."""

    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    transcript: str = ""
    reply: str = ""
    error: Optional[str] = None
    heard_at: float = field(default_factory=time.time)
    dispatched_at: float = 0.0
    replied_at: float = 0.0
    latency_ms: int = 0
    exit_code: Optional[int] = None

    def to_log_record(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "timestamp": self.heard_at,
            "transcript": self.transcript,
            "reply": self.reply,
            "error": self.error,
            "exit_code": self.exit_code,
            "latency_ms": self.latency_ms,
            "dispatched_at": self.dispatched_at,
            "replied_at": self.replied_at,
        }


# ---------------------------------------------------------------------------
# Pidfile helpers
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def _read_pidfile() -> Optional[int]:
    try:
        txt = PID_FILE.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not txt:
        return None
    try:
        pid = int(txt)
    except ValueError:
        return None
    return pid if _pid_alive(pid) else None


def _write_pidfile() -> None:
    HOPE_IO.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def _clear_pidfile() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Transcript source — tails ~/.hope/daemon.log and extracts `[HEARD] '...'`
# ---------------------------------------------------------------------------


class DaemonLogTail:
    """Follow ``~/.hope/daemon.log`` and push transcript strings onto a queue.

    We open the file fresh on start (ignoring history) and tail it like
    ``tail -F`` — handling truncation and file rotation by re-opening when
    the inode changes or the read position is past EOF.
    """

    def __init__(self, path: Path, out_queue: "Queue[str]") -> None:
        self._path = path
        self._queue = out_queue
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="hope-daemon-log-tail", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _open(self):
        try:
            fh = self._path.open("r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None, None
        fh.seek(0, os.SEEK_END)
        try:
            ino = os.fstat(fh.fileno()).st_ino
        except OSError:
            ino = None
        return fh, ino

    def _run(self) -> None:
        fh, ino = self._open()
        while not self._stop.is_set():
            if fh is None:
                time.sleep(0.5)
                fh, ino = self._open()
                continue
            line = fh.readline()
            if not line:
                # EOF — check for truncation/rotation, then sleep briefly.
                try:
                    st = self._path.stat()
                    if ino is None or st.st_ino != ino or st.st_size < fh.tell():
                        fh.close()
                        fh, ino = self._open()
                        continue
                except FileNotFoundError:
                    fh.close()
                    fh, ino = None, None
                    continue
                time.sleep(0.1)
                continue
            m = _RE_HEARD.search(line)
            if m is None:
                continue
            text = m.group(2).strip()
            if text:
                self._queue.put(text)
        if fh is not None:
            fh.close()


class JsonlTail:
    """Follow an optional STT JSONL queue. Each record: ``{"text": "..."}``."""

    def __init__(self, path: Path, out_queue: "Queue[str]") -> None:
        self._path = path
        self._queue = out_queue
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="hope-stt-jsonl-tail", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        # Create the file if absent so tail doesn't spin.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch(exist_ok=True)
        except OSError:
            pass
        try:
            fh = self._path.open("r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return
        fh.seek(0, os.SEEK_END)
        while not self._stop.is_set():
            line = fh.readline()
            if not line:
                time.sleep(0.1)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = str(rec.get("text", "")).strip()
            if text:
                self._queue.put(text)
        fh.close()


# ---------------------------------------------------------------------------
# Debouncer
# ---------------------------------------------------------------------------


class Debouncer:
    """Coalesce rapid bursts of transcripts into a single turn.

    Hope's Whisper-cpp backend ships only finalized utterances, so this
    is mostly defensive — but if two ``[HEARD]`` lines land within
    ``DEBOUNCE_SECONDS`` we use the last one (typically a correction).
    """

    def __init__(self, window_seconds: float = DEBOUNCE_SECONDS) -> None:
        self._window = window_seconds
        self._lock = threading.Lock()
        self._pending: Optional[str] = None
        self._last_update: float = 0.0
        self._timer: Optional[threading.Timer] = None
        self._callback = None  # type: ignore[assignment]

    def bind(self, callback) -> None:
        self._callback = callback

    def push(self, text: str) -> None:
        with self._lock:
            self._pending = text
            self._last_update = time.time()
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._window, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            text = self._pending
            self._pending = None
            self._timer = None
        if text and self._callback is not None:
            try:
                self._callback(text)
            except Exception:
                logger.exception("debouncer callback failed")

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


# ---------------------------------------------------------------------------
# Claude subprocess
# ---------------------------------------------------------------------------


def _locate_claude() -> str:
    for candidate in _CLAUDE_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # Fall back to PATH resolution — may still work in the parent env.
    return "claude"


def invoke_claude(
    transcript: str,
    *,
    timeout: float = CLAUDE_TIMEOUT_SECONDS,
) -> tuple[str, int]:
    """Spawn ``claude -p <transcript>`` and return ``(stdout_text, exit_code)``.

    Uses ``--print`` (one-shot, inherits Max auth from the CLI's cached
    credentials) and ``--output-format text`` for easy parsing. Never
    raises on subprocess failure — captures the exit code instead.
    """
    cmd = [
        _locate_claude(),
        "-p",
        transcript,
        "--output-format",
        "text",
    ]
    try:
        proc = subprocess.run(  # noqa: S603 — CLI with known-good binary
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ("", 124)
    except FileNotFoundError:
        logger.error("claude CLI not found at %s", cmd[0])
        return ("", 127)
    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0 and not stdout:
        stderr = (proc.stderr or "").strip()
        logger.warning(
            "claude -p exited %d: %s", proc.returncode, stderr[:200]
        )
    return (stdout, proc.returncode)


# ---------------------------------------------------------------------------
# Output writer — append-only JSONL queues
# ---------------------------------------------------------------------------


_FILE_LOCK = threading.Lock()


def _append_jsonl(path: Path, record: dict) -> None:
    """Append a JSON record to *path* (used for our own turns.jsonl).

    The TTS-out queue is written via :func:`voice_bridge.append_speech_line`
    — that path uses ``fcntl.flock`` to coordinate with other producers
    like the ``hope-speak`` shim. This helper is for internal-only files
    where we're the sole writer.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _FILE_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass


def queue_tts(turn_id: str, text: str, *, is_error: bool = False) -> str:
    """Append a record to ``tts-out.jsonl`` for the voice-out bridge.

    Delegates to :func:`voice_bridge.append_speech_line` (the canonical
    producer helper built by the voice-out bridge agent). We catch empty
    text early so we never wake the consumer for nothing. The ``turn_id``
    is carried along in the returned uuid via logs so we can trace a
    spoken utterance back to its turn.
    """
    if not text:
        return ""
    # Import inside the function so the turn_loop module stays importable
    # in environments (e.g. test harness) where the sibling helper is
    # still being authored.
    from voice_bridge.hope_speak_append import append_speech_line

    priority = 10 if is_error else None
    try:
        return append_speech_line(text, priority=priority)
    except ValueError:
        # Empty-after-strip — nothing to speak.
        return ""


def log_turn(turn: Turn) -> None:
    _append_jsonl(TURNS_LOG, turn.to_log_record())


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TurnLoop:
    """Main daemon: transcript source → debouncer → claude → queues."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._queue: "Queue[str]" = Queue()
        self._debouncer = Debouncer()
        self._debouncer.bind(self._handle_transcript)
        self._log_tail = DaemonLogTail(DAEMON_LOG, self._queue)
        self._jsonl_tail = JsonlTail(STT_IN_QUEUE, self._queue)
        # Single-slot worker so two rapid turns don't spawn parallel
        # claude subprocesses fighting for stdout ordering.
        self._worker: Optional[threading.Thread] = None
        self._active_turns = 0
        self._total_turns = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        HOPE_IO.mkdir(parents=True, exist_ok=True)
        logger.info(
            "turn_loop starting (pid=%d, claude=%s)",
            os.getpid(),
            _locate_claude(),
        )
        self._log_tail.start()
        self._jsonl_tail.start()
        self._worker = threading.Thread(
            target=self._pump, name="hope-turn-pump", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        logger.info("turn_loop stopping")
        self._stop.set()
        self._debouncer.stop()
        self._log_tail.stop()
        self._jsonl_tail.stop()
        if self._worker is not None:
            self._worker.join(timeout=2.0)

    def run_forever(self) -> None:
        self.start()
        # Install graceful signal handlers (SIGTERM from hope-talk stop,
        # SIGINT from Ctrl-C in foreground mode).
        def _sig(_sig, _frm):
            self._stop.set()

        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)
        while not self._stop.is_set():
            time.sleep(0.5)
        self.stop()

    # -- pipeline stages ----------------------------------------------------

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                text = self._queue.get(timeout=0.25)
            except Empty:
                continue
            # Push into the debouncer — final delivery happens after the
            # quiescence window expires.
            self._debouncer.push(text)

    def _handle_transcript(self, text: str) -> None:
        """Called by the debouncer once a turn's transcript is final."""
        turn = Turn(transcript=text)
        turn.dispatched_at = time.time()
        self._active_turns += 1
        self._total_turns += 1
        logger.info(
            "turn %s dispatch: %r", turn.turn_id, text[:80]
        )
        try:
            reply, exit_code = invoke_claude(text)
            turn.exit_code = exit_code
            turn.replied_at = time.time()
            turn.latency_ms = int(
                (turn.replied_at - turn.dispatched_at) * 1000
            )
            if exit_code != 0 or not reply:
                turn.error = (
                    f"claude exited {exit_code}"
                    if exit_code != 0
                    else "empty reply"
                )
                logger.warning(
                    "turn %s error: %s", turn.turn_id, turn.error
                )
                speak_id = queue_tts(
                    turn.turn_id, ERROR_REPLY, is_error=True
                )
                turn.reply = ""
                logger.info(
                    "turn %s queued error (speak_id=%s)",
                    turn.turn_id,
                    speak_id,
                )
            else:
                turn.reply = reply
                speak_id = queue_tts(turn.turn_id, reply)
                logger.info(
                    "turn %s reply (%dms, speak_id=%s): %r",
                    turn.turn_id,
                    turn.latency_ms,
                    speak_id,
                    reply[:120],
                )
        except Exception as exc:  # pragma: no cover — defensive
            turn.error = f"{type(exc).__name__}: {exc}"[:200]
            logger.exception("turn %s crashed", turn.turn_id)
            queue_tts(turn.turn_id, ERROR_REPLY, is_error=True)
        finally:
            self._active_turns -= 1
            log_turn(turn)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _configure_logging(to_file: bool) -> None:
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    handlers: list[logging.Handler] = []
    if to_file:
        HOPE_IO.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_FILE))
    handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hope-turn-loop",
        description="Hope voice-in bridge daemon.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run in foreground (no pidfile, no log redirect).",
    )
    parser.add_argument(
        "--probe",
        metavar="TEXT",
        help="Inject a synthetic transcript, process one turn, then exit.",
    )
    args = parser.parse_args(argv)

    _configure_logging(to_file=not args.foreground)

    if args.probe is not None:
        loop = TurnLoop()
        loop._handle_transcript(args.probe)
        return 0

    existing = _read_pidfile()
    if existing is not None:
        print(
            f"turn_loop already running (pid={existing})",
            file=sys.stderr,
        )
        return 1

    if not args.foreground:
        _write_pidfile()
    try:
        loop = TurnLoop()
        loop.run_forever()
    finally:
        if not args.foreground:
            _clear_pidfile()
    return 0


if __name__ == "__main__":
    sys.exit(main())
