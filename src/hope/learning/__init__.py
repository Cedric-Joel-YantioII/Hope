"""Learning primitive -- router policies, reward functions, learning."""

from __future__ import annotations

from hope.learning._stubs import (
    QueryAnalyzer,
    RewardFunction,
    RouterPolicy,
    RoutingContext,
)
from hope.learning.agents.agent_evolver import AgentConfigEvolver
from hope.learning.learning_orchestrator import LearningOrchestrator
from hope.learning.optimize.llm_optimizer import LLMOptimizer
from hope.learning.optimize.optimizer import OptimizationEngine
from hope.learning.optimize.store import OptimizationStore
from hope.learning.routing.complexity import (
    ComplexityQueryAnalyzer,
    score_complexity,
)
from hope.learning.routing.heuristic_reward import HeuristicRewardFunction
from hope.learning.routing.router import (
    HeuristicRouter,
    build_routing_context,
)
from hope.learning.training.data import TrainingDataMiner
from hope.learning.training.lora import HAS_TORCH, LoRATrainer, LoRATrainingConfig


def ensure_registered() -> None:
    """Ensure all learning policies are registered in RouterPolicyRegistry."""
    from hope.learning.routing.heuristic_policy import (
        ensure_registered as _reg_heuristic,
    )

    _reg_heuristic()

    from hope.learning.routing.learned_router import (
        ensure_registered as _reg_learned,
    )

    _reg_learned()

    # Intelligence training (optional deps)
    try:
        import hope.learning.intelligence  # noqa: F401
    except ImportError:
        pass

    # Orchestrator-specific training (optional deps)
    try:
        import hope.learning.intelligence.orchestrator  # noqa: F401
    except ImportError:
        pass

    # Agent optimizers (optional deps)
    try:
        import hope.learning.agents.dspy_optimizer  # noqa: F401
    except ImportError:
        pass
    try:
        import hope.learning.agents.gepa_optimizer  # noqa: F401
    except ImportError:
        pass


__all__ = [
    "AgentConfigEvolver",
    "ComplexityQueryAnalyzer",
    "HAS_TORCH",
    "HeuristicRewardFunction",
    "HeuristicRouter",
    "LLMOptimizer",
    "LearningOrchestrator",
    "LoRATrainer",
    "LoRATrainingConfig",
    "OptimizationEngine",
    "OptimizationStore",
    "QueryAnalyzer",
    "RewardFunction",
    "RouterPolicy",
    "RoutingContext",
    "TrainingDataMiner",
    "build_routing_context",
    "ensure_registered",
    "score_complexity",
]
