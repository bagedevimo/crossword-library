"""One sync run: calendar -> catalogue + completions -> downloads."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import psycopg

from . import db, store
from .fetch import FetchError
from .puzzmo import PuzzmoAuthError, PuzzmoError

log = logging.getLogger(__name__)

TOKEN_WARNING = timedelta(days=30)
MAX_MESSAGES = 100


class SyncAlreadyRunning(Exception):
    """Another process holds the sync lock."""


@dataclass
class SyncResult:
    run_id: int
    status: str
    counts: dict
    messages: list[dict]


@dataclass
class _RunState:
    counts: dict = field(
        default_factory=lambda: {
            "months_scanned": 0,
            "months_failed": 0,
            "puzzles_added": 0,
            "completions_added": 0,
            "files_fetched": 0,
            "download_failures": 0,
        }
    )
    messages: list[dict] = field(default_factory=list)
    has_errors: bool = False
    fatal: bool = False
    dropped: int = 0

    def _add(self, level: str, message: str) -> None:
        getattr(log, "error" if level == "error" else "warning")(message)
        if len(self.messages) < MAX_MESSAGES:
            self.messages.append({"level": level, "message": message})
        else:
            self.dropped += 1

    def warn(self, message: str) -> None:
        self._add("warning", message)

    def error(self, message: str) -> None:
        self.has_errors = True
        self._add("error", message)

    def final_messages(self) -> list[dict]:
        if self.dropped:
            return self.messages + [
                {"level": "warning", "message": f"{self.dropped} further messages omitted"}
            ]
        return list(self.messages)


def months_between(start: date, end: date) -> list[tuple[int, int]]:
    """Inclusive list of (year, month) from start's month to end's month."""
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_sync(
    database_url: str,
    client,
    fetcher,
    *,
    since: date,
    now: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], None] = time.sleep,
    download_delay: float = 3.0,
    force_retry: bool = False,
    token_expires_at: datetime | None = None,
) -> SyncResult:
    lock_conn = db.connect(database_url)
    acquired = False
    try:
        acquired = store.try_advisory_lock(lock_conn)
        if not acquired:
            raise SyncAlreadyRunning("another sync is already running")
        with db.connect(database_url) as conn:
            return _locked_sync(
                conn, client, fetcher, since, now, sleep, download_delay, force_retry,
                token_expires_at,
            )
    finally:
        if acquired:
            try:
                store.release_advisory_lock(lock_conn)
            except Exception:
                log.warning("could not release sync lock explicitly; closing connection releases it")
        lock_conn.close()


def _locked_sync(
    conn: psycopg.Connection,
    client,
    fetcher,
    since: date,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    download_delay: float,
    force_retry: bool,
    token_expires_at: datetime | None,
) -> SyncResult:
    abandoned = store.abandon_running_runs(conn, now())
    if abandoned:
        log.warning("marked %d abandoned run(s) as failed", abandoned)
    run_id = store.start_run(conn, now(), token_expires_at)
    state = _RunState()

    if token_expires_at is not None and token_expires_at - now() < TOKEN_WARNING:
        state.warn(f"PUZZMO_TOKEN expires {token_expires_at:%Y-%m-%d}; replace it soon")

    try:
        if _scan_calendar(conn, client, since, now().date(), state):
            _download_missing(conn, fetcher, now, sleep, download_delay, force_retry, state)
    except BaseException as exc:
        state.error(f"run aborted: {type(exc).__name__}: {exc}")
        try:
            store.finish_run(conn, run_id, now(), "failed", state.counts, state.final_messages())
        except Exception:
            log.exception("could not record failed run %s", run_id)
        raise

    status = "failed" if state.fatal else ("partial" if state.has_errors else "ok")
    messages = state.final_messages()
    store.finish_run(conn, run_id, now(), status, state.counts, messages)
    log.info("sync %s: %s", status, state.counts)
    return SyncResult(run_id, status, dict(state.counts), messages)


def _scan_calendar(conn, client, since: date, today: date, state: _RunState) -> bool:
    """Returns False if the run must stop (token rejected)."""
    for year, month in months_between(since, today):
        try:
            entries = client.fetch_month(year, month)
        except PuzzmoAuthError as exc:
            state.fatal = True
            state.error(str(exc))
            return False
        except PuzzmoError as exc:
            state.counts["months_failed"] += 1
            state.error(f"calendar {year}-{month:02d}: {exc}")
            continue
        with conn.transaction():
            for entry in entries:
                puzzle_id, inserted = store.upsert_puzzle(conn, entry)
                if inserted:
                    state.counts["puzzles_added"] += 1
                if entry.completed and store.record_puzzmo_completion(
                    conn, puzzle_id, entry.completed_at
                ):
                    state.counts["completions_added"] += 1
        state.counts["months_scanned"] += 1
    return True


def _download_missing(conn, fetcher, now, sleep, download_delay, force_retry, state) -> None:
    pending = store.puzzles_needing_download(conn, now(), force_retry)
    for index, item in enumerate(pending):
        if index:
            sleep(download_delay)
        try:
            fetched = fetcher.fetch(item.url_path)
        except Exception as exc:  # FetchError, or anything unexpected from xword-dl
            reason = str(exc) if isinstance(exc, FetchError) else f"{type(exc).__name__}: {exc}"
            store.record_download_failure(conn, item.puzzle_id, reason, now())
            state.counts["download_failures"] += 1
            state.error(f"download {item.url_path}: {reason}")
            continue
        store.save_download(
            conn, item.puzzle_id, xd=fetched.xd, puz=fetched.puz, author=fetched.author, now=now()
        )
        state.counts["files_fetched"] += 1
        if fetched.title.strip() != item.title.strip():
            state.warn(
                f"title mismatch for {item.url_path}: calendar {item.title!r}, "
                f"puzzle {fetched.title!r}"
            )
