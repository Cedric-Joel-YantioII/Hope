"""Time the cold wake flow: voice phrase → instant ack → brain spawn → greeting.

The goal is that the user hears Hope respond within ~1 s of saying
"Hope", not 6–12 s later when Claude Code has finished its cold load.
"""

from __future__ import annotations

import json
import socket
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
    state = control("status")
    if state["state"]["orchestrator_started"]:
        print("[bench] WARNING — brain already up. Call sleep first for a cold test.")
    else:
        print(f"[bench] Confirmed cold start (orchestrator_started=False)")

    start_size = LOG.stat().st_size
    t0 = time.monotonic()
    # Inject a wake-phrase transcript via the bus. The PhraseMatcher
    # will see this on its SPEECH_TRANSCRIPT subscription and fire a
    # WAKE_TRIGGER, exercising the same path as a real spoken wake.
    control("speech_transcript", {"text": "Hope, hello there."})
    print(f"[t+    0 ms] injected: 'Hope, hello there.'")

    wake = grep_after("WAKE_TRIGGER received", start_size, timeout=5.0)
    if wake:
        t, _ = wake
        print(f"[t+{(t - t0)*1000:5.0f} ms] WAKE_TRIGGER received")

    # First [SPEAKING] line we see should be the instant wake-ack.
    speaking_first = grep_after("SPEAKING_STARTED", start_size, timeout=10.0)
    if speaking_first:
        t, line = speaking_first
        print(f"[t+{(t - t0)*1000:5.0f} ms] FIRST audio start (this is what the user hears)")

    # The Claude Code pane spawn finishes when we see the greeting line.
    greeting = grep_after("hope-main pane=", start_size, timeout=60.0)
    if greeting:
        t, line = greeting
        print(f"[t+{(t - t0):5.2f} s ] brain pane up")


if __name__ == "__main__":
    main()
