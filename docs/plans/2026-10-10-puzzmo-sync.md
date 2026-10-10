# Puzzmo Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python service that catalogues Puzzmo crosswords (daily, mini, Big) in PostgreSQL, downloads each as `.puz` plus the original Puzzmo text, and imports the user's Puzzmo completions, with a daily scheduled sync.

**Architecture:** One package `crossword_library` with focused modules: `puzzmo` (authenticated calendar client), `fetch` (anonymous downloads through `xword-dl`), `store` (SQL via psycopg 3), `sync` (orchestration under a Postgres advisory lock), `scheduler` (daily loop) and `cli` (argparse). Migrations are plain numbered SQL files. External dependencies (Puzzmo client, fetcher, clock, sleep) are injected, so the sync logic is tested against a real Postgres without network access.

**Tech Stack:** Python 3.13, uv, psycopg 3, requests, xword-dl 2025.10.14, PostgreSQL 17, pytest, responses, Docker Compose.

**Spec:** `docs/specs/2026-10-10-puzzmo-sync-design.md` (read it before starting; it has the API findings this plan relies on).

## Global Constraints

- Python `==3.13.*` (xword-dl pins `lxml==6.0.0`, which has no 3.14 wheels). Pin `xword-dl==2025.10.14`.
- Version control is **jj** (colocated). Never run raw `git` commands. Commit each task with `jj commit -m "<subject>" -m "<trailers>"` using the trailers shown in each task.
- PostgreSQL 17. Every connection is opened with `autocommit=True`; use `with conn.transaction():` for atomic blocks.
- Puzzle downloads are always **anonymous**. `PUZZMO_TOKEN` is sent only by `puzzmo.PuzzmoClient` and is never logged or printed.
- The Puzzmo calendar API's `month` is **0-indexed**. Code passes 1–12 everywhere and converts only inside `PuzzmoClient.fetch_month`.
- Dedupe key is `(publisher, source_id)` where `source_id` is Puzzmo's `puzzle.id`. A Big puzzle appears on several days but is one row, dated by its earliest `urlPath`.
- Completion rules: imports only insert (never update or delete); a `manual` row is never overwritten by an import; `unmark` writes `completed=false, source='manual'`.
- Defaults: `SYNC_TIME=06:00` (UTC), `DOWNLOAD_DELAY_SECONDS=3`, `SYNC_SINCE=2023-10`, retries 3 with backoff, download retry at most once per 24 h, give up after 5 attempts, token-expiry warning at 30 days.
- Exit codes: `0` ok/partial, `1` failed or user error, `2` configuration error, `3` sync already running.
- Tests never hit the network, except `tests/test_live.py`, which runs only with `CROSSWORD_LIVE=1`.
- Local Postgres: `docker compose up -d --wait postgres` (port `54329` on 127.0.0.1, user/password `crossword`/`crossword`).

## Review Focus

- A Puzzmo completion with `completedAt: null` must be recorded as complete with a NULL timestamp, not crash or be skipped (Task 3 parse test, Task 6 store test).
- Calendar and `.puz` titles differ only by sanitisation (em dash → `--`, emoji → `:name:`). This must not raise a title-mismatch warning, so the comparison uses the raw Puzzmo `name` (Task 5 `FetchedPuzzle.title`, Task 8 test).
- An unexpected exception from xword-dl for one puzzle (e.g. `TypeError` on a malformed response) must count as that puzzle's download failure while the run continues (Task 8 test).
- A run stopped by SIGTERM/`SystemExit` must leave its run row `failed`, and a hard-killed run's `running` row must be cleaned up by the next run (Task 8 tests).
- `mark`/`unmark` with an unknown reference (or a mid-run Big date) must print a clear error and exit 1, not a traceback (Task 9 test).

---

## File Structure

```
.tool-versions, .python-version     Python 3.13 for mise / uv
pyproject.toml, uv.lock             package, deps, console script `crossword-library`
compose.yml                         local postgres:17 (+ optional `app` service, profile "app")
.env.example, .gitignore, README.md
Dockerfile                          app image, default CMD `crossword-library serve`
scripts/capture_puzzle_fixture.py   one-off: save a raw Puzzmo puzzle payload as a test fixture
src/crossword_library/
  __init__.py
  config.py        Settings + load_settings(env)
  db.py            connect(), migrate()
  migrations/0001_initial.sql
  puzzmo.py        CalendarEntry, parse_month, token_expires_at, PuzzmoClient
  fetch.py         FetchedPuzzle, FetchError, fetch_raw, convert, XwordDlFetcher
  store.py         all SQL: catalogue, completions, downloads, runs, lock, queries
  sync.py          run_sync, months_between, SyncResult, SyncAlreadyRunning
  scheduler.py     next_run_at, serve loop
  cli.py           main(argv)
tests/
  conftest.py      test database fixtures
  fixtures/calendar_2026_10.json, fixtures/puzzles/*.json
  test_config.py test_db.py test_puzzmo_parse.py test_puzzmo_client.py
  test_fetch.py test_store_catalogue.py test_store_runs.py test_sync.py
  test_cli.py test_scheduler.py test_live.py
```

---

### Task 1: Project scaffold and settings

**Files:**
- Create: `.tool-versions`, `.python-version`, `pyproject.toml`, `compose.yml`, `.env.example`, `.gitignore`
- Create: `src/crossword_library/__init__.py`, `src/crossword_library/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `crossword_library.config.Settings` (frozen dataclass: `database_url: str`, `puzzmo_token: str | None`, `sync_time: datetime.time`, `download_delay_seconds: float`, `sync_since: datetime.date`), `load_settings(env: Mapping[str, str] | None = None) -> Settings`, `ConfigError(Exception)`.

- [ ] **Step 1: Create tooling files**

`.tool-versions`:
```
python 3.13.12
```

`.python-version`:
```
3.13
```

`.gitignore`:
```
.env
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
dist/
```

`.env.example`:
```
# Copy to .env and fill in. Load into your shell with: set -a; . ./.env; set +a
DATABASE_URL=postgresql://crossword:crossword@localhost:54329/crossword_library
PUZZMO_TOKEN=
SYNC_TIME=06:00
DOWNLOAD_DELAY_SECONDS=3
SYNC_SINCE=2023-10
```

`compose.yml`:
```yaml
name: crossword-library

services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: crossword
      POSTGRES_PASSWORD: crossword
      POSTGRES_DB: crossword_library
    ports:
      - "127.0.0.1:54329:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U crossword -d crossword_library"]
      interval: 2s
      timeout: 3s
      retries: 15

volumes:
  pgdata:
```

`pyproject.toml`:
```toml
[project]
name = "crossword-library"
version = "0.1.0"
description = "Self-hosted crossword catalogue and completion tracker"
requires-python = "==3.13.*"
dependencies = [
    "xword-dl==2025.10.14",
    "psycopg[binary]>=3.2,<4",
    "requests>=2.32,<3",
]

[project.scripts]
crossword-library = "crossword_library.cli:main"

[dependency-groups]
dev = [
    "pytest>=8",
    "responses>=0.25",
]

[build-system]
requires = ["uv_build>=0.8,<0.13"]
build-backend = "uv_build"

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["live: hits the real Puzzmo API (opt-in via CROSSWORD_LIVE=1)"]
```

`src/crossword_library/__init__.py`:
```python
"""Self-hosted crossword catalogue and completion tracker."""
```

- [ ] **Step 2: Install and start Postgres**

Run: `cd ~/Source/crossword-library && mise trust && mise install && uv sync && docker compose up -d --wait postgres`
Expected: `uv.lock` created, `.venv` populated, the postgres container reports healthy.

- [ ] **Step 3: Write the failing test**

`tests/test_config.py`:
```python
from datetime import date, time

import pytest

from crossword_library.config import ConfigError, load_settings

BASE = {"DATABASE_URL": "postgresql://u:p@localhost/db"}


def test_defaults():
    settings = load_settings(BASE)
    assert settings.database_url == "postgresql://u:p@localhost/db"
    assert settings.puzzmo_token is None
    assert settings.sync_time == time(6, 0)
    assert settings.download_delay_seconds == 3.0
    assert settings.sync_since == date(2023, 10, 1)


def test_overrides():
    settings = load_settings(
        BASE
        | {
            "PUZZMO_TOKEN": "tok",
            "SYNC_TIME": "07:30",
            "DOWNLOAD_DELAY_SECONDS": "0.5",
            "SYNC_SINCE": "2025-01",
        }
    )
    assert settings.puzzmo_token == "tok"
    assert settings.sync_time == time(7, 30)
    assert settings.download_delay_seconds == 0.5
    assert settings.sync_since == date(2025, 1, 1)


def test_blank_token_is_none():
    assert load_settings(BASE | {"PUZZMO_TOKEN": ""}).puzzmo_token is None


def test_database_url_required():
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings({})


@pytest.mark.parametrize(
    "key,value",
    [
        ("SYNC_TIME", "6am"),
        ("DOWNLOAD_DELAY_SECONDS", "fast"),
        ("SYNC_SINCE", "2023-10-05"),
        ("SYNC_SINCE", "October"),
    ],
)
def test_invalid_values(key, value):
    with pytest.raises(ConfigError, match=key):
        load_settings(BASE | {key: value})
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crossword_library.config'`

- [ ] **Step 5: Implement**

`src/crossword_library/config.py`:
```python
"""Settings loaded from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time


class ConfigError(Exception):
    """A required setting is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    puzzmo_token: str | None
    sync_time: time
    download_delay_seconds: float
    sync_since: date


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise ConfigError("DATABASE_URL is required")

    raw_time = env.get("SYNC_TIME", "06:00")
    try:
        sync_time = time.fromisoformat(raw_time)
    except ValueError:
        raise ConfigError(f"SYNC_TIME must be HH:MM (UTC), got {raw_time!r}") from None

    raw_delay = env.get("DOWNLOAD_DELAY_SECONDS", "3")
    try:
        delay = float(raw_delay)
    except ValueError:
        raise ConfigError(f"DOWNLOAD_DELAY_SECONDS must be a number, got {raw_delay!r}") from None

    raw_since = env.get("SYNC_SINCE", "2023-10")
    try:
        year, month = raw_since.split("-")
        since = date(int(year), int(month), 1)
    except ValueError:
        raise ConfigError(f"SYNC_SINCE must be YYYY-MM, got {raw_since!r}") from None

    return Settings(
        database_url=database_url,
        puzzmo_token=env.get("PUZZMO_TOKEN") or None,
        sync_time=sync_time,
        download_delay_seconds=delay,
        sync_since=since,
    )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (9 tests)

- [ ] **Step 7: Commit**

```bash
jj commit -m "Scaffold Python project with settings loading" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016CujFMHWVzRsNm4ExEVRMJ"
```

---

### Task 2: Database connection, schema and migrations

**Files:**
- Create: `src/crossword_library/db.py`, `src/crossword_library/migrations/0001_initial.sql`
- Create: `tests/conftest.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `db.connect(url: str) -> psycopg.Connection` (autocommit), `db.migrate(conn) -> list[str]` (names of newly applied files), `db.migration_files() -> list[tuple[str, str]]`.
- Produces test fixtures: `database_url` (session-scoped URL of a freshly created, migrated `crossword_library_test` database) and `conn` (per-test connection with all tables truncated).

- [ ] **Step 1: Write the schema**

`src/crossword_library/migrations/0001_initial.sql`:
```sql
CREATE TABLE puzzles (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publisher text NOT NULL,
    source_id text NOT NULL,
    series text NOT NULL CHECK (series IN ('daily', 'mini', 'big')),
    puzzle_date date NOT NULL,
    source_url text NOT NULL,
    title text NOT NULL DEFAULT '',
    author text,
    download_attempts integer NOT NULL DEFAULT 0,
    last_download_attempt_at timestamptz,
    last_download_error text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (publisher, source_id)
);

CREATE INDEX puzzles_puzzle_date_idx ON puzzles (puzzle_date DESC);
CREATE INDEX puzzles_source_url_idx ON puzzles (source_url);

CREATE TABLE puzzle_files (
    puzzle_id bigint NOT NULL REFERENCES puzzles (id) ON DELETE CASCADE,
    format text NOT NULL CHECK (format IN ('xd', 'puz')),
    content bytea NOT NULL,
    sha256 text NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (puzzle_id, format)
);

CREATE TABLE completions (
    puzzle_id bigint PRIMARY KEY REFERENCES puzzles (id) ON DELETE CASCADE,
    completed boolean NOT NULL,
    completed_at timestamptz,
    source text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sync_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    errors jsonb NOT NULL DEFAULT '[]'::jsonb,
    token_expires_at timestamptz
);
```

- [ ] **Step 2: Write test fixtures**

`tests/conftest.py`:
```python
import os

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from crossword_library import db

ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://crossword:crossword@localhost:54329/postgres"
)
TEST_DB = "crossword_library_test"


@pytest.fixture(scope="session")
def database_url():
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.exit(
            f"Postgres not reachable at {ADMIN_URL}; run `docker compose up -d --wait postgres` ({exc})",
            returncode=2,
        )
    with admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{TEST_DB}"')
    url = make_conninfo(ADMIN_URL, dbname=TEST_DB)
    with db.connect(url) as conn:
        db.migrate(conn)
    return url


@pytest.fixture
def conn(database_url):
    with db.connect(database_url) as connection:
        connection.execute(
            "TRUNCATE puzzles, puzzle_files, completions, sync_runs RESTART IDENTITY CASCADE"
        )
        yield connection
```

- [ ] **Step 3: Write the failing test**

`tests/test_db.py`:
```python
from crossword_library import db


def test_migration_files_are_packaged_and_ordered():
    names = [name for name, _ in db.migration_files()]
    assert names[0] == "0001_initial.sql"
    assert names == sorted(names)


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == []
    applied = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert applied == ["0001_initial.sql"]


def test_tables_exist(conn):
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    assert {"puzzles", "puzzle_files", "completions", "sync_runs", "schema_migrations"} <= tables


def test_connections_are_autocommit(database_url):
    with db.connect(database_url) as conn:
        assert conn.autocommit is True
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL with `ImportError: cannot import name 'db'`

- [ ] **Step 5: Implement**

`src/crossword_library/db.py`:
```python
"""Postgres connections and the plain-SQL migration runner."""

from __future__ import annotations

from importlib import resources

import psycopg


def connect(url: str) -> psycopg.Connection:
    """Open an autocommit connection; use `with conn.transaction():` for atomic work."""
    return psycopg.connect(url, autocommit=True)


def migration_files() -> list[tuple[str, str]]:
    root = resources.files("crossword_library") / "migrations"
    return sorted(
        (entry.name, entry.read_text(encoding="utf-8"))
        for entry in root.iterdir()
        if entry.name.endswith(".sql")
    )


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply any migration files not yet recorded. Returns the names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version text PRIMARY KEY,"
        " applied_at timestamptz NOT NULL DEFAULT now())"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    newly_applied = []
    for name, sql in migration_files():
        if name in applied:
            continue
        with conn.transaction():
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (name,))
        newly_applied.append(name)
    return newly_applied
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
jj commit -m "Add Postgres schema and migration runner" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016CujFMHWVzRsNm4ExEVRMJ"
```

---

### Task 3: Puzzmo calendar parsing and token expiry

**Files:**
- Create: `src/crossword_library/puzzmo.py`
- Create: `tests/fixtures/calendar_2026_10.json`
- Test: `tests/test_puzzmo_parse.py`

**Interfaces:**
- Produces: `CalendarEntry` (frozen dataclass: `source_id: str`, `series: str` (`daily`/`mini`/`big`), `puzzle_date: date`, `url_path: str`, `title: str`, `completed: bool`, `completed_at: datetime | None`), `series_for_url_path(url_path: str) -> str | None`, `parse_month(payload: dict) -> list[CalendarEntry]`, `token_expires_at(token: str) -> datetime | None`, exceptions `PuzzmoError`, `PuzzmoAuthError(PuzzmoError)`, `PuzzmoAPIError(PuzzmoError)`, constant `CALENDAR_QUERY`.

- [ ] **Step 1: Create the calendar fixture**

The fixture is hand-written in the shape of real responses (see the spec §2), with no account identifiers. `tests/fixtures/calendar_2026_10.json`:
```json
{
  "data": {
    "dailiesForAMonth": [
      {
        "day": "2026-10-05T05:00:00.000Z",
        "puzzles": [
          {"urlPath": "2026-10-05/crossword", "puzzle": {"id": "pz-daily-1005:puzzle", "name": "High Grades", "game": {"slug": "crossword"}, "communityPuzzle": null, "currentAccountGamePlayed": {"completed": true, "completedAt": "2026-10-05T09:15:00.000Z"}}},
          {"urlPath": "2026-10-05/crossword/mini", "puzzle": {"id": "pz-mini-1005:puzzle", "name": "Media Diet", "game": {"slug": "crossword"}, "communityPuzzle": null, "currentAccountGamePlayed": {"completed": true, "completedAt": null}}},
          {"urlPath": "2026-10-05/crossword/big", "puzzle": {"id": "pz-big-kicks:puzzle", "name": "Kicks In", "game": {"slug": "crossword"}, "communityPuzzle": null, "currentAccountGamePlayed": {"completed": false, "completedAt": null}}},
          {"urlPath": "2026-10-05/crossword", "puzzle": {"id": "pz-community:puzzle", "name": "Fan Made", "game": {"slug": "crossword"}, "communityPuzzle": {"id": "cp-1"}, "currentAccountGamePlayed": null}},
          {"urlPath": "2026-10-05/typeshift", "puzzle": {"id": "pz-typeshift:puzzle", "name": null, "game": {"slug": "typeshift"}, "communityPuzzle": null, "currentAccountGamePlayed": null}},
          {"urlPath": "2026-10-05/spelltower/strange/-1", "puzzle": {"id": "pz-spelltower:puzzle", "name": null, "game": {"slug": "spelltower"}, "communityPuzzle": null, "currentAccountGamePlayed": null}}
        ]
      },
      {
        "day": "2026-10-06T05:00:00.000Z",
        "puzzles": [
          {"urlPath": "2026-10-06/crossword", "puzzle": {"id": "pz-daily-1006:puzzle", "name": "Stretching the Joke", "game": {"slug": "crossword"}, "communityPuzzle": null, "currentAccountGamePlayed": null}},
          {"urlPath": "2026-10-06/crossword/big", "puzzle": {"id": "pz-big-kicks:puzzle", "name": "Kicks In", "game": {"slug": "crossword"}, "communityPuzzle": null, "currentAccountGamePlayed": {"completed": false, "completedAt": null}}}
        ]
      }
    ]
  }
}
```

- [ ] **Step 2: Write the failing test**

`tests/test_puzzmo_parse.py`:
```python
import base64
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from crossword_library.puzzmo import (
    CalendarEntry,
    PuzzmoAPIError,
    PuzzmoAuthError,
    parse_month,
    series_for_url_path,
    token_expires_at,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize(
    "url_path,series",
    [
        ("2026-10-05/crossword", "daily"),
        ("2026-10-05/crossword/mini", "mini"),
        ("2026-10-05/crossword/big", "big"),
        ("2026-10-05/crossword/mini/2", None),
        ("2026-10-05/typeshift", None),
        ("2026-10-05", None),
    ],
)
def test_series_for_url_path(url_path, series):
    assert series_for_url_path(url_path) == series


def test_parse_month_keeps_only_publisher_crosswords():
    entries = parse_month(load("calendar_2026_10.json"))
    assert [(e.url_path, e.source_id) for e in entries] == [
        ("2026-10-05/crossword", "pz-daily-1005:puzzle"),
        ("2026-10-05/crossword/mini", "pz-mini-1005:puzzle"),
        ("2026-10-05/crossword/big", "pz-big-kicks:puzzle"),
        ("2026-10-06/crossword", "pz-daily-1006:puzzle"),
        ("2026-10-06/crossword/big", "pz-big-kicks:puzzle"),
    ]


def test_parse_month_entry_fields():
    daily, mini, big, unplayed, _ = parse_month(load("calendar_2026_10.json"))
    assert daily == CalendarEntry(
        source_id="pz-daily-1005:puzzle",
        series="daily",
        puzzle_date=date(2026, 10, 5),
        url_path="2026-10-05/crossword",
        title="High Grades",
        completed=True,
        completed_at=datetime(2026, 10, 5, 9, 15, tzinfo=timezone.utc),
    )
    # Completed but Puzzmo gave no timestamp: still completed, timestamp None.
    assert mini.completed is True and mini.completed_at is None
    assert big.series == "big" and big.completed is False
    assert unplayed.completed is False and unplayed.completed_at is None


def test_parse_month_empty_month():
    assert parse_month({"data": {"dailiesForAMonth": []}}) == []


def test_parse_month_auth_error():
    payload = {"errors": [{"message": "Must be logged in"}], "data": None}
    with pytest.raises(PuzzmoAuthError, match="PUZZMO_TOKEN"):
        parse_month(payload)


def test_parse_month_other_graphql_error():
    payload = {"errors": [{"message": 'Cannot query field "foo"'}], "data": None}
    with pytest.raises(PuzzmoAPIError, match="Cannot query field"):
        parse_month(payload)


def test_parse_month_missing_data():
    with pytest.raises(PuzzmoAPIError, match="dailiesForAMonth"):
        parse_month({"data": {}})


def _jwt(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.signature"


def test_token_expires_at():
    assert token_expires_at(_jwt({"exp": 1808277780})) == datetime(
        2027, 4, 21, 3, 23, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("token", ["not-a-jwt", _jwt({"sub": "x"}), "a.!!!.c", ""])
def test_token_expires_at_unparseable(token):
    assert token_expires_at(token) is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_puzzmo_parse.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crossword_library.puzzmo'`

- [ ] **Step 4: Implement**

`src/crossword_library/puzzmo.py`:
```python
"""Puzzmo calendar client: puzzle catalogue and the user's completions.

This is the only module that uses PUZZMO_TOKEN. Puzzle downloads live in
`fetch` and are always anonymous.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

CROSSWORD_GAME_SLUG = "crossword"

CALENDAR_QUERY = """query CalendarQuery($month: Int!, $year: Int!) {
  dailiesForAMonth(month: $month, year: $year) {
    day
    puzzles(cacheKey: "calendar") {
      urlPath
      puzzle {
        id
        name
        game { slug }
        communityPuzzle { id }
        currentAccountGamePlayed { completed completedAt }
      }
    }
  }
}"""


class PuzzmoError(Exception):
    """Base class for Puzzmo API failures."""


class PuzzmoAuthError(PuzzmoError):
    """The token was rejected or has expired."""


class PuzzmoAPIError(PuzzmoError):
    """Puzzmo returned an error, an unexpected response, or kept failing."""


@dataclass(frozen=True)
class CalendarEntry:
    source_id: str
    series: str
    puzzle_date: date
    url_path: str
    title: str
    completed: bool
    completed_at: datetime | None


def series_for_url_path(url_path: str) -> str | None:
    """Map a calendar urlPath to daily/mini/big, or None if it isn't one."""
    parts = url_path.split("/")
    if len(parts) < 2 or parts[1] != CROSSWORD_GAME_SLUG:
        return None
    rest = parts[2:]
    if rest == []:
        return "daily"
    if rest == ["mini"]:
        return "mini"
    if rest == ["big"]:
        return "big"
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def parse_month(payload: dict) -> list[CalendarEntry]:
    """Extract publisher crosswords from a dailiesForAMonth response."""
    errors = payload.get("errors")
    if errors:
        messages = "; ".join(str(e.get("message", "?")) for e in errors)
        if any("Must be logged in" in str(e.get("message", "")) for e in errors):
            raise PuzzmoAuthError(f"Puzzmo rejected the token ({messages}); replace PUZZMO_TOKEN")
        raise PuzzmoAPIError(f"Puzzmo GraphQL error: {messages}")

    days = (payload.get("data") or {}).get("dailiesForAMonth")
    if days is None:
        raise PuzzmoAPIError("Puzzmo response had no dailiesForAMonth data")

    entries = []
    for day in days:
        for item in day.get("puzzles") or []:
            url_path = item.get("urlPath") or ""
            puzzle = item.get("puzzle") or {}
            if (puzzle.get("game") or {}).get("slug") != CROSSWORD_GAME_SLUG:
                continue
            if puzzle.get("communityPuzzle"):
                continue
            series = series_for_url_path(url_path)
            if series is None:
                continue
            played = puzzle.get("currentAccountGamePlayed") or {}
            entries.append(
                CalendarEntry(
                    source_id=puzzle["id"],
                    series=series,
                    puzzle_date=date.fromisoformat(url_path[:10]),
                    url_path=url_path,
                    title=puzzle.get("name") or "",
                    completed=bool(played.get("completed")),
                    completed_at=_parse_datetime(played.get("completedAt")),
                )
            )
    return entries


def token_expires_at(token: str) -> datetime | None:
    """Read the JWT `exp` claim without verifying the signature."""
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body))
        return datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc)
    except (IndexError, ValueError, KeyError, TypeError):
        return None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_puzzmo_parse.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
jj commit -m "Parse Puzzmo calendar responses and token expiry" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016CujFMHWVzRsNm4ExEVRMJ"
```

---

### Task 4: Puzzmo HTTP client with retries

**Files:**
- Modify: `src/crossword_library/puzzmo.py` (append the client)
- Test: `tests/test_puzzmo_client.py`

**Interfaces:**
- Consumes: `parse_month`, `CALENDAR_QUERY`, `PuzzmoAuthError`, `PuzzmoAPIError` from Task 3.
- Produces: `PuzzmoClient(token: str, session: requests.Session | None = None, retries: int = 3, backoff_seconds: float = 1.0, sleep: Callable[[float], None] = time.sleep, timeout: float = 30)` with `fetch_month(year: int, month: int) -> list[CalendarEntry]` (`month` is 1–12), constant `API_URL`.

- [ ] **Step 1: Write the failing test**

`tests/test_puzzmo_client.py`:
```python
import json
from pathlib import Path

import pytest
import requests
import responses

from crossword_library.puzzmo import API_URL, PuzzmoAPIError, PuzzmoAuthError, PuzzmoClient

CALENDAR = json.loads((Path(__file__).parent / "fixtures" / "calendar_2026_10.json").read_text())


def make_client(**kwargs):
    sleeps = []
    client = PuzzmoClient("secret-token", sleep=sleeps.append, **kwargs)
    return client, sleeps


@responses.activate
def test_fetch_month_sends_zero_based_month_and_auth():
    responses.post(API_URL, json=CALENDAR)
    client, _ = make_client()
    entries = client.fetch_month(2026, 10)
    assert len(entries) == 5
    request = responses.calls[0].request
    body = json.loads(request.body)
    assert body["variables"] == {"year": 2026, "month": 9}
    assert body["operationName"] == "CalendarQuery"
    assert request.headers["authorization"] == "Bearer secret-token"
    assert request.headers["auth-provider"] == "custom"


@responses.activate
def test_http_401_is_auth_error_without_retry():
    responses.post(API_URL, status=401, json={})
    client, sleeps = make_client()
    with pytest.raises(PuzzmoAuthError, match="PUZZMO_TOKEN"):
        client.fetch_month(2026, 10)
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_graphql_login_error_is_auth_error():
    responses.post(API_URL, json={"errors": [{"message": "Must be logged in"}], "data": None})
    client, _ = make_client()
    with pytest.raises(PuzzmoAuthError):
        client.fetch_month(2026, 10)


@responses.activate
def test_server_errors_retry_with_backoff_then_succeed():
    responses.post(API_URL, status=502)
    responses.post(API_URL, status=503)
    responses.post(API_URL, json=CALENDAR)
    client, sleeps = make_client(backoff_seconds=1.0)
    assert len(client.fetch_month(2026, 10)) == 5
    assert sleeps == [1.0, 2.0]


@responses.activate
def test_gives_up_after_retries():
    for _ in range(4):
        responses.post(API_URL, status=500)
    client, sleeps = make_client(retries=3)
    with pytest.raises(PuzzmoAPIError, match="2026-10 failed after 4 attempts: HTTP 500"):
        client.fetch_month(2026, 10)
    assert len(responses.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


@responses.activate
def test_network_errors_are_retried():
    responses.post(API_URL, body=requests.ConnectionError("boom"))
    responses.post(API_URL, json=CALENDAR)
    client, sleeps = make_client()
    assert len(client.fetch_month(2026, 10)) == 5
    assert sleeps == [1.0]


@responses.activate
def test_unexpected_status_is_api_error():
    responses.post(API_URL, status=400, body="bad request")
    client, _ = make_client()
    with pytest.raises(PuzzmoAPIError, match="HTTP 400"):
        client.fetch_month(2026, 10)


@responses.activate
def test_non_json_is_api_error():
    responses.post(API_URL, body="<html>oops</html>")
    client, _ = make_client()
    with pytest.raises(PuzzmoAPIError, match="non-JSON"):
        client.fetch_month(2026, 10)


@responses.activate
def test_token_not_in_error_messages():
    for _ in range(4):
        responses.post(API_URL, status=500)
    client, _ = make_client()
    with pytest.raises(PuzzmoAPIError) as excinfo:
        client.fetch_month(2026, 10)
    assert "secret-token" not in str(excinfo.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_puzzmo_client.py -v`
Expected: FAIL with `ImportError: cannot import name 'API_URL'`

- [ ] **Step 3: Implement**

Add these imports at the top of `src/crossword_library/puzzmo.py` (next to the existing ones):
```python
import time
from collections.abc import Callable

import requests
```

Append to `src/crossword_library/puzzmo.py`:
```python
API_URL = "https://api.puzzmo.com/graphql"
USER_AGENT = "crossword-library/0.1"


class PuzzmoClient:
    """Fetches one calendar month at a time with the user's token."""

    def __init__(
        self,
        token: str,
        session: requests.Session | None = None,
        retries: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30,
    ):
        self._token = token
        self._session = session or requests.Session()
        self._retries = retries
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._timeout = timeout

    def fetch_month(self, year: int, month: int) -> list[CalendarEntry]:
        """Fetch crosswords for a month. `month` is 1-12; Puzzmo's API is 0-indexed."""
        body = {
            "operationName": "CalendarQuery",
            "query": CALENDAR_QUERY,
            "variables": {"year": year, "month": month - 1},
        }
        headers = {
            "authorization": f"Bearer {self._token}",
            "auth-provider": "custom",
            "User-Agent": USER_AGENT,
        }
        last_error = "no attempts made"
        for attempt in range(self._retries + 1):
            try:
                response = self._session.post(
                    API_URL, json=body, headers=headers, timeout=self._timeout
                )
            except requests.RequestException as exc:
                last_error = f"network error: {type(exc).__name__}"
            else:
                status = response.status_code
                if status in (401, 403):
                    raise PuzzmoAuthError(
                        f"Puzzmo rejected the token (HTTP {status}); replace PUZZMO_TOKEN"
                    )
                if status >= 500 or status == 429:
                    last_error = f"HTTP {status}"
                elif status != 200:
                    raise PuzzmoAPIError(
                        f"Unexpected HTTP {status} from Puzzmo: {response.text[:200]}"
                    )
                else:
                    try:
                        payload = response.json()
                    except ValueError:
                        raise PuzzmoAPIError("Puzzmo returned a non-JSON response") from None
                    return parse_month(payload)
            if attempt < self._retries:
                self._sleep(self._backoff_seconds * 2**attempt)
        raise PuzzmoAPIError(
            f"Calendar {year}-{month:02d} failed after {self._retries + 1} attempts: {last_error}"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_puzzmo_client.py tests/test_puzzmo_parse.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
jj commit -m "Add Puzzmo calendar HTTP client with retries" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016CujFMHWVzRsNm4ExEVRMJ"
```

---

### Task 5: Anonymous puzzle fetching via xword-dl

**Files:**
- Create: `src/crossword_library/fetch.py`, `scripts/capture_puzzle_fixture.py`
- Create: `tests/fixtures/puzzles/daily-2026-10-08.json`, `mini-2026-10-08.json`, `big-2026-10-05.json` (captured in Step 3)
- Test: `tests/test_fetch.py`

**Interfaces:**
- Produces: `FetchedPuzzle` (frozen dataclass: `title: str` (raw, unsanitised Puzzmo `name`), `author: str`, `xd: str`, `puz: bytes`), `FetchError(Exception)`, `fetch_raw(url_path: str) -> dict`, `convert(raw: dict) -> FetchedPuzzle`, `XwordDlFetcher` with `fetch(url_path: str) -> FetchedPuzzle`. Every failure inside `fetch_raw`/`convert` is raised as `FetchError`.

- [ ] **Step 1: Implement `fetch_raw` first (needed to capture fixtures)**

`src/crossword_library/fetch.py`:
```python
"""Anonymous puzzle downloads through xword-dl.

Never pass PUZZMO_TOKEN here: Puzzmo's startOrFindGameplay creates a gameplay
record, so an authenticated fetch would mark puzzles as started on the
user's account. xword-dl sends a random Puzzmo-Gameplay-Id instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests
from xword_dl.downloader.puzzmodownloader import PuzzmoDownloader
from xword_dl.util import XWordDLException, sanitize_for_puzfile


class FetchError(Exception):
    """A puzzle could not be downloaded or converted."""


@dataclass(frozen=True)
class FetchedPuzzle:
    title: str  # raw Puzzmo name, before .puz sanitisation; compare this with the calendar
    author: str
    xd: str
    puz: bytes


class _UrlPathDownloader(PuzzmoDownloader):
    """PuzzmoDownloader for any calendar urlPath: daily, crossword/mini or crossword/big."""

    def __init__(self, url_path: str):
        super().__init__()
        self.finder_key = "today:/" + url_path
        self.date_string = url_path[:10]


def fetch_raw(url_path: str) -> dict:
    """Return Puzzmo's puzzle payload (name, authors, xd `puzzle` text, ...)."""
    downloader = _UrlPathDownloader(url_path)
    try:
        return downloader.fetch_data(f"https://www.puzzmo.com/puzzle/{url_path}")
    except XWordDLException as exc:
        raise FetchError(str(exc)) from exc
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc
```

- [ ] **Step 2: Write the fixture capture script**

`scripts/capture_puzzle_fixture.py`:
```python
"""Save a raw Puzzmo puzzle payload as a test fixture (anonymous request).

Usage: uv run python scripts/capture_puzzle_fixture.py 2026-10-08/crossword/mini out.json
"""

import json
import sys

from crossword_library.fetch import fetch_raw

url_path, out_path = sys.argv[1], sys.argv[2]
raw = fetch_raw(url_path)
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(raw, fh, indent=1, ensure_ascii=False)
print(f"saved {url_path}: {raw.get('name')!r}")
```

- [ ] **Step 3: Capture fixtures (live, anonymous)**

Run:
```bash
mkdir -p tests/fixtures/puzzles
uv run python scripts/capture_puzzle_fixture.py 2026-10-08/crossword tests/fixtures/puzzles/daily-2026-10-08.json
uv run python scripts/capture_puzzle_fixture.py 2026-10-08/crossword/mini tests/fixtures/puzzles/mini-2026-10-08.json
uv run python scripts/capture_puzzle_fixture.py 2026-10-05/crossword/big tests/fixtures/puzzles/big-2026-10-05.json
```
Expected output:
```
saved 2026-10-08/crossword: 'a pisces and a gemini'
saved 2026-10-08/crossword/mini: 'Reader Beware'
saved 2026-10-05/crossword/big: 'Kicks In'
```

- [ ] **Step 4: Write the failing test**

`tests/test_fetch.py`:
```python
import io
import json
from pathlib import Path

import pytest
import puz

from crossword_library import fetch
from crossword_library.fetch import FetchError, FetchedPuzzle, XwordDlFetcher, convert

PUZZLES = Path(__file__).parent / "fixtures" / "puzzles"


def raw(name):
    return json.loads((PUZZLES / name).read_text(encoding="utf-8"))


def load_puz(data: bytes) -> puz.Puzzle:
    return puz.load(data)


def test_convert_daily():
    fetched = convert(raw("daily-2026-10-08.json"))
    assert fetched.title == "a pisces and a gemini"
    assert fetched.author == "Dan Schwartz"
    assert "## Grid" in fetched.xd
    parsed = load_puz(fetched.puz)
    assert (parsed.width, parsed.height) == (10, 10)
    assert parsed.title == "a pisces and a gemini"


def test_convert_mini_with_emoji_is_latin1_safe():
    payload = raw("mini-2026-10-08.json")
    assert any(ord(ch) > 255 for ch in payload["puzzle"])  # raw text has emoji
    fetched = convert(payload)
    parsed = load_puz(fetched.puz)
    assert (parsed.width, parsed.height) == (5, 5)
    assert parsed.title == "Reader Beware"
    for clue in parsed.clues:
        assert all(ord(ch) < 256 for ch in clue)


def test_convert_big_keeps_circles():
    fetched = convert(raw("big-2026-10-05.json"))
    parsed = load_puz(fetched.puz)
    assert (parsed.width, parsed.height) == (15, 15)
    assert b"GEXT" in parsed.extensions


def test_title_is_raw_puzzmo_name():
    payload = raw("daily-2026-10-08.json") | {"name": "Lay the table with the fancy sh—"}
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
        return raw("mini-2026-10-08.json")

    monkeypatch.setattr(fetch, "fetch_raw", fake_fetch_raw)
    fetched = XwordDlFetcher().fetch("2026-10-08/crossword/mini")
    assert calls == ["2026-10-08/crossword/mini"]
    assert isinstance(fetched, FetchedPuzzle)


def test_downloader_finder_key():
    downloader = fetch._UrlPathDownloader("2026-10-05/crossword/big")
    assert downloader.finder_key == "today:/2026-10-05/crossword/big"
    assert "authorization" not in {k.lower() for k in downloader.session.headers}
```

- [ ] **Step 5: Run test to verify it fails**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: FAIL with `ImportError: cannot import name 'XwordDlFetcher'` (and `convert`)

- [ ] **Step 6: Implement `convert` and `XwordDlFetcher`**

Append to `src/crossword_library/fetch.py`:
```python
def convert(raw: dict) -> FetchedPuzzle:
    """Convert a Puzzmo payload to a Latin-1-safe .puz plus the original xd text."""
    try:
        puzzle = sanitize_for_puzfile(PuzzmoDownloader().parse_xword(raw))
        puz_bytes = puzzle.tobytes()
        xd = raw["puzzle"]
        if not isinstance(xd, str):
            raise TypeError("puzzle text missing")
    except Exception as exc:  # xword-dl/puzpy raise many types; normalise them
        raise FetchError(f"could not convert to .puz: {type(exc).__name__}: {exc}") from exc
    return FetchedPuzzle(
        title=raw.get("name") or "",
        author=puzzle.author or "",
        xd=xd,
        puz=puz_bytes,
    )


class XwordDlFetcher:
    """Production fetcher: anonymous download, then conversion."""

    def fetch(self, url_path: str) -> FetchedPuzzle:
        return convert(fetch_raw(url_path))
```

Note: `XwordDlFetcher.fetch` must look up `fetch_raw` through the module (as written) so the monkeypatch test works.

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: PASS. If `test_convert_daily`'s author differs, check the fixture's `authors[].publishingName` and use that value; the title and grid size assertions must not change.

- [ ] **Step 8: Commit**

```bash
jj commit -m "Fetch Puzzmo puzzles anonymously via xword-dl" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016CujFMHWVzRsNm4ExEVRMJ"
```

---

### Task 6: Store — catalogue and completions

**Files:**
- Create: `src/crossword_library/store.py`
- Test: `tests/test_store_catalogue.py`

**Interfaces:**
- Consumes: `CalendarEntry` (Task 3), `conn` fixture (Task 2).
- Produces:
  - `upsert_puzzle(conn, entry: CalendarEntry) -> tuple[int, bool]` (puzzle id, inserted?)
  - `record_puzzmo_completion(conn, puzzle_id: int, completed_at: datetime | None) -> bool` (inserted?)
  - `set_manual_completion(conn, puzzle_id: int, completed: bool, at: datetime) -> None`
  - `get_completion(conn, puzzle_id: int) -> dict | None` (keys `completed`, `completed_at`, `source`)
  - `resolve_puzzle(conn, ref: str) -> int | None`
  - `get_puzzle(conn, puzzle_id: int) -> dict` (all `puzzles` columns)

- [ ] **Step 1: Write the failing test**

`tests/test_store_catalogue.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_catalogue.py -v`
Expected: FAIL with `ImportError: cannot import name 'store'`

- [ ] **Step 3: Implement**

`src/crossword_library/store.py`:
```python
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
    if ref.isdigit():
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_store_catalogue.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
jj commit -m "Store catalogue and completions in Postgres" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016CujFMHWVzRsNm4ExEVRMJ"
```

---

### Task 7: Store — downloads, run log, lock and queries

**Files:**
- Modify: `src/crossword_library/store.py` (append)
- Test: `tests/test_store_runs.py`

**Interfaces:**
- Consumes: Task 6 functions.
- Produces:
  - constants `LOCK_KEY = 0x63726F73`, `MAX_DOWNLOAD_ATTEMPTS = 5`, `RETRY_AFTER = timedelta(hours=24)`
  - `PendingDownload` (frozen dataclass: `puzzle_id: int`, `url_path: str`, `title: str`)
  - `puzzles_needing_download(conn, now: datetime, force_retry: bool = False) -> list[PendingDownload]` (newest first)
  - `save_download(conn, puzzle_id: int, *, xd: str, puz: bytes, author: str, now: datetime) -> None`
  - `record_download_failure(conn, puzzle_id: int, error: str, now: datetime) -> None`
  - `get_file(conn, puzzle_id: int, fmt: str) -> bytes | None`
  - `try_advisory_lock(conn) -> bool`
  - `abandon_running_runs(conn, now: datetime) -> int`
  - `start_run(conn, now: datetime, token_expires_at: datetime | None) -> int`
  - `finish_run(conn, run_id: int, now: datetime, status: str, counts: dict, messages: list[dict]) -> None`
  - `recent_runs(conn, limit: int = 5) -> list[dict]`
  - `problem_puzzles(conn) -> list[dict]` (keys `id`, `source_url`, `title`, `download_attempts`, `last_download_error`)
  - `catalogue_counts(conn) -> list[dict]` (keys `series`, `total`, `downloaded`, `completed`)
  - `list_puzzles(conn, *, series: str | None = None, state: str | None = None, limit: int = 50) -> list[dict]` (`state` is `"done"`, `"todo"` or None; keys `id`, `puzzle_date`, `series`, `title`, `source_url`, `completed`, `downloaded`)

- [ ] **Step 1: Write the failing test**

`tests/test_store_runs.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_runs.py -v`
Expected: FAIL with `AttributeError: module 'crossword_library.store' has no attribute 'save_download'`

- [ ] **Step 3: Implement**

Add these imports to the top of `src/crossword_library/store.py`:
```python
import hashlib
from dataclasses import dataclass
from datetime import timedelta

from psycopg.types.json import Jsonb
```

Append to `src/crossword_library/store.py`:
```python
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
```

Note: in `test_catalogue_counts_and_list`, d1 and m1 share a date; the ordering `puzzle_date DESC, series, id` gives `d2, d1, m1`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_store_runs.py tests/test_store_catalogue.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
jj commit -m "Store downloads, run log, advisory lock and queries" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016CujFMHWVzRsNm4ExEVRMJ"
```

---

### Task 8: Sync orchestration

**Files:**
- Create: `src/crossword_library/sync.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `db.connect` (Task 2), `PuzzmoAuthError`/`PuzzmoError` and `CalendarEntry` (Task 3), `FetchError`/`FetchedPuzzle` (Task 5), store functions (Tasks 6–7).
- Produces:
  - `months_between(start: date, end: date) -> list[tuple[int, int]]` (inclusive, months 1–12)
  - `SyncAlreadyRunning(Exception)`
  - `SyncResult` (dataclass: `run_id: int`, `status: str`, `counts: dict`, `messages: list[dict]`)
  - `run_sync(database_url: str, client, fetcher, *, since: date, now: Callable[[], datetime] = <utc now>, sleep: Callable[[float], None] = time.sleep, download_delay: float = 3.0, force_retry: bool = False, token_expires_at: datetime | None = None) -> SyncResult`
  - `client` needs `fetch_month(year, month) -> list[CalendarEntry]`; `fetcher` needs `fetch(url_path) -> FetchedPuzzle`.
  - Each message is `{"level": "error" | "warning", "message": str}`.

- [ ] **Step 1: Write the failing test**

`tests/test_sync.py`:
```python
import threading
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crossword_library.sync'`

- [ ] **Step 3: Implement**

`src/crossword_library/sync.py`:
```python
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
    try:
        if not store.try_advisory_lock(lock_conn):
            raise SyncAlreadyRunning("another sync is already running")
        with db.connect(database_url) as conn:
            return _locked_sync(
                conn, client, fetcher, since, now, sleep, download_delay, force_retry,
                token_expires_at,
            )
    finally:
        lock_conn.close()  # releases the session advisory lock


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_sync.py -v`
Expected: PASS. In `test_full_sync`, downloads run newest first (`2026-10-05` before `2026-09-30`) and, within a date, ordered by id (insertion order: daily d5, mini m5, then big). If the fetch order differs, check `puzzles_needing_download`'s `ORDER BY` rather than editing the test.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
jj commit -m "Orchestrate sync runs under an advisory lock" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016CujFMHWVzRsNm4ExEVRMJ"
```

---

### Task 9: Command-line interface

**Files:**
- Create: `src/crossword_library/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `load_settings`/`ConfigError` (Task 1), `db.connect`/`db.migrate` (Task 2), `PuzzmoClient`/`token_expires_at` (Tasks 3–4), `XwordDlFetcher` (Task 5), store functions (Tasks 6–7), `run_sync`/`SyncAlreadyRunning` (Task 8).
- Produces: `main(argv: list[str] | None = None) -> int`; module-level factories `make_client(token: str)` and `make_fetcher()` (monkeypatched in tests); `run_sync_from_settings(settings, *, since=None, force_retry=False) -> SyncResult` (used by `serve` in Task 10).
- Subcommands: `migrate`, `sync [--since YYYY-MM] [--retry]`, `mark REF [--at ISO_DATETIME]`, `unmark REF`, `list [--series daily|mini|big] [--done|--todo] [--limit N]`, `status`. (`serve` is added in Task 10.)

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ImportError: cannot import name 'cli'`

- [ ] **Step 3: Implement**

`src/crossword_library/cli.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Smoke-test against the dev database**

Run:
```bash
cp -n .env.example .env
set -a; . ./.env; set +a
uv run crossword-library migrate && uv run crossword-library status
```
Expected: `applied: 0001_initial.sql` (or `schema up to date`), then a status report with an empty catalogue and `PUZZMO_TOKEN not set`.

- [ ] **Step 6: Commit**

```bash
jj commit -m "Add command-line interface" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016CujFMHWVzRsNm4ExEVRMJ"
```

---

### Task 10: Daily scheduler, Docker image and live check

**Files:**
- Create: `src/crossword_library/scheduler.py`, `Dockerfile`, `README.md`, `tests/test_scheduler.py`, `tests/test_live.py`
- Modify: `src/crossword_library/cli.py` (add `serve`), `compose.yml` (add `app` service)

**Interfaces:**
- Consumes: `run_sync_from_settings` (Task 9), `SyncAlreadyRunning` (Task 8), `db.migrate` (Task 2), `PuzzmoClient` (Task 4), `XwordDlFetcher` (Task 5).
- Produces: `scheduler.next_run_at(now: datetime, at: time) -> datetime`, `scheduler.serve(sync_time: time, run_once: Callable[[], object], *, now=..., sleep=..., should_stop: Callable[[], bool] = lambda: False) -> None`, CLI `serve`.

- [ ] **Step 1: Write the failing test**

`tests/test_scheduler.py`:
```python
from datetime import datetime, time, timedelta, timezone

from crossword_library.scheduler import next_run_at, serve
from crossword_library.sync import SyncAlreadyRunning

UTC = timezone.utc


def test_next_run_later_today():
    assert next_run_at(datetime(2026, 10, 10, 5, 0, tzinfo=UTC), time(6, 0)) == datetime(
        2026, 10, 10, 6, 0, tzinfo=UTC)


def test_next_run_tomorrow_when_passed_or_exact():
    assert next_run_at(datetime(2026, 10, 10, 6, 0, tzinfo=UTC), time(6, 0)) == datetime(
        2026, 10, 11, 6, 0, tzinfo=UTC)
    assert next_run_at(datetime(2026, 12, 31, 7, 0, tzinfo=UTC), time(6, 0)) == datetime(
        2027, 1, 1, 6, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start):
        self.current = start

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


def test_serve_runs_daily_and_survives_errors():
    clock = Clock(datetime(2026, 10, 10, 5, 0, tzinfo=UTC))
    runs = []
    outcomes = iter([None, RuntimeError("boom"), SyncAlreadyRunning("busy")])

    def run_once():
        runs.append(clock.now())
        outcome = next(outcomes)
        if outcome:
            raise outcome

    serve(time(6, 0), run_once, now=clock.now, sleep=clock.sleep,
          should_stop=lambda: len(runs) >= 3)
    assert runs == [
        datetime(2026, 10, 10, 6, 0, tzinfo=UTC),
        datetime(2026, 10, 11, 6, 0, tzinfo=UTC),
        datetime(2026, 10, 12, 6, 0, tzinfo=UTC),
    ]
```

`tests/test_live.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crossword_library.scheduler'`

- [ ] **Step 3: Implement the scheduler**

`src/crossword_library/scheduler.py`:
```python
"""Run a sync once a day at a fixed UTC time."""

from __future__ import annotations

import logging
import time as _time
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone

from .sync import SyncAlreadyRunning

log = logging.getLogger(__name__)


def next_run_at(now: datetime, at: time) -> datetime:
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def serve(
    sync_time: time,
    run_once: Callable[[], object],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = _time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    while not should_stop():
        target = next_run_at(now(), sync_time)
        log.info("next sync at %s", target.isoformat())
        sleep(max(0.0, (target - now()).total_seconds()))
        try:
            run_once()
        except SyncAlreadyRunning:
            log.warning("skipping scheduled sync: another sync is running")
        except Exception:
            log.exception("scheduled sync failed")
```

- [ ] **Step 4: Add `serve` to the CLI**

In `src/crossword_library/cli.py`, add `import signal` to the imports and `from . import scheduler` next to `from . import db, store`, then add this function above `build_parser`:
```python
def cmd_serve(args, settings: Settings) -> int:
    if not settings.puzzmo_token:
        raise ConfigError("PUZZMO_TOKEN is required for serve")
    # SIGTERM (docker stop) becomes SystemExit, so a running sync records itself as failed.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    with db.connect(settings.database_url) as conn:
        applied = db.migrate(conn)
    if applied:
        logging.getLogger(__name__).info("applied migrations: %s", ", ".join(applied))
    scheduler.serve(settings.sync_time, lambda: run_sync_from_settings(settings))
    return EXIT_OK
```

And in `build_parser`, before `return parser`:
```python
    sub.add_parser("serve", help="apply migrations, then sync daily at SYNC_TIME (UTC)").set_defaults(
        func=cmd_serve
    )
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest -v`
Expected: PASS, with the two live tests skipped.

- [ ] **Step 6: Write the Dockerfile and compose app service**

`Dockerfile`:
```dockerfile
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    XDG_CONFIG_HOME=/tmp/xdg

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 app
USER app

CMD ["crossword-library", "serve"]
```

Append to `compose.yml` under `services:` (before the top-level `volumes:`):
```yaml
  app:
    build: .
    profiles: ["app"]
    environment:
      DATABASE_URL: postgresql://crossword:crossword@postgres:5432/crossword_library
      PUZZMO_TOKEN: ${PUZZMO_TOKEN:-}
      SYNC_TIME: ${SYNC_TIME:-06:00}
      DOWNLOAD_DELAY_SECONDS: ${DOWNLOAD_DELAY_SECONDS:-3}
    depends_on:
      postgres:
        condition: service_healthy
```

- [ ] **Step 7: Verify the image (checks migrations are packaged in a non-editable install)**

Run:
```bash
docker compose --profile app build app
docker compose --profile app run --rm app crossword-library migrate
docker compose --profile app run --rm app crossword-library status
```
Expected: the build succeeds; `migrate` prints `schema up to date` (the dev DB was migrated in Task 9) or `applied: 0001_initial.sql`; `status` prints a report. If `migrate` fails with a missing `migrations` directory, uv_build did not package the `.sql` files: add `[tool.uv.build-backend]` with `source-include = ["src/crossword_library/migrations/*.sql"]` to `pyproject.toml` and rebuild.

- [ ] **Step 8: Write the README**

`README.md`:
````markdown
# crossword-library

Self-hosted crossword catalogue and completion tracker. This first slice syncs
Puzzmo crosswords (daily, mini, Big): it downloads each puzzle as `.puz` (plus
Puzzmo's original text) into PostgreSQL and imports which ones you've completed.

Design: [docs/specs/2026-10-10-puzzmo-sync-design.md](docs/specs/2026-10-10-puzzmo-sync-design.md)

## Setup

```sh
mise install && uv sync
docker compose up -d --wait postgres
cp .env.example .env   # add PUZZMO_TOKEN (the bearer token from a logged-in puzzmo.com request)
set -a; . ./.env; set +a
uv run crossword-library migrate
```

## Commands

| Command | What it does |
|---|---|
| `crossword-library sync [--since YYYY-MM] [--retry]` | Catalogue, completions, then missing downloads. Safe to re-run; the first full sync takes about 75 minutes. |
| `crossword-library status` | Catalogue counts, recent runs, problem puzzles, token expiry. |
| `crossword-library list [--series mini] [--done\|--todo]` | List puzzles. |
| `crossword-library mark REF [--at ISO]` / `unmark REF` | Manual completion; REF is an id from `list`, a path like `2026-10-05/crossword/big`, or a Puzzmo id. |
| `crossword-library serve` | Apply migrations, then sync daily at `SYNC_TIME` (UTC). The container's default command. |

Exit codes: 0 ok/partial, 1 failed, 2 configuration error, 3 sync already running.

## Tests

```sh
docker compose up -d --wait postgres
uv run pytest
CROSSWORD_LIVE=1 uv run pytest tests/test_live.py   # opt-in, hits Puzzmo
```

## Notes

- Puzzle downloads are anonymous on purpose: an authenticated download would
  mark the puzzle as started on your Puzzmo account.
- Deployment to triton (`~/Source/benanderson/infra`) is a separate task; see
  the spec, section 9.
````

- [ ] **Step 9: Run the live check (optional, needs network)**

Run: `set -a; . ./.env; set +a; CROSSWORD_LIVE=1 uv run pytest tests/test_live.py -v`
Expected: PASS (the calendar test skips if `PUZZMO_TOKEN` is empty).

- [ ] **Step 10: Commit**

```bash
jj commit -m "Add daily scheduler, Docker image and live checks" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016CujFMHWVzRsNm4ExEVRMJ"
```
