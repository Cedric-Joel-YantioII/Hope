"""Wake-up subsystem: clap + spoken-phrase detectors publishing WAKE_TRIGGER.

Public surface (stable, bound by the daemon/CLI handshake contract):

* :class:`WakeMonitor` — coordinator with ``.start()`` / ``.stop()`` / ``.is_monitoring``.
* :class:`WakeConfig` — re-exported from :mod:`hope.core.config` for convenience.
* :class:`WakeSource` — string enum identifying the origin of a WAKE_TRIGGER.

See :data:`hope.core.events.EventType.WAKE_TRIGGER` for the payload shape.
"""

from __future__ import annotations

from enum import Enum

from hope.core.config import WakeConfig

from .clap_detector import ClapDetector, ClapDetectorConfig
from .phrase_matcher import PhraseMatcher
from .wake_monitor import WakeMonitor


class WakeSource(str, Enum):
    """Valid values for the ``source`` field in a WAKE_TRIGGER payload."""

    VOICE = "voice"
    CLAP = "clap"
    # The daemon/CLI may emit ``manual`` via ``hope wake``; the wakeword
    # subsystem itself never emits this value, but we expose it so callers
    # can type-check a single enum.
    MANUAL = "manual"


__all__ = [
    "ClapDetector",
    "ClapDetectorConfig",
    "PhraseMatcher",
    "WakeConfig",
    "WakeMonitor",
    "WakeSource",
]
