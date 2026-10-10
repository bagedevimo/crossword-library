"""All SQL lives here. Functions take an autocommit psycopg connection."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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


LOCK_KEY = 0x63726F73  # "cros"
MAX_DOWNLOAD_ATTEMPTS = 5
RETRY_AFTER = timedelta(hours=24)


@dataclass(frozen=True)
class PendingDownload:
    puzzle_id: int
    url_path: str
    title: str


def puzzles_needing_download(
    conn: psycopg.Connection, now: datetime, force_retry: bool = False
) -> list[PendingDownload]:
    rows = conn.execute(
        """
        SELECT p.id, p.source_url, p.title
        FROM puzzles p
        WHERE NOT EXISTS (
                SELECT 1 FROM puzzle_files f WHERE f.puzzle_id = p.id AND f.format = 'puz')
          AND (%(force)s OR (
                p.download_attempts < %(max_attempts)s
                AND (p.last_download_attempt_at IS NULL
                     OR p.last_download_attempt_at <= %(cutoff)s)))
        ORDER BY p.puzzle_date DESC, p.id
        """,
        {"force": force_retry, "max_attempts": MAX_DOWNLOAD_ATTEMPTS, "cutoff": now - RETRY_AFTER},
    ).fetchall()
    return [PendingDownload(*row) for row in rows]


def save_download(
    conn: psycopg.Connection, puzzle_id: int, *, xd: str, puz: bytes, author: str, now: datetime
) -> None:
    with conn.transaction():
        for fmt, content in (("xd", xd.encode("utf-8")), ("puz", puz)):
            conn.execute(
                """
                INSERT INTO puzzle_files (puzzle_id, format, content, sha256, fetched_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (puzzle_id, format) DO UPDATE SET
                    content = EXCLUDED.content,
                    sha256 = EXCLUDED.sha256,
                    fetched_at = EXCLUDED.fetched_at
                """,
                (puzzle_id, fmt, content, hashlib.sha256(content).hexdigest(), now),
            )
        conn.execute(
            """
            UPDATE puzzles SET
                author = %s,
                download_attempts = download_attempts + 1,
                last_download_attempt_at = %s,
                last_download_error = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (author or None, now, puzzle_id),
        )


def record_download_failure(
    conn: psycopg.Connection, puzzle_id: int, error: str, now: datetime
) -> None:
    conn.execute(
        """
        UPDATE puzzles SET
            download_attempts = download_attempts + 1,
            last_download_attempt_at = %s,
            last_download_error = %s
        WHERE id = %s
        """,
        (now, error[:2000], puzzle_id),
    )


def get_file(conn: psycopg.Connection, puzzle_id: int, fmt: str) -> bytes | None:
    row = conn.execute(
        "SELECT content FROM puzzle_files WHERE puzzle_id = %s AND format = %s", (puzzle_id, fmt)
    ).fetchone()
    return bytes(row[0]) if row else None


def try_advisory_lock(conn: psycopg.Connection) -> bool:
    """Session-level lock; released when the connection closes."""
    return conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]


def release_advisory_lock(conn: psycopg.Connection) -> None:
    """Release the sync lock now; closing the connection also releases it, but asynchronously."""
    conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def abandon_running_runs(conn: psycopg.Connection, now: datetime) -> int:
    """Call only while holding the advisory lock: no other run can be live."""
    cur = conn.execute(
        """
        UPDATE sync_runs SET
            status = 'failed',
            finished_at = %s,
            errors = errors || %s
        WHERE status = 'running'
        """,
        (now, Jsonb([{"level": "error", "message": "abandoned: run did not finish (process stopped)"}])),
    )
    return cur.rowcount


def start_run(conn: psycopg.Connection, now: datetime, token_expires_at: datetime | None) -> int:
    return conn.execute(
        "INSERT INTO sync_runs (started_at, status, token_expires_at) VALUES (%s, 'running', %s) RETURNING id",
        (now, token_expires_at),
    ).fetchone()[0]


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    now: datetime,
    status: str,
    counts: dict,
    messages: list[dict],
) -> None:
    conn.execute(
        "UPDATE sync_runs SET finished_at = %s, status = %s, counts = %s, errors = %s WHERE id = %s",
        (now, status, Jsonb(counts), Jsonb(messages), run_id),
    )


def recent_runs(conn: psycopg.Connection, limit: int = 5) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT %s", (limit,)
        ).fetchall()


def problem_puzzles(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT p.id, p.source_url, p.title, p.download_attempts, p.last_download_error
            FROM puzzles p
            WHERE p.download_attempts >= %s
              AND NOT EXISTS (
                SELECT 1 FROM puzzle_files f WHERE f.puzzle_id = p.id AND f.format = 'puz')
            ORDER BY p.puzzle_date DESC
            """,
            (MAX_DOWNLOAD_ATTEMPTS,),
        ).fetchall()


def catalogue_counts(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT p.series,
                   count(*) AS total,
                   count(f.puzzle_id) AS downloaded,
                   count(*) FILTER (WHERE c.completed) AS completed
            FROM puzzles p
            LEFT JOIN puzzle_files f ON f.puzzle_id = p.id AND f.format = 'puz'
            LEFT JOIN completions c ON c.puzzle_id = p.id
            GROUP BY p.series
            ORDER BY p.series
            """
        ).fetchall()


def list_puzzles(
    conn: psycopg.Connection,
    *,
    series: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT p.id, p.puzzle_date, p.series, p.title, p.source_url,
                   coalesce(c.completed, false) AS completed,
                   (f.puzzle_id IS NOT NULL) AS downloaded
            FROM puzzles p
            LEFT JOIN completions c ON c.puzzle_id = p.id
            LEFT JOIN puzzle_files f ON f.puzzle_id = p.id AND f.format = 'puz'
            WHERE (%(series)s::text IS NULL OR p.series = %(series)s)
              AND (%(state)s::text IS NULL
                   OR (%(state)s = 'done' AND coalesce(c.completed, false))
                   OR (%(state)s = 'todo' AND NOT coalesce(c.completed, false)))
            ORDER BY p.puzzle_date DESC, p.series, p.id
            LIMIT %(limit)s
            """,
            {"series": series, "state": state, "limit": limit},
        ).fetchall()
