"""``hope start`` — bring up the brain daemon (tmux orchestrator + wake monitor).

This verb replaces the legacy "start the API server" behavior for the
personal-assistant mode. It:

1. Checks the daemon PID file at ``~/.hope/daemon.pid`` — if a live process
   owns it, refuses to start (the user must ``hope sleep`` first).
2. Constructs a :class:`~hope.agents.tmux_orchestrator.TmuxOrchestrator`,
   calls ``.start()`` so a ``hope`` tmux session with a hope-main pane
   running ``claude --dangerously-skip-permissions`` is live.
3. Starts the :class:`hope.wakeword.WakeMonitor` unless ``--no-wake``
   is passed (or the module is not importable — we log and continue).
4. Prints the pane id, bus socket path, and control socket path, and
   (unless ``--detach`` is false) runs in the foreground so Ctrl-C →
   clean shutdown.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

from hope.core.config import DEFAULT_CONFIG_DIR

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--no-wake",
    is_flag=True,
    default=False,
    help="Skip WakeMonitor (foreground-only mode — no mic listening).",
)
@click.option(
    "--detach/--foreground",
    default=False,
    help="Fork into the background after start (default: foreground).",
)
def start(no_wake: bool, detach: bool) -> None:
    """Start Hope's brain: tmux orchestrator + wake monitor."""
    console = Console(stderr=True)

    # Late imports so ``hope --help`` stays cheap and tests can patch easily.
    from hope.daemon.core import (
        LOG_FILE,
        PID_FILE,
        HopeDaemon,
        read_pid,
    )

    existing = read_pid(PID_FILE)
    if existing is not None:
        console.print(
            f"[yellow]Hope is already running (PID {existing}).[/yellow]\n"
            "Use 'hope sleep' to stop, then 'hope start' to re-launch."
        )
        sys.exit(1)

    if detach:
        _spawn_detached(no_wake=no_wake, console=console)
        return

    # Foreground mode — we are the daemon.
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    daemon = HopeDaemon(enable_wake=not no_wake)
    try:
        state = daemon.start()
    except Exception as exc:
        console.print(f"[red]Failed to start Hope: {exc}[/red]")
        logger.exception("daemon start failed")
        sys.exit(1)

    console.print(
        "[green]Hope is ready.[/green]\n"
        f"  hope-main pane: [cyan]{state.hope_main_pane_id or '(none)'}[/cyan]\n"
        f"  bus socket:     [cyan]{state.bus_socket}[/cyan]\n"
        f"  control socket: [cyan]{state.control_socket}[/cyan]\n"
        f"  wake monitor:   "
        + ("[cyan]active[/cyan]" if state.wake_monitor_active else "[dim]off[/dim]")
        + f"\n  PID:            [cyan]{state.pid}[/cyan]\n"
        f"  log:            [dim]{LOG_FILE}[/dim]"
    )
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        console.print("[yellow]Received interrupt — shutting down.[/yellow]")
    finally:
        daemon.shutdown()


def _spawn_detached(*, no_wake: bool, console: Console) -> None:
    """Re-exec ``python -m hope.cli start --foreground`` as a background proc."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    from hope.daemon.core import LOG_FILE

    cmd = [sys.executable, "-m", "hope.cli", "start", "--foreground"]
    if no_wake:
        cmd.append("--no-wake")
    log_fh = open(LOG_FILE, "a")  # noqa: SIM115
    proc = subprocess.Popen(  # noqa: S603 — cmd is fully controlled
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(Path.cwd()),
        env=os.environ.copy(),
    )
    console.print(
        f"[green]Hope is launching in the background[/green] (PID {proc.pid})\n"
        f"  log: {LOG_FILE}\n"
        "Use 'hope status' to confirm readiness, 'hope sleep' to stop."
    )


__all__ = ["start"]
