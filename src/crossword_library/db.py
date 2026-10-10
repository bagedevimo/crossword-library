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
