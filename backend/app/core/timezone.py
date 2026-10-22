"""Shared server-side IANA timezone validation (Spec SET-002).

One function, used by signup (`SignupRequest.business_timezone`) and by Business Settings
updates, so a Business timezone is validated identically wherever it can enter the system.
Roughly 38 downstream consumers call `ZoneInfo(business.timezone)` unguarded; an invalid
stored value would therefore break every report for the tenant, which is why validation is
a server-authoritative write-time rule (the frontend is only a convenience).

Acceptance is case-exact membership in `zoneinfo.available_timezones()` **and** a
successful `ZoneInfo` load. Legacy aliases the tz database ships (for example
`US/Pacific`, `EST`) are accepted because they are valid IANA identifiers for this stack;
free-form text such as `Mars/Phobos`, `localtime`, or a path-like string is not.
"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

INVALID_TIMEZONE_MESSAGE = "Enter a valid IANA timezone, for example America/New_York."


@lru_cache(maxsize=1)
def _known_timezones() -> frozenset[str]:
    return frozenset(available_timezones())


def validate_iana_timezone(value: str) -> str:
    """Returns the trimmed timezone identifier, or raises `ValueError` (which Pydantic
    turns into the normal structured `422`)."""
    candidate = value.strip()
    if not candidate or candidate not in _known_timezones():
        raise ValueError(INVALID_TIMEZONE_MESSAGE)
    try:
        ZoneInfo(candidate)
    except Exception:  # noqa: BLE001 - any load failure means the identifier is unusable
        raise ValueError(INVALID_TIMEZONE_MESSAGE) from None
    return candidate
