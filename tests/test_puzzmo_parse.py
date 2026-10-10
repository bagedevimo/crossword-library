import base64
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from crossword_library.puzzmo import (
    CalendarEntry,
    PuzzmoAPIError,
    PuzzmoAuthError,
    parse_month,
    series_for_url_path,
    token_expires_at,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize(
    "url_path,series",
    [
        ("2026-10-05/crossword", "daily"),
        ("2026-10-05/crossword/mini", "mini"),
        ("2026-10-05/crossword/big", "big"),
        ("2026-10-05/crossword/mini/2", None),
        ("2026-10-05/typeshift", None),
        ("2026-10-05", None),
    ],
)
def test_series_for_url_path(url_path, series):
    assert series_for_url_path(url_path) == series


def test_parse_month_keeps_only_publisher_crosswords():
    entries = parse_month(load("calendar_2026_10.json"))
    assert [(e.url_path, e.source_id) for e in entries] == [
        ("2026-10-05/crossword", "pz-daily-1005:puzzle"),
        ("2026-10-05/crossword/mini", "pz-mini-1005:puzzle"),
        ("2026-10-05/crossword/big", "pz-big-kicks:puzzle"),
        ("2026-10-06/crossword", "pz-daily-1006:puzzle"),
        ("2026-10-06/crossword/big", "pz-big-kicks:puzzle"),
    ]


def test_parse_month_entry_fields():
    daily, mini, big, unplayed, _ = parse_month(load("calendar_2026_10.json"))
    assert daily == CalendarEntry(
        source_id="pz-daily-1005:puzzle",
        series="daily",
        puzzle_date=date(2026, 10, 5),
        url_path="2026-10-05/crossword",
        title="High Grades",
        completed=True,
        completed_at=datetime(2026, 10, 5, 9, 15, tzinfo=timezone.utc),
    )
    # Completed but Puzzmo gave no timestamp: still completed, timestamp None.
    assert mini.completed is True and mini.completed_at is None
    assert big.series == "big" and big.completed is False
    assert unplayed.completed is False and unplayed.completed_at is None


def test_parse_month_empty_month():
    assert parse_month({"data": {"dailiesForAMonth": []}}) == []


def test_parse_month_auth_error():
    payload = {"errors": [{"message": "Must be logged in"}], "data": None}
    with pytest.raises(PuzzmoAuthError, match="PUZZMO_TOKEN"):
        parse_month(payload)


def test_parse_month_other_graphql_error():
    payload = {"errors": [{"message": 'Cannot query field "foo"'}], "data": None}
    with pytest.raises(PuzzmoAPIError, match="Cannot query field"):
        parse_month(payload)


def test_parse_month_missing_data():
    with pytest.raises(PuzzmoAPIError, match="dailiesForAMonth"):
        parse_month({"data": {}})


def _jwt(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.signature"


def test_token_expires_at():
    assert token_expires_at(_jwt({"exp": 1808277780})) == datetime(
        2027, 4, 21, 3, 23, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("token", ["not-a-jwt", _jwt({"sub": "x"}), "a.!!!.c", ""])
def test_token_expires_at_unparseable(token):
    assert token_expires_at(token) is None
