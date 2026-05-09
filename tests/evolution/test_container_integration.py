"""Integration test: actually spin up the evolve container.

Skipped unless Docker is reachable AND the ``hope-evolve:latest`` image is
already built (we don't build it in-test because that's a 5-minute step).
Local dev: ``docker build -f deploy/docker/Dockerfile.evolve -t hope-evolve:latest .``
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _image_present(tag: str) -> bool:
    r = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    )
    return r.returncode == 0


pytestmark = pytest.mark.skipif(
    not (_docker_ok() and _image_present("hope-evolve:latest")),
    reason="requires Docker and the hope-evolve:latest image",
)


def test_container_can_import_hope(tmp_path: Path) -> None:
    """Sanity check — the container boots and `import hope` works."""
    repo = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        [
            "docker", "run", "--rm",
            "--network=none",
            "-v", f"{repo}:/workspace:rw",
            "hope-evolve:latest",
            "python", "-c", "import hope; print(hope.__version__)",
        ],
        capture_output=True, text=True, timeout=600,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip()  # got *some* version string


def test_container_has_no_network(tmp_path: Path) -> None:
    """--network=none must be honored — no egress."""
    repo = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        [
            "docker", "run", "--rm",
            "--network=none",
            "-v", f"{repo}:/workspace:rw",
            "hope-evolve:latest",
            "python", "-c",
            "import socket; socket.create_connection(('1.1.1.1', 53), timeout=2)",
        ],
        capture_output=True, text=True, timeout=60,
    )
    # We expect failure (network unreachable) — not success.
    assert r.returncode != 0
