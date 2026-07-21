import sqlite3

import pytest

from rce import db


def test_migrate_builds_schema_from_scratch(conn):
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"nodes", "edges", "schema_migrations"} <= tables


def test_migrate_is_idempotent(conn):
    # conn fixture already migrated once; migrating again must be a no-op.
    assert db.migrate(conn) == []


def test_foreign_keys_pragma_enabled(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# -- node type CHECK -----------------------------------------------------


def test_upsert_node_rejects_illegal_type_in_python(conn):
    with pytest.raises(ValueError):
        db.upsert_node(conn, "meeting:standup", "meeting")


def test_illegal_node_type_rejected_at_db_level(conn):
    # Proves the CHECK constraint is enforced by SQLite itself, not just by
    # the Python-level guard in upsert_node.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO nodes (id, type, attrs, human_fields) VALUES (?, ?, '{}', '{}')",
            ("meeting:standup", "meeting"),
        )


# -- edge type / status CHECK --------------------------------------------


def test_upsert_edge_rejects_illegal_type_in_python(conn):
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    with pytest.raises(ValueError):
        db.upsert_edge(
            conn, "commit:abc", "figure:fig1.png", "haunts", "test-extractor", {}, 1.0
        )


def test_upsert_edge_rejects_illegal_status_in_python(conn):
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    with pytest.raises(ValueError):
        db.upsert_edge(
            conn,
            "commit:abc",
            "figure:fig1.png",
            "generates",
            "test-extractor",
            {},
            1.0,
            status="in_review",
        )


def test_illegal_edge_type_rejected_at_db_level(conn):
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO edges (src, dst, type, extractor, evidence, confidence, status)
            VALUES (?, ?, ?, ?, '{}', 1.0, 'auto')
            """,
            ("commit:abc", "figure:fig1.png", "haunts", "test-extractor"),
        )


def test_edge_foreign_key_enforced(conn):
    # src/dst must reference existing nodes.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO edges (src, dst, type, extractor, evidence, confidence, status)
            VALUES (?, ?, 'generates', 'test-extractor', '{}', 1.0, 'auto')
            """,
            ("commit:missing", "figure:missing.png"),
        )


# -- idempotent upsert ----------------------------------------------------


def test_upsert_node_idempotent(conn):
    db.upsert_node(conn, "commit:abc", "commit", title="first")
    db.upsert_node(conn, "commit:abc", "commit", title="second", attrs={"n": 2})

    count = conn.execute("SELECT COUNT(*) FROM nodes WHERE id='commit:abc'").fetchone()[0]
    assert count == 1

    node = db.get_node(conn, "commit:abc")
    assert node["title"] == "second"
    assert node["attrs"] == {"n": 2}


def test_upsert_edge_idempotent(conn):
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")

    db.upsert_edge(
        conn,
        "commit:abc",
        "figure:fig1.png",
        "generates",
        "ast-scanner",
        {"file": "plot.py", "line": 10},
        0.5,
    )
    db.upsert_edge(
        conn,
        "commit:abc",
        "figure:fig1.png",
        "generates",
        "ast-scanner",
        {"file": "plot.py", "line": 12},
        0.9,
    )

    edges = db.query_edges(conn, src="commit:abc", dst="figure:fig1.png")
    assert len(edges) == 1
    assert edges[0]["confidence"] == 0.9
    assert edges[0]["evidence"] == {"file": "plot.py", "line": 12}


# -- human_fields invariant -----------------------------------------------


def test_upsert_node_never_overwrites_human_fields(conn):
    db.upsert_node(conn, "experiment:run1", "experiment", title="run 1")
    db.set_human_fields(conn, "experiment:run1", {"status": "verified_by_owner"})

    # A later machine re-ingest with completely different attrs must not
    # touch human_fields.
    db.upsert_node(conn, "experiment:run1", "experiment", title="run 1 (re-ingested)", attrs={"loss": 0.1})

    node = db.get_node(conn, "experiment:run1")
    assert node["human_fields"] == {"status": "verified_by_owner"}
    assert node["attrs"] == {"loss": 0.1}


def test_new_node_has_empty_human_fields(conn):
    db.upsert_node(conn, "project:demo", "project")
    node = db.get_node(conn, "project:demo")
    assert node["human_fields"] == {}


# -- confirmation queue -----------------------------------------------------


def test_pending_edges_is_the_confirmation_queue(conn):
    db.upsert_node(conn, "claim:paper.tex#abc123", "claim")
    db.upsert_node(conn, "experiment:run1", "experiment")
    db.upsert_node(conn, "experiment:run2", "experiment")

    db.upsert_edge(
        conn, "claim:paper.tex#abc123", "experiment:run1", "backed_by", "7b-judge", {}, 0.4, status="pending"
    )
    db.upsert_edge(
        conn, "claim:paper.tex#abc123", "experiment:run2", "backed_by", "7b-judge", {}, 0.95, status="auto"
    )

    pending = db.pending_edges(conn)
    assert len(pending) == 1
    assert pending[0]["dst"] == "experiment:run1"
