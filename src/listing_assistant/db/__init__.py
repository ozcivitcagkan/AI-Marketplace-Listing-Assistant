"""SQLite connection setup and numbered schema migrations."""

import sqlite3
from importlib import resources
from pathlib import Path


def _migration_scripts() -> list[tuple[int, str]]:
    folder = resources.files("listing_assistant.db") / "migrations"
    scripts = [
        (int(entry.name.split("_", 1)[0]), entry.read_text(encoding="utf-8"))
        for entry in folder.iterdir()
        if entry.name.endswith(".sql")
    ]
    return sorted(scripts)


def latest_schema_version() -> int:
    return _migration_scripts()[-1][0]


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    """Apply every migration newer than the database's recorded version, in order."""
    current = schema_version(conn)
    for version, script in _migration_scripts():
        if version <= current:
            continue
        try:
            # One transaction per migration: it applies completely or not at all.
            conn.executescript("BEGIN;\n" + script + "\nCOMMIT;")
        except sqlite3.Error:
            conn.rollback()
            raise
        if schema_version(conn) != version:
            raise RuntimeError(f"migration {version} must end with PRAGMA user_version = {version}")


def open_database(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # SQLite ignores foreign keys unless enabled on every connection.
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn
