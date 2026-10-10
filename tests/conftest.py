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
