"""Command-line interface for Hope (Click-based)."""

from __future__ import annotations

import click

import hope
from hope.cli.config_cmd import config
from hope.cli.connect_cmd import connect
from hope.cli.digest_cmd import digest
from hope.cli.doctor_cmd import doctor
from hope.cli.init_cmd import init
from hope.cli.memory_cmd import memory
from hope.cli.scheduler_cmd import scheduler
from hope.cli.skill_cmd import skill
from hope.cli.sleep_cmd import sleep as sleep_cmd
from hope.cli.start_cmd import start
from hope.cli.status_cmd import status
from hope.cli.stop_cmd import stop
from hope.cli.vault_cmd import vault
from hope.cli.wake_cmd import wake as wake_cmd


@click.group(help="Hope — local-first voice-interactive personal AI assistant")
@click.version_option(version=hope.__version__, prog_name="hope")
@click.option("--verbose", is_flag=True, default=False, help="Enable debug logging")
@click.option("--quiet", is_flag=True, default=False, help="Suppress non-error output")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, quiet: bool) -> None:
    """Top-level CLI group."""
    from hope.cli.log_config import setup_logging

    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet
    setup_logging(verbose=verbose, quiet=quiet)

    # Check for updates on interactive commands
    if not quiet and ctx.invoked_subcommand:
        from hope.cli._version_check import check_for_updates

        check_for_updates(ctx.invoked_subcommand)


# Core lifecycle
cli.add_command(init, "init")
cli.add_command(start, "start")
cli.add_command(stop, "stop")
cli.add_command(status, "status")
cli.add_command(wake_cmd, "wake")
cli.add_command(sleep_cmd, "sleep")
cli.add_command(doctor, "doctor")

# Configuration & credentials
cli.add_command(config, "config")
cli.add_command(vault, "vault")

# Data / knowledge
cli.add_command(memory, "memory")
cli.add_command(connect, "connect")
cli.add_command(digest, "digest")
cli.add_command(skill, "skill")
cli.add_command(scheduler, "scheduler")

# Sibling-owned commands that may not exist yet
try:
    from hope.cli.evolve_cmd import evolve

    cli.add_command(evolve, "evolve")
except ImportError:
    pass


def main() -> None:
    """Entry point registered as ``hope`` console script."""
    cli()


__all__ = ["cli", "main"]
