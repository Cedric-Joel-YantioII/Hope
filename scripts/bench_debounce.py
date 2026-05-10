"""Verify the daemon's debounce-and-merge by injecting two transcripts close together.

Sends segment 1, then segment 2 a configurable number of milliseconds later,
both via the control socket's ``speech_transcript`` injection. Watches the
launchd error log for ``[DEBOUNCE-MERGE]`` / ``[DEBOUNCE-FLUSH]`` markers
and the eventual ``[→ BRAIN]`` line so the operator can see what got
shipped vs. shipped-separately.

Usage:
  .venv/bin/python scripts/bench_debounce.py           # default 200 ms gap
  .venv/bin/python scripts/bench_debounce.py 200 800   # 200 ms then 800 ms gap
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

LOG = Path("/Users/joelc/.hope/daemon-launchd.err.log")
SOCK = Path("/Users/joelc/.hope/daemon.sock")


def control(cmd: str, payload: dict | None = None) -> dict:
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


def grep_after(needle: str, after: int, timeout: float):
    deadline = time.monotonic() + timeout
    last = after
    while time.monotonic() < deadline:
        sz = LOG.stat().st_size
        if sz > last:
            with LOG.open("rb") as fh:
                fh.seek(last)
                chunk = fh.read(sz - last).decode(errors="replace")
            for line in chunk.splitlines():
                if needle in line:
                    return time.monotonic(), line
            last = sz
        time.sleep(0.02)
    return None


def main() -> None:
    gap_ms = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    seg_a = "Hope, can you find me"
    seg_b = "the latest news on AI"
    print(f"[bench] gap = {gap_ms} ms (debounce SEC default 0.5)")

    start = LOG.stat().st_size
    t0 = time.monotonic()
    control("speech_transcript", {"text": seg_a})
    time.sleep(gap_ms / 1000.0)
    control("speech_transcript", {"text": seg_b})

    merge = grep_after("[DEBOUNCE-MERGE]", start, timeout=5.0)
    flush = grep_after("[DEBOUNCE-FLUSH]", start, timeout=5.0)
    brain = grep_after("[→ BRAIN]", start, timeout=10.0)

    if merge:
        t, line = merge
        print(f"[t+{(t - t0)*1000:6.1f}ms] {line[:140]}")
    else:
        print("[bench] NO MERGE detected — segments dispatched separately")
    if flush:
        t, line = flush
        print(f"[t+{(t - t0)*1000:6.1f}ms] {line[:140]}")
    if brain:
        t, line = brain
        print(f"[t+{(t - t0)*1000:6.1f}ms] {line[:140]}")


if __name__ == "__main__":
    main()
