"""Tests for ``hope.vision.gemma_vision``.

Two tiers:

* **Fast, always-on** — use a fake loader / monkey-patched ``_generate`` so
  the full event-driven control flow (lazy load, publish-result, idle
  unload) can run in <1s without MLX installed.

* **Slow / apple-only** — marked ``@pytest.mark.slow`` and
  ``@pytest.mark.apple``; boots the real ``mlx-vlm`` stack against a tiny
  PNG and asserts the model returns non-empty text. Skipped when MLX is
  not importable, so CI on Linux / no-MLX boxes stays green.
"""

from __future__ import annotations

import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Any, List

import pytest

from hope.core.config import VisionConfig
from hope.core.events import EventBus, EventType
from hope.vision import gemma_vision as gv
from hope.vision.gemma_vision import GemmaVision
from hope.vision.model_loader import MlxVisionLoader

# ---------------------------------------------------------------------------
# Tiny PNG fixture — 64x64 solid white, hand-rolled so we don't need Pillow
# to run the fast tests.
# ---------------------------------------------------------------------------


def _make_white_png(path: Path, size: int = 64) -> Path:
    """Write a minimal valid PNG (8-bit grayscale, solid white) to *path*."""
    width = height = size
    # Raw scanlines: filter byte 0 + row of 0xFF.
    raw = b"".join(b"\x00" + b"\xff" * width for _ in range(height))
    compressed = zlib.compress(raw, 9)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # grayscale
    png = signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(
        b"IEND", b""
    )
    path.write_bytes(png)
    return path


@pytest.fixture
def tiny_png(tmp_path: Path) -> Path:
    return _make_white_png(tmp_path / "white.png")


# ---------------------------------------------------------------------------
# Fake loader/handle so the fast tests never touch MLX
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(self) -> None:
        self.model = object()
        self.processor = object()
        self.config = object()
        self.backend = "mlx_vlm"

    def __enter__(self) -> "_FakeHandle":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeLoader:
    """Drop-in stand-in for :class:`MlxVisionLoader`."""

    def __init__(self) -> None:
        self._loaded = False
        self.acquire_count = 0
        self.unload_count = 0
        self.repo = "fake/gemma-4-e4b"

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def acquire(self) -> _FakeHandle:
        self._loaded = True
        self.acquire_count += 1
        return _FakeHandle()

    def unload(self) -> None:
        self._loaded = False
        self.unload_count += 1

    def shutdown(self) -> None:
        self.unload()

    def touch(self) -> None:  # pragma: no cover — symmetry with real loader
        pass


@pytest.fixture
def fake_loader() -> _FakeLoader:
    return _FakeLoader()


@pytest.fixture
def vision(
    fake_loader: _FakeLoader, monkeypatch: pytest.MonkeyPatch
) -> GemmaVision:
    # Stub the generate() call so no MLX import is needed.
    def _fake_generate(*, handle, image_paths, prompt, max_tokens, temperature):
        assert handle.backend == "mlx_vlm"
        assert len(image_paths) >= 1
        return f"a white square. prompt={prompt[:20]}", 7

    monkeypatch.setattr(gv, "_generate", _fake_generate)
    bus = EventBus(record_history=True)
    return GemmaVision(
        config=VisionConfig(idle_timeout_sec=5, max_tokens=16),
        loader=fake_loader,  # type: ignore[arg-type]
        bus=bus,
    )


# ---------------------------------------------------------------------------
# Fast tests — no MLX required
# ---------------------------------------------------------------------------


def test_describe_lazy_loads_and_returns_text(
    vision: GemmaVision, fake_loader: _FakeLoader, tiny_png: Path
) -> None:
    assert not fake_loader.is_loaded, "model must not be pre-loaded"
    text = vision.describe(tiny_png, prompt="What is this?")
    assert fake_loader.acquire_count == 1
    assert fake_loader.is_loaded
    assert text and "white" in text.lower()


def test_event_request_publishes_result_with_matching_request_id(
    vision: GemmaVision, tiny_png: Path
) -> None:
    vision.bind_to_bus()
    results: List[dict] = []
    vision._bus.subscribe(  # type: ignore[attr-defined]
        EventType.VISION_RESULT, lambda e: results.append(e.data)
    )

    vision._bus.publish(  # type: ignore[attr-defined]
        EventType.VISION_REQUEST,
        {
            "image_paths": [str(tiny_png)],
            "prompt": "describe",
            "max_tokens": 8,
            "request_id": "req-42",
        },
    )

    assert len(results) == 1
    payload = results[0]
    assert payload["request_id"] == "req-42"
    assert payload["text"]
    assert payload["tokens"] > 0
    assert payload["latency_ms"] >= 0.0
    assert "error" not in payload


def test_missing_image_returns_error_without_loading(
    fake_loader: _FakeLoader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reset to a pristine vision that never hits _generate
    def _should_not_run(**_: Any):  # pragma: no cover
        raise AssertionError("_generate must not run when image is missing")

    monkeypatch.setattr(gv, "_generate", _should_not_run)
    bus = EventBus(record_history=True)
    vision = GemmaVision(
        config=VisionConfig(idle_timeout_sec=5),
        loader=fake_loader,  # type: ignore[arg-type]
        bus=bus,
    )
    vision.bind_to_bus()

    captured: List[dict] = []
    bus.subscribe(EventType.VISION_RESULT, lambda e: captured.append(e.data))
    bus.publish(
        EventType.VISION_REQUEST,
        {
            "image_paths": [str(tmp_path / "does-not-exist.png")],
            "prompt": "hi",
            "max_tokens": 8,
            "request_id": "req-missing",
        },
    )

    assert len(captured) == 1
    assert captured[0]["request_id"] == "req-missing"
    assert captured[0]["error"].startswith("image not found")
    assert not fake_loader.is_loaded


def test_idle_unload_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # Use the real loader but a fake _load() so no MLX is imported.
    loader = MlxVisionLoader(repo="fake/model", idle_timeout_sec=5)

    from hope.vision.model_loader import LoadedModel

    fake_lm = LoadedModel(
        model=object(), processor=object(), config=object(), backend="mlx_vlm"
    )
    monkeypatch.setattr(loader, "_load", lambda: fake_lm)

    # Accelerate the clock by patching idle-check timeout to ~0s.
    loader._idle_timeout = 0  # type: ignore[attr-defined]

    with loader.acquire():
        assert loader.is_loaded
    # Watcher wakes once per second; give it headroom.
    deadline = time.monotonic() + 4.0
    while loader.is_loaded and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not loader.is_loaded, "loader should have unloaded after idle timeout"
    loader.shutdown()


# ---------------------------------------------------------------------------
# Slow integration test — requires real MLX + Gemma 4 weights
# ---------------------------------------------------------------------------


def _mlx_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:  # pragma: no cover — probe only
        import importlib

        importlib.import_module("mlx_vlm")
        return True
    except Exception:
        return False


@pytest.mark.slow
@pytest.mark.apple
@pytest.mark.skipif(not _mlx_available(), reason="mlx-vlm not installed")
def test_describe_real_gemma4_on_white_square(tmp_path: Path) -> None:
    png = _make_white_png(tmp_path / "white.png")
    vision = GemmaVision(
        config=VisionConfig(idle_timeout_sec=30, max_tokens=64, temperature=0.0)
    )
    text = vision.describe(png, prompt="Describe the image in one short sentence.")
    assert text.strip(), "model returned empty text"
    vision.shutdown()
