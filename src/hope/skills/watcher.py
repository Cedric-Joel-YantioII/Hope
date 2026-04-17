"""Filesystem watcher that re-discovers skills on change.

The Claude Code harness (running inside a persistent tmux pane as Hope's
"brain") writes new ``SKILL.md`` / ``skill.toml`` files into
``.claude/skills/`` at runtime.  :class:`SkillWatcher` observes the skill
directories and re-runs :meth:`SkillManager.discover` whenever anything
changes so the new skills become invocable without restarting Hope.

Key design points
-----------------

* **Debounced.**  Rapid successive edits (Claude Code often rewrites a
  skill multiple times in a second) coalesce into a single
  ``manager.discover()`` call.  We use a simple timer-based debouncer
  rather than a polling loop so idle watchers consume no CPU.
* **Idempotent.**  :meth:`SkillManager.discover` is safe to call over and
  over; the watcher never tries to diff events itself.
* **Tool executor refresh.**  If a :class:`ToolExecutor` is attached, the
  watcher replaces its registered :class:`SkillTool` instances after each
  discover so newly-added skills are immediately available to agents.
  Non-skill tools are left alone.
* **Event emission.**  A :data:`EventType.SKILLS_UPDATED` event is
  published with ``{added, removed, changed}`` skill names so
  subscribers (telemetry, UIs, the brain itself) can react.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from hope.core.events import Event, EventBus, EventType

if TYPE_CHECKING:
    from hope.skills.manager import SkillManager
    from hope.skills.types import SkillManifest
    from hope.tools._stubs import ToolExecutor

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skill-relevant path filter
# ---------------------------------------------------------------------------

_SKILL_FILENAMES = frozenset({"skill.toml", "SKILL.md"})


def _is_skill_relevant(path_str: str) -> bool:
    """Return True when *path_str* looks like it could change a skill.

    We only care about ``skill.toml`` / ``SKILL.md`` files and flat
    ``*.toml`` skill manifests.  Transient editor artefacts (``.swp``,
    ``~``, ``.#lock``, hidden dotfiles from OS tooling) are ignored so
    they do not trigger spurious rediscovery storms.
    """
    if not path_str:
        return False
    name = Path(path_str).name
    if not name:
        return False
    # Filter out obvious junk: editor tempfiles, lock files, hidden macOS dsstore
    if name.endswith(("~", ".swp", ".swx", ".tmp")) or name.startswith((".#", ".DS")):
        return False
    if name in _SKILL_FILENAMES:
        return True
    # Flat *.toml manifest at the root of a watched dir.
    if name.endswith(".toml"):
        return True
    # Markdown files inside a skill package directory — Claude Code writes
    # SKILL.md, but some tools lowercase it.
    if name.lower() == "skill.md":
        return True
    return False


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class SkillWatcher:
    """Watch a set of directories and refresh the :class:`SkillManager`.

    Parameters
    ----------
    manager:
        The :class:`SkillManager` whose catalog should be refreshed.
    paths:
        Directories to watch.  Non-existent directories are tolerated —
        they are silently skipped on :meth:`start` and can be created
        later (the watcher won't pick them up until restarted, which is
        acceptable for the default Hope layout where these directories
        exist from ``init``).
    debounce_ms:
        Minimum quiet period (in milliseconds) before a debounced
        discover runs.  Defaults to 500 ms.
    tool_executor:
        Optional :class:`ToolExecutor` whose skill tools should be
        refreshed after each rediscover.
    bus:
        Optional :class:`EventBus`.  Falls back to ``manager._bus`` so
        callers don't have to pass it twice.
    """

    def __init__(
        self,
        manager: "SkillManager",
        paths: List[Path],
        *,
        debounce_ms: int = 500,
        tool_executor: Optional["ToolExecutor"] = None,
        bus: Optional[EventBus] = None,
    ) -> None:
        self._manager = manager
        self._paths: List[Path] = [Path(p).expanduser() for p in paths]
        self._debounce_seconds = max(0.0, float(debounce_ms) / 1000.0)
        self._tool_executor = tool_executor
        # Fall back to the manager's bus for event emission.
        self._bus: Optional[EventBus] = bus or getattr(manager, "_bus", None)

        self._observer: Optional[object] = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin watching.

        Silently does nothing if already started.  Missing directories
        are skipped with a debug log; callers don't need to pre-create
        every path.  If ``watchdog`` is not installed this raises
        :class:`ImportError` with a hint to install the ``skills`` extra.
        """
        with self._lock:
            if self._started:
                return
            try:
                from watchdog.events import FileSystemEventHandler
                from watchdog.observers import Observer
            except ImportError as exc:  # pragma: no cover - env-dependent
                raise ImportError(
                    "SkillWatcher requires the 'watchdog' package. "
                    "Install with: pip install 'hope[skills]'"
                ) from exc

            handler = _SkillEventHandler(self)
            observer = Observer()

            scheduled_any = False
            for path in self._paths:
                if not path.exists():
                    LOGGER.debug(
                        "SkillWatcher: skipping missing path %s", path
                    )
                    continue
                if not path.is_dir():
                    LOGGER.debug(
                        "SkillWatcher: %s is not a directory; skipping", path
                    )
                    continue
                observer.schedule(handler, str(path), recursive=True)
                scheduled_any = True

            if scheduled_any:
                observer.start()
                self._observer = observer
            else:
                LOGGER.info(
                    "SkillWatcher started with no valid directories; "
                    "will no-op until restarted."
                )
            self._started = True

    def stop(self) -> None:
        """Stop watching and cancel any pending debounce timer.

        Safe to call multiple times or before :meth:`start`.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            observer = self._observer
            self._observer = None
            self._started = False

        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=2.0)
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                LOGGER.warning("SkillWatcher: observer stop failed: %s", exc)

    # Context-manager sugar so callers can ``with SkillWatcher(...) as w:``.
    def __enter__(self) -> "SkillWatcher":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: D401 - stdlib
        self.stop()

    # ------------------------------------------------------------------
    # Debounce + rediscover
    # ------------------------------------------------------------------

    def _on_fs_event(self, path_str: str) -> None:
        """Called by the watchdog handler for every relevant event.

        Schedules a debounced call to :meth:`_rediscover`.  Successive
        events reset the timer so a burst of writes coalesces into one
        rediscover at the end of the quiet period.
        """
        if not _is_skill_relevant(path_str):
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                self._debounce_seconds, self._rediscover
            )
            self._timer.daemon = True
            self._timer.start()

    def trigger_now(self) -> None:
        """Force an immediate synchronous rediscover (test hook)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._rediscover()

    def _rediscover(self) -> None:
        """Re-scan the watched dirs and refresh the catalog + tools.

        Compares the skill catalog before and after to compute
        ``added/removed/changed`` for the :data:`SKILLS_UPDATED` event.
        Exceptions from individual skills are already swallowed inside
        :func:`discover_skills`; any remaining error (dependency cycle,
        etc.) is logged and suppressed so the watcher stays alive.
        """
        try:
            before = _snapshot(self._manager)
            self._manager.discover(paths=list(self._paths))
            after = _snapshot(self._manager)

            added = sorted(set(after) - set(before))
            removed = sorted(set(before) - set(after))
            changed = sorted(
                name
                for name in set(after) & set(before)
                if after[name] != before[name]
            )

            # Refresh tool executor if provided
            if self._tool_executor is not None:
                try:
                    self._refresh_tool_executor()
                except Exception as exc:
                    LOGGER.warning(
                        "SkillWatcher: tool executor refresh failed: %s", exc
                    )

            if self._bus is not None:
                self._bus.publish(
                    EventType.SKILLS_UPDATED,
                    {
                        "added": added,
                        "removed": removed,
                        "changed": changed,
                    },
                )
            LOGGER.info(
                "SkillWatcher: rediscover done (added=%d removed=%d changed=%d)",
                len(added),
                len(removed),
                len(changed),
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("SkillWatcher: rediscover failed: %s", exc)
        finally:
            with self._lock:
                self._timer = None

    def _refresh_tool_executor(self) -> None:
        """Replace the executor's SkillTool set with the current catalog.

        Leaves non-skill tools untouched.  Uses the executor's private
        ``_tools`` dict because the public API does not expose a
        swap-tools method — we import :class:`SkillTool` lazily here to
        avoid a circular import.
        """
        from hope.skills.tool_adapter import SkillTool

        executor = self._tool_executor
        if executor is None:
            return

        # Drop previously-registered SkillTool instances.
        kept: Dict[str, object] = {
            name: tool
            for name, tool in executor._tools.items()
            if not isinstance(tool, SkillTool)
        }

        # Build fresh skill tools from the current manifests.
        fresh = self._manager.get_skill_tools(tool_executor=executor)
        for tool in fresh:
            kept[tool.spec.name] = tool

        executor._tools = kept  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _snapshot(manager: "SkillManager") -> Dict[str, bytes]:
    """Capture a lightweight fingerprint of every registered manifest.

    Uses ``SkillManifest.manifest_bytes()`` (which already excludes the
    signature) so we can detect "changed" skills without deep-diffing.
    """
    out: Dict[str, bytes] = {}
    skills: Dict[str, "SkillManifest"] = getattr(manager, "_skills", {})
    for name, manifest in skills.items():
        try:
            out[name] = manifest.manifest_bytes()
        except Exception:
            out[name] = b""
    return out


# ---------------------------------------------------------------------------
# Watchdog adapter (kept internal so callers don't take a hard dep)
# ---------------------------------------------------------------------------


def _make_handler_class():
    """Defer watchdog import so ``hope.skills.watcher`` is import-safe.

    Returns the ``FileSystemEventHandler`` subclass class object — we
    can't subclass at module scope without importing watchdog eagerly,
    which would break installations without the ``skills`` extra.
    """
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def __init__(self, owner: "SkillWatcher") -> None:
            super().__init__()
            self._owner = owner

        # All watchdog events carry a ``src_path``; ``moved`` also has
        # ``dest_path``.  We forward both so a rename that creates a new
        # skill.toml is not missed.
        def on_created(self, event):
            self._owner._on_fs_event(getattr(event, "src_path", ""))

        def on_modified(self, event):
            self._owner._on_fs_event(getattr(event, "src_path", ""))

        def on_deleted(self, event):
            self._owner._on_fs_event(getattr(event, "src_path", ""))

        def on_moved(self, event):
            self._owner._on_fs_event(getattr(event, "src_path", ""))
            dest = getattr(event, "dest_path", "")
            if dest:
                self._owner._on_fs_event(dest)

    return _Handler


# Lazily-materialized handler class — created on first use so imports
# succeed when watchdog is absent.
_HANDLER_CLASS: Optional[type] = None


def _SkillEventHandler(owner: "SkillWatcher"):
    global _HANDLER_CLASS
    if _HANDLER_CLASS is None:
        _HANDLER_CLASS = _make_handler_class()
    return _HANDLER_CLASS(owner)


__all__ = ["SkillWatcher"]
