#!/usr/bin/env python3
"""Nightly memory consolidation entry point.

Wire this into cron / launchd:

    0 3 * * *  /usr/bin/env python3 /path/to/Hope/scripts/hope_consolidate.py

The script is deliberately thin: it discovers registered memory
backends, calls :func:`hope.tools.storage.consolidation.run`, and prints
a JSON report to stdout for log scraping.  Everything interesting lives
inside the library so it can also be invoked in-process.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import List

from hope.tools.storage import consolidation
from hope.tools.storage._stubs import MemoryBackend


def _discover_backends() -> List[MemoryBackend]:
    """Return registered memory backends as a list.

    We intentionally only pick up backends that have been explicitly
    instantiated against the global registry — a cron invocation should
    NOT spin up a new SQLite file or an Ollama connection as a side
    effect.  If nothing is registered we return an empty list and the
    consolidation run becomes a no-op.
    """
    try:
        from hope.core.registry import MemoryRegistry
    except Exception:  # pragma: no cover - defensive
        return []

    get_instances = getattr(MemoryRegistry, "instances", None)
    if callable(get_instances):
        return list(get_instances())
    # Registry only has classes, not instances — nothing to do.
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--flush-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the async embed queue to drain.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    backends = _discover_backends()
    report = consolidation.run(
        backends,
        flush_timeout_s=args.flush_timeout,
    )
    json.dump(report.as_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    # Non-zero exit on error so cron can alert.
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
