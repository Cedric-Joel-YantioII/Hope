"""Base class for evolution experiments + a tiny registry.

An :class:`Experiment` is a self-contained, idempotent attempt to improve
Hope. It has two phases:

* :meth:`Experiment.apply` — mutate the source tree at ``workspace_path``.
  Must be deterministic given its inputs; the runner calls it exactly once
  per cycle inside a clean git branch. May write files, rewrite constants,
  add tests, etc.
* :meth:`Experiment.evaluate` — run the test suite + any custom scorers
  against the mutated tree and return an :class:`EvaluationResult`.

The runner decides whether to *propose* the change by comparing
``result.score`` to the baseline score produced by an evaluation of HEAD
before ``apply`` ran.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Type


@dataclass(slots=True)
class EvaluationResult:
    """Outcome of running :meth:`Experiment.evaluate`.

    Attributes
    ----------
    success:
        Did the experiment reach its goal? Runner treats ``False`` as "do
        not propose a merge".
    score:
        A single scalar the runner compares against the baseline. Higher
        is better. Scale is experiment-defined; ack-phrases uses 0-10.
    tests_passed:
        Required for any merge proposal (safety rule).
    details:
        Free-form diagnostics — surfaced in ``hope evolve status``.
    artifacts:
        Absolute paths of files the experiment produced (diffs, logs).
    """

    success: bool
    score: float
    tests_passed: bool
    details: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[str] = field(default_factory=list)


class Experiment(abc.ABC):
    """Base class for a single self-improvement experiment.

    Subclasses live under ``hope.evolution.experiments`` and register
    themselves via :meth:`ExperimentRegistry.register`.
    """

    #: Short machine name, e.g. ``"improve_ack_phrases"``. Used by
    #: ``hope evolve run <name>`` and in commit messages.
    name: str = ""

    #: One-sentence human description.
    description: str = ""

    @abc.abstractmethod
    def apply(self, workspace_path: Path) -> None:
        """Mutate the source tree in-place.

        ``workspace_path`` is the root of a fresh git worktree / volume
        mount (e.g. ``/workspace`` inside the container, or a temp
        checkout on the host for dry-runs). The runner guarantees the
        working tree is clean before calling this.
        """

    @abc.abstractmethod
    def evaluate(self, workspace_path: Path) -> EvaluationResult:
        """Run tests + custom scoring against the mutated tree."""

    # Optional override: most experiments are fine with "what changed in
    # the working tree". But some may want to restrict the diff the
    # runner commits (e.g. "only stage src/hope/daemon/core.py").
    def files_to_stage(self, workspace_path: Path) -> List[str]:
        """Paths (relative to ``workspace_path``) to ``git add`` on success.

        Default: empty list ⇒ runner uses ``git add -A``. Override to
        constrain the blast radius.
        """
        return []

    def commit_message(self) -> str:
        """Single-line subject for the evolution commit."""
        return f"evolve({self.name}): {self.description}"


class ExperimentRegistry:
    """Tiny in-process registry so ``hope evolve`` can discover experiments.

    Experiments self-register at import time via ``@ExperimentRegistry.register``.
    ``hope.evolution.runner`` imports the ``experiments`` package to trigger
    registration.
    """

    _registry: Dict[str, Type[Experiment]] = {}

    @classmethod
    def register(
        cls, experiment_cls: Type[Experiment] | None = None
    ) -> Callable[[Type[Experiment]], Type[Experiment]] | Type[Experiment]:
        """Decorator form: ``@ExperimentRegistry.register``."""

        def _do(c: Type[Experiment]) -> Type[Experiment]:
            if not c.name:
                raise ValueError(
                    f"Experiment {c.__name__} must set a non-empty .name"
                )
            if c.name in cls._registry:
                # Re-registering (e.g. reload in tests) — allow.
                pass
            cls._registry[c.name] = c
            return c

        if experiment_cls is None:
            return _do
        return _do(experiment_cls)

    @classmethod
    def get(cls, name: str) -> Type[Experiment]:
        if name not in cls._registry:
            raise KeyError(
                f"Experiment not found: {name!r}. Known: {sorted(cls._registry)}"
            )
        return cls._registry[name]

    @classmethod
    def all(cls) -> Dict[str, Type[Experiment]]:
        return dict(cls._registry)

    @classmethod
    def clear(cls) -> None:
        """For tests."""
        cls._registry.clear()


__all__ = [
    "EvaluationResult",
    "Experiment",
    "ExperimentRegistry",
]
