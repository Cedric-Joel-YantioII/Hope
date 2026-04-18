"""``hope wake`` — manually publish a WAKE_TRIGGER to a running daemon.

If the daemon isn't running, this verb transparently falls back to
``hope start --detach`` semantics so ``hope wake`` on a cold machine
"just works". Otherwise it opens the control socket, sends
``{"cmd": "wake", "payload": {"source": "manual"}}`` and prints the reply.
"""

from __future__ import annotations

import logging
import sys

import click
from rich.console import Console

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--text",
    default=None,
    help="Optional text payload (e.g. the transcript that triggered wake).",
)
@click.option(
    "--source",
    default="manual",
    type=click.Choice(["manual", "voice", "clap"], case_sensitive=False),
    help="Source tag for the WAKE_TRIGGER event.",
)
def wake(text: str | None, source: str) -> None:
    """Send a WAKE_TRIGGER to the daemon; start it first if it isn't running."""
    console = Console(stderr=True)
    from hope.daemon.core import (
        CONTROL_SOCKET,
        PID_FILE,
        read_pid,
        send_control,
    )

    pid = read_pid(PID_FILE)
    if pid is None:
        console.print(
            "[yellow]Daemon not running — starting Hope in the background.[/yellow]"
        )
        # Delegate to `hope start --detach` so the start-semantics live
        # in exactly one place.
        from hope.cli.start_cmd import _spawn_detached

        _spawn_detached(no_wake=False, console=console)
        return

    payload = {"source": source.lower()}
    if text:
        payload["text"] = text

    try:
        resp = send_control("wake", payload, socket_path=CONTROL_SOCKET)
    except FileNotFoundError:
        console.print(
            f"[red]Daemon PID {pid} is live but control socket is missing.[/red]\n"
            "Try 'hope sleep' then 'hope start'."
        )
        sys.exit(1)
    except Exception as exc:
        console.print(f"[red]Wake failed: {exc}[/red]")
        sys.exit(1)

    if resp.get("ok"):
        console.print("[green]Wake trigger sent.[/green]")
    else:
        console.print(
            f"[red]Daemon rejected wake:[/red] {resp.get('error', resp)}"
        )
        sys.exit(1)


__all__ = ["wake"]
