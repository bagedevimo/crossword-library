from datetime import date, datetime, timezone

import pytest

from crossword_library import cli, store
from crossword_library.fetch import FetchedPuzzle
from crossword_library.puzzmo import CalendarEntry
from crossword_library.sync import SyncAlreadyRunning


@pytest.fixture
def env(monkeypatch, database_url, conn):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("PUZZMO_TOKEN", "header.eyJleHAiOjE4MDgyNzc3ODB9.sig")
    monkeypatch.setenv("DOWNLOAD_DELAY_SECONDS", "0")
    monkeypatch.setenv("SYNC_SINCE", "2026-10")
    return conn


def add(conn, source_id="d5", day="2026-10-05", title="High Grades"):
    pid, _ = store.upsert_puzzle(conn, CalendarEntry(
        source_id, "daily", date.fromisoformat(day), f"{day}/crossword", title, False, None))
    return pid


class FakeClient:
    def __init__(self, entries):
        self.entries = entries

    def fetch_month(self, year, month):
        return [e for e in self.entries if (e.puzzle_date.year, e.puzzle_date.month) == (year, month)]


class FakeFetcher:
    def fetch(self, url_path):
        return FetchedPuzzle(title="High Grades", author="A", xd="xd", puz=b"PUZ")


def test_missing_database_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main(["status"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_migrate(env, capsys):
    assert cli.main(["migrate"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_sync_requires_token(env, monkeypatch, capsys):
    monkeypatch.delenv("PUZZMO_TOKEN")
    assert cli.main(["sync"]) == 2
    assert "PUZZMO_TOKEN" in capsys.readouterr().err


def test_sync_runs_and_reports(env, monkeypatch, capsys):
    entries = [CalendarEntry("d5", "daily", date(2026, 10, 5), "2026-10-05/crossword",
                             "High Grades", True, datetime(2026, 10, 5, tzinfo=timezone.utc))]
    tokens = []
    monkeypatch.setattr(cli, "make_client", lambda token: tokens.append(token) or FakeClient(entries))
    monkeypatch.setattr(cli, "make_fetcher", lambda: FakeFetcher())
    assert cli.main(["sync", "--since", "2026-10"]) == 0
    out = capsys.readouterr().out
    assert "ok" in out and "files_fetched=1" in out
    assert tokens == ["header.eyJleHAiOjE4MDgyNzc3ODB9.sig"]
    assert "eyJleHAi" not in out  # token never printed


def test_sync_already_running_exit_code(env, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise SyncAlreadyRunning("another sync is already running")

    monkeypatch.setattr(cli, "run_sync", boom)
    assert cli.main(["sync"]) == 3
    assert "already running" in capsys.readouterr().err


def test_sync_bad_since(env, capsys):
    assert cli.main(["sync", "--since", "2026-10-05"]) == 2


def test_mark_and_unmark(env, capsys):
    pid = add(env)
    assert cli.main(["mark", "2026-10-05/crossword", "--at", "2026-10-06T08:00:00+00:00"]) == 0
    assert store.get_completion(env, pid) == {
        "completed": True, "completed_at": datetime(2026, 10, 6, 8, tzinfo=timezone.utc),
        "source": "manual"}
    assert cli.main(["unmark", str(pid)]) == 0
    assert store.get_completion(env, pid)["completed"] is False
    assert "High Grades" in capsys.readouterr().out


def test_mark_unknown_reference(env, capsys):
    assert cli.main(["mark", "2026-10-06/crossword/big"]) == 1
    err = capsys.readouterr().err
    assert "no puzzle matches" in err and "Traceback" not in err


def test_list(env, capsys):
    done = add(env, "d5", "2026-10-05", "High Grades")
    add(env, "d6", "2026-10-06", "Stretching the Joke")
    store.record_puzzmo_completion(env, done, None)
    assert cli.main(["list", "--done"]) == 0
    out = capsys.readouterr().out
    assert "High Grades" in out and "Stretching the Joke" not in out


def test_status(env, capsys):
    add(env)
    run_id = store.start_run(env, datetime(2026, 10, 10, tzinfo=timezone.utc), None)
    store.finish_run(env, run_id, datetime(2026, 10, 10, 0, 5, tzinfo=timezone.utc), "ok",
                     {"files_fetched": 0}, [])
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "daily" in out and "ok" in out
    assert "token expires 2027-04-21" in out
