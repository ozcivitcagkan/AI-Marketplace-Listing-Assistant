import sqlite3

import pytest

from listing_assistant.db import latest_schema_version, open_database, schema_version
from listing_assistant.models import new_id, utc_now


def insert_fact(conn, listing_id, fact_id=None):
    fact_id = fact_id or new_id()
    with conn:
        conn.execute(
            "INSERT INTO listing_facts (id, listing_id, field_key, value, source, status,"
            " created_at) VALUES (?, ?, 'color', 'kırmızı', 'user', 'proposed', ?)",
            (fact_id, listing_id, utc_now().isoformat()),
        )
    return fact_id


def test_open_creates_file_and_applies_all_migrations(tmp_path):
    path = tmp_path / "nested" / "app.db"
    conn = open_database(path)
    assert path.exists()
    assert schema_version(conn) == latest_schema_version()
    conn.close()


def test_reopening_does_not_reapply_migrations(tmp_path):
    path = tmp_path / "app.db"
    open_database(path).close()
    conn = open_database(path)  # would fail with "table already exists" if re-applied
    assert schema_version(conn) == latest_schema_version()
    conn.close()


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert_fact(conn, listing_id=new_id())


def test_check_constraint_rejects_unknown_status(conn):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), conn:
        conn.execute(
            "INSERT INTO listings (id, category, status, created_at)"
            " VALUES (?, 'car', 'hacked', ?)",
            (new_id(), utc_now().isoformat()),
        )


# --- Append-only and immutability rules enforced by triggers ----------------------------


def test_audit_logs_cannot_be_updated_or_deleted(conn):
    with conn:
        conn.execute(
            "INSERT INTO audit_logs (id, created_at, actor_type, actor_name, action,"
            " resource_type, details_json) VALUES (?, ?, 'system', 'test', 'x', 'y', '{}')",
            (new_id(), utc_now().isoformat()),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), conn:
        conn.execute("UPDATE audit_logs SET action = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), conn:
        conn.execute("DELETE FROM audit_logs")


def test_fact_value_cannot_change_but_status_can(conn, listing):
    fact_id = insert_fact(conn, listing.id)
    with pytest.raises(sqlite3.IntegrityError, match="only listing_facts.status"), conn:
        conn.execute("UPDATE listing_facts SET value = 'mavi' WHERE id = ?", (fact_id,))
    with conn:
        conn.execute("UPDATE listing_facts SET status = 'approved' WHERE id = ?", (fact_id,))


def test_facts_cannot_be_deleted(conn, listing):
    insert_fact(conn, listing.id)
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"), conn:
        conn.execute("DELETE FROM listing_facts")
