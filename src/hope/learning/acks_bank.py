"""Context-aware acknowledgement selection for Hope's parallel-ack path.

The daemon speaks an ``ack`` the moment a voice turn arrives — in parallel
with the brain generating its reply — so the user hears *something*
within a second even on long turns. Previously this pulled from a flat
8-entry tuple, which made Hope feel cycled and mechanical. This module
replaces that with:

  1. A categorizer that classifies the transcript into a coarse intent
     bucket (``task``, ``question``, ``followup``, ``gibberish``).
  2. A per-category pool of short, varied British-voiced acks.
  3. A no-repeat ring so she never picks the same ack twice in a row
     (within the caller-supplied recent-acks memory).
  4. A rare playful pool — ~1-in-8 turns gets a slightly wittier ack,
     matching Hope's dry-wit character without over-acting.

Acks are fire-and-forget TTS. They must be SHORT (~1s spoken, i.e.
under ~8 words) so they finish before the brain's reply is ready.
"""

from __future__ import annotations

import random
import re
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Pools — grouped by what the user just said. Keep every entry ≤ 8 words,
# British-voiced, and specific enough that Hope doesn't sound like she's
# pulling from a corporate FAQ.
# ---------------------------------------------------------------------------

_POOL_TASK = (
    "On it.",
    "Right, doing it now.",
    "Handling it.",
    "Give me a moment, sir.",
    "Just a second.",
    "Taking care of it.",
    "Consider it in progress.",
    "Working on that now.",
)

_POOL_QUESTION = (
    "Let me check.",
    "Looking that up.",
    "One moment, finding out.",
    "Right, let me think.",
    "Give me a beat.",
    "Hmm, one moment.",
    "Let me see.",
    "Looking into it.",
)

_POOL_FOLLOWUP = (
    "Mm-hmm.",
    "Right.",
    "Got it.",
    "Of course.",
    "Ah, yes.",
    "Sure thing.",
    "Okay.",
    "Right you are.",
)

_POOL_GIBBERISH = (
    "Sorry, say that again?",
    "Didn't quite catch that.",
    "One more time?",
    "Hmm, I missed it.",
    "Come again, sir?",
    "Pardon — one more time?",
)

_POOL_GENERIC = (
    "Okay, let me think about that.",
    "One moment, looking into it now.",
    "Right, give me just a second.",
    "Sure, let me take a look.",
    "Hmm, let me check that for you.",
    "Working on it.",
    "Got it, one moment.",
    "Alright, thinking it through.",
)

# Rare, played ~1 in 8 turns. Hope's understated wit — never full stand-up.
_POOL_PLAYFUL = (
    "Right, right. Let's see.",
    "On the case.",
    "Working, working.",
    "Doing my level best.",
    "Mm, fun.",
    "Oh, we're doing that now?",
    "Of course it's this, too.",
    "At your service, sir.",
)

# Probability Hope pulls from the playful pool when the category allows it
# (task and question, where lightness is appropriate — not gibberish).
_PLAYFUL_RATE = 0.125


# ---------------------------------------------------------------------------
# Categorizer
# ---------------------------------------------------------------------------

_TASK_START_VERBS = {
    "open", "close", "launch", "quit", "kill", "start", "stop", "play",
    "pause", "resume", "run", "make", "do", "create", "write", "send",
    "show", "bring", "fetch", "pull", "grab", "set", "turn", "move",
    "delete", "remove", "save", "add", "edit", "rename", "find", "search",
    "check", "take", "tell",  # "tell me to do X" is a task; distinguishable
                               #  from "tell me how X works" (question) by
                               #  the next word, but coarse match is fine.
    "go", "clean", "clear", "reset", "copy", "paste", "build", "install",
}

_QUESTION_START = re.compile(
    r"^\s*(?:what|how|why|when|where|who|which|is|are|was|were|am|"
    r"can|could|would|should|will|do|does|did|have|has|had)\b",
    re.IGNORECASE,
)

_VERB_START = re.compile(r"^\s*([a-zA-Z']+)\b")

_NOISE_PATTERNS = re.compile(
    r"^\s*(?:uh|um|hmm+|er|erm|\.\.\.|\W)*\s*$",
    re.IGNORECASE,
)


def categorize(text: str) -> str:
    """Map an incoming transcript to an ack category.

    Returns one of: ``task``, ``question``, ``followup``, ``gibberish``,
    ``generic``. The categorizer is intentionally coarse — the point is
    a different-sounding ack, not a perfect intent model.
    """
    t = (text or "").strip()
    if not t or _NOISE_PATTERNS.match(t):
        return "gibberish"
    # Very short with no verb → probably a follow-up like "yes" / "the other one"
    if len(t) < 18 and not _QUESTION_START.match(t):
        word_match = _VERB_START.match(t)
        first = word_match.group(1).lower() if word_match else ""
        if first not in _TASK_START_VERBS:
            return "followup"
    if _QUESTION_START.match(t):
        return "question"
    word_match = _VERB_START.match(t)
    if word_match and word_match.group(1).lower() in _TASK_START_VERBS:
        return "task"
    return "generic"


# ---------------------------------------------------------------------------
# Picker
# ---------------------------------------------------------------------------

_CATEGORY_POOLS = {
    "task": _POOL_TASK,
    "question": _POOL_QUESTION,
    "followup": _POOL_FOLLOWUP,
    "gibberish": _POOL_GIBBERISH,
    "generic": _POOL_GENERIC,
}


def pick_ack(
    text: str,
    recent: Optional[Sequence[str]] = None,
    *,
    rng: Optional[random.Random] = None,
) -> str:
    """Return an ack line appropriate to *text*, avoiding any in *recent*.

    *recent* should be a short sequence (last 2–3 acks) of lines Hope has
    spoken recently; the picker will never pick one of them if it can
    help it, so she doesn't sound like a cycling loop.
    """
    rng = rng or random
    cat = categorize(text)
    pool = _CATEGORY_POOLS.get(cat, _POOL_GENERIC)
    # Small chance to slip in a playful ack when the category allows it.
    if cat in ("task", "question") and rng.random() < _PLAYFUL_RATE:
        pool = _POOL_PLAYFUL
    recent_set = set(recent or ())
    # Prefer entries not recently spoken.
    fresh = [p for p in pool if p not in recent_set]
    if fresh:
        return rng.choice(fresh)
    # All recent — fine, just pick any.
    return rng.choice(pool)


def all_pools() -> dict:
    """Introspection helper (tests, diagnostics)."""
    return {
        "task": _POOL_TASK,
        "question": _POOL_QUESTION,
        "followup": _POOL_FOLLOWUP,
        "gibberish": _POOL_GIBBERISH,
        "generic": _POOL_GENERIC,
        "playful": _POOL_PLAYFUL,
    }


__all__ = ["categorize", "pick_ack", "all_pools"]
