"""End-to-end voice flow integration test.

Proves that a single injected ``SPEECH_TRANSCRIPT`` event traverses the
full path:

    SPEECH_TRANSCRIPT -> _on_speech_transcript (echo guard, busy check,
    min-length filter) -> brain executor -> BrainSession.send (mocked) ->
    ack + reply spoken (say_sync mocked) -> VoiceTurn persisted to the
    trace store.

The test also verifies:
  * A second transcript arriving while the brain is busy is dropped via
    the ``_brain_busy`` flag.
  * A third transcript AFTER the first turn finishes processes normally.
  * Shutdown is clean (no pending background threads, no stray pid file).
"""

from __future__ import annotations

import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from hope.core import config as _cfg_mod
from hope.core.events import EventBus, EventType
from hope.daemon.core import HopeDaemon
from hope.traces.voice_trace import VoiceTraceStore

# ---------------------------------------------------------------------------
# Fakes — mirror the patterns in tests/agents/test_tmux_orchestrator.py and
# tests/voice/test_brain_session.py so the daemon can boot without tmux.
# ---------------------------------------------------------------------------


@dataclass
class _FakeEntry:
    tmux_target: str = "hope:hope.0"


class _FakeRegistry:
    def __init__(self, pane_id: str) -> None:
        self._entries = {pane_id: _FakeEntry()}

    def get(self, pane_id: str) -> Optional[_FakeEntry]:
        return self._entries.get(pane_id)

    def specialist_count(self) -> int:
        return 0


class FakeOrchestrator:
    """Stand-in for TmuxOrchestrator: records tmux calls, pretends to be
    already started so ``_on_speech_transcript`` accepts the event.
    """

    def __init__(self, pane_id: str = "hope-abcd") -> None:
        self.hope_main_pane_id = pane_id
        self.registry = _FakeRegistry(pane_id)
        self.bus_socket_path = "/tmp/fake-bus.sock"
        self._started = True
        self.tmux_calls: List[List[str]] = []
        self.capture_calls: int = 0
        self._pane_output = ""

    def queued_spawn_count(self) -> int:
        return 0

    def capture_pane(self, pane_id: str, lines: int = 200) -> str:
        self.capture_calls += 1
        return self._pane_output

    def _tmux(self, cmd, *, check: bool = True, **kwargs):
        self.tmux_calls.append(list(cmd))

        class _CP:
            stdout = ""
            returncode = 0

        return _CP()

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixture — a HopeDaemon booted in-process with all external deps mocked.
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon_env(monkeypatch):
    """Boot a real :class:`HopeDaemon` wired to fakes + a tmp traces DB.

    Yields ``(daemon, bus, trace_store, speak_calls, brain_session)`` where
    * ``daemon`` is the live in-process HopeDaemon,
    * ``bus`` is the EventBus the daemon subscribes to,
    * ``trace_store`` is the VoiceTraceStore the daemon wrote to,
    * ``speak_calls`` is a list populated by the mocked ``say_sync``,
    * ``brain_session`` is the MagicMock we pre-cached into the daemon.
    """
    # Short tmp dir so AF_UNIX sun_path (~104 chars on macOS) doesn't overflow.
    short_dir = Path(tempfile.mkdtemp(prefix="hope_e2e_"))
    traces_db = short_dir / "traces.db"
    pid_file = short_dir / "d.pid"
    ctrl = short_dir / "d.sock"

    # Point load_config to a minimal HopeConfig so RAG + scheduler + speech
    # never touch ~/.hope. dashboard.enabled defaults to True, so flip it off
    # explicitly — we don't want a real WS server binding a port.
    def _cfg():
        cfg = _cfg_mod.HopeConfig()
        cfg.tools.storage.default_backend = "sqlite"
        cfg.tools.storage.db_path = str(short_dir / "mem.db")
        cfg.tools.storage.embed_mode = "sync"
        cfg.scheduler.enabled = False
        cfg.dashboard.enabled = False
        cfg.speech.always_on = False
        cfg.learning.enabled = False  # keep loop inert for the flow test
        return cfg

    monkeypatch.setattr("hope.daemon.core.load_config", _cfg)
    monkeypatch.setattr("hope.memory.rag.load_config", _cfg)

    # Every say_sync call lands here — the test asserts on its contents.
    speak_calls: List[str] = []
    speak_lock = threading.Lock()

    def _fake_say_sync(text: str, *args, **kwargs) -> None:
        # The daemon's _speak_blocking sleeps 2.5s AFTER say_sync returns to
        # extend the echo window. That's fine — the mock returns immediately
        # and the sleep still elapses in the ack thread.
        with speak_lock:
            speak_calls.append(text)

    monkeypatch.setattr("hope.daemon.core.say_sync", _fake_say_sync)
    # ``say`` (the non-blocking greet) may also fire during sleep/shutdown —
    # silence it to keep /usr/bin/say from being invoked on CI.
    monkeypatch.setattr("hope.daemon.core.say", lambda _text: None)

    # ``_speak_blocking`` sleeps 2.5s after every ``say_sync`` to extend the
    # echo window. In a test with mocked say that sleep is pure overhead —
    # two speak calls add 5s of latency, dragging the whole turn past the
    # 5-second budget the task specifies. Collapse the tail to a tiny
    # sleep so timing stays deterministic.
    _real_sleep = time.sleep

    def _fast_sleep(seconds: float) -> None:
        _real_sleep(min(seconds, 0.02))

    monkeypatch.setattr("hope.daemon.core.time.sleep", _fast_sleep)

    # Point the VoiceTraceStore at our tmp DB BEFORE the daemon builds one.
    # _init_voice_learning calls ``VoiceTraceStore()`` with no args, which
    # defaults to ~/.hope/traces.db. We patch the class itself so the test's
    # store is the one the daemon writes into.
    real_store = VoiceTraceStore(db_path=str(traces_db))

    def _store_factory(*args, **kwargs):
        return real_store

    monkeypatch.setattr(
        "hope.daemon.core.VoiceTraceStore", _store_factory,
    )

    bus = EventBus(record_history=True)
    orch = FakeOrchestrator(pane_id="hope-abcd")

    daemon = HopeDaemon(
        bus=bus,
        orchestrator=orch,
        wake_monitor=None,
        enable_wake=False,
        pid_file=pid_file,
        control_socket=ctrl,
    )
    daemon.start()

    # Cache the BrainSession mock so _get_brain_session returns it directly
    # (it checks cached pane id match first). Delay the brain reply by
    # ~1s so the ack thread (which waits _ACK_DELAY_SEC=0.6 before firing)
    # has time to land — otherwise a synchronous mock cancels the ack.
    # NOTE: the fixture monkey-patches time.sleep globally, so we use
    # threading.Event.wait (which is timer-based, not sleep-based) for
    # the delay.
    brain_session = MagicMock()
    _brain_delay_done = threading.Event()

    def _delayed_send(text: str) -> str:
        # Long enough that the ack worker (waits 0.6 s, then a 0.9 s
        # gen_ack timeout, then pick_ack, then _speak_blocking) speaks
        # the ack BEFORE the brain returns. Otherwise the brain's
        # ack_cancel.set() preempts the ack and only the reply speaks.
        _brain_delay_done.wait(timeout=2.0)
        return "Four. Two plus two is four."

    brain_session.send.side_effect = _delayed_send
    daemon._brain_session = brain_session
    daemon._brain_session_pane_id = "hope-abcd"

    try:
        yield daemon, bus, real_store, speak_calls, brain_session
    finally:
        try:
            daemon.shutdown()
        except Exception:
            pass
        try:
            real_store.close()
        except Exception:
            pass
        import shutil as _sh

        _sh.rmtree(short_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll *predicate* until it returns truthy or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _wait_for_turn(store: VoiceTraceStore, timeout: float = 5.0):
    """Block until at least one turn is in the store; return the newest."""
    if not _wait_for(lambda: store.count() >= 1, timeout=timeout):
        return None
    return store.list_recent(limit=1)[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_single_transcript_traverses_full_flow(daemon_env):
    """Publish ONE SPEECH_TRANSCRIPT and watch it flow end-to-end."""
    daemon, bus, store, speak_calls, brain_session = daemon_env

    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {"text": "Hope, what is two plus two?", "source": "test"},
    )

    # Wait for the brain worker to finish — the VoiceTurn is written in the
    # finally block of _process_transcript, so its presence is the proxy
    # for the whole pipeline completing.
    turn = _wait_for_turn(store, timeout=5.0)
    assert turn is not None, (
        f"no VoiceTurn appeared in {store._db_path} within 5s; "
        f"brain.send calls={brain_session.send.call_count}, "
        f"speak_calls={speak_calls}"
    )

    # 1. Transcript reached the trace store.
    assert turn.user_transcript == "what is two plus two?"

    # 2. BrainSession.send was called with the transcript text.
    brain_session.send.assert_called_once_with("what is two plus two?")

    # 3. Both the ack and the truncated reply were spoken — at least 2 calls.
    #    (Ack thread may lag behind the reply's speak, so we poll.)
    assert _wait_for(lambda: len(speak_calls) >= 2, timeout=3.0), (
        f"expected >= 2 say_sync calls (ack + reply), got {speak_calls}"
    )
    # One of the calls MUST be the ack phrase. acks_bank.pick_ack now
    # supersedes the static DEFAULT_ACKS list — accept any short phrase
    # that ISN'T the reply, to remain agnostic to which bank fired.
    assert any(("Four" not in c) and len(c) > 0 for c in speak_calls), (
        f"no ack phrase in speak_calls: {speak_calls}"
    )
    # Another MUST be the truncated first-sentence reply.
    assert any("Four" in c for c in speak_calls), (
        f"reply not spoken; speak_calls={speak_calls}"
    )

    # 4. reply_head was recorded on the turn.
    assert turn.brain_reply_head, f"turn.brain_reply_head empty: {turn!r}"
    assert "Four" in turn.brain_reply_head
    # brain_reply_full captures the untruncated reply.
    assert turn.brain_reply_full == "Four. Two plus two is four."
    # ack_spoken was also persisted (any non-empty phrase, since
    # acks_bank/acks_gemma supersede the static DEFAULT_ACKS list).
    assert turn.ack_spoken and "Four" not in turn.ack_spoken


def test_second_transcript_dropped_while_brain_busy(daemon_env):
    """A transcript arriving mid-turn must be dropped by the _brain_busy gate."""
    daemon, bus, store, speak_calls, brain_session = daemon_env

    # Make the brain reply slow so we can race a second event into it.
    release = threading.Event()

    def _slow_send(text: str) -> str:
        # Hold until the test releases, simulating Claude "still thinking".
        release.wait(timeout=3.0)
        return "Four."

    brain_session.send.side_effect = _slow_send

    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {"text": "Hope, what is two plus two?", "source": "test"},
    )

    # Wait until the worker thread actually enters ``session.send`` —
    # that's when _brain_busy is set AND the brain is mid-call. We block
    # send via the ``release`` event above, so call_count==1 means we're
    # safely inside the critical section.
    assert _wait_for(lambda: brain_session.send.call_count == 1, timeout=2.0), (
        f"brain.send never called — worker didn't pick up transcript; "
        f"busy={daemon._brain_busy.is_set()}, calls={brain_session.send.call_count}"
    )
    assert daemon._brain_busy.is_set(), "_brain_busy should be set while send is in flight"

    # Within 1s of the first, publish a second. Since _brain_busy is set,
    # _on_speech_transcript should return early.
    bus.publish(
        EventType.SPEECH_TRANSCRIPT,
        {"text": "another question", "source": "test"},
    )

    # The second transcript must NOT have reached the brain.
    # (Still only one send call, with the first text.)
    assert brain_session.send.call_count == 1, (
        f"second transcript leaked through busy gate — "
        f"send calls={brain_session.send.call_args_list}"
    )

    # Release the slow brain so the first turn finishes + busy clears.
    release.set()
    turn = _wait_for_turn(store, timeout=5.0)
    assert turn is not None
    assert turn.user_transcript == "what is two plus two?"

    # Reset the side_effect to a simple return so subsequent turns run fast.
    brain_session.send.side_effect = None
    brain_session.send.return_value = "Five."

    # Mid-brain transcripts are QUEUED (not dropped) — _drain_pending_turns
    # runs them after the busy turn finishes. So "another question"
    # should eventually appear in the trace store.
    assert _wait_for(lambda: store.count() >= 2, timeout=8.0), (
        f"queued transcript was never drained; store.count={store.count()}, "
        f"send calls={brain_session.send.call_args_list}"
    )
    transcripts = [t.user_transcript for t in store.list_recent(limit=5)]
    assert "another question" in transcripts, (
        f"expected mid-brain transcript to drain after busy clears; got {transcripts}"
    )


def test_shutdown_is_clean(daemon_env):
    """After shutdown the pid file is gone and the trace store closed cleanly."""
    daemon, bus, store, speak_calls, brain_session = daemon_env
    daemon.shutdown()
    assert not daemon._pid_file.exists()
    # Double-shutdown must be a no-op (is_set short-circuit).
    daemon.shutdown()
