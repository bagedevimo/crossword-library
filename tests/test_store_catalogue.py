from datetime import date, datetime, timezone

from crossword_library import store
from crossword_library.puzzmo import CalendarEntry

T1 = datetime(2026, 10, 5, 9, 15, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def entry(source_id="pz-1:puzzle", series="daily", day="2026-10-05", path=None, title="High Grades",
          completed=False, completed_at=None):
    suffix = {"daily": "", "mini": "/mini", "big": "/big"}[series]
    return CalendarEntry(
        source_id=source_id,
        series=series,
        puzzle_date=date.fromisoformat(day),
        url_path=path or f"{day}/crossword{suffix}",
        title=title,
        completed=completed,
        completed_at=completed_at,
    )


def test_upsert_inserts_then_is_noop(conn):
    puzzle_id, inserted = store.upsert_puzzle(conn, entry())
    assert inserted is True
    before = store.get_puzzle(conn, puzzle_id)
    again_id, inserted_again = store.upsert_puzzle(conn, entry())
    assert (again_id, inserted_again) == (puzzle_id, False)
    assert store.get_puzzle(conn, puzzle_id) == before  # updated_at untouched too


def test_big_across_days_is_one_row_dated_earliest(conn):
    later_id, _ = store.upsert_puzzle(conn, entry("big:puzzle", "big", "2026-10-06"))
    earlier_id, inserted = store.upsert_puzzle(conn, entry("big:puzzle", "big", "2026-10-05"))
    store.upsert_puzzle(conn, entry("big:puzzle", "big", "2026-10-07"))
    assert earlier_id == later_id and inserted is False
    row = store.get_puzzle(conn, later_id)
    assert row["puzzle_date"] == date(2026, 10, 5)
    assert row["source_url"] == "2026-10-05/crossword/big"
    assert conn.execute("SELECT count(*) FROM puzzles").fetchone()[0] == 1


def test_title_refresh(conn):
    puzzle_id, _ = store.upsert_puzzle(conn, entry(title="Old"))
    store.upsert_puzzle(conn, entry(title="New"))
    assert store.get_puzzle(conn, puzzle_id)["title"] == "New"


def test_puzzmo_completion_inserted_once(conn):
    puzzle_id, _ = store.upsert_puzzle(conn, entry())
    assert store.record_puzzmo_completion(conn, puzzle_id, T1) is True
    assert store.record_puzzmo_completion(conn, puzzle_id, T2) is False
    assert store.get_completion(conn, puzzle_id) == {
        "completed": True, "completed_at": T1, "source": "puzzmo",
    }


def test_puzzmo_completion_with_null_timestamp(conn):
    puzzle_id, _ = store.upsert_puzzle(conn, entry())
    assert store.record_puzzmo_completion(conn, puzzle_id, None) is True
    assert store.get_completion(conn, puzzle_id) == {
        "completed": True, "completed_at": None, "source": "puzzmo",
    }


def test_manual_unmark_wins_over_import(conn):
    puzzle_id, _ = store.upsert_puzzle(conn, entry())
    store.record_puzzmo_completion(conn, puzzle_id, T1)
    store.set_manual_completion(conn, puzzle_id, False, T2)
    assert store.record_puzzmo_completion(conn, puzzle_id, T1) is False
    assert store.get_completion(conn, puzzle_id) == {
        "completed": False, "completed_at": None, "source": "manual",
    }


def test_manual_mark(conn):
    puzzle_id, _ = store.upsert_puzzle(conn, entry())
    store.set_manual_completion(conn, puzzle_id, True, T2)
    assert store.get_completion(conn, puzzle_id) == {
        "completed": True, "completed_at": T2, "source": "manual",
    }


def test_no_completion_row(conn):
    puzzle_id, _ = store.upsert_puzzle(conn, entry())
    assert store.get_completion(conn, puzzle_id) is None


def test_resolve_puzzle(conn):
    daily_id, _ = store.upsert_puzzle(conn, entry("cmabc:puzzle", "daily"))
    big_id, _ = store.upsert_puzzle(conn, entry("cmbig:puzzle", "big"))
    assert store.resolve_puzzle(conn, str(daily_id)) == daily_id
    assert store.resolve_puzzle(conn, "2026-10-05/crossword") == daily_id
    assert store.resolve_puzzle(conn, "2026-10-05/crossword/big") == big_id
    assert store.resolve_puzzle(conn, "cmabc:puzzle") == daily_id
    assert store.resolve_puzzle(conn, "cmabc") == daily_id
    assert store.resolve_puzzle(conn, "2026-10-06/crossword/big") is None
    assert store.resolve_puzzle(conn, "999") is None


def test_resolve_puzzle_non_ascii_digit_is_none(conn):
    store.upsert_puzzle(conn, entry("cmabc:puzzle", "daily"))
    assert store.resolve_puzzle(conn, "²") is None


def test_resolve_puzzle_out_of_bigint_range_is_none(conn):
    store.upsert_puzzle(conn, entry("cmabc:puzzle", "daily"))
    assert store.resolve_puzzle(conn, "99999999999999999999") is None
