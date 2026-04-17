"""Gemma 4 E4B vision service — direct API + EventBus handler.

Two ways to use this module:

1. **Direct** — instantiate :class:`GemmaVision` and call ``describe()``
   for synchronous screen descriptions::

        vision = GemmaVision()
        text = vision.describe(Path("screenshot.png"))

2. **Event-driven** — call ``bind_to_bus()`` once at startup; the service
   then subscribes to ``VISION_REQUEST`` events and publishes matching
   ``VISION_RESULT`` events. Every result is correlated by ``request_id``
   so callers can await their specific response.

The underlying weights are held by :class:`MlxVisionLoader`, which
lazy-loads on first use and unloads after ``idle_timeout_sec`` of
inactivity. The model itself is ~2.5-3 GB resident — do *not* pre-load.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from hope.core.config import VisionConfig, load_config
from hope.core.events import Event, EventBus, EventType, get_event_bus
from hope.vision.model_loader import MlxVisionLoader, VisionLoadError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------


class GemmaVision:
    """Event-triggered on-device vision via Gemma 4 E4B + MLX.

    Parameters
    ----------
    config:
        Optional :class:`VisionConfig`. If omitted, pulled from ``load_config()``.
    loader:
        Inject a pre-built loader (mostly useful for tests). If omitted, one
        is constructed from ``config``.
    bus:
        Optional :class:`EventBus`. Defaults to the process-wide singleton.
    """

    def __init__(
        self,
        config: Optional[VisionConfig] = None,
        *,
        loader: Optional[MlxVisionLoader] = None,
        bus: Optional[EventBus] = None,
    ) -> None:
        self._config = config or load_config().vision
        self._bus = bus or get_event_bus()
        self._loader = loader or MlxVisionLoader(
            repo=self._config.model_repo,
            idle_timeout_sec=self._config.idle_timeout_sec,
            backend=self._config.backend,
        )
        self._subscribed = False
        self._sub_lock = threading.Lock()

    # -- public properties --------------------------------------------------

    @property
    def loader(self) -> MlxVisionLoader:
        return self._loader

    @property
    def is_loaded(self) -> bool:
        return self._loader.is_loaded

    # -- direct API ---------------------------------------------------------

    def describe(
        self,
        image: Path,
        prompt: str = (
            "Describe this screen precisely, including visible UI elements and text."
        ),
        *,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Synchronously describe a single image and return the text."""
        result = self._run(
            image_paths=[Path(image)],
            prompt=prompt,
            max_tokens=max_tokens or self._config.max_tokens,
        )
        if result.error:
            raise RuntimeError(result.error)
        return result.text

    # -- event handler ------------------------------------------------------

    def bind_to_bus(self) -> None:
        """Subscribe to ``VISION_REQUEST`` events on the shared bus.

        Safe to call more than once — subsequent calls are no-ops.
        """
        with self._sub_lock:
            if self._subscribed:
                return
            self._bus.subscribe(EventType.VISION_REQUEST, self._on_request)
            self._subscribed = True
            logger.info(
                "vision: bound to EventBus (repo=%s, idle_timeout=%ds)",
                self._config.model_repo,
                self._config.idle_timeout_sec,
            )

    def unbind_from_bus(self) -> None:
        with self._sub_lock:
            if not self._subscribed:
                return
            self._bus.unsubscribe(EventType.VISION_REQUEST, self._on_request)
            self._subscribed = False

    def shutdown(self) -> None:
        """Unbind from bus and release MLX weights."""
        self.unbind_from_bus()
        self._loader.shutdown()

    # -- internals ----------------------------------------------------------

    def _on_request(self, event: Event) -> None:
        """EventBus callback — runs in the publisher's thread."""
        data = event.data or {}
        request_id = str(data.get("request_id") or uuid.uuid4())
        raw_paths = data.get("image_paths") or []
        prompt = str(data.get("prompt") or self._config.default_prompt)
        max_tokens = int(data.get("max_tokens") or self._config.max_tokens)

        try:
            image_paths = [Path(p) for p in raw_paths]
        except TypeError:
            self._publish_error(request_id, f"invalid image_paths: {raw_paths!r}")
            return

        result = self._run(
            image_paths=image_paths,
            prompt=prompt,
            max_tokens=max_tokens,
            request_id=request_id,
        )
        self._bus.publish(
            EventType.VISION_RESULT,
            {
                "request_id": result.request_id,
                "text": result.text,
                "latency_ms": result.latency_ms,
                "tokens": result.tokens,
                **({"error": result.error} if result.error else {}),
            },
        )

    def _publish_error(self, request_id: str, message: str) -> None:
        logger.error("vision request %s failed: %s", request_id, message)
        self._bus.publish(
            EventType.VISION_RESULT,
            {
                "request_id": request_id,
                "text": "",
                "latency_ms": 0.0,
                "tokens": 0,
                "error": message,
            },
        )

    def _run(
        self,
        *,
        image_paths: Sequence[Path],
        prompt: str,
        max_tokens: int,
        request_id: Optional[str] = None,
    ) -> "_RunResult":
        rid = request_id or str(uuid.uuid4())
        t0 = time.monotonic()

        # Validate inputs up front — cheap, and avoids loading MLX for junk.
        for p in image_paths:
            if not p.exists():
                return _RunResult(
                    request_id=rid, text="", latency_ms=0.0, tokens=0,
                    error=f"image not found: {p}",
                )

        try:
            with self._loader.acquire() as handle:
                text, token_count = _generate(
                    handle=handle,
                    image_paths=image_paths,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=self._config.temperature,
                )
        except VisionLoadError as exc:
            return _RunResult(
                request_id=rid, text="", latency_ms=0.0, tokens=0, error=str(exc)
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("vision inference failed")
            return _RunResult(
                request_id=rid, text="", latency_ms=0.0, tokens=0,
                error=f"inference error: {exc}",
            )

        latency_ms = (time.monotonic() - t0) * 1000.0
        return _RunResult(
            request_id=rid, text=text, latency_ms=latency_ms, tokens=token_count,
            error=None,
        )


# ---------------------------------------------------------------------------
# Inference helpers — isolated so they can be monkey-patched in tests
# ---------------------------------------------------------------------------


class _RunResult:
    __slots__ = ("request_id", "text", "latency_ms", "tokens", "error")

    def __init__(
        self,
        *,
        request_id: str,
        text: str,
        latency_ms: float,
        tokens: int,
        error: Optional[str],
    ) -> None:
        self.request_id = request_id
        self.text = text
        self.latency_ms = latency_ms
        self.tokens = tokens
        self.error = error


def _generate(
    *,
    handle: Any,
    image_paths: Sequence[Path],
    prompt: str,
    max_tokens: int,
    temperature: float,
) -> tuple[str, int]:
    """Run one generation with the loaded model.

    Kept intentionally thin and at module scope so tests can monkey-patch
    it without having to spin up real MLX.
    """
    if handle.backend == "mlx_vlm":
        from mlx_vlm import generate as vlm_generate  # type: ignore
        from mlx_vlm.prompt_utils import apply_chat_template  # type: ignore

        str_paths = [str(p) for p in image_paths]
        formatted = apply_chat_template(
            handle.processor, handle.config, prompt, num_images=len(str_paths)
        )
        out = vlm_generate(
            handle.model,
            handle.processor,
            formatted,
            str_paths,
            max_tokens=max_tokens,
            temperature=temperature,
            verbose=False,
        )
        # mlx-vlm returns either a string or a GenerationResult with .text
        text = getattr(out, "text", out) if not isinstance(out, str) else out
        tokens = getattr(out, "generation_tokens", None)
        if tokens is None:
            tokens = max(1, len(text) // 4)  # rough fallback
        return str(text).strip(), int(tokens)

    if handle.backend == "mlx_lm":
        from mlx_lm import generate as lm_generate  # type: ignore

        if image_paths:
            logger.warning(
                "mlx_lm backend ignores %d image(s) — text-only fallback",
                len(image_paths),
            )
        text = lm_generate(
            handle.model,
            handle.processor,
            prompt=prompt,
            max_tokens=max_tokens,
            temp=temperature,
            verbose=False,
        )
        return str(text).strip(), max(1, len(text) // 4)

    raise VisionLoadError(f"unsupported backend: {handle.backend!r}")


def _iter_image_paths(paths: Iterable[Any]) -> List[Path]:
    """Narrow helper used by callers that want the same normalisation."""
    return [Path(p) for p in paths]


__all__ = ["GemmaVision"]
