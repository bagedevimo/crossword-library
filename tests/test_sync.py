from datetime import date, datetime, timedelta, timezone

import pytest

from crossword_library import db, store
from crossword_library.fetch import FetchError, FetchedPuzzle
from crossword_library.puzzmo import CalendarEntry, PuzzmoAPIError, PuzzmoAuthError
from crossword_library.sync import SyncAlreadyRunning, months_between, run_sync

NOW = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
DONE_AT = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def entry(source_id, day, series="daily", title=None, completed=False, completed_at=None):
    suffix = {"daily": "", "mini": "/mini", "big": "/big"}[series]
    return CalendarEntry(source_id, series, date.fromisoformat(day), f"{day}/crossword{suffix}",
                         title or f"Title {source_id}", completed, completed_at)


class FakeClient:
    def __init__(self, months):
        self.months = months  # {(year, month): list[CalendarEntry] | Exception}
        self.calls = []

    def fetch_month(self, year, month):
        self.calls.append((year, month))
        result = self.months.get((year, month), [])
        if isinstance(result, Exception):
            raise result
        return result


class FakeFetcher:
    def __init__(self, results=None, default_title=None):
        self.results = results or {}  # {url_path: FetchedPuzzle | Exception}
        self.default_title = default_title
        self.calls = []

    def fetch(self, url_path):
        self.calls.append(url_path)
        result = self.results.get(url_path)
        if isinstance(result, BaseException):
            raise result
        if result is None:
            return FetchedPuzzle(title=self.default_title(url_path), author="A", xd="xd", puz=b"PUZ")
        return result


def titles_from(entries):
    by_path = {e.url_path: e.title for e in entries}
    return lambda url_path: by_path[url_path]


def sync(database_url, client, fetcher, **kwargs):
    sleeps = []
    kwargs.setdefault("since", date(2026, 9, 1))
    result = run_sync(database_url, client, fetcher, now=lambda: NOW, sleep=sleeps.append,
                      download_delay=3.0, **kwargs)
    return result, sleeps


OCT = [
    entry("d5", "2026-10-05", completed=True, completed_at=DONE_AT),
    entry("m5", "2026-10-05", "mini", completed=True, completed_at=None),
    entry("big", "2026-10-05", "big"),
    entry("big", "2026-10-06", "big"),
]
SEP = [entry("d30", "2026-09-30")]


def test_months_between():
    assert months_between(date(2025, 11, 15), date(2026, 2, 1)) == [
        (2025, 11), (2025, 12), (2026, 1), (2026, 2)]
    assert months_between(date(2026, 10, 1), date(2026, 10, 31)) == [(2026, 10)]


def test_full_sync(conn, database_url):
    client = FakeClient({(2026, 9): SEP, (2026, 10): OCT})
    fetcher = FakeFetcher(default_title=titles_from(SEP + OCT))
    result, sleeps = sync(database_url, client, fetcher)

    assert result.status == "ok"
    assert client.calls == [(2026, 9), (2026, 10)]
    assert result.counts == {"months_scanned": 2, "months_failed": 0, "puzzles_added": 4,
                             "completions_added": 2, "files_fetched": 4, "download_failures": 0}
    assert fetcher.calls == ["2026-10-05/crossword", "2026-10-05/crossword/mini",
                             "2026-10-05/crossword/big", "2026-09-30/crossword"]
    assert sleeps == [3.0, 3.0, 3.0]
    assert conn.execute("SELECT count(*) FROM puzzles").fetchone()[0] == 4
    (run,) = store.recent_runs(conn)
    assert run["status"] == "ok" and run["counts"] == result.counts


def test_second_sync_changes_nothing(conn, database_url):
    client = FakeClient({(2026, 9): SEP, (2026, 10): OCT})
    fetcher = FakeFetcher(default_title=titles_from(SEP + OCT))
    sync(database_url, client, fetcher)
    snapshot = conn.execute("SELECT * FROM puzzles ORDER BY id").fetchall()
    fetcher.calls.clear()
    result, sleeps = sync(database_url, client, fetcher)
    assert result.counts["puzzles_added"] == 0
    assert result.counts["completions_added"] == 0
    assert fetcher.calls == [] and sleeps == []
    assert conn.execute("SELECT * FROM puzzles ORDER BY id").fetchall() == snapshot


def test_manual_unmark_survives_sync(conn, database_url):
    client = FakeClient({(2026, 10): OCT})
    fetcher = FakeFetcher(default_title=titles_from(OCT))
    sync(database_url, client, fetcher)
    pid = store.resolve_puzzle(conn, "2026-10-05/crossword")
    store.set_manual_completion(conn, pid, False, NOW)
    sync(database_url, client, fetcher)
    assert store.get_completion(conn, pid)["completed"] is False


def test_auth_error_aborts_without_downloads(conn, database_url):
    client = FakeClient({(2026, 9): PuzzmoAuthError("Puzzmo rejected the token; replace PUZZMO_TOKEN")})
    fetcher = FakeFetcher()
    result, _ = sync(database_url, client, fetcher)
    assert result.status == "failed"
    assert client.calls == [(2026, 9)]
    assert fetcher.calls == []
    assert any("PUZZMO_TOKEN" in m["message"] for m in result.messages)


def test_month_error_is_partial_and_continues(conn, database_url):
    client = FakeClient({(2026, 9): PuzzmoAPIError("HTTP 500"), (2026, 10): OCT})
    result, _ = sync(database_url, client, FakeFetcher(default_title=titles_from(OCT)))
    assert result.status == "partial"
    assert result.counts["months_failed"] == 1
    assert result.counts["months_scanned"] == 1
    assert result.counts["files_fetched"] == 3


def test_download_failures_including_unexpected_exceptions(conn, database_url):
    client = FakeClient({(2026, 10): OCT})
    fetcher = FakeFetcher(
        results={
            "2026-10-05/crossword": FetchError("Puzzmo error: No puzzle found"),
            "2026-10-05/crossword/mini": TypeError("'NoneType' object is not subscriptable"),
        },
        default_title=titles_from(OCT),
    )
    result, _ = sync(database_url, client, fetcher)
    assert result.status == "partial"
    assert result.counts["download_failures"] == 2
    assert result.counts["files_fetched"] == 1
    mini = store.get_puzzle(conn, store.resolve_puzzle(conn, "2026-10-05/crossword/mini"))
    assert mini["download_attempts"] == 1
    assert "TypeError" in mini["last_download_error"]


def test_title_mismatch_warns_but_stores(conn, database_url):
    entries = [entry("d5", "2026-10-05", title="Lay the table with the fancy sh—")]
    client = FakeClient({(2026, 10): entries})
    fetcher = FakeFetcher(results={"2026-10-05/crossword": FetchedPuzzle(
        title="Something Else", author="A", xd="xd", puz=b"PUZ")})
    result, _ = sync(database_url, client, fetcher)
    assert result.status == "ok"
    assert [m["level"] for m in result.messages] == ["warning"]
    assert "title mismatch" in result.messages[0]["message"]
    assert result.counts["files_fetched"] == 1


def test_sanitised_title_is_not_a_mismatch(conn, database_url):
    title = "Lay the table with the fancy sh—"
    client = FakeClient({(2026, 10): [entry("d5", "2026-10-05", title=title)]})
    fetcher = FakeFetcher(results={"2026-10-05/crossword": FetchedPuzzle(
        title=title, author="A", xd="xd", puz=b"PUZ")})
    result, _ = sync(database_url, client, fetcher)
    assert result.messages == []


def test_token_expiry_warning(conn, database_url):
    result, _ = sync(database_url, FakeClient({}), FakeFetcher(),
                     token_expires_at=NOW + timedelta(days=10))
    assert result.status == "ok"
    assert any(m["level"] == "warning" and "expires" in m["message"] for m in result.messages)
    assert store.recent_runs(conn)[0]["token_expires_at"] == NOW + timedelta(days=10)


def test_already_running(conn, database_url):
    holder = db.connect(database_url)
    try:
        assert store.try_advisory_lock(holder)
        with pytest.raises(SyncAlreadyRunning):
            sync(database_url, FakeClient({}), FakeFetcher())
        assert store.recent_runs(conn) == []
    finally:
        holder.close()


def test_abandoned_runs_cleaned_up(conn, database_url):
    stale = store.start_run(conn, NOW - timedelta(days=1), None)
    sync(database_url, FakeClient({}), FakeFetcher())
    runs = {r["id"]: r for r in store.recent_runs(conn)}
    assert runs[stale]["status"] == "failed"


def test_system_exit_marks_run_failed_and_releases_lock(conn, database_url):
    client = FakeClient({(2026, 10): OCT})
    fetcher = FakeFetcher(results={"2026-10-05/crossword": SystemExit(0)},
                          default_title=titles_from(OCT))
    with pytest.raises(SystemExit):
        sync(database_url, client, fetcher)
    (run,) = store.recent_runs(conn)
    assert run["status"] == "failed"
    assert any("aborted" in m["message"] for m in run["errors"])
    probe = db.connect(database_url)
    try:
        assert store.try_advisory_lock(probe) is True
    finally:
        probe.close()
