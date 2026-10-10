"""Opt-in checks against the real Puzzmo API: CROSSWORD_LIVE=1 uv run pytest tests/test_live.py"""

import os
from datetime import date

import pytest

from crossword_library.fetch import XwordDlFetcher
from crossword_library.puzzmo import PuzzmoClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("CROSSWORD_LIVE") != "1", reason="set CROSSWORD_LIVE=1"),
]


def test_live_calendar_current_month():
    token = os.environ.get("PUZZMO_TOKEN")
    if not token:
        pytest.skip("PUZZMO_TOKEN not set")
    today = date.today()
    entries = PuzzmoClient(token).fetch_month(today.year, today.month)
    assert {"daily", "mini"} <= {e.series for e in entries}


def test_live_anonymous_download():
    fetched = XwordDlFetcher().fetch("2026-10-08/crossword")
    assert fetched.title == "a pisces and a gemini"
    assert fetched.puz[2:14] == b"ACROSS&DOWN\x00"
