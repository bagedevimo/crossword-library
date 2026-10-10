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
