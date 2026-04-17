"""Data source connectors for Deep Research."""

from hope.connectors._stubs import (
    Attachment,
    BaseConnector,
    Document,
    SyncStatus,
)
from hope.connectors.store import KnowledgeStore

__all__ = ["Attachment", "BaseConnector", "Document", "KnowledgeStore", "SyncStatus"]

# Auto-register built-in connectors
import hope.connectors.obsidian  # noqa: F401

try:
    import hope.connectors.gmail  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.gmail_imap  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.gdrive  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import hope.connectors.notion  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.granola  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.gcontacts  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.imessage  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.apple_notes  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.apple_music  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.apple_contacts  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.slack_connector  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.outlook  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.gcalendar  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.dropbox  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import hope.connectors.whatsapp  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.oura  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.apple_health  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.strava  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.spotify  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.google_tasks  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.weather  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.github_notifications  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.hackernews  # noqa: F401
except ImportError:
    pass

try:
    import hope.connectors.news_rss  # noqa: F401
except ImportError:
    pass
