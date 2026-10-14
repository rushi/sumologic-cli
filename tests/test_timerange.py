from datetime import UTC, datetime

import pytest

from sumologic_cli.timerange import TimeRangeError, prepare_log_search

NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
SCOPED = "_sourceCategory=prod/api | count"


def run(**arguments):
    return prepare_log_search(arguments, now=NOW)


def test_relative_offsets_resolve_in_utc_by_default():
    result = run(query="error", **{"from": "-15m", "to": "now"})
    assert result["from"] == "2026-06-08T11:45:00"
    assert result["to"] == "2026-06-08T12:00:00"
    assert result["timezone"] == "UTC"


@pytest.mark.parametrize("value", ["-2h", "now-2h", "now - 2h", "2h ago", "2 hours ago", "-120 min"])
def test_offset_spellings(value):
    result = run(query=SCOPED, **{"from": value, "to": "now"})
    assert result["from"] == "2026-06-08T10:00:00"


def test_relative_times_resolve_in_the_search_timezone():
    # 12:00Z is 08:00 EDT, so two hours earlier is 06:00 local.
    result = run(query=SCOPED, timezone="America/New_York", **{"from": "2h ago", "to": "now"})
    assert result["from"] == "2026-06-08T06:00:00"
    assert result["to"] == "2026-06-08T08:00:00"


def test_offset_timestamps_convert_to_the_search_timezone():
    result = run(
        query=SCOPED,
        timezone="America/New_York",
        **{"from": "2026-06-08T00:00:00Z", "to": "2026-06-08T00:20:00+00:00"},
    )
    assert result["from"] == "2026-06-07T20:00:00"
    assert result["to"] == "2026-06-07T20:20:00"


def test_naive_timestamps_pass_through():
    result = run(query="x", **{"from": "2026-06-08T00:00:00", "to": "2026-06-08T00:10:00"})
    assert (result["from"], result["to"]) == ("2026-06-08T00:00:00", "2026-06-08T00:10:00")


def test_thirty_minutes_unscoped_is_allowed():
    run(query="error", **{"from": "-30m", "to": "now"})


def test_over_thirty_minutes_unscoped_is_rejected():
    with pytest.raises(TimeRangeError, match="31 minutes"):
        run(query="error", **{"from": "-31m", "to": "now"})


@pytest.mark.parametrize(
    "query",
    ["_sourceCategory=prod/api", "_sourcecategory = prod", "_collector=c1", "_index=main", "_view=v error"],
)
def test_scoping_filters_allow_long_ranges(query):
    run(query=query, **{"from": "-1d", "to": "now"})


def test_similar_field_names_do_not_count_as_scoping():
    with pytest.raises(TimeRangeError):
        run(query="my_sourceCategory=x", **{"from": "-1h", "to": "now"})


def test_from_after_to_is_rejected():
    with pytest.raises(TimeRangeError, match="before"):
        run(query="x", **{"from": "now", "to": "-5m"})


@pytest.mark.parametrize("value", ["-5y", "yesterday", "2026-13-01T00:00:00"])
def test_unparseable_times_are_rejected(value):
    with pytest.raises(TimeRangeError):
        run(query="x", **{"from": value, "to": "now"})


def test_missing_bounds_are_rejected():
    with pytest.raises(TimeRangeError, match="missing --to"):
        run(query="x", **{"from": "-5m"})


def test_unknown_timezone_is_rejected():
    with pytest.raises(TimeRangeError, match="timezone"):
        run(query="x", timezone="Mars/Olympus", **{"from": "-5m", "to": "now"})
