"""Learning primitive — routing, reward, skill-level optimization.

Trimmed during the voice-arch cleanup: the config-search (GEPA, DSPy,
optimizer/trial_runner) and intelligence-training stacks were removed.
The surviving surface is routing + skill-learning, which the sibling
``telemetry -> learning`` loop consumes.
"""

from __future__ import annotations

from hope.learning._stubs import (
    QueryAnalyzer,
    RewardFunction,
    RouterPolicy,
    RoutingContext,
)

__all__ = [
    "QueryAnalyzer",
    "RewardFunction",
    "RouterPolicy",
    "RoutingContext",
]
