"""Hope's nightly self-evolution sandbox.

Every night the scheduler fires ``EvolutionRunner.run_cycle()``. The runner:

1. snapshots the current HEAD into a fresh ``evolve/<timestamp>`` branch,
2. starts a Docker container (image ``hope-evolve:latest``) with the branch
   volume-mounted at ``/workspace`` and ``~/.hope/traces.db`` mounted
   read-only,
3. runs a chosen :class:`Experiment` — ``apply`` to modify the source tree
   in-place, then ``evaluate`` to run the test suite + score,
4. if evaluation beats the baseline, commits the diff to a
   ``evolve/merged-<timestamp>`` branch for human review,
5. merging to ``main`` is always human-gated (``hope evolve approve``).

Phase-1 ships the framework + one concrete experiment
(``improve_ack_phrases``). Additional experiments plug in by subclassing
:class:`Experiment` under ``hope.evolution.experiments``.
"""

from hope.evolution.experiment import (
    EvaluationResult,
    Experiment,
    ExperimentRegistry,
)
from hope.evolution.runner import (
    EvolutionCycleResult,
    EvolutionRunner,
    RunnerConfig,
)

__all__ = [
    "EvaluationResult",
    "EvolutionCycleResult",
    "EvolutionRunner",
    "Experiment",
    "ExperimentRegistry",
    "RunnerConfig",
]
