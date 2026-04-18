"""``hope sleep`` — put the brain to sleep, keep the daemon listening.

Sends a ``sleep`` request over the control socket. The daemon kills the
hope-main pane (and any live specialists) but stays alive to keep
listening for the next wake trigger. Use ``hope stop`` for a full
daemon teardown.
"""

from __future__ import annotations

import logging
import sys

import click
from rich.console import Console

logger = logging.getLogger(__name__)


@click.command()
def sleep() -> None:
    """Put the brain to sleep. Daemon keeps listening for wake."""
    console = Console(stderr=True)
    from hope.daemon.core import CONTROL_SOCKET, PID_FILE, read_pid, send_control

    pid = read_pid(PID_FILE)
    if pid is None:
        console.print("[yellow]Hope is not running.[/yellow]")
        sys.exit(1)

    try:
        resp = send_control("sleep", socket_path=CONTROL_SOCKET)
    except FileNotFoundError:
        console.print(
            "[yellow]Daemon control socket missing. "
            "Use 'hope stop' to force a full shutdown.[/yellow]"
        )
        sys.exit(1)
    except Exception as exc:
        logger.debug("control-socket sleep failed: %s", exc)
        console.print(f"[red]Sleep failed: {exc}[/red]")
        sys.exit(1)

    if resp and resp.get("ok"):
        console.print(
            "[green]Brain is sleeping.[/green] "
            "Daemon still listening — clap twice or say 'Wake up Hope' to wake her."
        )
    else:
        console.print(f"[red]Sleep not acknowledged: {resp}[/red]")
        sys.exit(1)


__all__ = ["sleep"]
