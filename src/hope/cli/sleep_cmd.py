"""``hope sleep`` — graceful shutdown of the Hope daemon.

Prefers the control-socket path (daemon stops its own orchestrator and
wake monitor, removes the PID file, speaks "Hope is sleeping"). Falls
back to SIGTERM on the daemon PID if the socket is missing.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

import click
from rich.console import Console

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Skip the graceful control-socket path; SIGTERM the PID directly.",
)
@click.option(
    "--timeout",
    default=10.0,
    type=float,
    help="Seconds to wait for the daemon process to exit (default: 10).",
)
def sleep(force: bool, timeout: float) -> None:
    """Stop the Hope daemon gracefully."""
    console = Console(stderr=True)
    from hope.daemon.core import (
        CONTROL_SOCKET,
        PID_FILE,
        clear_pid,
        read_pid,
        send_control,
    )

    pid = read_pid(PID_FILE)
    if pid is None:
        console.print("[yellow]Hope is not running.[/yellow]")
        sys.exit(1)

    if not force:
        try:
            resp = send_control("sleep", socket_path=CONTROL_SOCKET)
        except FileNotFoundError:
            resp = None
        except Exception as exc:
            logger.debug("control-socket sleep failed: %s", exc)
            resp = None
        if resp is not None and resp.get("ok"):
            console.print("[green]Sleep request acknowledged.[/green]")
        else:
            force = True  # fall through to SIGTERM path

    if force:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            clear_pid(PID_FILE)
            console.print("[yellow]Daemon process was already gone.[/yellow]")
            return

    # Wait for the process to actually exit.
    deadline = time.time() + max(0.5, timeout)
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            clear_pid(PID_FILE)
            console.print(f"[green]Hope stopped.[/green] (PID {pid})")
            return
        time.sleep(0.25)

    # Still alive — escalate to SIGKILL.
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    clear_pid(PID_FILE)
    console.print(
        f"[yellow]Hope did not exit within {timeout:.1f}s; sent SIGKILL "
        f"to PID {pid}.[/yellow]"
    )


__all__ = ["sleep"]
