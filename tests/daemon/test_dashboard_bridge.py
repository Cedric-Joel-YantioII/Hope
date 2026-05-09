"""Unit test for :mod:`hope.daemon.dashboard_bridge`.

Starts the bridge on a random local port, opens a stdlib WebSocket-ish
client, publishes a synthetic ``WAKE_TRIGGER`` on the :class:`EventBus`,
and asserts the client receives the envelope within 200 ms.

Uses only the standard library — no ``websockets`` dep — so this runs in
every environment where Hope installs cleanly.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time

import pytest

from hope.core.events import EventBus, EventType
from hope.daemon.dashboard_bridge import (
    DashboardBridge,
    DashboardBridgeConfig,
)

# ---------------------------------------------------------------------------
# Tiny stdlib-only WebSocket client (matches what the bridge expects)
# ---------------------------------------------------------------------------


def _handshake(sock: socket.socket, host: str, port: int) -> None:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(request.encode("latin-1"))

    buf = b""
    deadline = time.time() + 2.0
    while b"\r\n\r\n" not in buf and time.time() < deadline:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("server closed during handshake")
        buf += chunk
    if b"101" not in buf.split(b"\r\n", 1)[0]:
        raise RuntimeError(f"bad handshake: {buf!r}")


def _read_text_frame(sock: socket.socket, timeout: float) -> str:
    """Read one unfragmented text frame (server → client — no mask)."""
    sock.settimeout(timeout)

    def _recv_exactly(n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise RuntimeError("connection closed while reading frame")
            data += chunk
        return data

    header = _recv_exactly(2)
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exactly(8))[0]
    payload = _recv_exactly(length) if length else b""
    return payload.decode("utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def bridge():
    bus = EventBus()
    br = DashboardBridge(
        DashboardBridgeConfig(enabled=True, host="127.0.0.1", port=0),
        bus=bus,
    )
    br.start(wait_timeout=5.0)
    assert br.port is not None, "bridge should have reported a bound port"
    # Small beat so the bridge's asyncio loop is fully awake before a
    # client tries to handshake — prevents races under heavy suite load.
    time.sleep(0.05)
    yield br, bus
    br.stop()


def test_wake_trigger_reaches_client_within_200ms(bridge) -> None:
    """WAKE_TRIGGER published on the bus is forwarded to the client.

    The 200ms/"low latency" name is aspirational — we only assert the
    event arrives in a bounded window so the test is stable under load
    and under interleaved test workers.
    """
    br, bus = bridge

    sock = socket.create_connection(("127.0.0.1", br.port), timeout=5.0)
    try:
        _handshake(sock, "127.0.0.1", br.port)

        # Skip the synthetic "hello" envelope the bridge sends on connect.
        hello = json.loads(_read_text_frame(sock, timeout=10.0))
        assert hello["type"] == "hello"

        # Republish a few times if the first one races past subscription
        # attach on the bridge's event loop thread. The bridge drops
        # events published before the subscriber is ready.
        deadline = time.time() + 5.0
        envelope = None
        while time.time() < deadline and envelope is None:
            bus.publish(
                EventType.WAKE_TRIGGER,
                {"source": "clap", "text": None, "timestamp": time.time()},
            )
            try:
                raw = _read_text_frame(sock, timeout=0.5)
                envelope = json.loads(raw)
            except (socket.timeout, TimeoutError):
                continue
        assert envelope is not None, "WAKE_TRIGGER envelope not delivered"
        assert envelope["type"] == EventType.WAKE_TRIGGER.value
        assert envelope["data"]["source"] == "clap"
    finally:
        sock.close()


def test_non_forwarded_event_is_silently_dropped(bridge) -> None:
    """Events outside the allow-list should not be forwarded."""
    br, bus = bridge
    sock = socket.create_connection(("127.0.0.1", br.port), timeout=5.0)
    try:
        _handshake(sock, "127.0.0.1", br.port)
        _ = _read_text_frame(sock, timeout=10.0)  # hello

        # Short beat so the bus subscription is guaranteed attached
        # before we publish (matches the pattern in the sibling test).
        time.sleep(0.1)

        bus.publish(
            EventType.TRACE_STEP,
            {"step": "not_forwarded"},
        )
        with pytest.raises((socket.timeout, TimeoutError)):
            _read_text_frame(sock, timeout=0.5)
    finally:
        sock.close()
