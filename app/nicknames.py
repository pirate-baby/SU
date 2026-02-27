"""
Nickname resolver: reads SU's nickname repertoire from basic-memory and
picks an appropriate name for the user based on context.

The repertoire lives in ~/basic-memory/people/user-nicknames.md as a
standard basic-memory note with observation entries. This module reads
that file directly (no MCP, no subprocess) and caches it in memory.

The REM agent is responsible for creating and evolving the note over time.
"""
import random
import re
import time
from pathlib import Path
from typing import Optional

from app.config import settings
from app.logger import get_logger

log = get_logger(__name__)

# Cache the parsed nicknames with a TTL so we pick up REM updates
_cache: dict = {"nicknames": [], "loaded_at": 0.0}
_CACHE_TTL = 300  # seconds

NOTE_PATH = Path.home() / "basic-memory" / "people" / "user-nicknames.md"

# Pattern matches lines like: - [nickname] "Boss" — casual deference #familiar
_NICKNAME_RE = re.compile(
    r'^\s*-\s*\[nickname\]\s*"([^"]+)"\s*[—–-]\s*(.+?)(?:\s+#\S+)*\s*$',
    re.IGNORECASE,
)

# Pattern matches retired/avoided entries
_RETIRED_RE = re.compile(r'#retired|#avoid', re.IGNORECASE)


class Nickname:
    """A single nickname entry."""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description.strip()

    def __repr__(self) -> str:
        return f"Nickname({self.name!r})"


def _load_nicknames() -> list[Nickname]:
    """Read and parse the nicknames note from disk."""
    if not NOTE_PATH.exists():
        return []

    try:
        content = NOTE_PATH.read_text(encoding="utf-8")
    except OSError:
        log.warning("nicknames.read_failed", path=str(NOTE_PATH))
        return []

    nicknames: list[Nickname] = []
    for line in content.splitlines():
        if _RETIRED_RE.search(line):
            continue
        m = _NICKNAME_RE.match(line)
        if m:
            nicknames.append(Nickname(name=m.group(1), description=m.group(2)))

    log.debug("nicknames.loaded", count=len(nicknames), path=str(NOTE_PATH))
    return nicknames


def get_nicknames() -> list[Nickname]:
    """Return cached nicknames, refreshing if stale."""
    now = time.monotonic()
    if now - _cache["loaded_at"] > _CACHE_TTL:
        _cache["nicknames"] = _load_nicknames()
        _cache["loaded_at"] = now
    return _cache["nicknames"]


def resolve_name(context: Optional[str] = None) -> str:
    """Pick a name for the user.

    Returns one of SU's nicknames if available, otherwise falls back
    to settings.user_name. The selection is weighted random — the
    context string (e.g. "urgent", "casual", "morning_greeting") is
    available for future refinement but currently we just vary naturally.
    """
    nicknames = get_nicknames()
    if not nicknames:
        return settings.user_name

    return random.choice(nicknames).name
