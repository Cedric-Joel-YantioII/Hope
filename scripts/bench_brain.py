"""Quick latency benchmark for BrainSession against the live daemon.

Usage: .venv/bin/python scripts/bench_brain.py [prompt]

Sends a single message into the live hope-main pane and times the round
trip from send-keys → cleaned reply. Used to verify the
BrainSession.POLL_INTERVAL_SEC change without relying on a real mic.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

# Allow running with no editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hope.agents.tmux_orchestrator import TmuxOrchestrator
from hope.core.config import OrchestratorConfig
from hope.voice.brain_session import BrainSession


def daemon_pane_id() -> str:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect("/Users/joelc/.hope/daemon.sock")
    sock.sendall(b'{"cmd":"status"}\n')
    sock.shutdown(socket.SHUT_WR)
    buf = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if b"\n" in chunk:
            break
    sock.close()
    state = json.loads(buf.split(b"\n", 1)[0])["state"]
    return state["hope_main_pane_id"]


def main() -> None:
    msg = " ".join(sys.argv[1:]) or "say the current time in five words or fewer."
    pane_id = daemon_pane_id()
    cfg = OrchestratorConfig()
    # We attach to the existing daemon's tmux session — the orchestrator
    # rebuilds its registry from ~/.hope/agents.db on construction.
    orch = TmuxOrchestrator(config=cfg)
    try:
        session = BrainSession(orch, pane_id, send_timeout_sec=60.0)
        t0 = time.monotonic()
        reply = session.send(msg)
        dt = time.monotonic() - t0
        print(f"[bench] poll_interval_sec={session._poll_interval_sec}")
        print(f"[bench] elapsed={dt:.2f}s")
        print(f"[bench] reply={reply!r}")
    finally:
        try:
            orch.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
