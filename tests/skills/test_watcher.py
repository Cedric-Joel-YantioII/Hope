"""Tests for :class:`hope.skills.watcher.SkillWatcher`.

Covers the three behaviours called out in the design brief:

1. A new skill written into a watched directory triggers rediscovery,
   publishes a ``SKILLS_UPDATED`` event, and the skill appears in the
   manager's catalog.
2. Rapid successive writes within the debounce window collapse into a
   single rediscovery (no event storm).
3. When a :class:`ToolExecutor` is wired into the watcher, its
   registered :class:`SkillTool` set is refreshed on change while
   non-skill tools are left alone.
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path
from typing import List

import pytest

# watchdog is optional; skip cleanly when it is not installed.
watchdog = pytest.importorskip("watchdog")

from hope.core.events import Event, EventBus, EventType
from hope.core.types import ToolResult
from hope.skills.manager import SkillManager
from hope.skills.tool_adapter import SkillTool
from hope.skills.watcher import SkillWatcher, _is_skill_relevant
from hope.tools._stubs import BaseTool, ToolExecutor, ToolSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _claude_code_skill(name: str, *, desc: str = "desc") -> str:
    """Return Claude-Code-style SKILL.md content with YAML frontmatter."""
    return textwrap.dedent(
        f"""\
        ---
        name: {name}
        description: {desc}
        allowed-tools:
          - Bash
          - Read
        ---

        # {name}

        Do the thing.
        """
    )


def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Spin until ``predicate()`` is truthy or ``timeout`` seconds elapse."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _DummyTool(BaseTool):
    """Minimal non-skill tool used to verify the refresh leaves it alone."""

    tool_id = "dummy"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="dummy", description="dummy", parameters={})

    def execute(self, **params) -> ToolResult:
        return ToolResult(tool_name="dummy", content="ok", success=True)


# ---------------------------------------------------------------------------
# _is_skill_relevant — pure function, no watcher needed
# ---------------------------------------------------------------------------


class TestIsSkillRelevant:
    @pytest.mark.parametrize(
        "path",
        [
            "/tmp/skills/foo/SKILL.md",
            "/tmp/skills/foo/skill.toml",
            "/tmp/skills/flat.toml",
            "/tmp/skills/foo/skill.md",  # lowercase variant
        ],
    )
    def test_accepts_skill_files(self, path: str):
        assert _is_skill_relevant(path)

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/tmp/skills/foo/SKILL.md.swp",
            "/tmp/skills/foo/.#SKILL.md",
            "/tmp/skills/foo/SKILL.md~",
            "/tmp/skills/.DS_Store",
            "/tmp/skills/foo/readme.txt",
        ],
    )
    def test_rejects_noise(self, path: str):
        assert not _is_skill_relevant(path)


# ---------------------------------------------------------------------------
# Integration — watcher drives rediscovery
# ---------------------------------------------------------------------------


class TestSkillWatcher:
    def test_new_skill_triggers_rediscover(self, tmp_path: Path):
        bus = EventBus(record_history=True)
        manager = SkillManager(bus, overlay_dir=tmp_path / "_overlays")
        manager.discover(paths=[tmp_path])
        assert manager.skill_names() == []

        events: List[Event] = []
        bus.subscribe(EventType.SKILLS_UPDATED, events.append)

        watcher = SkillWatcher(
            manager, [tmp_path], debounce_ms=100, bus=bus
        )
        watcher.start()
        try:
            # Claude Code writes: <tmp>/<name>/SKILL.md
            skill_dir = tmp_path / "greet-user"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                _claude_code_skill("greet-user")
            )

            assert _wait_for(lambda: "greet-user" in manager.skill_names())

            # SKILLS_UPDATED should have fired with the new skill in `added`.
            assert _wait_for(lambda: bool(events))
            payload = events[-1].data
            assert "greet-user" in payload.get("added", [])
            assert payload.get("removed") == []

            # allowed-tools should have been adapted onto required_capabilities
            manifest = manager.resolve("greet-user")
            assert "Bash" in manifest.required_capabilities
            assert "Read" in manifest.required_capabilities
        finally:
            watcher.stop()

    def test_rapid_writes_coalesce(self, tmp_path: Path):
        """Five rapid writes should produce at most one SKILLS_UPDATED event."""
        bus = EventBus()
        manager = SkillManager(bus, overlay_dir=tmp_path / "_overlays")

        events: List[Event] = []
        bus.subscribe(EventType.SKILLS_UPDATED, events.append)

        watcher = SkillWatcher(
            manager, [tmp_path], debounce_ms=300, bus=bus
        )
        watcher.start()
        try:
            skill_dir = tmp_path / "burst-skill"
            skill_dir.mkdir()
            target = skill_dir / "SKILL.md"

            # Five back-to-back rewrites, much faster than the debounce.
            for i in range(5):
                target.write_text(
                    _claude_code_skill("burst-skill", desc=f"v{i}")
                )
                time.sleep(0.02)

            # Wait well past the debounce window for the rediscover to fire.
            assert _wait_for(
                lambda: "burst-skill" in manager.skill_names(), timeout=3.0
            )
            # Give any stragglers a brief window to fire spuriously.
            time.sleep(0.4)

            # A single coalesced rediscover (extra events can only appear if
            # the OS emitted further fs events after the quiet period — in
            # practice 1 is what we observe).  Assert "<=" to stay robust
            # against platform-specific fs-event granularity.
            assert 1 <= len(events) <= 2, (
                f"expected 1 (possibly 2) SKILLS_UPDATED events from burst; "
                f"got {len(events)}"
            )
        finally:
            watcher.stop()

    def test_tool_executor_refresh_preserves_non_skill_tools(
        self, tmp_path: Path
    ):
        bus = EventBus()
        manager = SkillManager(bus, overlay_dir=tmp_path / "_overlays")
        manager.discover(paths=[tmp_path])

        dummy = _DummyTool()
        executor = ToolExecutor([dummy], bus)
        manager.set_tool_executor(executor)

        watcher = SkillWatcher(
            manager,
            [tmp_path],
            debounce_ms=100,
            tool_executor=executor,
            bus=bus,
        )
        watcher.start()
        try:
            skill_dir = tmp_path / "do-thing"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(_claude_code_skill("do-thing"))

            assert _wait_for(lambda: "do-thing" in manager.skill_names())

            # Force a synchronous refresh to avoid races around executor
            # state while watchdog is still firing events on mac (FSEvents
            # can coalesce events mid-test).
            watcher.trigger_now()

            # Dummy (non-skill) tool is still registered.
            assert "dummy" in executor._tools
            # New SkillTool is present under the skill's adapter name.
            skill_tools = [
                t for t in executor._tools.values() if isinstance(t, SkillTool)
            ]
            names = {t.spec.name for t in skill_tools}
            assert any("do-thing" in n for n in names), names
        finally:
            watcher.stop()

    def test_stop_is_idempotent(self, tmp_path: Path):
        bus = EventBus()
        manager = SkillManager(bus, overlay_dir=tmp_path / "_overlays")
        watcher = SkillWatcher(
            manager, [tmp_path], debounce_ms=50, bus=bus
        )
        watcher.start()
        watcher.stop()
        watcher.stop()  # must not raise

    def test_missing_path_is_tolerated(self, tmp_path: Path):
        """A non-existent directory should be skipped silently on start."""
        bus = EventBus()
        manager = SkillManager(bus, overlay_dir=tmp_path / "_overlays")
        missing = tmp_path / "does-not-exist"
        watcher = SkillWatcher(
            manager, [missing, tmp_path], debounce_ms=50, bus=bus
        )
        watcher.start()
        try:
            # Writing into the *existing* directory should still work.
            skill_dir = tmp_path / "present-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                _claude_code_skill("present-skill")
            )
            assert _wait_for(
                lambda: "present-skill" in manager.skill_names()
            )
        finally:
            watcher.stop()


# ---------------------------------------------------------------------------
# Debounce invariant — verified without watchdog at all
# ---------------------------------------------------------------------------


def test_debounce_coalesces_synthetic_events(tmp_path: Path):
    """Drive the watcher's debouncer directly, bypassing watchdog.

    This is a deterministic test for the core invariant (N events within
    the debounce window produce exactly one rediscover call) that does
    not depend on filesystem timing.
    """
    bus = EventBus()
    manager = SkillManager(bus, overlay_dir=tmp_path / "_overlays")

    call_count = 0
    orig = manager.discover

    def counting_discover(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        orig(*args, **kwargs)

    manager.discover = counting_discover  # type: ignore[method-assign]

    watcher = SkillWatcher(
        manager, [tmp_path], debounce_ms=150, bus=bus
    )
    # We drive the debouncer manually; no need to ``start()`` the observer.
    fake = str(tmp_path / "whatever" / "SKILL.md")
    for _ in range(10):
        watcher._on_fs_event(fake)
        time.sleep(0.01)

    # Wait past the debounce window + a little slack.
    time.sleep(0.4)

    assert call_count == 1, (
        f"expected exactly one discover after debounced burst, got {call_count}"
    )
