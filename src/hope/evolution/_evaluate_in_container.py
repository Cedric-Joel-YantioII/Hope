"""In-container evaluator shim.

Invoked inside the Docker sandbox by :meth:`EvolutionRunner._run_evaluation`.
Reads the experiment name from ``HOPE_EVOLVE_EXPERIMENT``, runs
``evaluate(/workspace)``, and writes a JSON :class:`EvaluationResult` to
``HOPE_EVOLVE_RESULT_PATH``.

Kept deliberately tiny — the heavy lifting lives in each experiment.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import traceback
from pathlib import Path


def _main() -> int:
    # Import experiments package to trigger registration.
    from hope.evolution import experiments  # noqa: F401
    from hope.evolution.experiment import ExperimentRegistry

    name = os.environ.get("HOPE_EVOLVE_EXPERIMENT", "")
    out_path = os.environ.get("HOPE_EVOLVE_RESULT_PATH", "/out/result.json")
    workspace = Path(os.environ.get("HOPE_EVOLVE_WORKSPACE", "/workspace"))

    if not name:
        _write_failure(out_path, "HOPE_EVOLVE_EXPERIMENT not set")
        return 2

    try:
        cls = ExperimentRegistry.get(name)
    except KeyError as exc:
        _write_failure(out_path, f"unknown experiment: {exc}")
        return 2

    try:
        result = cls().evaluate(workspace)
    except Exception:  # noqa: BLE001
        _write_failure(out_path, traceback.format_exc())
        return 1

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(
        json.dumps(dataclasses.asdict(result), default=str)
    )
    return 0


def _write_failure(out_path: str, msg: str) -> None:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps({
        "success": False,
        "score": 0.0,
        "tests_passed": False,
        "details": {"error": msg[:4000]},
        "artifacts": [],
    }))


if __name__ == "__main__":
    sys.exit(_main())
