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
