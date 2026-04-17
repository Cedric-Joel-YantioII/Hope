"""On-device vision for Hope — event-triggered Gemma 4 E4B via MLX.

This subpackage exposes a single public surface, :class:`GemmaVision`, plus
its lazy-loading helper :class:`MlxVisionLoader`. The model is NOT loaded at
startup; the first ``VISION_REQUEST`` event (or the first direct
``describe()`` call) triggers a lazy load. After ``idle_timeout_sec`` of
inactivity the weights are unloaded to reclaim ~2.5-3 GB of unified memory.
"""

from hope.vision.gemma_vision import GemmaVision
from hope.vision.model_loader import MlxVisionLoader, VisionLoadError

__all__ = ["GemmaVision", "MlxVisionLoader", "VisionLoadError"]
