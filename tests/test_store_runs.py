import time
from datetime import date, datetime, timedelta, timezone

from crossword_library import db, store
from crossword_library.puzzmo import CalendarEntry

NOW = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)


def add(conn, source_id, day, series="daily", title="T"):
    suffix = {"daily": "", "mini": "/mini", "big": "/big"}[series]
    puzzle_id, _ = store.upsert_puzzle(
        conn,
        CalendarEntry(source_id, series, date.fromisoformat(day), f"{day}/crossword{suffix}",
                      title, False, None),
    )
    return puzzle_id


def test_needing_download_newest_first_and_skips_downloaded(conn):
    old = add(conn, "a", "2026-10-01")
    new = add(conn, "b", "2026-10-08")
    done = add(conn, "c", "2026-10-05")
    store.save_download(conn, done, xd="xd", puz=b"PUZ", author="A", now=NOW)
    assert [p.puzzle_id for p in store.puzzles_needing_download(conn, NOW)] == [new, old]
    first = store.puzzles_needing_download(conn, NOW)[0]
    assert (first.url_path, first.title) == ("2026-10-08/crossword", "T")


def test_save_download_stores_both_formats(conn):
    pid = add(conn, "a", "2026-10-01")
    store.record_download_failure(conn, pid, "boom", NOW - timedelta(days=2))
    store.save_download(conn, pid, xd="## Grid\nAB", puz=b"\x00PUZ", author="Ann", now=NOW)
    assert store.get_file(conn, pid, "xd") == "## Grid\nAB".encode()
    assert store.get_file(conn, pid, "puz") == b"\x00PUZ"
    row = store.get_puzzle(conn, pid)
    assert row["author"] == "Ann"
    assert row["last_download_error"] is None
    assert row["download_attempts"] == 2


def test_failed_download_retry_rules(conn):
    pid = add(conn, "a", "2026-10-01")
    store.record_download_failure(conn, pid, "No puzzle found", NOW)
    assert store.puzzles_needing_download(conn, NOW + timedelta(hours=23)) == []
    assert [p.puzzle_id for p in store.puzzles_needing_download(conn, NOW + timedelta(hours=24))] == [pid]
    for i in range(1, 5):
        store.record_download_failure(conn, pid, "No puzzle found", NOW + timedelta(days=i))
    later = NOW + timedelta(days=30)
    assert store.puzzles_needing_download(conn, later) == []  # 5 attempts: given up
    assert [p.puzzle_id for p in store.puzzles_needing_download(conn, later, force_retry=True)] == [pid]
    problems = store.problem_puzzles(conn)
    assert [(p["id"], p["download_attempts"], p["last_download_error"]) for p in problems] == [
        (pid, 5, "No puzzle found")
    ]


def test_advisory_lock_is_exclusive_and_released_on_close(database_url):
    first = db.connect(database_url)
    second = db.connect(database_url)
    try:
        assert store.try_advisory_lock(first) is True
        assert store.try_advisory_lock(second) is False
        first.close()
        # Postgres releases the lock asynchronously after the client disconnects.
        deadline = time.monotonic() + 2
        acquired = store.try_advisory_lock(second)
        while not acquired and time.monotonic() < deadline:
            time.sleep(0.02)
            acquired = store.try_advisory_lock(second)
        assert acquired is True
    finally:
        first.close()
        second.close()


def test_release_advisory_lock_is_immediate(database_url):
    first = db.connect(database_url)
    second = db.connect(database_url)
    try:
        assert store.try_advisory_lock(first) is True
        store.release_advisory_lock(first)
        assert store.try_advisory_lock(second) is True
    finally:
        first.close()
        second.close()


def test_run_lifecycle(conn):
    run_id = store.start_run(conn, NOW, NOW + timedelta(days=200))
    store.finish_run(conn, run_id, NOW + timedelta(minutes=5), "partial",
                     {"files_fetched": 2}, [{"level": "error", "message": "x"}])
    (run,) = store.recent_runs(conn)
    assert run["id"] == run_id
    assert run["status"] == "partial"
    assert run["counts"] == {"files_fetched": 2}
    assert run["errors"] == [{"level": "error", "message": "x"}]
    assert run["finished_at"] == NOW + timedelta(minutes=5)
    assert run["token_expires_at"] == NOW + timedelta(days=200)


def test_abandon_running_runs(conn):
    stale = store.start_run(conn, NOW, None)
    finished = store.start_run(conn, NOW, None)
    store.finish_run(conn, finished, NOW, "ok", {}, [])
    assert store.abandon_running_runs(conn, NOW + timedelta(hours=1)) == 1
    runs = {r["id"]: r for r in store.recent_runs(conn)}
    assert runs[stale]["status"] == "failed"
    assert "abandoned" in runs[stale]["errors"][-1]["message"]
    assert runs[finished]["status"] == "ok"


def test_catalogue_counts_and_list(conn):
    d1 = add(conn, "d1", "2026-10-05", "daily", "Daily One")
    d2 = add(conn, "d2", "2026-10-06", "daily", "Daily Two")
    m1 = add(conn, "m1", "2026-10-05", "mini", "Mini One")
    store.save_download(conn, d1, xd="x", puz=b"p", author="", now=NOW)
    store.record_puzzmo_completion(conn, d1, NOW)
    store.set_manual_completion(conn, d2, False, NOW)
    counts = {c["series"]: c for c in store.catalogue_counts(conn)}
    assert counts["daily"] == {"series": "daily", "total": 2, "downloaded": 1, "completed": 1}
    assert counts["mini"] == {"series": "mini", "total": 1, "downloaded": 0, "completed": 0}

    assert [p["id"] for p in store.list_puzzles(conn)] == [d2, d1, m1]
    assert [p["id"] for p in store.list_puzzles(conn, state="done")] == [d1]
    assert [p["id"] for p in store.list_puzzles(conn, state="todo")] == [d2, m1]
    assert [p["id"] for p in store.list_puzzles(conn, series="mini")] == [m1]
    first = store.list_puzzles(conn, limit=1)[0]
    assert first == {"id": d2, "puzzle_date": date(2026, 10, 6), "series": "daily",
                     "title": "Daily Two", "source_url": "2026-10-06/crossword",
                     "completed": False, "downloaded": False}
