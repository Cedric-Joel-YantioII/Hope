"""``hope status`` — report daemon + orchestrator + wake monitor state.

If the daemon is up we prefer the live control-socket snapshot; if the
socket isn't responding we fall back to whatever the PID file tells us.
Supports ``--json`` for machine consumption (used by tests).
"""

from __future__ import annotations

import json as _json
import logging
import sys
import time
from typing import Any, Dict, Optional

import click
from rich.console import Console

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a single JSON object to stdout instead of human text.",
)
def status(as_json: bool) -> None:
    """Show current Hope daemon status."""
    console = Console(stderr=True) if not as_json else Console()

    from hope.daemon.core import (
        CONTROL_SOCKET,
        PID_FILE,
        read_pid,
        send_control,
    )

    pid = read_pid(PID_FILE)
    state: Optional[Dict[str, Any]] = None
    control_error: Optional[str] = None

    if pid is not None:
        try:
            resp = send_control("status", socket_path=CONTROL_SOCKET)
            if resp.get("ok"):
                state = resp.get("state", {})
            else:
                control_error = resp.get("error", "status rejected")
        except FileNotFoundError:
            control_error = "control socket missing"
        except Exception as exc:
            control_error = f"{exc}"

    if as_json:
        payload = {
            "running": pid is not None,
            "pid": pid,
            "state": state,
            "control_error": control_error,
            "queried_at": time.time(),
        }
        click.echo(_json.dumps(payload, indent=2))
        return

    if pid is None:
        console.print("[yellow]Hope is not running.[/yellow]")
        # Exit 0 — status is a query, not a gate.
        return

    if state is None:
        console.print(
            f"[yellow]Hope PID {pid} is alive but status query failed:[/yellow] "
            f"{control_error or 'unknown error'}"
        )
        sys.exit(0)

    lines = [
        f"[green]Hope is running[/green] (PID {state.get('pid', pid)})",
        f"  hope-main pane:    [cyan]{state.get('hope_main_pane_id') or '(none)'}[/cyan]",
        "  orchestrator:      "
        + (
            "[green]started[/green]"
            if state.get("orchestrator_started")
            else "[yellow]not started[/yellow]"
        ),
        f"  live specialists:  [cyan]{state.get('specialist_count', 0)}[/cyan]",
        f"  queued spawns:     [cyan]{state.get('queued_spawn_count', 0)}[/cyan]",
        "  wake monitor:      "
        + _format_wake(state.get("wake_monitor_available"), state.get("wake_monitor_active")),
        f"  bus socket:        [dim]{state.get('bus_socket') or '(none)'}[/dim]",
        f"  control socket:    [dim]{state.get('control_socket') or '(none)'}[/dim]",
    ]
    console.print("\n".join(lines))


def _format_wake(available: Any, active: Any) -> str:
    if active:
        return "[green]active[/green]"
    if available:
        return "[yellow]idle[/yellow]"
    return "[dim]unavailable[/dim]"


__all__ = ["status"]
