# Puzzmo Sync — Design

**Status:** Draft for review
**Date:** 2026-10-10
**Builds on:** [2026-10-09-crossword-library-product-spec.md](2026-10-09-crossword-library-product-spec.md)

## 1. Goal

The first slice of the crossword library: a service that

1. catalogues Puzzmo crosswords (daily, mini, Big),
2. downloads each puzzle as `.puz` (plus the original Puzzmo source), and
3. imports which puzzles the user has completed on Puzzmo, keeping a durable
   completion record that can also be set manually.

It runs as a container on **triton** (the home server managed from
`~/Source/benanderson/infra`) and does a full sync once a day. It is developed
and tested on the laptop.

### Changes to the product spec

- **Importing completions from Puzzmo is in scope** (product spec §3 listed
  publisher-side completion import as a non-goal; open question #7). The
  discovery spike showed it is cheap and reliable for Puzzmo.
- **PostgreSQL instead of SQLite** (product spec §5), because the service will
  grow an HTTP API and web UI.
- **Puzzle identity** (open question #2) is resolved for Puzzmo: identity is
  Puzzmo's own puzzle ID; the URL date + type is for display only (see §4).

### Out of scope for this slice

HTTP API, web UI, NYT, KOReader and desktop integration, alerting. These build on
the tables defined here.

## 2. Discovery findings

Verified on 2026-10-10 against the live service with the user's account.

### Puzzmo GraphQL API (completions and catalogue)

- Endpoint `POST https://api.puzzmo.com/graphql`, query `dailiesForAMonth(month, year)`.
  Authenticated with `authorization: Bearer <JWT>` plus `auth-provider: custom`.
  No other browser headers are required.
- Unauthenticated requests fail with `"Must be logged in"`.
- The JWT is long-lived (the current one: issued 2026-03-17, expires 2027-04-21).
  Its `exp` claim can be read locally without verifying the signature.
- **`month` is 0-indexed** (`month: 9` returns October).
- One request returns every day of the month, each with ~27 puzzles across all
  games. `gameFilter: "crossword"` is **ignored**, so filter on our side.
- Each puzzle entry has `urlPath` (e.g. `2026-10-05/crossword/big`),
  `puzzle.id`, `puzzle.name`, `puzzle.game.slug`, `communityPuzzle`, and
  `puzzle.currentAccountGamePlayed`, which is `null` (never opened) or
  `{completed, pointsAwarded, ...}`.
- Fields found on `GamePlayed`: `completed`, `completedAt`, `createdAt`,
  `updatedAt`, `elapsedTimeSecs`, `pointsAwarded`, `slug`, `id`. Introspection
  is disabled, so other fields can only be found by guessing.
- Puzzmo's day boundary is 05:00 UTC; the date in `urlPath` is authoritative.
- **A Big crossword spans several days.** The same `puzzle.id` (and the same
  `GamePlayed`) appears on each day it is live (e.g. 5, 6 and 7 Oct 2026).
- Crossword types seen in `urlPath`: `crossword` (daily), `crossword/mini`,
  `crossword/big`.

### Puzzle content (downloads)

- `xword-dl` (2025.10.14) supports `pzm` (daily) and `pzmb` (Big), with or
  without a date, and no authentication. Historical dailies were confirmed
  back to 2025-01-15.
- `xword-dl` fetches via the `startOrFindGameplay` GraphQL mutation with
  `finderKey: "today:/<urlPath>"` and a random `Puzzmo-Gameplay-Id`. This
  **creates a gameplay record**, so it must stay anonymous: never send the
  user's token, or puzzles get marked as started on their account.
- **Minis** work through the same mechanism with `finderKey`
  `today:/<date>/crossword/mini`, via a three-line subclass of
  `PuzzmoDownloader`. Confirmed for 2026-03-08, 2026-04-13 and 2026-10-08.
- The response contains the puzzle in Puzzmo's text ("xd") format, including
  metadata such as `publishdate`. `xword-dl` converts it to `.puz`.
- `.puz` is Latin-1 only, so emoji in clues crash a direct `save()`.
  `xword-dl`'s `download()` applies `sanitize_for_puzfile` (emoji become
  `:name:`, accents are kept). Always go through that path.
- The `.puz` file contains no Puzzmo ID, but its title matches `puzzle.name`
  in the calendar for the same `urlPath`.
- Requesting a Big by a mid-run date returns "No puzzle found". Using the
  first-appearance `urlPath` works.

### Availability

| Series | Earliest seen | Notes |
|---|---|---|
| Daily | ~Oct 2023 (puzzle #1091 on 2026-10-10) | download confirmed to Jan 2025 |
| Big | Jan 2025 | biweekly, then monthly; spans several days |
| Mini | between Aug and Oct 2025 | daily since Oct 2025 |

## 3. Architecture

One Python package, `crossword_library`, with CLI `crossword-library`.

| Module | Responsibility |
|---|---|
| `puzzmo` | Calendar client: fetch a month, parse crossword entries and completions, handle 0-based months, check token expiry. The **only** code that uses the token. |
| `fetch` | Download puzzle content through `xword-dl` (anonymous). Generic over daily/mini/Big via `finderKey = "today:/" + urlPath`. Returns the original xd text and sanitised `.puz` bytes. |
| `store` | Postgres access (psycopg 3): catalogue, files, completions, run log. |
| `sync` | Orchestrates one run (see §5). Takes the advisory lock. |
| `cli` | `sync [--since DATE] [--retry]`, `mark`/`unmark <puzzle>`, `list`, `status`, `migrate`, `serve`. |

Migrations are numbered plain-SQL files applied by a small runner (`migrate`,
also run automatically by `serve`). No ORM.

## 4. Data model

### `puzzles`

One row per puzzle, not per calendar day.

| column | type | notes |
|---|---|---|
| `id` | bigint identity PK | internal |
| `publisher` | text | `puzzmo` |
| `source_id` | text | Puzzmo `puzzle.id`; `UNIQUE (publisher, source_id)` is the dedupe key |
| `series` | text | `daily` / `mini` / `big` |
| `puzzle_date` | date | from the first-appearance `urlPath` |
| `source_url` | text | first-appearance `urlPath`, e.g. `2026-10-05/crossword/big`; also used to fetch |
| `title`, `author` | text | display |
| `download_attempts` | int, default 0 | |
| `last_download_attempt_at` | timestamptz | |
| `last_download_error` | text | null when last attempt succeeded |
| `first_seen_at`, `updated_at` | timestamptz | |

### `puzzle_files`

| column | type | notes |
|---|---|---|
| `puzzle_id` | bigint FK | |
| `format` | text | `xd` (original Puzzmo text) or `puz` |
| `content` | bytea | 0.5–3 KB each |
| `sha256` | text | |
| `fetched_at` | timestamptz | |

`UNIQUE (puzzle_id, format)`. Files live in Postgres so one `pg_dump` backs up
everything.

### `completions`

At most one row per puzzle.

| column | type | notes |
|---|---|---|
| `puzzle_id` | bigint PK/FK | |
| `completed` | boolean | |
| `completed_at` | timestamptz | Puzzmo's `completedAt`, or when manually marked |
| `source` | text | `puzzmo` / `manual` (later `koreader`, …) |
| `recorded_at` | timestamptz | |

Rules:

- No row means "not marked complete".
- Imports never remove a completion and never set `completed = false`.
- A `manual` row is never overwritten by an import. `unmark` writes
  `completed = false, source = manual`, so a puzzle finished on Puzzmo can be
  redone elsewhere without the next sync marking it complete again.
- A started-but-unfinished Puzzmo game produces no row.

### `sync_runs`

`id`, `started_at`, `finished_at`, `status` (`running` / `ok` / `partial` /
`failed`), `counts` (jsonb: months scanned, puzzles added, completions added,
files fetched, download failures), `errors` (jsonb list), `token_expires_at`.

## 5. Sync flow

1. **Lock.** Open a dedicated connection and call
   `pg_try_advisory_lock(<fixed key>)`. If it is not acquired, exit with
   "sync already running" and a distinct exit code. Hold the connection for the
   whole run; Postgres releases the lock if the process dies. Once the lock is
   held, mark any leftover `running` rows in `sync_runs` as `failed`
   (abandoned), then insert this run's row.
2. **Token check.** Read the JWT `exp` claim and record it on the run. Log a
   warning if it expires within 30 days.
3. **Calendar scan.** For every month from `SYNC_SINCE` (default `2023-10`) or `--since` through the current month, make one calendar request.
   Keep entries where `game.slug == "crossword"`, `communityPuzzle` is null, and
   the `urlPath` type is the daily, `crossword/mini` or `crossword/big`.
   Rescanning everything daily (~37 requests now) is intentional, so that
   backlog completions of old puzzles are picked up.
4. **Catalogue upsert.** Upsert on `(publisher, source_id)`. Keep the earliest
   `puzzle_date`/`source_url`, and refresh title and author. Each month commits
   separately.
5. **Completion upsert.** For entries where `currentAccountGamePlayed.completed`
   is true and there is no `manual` row, upsert
   `completed = true, completed_at = completedAt, source = puzzmo`.
6. **Downloads.** For puzzles with no `puz` file whose retry rules allow it
   (§6), fetch anonymously, store `xd` and `puz` in one transaction, and sleep
   `DOWNLOAD_DELAY_SECONDS` (default 3) between fetches. If the fetched title
   differs from the catalogue title, store anyway and add a warning to the run.
7. **Finish.** Set the status, counts, errors and `finished_at`, then release
   the lock.

Every step commits as it goes and skips work already done, so an interrupted run,
including the ~75-minute first import of about 1,500 puzzles, continues where it
stopped on the next run.

**Schedule:** `serve` applies migrations, then runs `sync` daily at `SYNC_TIME`
(default `06:00` UTC, just after Puzzmo's rollover). `sync` can also be run by
hand at any time.

## 6. Error handling

| Failure | Behaviour |
|---|---|
| Token rejected (401 / "Must be logged in") | Abort the run as `failed` with a clear "replace PUZZMO_TOKEN" message. No retries. |
| Token expiring within 30 days | Warning in the log and on the run. |
| Network error or 5xx on a calendar request | Up to 3 retries with backoff. Then that month fails, other months continue, and the run is `partial`. |
| GraphQL `errors` in a response | That month fails, the error text is recorded, and the run is `partial`. Never silently treated as "no data". |
| Download failure (no puzzle found, conversion error, network) | Increment `download_attempts`, record the error, continue; the run is `partial`. Retry on later runs at most once per 24 hours. After 5 attempts, stop retrying automatically (listed as a problem). `sync --retry` forces a retry. |
| Title mismatch on download | Store, and record a warning. |

**Surfacing:** logs go to stdout (`docker logs` / `mise run logs`).
`crossword-library status` shows recent runs, problem puzzles, days until token
expiry, and catalogue and completion counts. A `failed` run exits non-zero.
Alerting is deferred to the web UI.

## 7. Configuration and secrets

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | required |
| `PUZZMO_TOKEN` | — | required for `sync`; never logged; sent only by the `puzzmo` module |
| `SYNC_TIME` | `06:00` | UTC, used by `serve` |
| `DOWNLOAD_DELAY_SECONDS` | `3` | |
| `SYNC_SINCE` | `2023-10` | earliest month scanned; confirm Puzzmo's first month with crosswords during implementation (empty months are harmless) |

Locally these come from an ignored `.env` (`.env.example` is committed). On
triton, `DATABASE_URL`'s password and `PUZZMO_TOKEN` come from the stack's
`secrets.sops.env`.

## 8. Testing

- **`puzzmo`:** saved responses trimmed from real ones, with account
  identifiers removed. They cover 0-based months, Big spanning several days,
  crossword filtering, community puzzles, 401, GraphQL `errors`, and JWT expiry
  parsing.
- **`fetch`:** saved xd payloads (one daily, mini and Big, including one with
  an emoji clue) converted to `.puz` and read back. Tests never call the real
  Puzzmo.
- **`store` and `sync`:** against a real Postgres (local `compose.yml`), with a
  throwaway database per test session. They cover: running twice changes
  nothing; imports never remove a completion; a manual row wins over an import;
  a Big puzzle stays one row; download failure counting and retry rules; the
  lock blocks a concurrent run; abandoned `running` rows are cleaned up.
- **Live check** (opt-in, not in the default run): fetch the current month's
  calendar with the real token and one anonymous download, to catch changes on
  Puzzmo's side.

## 9. Development and deployment

- **Local:** `mise` with `.tool-versions` (Python), `uv` for dependencies,
  `compose.yml` with `postgres:17` (and an optional app service), and pytest.
- **Image:** a `Dockerfile` whose default command is `crossword-library serve`.
- **triton** (later task, following `infra/docs/adding-a-service.md`):
  `stacks/crossword-library/compose.yml` with the app and `postgres:17` on a
  stack-local network (not `edge`), named volume `crossword-library_pgdata`,
  `secrets.sops.env` holding `PUZZMO_TOKEN` and the Postgres password, and the
  stack registered in `group_vars/all/vars.yml`. No Traefik labels or DNS until
  there is a web UI. The volume should go into the backups work item.
- **Open:** where the image comes from (a registry, or built on triton). To be
  decided in the deployment task.
- **First import:** `crossword-library sync` (on triton:
  `docker compose exec app crossword-library sync`).
