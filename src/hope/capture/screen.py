"""On-demand screen capture + frame extractor for Hope.

The brain (persistent Claude Code CLI) decides when Hope needs to see the
screen. It publishes a ``SCREEN_CAPTURE_START`` event on the shared
:class:`~hope.core.events.EventBus` to begin a session and a
``SCREEN_CAPTURE_STOP`` event to end it. While a session is active a
dedicated worker thread samples the display at ``fps`` (default 2), writes
each frame to ``~/.hope/captures/<session_id>/frame_NNNNNN.png`` and emits
a ``SCREEN_FRAME`` event per frame so downstream vision modules can pick
them up without blocking the capture loop.

Backends (in preference order):

1. ``mss`` — zero-dep, fast, cross-platform.
2. ``screencapture -x`` — macOS CLI fallback.

Only the capture and event plumbing lives here: no vision, no orchestrator,
no microphone. On macOS the user must grant **Screen Recording** permission
to the terminal hosting Hope (System Settings → Privacy & Security → Screen
Recording) — if denied, :class:`ScreenCapture.start` raises
:class:`ScreenRecordingPermissionError` with guidance.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hope.core.events import Event, EventBus, EventType, get_event_bus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

#: Bounding box in screen pixels: ``(left, top, width, height)``.
BBox = Tuple[int, int, int, int]


@dataclass(slots=True)
class CaptureSummary:
    """Returned from :meth:`ScreenCapture.stop`."""

    session_id: str
    frames_captured: int
    frames_dropped: int
    duration_s: float
    output_dir: Path
    last_frame_path: Optional[Path] = None
    avg_latency_ms: float = 0.0
    backend: str = "mss"


@dataclass(slots=True)
class _Session:
    session_id: str
    fps: int
    display: int
    region: Optional[BBox]
    output_dir: Path
    started_at: float
    thread: threading.Thread
    stop_event: threading.Event
    frames_captured: int = 0
    frames_dropped: int = 0
    last_frame_path: Optional[Path] = None
    latencies_ms: List[float] = field(default_factory=list)
    backend: str = "mss"


class ScreenRecordingPermissionError(RuntimeError):
    """Raised when macOS refuses screen capture due to missing permission."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ROOT = Path.home() / ".hope" / "captures"
PRUNE_AFTER_SECONDS = 24 * 60 * 60  # 24h
PNG_COMPRESS_LEVEL = 1  # fast, ~minimal CPU — not tuned for size
_MAX_LATENCY_SAMPLES = 240  # cap memory for long sessions (~2min @ 2fps)


# ---------------------------------------------------------------------------
# ScreenCapture
# ---------------------------------------------------------------------------


class ScreenCapture:
    """On-demand screen recorder.

    Supports multiple simultaneous sessions keyed by ``session_id`` (rare but
    possible, e.g. primary + external display).

    Example
    -------
    >>> cap = ScreenCapture()
    >>> cap.start("debug-1", fps=2)
    >>> time.sleep(1.0)
    >>> summary = cap.stop("debug-1")
    >>> summary.frames_captured
    2
    """

    def __init__(
        self,
        *,
        root_dir: Optional[Path] = None,
        bus: Optional[EventBus] = None,
        auto_prune: bool = True,
    ) -> None:
        self._root = Path(root_dir) if root_dir else DEFAULT_ROOT
        self._root.mkdir(parents=True, exist_ok=True)
        self._bus = bus or get_event_bus()
        self._sessions: Dict[str, _Session] = {}
        self._lock = threading.Lock()
        # Subscribe so the brain can drive capture purely via events.
        self._bus.subscribe(EventType.SCREEN_CAPTURE_START, self._on_start_event)
        self._bus.subscribe(EventType.SCREEN_CAPTURE_STOP, self._on_stop_event)
        if auto_prune:
            try:
                self.prune_old_sessions()
            except Exception as exc:  # pragma: no cover — never fatal
                logger.warning("Screen capture: prune failed: %s", exc)

    # ------------------------------------------------------------------ API

    def list_displays(self) -> List[Dict[str, int]]:
        """Return metadata about available displays.

        Each entry mirrors ``mss``'s monitor dict: ``{left, top, width,
        height}`` with the 0-index being the *virtual* union of all
        monitors. If ``mss`` isn't importable, returns a one-element list
        describing the primary display as unknown-sized.
        """
        try:
            import mss  # type: ignore

            with mss.mss() as sct:
                return [dict(m) for m in sct.monitors]
        except Exception:
            return [{"left": 0, "top": 0, "width": 0, "height": 0}]

    def start(
        self,
        session_id: str,
        fps: int = 2,
        display: int = 0,
        region: Optional[BBox] = None,
    ) -> None:
        """Start a capture session.

        Parameters
        ----------
        session_id: Unique id used for the output directory and event payloads.
        fps: Target frames per second. Default 2; >10 is discouraged.
        display: ``mss`` monitor index. 0 = primary on single-display setups
            (mss treats index 0 as the virtual super-monitor; we map 0 → 1
            when multiple monitors exist so "default" means primary).
        region: Optional ``(left, top, width, height)`` sub-region.

        Raises
        ------
        ValueError: if ``fps`` is non-positive or the session is already running.
        ScreenRecordingPermissionError: on macOS when the host process has
            not been granted the Screen Recording permission.
        """
        if fps <= 0:
            raise ValueError(f"fps must be > 0, got {fps}")

        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"session '{session_id}' already active")

            # Verify permission eagerly — grabbing once here produces a
            # clear error instead of silently writing black frames on
            # macOS Sequoia when Screen Recording is denied.
            backend = _probe_backend()
            _verify_screen_recording_permission(backend)

            session_dir = self._root / session_id
            session_dir.mkdir(parents=True, exist_ok=True)

            stop_event = threading.Event()
            session = _Session(
                session_id=session_id,
                fps=fps,
                display=display,
                region=region,
                output_dir=session_dir,
                started_at=time.time(),
                thread=None,  # type: ignore[arg-type]
                stop_event=stop_event,
                backend=backend,
            )
            thread = threading.Thread(
                target=self._run_capture_loop,
                args=(session,),
                name=f"hope-capture-{session_id}",
                daemon=True,
            )
            session.thread = thread
            self._sessions[session_id] = session
            thread.start()
            logger.info(
                "Screen capture started (session=%s, fps=%d, backend=%s)",
                session_id,
                fps,
                backend,
            )

    def stop(self, session_id: str) -> CaptureSummary:
        """Stop a session and return a summary.

        Raises :class:`KeyError` if the session is unknown.
        """
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise KeyError(f"no active session '{session_id}'")

        session.stop_event.set()
        # Generous timeout: one frame-interval + slack.
        session.thread.join(timeout=max(2.0, 2.0 / session.fps))
        duration = time.time() - session.started_at
        avg_latency = (
            sum(session.latencies_ms) / len(session.latencies_ms)
            if session.latencies_ms
            else 0.0
        )
        summary = CaptureSummary(
            session_id=session_id,
            frames_captured=session.frames_captured,
            frames_dropped=session.frames_dropped,
            duration_s=duration,
            output_dir=session.output_dir,
            last_frame_path=session.last_frame_path,
            avg_latency_ms=avg_latency,
            backend=session.backend,
        )
        logger.info(
            "Screen capture stopped (session=%s, frames=%d, avg=%0.1fms)",
            session_id,
            summary.frames_captured,
            summary.avg_latency_ms,
        )
        return summary

    def is_active(self, session_id: str) -> bool:
        """Return True if ``session_id`` is currently capturing."""
        with self._lock:
            return session_id in self._sessions

    def stop_all(self) -> List[CaptureSummary]:
        """Stop every active session. Useful for shutdown hooks."""
        with self._lock:
            ids = list(self._sessions.keys())
        return [self.stop(sid) for sid in ids]

    def prune_old_sessions(self, *, max_age_s: int = PRUNE_AFTER_SECONDS) -> int:
        """Delete session dirs older than ``max_age_s`` seconds.

        Returns the number of directories removed.
        """
        if not self._root.exists():
            return 0
        now = time.time()
        removed = 0
        for child in self._root.iterdir():
            if not child.is_dir():
                continue
            try:
                age = now - child.stat().st_mtime
            except OSError:
                continue
            if age > max_age_s:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed

    # ------------------------------------------------------------- internals

    def _on_start_event(self, event: Event) -> None:
        data = event.data or {}
        session_id = str(data.get("session_id") or f"auto-{uuid.uuid4().hex[:8]}")
        if self.is_active(session_id):
            return
        try:
            self.start(
                session_id=session_id,
                fps=int(data.get("fps", 2)),
                display=int(data.get("display", 0)),
                region=data.get("region"),
            )
        except ScreenRecordingPermissionError as exc:
            logger.error("Screen capture permission denied: %s", exc)
            self._bus.publish(
                EventType.SECURITY_ALERT,
                {"source": "screen_capture", "reason": str(exc)},
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("Screen capture start failed: %s", exc)

    def _on_stop_event(self, event: Event) -> None:
        data = event.data or {}
        session_id = data.get("session_id")
        if not session_id:
            # Stop all sessions if no id specified.
            self.stop_all()
            return
        if self.is_active(str(session_id)):
            self.stop(str(session_id))

    # --------------------------------------------------------- capture loop

    def _run_capture_loop(self, session: _Session) -> None:
        interval = 1.0 / session.fps
        frame_idx = 0
        next_tick = time.monotonic()
        grabber = _build_grabber(session)
        try:
            while not session.stop_event.is_set():
                cycle_start = time.monotonic()
                try:
                    latency_ms = grabber.capture(
                        session.output_dir / f"frame_{frame_idx + 1:06d}.png"
                    )
                except ScreenRecordingPermissionError as exc:
                    logger.error(
                        "Screen capture halted (session=%s): %s",
                        session.session_id,
                        exc,
                    )
                    break
                except Exception as exc:
                    session.frames_dropped += 1
                    logger.warning(
                        "Frame capture failed (session=%s, frame=%d): %s",
                        session.session_id,
                        frame_idx + 1,
                        exc,
                    )
                else:
                    frame_idx += 1
                    session.frames_captured = frame_idx
                    frame_path = session.output_dir / f"frame_{frame_idx:06d}.png"
                    session.last_frame_path = frame_path
                    if len(session.latencies_ms) < _MAX_LATENCY_SAMPLES:
                        session.latencies_ms.append(latency_ms)
                    self._emit_frame_event(session, frame_idx, frame_path)

                # Steady cadence — drift-free sleep.
                next_tick += interval
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    # Use stop_event.wait so stop() is near-instant.
                    if session.stop_event.wait(timeout=sleep_for):
                        break
                else:
                    # Behind schedule; reset the clock to avoid burst catch-up.
                    session.frames_dropped += max(0, int(-sleep_for / interval))
                    next_tick = time.monotonic()
                _ = cycle_start  # kept for future profiling
        finally:
            try:
                grabber.close()
            except Exception:  # pragma: no cover
                pass

    def _emit_frame_event(
        self, session: _Session, frame_idx: int, path: Path
    ) -> None:
        try:
            self._bus.publish(
                EventType.SCREEN_FRAME,
                {
                    "session_id": session.session_id,
                    "frame_idx": frame_idx,
                    "path": str(path),
                    "timestamp": time.time(),
                },
            )
        except Exception as exc:  # pragma: no cover — never kill the loop
            logger.warning("SCREEN_FRAME publish failed: %s", exc)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def _probe_backend() -> str:
    """Return ``"mss"`` if importable, else ``"screencapture"`` on macOS."""
    try:
        import mss  # noqa: F401

        return "mss"
    except Exception:
        if sys.platform == "darwin" and shutil.which("screencapture"):
            return "screencapture"
        raise RuntimeError(
            "No screen capture backend available. Install mss: "
            "`uv sync --extra capture` (or `pip install mss pillow`)."
        )


def _build_grabber(session: _Session) -> "_FrameGrabber":
    if session.backend == "mss":
        return _MssGrabber(display=session.display, region=session.region)
    if session.backend == "screencapture":
        return _ScreencaptureGrabber(display=session.display, region=session.region)
    raise RuntimeError(f"Unknown backend: {session.backend}")


def _verify_screen_recording_permission(backend: str) -> None:
    """Attempt a 1-pixel grab so macOS surfaces its permission prompt.

    On Sequoia, a denied app receives a black/empty frame rather than an
    error, so we check pixel variance on a tiny region and raise a helpful
    error when the frame is clearly uniform black.
    """
    if platform.system() != "Darwin":
        return

    try:
        if backend == "mss":
            import mss  # type: ignore

            with mss.mss() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                probe = {
                    "left": mon["left"],
                    "top": mon["top"],
                    "width": min(32, mon["width"] or 32),
                    "height": min(32, mon["height"] or 32),
                }
                img = sct.grab(probe)
                pixels = bytes(img.rgb)
        else:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                subprocess.run(
                    ["screencapture", "-x", "-t", "png", tmp_path],
                    check=True,
                    capture_output=True,
                    timeout=5,
                )
                from PIL import Image  # type: ignore

                with Image.open(tmp_path) as im:
                    im = im.convert("RGB").resize((32, 32))
                    pixels = im.tobytes()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except subprocess.CalledProcessError as exc:
        raise ScreenRecordingPermissionError(
            "screencapture failed — likely Screen Recording permission denied. "
            "Grant access in System Settings → Privacy & Security → Screen "
            f"Recording, then restart Hope. ({exc})"
        ) from exc
    except Exception as exc:
        # Non-permission errors (e.g. missing mss) bubble up unchanged.
        raise

    # All-zero or near-uniform-black pixels ≈ permission denied on Sequoia.
    if pixels and len(set(pixels[:3072])) <= 2:
        raise ScreenRecordingPermissionError(
            "Screen Recording appears to be denied for this process. Open "
            "System Settings → Privacy & Security → Screen Recording, enable "
            "the terminal/app running Hope, then restart it. (Received a "
            "uniform/black probe frame.)"
        )


class _FrameGrabber:
    """Abstract one-shot grabber."""

    def capture(self, dest: Path) -> float:  # pragma: no cover — interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover — interface
        pass


class _MssGrabber(_FrameGrabber):
    """``mss`` + Pillow PNG writer. Fastest path on M2."""

    def __init__(self, display: int, region: Optional[BBox]) -> None:
        import mss  # type: ignore

        self._sct = mss.mss()
        monitors = self._sct.monitors
        # mss monitors[0] is the union of all screens. For "default", prefer
        # primary physical (index 1) when available.
        if region:
            left, top, width, height = region
            self._mon = {
                "left": left,
                "top": top,
                "width": width,
                "height": height,
            }
        else:
            idx = display
            if idx == 0 and len(monitors) > 1:
                idx = 1
            if idx >= len(monitors):
                raise ValueError(
                    f"display index {display} out of range "
                    f"(have {len(monitors) - 1} physical monitors)"
                )
            self._mon = monitors[idx]

    def capture(self, dest: Path) -> float:
        from PIL import Image  # type: ignore

        t0 = time.perf_counter()
        shot = self._sct.grab(self._mon)
        # ``mss`` gives BGRA; reorder to RGB for PIL without an extra copy.
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        img.save(dest, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
        return (time.perf_counter() - t0) * 1000.0

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:  # pragma: no cover
            pass


class _ScreencaptureGrabber(_FrameGrabber):
    """Fallback: shell out to macOS ``screencapture -x``."""

    def __init__(self, display: int, region: Optional[BBox]) -> None:
        self._display = display
        self._region = region
        if shutil.which("screencapture") is None:
            raise RuntimeError("`screencapture` CLI not found")

    def capture(self, dest: Path) -> float:
        t0 = time.perf_counter()
        cmd = ["screencapture", "-x", "-t", "png"]
        if self._display and self._display > 0:
            cmd += ["-D", str(self._display)]
        if self._region:
            left, top, width, height = self._region
            cmd += ["-R", f"{left},{top},{width},{height}"]
        cmd.append(str(dest))
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=5)
        except subprocess.CalledProcessError as exc:
            if b"not authorized" in (exc.stderr or b"").lower():
                raise ScreenRecordingPermissionError(
                    "Screen Recording permission denied. Enable it in "
                    "System Settings → Privacy & Security → Screen Recording."
                ) from exc
            raise
        return (time.perf_counter() - t0) * 1000.0


__all__ = [
    "BBox",
    "CaptureSummary",
    "ScreenCapture",
    "ScreenRecordingPermissionError",
]
