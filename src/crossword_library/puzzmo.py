"""Puzzmo calendar client: puzzle catalogue and the user's completions.

This is the only module that uses PUZZMO_TOKEN. Puzzle downloads live in
`fetch` and are always anonymous.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

CROSSWORD_GAME_SLUG = "crossword"

CALENDAR_QUERY = """query CalendarQuery($month: Int!, $year: Int!) {
  dailiesForAMonth(month: $month, year: $year) {
    day
    puzzles(cacheKey: "calendar") {
      urlPath
      puzzle {
        id
        name
        game { slug }
        communityPuzzle { id }
        currentAccountGamePlayed { completed completedAt }
      }
    }
  }
}"""


class PuzzmoError(Exception):
    """Base class for Puzzmo API failures."""


class PuzzmoAuthError(PuzzmoError):
    """The token was rejected or has expired."""


class PuzzmoAPIError(PuzzmoError):
    """Puzzmo returned an error, an unexpected response, or kept failing."""


@dataclass(frozen=True)
class CalendarEntry:
    source_id: str
    series: str
    puzzle_date: date
    url_path: str
    title: str
    completed: bool
    completed_at: datetime | None


def series_for_url_path(url_path: str) -> str | None:
    """Map a calendar urlPath to daily/mini/big, or None if it isn't one."""
    parts = url_path.split("/")
    if len(parts) < 2 or parts[1] != CROSSWORD_GAME_SLUG:
        return None
    rest = parts[2:]
    if rest == []:
        return "daily"
    if rest == ["mini"]:
        return "mini"
    if rest == ["big"]:
        return "big"
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def parse_month(payload: dict) -> list[CalendarEntry]:
    """Extract publisher crosswords from a dailiesForAMonth response."""
    errors = payload.get("errors")
    if errors:
        messages = "; ".join(str(e.get("message", "?")) for e in errors)
        if any("Must be logged in" in str(e.get("message", "")) for e in errors):
            raise PuzzmoAuthError(f"Puzzmo rejected the token ({messages}); replace PUZZMO_TOKEN")
        raise PuzzmoAPIError(f"Puzzmo GraphQL error: {messages}")

    days = (payload.get("data") or {}).get("dailiesForAMonth")
    if days is None:
        raise PuzzmoAPIError("Puzzmo response had no dailiesForAMonth data")

    entries = []
    for day in days:
        for item in day.get("puzzles") or []:
            url_path = item.get("urlPath") or ""
            puzzle = item.get("puzzle") or {}
            if (puzzle.get("game") or {}).get("slug") != CROSSWORD_GAME_SLUG:
                continue
            if puzzle.get("communityPuzzle"):
                continue
            series = series_for_url_path(url_path)
            if series is None:
                continue
            played = puzzle.get("currentAccountGamePlayed") or {}
            entries.append(
                CalendarEntry(
                    source_id=puzzle["id"],
                    series=series,
                    puzzle_date=date.fromisoformat(url_path[:10]),
                    url_path=url_path,
                    title=puzzle.get("name") or "",
                    completed=bool(played.get("completed")),
                    completed_at=_parse_datetime(played.get("completedAt")),
                )
            )
    return entries


def token_expires_at(token: str) -> datetime | None:
    """Read the JWT `exp` claim without verifying the signature."""
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body))
        return datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc)
    except (IndexError, ValueError, KeyError, TypeError):
        return None
