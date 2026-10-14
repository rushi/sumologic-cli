"""Client-side handling of log search time ranges.

The gateway takes absolute ISO 8601 timestamps only and rejects ranges over 30
minutes that lack a scoping filter. Agents get both wrong often, so resolve
relative times and check the range locally, before a round trip.
"""

import re
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "UTC"
MAX_UNSCOPED_RANGE = timedelta(minutes=30)
SCOPING_FIELDS = ("_sourceCategory", "_collector", "_index", "_view")

# Unit spelling to timedelta keyword.
UNITS = {
    **dict.fromkeys(("s", "sec", "secs", "second", "seconds"), "seconds"),
    **dict.fromkeys(("m", "min", "mins", "minute", "minutes"), "minutes"),
    **dict.fromkeys(("h", "hr", "hrs", "hour", "hours"), "hours"),
    **dict.fromkeys(("d", "day", "days"), "days"),
    **dict.fromkeys(("w", "week", "weeks"), "weeks"),
}

# -15m, now-15m, now - 2h
_OFFSET = re.compile(r"^(?:now\s*)?-\s*(\d+)\s*([a-z]+)$")
# 15m ago, 2 hours ago
_AGO = re.compile(r"^(\d+)\s*([a-z]+)\s+ago$")
_SCOPED = re.compile(rf"\b({'|'.join(SCOPING_FIELDS)})\s*=", re.IGNORECASE)

# The gateway caps --from/--to at 20 characters, so send naive local time in
# the search timezone rather than an offset-suffixed timestamp.
WIRE_FORMAT = "%Y-%m-%dT%H:%M:%S"

TIME_HELP = "Use an ISO 8601 time (2026-06-08T00:00:00Z), 'now', '-15m', or '2h ago'."


class TimeRangeError(ValueError):
    pass


def get_zone(name: str) -> tzinfo:
    if name.upper() == "Z":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimeRangeError(f"unknown --timezone {name!r}. Use an IANA name such as UTC or America/New_York.") from exc


def _offset(amount: str, unit: str) -> timedelta | None:
    keyword = UNITS.get(unit)
    if keyword is None:
        return None
    return timedelta(**{keyword: int(amount)})


def parse_time(value: str, zone: tzinfo, now: datetime) -> datetime:
    """Return `value` as an aware datetime in `zone`.

    Accepts `now`, relative offsets, and ISO 8601. A naive ISO time is read in
    `zone`, matching how the gateway interprets it.
    """
    text = value.strip().lower()
    if text == "now":
        return now.astimezone(zone)

    match = _OFFSET.match(text) or _AGO.match(text)
    if match:
        delta = _offset(*match.groups())
        if delta is None:
            raise TimeRangeError(f"unknown time unit in {value!r}. {TIME_HELP}")
        return (now - delta).astimezone(zone)

    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise TimeRangeError(f"cannot parse time {value!r}. {TIME_HELP}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def is_scoped(query: str) -> bool:
    return _SCOPED.search(query) is not None


def prepare_log_search(arguments: dict, now: datetime | None = None) -> dict:
    """Resolve the time range of a log search and check it against gateway limits.

    Returns a copy of `arguments` with `from`/`to` in wire format and
    `timezone` defaulted. Raises TimeRangeError for input the gateway would reject.
    """
    now = now or datetime.now(UTC)
    prepared = dict(arguments)
    zone_name = prepared.get("timezone") or DEFAULT_TIMEZONE
    prepared["timezone"] = zone_name
    zone = get_zone(zone_name)

    missing = [flag for flag in ("from", "to") if not prepared.get(flag)]
    if missing:
        raise TimeRangeError(f"missing --{' and --'.join(missing)}. {TIME_HELP}")

    start = parse_time(str(prepared["from"]), zone, now)
    end = parse_time(str(prepared["to"]), zone, now)
    if start >= end:
        raise TimeRangeError(
            f"--from ({start.strftime(WIRE_FORMAT)}) must be before --to ({end.strftime(WIRE_FORMAT)})."
        )

    query = prepared.get("query") or ""
    if end - start > MAX_UNSCOPED_RANGE and not is_scoped(query):
        minutes = int((end - start).total_seconds() // 60)
        raise TimeRangeError(
            f"range is {minutes} minutes. Ranges over 30 minutes need a "
            f"{', '.join(SCOPING_FIELDS[:-1])}, or {SCOPING_FIELDS[-1]} filter in --query. "
            f"Add one (run discovery:list-partitions to find values) or narrow the range."
        )

    prepared["from"] = start.strftime(WIRE_FORMAT)
    prepared["to"] = end.strftime(WIRE_FORMAT)
    return prepared
