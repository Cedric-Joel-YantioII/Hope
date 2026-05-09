"""Voice-turn learning loop.

Owns the overlay state Hope mutates to improve herself over time:

* ``~/.hope/learning/acks.json`` — ack phrases used by the daemon.
* ``~/.hope/learning/wake_phrases.json`` — extra wake-phrase alternates.
* ``~/.hope/learning/skills/<name>/optimized.md`` — rewritten skill bodies.

The loop runs opt-in (config key ``learning.enabled``) on a background
thread. Every N turns OR every M minutes it:

1. Pulls unscored :class:`VoiceTurn` rows + enough context to score them.
2. Applies the cheap scorers from :mod:`hope.learning.voice_scorers`.
3. Writes scores back to the store.
4. Evolves ack-phrase + wake-phrase overlays based on correlations.
5. Optionally triggers :class:`SkillOptimizer` (nightly cadence).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from hope.core.config import DEFAULT_CONFIG_DIR
from hope.learning.voice_scorers import score_window
from hope.traces.voice_trace import VoiceTraceStore, VoiceTurn

logger = logging.getLogger(__name__)


LEARNING_DIR = DEFAULT_CONFIG_DIR / "learning"
ACKS_PATH = LEARNING_DIR / "acks.json"
WAKE_PATH = LEARNING_DIR / "wake_phrases.json"
SKILL_OVERLAY_DIR = LEARNING_DIR / "skills"


DEFAULT_ACKS: Tuple[str, ...] = (
    "Okay, let me think about that.",
    "One moment, looking into it now.",
    "Right, give me just a second.",
    "Sure, let me take a look.",
    "Hmm, let me check that for you.",
    "Okay, working on it right now.",
    "Got it, just a moment please.",
    "Alright, thinking it through now.",
)

DEFAULT_WAKE_PHRASES: Tuple[str, ...] = (
    "wake up hope",
    "hey hope",
    "hope wake up",
    "ok hope",
)


# ---------------------------------------------------------------------------
# JSON overlay accessors
# ---------------------------------------------------------------------------


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_acks() -> List[str]:
    """Return the current ack phrases (file overlay or baked-in defaults)."""
    try:
        raw = ACKS_PATH.read_text()
    except (FileNotFoundError, OSError):
        return list(DEFAULT_ACKS)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("acks.json malformed; falling back to defaults")
        return list(DEFAULT_ACKS)
    phrases = data.get("phrases") if isinstance(data, dict) else data
    if not isinstance(phrases, list) or not phrases:
        return list(DEFAULT_ACKS)
    return [str(p) for p in phrases if isinstance(p, str) and p.strip()]


def save_acks(phrases: Sequence[str]) -> None:
    _ensure_parent(ACKS_PATH)
    ACKS_PATH.write_text(
        json.dumps({"phrases": list(phrases)}, indent=2, ensure_ascii=False)
    )


def load_wake_phrases() -> List[str]:
    try:
        raw = WAKE_PATH.read_text()
    except (FileNotFoundError, OSError):
        return list(DEFAULT_WAKE_PHRASES)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return list(DEFAULT_WAKE_PHRASES)
    phrases = data.get("phrases") if isinstance(data, dict) else data
    if not isinstance(phrases, list) or not phrases:
        return list(DEFAULT_WAKE_PHRASES)
    return [str(p).lower() for p in phrases if isinstance(p, str) and p.strip()]


def save_wake_phrases(phrases: Sequence[str]) -> None:
    _ensure_parent(WAKE_PATH)
    # De-duplicate case-insensitively while preserving order.
    seen: set[str] = set()
    clean: List[str] = []
    for p in phrases:
        low = p.lower().strip()
        if low and low not in seen:
            seen.add(low)
            clean.append(low)
    WAKE_PATH.write_text(
        json.dumps({"phrases": clean}, indent=2, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# Ack-phrase evolution
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AckEvolutionResult:
    kept: List[str] = field(default_factory=list)
    pruned: List[str] = field(default_factory=list)
    ack_scores: Dict[str, float] = field(default_factory=dict)


def evolve_acks(
    turns: Sequence[VoiceTurn],
    *,
    min_samples: int = 5,
    prune_below: float = 0.35,
    keep_floor: int = 4,
) -> AckEvolutionResult:
    """Prune ack phrases correlated with low-scoring turns.

    Only acks with at least *min_samples* uses and mean score < *prune_below*
    are pruned. Never shrinks below *keep_floor* phrases.
    """
    current = load_acks()
    buckets: Dict[str, List[float]] = defaultdict(list)
    for t in turns:
        if not t.ack_spoken or t.score is None:
            continue
        buckets[t.ack_spoken].append(float(t.score))

    ack_scores = {
        phrase: (sum(scores) / len(scores)) if scores else 0.5
        for phrase, scores in buckets.items()
    }

    pruned: List[str] = []
    kept: List[str] = list(current)
    for phrase, mean in ack_scores.items():
        if phrase not in kept:
            continue
        if len(buckets[phrase]) >= min_samples and mean < prune_below:
            pruned.append(phrase)

    for p in pruned:
        if len(kept) <= keep_floor:
            break
        try:
            kept.remove(p)
        except ValueError:
            pass

    if pruned and len(kept) > keep_floor:
        save_acks(kept)
        logger.info("ack-evolve: pruned=%s kept=%d", pruned, len(kept))

    return AckEvolutionResult(kept=kept, pruned=pruned, ack_scores=ack_scores)


# ---------------------------------------------------------------------------
# Wake-phrase learning
# ---------------------------------------------------------------------------


_WAKE_HINT_WORDS = ("hope", "help")


def mine_wake_alternates(
    turns: Sequence[VoiceTurn],
    *,
    min_uses: int = 3,
) -> List[str]:
    """Return transcripts that LOOK like mis-heard wake phrases.

    Heuristic: short utterances (<=5 tokens) that contain a token near
    "hope" AND recur — meaning whisper keeps mishearing the wake phrase
    the same way. Worth adding as an alternate.
    """
    candidates: Dict[str, int] = defaultdict(int)
    current = set(load_wake_phrases())
    for t in turns:
        text = (t.user_transcript or "").lower().strip(" .,!?")
        if not text or len(text.split()) > 5:
            continue
        if text in current:
            continue
        if any(hint in text for hint in _WAKE_HINT_WORDS):
            candidates[text] += 1
    return [phrase for phrase, count in candidates.items() if count >= min_uses]


def evolve_wake_phrases(turns: Sequence[VoiceTurn]) -> List[str]:
    alternates = mine_wake_alternates(turns)
    if not alternates:
        return load_wake_phrases()
    merged = load_wake_phrases() + alternates
    save_wake_phrases(merged)
    logger.info("wake-evolve: added %s", alternates)
    return load_wake_phrases()


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LoopConfig:
    enabled: bool = False
    turns_between_runs: int = 10
    min_seconds_between_runs: float = 60.0
    window_hours: float = 24.0
    nightly_skill_optimize: bool = True
    skill_optimize_hour_utc: int = 6  # "nightly" run ~6am UTC


class VoiceLearningLoop:
    """Lightweight background coordinator.

    Not a full scheduler — wake by :meth:`note_turn` after each voice turn
    (cheap) and internally debounce. :meth:`tick` is also safe to call
    from a cron/systemd-style outside scheduler.
    """

    def __init__(
        self,
        store: VoiceTraceStore,
        *,
        config: Optional[LoopConfig] = None,
        skill_optimize_hook: Optional[Callable[[], None]] = None,
    ) -> None:
        self._store = store
        self._cfg = config or LoopConfig()
        self._lock = threading.Lock()
        self._turns_since_run = 0
        self._last_run_at = 0.0
        self._last_skill_run_day = -1
        self._skill_hook = skill_optimize_hook

    # -- triggers ----------------------------------------------------------

    def note_turn(self) -> None:
        """Increment counter; run the loop if thresholds crossed."""
        if not self._cfg.enabled:
            return
        with self._lock:
            self._turns_since_run += 1
            now = time.time()
            enough_turns = (
                self._turns_since_run >= self._cfg.turns_between_runs
            )
            enough_time = (
                now - self._last_run_at >= self._cfg.min_seconds_between_runs
            )
            if not (enough_turns and enough_time):
                return
            self._turns_since_run = 0
            self._last_run_at = now
        threading.Thread(
            target=self._safe_tick,
            name="hope-voice-learning",
            daemon=True,
        ).start()

    def _safe_tick(self) -> None:
        try:
            self.tick()
        except Exception:
            logger.exception("voice learning tick failed")

    # -- work --------------------------------------------------------------

    def tick(self) -> Dict[str, Any]:
        """Score unscored recent turns and run evolution steps."""
        since = time.time() - self._cfg.window_hours * 3600.0
        recent = self._store.list_recent(since=since, limit=500)
        if not recent:
            return {"scored": 0, "status": "no-turns"}

        scored_results = score_window(recent)
        unscored_ids = {t.turn_id for t in recent if t.score is None}
        updated = 0
        for turn_id, score, reason in scored_results:
            if turn_id in unscored_ids:
                if self._store.update_score(turn_id, score, reason):
                    updated += 1

        recent_scored = self._store.list_recent(
            since=since, limit=500, only_unscored=False
        )

        ack_result = evolve_acks(recent_scored)
        wake_phrases = evolve_wake_phrases(recent_scored)

        skill_run = False
        if self._cfg.nightly_skill_optimize and self._skill_hook is not None:
            now = time.gmtime()
            if (
                now.tm_hour == self._cfg.skill_optimize_hour_utc
                and now.tm_yday != self._last_skill_run_day
            ):
                try:
                    self._skill_hook()
                    skill_run = True
                    self._last_skill_run_day = now.tm_yday
                except Exception:
                    logger.exception("nightly skill optimize failed")

        return {
            "scored": updated,
            "window_turns": len(recent_scored),
            "ack_pruned": ack_result.pruned,
            "wake_phrases": wake_phrases,
            "skill_optimize_run": skill_run,
        }


def default_skill_optimize_hook(store: VoiceTraceStore) -> Callable[[], None]:
    """Return a closure that runs :class:`SkillOptimizer` over *store*.

    Adapts voice turns into the shape the existing SkillOptimizer expects
    by presenting a tiny shim with ``list_traces()``.
    """
    def _run() -> None:
        try:
            from hope.core.events import EventBus
            from hope.core.types import StepType, Trace, TraceStep
            from hope.learning.agents.skill_optimizer import SkillOptimizer
            from hope.skills.manager import SkillManager
        except Exception as exc:
            logger.info("skill optimizer deps missing: %s", exc)
            return

        class _Adapter:
            def __init__(self, turns: Sequence[VoiceTurn]) -> None:
                self._turns = list(turns)

            def list_traces(self, *, limit: int = 100, **_: Any) -> List[Trace]:
                out: List[Trace] = []
                for t in self._turns[:limit]:
                    steps: List[TraceStep] = []
                    for name in t.skill_tags or []:
                        steps.append(
                            TraceStep(
                                step_type=StepType.TOOL_CALL,
                                timestamp=t.started_at,
                                duration_seconds=t.duration_seconds,
                                input={"content": t.user_transcript},
                                output={"content": t.brain_reply_full},
                                metadata={"skill": name},
                            )
                        )
                    out.append(
                        Trace(
                            trace_id=t.turn_id,
                            query=t.user_transcript,
                            agent="hope-voice",
                            model="tmux-brain",
                            engine="voice",
                            result=t.brain_reply_full,
                            outcome="ok" if not t.error else "error",
                            feedback=t.score,
                            started_at=t.started_at,
                            ended_at=t.ended_at,
                            total_tokens=0,
                            total_latency_seconds=t.duration_seconds,
                            metadata=t.metadata,
                            messages=[],
                            steps=steps,
                        )
                    )
                return out

        since = time.time() - 7 * 24 * 3600.0
        turns = store.list_recent(since=since, limit=2000)
        adapter = _Adapter(turns)
        mgr = SkillManager(bus=EventBus())
        try:
            mgr.discover()
        except Exception:
            logger.exception("SkillManager.discover failed")
            return
        opt = SkillOptimizer(min_traces_per_skill=20, optimizer="dspy")
        opt.optimize(adapter, mgr, overlay_dir=SKILL_OVERLAY_DIR)

    return _run


__all__ = [
    "ACKS_PATH",
    "DEFAULT_ACKS",
    "DEFAULT_WAKE_PHRASES",
    "LEARNING_DIR",
    "LoopConfig",
    "SKILL_OVERLAY_DIR",
    "VoiceLearningLoop",
    "WAKE_PATH",
    "AckEvolutionResult",
    "default_skill_optimize_hook",
    "evolve_acks",
    "evolve_wake_phrases",
    "load_acks",
    "load_wake_phrases",
    "mine_wake_alternates",
    "save_acks",
    "save_wake_phrases",
]
