"""``EvolutionRunner`` — orchestrates one self-improvement cycle.

High-level flow of :meth:`EvolutionRunner.run_cycle`::

    HEAD ──►  snapshot to  evolve/<ts>  ◄── fresh branch
                │
                ▼
       start Docker container (network=none,
       traces.db read-only, workspace rw)
                │
                ▼
       evaluate HEAD            ── baseline score
                │
                ▼
       experiment.apply()       ── mutate src
                │
                ▼
       experiment.evaluate()    ── candidate score
                │
                ▼
       score > baseline + margin AND tests_passed?
         │                     │
       yes                    no
         │                     │
       commit to              discard branch,
       evolve/merged-<ts>     record reject
       (human-gated merge)

Safety:

* branches are always created off a detached clean tree
* no auto-merge to main (phase-1 rule — ``hope evolve approve`` is the
  only way)
* container gets ``--network=none`` by default
* ``~/.hope/traces.db`` is mounted ``:ro``
* experiment artifacts land under ``~/.hope/evolution/<ts>/``
"""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hope.evolution.experiment import EvaluationResult, Experiment

logger = logging.getLogger(__name__)


DEFAULT_IMAGE = "hope-evolve:latest"
DEFAULT_REPO = Path(__file__).resolve().parents[3]  # repo root
DEFAULT_STATE_DIR = Path.home() / ".hope" / "evolution"
DEFAULT_TRACES_DB = Path.home() / ".hope" / "traces.db"


@dataclass(slots=True)
class RunnerConfig:
    """Injected config — keeps the runner testable.

    Everything is overridable so tests can point at a tmp git repo, mock
    out Docker, and swap the traces DB.
    """

    repo_path: Path = DEFAULT_REPO
    state_dir: Path = DEFAULT_STATE_DIR
    traces_db: Path = DEFAULT_TRACES_DB
    docker_image: str = DEFAULT_IMAGE
    use_docker: bool = True
    network_mode: str = "none"
    min_score_improvement: float = 0.0  # candidate must exceed baseline by this
    pytest_args: List[str] = field(default_factory=lambda: ["-q"])

    def ensure_state_dir(self) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir


@dataclass(slots=True)
class EvolutionCycleResult:
    """Outcome of a single ``run_cycle`` call — persisted as JSON."""

    experiment: str
    timestamp: str
    branch: str
    merged_branch: Optional[str]
    baseline_score: Optional[float]
    candidate_score: Optional[float]
    tests_passed: bool
    proposed: bool  # True ⇒ merged_branch exists, awaiting human approval
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class EvolutionRunner:
    """Orchestrates a single evolution cycle.

    Designed so unit tests can bypass Docker entirely
    (``RunnerConfig(use_docker=False)`` runs ``apply`` + ``evaluate`` in-process
    against a temporary git worktree). The integration test flips
    ``use_docker`` back on when a Docker daemon is available.
    """

    def __init__(self, config: Optional[RunnerConfig] = None) -> None:
        self.config = config or RunnerConfig()
        self.config.ensure_state_dir()

    # -- Public API ----------------------------------------------------------

    def run_cycle(self, experiment: Experiment) -> EvolutionCycleResult:
        """Run the full cycle. Never raises to the caller — failures are
        captured in the returned :class:`EvolutionCycleResult` so the
        scheduler never sees an exception.
        """
        ts = _timestamp()
        branch = f"evolve/{experiment.name}-{ts}"
        logger.info("evolution: starting cycle experiment=%s branch=%s",
                    experiment.name, branch)

        try:
            # 1. Snapshot HEAD into a fresh branch.
            head = self._git("rev-parse", "HEAD").strip()
            self._git("checkout", "-b", branch, head)

            # 2. Baseline evaluation (pre-apply). Same evaluate() call so
            #    the two scores are comparable.
            try:
                baseline = experiment.evaluate(self.config.repo_path)
            except Exception as exc:  # noqa: BLE001
                logger.exception("baseline eval failed")
                return self._abort(
                    experiment, ts, branch,
                    reason=f"baseline_failed: {exc}",
                    baseline=None, candidate=None, tests=False,
                )

            # 3. Apply the experiment.
            try:
                experiment.apply(self.config.repo_path)
            except Exception as exc:  # noqa: BLE001
                logger.exception("apply failed")
                self._restore_branch(branch)
                return self._abort(
                    experiment, ts, branch,
                    reason=f"apply_failed: {exc}",
                    baseline=baseline.score, candidate=None, tests=False,
                )

            # 4. Evaluate candidate — inside container if available.
            candidate = self._run_evaluation(experiment)

            # 5. Decide.
            improved = (
                candidate.tests_passed
                and candidate.success
                and candidate.score >
                    baseline.score + self.config.min_score_improvement
            )
            if not improved:
                reason = self._format_reject_reason(baseline, candidate)
                self._restore_branch(branch)
                return self._persist(EvolutionCycleResult(
                    experiment=experiment.name,
                    timestamp=ts,
                    branch=branch,
                    merged_branch=None,
                    baseline_score=baseline.score,
                    candidate_score=candidate.score,
                    tests_passed=candidate.tests_passed,
                    proposed=False,
                    reason=reason,
                    details={
                        "baseline_details": baseline.details,
                        "candidate_details": candidate.details,
                    },
                ))

            # 6. Propose: stage, commit, then create the merged-<ts>
            #    branch pointing at that commit for human review.
            stage_paths = experiment.files_to_stage(self.config.repo_path)
            if stage_paths:
                self._git("add", "--", *stage_paths)
            else:
                self._git("add", "-A")

            commit_msg = (
                f"{experiment.commit_message()}\n\n"
                f"Automated evolution cycle.\n"
                f"baseline_score={baseline.score:.4f} "
                f"candidate_score={candidate.score:.4f}\n"
                f"tests_passed={candidate.tests_passed}\n"
                f"\nCo-authored-by: hope-evolve <evolve@hope.local>"
            )
            self._git("commit", "-m", commit_msg)
            merged_branch = f"evolve/merged-{experiment.name}-{ts}"
            self._git("branch", merged_branch)

            return self._persist(EvolutionCycleResult(
                experiment=experiment.name,
                timestamp=ts,
                branch=branch,
                merged_branch=merged_branch,
                baseline_score=baseline.score,
                candidate_score=candidate.score,
                tests_passed=candidate.tests_passed,
                proposed=True,
                reason="improvement_detected",
                details={
                    "baseline_details": baseline.details,
                    "candidate_details": candidate.details,
                    "artifacts": candidate.artifacts,
                },
            ))
        except Exception as exc:  # noqa: BLE001 — must not propagate
            logger.exception("evolution cycle crashed")
            return self._abort(
                experiment, ts, branch,
                reason=f"cycle_crashed: {exc}",
                baseline=None, candidate=None, tests=False,
            )

    # -- Evaluation backends ------------------------------------------------

    def _run_evaluation(self, experiment: Experiment) -> EvaluationResult:
        """Evaluate inside Docker when enabled; fallback to in-process."""
        if not self.config.use_docker:
            return experiment.evaluate(self.config.repo_path)

        if not self._docker_available():
            logger.warning("docker not available; falling back to in-process eval")
            return experiment.evaluate(self.config.repo_path)

        # The experiment's `evaluate` runs inside the container via
        # `python -m hope.evolution._evaluate_in_container <experiment>`.
        # Results come back as JSON on stdout.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "result.json"
            cmd = [
                "docker", "run", "--rm",
                f"--network={self.config.network_mode}",
                "-v", f"{self.config.repo_path}:/workspace:rw",
                "-v", f"{self.config.traces_db}:/readonly/traces.db:ro",
                "-v", f"{tmp}:/out:rw",
                "-e", f"HOPE_EVOLVE_EXPERIMENT={experiment.name}",
                "-e", "HOPE_EVOLVE_RESULT_PATH=/out/result.json",
                self.config.docker_image,
                "python", "-m", "hope.evolution._evaluate_in_container",
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=900)
            except subprocess.CalledProcessError as exc:
                err_tail = (
                    exc.stderr[:500] if exc.stderr else str(exc)
                )
                logger.error("docker eval failed: %s", err_tail)
                stderr_txt = (exc.stderr or b"").decode(errors="replace")
                return EvaluationResult(
                    success=False, score=0.0, tests_passed=False,
                    details={
                        "error": "docker_eval_failed",
                        "stderr": stderr_txt[:2000],
                    },
                )
            except subprocess.TimeoutExpired:
                return EvaluationResult(
                    success=False, score=0.0, tests_passed=False,
                    details={"error": "docker_eval_timeout"},
                )
            if not out_file.exists():
                return EvaluationResult(
                    success=False, score=0.0, tests_passed=False,
                    details={"error": "no_result_written"},
                )
            data = json.loads(out_file.read_text())
            return EvaluationResult(**data)

    # -- Helpers ------------------------------------------------------------

    def _git(self, *args: str) -> str:
        cmd = ["git", "-C", str(self.config.repo_path), *args]
        result = subprocess.run(
            cmd, check=True, capture_output=True, text=True,
        )
        return result.stdout

    def _restore_branch(self, branch: str) -> None:
        """Reset the working tree + delete the experiment branch on reject."""
        try:
            # Best-effort: get back to whatever main-like branch we came from.
            self._git("checkout", "-f", "HEAD")
            # Switch off the experiment branch before deleting.
            head_ref = self._git("symbolic-ref", "--short", "HEAD").strip()
            if head_ref == branch:
                # Detach first so we can delete the branch.
                self._git("checkout", "--detach")
            self._git("branch", "-D", branch)
        except subprocess.CalledProcessError:
            logger.exception("failed to restore branch %s", branch)

    def _docker_available(self) -> bool:
        return shutil.which("docker") is not None and (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5,
            ).returncode == 0
        )

    def _persist(self, result: EvolutionCycleResult) -> EvolutionCycleResult:
        """Write the cycle result to state_dir as JSON (append-only)."""
        out = self.config.state_dir / f"cycle-{result.timestamp}.json"
        out.write_text(json.dumps(result.to_dict(), indent=2, default=str))
        logger.info("evolution: result written to %s (proposed=%s)",
                    out, result.proposed)
        return result

    def _abort(
        self,
        experiment: Experiment,
        ts: str,
        branch: str,
        *,
        reason: str,
        baseline: Optional[float],
        candidate: Optional[float],
        tests: bool,
    ) -> EvolutionCycleResult:
        return self._persist(EvolutionCycleResult(
            experiment=experiment.name,
            timestamp=ts,
            branch=branch,
            merged_branch=None,
            baseline_score=baseline,
            candidate_score=candidate,
            tests_passed=tests,
            proposed=False,
            reason=reason,
        ))

    @staticmethod
    def _format_reject_reason(
        baseline: EvaluationResult,
        candidate: EvaluationResult,
    ) -> str:
        if not candidate.tests_passed:
            return "tests_failed"
        if not candidate.success:
            return f"experiment_unsuccessful: {candidate.details.get('reason', '')}"
        if candidate.score <= baseline.score:
            return (
                f"no_improvement: baseline={baseline.score:.4f} "
                f"candidate={candidate.score:.4f}"
            )
        return "below_margin"

    def list_pending_proposals(self) -> List[Dict[str, Any]]:
        """Return cycle results with ``proposed=True`` that haven't been merged."""
        state = self.config.state_dir
        if not state.exists():
            return []
        out: List[Dict[str, Any]] = []
        for p in sorted(state.glob("cycle-*.json")):
            try:
                data = json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                continue
            if data.get("proposed") and data.get("merged_branch"):
                out.append(data)
        return out

    def last_result(self) -> Optional[Dict[str, Any]]:
        state = self.config.state_dir
        if not state.exists():
            return None
        files = sorted(state.glob("cycle-*.json"))
        if not files:
            return None
        return json.loads(files[-1].read_text())


def _timestamp() -> str:
    """UTC timestamp safe for git branches and filenames."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


__all__ = [
    "DEFAULT_IMAGE",
    "DEFAULT_REPO",
    "DEFAULT_STATE_DIR",
    "EvolutionCycleResult",
    "EvolutionRunner",
    "RunnerConfig",
]
