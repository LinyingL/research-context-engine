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


def test_migrate_rolls_back_and_self_heals_on_mid_script_failure(tmp_path):
    """T0-fix blocker regression: a migration that fails partway must not
    leave a half-applied, permanently-stuck schema behind.

    Reproduces the reported failure mode directly: a migration file whose
    first statement succeeds and second statement is invalid SQL used to
    leave the first CREATE TABLE committed with no schema_migrations row,
    so every retry died on "table already exists". migrate() must instead
    roll the whole file back, and a corrected retry must succeed cleanly.
    """
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    broken_sql = (
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY);\n"
        "THIS IS NOT VALID SQL;\n"
    )
    (migrations_dir / "0001_init.sql").write_text(broken_sql)

    conn = db.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.migrate(conn, migrations_dir)

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "widgets" not in tables

        applied = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
        assert 1 not in applied

        # Retrying after fixing the file must succeed -- not "already exists".
        (migrations_dir / "0001_init.sql").write_text(
            "CREATE TABLE widgets (id INTEGER PRIMARY KEY);\n"
        )
        assert db.migrate(conn, migrations_dir) == [1]
    finally:
        conn.close()


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
            conn,
            "commit:abc",
            "figure:fig1.png",
            "haunts",
            "test-extractor",
            {"file": "plot.py", "line": 1},
            1.0,
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
            {"file": "plot.py", "line": 1},
            1.0,
            status="in_review",
        )


def test_upsert_edge_rejects_confirmed_or_rejected_status_in_python(conn):
    # upsert_edge is the machine path -- it must never be able to conjure a
    # human verdict out of thin air. Only set_edge_status may write these.
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    for illegal_status in ("confirmed", "rejected"):
        with pytest.raises(ValueError):
            db.upsert_edge(
                conn,
                "commit:abc",
                "figure:fig1.png",
                "generates",
                "test-extractor",
                {"file": "plot.py", "line": 1},
                1.0,
                status=illegal_status,
            )


# -- confidence range CHECK ------------------------------------------------


def test_upsert_edge_rejects_confidence_out_of_range_in_python(conn):
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    for bad_confidence in (-0.01, 1.01, 2.0, -5.0):
        with pytest.raises(ValueError):
            db.upsert_edge(
                conn,
                "commit:abc",
                "figure:fig1.png",
                "generates",
                "test-extractor",
                {"file": "plot.py", "line": 1},
                bad_confidence,
            )


def test_upsert_edge_accepts_confidence_boundaries(conn):
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    db.upsert_edge(
        conn, "commit:abc", "figure:fig1.png", "generates", "test-extractor",
        {"file": "plot.py", "line": 1}, 0.0,
    )
    db.upsert_edge(
        conn, "commit:abc", "figure:fig1.png", "generates", "test-extractor",
        {"file": "plot.py", "line": 1}, 1.0,
    )


def test_illegal_edge_type_rejected_at_db_level(conn):
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO edges (src, dst, type, extractor, evidence, confidence, status)
            VALUES (?, ?, ?, ?, '{"file": "plot.py", "line": 1}', 1.0, 'auto')
            """,
            ("commit:abc", "figure:fig1.png", "haunts", "test-extractor"),
        )


def test_edge_foreign_key_enforced(conn):
    # src/dst must reference existing nodes.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO edges (src, dst, type, extractor, evidence, confidence, status)
            VALUES (?, ?, 'generates', 'test-extractor', '{"file": "plot.py", "line": 1}', 1.0, 'auto')
            """,
            ("commit:missing", "figure:missing.png"),
        )


# -- evidence-required invariant ------------------------------------------


def test_upsert_edge_rejects_empty_evidence_in_python(conn):
    # HANDOFF-SPEC.md section 2/4 hard invariant: no edge without evidence.
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    with pytest.raises(ValueError):
        db.upsert_edge(
            conn, "commit:abc", "figure:fig1.png", "generates", "test-extractor", {}, 1.0
        )


def test_empty_evidence_rejected_at_db_level(conn):
    # Proves the CHECK constraint is enforced by SQLite itself, not just by
    # the Python-level guard in upsert_edge.
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO edges (src, dst, type, extractor, evidence, confidence, status)
            VALUES (?, ?, 'generates', 'test-extractor', '{}', 1.0, 'auto')
            """,
            ("commit:abc", "figure:fig1.png"),
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
    """Re-upserting the same (src,dst,type,extractor) key updates the row in
    place -- still one edge, confidence still moves to the latest value.
    Evidence itself is covered separately below (T10: it now accumulates
    rather than being overwritten)."""
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


# -- evidence accumulates as occurrences (T10) -----------------------------
#
# HANDOFF-SPEC.md section 2/4 hard invariant ("no edge without evidence")
# plus a real testbed regression: the old UNIQUE(src,dst,type,extractor)
# upsert overwrote `evidence` wholesale, so a figure \included twice in the
# same section silently lost the first occurrence's evidence. Evidence is
# now always `{"occurrences": [...]}`, even for a single occurrence, and a
# second upsert on the same key folds its evidence in rather than replacing
# it. status/confidence protection logic is unchanged (see the tests above
# and below).


def test_upsert_edge_first_write_wraps_evidence_in_occurrences_structure(conn):
    # "单次也用该结构" -- even a brand-new edge's evidence is wrapped, not bare.
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    db.upsert_edge(
        conn, "commit:abc", "figure:fig1.png", "generates", "ast-scanner",
        {"file": "plot.py", "line": 10}, 0.5,
    )
    edge = db.query_edges(conn, src="commit:abc", dst="figure:fig1.png")[0]
    assert edge["evidence"] == {"occurrences": [{"file": "plot.py", "line": 10}]}


def test_upsert_edge_merges_distinct_evidence_into_occurrences_list(conn):
    # The regression this fixes: same figure referenced twice (e.g. from two
    # different lines of the same section) must keep BOTH occurrences, not
    # just the latest one.
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    db.upsert_edge(
        conn, "commit:abc", "figure:fig1.png", "generates", "ast-scanner",
        {"file": "plot.py", "line": 10}, 0.5,
    )
    db.upsert_edge(
        conn, "commit:abc", "figure:fig1.png", "generates", "ast-scanner",
        {"file": "plot.py", "line": 12}, 0.9,
    )
    edge = db.query_edges(conn, src="commit:abc", dst="figure:fig1.png")[0]
    assert edge["evidence"] == {
        "occurrences": [
            {"file": "plot.py", "line": 10},
            {"file": "plot.py", "line": 12},
        ]
    }


def test_upsert_edge_dedupes_identical_evidence_by_content(conn):
    # An idempotent re-ingest of the exact same line must not grow the list.
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    for _ in range(3):
        db.upsert_edge(
            conn, "commit:abc", "figure:fig1.png", "generates", "ast-scanner",
            {"file": "plot.py", "line": 10}, 0.5,
        )
    edge = db.query_edges(conn, src="commit:abc", dst="figure:fig1.png")[0]
    assert edge["evidence"] == {"occurrences": [{"file": "plot.py", "line": 10}]}


def test_upsert_edge_migrates_legacy_bare_evidence_row_on_next_write(conn):
    # A pre-T10 row has evidence stored as a bare dict (no "occurrences"
    # wrapper). The next machine write must fold it in as a single legacy
    # occurrence rather than erroring or discarding it.
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    conn.execute(
        """
        INSERT INTO edges (src, dst, type, extractor, evidence, confidence, status)
        VALUES (?, ?, 'generates', 'ast-scanner', '{"file": "plot.py", "line": 1}', 0.5, 'auto')
        """,
        ("commit:abc", "figure:fig1.png"),
    )
    conn.commit()

    db.upsert_edge(
        conn, "commit:abc", "figure:fig1.png", "generates", "ast-scanner",
        {"file": "plot.py", "line": 2}, 0.7,
    )

    edge = db.query_edges(conn, src="commit:abc", dst="figure:fig1.png")[0]
    assert edge["evidence"] == {
        "occurrences": [
            {"file": "plot.py", "line": 1},
            {"file": "plot.py", "line": 2},
        ]
    }


def test_upsert_edge_caps_occurrences_and_drops_oldest_with_warning(conn, caplog):
    db.upsert_node(conn, "commit:abc", "commit")
    db.upsert_node(conn, "figure:fig1.png", "figure")
    with caplog.at_level("WARNING", logger="rce.db"):
        for line in range(1, 23):  # 22 distinct occurrences, cap is 20
            db.upsert_edge(
                conn, "commit:abc", "figure:fig1.png", "generates", "ast-scanner",
                {"file": "plot.py", "line": line}, 0.5,
            )

    edge = db.query_edges(conn, src="commit:abc", dst="figure:fig1.png")[0]
    occurrences = edge["evidence"]["occurrences"]
    assert len(occurrences) == 20
    # Oldest (line 1, 2) dropped; newest (line 22) kept.
    assert occurrences[0] == {"file": "plot.py", "line": 3}
    assert occurrences[-1] == {"file": "plot.py", "line": 22}
    assert any("exceeded cap" in r.message for r in caplog.records)


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
        conn,
        "claim:paper.tex#abc123",
        "experiment:run1",
        "backed_by",
        "7b-judge",
        {"claim_text": "87.3%", "run_metric": "accuracy=0.873"},
        0.4,
        status="pending",
    )
    db.upsert_edge(
        conn,
        "claim:paper.tex#abc123",
        "experiment:run2",
        "backed_by",
        "7b-judge",
        {"claim_text": "87.3%", "run_metric": "accuracy=0.871"},
        0.95,
        status="auto",
    )

    pending = db.pending_edges(conn)
    assert len(pending) == 1
    assert pending[0]["dst"] == "experiment:run1"


# -- edge status is human-owned once acted on ------------------------------


def test_reingest_never_overwrites_confirmed_edge_status(conn):
    """Core trust-model regression (T0-fix blocker).

    7b-judge creates a pending edge -> a human confirms it -> the same
    extractor re-ingests overnight and tries to write status='pending'
    again. The human's confirmation must survive: a routine machine
    re-ingest must never reopen a confirmed (or rejected) edge.
    """
    db.upsert_node(conn, "claim:paper.tex#abc123", "claim")
    db.upsert_node(conn, "experiment:run1", "experiment")

    db.upsert_edge(
        conn,
        "claim:paper.tex#abc123",
        "experiment:run1",
        "backed_by",
        "7b-judge",
        {"claim_text": "87.3%", "run_metric": "accuracy=0.873"},
        0.4,
        status="pending",
    )

    # Human confirms it via set_edge_status, keyed on the SAME
    # (src, dst, type, extractor) row -- upsert_edge cannot write 'confirmed'
    # itself (see test_upsert_edge_rejects_confirmed_or_rejected_status_in_python).
    db.set_edge_status(
        conn, "claim:paper.tex#abc123", "experiment:run1", "backed_by", "7b-judge",
        status="confirmed",
    )

    # Overnight re-ingest by the SAME extractor tries to reset it to
    # pending -- this is the exact clobber the blocker report reproduced.
    db.upsert_edge(
        conn,
        "claim:paper.tex#abc123",
        "experiment:run1",
        "backed_by",
        "7b-judge",
        {"claim_text": "87.3%", "run_metric": "accuracy=0.873 (re-extracted)"},
        0.4,
        status="pending",
    )

    edges = db.query_edges(
        conn, src="claim:paper.tex#abc123", dst="experiment:run1", type="backed_by"
    )
    assert len(edges) == 1
    assert edges[0]["status"] == "confirmed"
    # evidence/confidence remain machine-owned and do keep updating -- but
    # (T10) evidence now accumulates as occurrences rather than being
    # overwritten, so both the original and re-extracted evidence survive.
    occurrences = edges[0]["evidence"]["occurrences"]
    assert len(occurrences) == 2
    assert occurrences[-1]["run_metric"] == "accuracy=0.873 (re-extracted)"

    # A confirmed edge must never show up back in the confirmation queue.
    assert edges[0]["dst"] not in {e["dst"] for e in db.pending_edges(conn)}


def test_reingest_never_overwrites_rejected_edge_status(conn):
    db.upsert_node(conn, "claim:paper.tex#def456", "claim")
    db.upsert_node(conn, "experiment:run3", "experiment")

    db.upsert_edge(
        conn,
        "claim:paper.tex#def456",
        "experiment:run3",
        "backed_by",
        "7b-judge",
        {"claim_text": "12.0", "run_metric": "loss=12.0"},
        0.3,
        status="pending",
    )
    db.set_edge_status(
        conn, "claim:paper.tex#def456", "experiment:run3", "backed_by", "7b-judge",
        status="rejected",
    )
    # Same extractor re-ingests and tries to put it back in the queue.
    db.upsert_edge(
        conn,
        "claim:paper.tex#def456",
        "experiment:run3",
        "backed_by",
        "7b-judge",
        {"claim_text": "12.0", "run_metric": "loss=12.0"},
        0.3,
        status="pending",
    )

    edges = db.query_edges(
        conn, src="claim:paper.tex#def456", dst="experiment:run3", type="backed_by"
    )
    assert len(edges) == 1
    assert edges[0]["status"] == "rejected"


# -- set_edge_status: the human-only write path ----------------------------


def test_set_edge_status_lets_human_move_between_any_status(conn):
    # Unlike upsert_edge, set_edge_status is unrestricted: a human correcting
    # their own earlier confirm/reject mistake may move an edge to any of
    # the four statuses, in any order.
    db.upsert_node(conn, "claim:paper.tex#xyz", "claim")
    db.upsert_node(conn, "experiment:run9", "experiment")
    db.upsert_edge(
        conn, "claim:paper.tex#xyz", "experiment:run9", "backed_by", "7b-judge",
        {"claim_text": "1.0", "run_metric": "loss=1.0"}, 0.5, status="pending",
    )

    for status in ("confirmed", "rejected", "pending", "confirmed", "auto"):
        db.set_edge_status(
            conn, "claim:paper.tex#xyz", "experiment:run9", "backed_by", "7b-judge",
            status=status,
        )
        edge = db.query_edges(
            conn, src="claim:paper.tex#xyz", dst="experiment:run9", type="backed_by"
        )[0]
        assert edge["status"] == status


def test_set_edge_status_rejects_illegal_status(conn):
    db.upsert_node(conn, "claim:paper.tex#xyz", "claim")
    db.upsert_node(conn, "experiment:run9", "experiment")
    db.upsert_edge(
        conn, "claim:paper.tex#xyz", "experiment:run9", "backed_by", "7b-judge",
        {"claim_text": "1.0", "run_metric": "loss=1.0"}, 0.5, status="pending",
    )
    with pytest.raises(ValueError):
        db.set_edge_status(
            conn, "claim:paper.tex#xyz", "experiment:run9", "backed_by", "7b-judge",
            status="in_review",
        )


def test_set_edge_status_is_a_noop_on_unknown_edge(conn):
    # Mirrors set_human_fields's behavior for an unknown node_id: no matching
    # row means no error and no row is created.
    db.set_edge_status(
        conn, "claim:missing", "experiment:missing", "backed_by", "7b-judge",
        status="confirmed",
    )
    assert db.query_edges(conn) == []
