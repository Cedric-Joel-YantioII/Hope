"""Single-prompt latency benchmark for the live daemon.

Times one transcript-injection round trip from `speech_transcript`
control-socket call → `[BRAIN ←]` line in the launchd error log.

Usage: .venv/bin/python scripts/bench_single.py "your prompt"
"""

from __future__ import annotations

import json
import socket
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


def wait_for_marker(needle: str, after: int, timeout: float) -> tuple[float, str] | None:
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
    prompt = " ".join(sys.argv[1:]) or (
        "Hope, please reply with exactly: 'Latency probe done.' and nothing else."
    )
    start_size = LOG.stat().st_size
    t0 = time.monotonic()
    send("speech_transcript", {"text": prompt})
    sent_at = time.monotonic() - t0
    heard = wait_for_marker("[→ BRAIN]", after=start_size, timeout=10.0)
    if heard:
        t_brain, _ = heard
        print(f"[bench] transcript→[→ BRAIN]   = {(t_brain - t0)*1000:6.1f} ms")
    reply = wait_for_marker("[BRAIN ←]", after=start_size, timeout=120.0)
    if reply:
        t_reply, line = reply
        print(f"[bench] transcript→[BRAIN ←]    = {(t_reply - t0):6.2f} s")
        print(f"[bench] reply line: {line[:140]}")
    speak = wait_for_marker("[SPEAKING]", after=start_size, timeout=120.0)
    if speak:
        t_spk, line = speak
        print(f"[bench] transcript→[SPEAKING]  = {(t_spk - t0):6.2f} s")
        print(f"[bench] spoken: {line[:140]}")
    print(f"[bench] (control-socket call took {sent_at*1000:.1f} ms)")


if __name__ == "__main__":
    main()
