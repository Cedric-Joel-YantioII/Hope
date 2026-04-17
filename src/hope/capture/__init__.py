"""Capture subsystem — microphone, screen, and other I/O streams.

This package owns "always-on" capture primitives that feed the Hope runtime
via the :mod:`hope.core.events` bus. The first inhabitant is the microphone
capture loop in :mod:`hope.capture.mic`, which is consumed by the
``whisper-cpp`` STT backend.
"""

from hope.capture.mic import (
    MicCapture,
    MicFrame,
    VADConfig,
    VADGatedSegmenter,
)
from hope.capture.screen import (
    BBox,
    CaptureSummary,
    ScreenCapture,
    ScreenRecordingPermissionError,
)

__all__ = [
    "MicCapture",
    "MicFrame",
    "VADConfig",
    "VADGatedSegmenter",
    "BBox",
    "CaptureSummary",
    "ScreenCapture",
    "ScreenRecordingPermissionError",
]
