"""``hope evolve`` — control surface for the self-evolution sandbox.

Subcommands:

* ``list-experiments`` — all registered :class:`Experiment` subclasses.
* ``run <name>`` — execute one cycle manually (dev / debug).
* ``status`` — last cycle outcome + any pending merge proposals.
* ``approve <branch>`` — human-gated merge of ``evolve/merged-<ts>`` into
  ``main`` via squash.

No subcommand auto-merges. Approval is always explicit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table


@click.group(help="Manage Hope's nightly self-evolution sandbox.")
def evolve() -> None:
    """Top-level ``hope evolve`` group."""


@evolve.command("list-experiments")
def list_experiments() -> None:
    """List registered experiments."""
    from hope.evolution import experiments  # noqa: F401 — trigger registration
    from hope.evolution.experiment import ExperimentRegistry

    console = Console()
    all_exps = ExperimentRegistry.all()
    if not all_exps:
        console.print("[dim]No experiments registered.[/dim]")
        return

    table = Table(title="Evolution experiments")
    table.add_column("name", style="cyan")
    table.add_column("description")
    for name, cls in sorted(all_exps.items()):
        table.add_row(name, cls.description or "—")
    console.print(table)


@evolve.command("run")
@click.argument("name")
@click.option(
    "--no-docker",
    is_flag=True,
    default=False,
    help="Run evaluate() in-process instead of spinning up the container. "
         "Useful for dev.",
)
def run(name: str, no_docker: bool) -> None:
    """Run one evolution cycle for experiment NAME."""
    from hope.evolution import experiments  # noqa: F401 — trigger registration
    from hope.evolution.experiment import ExperimentRegistry
    from hope.evolution.runner import EvolutionRunner, RunnerConfig

    console = Console()
    try:
        cls = ExperimentRegistry.get(name)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise click.exceptions.Exit(2) from exc

    cfg = RunnerConfig(use_docker=not no_docker)
    runner = EvolutionRunner(cfg)
    console.print(
        f"[yellow]Running experiment {name!r} (docker={not no_docker})...[/yellow]"
    )
    result = runner.run_cycle(cls())

    _print_cycle_result(console, result.to_dict())
    if result.proposed:
        console.print(
            f"\n[green]Proposal branch:[/green] {result.merged_branch}\n"
            f"Approve with: "
            f"[cyan]hope evolve approve {result.merged_branch}[/cyan]"
        )


@evolve.command("status")
def status() -> None:
    """Show last cycle outcome + any pending merge proposals."""
    from hope.evolution.runner import EvolutionRunner

    console = Console()
    runner = EvolutionRunner()

    last = runner.last_result()
    if last is None:
        console.print("[dim]No evolution cycles have run yet.[/dim]")
    else:
        console.print("[bold]Last cycle:[/bold]")
        _print_cycle_result(console, last)

    pending = runner.list_pending_proposals()
    if not pending:
        console.print("\n[dim]No pending merge proposals.[/dim]")
        return

    table = Table(title="Pending proposals")
    table.add_column("timestamp", style="cyan")
    table.add_column("experiment")
    table.add_column("baseline", justify="right")
    table.add_column("candidate", justify="right")
    table.add_column("branch", style="green")
    for p in pending:
        table.add_row(
            str(p.get("timestamp", "")),
            str(p.get("experiment", "")),
            _fmt_score(p.get("baseline_score")),
            _fmt_score(p.get("candidate_score")),
            str(p.get("merged_branch", "")),
        )
    console.print(table)
    console.print(
        "\nApprove one with: [cyan]hope evolve approve <branch>[/cyan]"
    )


@evolve.command("approve")
@click.argument("branch")
@click.option(
    "--target",
    default="main",
    help="Branch to squash-merge into (default: main).",
)
@click.option(
    "--repo",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Repository root (default: discovered from cwd).",
)
def approve(branch: str, target: str, repo: Optional[Path]) -> None:
    """Human-gated squash-merge of BRANCH into TARGET.

    Phase-1 safety: ``hope evolve`` never merges on its own. The nightly
    cron only creates proposal branches; this command is the only path
    from proposal to ``main``.
    """
    from hope.evolution.merge import MergeError, approve_and_merge
    from hope.evolution.runner import DEFAULT_REPO

    console = Console()
    repo_path = repo or DEFAULT_REPO
    console.print(
        f"[yellow]Squash-merging {branch} → {target} "
        f"(repo={repo_path})[/yellow]"
    )
    try:
        sha = approve_and_merge(
            branch,
            repo_path=repo_path,
            target_branch=target,
            squash=True,
        )
    except MergeError as exc:
        console.print(f"[red]MergeError: {exc}[/red]")
        raise click.exceptions.Exit(1) from exc

    console.print(f"[green]Merged as {sha[:12]}[/green]")
    console.print(
        "[dim]Not pushed. Review the commit, then `git push` manually.[/dim]"
    )


# -- helpers ----------------------------------------------------------------


def _print_cycle_result(console: Console, data: dict) -> None:
    """Pretty-print a cycle-result dict."""
    proposed = data.get("proposed")
    color = "green" if proposed else "yellow"
    console.print(
        f"  experiment: [cyan]{data.get('experiment')}[/cyan]\n"
        f"  timestamp:  {data.get('timestamp')}\n"
        f"  branch:     {data.get('branch')}\n"
        f"  baseline:   {_fmt_score(data.get('baseline_score'))}\n"
        f"  candidate:  {_fmt_score(data.get('candidate_score'))}\n"
        f"  tests:      {data.get('tests_passed')}\n"
        f"  proposed:   [{color}]{proposed}[/{color}]\n"
        f"  reason:     {data.get('reason')}"
    )
    details = data.get("details") or {}
    if details:
        console.print(
            f"  details:    [dim]{json.dumps(details)[:400]}...[/dim]"
        )


def _fmt_score(x: object) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):.4f}"
    except (TypeError, ValueError):
        return str(x)


__all__ = ["evolve"]
