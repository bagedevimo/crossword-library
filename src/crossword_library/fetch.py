"""Anonymous puzzle downloads through xword-dl.

Never pass PUZZMO_TOKEN here: Puzzmo's startOrFindGameplay creates a gameplay
record, so an authenticated fetch would mark puzzles as started on the
user's account. xword-dl sends a random Puzzmo-Gameplay-Id instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests
from xword_dl.downloader.puzzmodownloader import PuzzmoDownloader
from xword_dl.util import XWordDLException, sanitize_for_puzfile


class FetchError(Exception):
    """A puzzle could not be downloaded or converted."""


@dataclass(frozen=True)
class FetchedPuzzle:
    title: str  # raw Puzzmo name, before .puz sanitisation; compare this with the calendar
    author: str
    xd: str
    puz: bytes


class _UrlPathDownloader(PuzzmoDownloader):
    """PuzzmoDownloader for any calendar urlPath: daily, crossword/mini or crossword/big."""

    def __init__(self, url_path: str):
        super().__init__()
        self.finder_key = "today:/" + url_path
        self.date_string = url_path[:10]


def fetch_raw(url_path: str) -> dict:
    """Return Puzzmo's puzzle payload (name, authors, xd `puzzle` text, ...)."""
    downloader = _UrlPathDownloader(url_path)
    try:
        return downloader.fetch_data(f"https://www.puzzmo.com/puzzle/{url_path}")
    except XWordDLException as exc:
        raise FetchError(str(exc)) from exc
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc


def convert(raw: dict) -> FetchedPuzzle:
    """Convert a Puzzmo payload to a Latin-1-safe .puz plus the original xd text."""
    try:
        puzzle = sanitize_for_puzfile(PuzzmoDownloader().parse_xword(raw))
        puz_bytes = puzzle.tobytes()
        xd = raw["puzzle"]
        if not isinstance(xd, str):
            raise TypeError("puzzle text missing")
    except Exception as exc:  # xword-dl/puzpy raise many types; normalise them
        raise FetchError(f"could not convert to .puz: {type(exc).__name__}: {exc}") from exc
    return FetchedPuzzle(
        title=raw.get("name") or "",
        author=puzzle.author or "",
        xd=xd,
        puz=puz_bytes,
    )


class XwordDlFetcher:
    """Production fetcher: anonymous download, then conversion."""

    def fetch(self, url_path: str) -> FetchedPuzzle:
        return convert(fetch_raw(url_path))
