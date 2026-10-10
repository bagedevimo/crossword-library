"""Puzzmo calendar client: puzzle catalogue and the user's completions.

This is the only module that uses PUZZMO_TOKEN. Puzzle downloads live in
`fetch` and are always anonymous.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone

import requests

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
        texts = [str(e.get("message", "?")) if isinstance(e, dict) else str(e) for e in errors]
        messages = "; ".join(texts)
        if any("Must be logged in" in t for t in texts):
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
    except (IndexError, ValueError, KeyError, TypeError, OverflowError, OSError):
        return None


API_URL = "https://api.puzzmo.com/graphql"
USER_AGENT = "crossword-library/0.1"


class PuzzmoClient:
    """Fetches one calendar month at a time with the user's token."""

    def __init__(
        self,
        token: str,
        session: requests.Session | None = None,
        retries: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30,
    ):
        self._token = token
        self._session = session or requests.Session()
        self._retries = retries
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._timeout = timeout

    def fetch_month(self, year: int, month: int) -> list[CalendarEntry]:
        """Fetch crosswords for a month. `month` is 1-12; Puzzmo's API is 0-indexed."""
        body = {
            "operationName": "CalendarQuery",
            "query": CALENDAR_QUERY,
            "variables": {"year": year, "month": month - 1},
        }
        headers = {
            "authorization": f"Bearer {self._token}",
            "auth-provider": "custom",
            "User-Agent": USER_AGENT,
        }
        last_error = "no attempts made"
        for attempt in range(self._retries + 1):
            try:
                response = self._session.post(
                    API_URL, json=body, headers=headers, timeout=self._timeout
                )
            except requests.RequestException as exc:
                last_error = f"network error: {type(exc).__name__}"
            else:
                status = response.status_code
                if status in (401, 403):
                    raise PuzzmoAuthError(
                        f"Puzzmo rejected the token (HTTP {status}); replace PUZZMO_TOKEN"
                    )
                if status >= 500 or status == 429:
                    last_error = f"HTTP {status}"
                elif status != 200:
                    raise PuzzmoAPIError(
                        f"Unexpected HTTP {status} from Puzzmo: {response.text[:200]}"
                    )
                else:
                    try:
                        payload = response.json()
                    except ValueError:
                        raise PuzzmoAPIError("Puzzmo returned a non-JSON response") from None
                    try:
                        return parse_month(payload)
                    except (KeyError, ValueError, TypeError, AttributeError) as exc:
                        raise PuzzmoAPIError(
                            f"malformed calendar data for {year}-{month:02d}: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc
            if attempt < self._retries:
                self._sleep(self._backoff_seconds * 2**attempt)
        raise PuzzmoAPIError(
            f"Calendar {year}-{month:02d} failed after {self._retries + 1} attempts: {last_error}"
        )
