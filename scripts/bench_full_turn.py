"""Full-turn benchmark — measures every audible event in a single voice turn.

Times: transcript injection → ack TTS start → brain reply → reply TTS start.
Tails the launchd err log for [HEARD] / [→ BRAIN] / [BRAIN ←] / [SPEAKING]
markers AND parses SPEAKING_STARTED bus events to get the actual audio-start
moment.

Usage: .venv/bin/python scripts/bench_full_turn.py "your prompt"
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


def grep_after(needle: str, after: int, timeout: float) -> tuple[float, str, int] | None:
    """Wait for *needle*, return (monotonic_time, line, end_byte)."""
    deadline = time.monotonic() + timeout
    last = after
    while time.monotonic() < deadline:
        sz = LOG.stat().st_size
        if sz > last:
            with LOG.open("rb") as fh:
                fh.seek(last)
                chunk = fh.read(sz - last).decode(errors="replace")
            offset = last
            for line in chunk.splitlines(keepends=True):
                if needle in line:
                    return time.monotonic(), line.rstrip(), offset + len(line)
                offset += len(line)
            last = sz
        time.sleep(0.02)
    return None


def main() -> None:
    prompt = " ".join(sys.argv[1:]) or (
        "Hope, in three words: name a fruit."
    )

    start_size = LOG.stat().st_size
    t0 = time.monotonic()
    control("speech_transcript", {"text": prompt})

    heard = grep_after("[→ BRAIN]", start_size, timeout=10.0)
    if heard:
        t, _, off = heard
        print(f"[t+{(t - t0)*1000:6.1f}ms] dispatched to brain")
        cursor = off
    else:
        print("[bench] FAIL — never saw [→ BRAIN]")
        return

    # The [ACK] line fires when the canned ack worker speaks.
    ack = grep_after("[ACK]", cursor, timeout=15.0)
    if ack:
        t, line, _ = ack
        print(f"[t+{(t - t0)*1000:6.1f}ms] ACK spoken: {line.split('[ACK]')[1].strip()[:80]}")

    reply = grep_after("[BRAIN ←]", cursor, timeout=120.0)
    if reply:
        t, line, off = reply
        print(f"[t+{(t - t0):6.2f}s ] brain reply: {line.split('[BRAIN ←]')[1].strip()[:80]}")
        cursor2 = off
        spoken = grep_after("[SPEAKING]", cursor2, timeout=15.0)
        if spoken:
            t2, line2, _ = spoken
            print(f"[t+{(t2 - t0):6.2f}s ] reply TTS: {line2.split('[SPEAKING]')[1].strip()[:80]}")
    print()
    print("Pipeline test complete.")


if __name__ == "__main__":
    main()
