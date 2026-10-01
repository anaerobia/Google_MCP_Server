"""Unit tests for the pieces the protocol tests cannot reach directly.

These cover the arithmetic and the token bookkeeping where an off-by-one is
easy to make and hard to see from the outside.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from gcal_mcp.errors import ErrorCode, from_http_status
from gcal_mcp.oauth import EXPIRY_SKEW_SECONDS, Token, TokenStore
from gcal_mcp.scheduling import (
    TimeParseError,
    busy_intervals,
    find_free_slots,
    parse_rfc3339,
)
from gcal_mcp.shaping import MAX_LISTED_ATTENDEES, shape_event_detail


def _response(status: int, body: dict[str, object], **headers: str) -> httpx.Response:
    """Build a stub httpx response for status-mapping tests.

    Args:
        status: HTTP status code.
        body: JSON body.
        **headers: Response headers.

    Returns:
        The response.
    """
    return httpx.Response(status, json=body, headers=headers)


def test_overlapping_busy_periods_are_merged() -> None:
    """Two overlapping busy blocks collapse into one interval."""
    busy, warnings = busy_intervals(
        {
            "calendars": {
                "primary": {
                    "busy": [
                        {
                            "start": "2026-10-01T10:00:00-07:00",
                            "end": "2026-10-01T11:00:00-07:00",
                        },
                        {
                            "start": "2026-10-01T10:30:00-07:00",
                            "end": "2026-10-01T12:00:00-07:00",
                        },
                    ]
                }
            }
        }
    )
    assert warnings == []
    assert len(busy) == 1
    assert busy[0][1] == parse_rfc3339("2026-10-01T12:00:00-07:00", field="t")


def test_gap_shorter_than_the_meeting_is_not_offered() -> None:
    """A 15-minute hole is not reported as a home for a 30-minute meeting."""
    window_start = parse_rfc3339("2026-10-01T09:00:00-07:00", field="t")
    window_end = parse_rfc3339("2026-10-01T10:30:00-07:00", field="t")
    busy = [
        (
            parse_rfc3339("2026-10-01T09:15:00-07:00", field="t"),
            parse_rfc3339("2026-10-01T10:00:00-07:00", field="t"),
        )
    ]
    slots = find_free_slots(
        window_start=window_start,
        window_end=window_end,
        busy=busy,
        duration_minutes=30,
        max_slots=5,
    )
    assert [slot["start"] for slot in slots] == ["2026-10-01T10:00:00-07:00"]


def test_slots_keep_the_offset_they_were_asked_in() -> None:
    """Results come back in the caller's offset, not normalized to UTC."""
    window_start = parse_rfc3339("2026-10-01T09:00:00-07:00", field="t")
    window_end = parse_rfc3339("2026-10-01T10:00:00-07:00", field="t")
    slots = find_free_slots(
        window_start=window_start,
        window_end=window_end,
        busy=[],
        duration_minutes=30,
        max_slots=1,
    )
    assert slots[0]["start"].endswith("-07:00")
    assert slots[0]["suggested_end"] == "2026-10-01T09:30:00-07:00"


@pytest.mark.parametrize(
    "value", ["", "not a time", "2026-10-01T09:00:00", "2026-13-01T09:00:00Z"]
)
def test_unusable_timestamps_are_rejected(value: str) -> None:
    """Empty, malformed and offset-less timestamps all raise."""
    with pytest.raises(TimeParseError):
        parse_rfc3339(value, field="time_min")


def test_huge_attendee_lists_are_summarised() -> None:
    """Past the listing limit, attendees become a count and a note."""
    raw = {
        "id": "e",
        "attendees": [
            {"email": f"person{index}@example.com"}
            for index in range(MAX_LISTED_ATTENDEES + 5)
        ],
    }
    shaped = shape_event_detail(raw, calendar_id="primary")
    assert shaped["attendee_count"] == MAX_LISTED_ATTENDEES + 5
    assert "attendees" not in shaped
    assert "too many to list" in shaped["attendees_omitted"]


def test_rate_limit_reason_outranks_a_403_status() -> None:
    """Google reports quota exhaustion as 403; it must stay retryable."""
    error = from_http_status(
        _response(
            403,
            {
                "error": {
                    "message": "Rate Limit Exceeded",
                    "errors": [{"reason": "rateLimitExceeded"}],
                }
            },
            **{"Retry-After": "17"},
        ),
        context="list calendars",
    )
    assert error.code is ErrorCode.RATE_LIMITED
    assert error.retryable is True
    assert error.retry_after_seconds == 17


def test_plain_403_is_permission_denied_and_final() -> None:
    """A genuine authorization failure is not retryable."""
    error = from_http_status(
        _response(
            403,
            {"error": {"message": "Forbidden", "errors": [{"reason": "x"}]}},
        ),
        context="create an event",
    )
    assert error.code is ErrorCode.PERMISSION_DENIED
    assert error.retryable is False


def test_non_json_error_body_still_maps_cleanly() -> None:
    """An HTML error page from a proxy does not crash the mapper."""
    error = from_http_status(
        httpx.Response(502, text="<html>bad gateway</html>"),
        context="get event",
    )
    assert error.code is ErrorCode.UPSTREAM_ERROR
    assert error.retryable is True


def test_token_is_refreshed_before_it_actually_expires() -> None:
    """The skew window means a token is replaced early, never used late."""
    inside_skew = Token("a", "r", time.time() + EXPIRY_SKEW_SECONDS - 10)
    outside_skew = Token("a", "r", time.time() + EXPIRY_SKEW_SECONDS + 60)
    assert inside_skew.is_fresh() is False
    assert outside_skew.is_fresh() is True


def test_corrupt_token_cache_reads_as_not_logged_in(tmp_path: Path) -> None:
    """A truncated cache file is treated as absent, not as a crash."""
    path = tmp_path / "token.json"
    path.write_text("{not json", encoding="utf-8")
    assert TokenStore(path).load() is None


def test_saved_token_is_owner_only_and_round_trips(tmp_path: Path) -> None:
    """Saving writes mode 0600 and reloads to an equal token."""
    path = tmp_path / "nested" / "token.json"
    store = TokenStore(path)
    token = Token("access", "refresh", time.time() + 3600, scope="a b")
    store.save(token)
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["scope"] == "a b"
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.access_token == "access"
