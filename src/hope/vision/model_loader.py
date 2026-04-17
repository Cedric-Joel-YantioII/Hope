"""Lazy MLX loader for Gemma 4 E4B with idle-timeout unloading.

The loader is the single place where MLX weights live in memory. It keeps
the model resident between consecutive requests (warm path) and unloads it
after ``idle_timeout_sec`` of inactivity so the ~2.5-3 GB of unified memory
can be reclaimed by the rest of Hope — important on an 8 GB Apple Silicon
machine where the Claude Code brain also competes for RAM.

Thread-safety
-------------
- ``acquire()`` and ``touch()`` are protected by a re-entrant lock so that
  concurrent ``VISION_REQUEST`` events do not race on first-load.
- The idle watcher is a single daemon thread that wakes once per second,
  checks ``last_used``, and calls ``unload()`` once the idle deadline has
  elapsed. The watcher itself acquires the same lock before unloading so it
  cannot tear down the model mid-inference.
- ``acquire()`` bumps ``last_used`` *before* returning, and the caller is
  expected to ``touch()`` again when the inference finishes; this means a
  long-running generation cannot be unloaded from underneath itself.

Backends
--------
We try ``mlx_vlm`` first — it is the MLX vision-language-model lib and
supports Gemma 4 natively (any-to-any models on mlx-community). If the
user forces ``backend = "mlx_lm"`` in config, we fall back to ``mlx_lm``
for text-only use (images are ignored with a warning). Import happens
inside ``_load()`` so merely importing this module never pulls in MLX.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


class VisionLoadError(RuntimeError):
    """Raised when the MLX vision stack cannot be imported or the model
    weights cannot be resolved."""


@dataclass(slots=True)
class LoadedModel:
    """Container for a loaded MLX vision model + its processor/config.

    The shape mirrors ``mlx_vlm.load()`` return value: a tuple of
    ``(model, processor)`` plus the raw model config. We wrap it so the
    call sites never reach into MLX types directly.
    """

    model: Any
    processor: Any
    config: Any
    backend: str  # "mlx_vlm" | "mlx_lm"


class MlxVisionLoader:
    """Lazy MLX loader with reference-counted idle unload.

    Typical flow::

        loader = MlxVisionLoader(repo="mlx-community/gemma-4-e4b-it-4bit")
        with loader.acquire() as lm:
            # lm.model, lm.processor — generate here
            ...
        # loader auto-unloads after idle_timeout_sec of inactivity
    """

    def __init__(
        self,
        *,
        repo: str = "mlx-community/gemma-4-e4b-it-4bit",
        idle_timeout_sec: int = 120,
        backend: str = "mlx_vlm",
    ) -> None:
        self._repo = repo
        self._idle_timeout = max(5, int(idle_timeout_sec))
        self._backend_pref = backend
        self._lock = threading.RLock()
        self._lm: Optional[LoadedModel] = None
        self._last_used: float = 0.0
        self._in_flight: int = 0
        self._watcher: Optional[threading.Thread] = None
        self._stop_watcher = threading.Event()

    # -- public API ---------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._lm is not None

    @property
    def repo(self) -> str:
        return self._repo

    def touch(self) -> None:
        """Mark the model as 'used right now' (restarts the idle timer)."""
        with self._lock:
            self._last_used = time.monotonic()

    def acquire(self) -> "_LoaderHandle":
        """Load (if needed) and return a handle exposing model/processor.

        The handle is a context manager that increments an in-flight counter
        on enter and decrements on exit — preventing the idle watcher from
        unloading weights mid-inference.
        """
        with self._lock:
            if self._lm is None:
                self._lm = self._load()
                self._start_watcher_locked()
            self._in_flight += 1
            self._last_used = time.monotonic()
            return _LoaderHandle(self, self._lm)

    def unload(self) -> None:
        """Drop the loaded weights. Safe to call when nothing is loaded."""
        with self._lock:
            if self._in_flight > 0:
                # A request is mid-flight; skip this cycle, watcher retries.
                return
            if self._lm is None:
                return
            logger.info("vision: unloading %s (idle > %ds)", self._repo, self._idle_timeout)
            self._lm = None
            # Best-effort MLX memory reclaim — non-fatal if unavailable.
            try:
                import mlx.core as mx  # type: ignore

                mx.clear_cache()
            except Exception:  # pragma: no cover — depends on mlx version
                pass

    def shutdown(self) -> None:
        """Stop the watcher thread and unload. Call on process exit."""
        self._stop_watcher.set()
        watcher = self._watcher
        self._watcher = None
        if watcher is not None and watcher.is_alive():
            watcher.join(timeout=2.0)
        with self._lock:
            # Force-drop even if in_flight (process exit).
            self._lm = None

    # -- internals ----------------------------------------------------------

    def _load(self) -> LoadedModel:
        """Import MLX and pull weights. Runs under ``self._lock``."""
        t0 = time.monotonic()
        backend = self._backend_pref
        try:
            if backend == "mlx_vlm":
                from mlx_vlm import load as vlm_load  # type: ignore
                from mlx_vlm.utils import load_config  # type: ignore

                model, processor = vlm_load(self._repo)
                config = load_config(self._repo)
                logger.info(
                    "vision: loaded %s via mlx-vlm in %.2fs",
                    self._repo,
                    time.monotonic() - t0,
                )
                return LoadedModel(model=model, processor=processor, config=config, backend="mlx_vlm")

            if backend == "mlx_lm":
                from mlx_lm import load as lm_load  # type: ignore

                model, tokenizer = lm_load(self._repo)
                logger.warning(
                    "vision: loaded %s via mlx-lm (TEXT ONLY — images ignored)",
                    self._repo,
                )
                return LoadedModel(
                    model=model, processor=tokenizer, config=None, backend="mlx_lm"
                )

            raise VisionLoadError(f"unknown vision backend: {backend!r}")
        except ImportError as exc:
            raise VisionLoadError(
                f"MLX vision stack not installed. Install with: "
                f"`pip install 'hope[vision]'` (Darwin arm64 only). "
                f"Original error: {exc}"
            ) from exc
        except Exception as exc:
            raise VisionLoadError(
                f"failed to load {self._repo} via {backend}: {exc}"
            ) from exc

    def _start_watcher_locked(self) -> None:
        """Spawn the idle-unload watcher. Caller holds ``self._lock``."""
        if self._watcher is not None and self._watcher.is_alive():
            return
        self._stop_watcher.clear()
        t = threading.Thread(
            target=self._watch_idle, name="hope-vision-idle", daemon=True
        )
        t.start()
        self._watcher = t

    def _watch_idle(self) -> None:
        while not self._stop_watcher.wait(1.0):
            with self._lock:
                if self._lm is None:
                    # Nothing to unload — watcher can exit; acquire() will
                    # restart it on next load.
                    self._watcher = None
                    return
                if self._in_flight > 0:
                    continue
                idle = time.monotonic() - self._last_used
                if idle < self._idle_timeout:
                    continue
            # Outside the lock so unload() can re-acquire safely.
            self.unload()

    # Release is called by the handle on context exit.
    def _release(self) -> None:
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._last_used = time.monotonic()


class _LoaderHandle:
    """Context-manager handle returned by ``MlxVisionLoader.acquire()``."""

    __slots__ = ("_loader", "model", "processor", "config", "backend")

    def __init__(self, loader: MlxVisionLoader, lm: LoadedModel) -> None:
        self._loader = loader
        self.model = lm.model
        self.processor = lm.processor
        self.config = lm.config
        self.backend = lm.backend

    def __enter__(self) -> "_LoaderHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._loader._release()


__all__ = ["LoadedModel", "MlxVisionLoader", "VisionLoadError"]
