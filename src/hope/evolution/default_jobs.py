"""Default scheduler jobs contributed by the evolution module.

The sibling agent's ``hope.scheduler.scheduler`` is growing a concept of
"default jobs" that are seeded on first run. This module exposes the
single evolution job that should be installed alongside them.

Contract (negotiated with the scheduler agent):

* ``DEFAULT_JOBS`` is a list of dicts. Each dict is passed through to
  ``TaskScheduler.create_task(**job)``.
* ``id`` is a stable string key (``evolution_run_cycle``) so the
  scheduler can dedupe across restarts.
* The job is disabled by default. The scheduler's seeding code checks
  ``config.evolution.enabled`` before registering it; see
  :func:`should_install`.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Cron: 4 AM daily. Consolidation job (sibling agent) runs at 3 AM; we
# run one hour later so any new trace data it produces is available.
EVOLUTION_CRON = "0 4 * * *"

EVOLUTION_JOB_ID = "evolution_run_cycle"

#: What the scheduler should pass through to ``create_task``. The agent
#: hook (``evolution_agent``) calls :func:`hope.evolution.runner.run_nightly`
#: to pick the next experiment.
DEFAULT_JOBS: List[Dict[str, Any]] = [
    {
        "id": EVOLUTION_JOB_ID,
        "prompt": "evolution:run_cycle",  # intercepted by the evolution agent
        "schedule_type": "cron",
        "schedule_value": EVOLUTION_CRON,
        "agent": "evolution",
        "tools": "",
        "metadata": {
            "description": (
                "Nightly self-evolution cycle. Spins up a sandbox "
                "container, runs one experiment, proposes a merge "
                "branch on improvement. Never auto-merges."
            ),
            "owner": "hope.evolution",
            "default_experiment": "improve_ack_phrases",
            "requires_opt_in": True,
        },
    },
]


def should_install(config: Any) -> bool:
    """True iff the user has opted in via ``[evolution] enabled = true``.

    ``config`` is whatever ``hope.core.config.load_config()`` returns. We
    dig for an ``evolution`` section and respect its ``enabled`` flag;
    default OFF.
    """
    section = getattr(config, "evolution", None)
    if section is None:
        return False
    return bool(getattr(section, "enabled", False))


def run_nightly() -> Dict[str, Any]:
    """Agent entrypoint invoked by the scheduler at 04:00.

    Importable without Docker — returns early with an explanatory dict if
    the runner can't start. The scheduler logs the returned dict.
    """
    from hope.evolution.experiment import ExperimentRegistry
    from hope.evolution.experiments import improve_ack_phrases  # noqa: F401
    from hope.evolution.runner import EvolutionRunner

    runner = EvolutionRunner()
    experiment_name = "improve_ack_phrases"  # phase-1: only experiment
    try:
        cls = ExperimentRegistry.get(experiment_name)
    except KeyError as exc:
        return {"ok": False, "reason": str(exc)}
    result = runner.run_cycle(cls())
    return result.to_dict()


__all__ = [
    "DEFAULT_JOBS",
    "EVOLUTION_CRON",
    "EVOLUTION_JOB_ID",
    "run_nightly",
    "should_install",
]
