"""Hope audio subsystem — voice acknowledgments and mic helpers.

The top-level package exposes :func:`say` so callers can write
``from hope.audio import say`` without worrying about platform.
"""

from hope.audio.say import say, say_sync

__all__ = ["say", "say_sync"]
