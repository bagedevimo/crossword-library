"""All SQL lives here. Functions take an autocommit psycopg connection."""

from __future__ import annotations

from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from .puzzmo import CalendarEntry

PUBLISHER = "puzzmo"


def upsert_puzzle(conn: psycopg.Connection, entry: CalendarEntry) -> tuple[int, bool]:
    """Insert or update a puzzle; keep the earliest date. Returns (id, inserted)."""
    params = {
        "publisher": PUBLISHER,
        "source_id": entry.source_id,
        "series": entry.series,
        "puzzle_date": entry.puzzle_date,
        "url_path": entry.url_path,
        "title": entry.title,
    }
    row = conn.execute(
        """
        INSERT INTO puzzles (publisher, source_id, series, puzzle_date, source_url, title)
        VALUES (%(publisher)s, %(source_id)s, %(series)s, %(puzzle_date)s, %(url_path)s, %(title)s)
        ON CONFLICT (publisher, source_id) DO UPDATE SET
            series = EXCLUDED.series,
            title = EXCLUDED.title,
            puzzle_date = LEAST(puzzles.puzzle_date, EXCLUDED.puzzle_date),
            source_url = CASE WHEN EXCLUDED.puzzle_date < puzzles.puzzle_date
                              THEN EXCLUDED.source_url ELSE puzzles.source_url END,
            updated_at = now()
        WHERE puzzles.series IS DISTINCT FROM EXCLUDED.series
           OR puzzles.title IS DISTINCT FROM EXCLUDED.title
           OR EXCLUDED.puzzle_date < puzzles.puzzle_date
        RETURNING id, (xmax = 0) AS inserted
        """,
        params,
    ).fetchone()
    if row is not None:
        return row[0], row[1]
    existing = conn.execute(
        "SELECT id FROM puzzles WHERE publisher = %(publisher)s AND source_id = %(source_id)s",
        params,
    ).fetchone()
    return existing[0], False


def record_puzzmo_completion(
    conn: psycopg.Connection, puzzle_id: int, completed_at: datetime | None
) -> bool:
    """Insert a Puzzmo completion. Never overwrites an existing row. Returns inserted?"""
    row = conn.execute(
        """
        INSERT INTO completions (puzzle_id, completed, completed_at, source)
        VALUES (%s, true, %s, 'puzzmo')
        ON CONFLICT (puzzle_id) DO NOTHING
        RETURNING puzzle_id
        """,
        (puzzle_id, completed_at),
    ).fetchone()
    return row is not None


def set_manual_completion(
    conn: psycopg.Connection, puzzle_id: int, completed: bool, at: datetime
) -> None:
    conn.execute(
        """
        INSERT INTO completions (puzzle_id, completed, completed_at, source, recorded_at)
        VALUES (%s, %s, %s, 'manual', now())
        ON CONFLICT (puzzle_id) DO UPDATE SET
            completed = EXCLUDED.completed,
            completed_at = EXCLUDED.completed_at,
            source = 'manual',
            recorded_at = now()
        """,
        (puzzle_id, completed, at if completed else None),
    )


def get_completion(conn: psycopg.Connection, puzzle_id: int) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT completed, completed_at, source FROM completions WHERE puzzle_id = %s",
            (puzzle_id,),
        ).fetchone()


def get_puzzle(conn: psycopg.Connection, puzzle_id: int) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM puzzles WHERE id = %s", (puzzle_id,)).fetchone()


def resolve_puzzle(conn: psycopg.Connection, ref: str) -> int | None:
    """Find a puzzle by internal id, first-appearance urlPath, or Puzzmo id (':puzzle' optional)."""
    ref = ref.strip()
    if ref.isascii() and ref.isdigit() and int(ref) <= 2**63 - 1:
        row = conn.execute("SELECT id FROM puzzles WHERE id = %s", (int(ref),)).fetchone()
        if row:
            return row[0]
    row = conn.execute(
        """
        SELECT id FROM puzzles
        WHERE source_url = %(ref)s OR source_id = %(ref)s OR source_id = %(ref)s || ':puzzle'
        ORDER BY id LIMIT 1
        """,
        {"ref": ref},
    ).fetchone()
    return row[0] if row else None
