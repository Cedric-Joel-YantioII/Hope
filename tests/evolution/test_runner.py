"""Full-cycle test for :class:`EvolutionRunner` with a fake experiment.

We build a throwaway git repo with a single ``constant.py`` file holding
``X = 1``. A fake experiment flips it to ``X = 2`` and scores better; the
runner should produce an ``evolve/merged-*`` branch containing exactly
that change.

Docker is disabled (``use_docker=False``) so the test runs anywhere.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hope.evolution.experiment import (
    EvaluationResult,
    Experiment,
    ExperimentRegistry,
)
from hope.evolution.runner import EvolutionRunner, RunnerConfig


class _FlipConstant(Experiment):
    name = "flip_constant"
    description = "Flip X=1 to X=2"

    def __init__(self) -> None:
        self._post_apply = False

    def apply(self, workspace_path: Path) -> None:
        path = workspace_path / "constant.py"
        path.write_text("X = 2\n")
        self._post_apply = True

    def evaluate(self, workspace_path: Path) -> EvaluationResult:
        src = (workspace_path / "constant.py").read_text()
        value = 2 if "X = 2" in src else 1
        return EvaluationResult(
            success=True,
            score=float(value),
            tests_passed=True,
            details={"value": value},
        )

    def files_to_stage(self, workspace_path: Path) -> list[str]:
        return ["constant.py"]


@pytest.fixture()
def tmp_git_repo(tmp_path: Path) -> Path:
    """Initialise a tmp git repo with constant.py committed on main."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True,
    )
    (tmp_path / "constant.py").write_text("X = 1\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "constant.py"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True,
    )
    return tmp_path


def test_run_cycle_proposes_improvement(tmp_git_repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    cfg = RunnerConfig(
        repo_path=tmp_git_repo,
        state_dir=state,
        use_docker=False,
    )
    runner = EvolutionRunner(cfg)
    result = runner.run_cycle(_FlipConstant())

    assert result.proposed is True
    assert result.tests_passed is True
    assert result.candidate_score == pytest.approx(2.0)
    assert result.baseline_score == pytest.approx(1.0)
    assert result.merged_branch is not None
    assert result.merged_branch.startswith("evolve/merged-flip_constant-")

    # The merged branch should contain the flipped file.
    out = subprocess.run(
        ["git", "-C", str(tmp_git_repo), "show",
         f"{result.merged_branch}:constant.py"],
        check=True, capture_output=True, text=True,
    )
    assert "X = 2" in out.stdout

    # Main should still be on the original commit.
    main_val = subprocess.run(
        ["git", "-C", str(tmp_git_repo), "show", "main:constant.py"],
        check=True, capture_output=True, text=True,
    )
    assert "X = 1" in main_val.stdout

    # State dir should have exactly one cycle file.
    files = list(state.glob("cycle-*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["proposed"] is True


def test_run_cycle_rejects_regression(tmp_git_repo: Path, tmp_path: Path) -> None:
    """An experiment that does not improve the score is discarded."""

    class _NoOp(Experiment):
        name = "noop"
        description = "does nothing"

        def apply(self, workspace_path: Path) -> None:
            # Touch the file with the same content so git sees no diff.
            (workspace_path / "constant.py").write_text("X = 1\n")

        def evaluate(self, workspace_path: Path) -> EvaluationResult:
            return EvaluationResult(
                success=True, score=1.0, tests_passed=True, details={},
            )

    cfg = RunnerConfig(
        repo_path=tmp_git_repo,
        state_dir=tmp_path / "state",
        use_docker=False,
    )
    runner = EvolutionRunner(cfg)
    result = runner.run_cycle(_NoOp())

    assert result.proposed is False
    assert result.merged_branch is None
    assert "no_improvement" in result.reason or "below_margin" in result.reason


def test_apply_failure_does_not_crash(tmp_git_repo: Path, tmp_path: Path) -> None:
    class _Boom(Experiment):
        name = "boom"
        description = "raises in apply"

        def apply(self, workspace_path: Path) -> None:
            raise RuntimeError("kaboom")

        def evaluate(self, workspace_path: Path) -> EvaluationResult:
            return EvaluationResult(
                success=True, score=1.0, tests_passed=True, details={},
            )

    cfg = RunnerConfig(
        repo_path=tmp_git_repo,
        state_dir=tmp_path / "state",
        use_docker=False,
    )
    runner = EvolutionRunner(cfg)
    result = runner.run_cycle(_Boom())

    assert result.proposed is False
    assert "apply_failed" in result.reason
    # No crash means this test passes.


def test_registry_roundtrip() -> None:
    """Registering and looking up works (hygiene test for decorator)."""
    # Clean slate to avoid colliding with already-registered experiments.
    saved = ExperimentRegistry.all()
    try:
        ExperimentRegistry.clear()

        @ExperimentRegistry.register
        class _X(Experiment):
            name = "x"
            description = "x"

            def apply(self, workspace_path: Path) -> None: ...
            def evaluate(self, workspace_path: Path) -> EvaluationResult:
                return EvaluationResult(
                    success=True, score=0, tests_passed=True,
                )

        assert "x" in ExperimentRegistry.all()
        assert ExperimentRegistry.get("x") is _X
        with pytest.raises(KeyError):
            ExperimentRegistry.get("does-not-exist")
    finally:
        ExperimentRegistry.clear()
        # Restore whatever was registered at import time.
        for name, cls in saved.items():
            ExperimentRegistry._registry[name] = cls
