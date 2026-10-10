"""Command-line entry point: `crossword-library <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone

from . import db, store
from .config import ConfigError, Settings, load_settings
from .fetch import XwordDlFetcher
from .puzzmo import PuzzmoClient, token_expires_at
from .sync import SyncAlreadyRunning, SyncResult, run_sync

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_ALREADY_RUNNING = 3


def make_client(token: str) -> PuzzmoClient:
    return PuzzmoClient(token)


def make_fetcher() -> XwordDlFetcher:
    return XwordDlFetcher()


def _parse_month(value: str) -> date:
    try:
        year, month = value.split("-")
        return date(int(year), int(month), 1)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}") from None


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an ISO date/time, got {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def run_sync_from_settings(
    settings: Settings, *, since: date | None = None, force_retry: bool = False
) -> SyncResult:
    if not settings.puzzmo_token:
        raise ConfigError("PUZZMO_TOKEN is required for sync")
    return run_sync(
        settings.database_url,
        make_client(settings.puzzmo_token),
        make_fetcher(),
        since=since or settings.sync_since,
        download_delay=settings.download_delay_seconds,
        force_retry=force_retry,
        token_expires_at=token_expires_at(settings.puzzmo_token),
    )


def cmd_migrate(args, settings: Settings) -> int:
    with db.connect(settings.database_url) as conn:
        applied = db.migrate(conn)
    print(f"applied: {', '.join(applied)}" if applied else "schema up to date")
    return EXIT_OK


def cmd_sync(args, settings: Settings) -> int:
    try:
        result = run_sync_from_settings(settings, since=args.since, force_retry=args.retry)
    except SyncAlreadyRunning as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    counts = " ".join(f"{k}={v}" for k, v in result.counts.items())
    print(f"sync {result.status} (run {result.run_id}): {counts}")
    for message in result.messages:
        print(f"  {message['level']}: {message['message']}")
    return EXIT_FAILED if result.status == "failed" else EXIT_OK


def _set_completion(args, settings: Settings, completed: bool) -> int:
    with db.connect(settings.database_url) as conn:
        puzzle_id = store.resolve_puzzle(conn, args.ref)
        if puzzle_id is None:
            print(
                f"error: no puzzle matches {args.ref!r} (use an id from `list`, the puzzle's "
                "first-appearance path like 2026-10-05/crossword/big, or a Puzzmo id)",
                file=sys.stderr,
            )
            return EXIT_FAILED
        at = getattr(args, "at", None) or datetime.now(timezone.utc)
        store.set_manual_completion(conn, puzzle_id, completed, at)
        puzzle = store.get_puzzle(conn, puzzle_id)
    state = "complete" if completed else "not complete"
    print(f"marked {puzzle['source_url']} ({puzzle['title']}) as {state}")
    return EXIT_OK


def cmd_mark(args, settings: Settings) -> int:
    return _set_completion(args, settings, True)


def cmd_unmark(args, settings: Settings) -> int:
    return _set_completion(args, settings, False)


def cmd_list(args, settings: Settings) -> int:
    state = "done" if args.done else ("todo" if args.todo else None)
    with db.connect(settings.database_url) as conn:
        rows = store.list_puzzles(conn, series=args.series, state=state, limit=args.limit)
    for row in rows:
        mark = "x" if row["completed"] else " "
        file_flag = "" if row["downloaded"] else "  (no file)"
        print(f"[{mark}] {row['id']:>6}  {row['puzzle_date']}  {row['series']:<5}  {row['title']}{file_flag}")
    return EXIT_OK


def cmd_status(args, settings: Settings) -> int:
    with db.connect(settings.database_url) as conn:
        counts = store.catalogue_counts(conn)
        runs = store.recent_runs(conn)
        problems = store.problem_puzzles(conn)
    print("catalogue:")
    for row in counts:
        print(f"  {row['series']:<5} {row['total']:>5} puzzles, {row['downloaded']:>5} downloaded, "
              f"{row['completed']:>5} completed")
    print("recent runs:")
    for run in runs:
        print(f"  #{run['id']} {run['started_at']:%Y-%m-%d %H:%M} {run['status']} {run['counts']}")
    if problems:
        print("problem puzzles (downloads gave up; `sync --retry` to try again):")
        for p in problems:
            print(f"  {p['source_url']} {p['title']!r}: {p['last_download_error']}")
    if settings.puzzmo_token:
        expires = token_expires_at(settings.puzzmo_token)
        if expires:
            days = (expires - datetime.now(timezone.utc)).days
            print(f"token expires {expires:%Y-%m-%d} ({days} days)")
        else:
            print("token expiry unknown (not a JWT?)")
    else:
        print("PUZZMO_TOKEN not set")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crossword-library")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)

    p_sync = sub.add_parser("sync", help="sync catalogue, completions and downloads")
    p_sync.add_argument("--since", type=_parse_month, help="earliest month, YYYY-MM")
    p_sync.add_argument("--retry", action="store_true", help="retry downloads that gave up")
    p_sync.set_defaults(func=cmd_sync)

    p_mark = sub.add_parser("mark", help="manually mark a puzzle complete")
    p_mark.add_argument("ref")
    p_mark.add_argument("--at", type=_parse_datetime, help="completion time (ISO, default now)")
    p_mark.set_defaults(func=cmd_mark)

    p_unmark = sub.add_parser("unmark", help="manually mark a puzzle not complete")
    p_unmark.add_argument("ref")
    p_unmark.set_defaults(func=cmd_unmark)

    p_list = sub.add_parser("list", help="list puzzles")
    p_list.add_argument("--series", choices=["daily", "mini", "big"])
    state = p_list.add_mutually_exclusive_group()
    state.add_argument("--done", action="store_true")
    state.add_argument("--todo", action="store_true")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_list)

    sub.add_parser("status", help="show catalogue, runs and problems").set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse error/--help
        return int(exc.code or 0)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
        return args.func(args, settings)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
