"""End-to-end latency benchmark for the voice pipeline.

Injects a synthetic transcript through the daemon's control socket and
measures the wall-clock time between submission and the brain's reply
appearing in the daemon's stderr stream. Assumes the daemon was
launched via launchd (logs land in ``~/.hope/daemon-launchd.err.log``).

Usage:
  .venv/bin/python scripts/bench_voice_pipeline.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

LOG = Path("/Users/joelc/.hope/daemon-launchd.err.log")
SOCK = Path("/Users/joelc/.hope/daemon.sock")


def send(cmd: str, payload: dict | None = None) -> dict:
    sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sk.connect(str(SOCK))
    msg = {"cmd": cmd}
    if payload:
        msg["payload"] = payload
    sk.sendall((json.dumps(msg) + "\n").encode())
    sk.shutdown(socket.SHUT_WR)
    buf = b""
    while True:
        chunk = sk.recv(4096)
        if not chunk:
            break
        buf += chunk
        if b"\n" in chunk:
            break
    sk.close()
    return json.loads(buf.split(b"\n", 1)[0])


def wait_for_marker(needle: str, after: int, timeout: float = 60.0) -> tuple[float, str] | None:
    """Return (wall_seconds_since_call, line) when *needle* appears in the
    log AFTER byte offset *after*. Polls the file every 50 ms.
    """
    deadline = time.monotonic() + timeout
    last_size = after
    while time.monotonic() < deadline:
        try:
            sz = LOG.stat().st_size
        except FileNotFoundError:
            return None
        if sz > last_size:
            with LOG.open("rb") as fh:
                fh.seek(last_size)
                chunk = fh.read(sz - last_size).decode(errors="replace")
            for line in chunk.splitlines():
                if needle in line:
                    return time.monotonic(), line
            last_size = sz
        time.sleep(0.05)
    return None


def main() -> None:
    prompts = [
        "Hope, please reply with exactly: 'One, sir.' and nothing else.",
        "Hope, please reply with exactly: 'Two, sir.' and nothing else.",
        "Hope, please reply with exactly: 'Three, sir.' and nothing else.",
    ]
    deltas = []
    for prompt in prompts:
        start_size = LOG.stat().st_size
        t0 = time.monotonic()
        send("speech_transcript", {"text": prompt})
        result = wait_for_marker("[BRAIN ←]", after=start_size, timeout=120.0)
        if result is None:
            print(f"[bench] TIMEOUT for prompt: {prompt!r}")
            continue
        t1, line = result
        dt = t1 - t0
        deltas.append(dt)
        print(f"[bench] {dt:5.2f}s  {line[:120]}")
        # Wait for the speaking ack to clear before the next one (echo guard).
        time.sleep(8.0)
    if deltas:
        avg = sum(deltas) / len(deltas)
        print(f"[bench] avg = {avg:.2f}s over {len(deltas)} runs")


if __name__ == "__main__":
    main()
