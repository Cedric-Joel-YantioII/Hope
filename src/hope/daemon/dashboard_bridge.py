"""WebSocket bridge that streams :class:`hope.core.events.EventBus` events
to the Tauri dashboard frontend.

Design
------

* Single consumer (the local Tauri app), single publisher (the Hope daemon).
* Loopback-only (``127.0.0.1``) — never binds to an external interface.
* Stdlib-only protocol implementation (no ``websockets`` dep). RFC 6455 text
  frames, ≤ 64 KiB payloads, no permessage-deflate, no fragmentation.
  That is enough for the dashboard: every message is a small JSON line.
* The bridge subscribes to a curated allow-list of ``EventType`` values —
  all events related to wake, brain lifecycle, speech, and specialist panes —
  and broadcasts them as JSON lines to every connected client.
* Safe to call from any thread: ``EventBus`` subscribers run on the
  publisher's thread, so we push the envelope onto an asyncio queue that
  the event-loop thread drains at its own pace.

Message envelope
----------------

Every broadcast line is a single JSON object::

    {
      "type": "wake_trigger" | "pane_spawned" | ...,
      "timestamp": 1_700_000_000.123,
      "data": { ...event.data... }
    }

A synthetic ``"hello"`` event is sent on every new connection so the
frontend can immediately render current state on reconnect.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from hope.core.events import Event, EventBus, EventType, get_event_bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RFC 6455 constants
# ---------------------------------------------------------------------------

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OPCODE_TEXT = 0x1
_OPCODE_CLOSE = 0x8
_OPCODE_PING = 0x9
_OPCODE_PONG = 0xA

# Events the dashboard cares about. We intentionally do NOT forward
# high-volume events (e.g. TRACE_STEP) so the frontend stays snappy.
_FORWARDED_EVENTS: tuple[EventType, ...] = (
    EventType.WAKE_TRIGGER,
    EventType.LISTENING_PAUSED,
    EventType.LISTENING_RESUMED,
    EventType.SPEECH_TRANSCRIPT,
    EventType.PANE_SPAWNED,
    EventType.PANE_KILLED,
    EventType.PANE_MESSAGE,
    EventType.SPECIALIST_AT_CAPACITY,
    EventType.MEMORY_STORE,
    EventType.MEMORY_RETRIEVE,
    EventType.SCHEDULER_TASK_START,
    EventType.SCHEDULER_TASK_END,
    EventType.AGENT_TURN_START,
    EventType.AGENT_TURN_END,
    EventType.SPEAKING_STARTED,
    EventType.SPEAKING_ENDED,
    EventType.INFERENCE_START,
    EventType.INFERENCE_END,
)


@dataclass
class DashboardBridgeConfig:
    """Runtime knobs for :class:`DashboardBridge`."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    max_queue: int = 1024
    forwarded_events: tuple[EventType, ...] = field(
        default_factory=lambda: _FORWARDED_EVENTS
    )


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _serialise_event(event: Event) -> Dict[str, Any]:
    """Convert an :class:`Event` to a JSON-safe dict."""
    return {
        # Scheduler publishes bare-string event types instead of EventType
        # enum members — accept both.
        "type": getattr(event.event_type, "value", event.event_type),
        "timestamp": event.timestamp,
        "data": _json_safe(event.data),
    }


def _json_safe(value: Any) -> Any:
    """Best-effort coercion of nested values to JSON-safe primitives."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return repr(value)


# ---------------------------------------------------------------------------
# Minimal RFC 6455 framing (server → single local client)
# ---------------------------------------------------------------------------


def _build_accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _encode_text_frame(payload: bytes) -> bytes:
    """Encode a single unfragmented text frame with no mask (server → client)."""
    header = bytearray()
    header.append(0x80 | _OPCODE_TEXT)  # FIN + opcode
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header.extend(struct.pack(">H", length))
    else:
        header.append(127)
        header.extend(struct.pack(">Q", length))
    return bytes(header) + payload


def _encode_close_frame(code: int = 1000) -> bytes:
    body = struct.pack(">H", code)
    header = bytes([0x80 | _OPCODE_CLOSE, len(body)])
    return header + body


async def _read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
    data = await reader.readexactly(n)
    return data


async def _read_frame(reader: asyncio.StreamReader) -> Optional[tuple[int, bytes]]:
    """Return ``(opcode, payload)`` for the next frame or ``None`` on EOF.

    Supports the subset the dashboard needs: short/medium/long length,
    client-masked frames, no fragmentation handling (client sends only
    pings / closes which fit in a single frame).
    """
    try:
        header = await _read_exactly(reader, 2)
    except asyncio.IncompleteReadError:
        return None
    b0, b1 = header[0], header[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", await _read_exactly(reader, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await _read_exactly(reader, 8))[0]
    mask = b""
    if masked:
        mask = await _read_exactly(reader, 4)
    payload = await _read_exactly(reader, length) if length else b""
    if masked and payload:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


# ---------------------------------------------------------------------------
# DashboardBridge
# ---------------------------------------------------------------------------


class DashboardBridge:
    """WebSocket broadcaster wrapping the daemon's :class:`EventBus`.

    Lifecycle:

    * :meth:`start` — subscribes to the bus, spins up an asyncio event
      loop on a daemon thread, opens the listening socket.
    * :meth:`stop` — unsubscribes, closes sockets, joins the thread.

    Thread safety: ``EventBus`` publishes on arbitrary threads; we funnel
    each serialised envelope into ``asyncio.Queue`` via
    :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`.
    """

    def __init__(
        self,
        config: Optional[DashboardBridgeConfig] = None,
        bus: Optional[EventBus] = None,
        state_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        self._cfg = config or DashboardBridgeConfig()
        self._bus = bus or get_event_bus()
        # Called on every client connect to get the daemon's current
        # state (listening_paused, brain_state, hope_main_pane_id, ...).
        # We send the result as a `state_snapshot` event so the frontend
        # hydrates the store with reality instead of React defaults.
        self._state_provider = state_provider
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self._clients: Set[asyncio.Queue[Optional[bytes]]] = set()
        self._clients_lock = threading.Lock()
        self._started = threading.Event()
        self._stopping = threading.Event()
        self._actual_port: Optional[int] = None

    # -- public --------------------------------------------------------------

    @property
    def port(self) -> Optional[int]:
        """The port we're actually bound to (useful when ``port=0``)."""
        return self._actual_port

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    def start(self, *, wait_timeout: float = 2.0) -> None:
        """Spin up the bridge on a background thread. Idempotent."""
        if self._started.is_set() or not self._cfg.enabled:
            return
        self._thread = threading.Thread(
            target=self._run, name="hope-dashboard-bridge", daemon=True
        )
        self._thread.start()
        if not self._started.wait(timeout=wait_timeout):
            logger.warning("dashboard bridge did not report ready within %.1fs", wait_timeout)

        # Subscribe once the loop is up so we know call_soon_threadsafe is
        # safe to invoke.
        for event_type in self._cfg.forwarded_events:
            self._bus.subscribe(event_type, self._on_event)

    def stop(self) -> None:
        """Tear down the bridge. Safe to call repeatedly."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        for event_type in self._cfg.forwarded_events:
            try:
                self._bus.unsubscribe(event_type, self._on_event)
            except Exception:
                pass
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._shutdown_loop)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    # -- event fan-in --------------------------------------------------------

    def _on_event(self, event: Event) -> None:
        """Subscriber hook — runs on whatever thread the bus publishes on."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            envelope = json.dumps(_serialise_event(event), separators=(",", ":"))
        except (TypeError, ValueError):
            logger.exception("dashboard bridge: could not serialise event")
            return
        payload = envelope.encode("utf-8")
        loop.call_soon_threadsafe(self._broadcast, payload)

    def _broadcast(self, payload: bytes) -> None:
        """Fan *payload* out to every connected client. Runs on the loop."""
        with self._clients_lock:
            clients = list(self._clients)
        for queue in clients:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow consumer — drop the message rather than backing up
                # the daemon. Dashboards are not authoritative.
                logger.debug("dashboard bridge: client queue full, dropping frame")

    # -- lifecycle internals -------------------------------------------------

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except asyncio.CancelledError:
            # Clean shutdown path — _shutdown_loop cancels the serve task.
            pass
        except Exception:
            logger.exception("dashboard bridge crashed")
        finally:
            # Drain any lingering per-client tasks so their finally blocks
            # (close-frame write, writer.wait_closed) actually run before
            # the loop closes. Without this, cancelled handler coroutines
            # can leave sockets in a half-closed state that bleeds into
            # subsequent bridges spun up in the same process (pytest).
            try:
                pending = [
                    t for t in asyncio.all_tasks(loop=loop) if not t.done()
                ]
                if pending:
                    loop.run_until_complete(
                        asyncio.wait(pending, timeout=0.5)
                    )
            except Exception:
                pass
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._cfg.host, self._cfg.port
        )
        sockets = self._server.sockets or []
        if sockets:
            self._actual_port = sockets[0].getsockname()[1]
        logger.info(
            "dashboard bridge listening on ws://%s:%d",
            self._cfg.host,
            self._actual_port or self._cfg.port,
        )
        self._started.set()
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass

    def _shutdown_loop(self) -> None:
        # Close server + drop each client queue with sentinel None so the
        # writer coroutine exits cleanly.
        if self._server is not None:
            self._server.close()
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for queue in clients:
            queue.put_nowait(None)
        # Cancel all tasks so the loop can stop.
        for task in asyncio.all_tasks(loop=self._loop):
            task.cancel()

    # -- per-client ----------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        try:
            ok = await self._perform_handshake(reader, writer)
        except Exception:
            logger.exception("dashboard bridge: handshake failed peer=%s", peer)
            writer.close()
            return
        if not ok:
            writer.close()
            return

        queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(
            maxsize=self._cfg.max_queue
        )
        with self._clients_lock:
            self._clients.add(queue)

        # Greet the client so the frontend can render immediately even if
        # no interesting events have fired since boot. Write it directly
        # (bypassing the queue) and drain so the frame lands on the wire
        # before the writer loop starts — otherwise a very fast teardown
        # could cancel this coroutine between handshake and first queue
        # drain, leaving the client hanging after the 101 response.
        hello = json.dumps(
            {
                "type": "hello",
                "timestamp": time.time(),
                "data": {"pid": os.getpid()},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            writer.write(_encode_text_frame(hello))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            with self._clients_lock:
                self._clients.discard(queue)
            writer.close()
            return

        # State snapshot — sent right after the hello so the React store
        # hydrates with the daemon's current listening_paused /
        # brain_state / pane state instead of the default values it had
        # at webview mount. Required for #2 (state matches across daemon
        # restarts) and #3 (mute button reflects backend reality).
        if self._state_provider is not None:
            try:
                snap = self._state_provider() or {}
            except Exception:  # never let a broken provider drop the client
                snap = {}
            snapshot_frame = json.dumps(
                {
                    "type": "state_snapshot",
                    "timestamp": time.time(),
                    "data": snap,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                writer.write(_encode_text_frame(snapshot_frame))
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                with self._clients_lock:
                    self._clients.discard(queue)
                writer.close()
                return

        reader_task = asyncio.create_task(self._client_reader(reader, writer))
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                writer.write(_encode_text_frame(item))
                try:
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(queue)
            reader_task.cancel()
            try:
                writer.write(_encode_close_frame())
                await writer.drain()
            except Exception:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _client_reader(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Drain client → server frames (pings, closes). We don't accept
        commands here — the frontend uses the existing Unix control socket
        for RPC. Ignoring incoming data keeps the bridge a strict one-way
        firehose.
        """
        while True:
            frame = await _read_frame(reader)
            if frame is None:
                return
            opcode, payload = frame
            if opcode == _OPCODE_CLOSE:
                return
            if opcode == _OPCODE_PING:
                # Echo as pong.
                header = bytes([0x80 | _OPCODE_PONG, len(payload)])
                writer.write(header + payload)
                try:
                    await writer.drain()
                except Exception:
                    return

    async def _perform_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        """Read the HTTP upgrade request and reply with 101 Switching Protocols."""
        request_lines: List[bytes] = []
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            request_lines.append(line)
            if len(request_lines) > 64:  # DoS guard
                return False

        headers: Dict[str, str] = {}
        for raw in request_lines[1:]:
            try:
                k, v = raw.decode("latin-1").rstrip("\r\n").split(":", 1)
            except ValueError:
                continue
            headers[k.strip().lower()] = v.strip()

        upgrade = headers.get("upgrade", "").lower()
        key = headers.get("sec-websocket-key", "")
        if upgrade != "websocket" or not key:
            writer.write(
                b"HTTP/1.1 400 Bad Request\r\n"
                b"Content-Length: 0\r\n\r\n"
            )
            await writer.drain()
            return False

        accept = _build_accept_key(key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        writer.write(response.encode("latin-1"))
        await writer.drain()
        return True


__all__ = [
    "DashboardBridge",
    "DashboardBridgeConfig",
]
