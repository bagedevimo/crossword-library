import json
from pathlib import Path

import pytest
import puz
import requests
import responses

from crossword_library import fetch
from crossword_library.fetch import FetchError, FetchedPuzzle, XwordDlFetcher, convert

PUZZMO_GRAPHQL = "https://www.puzzmo.com/_api/prod/graphql"

PUZZLES = Path(__file__).parent / "fixtures" / "puzzles"


def raw(name):
    return json.loads((PUZZLES / name).read_text(encoding="utf-8"))


def load_puz(data: bytes) -> puz.Puzzle:
    return puz.load(data)


def test_convert_daily():
    fetched = convert(raw("daily.json"))
    assert fetched.title == "Synthetic Daily"
    assert fetched.author == "Test Author"
    assert "## Grid" in fetched.xd
    parsed = load_puz(fetched.puz)
    assert (parsed.width, parsed.height) == (4, 4)
    assert parsed.title == "Synthetic Daily"
    assert parsed.solution == "CARDAREAREARDART"
    assert len(parsed.clues) == 8


def test_convert_mini_with_emoji_is_latin1_safe():
    payload = raw("mini.json")
    assert any(ord(ch) > 255 for ch in payload["puzzle"])  # raw text has emoji
    fetched = convert(payload)
    parsed = load_puz(fetched.puz)
    assert (parsed.width, parsed.height) == (3, 3)
    assert parsed.title == "Synthetic Mini"
    assert any(":bat:" in clue for clue in parsed.clues)
    for clue in parsed.clues:
        assert all(ord(ch) < 256 for ch in clue)


def test_convert_big_keeps_circles():
    fetched = convert(raw("big.json"))
    parsed = load_puz(fetched.puz)
    assert (parsed.width, parsed.height) == (5, 5)
    assert b"GEXT" in parsed.extensions


def test_title_is_raw_puzzmo_name():
    payload = raw("daily.json") | {"name": "Lay the table with the fancy sh—"}
    fetched = convert(payload)
    assert fetched.title == "Lay the table with the fancy sh—"
    assert load_puz(fetched.puz).title == "Lay the table with the fancy sh--"


def test_convert_garbage_raises_fetch_error():
    with pytest.raises(FetchError, match="convert"):
        convert({"name": "x", "puzzle": None, "authors": []})


def test_fetcher_uses_fetch_raw(monkeypatch):
    calls = []

    def fake_fetch_raw(url_path):
        calls.append(url_path)
        return raw("mini.json")

    monkeypatch.setattr(fetch, "fetch_raw", fake_fetch_raw)
    fetched = XwordDlFetcher().fetch("2026-10-08/crossword/mini")
    assert calls == ["2026-10-08/crossword/mini"]
    assert isinstance(fetched, FetchedPuzzle)


def test_downloader_finder_key():
    downloader = fetch._UrlPathDownloader("2026-10-05/crossword/big")
    assert downloader.finder_key == "today:/2026-10-05/crossword/big"
    assert "authorization" not in {k.lower() for k in downloader.session.headers}


@responses.activate
def test_download_requests_carry_timeout():
    responses.post(PUZZMO_GRAPHQL, json={"data": {}})
    with pytest.raises(FetchError):
        fetch.fetch_raw("2026-10-05/crossword")
    assert responses.calls[0].request.req_kwargs["timeout"] == fetch.DOWNLOAD_TIMEOUT_SECONDS == 30


@responses.activate
def test_download_timeout_is_fetch_error():
    responses.post(PUZZMO_GRAPHQL, body=requests.Timeout("slow"))
    with pytest.raises(FetchError):
        fetch.fetch_raw("2026-10-05/crossword")
