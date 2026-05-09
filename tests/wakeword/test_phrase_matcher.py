"""Unit tests for :class:`hope.wakeword.phrase_matcher.PhraseMatcher`."""

from __future__ import annotations

from typing import List

import pytest

from hope.core.events import EventBus, EventType
from hope.wakeword.phrase_matcher import PhraseMatcher

DEFAULT_PHRASES = ["wake up hope", "hey hope", "hope wake up", "ok hope"]


@pytest.fixture
def bus() -> EventBus:
    return EventBus(record_history=False)


@pytest.fixture
def matches() -> List[str]:
    return []


@pytest.fixture
def matcher(bus: EventBus, matches: List[str]) -> PhraseMatcher:
    m = PhraseMatcher(
        bus=bus,
        on_match=matches.append,
        phrases=DEFAULT_PHRASES,
        min_confidence=0.5,
    )
    m.start()
    return m


def _publish(bus: EventBus, text: str, *, confidence: float | None = 0.95) -> None:
    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {
            "text": text,
            "confidence": confidence,
            "lang": "en",
            "timestamp": 0.0,
            "duration_ms": 800,
        },
    )


def test_exact_phrase_matches(bus: EventBus, matcher: PhraseMatcher, matches: List[str]) -> None:
    _publish(bus, "Wake up Hope!")
    assert matches == ["Wake up Hope!"]


def test_case_and_punctuation_insensitive(
    bus: EventBus, matcher: PhraseMatcher, matches: List[str]
) -> None:
    _publish(bus, "Hey, Hope.")
    assert matches == ["Hey, Hope."]


def test_fuzzy_misrecognition_wake_up_hop(
    bus: EventBus, matcher: PhraseMatcher, matches: List[str]
) -> None:
    # Whisper occasionally drops the trailing 'e' — Levenshtein distance 1.
    _publish(bus, "wake up hop")
    assert matches == ["wake up hop"]


def test_fuzzy_misrecognition_hey_hoped(
    bus: EventBus, matcher: PhraseMatcher, matches: List[str]
) -> None:
    # Insertion error — Levenshtein distance 1 vs "hey hope".
    _publish(bus, "hey hoped")
    assert matches == ["hey hoped"]


def test_unrelated_text_does_not_match(
    bus: EventBus, matcher: PhraseMatcher, matches: List[str]
) -> None:
    _publish(bus, "what's the weather today")
    _publish(bus, "play some music please")
    _publish(bus, "tell me a joke")
    assert matches == []


def test_phrase_inside_longer_utterance_matches(
    bus: EventBus, matcher: PhraseMatcher, matches: List[str]
) -> None:
    _publish(bus, "um okay wake up hope and check my email")
    assert len(matches) == 1


def test_low_confidence_is_ignored(
    bus: EventBus, matcher: PhraseMatcher, matches: List[str]
) -> None:
    _publish(bus, "wake up hope", confidence=0.3)
    assert matches == []


def test_missing_confidence_is_allowed(
    bus: EventBus, matcher: PhraseMatcher, matches: List[str]
) -> None:
    # When confidence is missing (None), we fall through the gate.
    _publish(bus, "wake up hope", confidence=None)
    assert matches == ["wake up hope"]


def test_too_far_off_does_not_match(
    bus: EventBus, matcher: PhraseMatcher, matches: List[str]
) -> None:
    # Edit distance > 2 vs every configured phrase.
    _publish(bus, "wake me up in an hour")
    assert matches == []


def test_empty_text_ignored(bus: EventBus, matcher: PhraseMatcher, matches: List[str]) -> None:
    _publish(bus, "")
    _publish(bus, "   ")
    assert matches == []


def test_stop_unsubscribes(bus: EventBus, matcher: PhraseMatcher, matches: List[str]) -> None:
    matcher.stop()
    _publish(bus, "wake up hope")
    assert matches == []
    # start() again should resume.
    matcher.start()
    _publish(bus, "hey hope")
    assert matches == ["hey hope"]


def test_start_is_idempotent(bus: EventBus, matcher: PhraseMatcher, matches: List[str]) -> None:
    matcher.start()  # second call — must not double-subscribe.
    _publish(bus, "ok hope")
    # If we had double-subscribed, we'd see two entries.
    assert matches == ["ok hope"]
