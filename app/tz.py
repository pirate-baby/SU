"""
Timezone utilities — SU lives in Brooklyn, NY.

All application code should use `now()` from this module instead of
`datetime.utcnow()` to get the current time in the user's local timezone.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")


def now() -> datetime:
    """Return the current time in America/New_York (EST/EDT), timezone-aware."""
    return datetime.now(LOCAL_TZ)


def now_iso() -> str:
    """Return the current time as an ISO-8601 string with timezone offset."""
    return now().isoformat()
