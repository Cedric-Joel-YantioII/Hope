"""Fuzzy spoken-phrase wake detector.

Subscribes to :data:`hope.core.events.EventType.SPEECH_TRANSCRIPT` events
(published by :mod:`hope.speech.whisper_cpp`) and fires a callback when
the transcript matches one of the configured wake phrases.

Matching strategy per phrase (tried in order, stop at first hit):

1. Normalise transcript (lowercase, strip punctuation, collapse whitespace).
2. **Substring match** — phrase appears verbatim anywhere in the transcript.
3. **Fuzzy match** — for every length-matched window of the transcript,
   compute Levenshtein distance; if any window is within
   :attr:`max_edit_distance` edits (default 2), count it as a match.

The Levenshtein implementation is the standard O(n*m) DP, which is more
than fast enough for our phrase lengths (< 20 chars). Pure stdlib, no deps.
"""

from __future__ import annotations

import logging
import re
import string
from typing import Callable, Iterable, List, Optional

from hope.core.events import Event, EventBus, EventType

logger = logging.getLogger(__name__)


_PUNCT_STRIPPER = str.maketrans("", "", string.punctuation)
_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower().translate(_PUNCT_STRIPPER)
    return _WHITESPACE.sub(" ", text).strip()


def _levenshtein(a: str, b: str) -> int:
    """Classic O(n*m) edit distance."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Ensure b is the shorter one to keep the row small.
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    curr = [0] * (len(b) + 1)
    for i, ca in enumerate(a, start=1):
        curr[0] = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                prev[j] + 1,       # deletion
                curr[j - 1] + 1,   # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev, curr = curr, prev
    return prev[-1]


def _fuzzy_contains(haystack: str, needle: str, max_distance: int) -> bool:
    """True if any len(needle)-sized window of haystack is within *max_distance* edits of needle.

    We scan windows of length ``len(needle)`` *and* ``len(needle) ± max_distance``
    so insertions/deletions near the boundary still match.
    """
    if not needle:
        return False
    n = len(needle)
    # Quick accept — exact substring.
    if needle in haystack:
        return True
    # Also try the whole haystack (when it is short, e.g. "hey hoped").
    if abs(len(haystack) - n) <= max_distance:
        if _levenshtein(haystack, needle) <= max_distance:
            return True
    # Sliding window over the haystack at multiple widths.
    widths = {n}
    for delta in range(1, max_distance + 1):
        widths.add(n + delta)
        if n - delta > 0:
            widths.add(n - delta)
    for w in widths:
        if w > len(haystack):
            continue
        for start in range(0, len(haystack) - w + 1):
            window = haystack[start : start + w]
            if _levenshtein(window, needle) <= max_distance:
                return True
    return False


class PhraseMatcher:
    """Subscribes to SPEECH_TRANSCRIPT and fires on fuzzy phrase hits.

    Parameters
    ----------
    bus:
        The event bus to subscribe against.
    on_match:
        Callable invoked with the original transcript text when any
        configured wake phrase matches.
    phrases:
        Wake phrases to look for. Stored normalised.
    min_confidence:
        Skip transcripts whose ``confidence`` field (if present and numeric)
        is below this value. Pass ``None`` to disable confidence gating.
    max_edit_distance:
        Max Levenshtein distance tolerated in fuzzy matches (default 2).
    """

    def __init__(
        self,
        bus: EventBus,
        on_match: Callable[[str], None],
        phrases: Iterable[str],
        *,
        min_confidence: Optional[float] = 0.5,
        max_edit_distance: int = 2,
    ) -> None:
        self._bus = bus
        self._on_match = on_match
        self._phrases: List[str] = [_normalize(p) for p in phrases if p.strip()]
        self._min_confidence = min_confidence
        self._max_edit_distance = max_edit_distance
        self._subscribed = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Subscribe to SPEECH_TRANSCRIPT. Idempotent."""
        if self._subscribed:
            return
        self._bus.subscribe(EventType.SPEECH_TRANSCRIPT, self._handle_event)
        self._subscribed = True

    def stop(self) -> None:
        """Unsubscribe from the bus. Idempotent."""
        if not self._subscribed:
            return
        self._bus.unsubscribe(EventType.SPEECH_TRANSCRIPT, self._handle_event)
        self._subscribed = False

    @property
    def is_active(self) -> bool:
        return self._subscribed

    # -- matching -----------------------------------------------------------

    def matches(self, text: str) -> bool:
        """True if *text* fuzzy-matches any configured wake phrase."""
        norm = _normalize(text)
        if not norm:
            return False
        for phrase in self._phrases:
            if _fuzzy_contains(norm, phrase, self._max_edit_distance):
                return True
        return False

    # -- event bridge -------------------------------------------------------

    def _handle_event(self, event: Event) -> None:
        data = event.data or {}
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        # Confidence gate (None or NaN-ish values pass through).
        if self._min_confidence is not None:
            conf = data.get("confidence")
            if isinstance(conf, (int, float)) and conf < self._min_confidence:
                return
        if not self.matches(text):
            return
        try:
            self._on_match(text)
        except Exception as exc:  # pragma: no cover — callback discipline
            logger.exception("phrase match callback raised: %s", exc)


__all__ = ["PhraseMatcher"]
