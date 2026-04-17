"""In-memory registry of live Hope specialist panes.

The :class:`TmuxOrchestrator` uses this to track every pane it owns —
the persistent ``hope-main`` brain plus the ephemeral specialists Hope
spawns on demand. Persistence (durable audit + replay) lives in the
``tmux_panes`` SQLite table; this module is the hot-path lookup table
used for routing messages, enforcing capacity, and iterating subscribers.

Every entry captures the pane id, its role, the tmux target the
orchestrator can address with ``tmux send-keys``, the per-pane FIFO
used for framed request/response IO, when the pane was spawned, the set
of pub/sub topics it is subscribed to, whether it is ephemeral, and an
optional parent pane (e.g. for specialists chained off other specialists).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set


@dataclass(slots=True)
class PaneEntry:
    """A single live pane tracked by the orchestrator."""

    pane_id: str
    role: str
    tmux_target: str
    fifo_path: str
    spawned_at: float
    subscribed_topics: Set[str] = field(default_factory=set)
    is_ephemeral: bool = True
    parent_pane: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "pane_id": self.pane_id,
            "role": self.role,
            "tmux_target": self.tmux_target,
            "fifo_path": self.fifo_path,
            "spawned_at": self.spawned_at,
            "subscribed_topics": sorted(self.subscribed_topics),
            "is_ephemeral": self.is_ephemeral,
            "parent_pane": self.parent_pane,
        }


class SpecialistRegistry:
    """Thread-safe registry of live panes.

    Read-heavy — ``TmuxOrchestrator.send_message`` iterates subscribers on
    every pub event — so we back it with a plain dict guarded by an RLock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._panes: Dict[str, PaneEntry] = {}

    # ── registration ─────────────────────────────────────────────────

    def register(
        self,
        *,
        pane_id: str,
        role: str,
        tmux_target: str,
        fifo_path: str,
        is_ephemeral: bool = True,
        parent_pane: Optional[str] = None,
        topics: Optional[Iterable[str]] = None,
    ) -> PaneEntry:
        """Add a pane to the registry.

        Raises :class:`ValueError` if *pane_id* is already registered.
        """
        with self._lock:
            if pane_id in self._panes:
                raise ValueError(f"pane '{pane_id}' already registered")
            entry = PaneEntry(
                pane_id=pane_id,
                role=role,
                tmux_target=tmux_target,
                fifo_path=fifo_path,
                spawned_at=time.time(),
                is_ephemeral=is_ephemeral,
                parent_pane=parent_pane,
                subscribed_topics=set(topics or []),
            )
            self._panes[pane_id] = entry
            return entry

    def deregister(self, pane_id: str) -> Optional[PaneEntry]:
        """Remove a pane from the registry. Idempotent."""
        with self._lock:
            return self._panes.pop(pane_id, None)

    # ── subscriptions ────────────────────────────────────────────────

    def subscribe(self, pane_id: str, topics: Iterable[str]) -> None:
        """Add *topics* to the pane's subscription set."""
        with self._lock:
            entry = self._panes.get(pane_id)
            if entry is None:
                raise KeyError(pane_id)
            entry.subscribed_topics.update(topics)

    def unsubscribe(self, pane_id: str, topics: Iterable[str]) -> None:
        """Remove *topics* from the pane's subscription set. Idempotent."""
        with self._lock:
            entry = self._panes.get(pane_id)
            if entry is None:
                return
            entry.subscribed_topics.difference_update(topics)

    def subscribers_for(self, topic: str) -> List[PaneEntry]:
        """Return every live pane subscribed to *topic*."""
        with self._lock:
            return [e for e in self._panes.values() if topic in e.subscribed_topics]

    # ── lookup ───────────────────────────────────────────────────────

    def get(self, pane_id: str) -> Optional[PaneEntry]:
        with self._lock:
            return self._panes.get(pane_id)

    def all(self) -> List[PaneEntry]:
        """Snapshot of every tracked pane."""
        with self._lock:
            return list(self._panes.values())

    def specialists(self) -> List[PaneEntry]:
        """Only the ephemeral specialist panes (excludes hope-main)."""
        with self._lock:
            return [e for e in self._panes.values() if e.is_ephemeral]

    def by_role(self, role: str) -> List[PaneEntry]:
        with self._lock:
            return [e for e in self._panes.values() if e.role == role]

    # ── capacity helpers ─────────────────────────────────────────────

    def specialist_count(self) -> int:
        return len(self.specialists())

    def __contains__(self, pane_id: object) -> bool:
        with self._lock:
            return pane_id in self._panes

    def __len__(self) -> int:
        with self._lock:
            return len(self._panes)


__all__ = ["PaneEntry", "SpecialistRegistry"]
