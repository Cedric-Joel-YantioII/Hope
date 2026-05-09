"""``hope connect`` -- manage data source connections.

Provides a lightweight onboarding layer over
:mod:`hope.connectors` — list available sources, walk the user through
OAuth / token / filesystem / local setup, and show sync status. The
connector registry auto-populates on import of :mod:`hope.connectors`;
this module stays connector-agnostic except for a hand-tuned wizard
order that promotes Gmail + Apple Notes as first-class onboarding
flows (the two shipping live examples).
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

# Connectors promoted to the top of the interactive wizard. Order matters
# — users onboarding for the first time usually want Gmail first, then
# the offline one (Apple Notes) as a demonstration that Hope works
# without cloud creds too.
_WIZARD_ORDER = ("gmail", "apple_notes")

# Connectors that need heavy external setup (iMessage Full Disk Access,
# Obsidian vault path, etc.) — left as stubs in the wizard so we can say
# "coming soon" rather than leaving the user hanging in a half-broken flow.
_WIZARD_STUBS = {
    "imessage": "Requires macOS Full Disk Access — run `hope connect imessage` manually.",  # noqa: E501
    "obsidian": "Needs --path pointing at your vault — run `hope connect obsidian --path ~/vault`.",  # noqa: E501
    "whatsapp": "Desktop bridge setup is not automated yet.",
    "slack": "Slack connector needs a workspace bot token — configure via env.",
}


def _list_sources(registry: object) -> None:
    """Print a Rich table of registered connectors and their sync status."""
    console = Console()
    items = registry.items()  # type: ignore[attr-defined]

    if not items:
        console.print("[yellow]No connectors registered.[/yellow]")
        return

    table = Table(title="Connected Sources")
    table.add_column("Source", style="cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Status", style="green")

    for key, connector_cls in items:
        # Try to instantiate with no args to check status (best-effort)
        try:
            instance = connector_cls()
            connected = instance.is_connected()
            status = "connected" if connected else "disconnected"
            auth_type = getattr(connector_cls, "auth_type", "unknown")
        except Exception:  # noqa: BLE001
            status = "unknown"
            auth_type = getattr(connector_cls, "auth_type", "unknown")

        table.add_row(key, auth_type, status)

    console.print(table)


def _disconnect_source(registry: object, source: str) -> None:
    """Find and disconnect a registered source connector."""
    console = Console()

    if not registry.contains(source):  # type: ignore[attr-defined]
        console.print(f"[red]Unknown source: {source}[/red]")
        return

    connector_cls = registry.get(source)  # type: ignore[attr-defined]
    try:
        instance = connector_cls()
        instance.disconnect()
        console.print(f"[green]Disconnected {source}.[/green]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Failed to disconnect {source}: {exc}[/red]")


def _connect_source(registry: object, source: str, path: str = "") -> None:
    """Route connector setup by auth_type."""
    console = Console()

    if not registry.contains(source):  # type: ignore[attr-defined]
        console.print(f"[red]Unknown source: {source}[/red]")
        console.print(
            "[yellow]Available sources: "
            + ", ".join(registry.keys())  # type: ignore[attr-defined]
            + "[/yellow]"
        )
        return

    connector_cls = registry.get(source)  # type: ignore[attr-defined]
    auth_type = getattr(connector_cls, "auth_type", "")

    if auth_type == "filesystem":
        # Filesystem connectors (e.g. Obsidian) need a path
        if not path:
            console.print(
                f"[red]{source} requires a --path argument (e.g. --path ~/vault).[/red]"
            )
            return
        try:
            instance = connector_cls(vault_path=path)
        except TypeError:
            try:
                instance = connector_cls(path)
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]Failed to create {source} connector: {exc}[/red]")
                return

        if instance.is_connected():
            console.print(f"[green]{source} connected at path: {path}[/green]")
        else:
            console.print(
                f"[red]{source}: path '{path}' does not exist or is not accessible."
                "[/red]"
            )

    elif auth_type == "oauth":
        # OAuth connectors — auto-open browser + catch callback
        from hope.connectors.oauth import (
            get_client_credentials,
            get_provider_for_connector,
            run_connector_oauth,
            save_client_credentials,
        )

        try:
            instance = connector_cls()
            if instance.is_connected():
                console.print(f"[green]{source} is already connected.[/green]")
                return

            provider = get_provider_for_connector(source)
            if provider is None:
                console.print(f"[red]No OAuth provider configured for {source}.[/red]")
                return

            creds = get_client_credentials(provider)
            client_id = creds[0] if creds else ""
            client_secret = creds[1] if creds else ""

            if not client_id or not client_secret:
                console.print(f"[cyan]First-time setup for {source}.[/cyan]")
                console.print(
                    f"[yellow]Create an OAuth app at: {provider.setup_url}[/yellow]"
                )
                console.print(f"[dim]{provider.setup_hint}[/dim]")
                client_id = click.prompt("Client ID")
                client_secret = click.prompt("Client Secret")
                save_client_credentials(provider, client_id, client_secret)

            run_connector_oauth(source, client_id, client_secret)
            console.print(f"[green]{source} authorised successfully.[/green]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]OAuth flow failed for {source}: {exc}[/red]")

    elif auth_type == "token":
        # Token-based connectors (e.g. Oura) — prompt for personal access token
        import json
        from pathlib import Path

        from hope.connectors.oauth import save_tokens
        from hope.core.config import DEFAULT_CONFIG_DIR

        try:
            instance = connector_cls()
            if instance.is_connected():
                console.print(f"[green]{source} is already connected.[/green]")
                return

            token = click.prompt(f"Enter your {source} personal access token")
            token_dir = Path(DEFAULT_CONFIG_DIR) / "connectors"
            token_dir.mkdir(parents=True, exist_ok=True)
            token_file = token_dir / f"{source}.json"
            token_file.write_text(json.dumps({"token": token}))
            save_tokens(source, {"token": token})
            console.print(f"[green]{source} connected successfully.[/green]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Token setup failed for {source}: {exc}[/red]")

    elif auth_type == "local":
        # Local/OS connectors (e.g. Apple Notes, iMessage) — no creds
        # required, they read a file the OS already owns. The best we can
        # do is report whether the source data is reachable + surface a
        # hint when it isn't (macOS Full Disk Access is a common pitfall).
        try:
            instance = connector_cls()
            if instance.is_connected():
                console.print(
                    f"[green]{source} is reachable (no auth needed).[/green]"
                )
            else:
                console.print(
                    f"[yellow]{source}: data file not found.[/yellow]"
                )
                if source in _WIZARD_STUBS:
                    console.print(f"[dim]{_WIZARD_STUBS[source]}[/dim]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Failed to probe {source}: {exc}[/red]")

    else:
        # Generic / bridge connectors
        try:
            instance = connector_cls()
            connected = instance.is_connected()
            status = "connected" if connected else "disconnected"
            console.print(f"{source} status: {status}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Failed to connect {source}: {exc}[/red]")


def _run_wizard(registry: object) -> None:
    """Walk the user through onboarding — Gmail, then Apple Notes, then others.

    For each wizard-ordered connector we print a one-line description,
    the auth type, and a ``[y/N]`` prompt. On "y" we hand off to
    :func:`_connect_source`. Stubs (iMessage/Obsidian/WhatsApp/Slack) are
    listed with a short "how to set me up manually" hint but skipped in
    the interactive flow so the user isn't blocked.
    """
    console = Console()

    ordered: list[str] = []
    for name in _WIZARD_ORDER:
        if registry.contains(name):  # type: ignore[attr-defined]
            ordered.append(name)
    # Everything else, alphabetical, minus stubs.
    others = sorted(
        k
        for k in registry.keys()  # type: ignore[attr-defined]
        if k not in ordered and k not in _WIZARD_STUBS
    )
    ordered.extend(others)

    if not ordered:
        console.print("[yellow]No connectors installed.[/yellow]")
        return

    console.print(
        "[bold]Hope connector onboarding[/bold] — hit Enter to skip any step."
    )

    for source in ordered:
        connector_cls = registry.get(source)  # type: ignore[attr-defined]
        auth_type = getattr(connector_cls, "auth_type", "unknown")
        try:
            instance = connector_cls()
            already = instance.is_connected()
        except Exception:  # noqa: BLE001
            already = False

        status = "[dim](already connected)[/dim]" if already else ""
        console.print(f"\n[cyan]{source}[/cyan] [{auth_type}] {status}")
        if already:
            continue
        if not click.confirm(f"Connect {source} now?", default=False):
            continue
        _connect_source(registry, source)

    # Print stubs last so the user knows what they're NOT getting today.
    pending = [k for k in _WIZARD_STUBS if registry.contains(k)]  # type: ignore[attr-defined]
    if pending:
        console.print("\n[dim]Deferred (manual setup required):[/dim]")
        for k in pending:
            console.print(f"  [dim]• {k} — {_WIZARD_STUBS[k]}[/dim]")


@click.group(invoke_without_command=True)
@click.argument("source", required=False)
@click.option(
    "--list",
    "list_sources",
    is_flag=True,
    help="List connected sources and sync status.",
)
@click.option(
    "--wizard",
    "run_wizard",
    is_flag=True,
    help="Walk through connector onboarding interactively.",
)
@click.option(
    "--sync",
    "trigger_sync",
    is_flag=True,
    help="Trigger incremental sync for all sources.",
)
@click.option(
    "--disconnect",
    "disconnect_source",
    default="",
    help="Disconnect a source.",
)
@click.option(
    "--path",
    default="",
    help="Path for filesystem connectors (e.g., Obsidian vault).",
)
@click.pass_context
def connect(
    ctx: click.Context,
    source: str | None,
    list_sources: bool,
    run_wizard: bool,
    trigger_sync: bool,
    disconnect_source: str,
    path: str,
) -> None:
    """Manage data source connections (Gmail, Apple Notes, etc.)."""
    # Lazy imports to avoid top-level side effects
    import hope.connectors  # noqa: F401 — registers all connectors
    from hope.core.registry import ConnectorRegistry

    if list_sources:
        _list_sources(ConnectorRegistry)
        return

    if run_wizard:
        _run_wizard(ConnectorRegistry)
        return

    if trigger_sync:
        click.echo("Sync not yet implemented in CLI")
        return

    if disconnect_source:
        _disconnect_source(ConnectorRegistry, disconnect_source)
        return

    if source:
        _connect_source(ConnectorRegistry, source, path=path)
        return

    # No arguments — show help
    click.echo(ctx.get_help())
