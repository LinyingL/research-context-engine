import sqlite3
import threading
import time
from unittest import mock

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


# -- migration 0002: attempt/uses ontology extension -----------------------


def test_0002_migration_preserves_data_from_0001_only_db(tmp_path):
    """The real-world upgrade path: a project whose .rce/graph.db already
    ran 0001 (and has real nodes/edges in it) picks up 0002 later, once this
    package ships it. Simulated by applying only a copy of 0001 first, then
    re-migrating with the real migrations directory (0001 already recorded,
    so only 0002 newly applies) -- must not lose a single row.
    """
    db_path = tmp_path / "graph.db"
    only_0001_dir = tmp_path / "only_0001"
    only_0001_dir.mkdir()
    (only_0001_dir / "0001_init.sql").write_text(
        (db.DEFAULT_MIGRATIONS_DIR / "0001_init.sql").read_text()
    )

    conn = db.connect(db_path)
    try:
        assert db.migrate(conn, only_0001_dir) == [1]

        db.upsert_node(conn, "project:demo", "project", title="Demo")
        db.upsert_node(conn, "commit:abc123", "commit")
        db.upsert_node(conn, "figure:fig1.png", "figure")
        db.set_human_fields(conn, "figure:fig1.png", {"caption_ok": True})
        db.upsert_edge(
            conn, "commit:abc123", "figure:fig1.png", "generates",
            "test-extractor", {"file": "plot.py", "line": 10}, 1.0,
        )
        db.set_edge_status(conn, "commit:abc123", "figure:fig1.png", "generates", "test-extractor", "confirmed")

        nodes_before = {n["id"]: n for n in (
            db.get_node(conn, "project:demo"),
            db.get_node(conn, "commit:abc123"),
            db.get_node(conn, "figure:fig1.png"),
        )}
        edges_before = db.query_edges(conn)

        # Now upgrade with the package's real migrations dir: 0001 is already
        # recorded, so this applies [2, 3] (migration 0003 -- task W2 -- now
        # also ships in DEFAULT_MIGRATIONS_DIR).
        assert db.migrate(conn) == [2, 3]

        for node_id, before in nodes_before.items():
            assert db.get_node(conn, node_id) == before
        assert db.query_edges(conn) == edges_before

        # And the widened CHECK constraints are now live.
        db.upsert_node(conn, "attempt:map.md#1", "attempt", title="attempt 1")
        db.upsert_edge(
            conn, "attempt:map.md#1", "commit:abc123", "uses",
            "test-extractor", {"file": "map.md", "line": 30}, 1.0,
        )
    finally:
        conn.close()


def test_0002_migration_applies_cleanly_on_a_fresh_empty_db(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")
    try:
        # Migration 0003 (task W2) now also ships in DEFAULT_MIGRATIONS_DIR.
        assert db.migrate(conn) == [1, 2, 3]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"nodes", "edges", "schema_migrations"} <= tables
    finally:
        conn.close()


# -- migration 0003: dataflow ontology extension (task W2) -----------------


def test_0003_migration_preserves_data_from_0001_0002_db(tmp_path):
    """Same upgrade-path guarantee as 0002's own test, one version later: a
    project whose .rce/graph.db already ran 0001+0002 (with real rows,
    including an attempt/uses row exercising 0002's own widened types)
    picks up 0003 later -- must not lose a single row, and the newly widened
    CHECK constraints (script/dataset nodes, reads/writes edges) must be
    live afterward.
    """
    db_path = tmp_path / "graph.db"
    only_0001_0002_dir = tmp_path / "only_0001_0002"
    only_0001_0002_dir.mkdir()
    (only_0001_0002_dir / "0001_init.sql").write_text(
        (db.DEFAULT_MIGRATIONS_DIR / "0001_init.sql").read_text()
    )
    (only_0001_0002_dir / "0002_attempt.sql").write_text(
        (db.DEFAULT_MIGRATIONS_DIR / "0002_attempt.sql").read_text()
    )

    conn = db.connect(db_path)
    try:
        assert db.migrate(conn, only_0001_0002_dir) == [1, 2]

        db.upsert_node(conn, "project:demo", "project", title="Demo")
        db.upsert_node(conn, "commit:abc123", "commit")
        db.upsert_node(conn, "figure:fig1.png", "figure")
        db.upsert_node(conn, "attempt:map.md#1", "attempt", title="attempt 1")
        db.upsert_edge(
            conn, "commit:abc123", "figure:fig1.png", "generates",
            "test-extractor", {"file": "plot.py", "line": 10}, 1.0,
        )
        db.upsert_edge(
            conn, "attempt:map.md#1", "commit:abc123", "uses",
            "test-extractor", {"file": "map.md", "line": 30}, 1.0,
        )
        db.set_edge_status(conn, "commit:abc123", "figure:fig1.png", "generates", "test-extractor", "confirmed")

        nodes_before = {n["id"]: n for n in (
            db.get_node(conn, "project:demo"),
            db.get_node(conn, "commit:abc123"),
            db.get_node(conn, "figure:fig1.png"),
            db.get_node(conn, "attempt:map.md#1"),
        )}
        edges_before = db.query_edges(conn)

        # Upgrade with the package's real migrations dir: 0001/0002 already
        # recorded, so this applies exactly [3].
        assert db.migrate(conn) == [3]

        for node_id, before in nodes_before.items():
            assert db.get_node(conn, node_id) == before
        assert db.query_edges(conn) == edges_before

        # And the widened CHECK constraints (task W2) are now live.
        db.upsert_node(conn, "script:scripts/gen.py", "script", title="scripts/gen.py")
        db.upsert_node(conn, "dataset:data/out.csv", "dataset", title="data/out.csv")
        db.upsert_edge(
            conn, "script:scripts/gen.py", "dataset:data/out.csv", "writes",
            "test-extractor", {"file": "scripts/gen.py", "line": 5}, 1.0,
        )
        db.upsert_edge(
            conn, "script:scripts/gen.py", "dataset:data/out.csv", "reads",
            "test-extractor", {"file": "scripts/gen.py", "line": 1}, 1.0,
        )
    finally:
        conn.close()


def test_script_and_dataset_node_types_accepted(conn):
    # Migration 0003 (task W2): the 10th/11th node types.
    db.upsert_node(conn, "script:scripts/gen.py", "script", title="scripts/gen.py")
    db.upsert_node(conn, "dataset:data/out.csv", "dataset", title="data/out.csv")
    assert db.get_node(conn, "script:scripts/gen.py")["type"] == "script"
    assert db.get_node(conn, "dataset:data/out.csv")["type"] == "dataset"


def test_reads_writes_edge_types_accepted(conn):
    # Migration 0003 (task W2): script --reads/writes--> dataset (or figure).
    db.upsert_node(conn, "script:scripts/gen.py", "script")
    db.upsert_node(conn, "dataset:data/out.csv", "dataset")
    db.upsert_edge(
        conn, "script:scripts/gen.py", "dataset:data/out.csv", "writes",
        "test-extractor", {"file": "scripts/gen.py", "line": 5}, 1.0,
    )
    edges = db.query_edges(conn, type="writes")
    assert len(edges) == 1
    assert edges[0]["src"] == "script:scripts/gen.py"
    assert edges[0]["dst"] == "dataset:data/out.csv"


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


def test_attempt_node_type_accepted(conn):
    # Migration 0002: the 9th node type, widened into the CHECK constraint.
    db.upsert_node(
        conn, "attempt:map.md#16", "attempt", title="TopicShift->volatility+adoption",
        attrs={"number": "16", "date": "07-26", "variable": "TopicShift -> RV/NetBuyRate"},
    )
    node = db.get_node(conn, "attempt:map.md#16")
    assert node["type"] == "attempt"
    assert node["attrs"]["number"] == "16"
    assert node["human_fields"] == {}  # verdict/result never land here


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


def test_uses_edge_type_accepted(conn):
    # Migration 0002: `attempt --uses--> commit`, deterministic and
    # machine-written like any other upsert_edge call.
    db.upsert_node(conn, "attempt:map.md#16", "attempt")
    db.upsert_node(conn, "commit:abc123", "commit")
    db.upsert_edge(
        conn, "attempt:map.md#16", "commit:abc123", "uses",
        "test-extractor", {"file": "16-叙事更替_TopicShift.py", "line": 1}, 1.0,
    )
    [edge] = db.query_edges(conn, type="uses")
    assert edge["src"] == "attempt:map.md#16"
    assert edge["dst"] == "commit:abc123"
    assert edge["status"] == "auto"


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


# -- set_edge_semantic_review (S2): sibling key beside occurrences --------
#
# rce.semantic.judge's only write path. Lives beside `occurrences` inside
# the same evidence JSON, and must survive a subsequent machine re-ingest
# (upsert_edge/_merge_edge_evidence) without either side clobbering the
# other -- see _merge_edge_evidence's own docstring for why it now passes
# through any sibling key it finds.


def test_set_edge_semantic_review_adds_sibling_key_beside_occurrences(conn):
    db.upsert_node(conn, "claim:paper.tex#abc", "claim")
    db.upsert_node(conn, "experiment:run_a", "experiment")
    db.upsert_edge(
        conn, "claim:paper.tex#abc", "experiment:run_a", "backed_by", "claims",
        {"metric": "grad_norm_epoch", "metric_value": 1.5786}, 1.0, status="pending",
    )

    db.set_edge_semantic_review(
        conn, "claim:paper.tex#abc", "experiment:run_a", "backed_by", "claims",
        {"related": False, "reason": "coincidental rounding", "better_match": "quantization"},
    )

    edge = db.query_edges(conn, src="claim:paper.tex#abc", dst="experiment:run_a")[0]
    assert edge["evidence"] == {
        "occurrences": [{"metric": "grad_norm_epoch", "metric_value": 1.5786}],
        "semantic_review": {
            "related": False, "reason": "coincidental rounding", "better_match": "quantization",
        },
    }
    assert edge["status"] == "pending"  # untouched -- this is not a status write path


def test_set_edge_semantic_review_is_a_noop_on_unknown_edge(conn):
    # Matches set_human_fields/set_edge_status's behavior for an unknown target.
    db.set_edge_semantic_review(
        conn, "claim:nope#0", "experiment:nope", "backed_by", "claims", {"related": True, "reason": "x"},
    )
    assert db.query_edges(conn) == []


def test_set_edge_semantic_review_survives_concurrent_upsert_edge(tmp_path):
    """Interleaved SELECT -- concurrent occurrence commit -- UPDATE.

    Opus-review blocker: before the fix, set_edge_semantic_review's SELECT
    and UPDATE ran as two separate autocommitted statements with no
    transaction around them. Reproduced directly: a concurrent connection
    committed a new occurrence between those two statements, and
    set_edge_semantic_review's UPDATE then overwrote `evidence` with its own
    stale in-memory copy, destroying the newly-committed occurrence -- a
    realistic window since a judge run stalls on model latency per edge
    while `rce ingest` runs in another process. The fix wraps the SELECT and
    UPDATE in one BEGIN IMMEDIATE transaction (mirroring upsert_edge's own
    T-blocker fix and its concurrency test below), so a concurrent
    upsert_edge can never land inside that window: it is either fully
    applied before this function's SELECT, or fully after its commit --
    either way, both the new occurrence and the semantic_review survive.
    """
    db_path = tmp_path / "graph.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    db.upsert_node(conn, "claim:paper.tex#abc", "claim")
    db.upsert_node(conn, "experiment:run1", "experiment")
    db.upsert_edge(
        conn, "claim:paper.tex#abc", "experiment:run1", "backed_by", "claims",
        {"metric": "m1", "metric_value": 1.0}, 1.0, status="pending",
    )
    conn.close()

    # paused_apply stands in for the exact gap the bug lived in: it runs
    # strictly after set_edge_semantic_review's internal SELECT and strictly
    # before its internal UPDATE, while its BEGIN IMMEDIATE still holds the
    # write lock. Sleeping there gives ingest_worker, on a genuinely separate
    # OS thread and connection, time to attempt its own write and block on
    # that lock -- proving the two operations cannot interleave.
    original_apply = db._apply_semantic_review

    def paused_apply(existing_evidence_json, semantic_review):
        result = original_apply(existing_evidence_json, semantic_review)
        time.sleep(0.3)
        return result

    def ingest_worker():
        time.sleep(0.05)  # let set_edge_semantic_review enter its transaction first
        ingest_conn = db.connect(db_path)
        try:
            db.upsert_edge(
                ingest_conn, "claim:paper.tex#abc", "experiment:run1", "backed_by", "claims",
                {"metric": "m2", "metric_value": 2.0}, 1.0, status="pending",
            )
        finally:
            ingest_conn.close()

    worker = threading.Thread(target=ingest_worker)
    worker.start()
    try:
        judge_conn = db.connect(db_path)
        try:
            with mock.patch.object(db, "_apply_semantic_review", side_effect=paused_apply):
                db.set_edge_semantic_review(
                    judge_conn, "claim:paper.tex#abc", "experiment:run1", "backed_by", "claims",
                    {"related": False, "reason": "coincidental", "better_match": None},
                )
        finally:
            judge_conn.close()
    finally:
        worker.join(timeout=5)
    assert not worker.is_alive()

    verify_conn = db.connect(db_path)
    try:
        edges = db.query_edges(
            verify_conn, src="claim:paper.tex#abc", dst="experiment:run1", type="backed_by"
        )
        assert len(edges) == 1
        occurrence_metrics = {o["metric"] for o in edges[0]["evidence"]["occurrences"]}
        assert occurrence_metrics == {"m1", "m2"}  # neither occurrence was lost
        assert edges[0]["evidence"]["semantic_review"]["reason"] == "coincidental"
    finally:
        verify_conn.close()


def test_upsert_edge_reingest_preserves_semantic_review_sibling_key(conn):
    # A routine re-ingest by the deterministic extractor (upsert_edge) must
    # never silently erase a semantic-layer annotation living beside
    # `occurrences` in the same evidence blob.
    db.upsert_node(conn, "claim:paper.tex#abc", "claim")
    db.upsert_node(conn, "experiment:run_a", "experiment")
    db.upsert_edge(
        conn, "claim:paper.tex#abc", "experiment:run_a", "backed_by", "claims",
        {"metric": "grad_norm_epoch", "metric_value": 1.5786}, 1.0, status="pending",
    )
    db.set_edge_semantic_review(
        conn, "claim:paper.tex#abc", "experiment:run_a", "backed_by", "claims",
        {"related": False, "reason": "coincidental rounding", "better_match": "quantization"},
    )

    # Re-ingest: same evidence content (idempotent) plus a second, distinct
    # occurrence (e.g. the claim also appears on another line).
    db.upsert_edge(
        conn, "claim:paper.tex#abc", "experiment:run_a", "backed_by", "claims",
        {"metric": "grad_norm_epoch", "metric_value": 1.5786}, 1.0, status="pending",
    )
    db.upsert_edge(
        conn, "claim:paper.tex#abc", "experiment:run_a", "backed_by", "claims",
        {"metric": "grad_norm_epoch", "metric_value": 1.5786, "line": 9}, 1.0, status="pending",
    )

    edge = db.query_edges(conn, src="claim:paper.tex#abc", dst="experiment:run_a")[0]
    assert edge["evidence"]["semantic_review"] == {
        "related": False, "reason": "coincidental rounding", "better_match": "quantization",
    }
    assert edge["evidence"]["occurrences"] == [
        {"metric": "grad_norm_epoch", "metric_value": 1.5786},
        {"metric": "grad_norm_epoch", "metric_value": 1.5786, "line": 9},
    ]


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


def test_attempt_verdict_and_result_survive_reingest_untouched(conn):
    """Constitutional boundary for `attempt` nodes (DESIGN.md section 4): a
    machine re-parse of the attempt timeline may refresh `attrs` (number,
    date, variable description, referenced step, source file/line) but must
    never move `verdict`/`result` -- those are a human's call and live only
    in `human_fields`, written solely through `set_human_fields`.
    """
    db.upsert_node(
        conn, "attempt:map.md#16", "attempt", title="attempt 16",
        attrs={"number": "16", "date": "07-26"},
    )
    db.set_human_fields(
        conn, "attempt:map.md#16",
        {"verdict": "✅ 现行", "result": "t=2.91, placebo p=0.0149"},
    )

    # A later machine re-ingest -- e.g. the map.md row's source line moved,
    # or the referenced step number was corrected -- must not touch verdict/result,
    # even though it rewrites attrs wholesale.
    db.upsert_node(
        conn, "attempt:map.md#16", "attempt", title="attempt 16 (re-ingested)",
        attrs={"number": "16", "date": "07-26", "source_line": 47},
    )

    node = db.get_node(conn, "attempt:map.md#16")
    assert node["human_fields"] == {
        "verdict": "✅ 现行", "result": "t=2.91, placebo p=0.0149",
    }
    assert node["attrs"] == {"number": "16", "date": "07-26", "source_line": 47}


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


# -- upsert_edge atomicity under concurrency (T-blocker regression) --------
#
# ec535b7 turned upsert_edge's single "INSERT ... ON CONFLICT DO UPDATE"
# statement (status CASE evaluated atomically by SQLite) into a two-step
# Python-level SELECT then INSERT/UPDATE, with no explicit transaction
# around the pair. The tests below use real file-backed databases (multiple
# connections, `:memory:` is private per-connection and can't reproduce
# this) to prove the fix: upsert_edge now wraps the read and the write in
# one BEGIN IMMEDIATE transaction, so another connection can never observe
# or act on the gap between them.


def test_upsert_edge_survives_concurrent_human_confirm(tmp_path):
    """Interleaved read -- human confirm -- write.

    Before the fix: upsert_edge's SELECT could read status='pending', then
    a human's set_edge_status(confirmed) on a different connection could
    land, and upsert_edge's subsequent UPDATE would overwrite it back to
    'pending' using the stale value it already read -- silently reopening a
    human-confirmed edge (HANDOFF-SPEC.md section 4: status fields are
    human-write only). The fix makes upsert_edge's SELECT+write one atomic
    transaction, so a concurrent confirm can never land inside that window:
    it either fully precedes the transaction (upsert_edge's own status-
    preservation logic then keeps it) or fully follows it (applied after
    upsert_edge has already committed) -- confirmed survives either way.
    """
    db_path = tmp_path / "graph.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    db.upsert_node(conn, "claim:paper.tex#abc", "claim")
    db.upsert_node(conn, "experiment:run1", "experiment")
    db.upsert_edge(
        conn, "claim:paper.tex#abc", "experiment:run1", "backed_by", "7b-judge",
        {"claim_text": "87.3%", "run_metric": "accuracy=0.873"}, 0.4, status="pending",
    )
    conn.close()

    # paused_merge stands in for the exact gap the bug lived in: it runs
    # strictly after upsert_edge's internal SELECT and strictly before its
    # internal UPDATE, while upsert_edge's BEGIN IMMEDIATE still holds the
    # write lock. Sleeping there (instead of returning immediately) gives
    # confirm_worker, on a genuinely separate OS thread and connection, time
    # to attempt its own write and block on that lock -- proving the two
    # operations cannot interleave.
    original_merge = db._merge_edge_evidence

    def paused_merge(existing_evidence_json, new_evidence):
        result = original_merge(existing_evidence_json, new_evidence)
        time.sleep(0.3)
        return result

    def confirm_worker():
        time.sleep(0.05)  # let the re-ingest below enter its transaction first
        confirm_conn = db.connect(db_path)
        try:
            db.set_edge_status(
                confirm_conn, "claim:paper.tex#abc", "experiment:run1", "backed_by",
                "7b-judge", status="confirmed",
            )
        finally:
            confirm_conn.close()

    worker = threading.Thread(target=confirm_worker)
    worker.start()
    try:
        reingest_conn = db.connect(db_path)
        try:
            with mock.patch.object(db, "_merge_edge_evidence", side_effect=paused_merge):
                # Overnight re-ingest by the same extractor, interleaved
                # (via paused_merge) with the human confirm above.
                db.upsert_edge(
                    reingest_conn, "claim:paper.tex#abc", "experiment:run1", "backed_by",
                    "7b-judge", {"claim_text": "87.3%", "run_metric": "accuracy=0.873 (re-extracted)"},
                    0.4, status="pending",
                )
        finally:
            reingest_conn.close()
    finally:
        worker.join(timeout=5)
    assert not worker.is_alive()

    verify_conn = db.connect(db_path)
    try:
        edges = db.query_edges(
            verify_conn, src="claim:paper.tex#abc", dst="experiment:run1", type="backed_by"
        )
        assert len(edges) == 1
        assert edges[0]["status"] == "confirmed"
    finally:
        verify_conn.close()


def test_upsert_edge_concurrent_first_write_does_not_raise_integrity_error(tmp_path):
    """Two connections upserting the very same brand-new edge at once.

    Before the fix, both connections' SELECTs could see `existing is None`
    and both attempt the INSERT branch; the second hit
    sqlite3.IntegrityError on the UNIQUE(src,dst,type,extractor) constraint
    instead of merging like a normal re-upsert would. BEGIN IMMEDIATE
    serializes the two calls completely, so the second always sees the
    first's row and takes the UPDATE/merge branch instead.
    """
    db_path = tmp_path / "graph.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    db.upsert_node(conn, "commit:aaa", "commit")
    db.upsert_node(conn, "figure:f1.png", "figure")
    conn.close()

    errors: list[BaseException] = []
    start_barrier = threading.Barrier(2)

    def worker(line: int) -> None:
        worker_conn = db.connect(db_path)
        try:
            start_barrier.wait(timeout=5)
            db.upsert_edge(
                worker_conn, "commit:aaa", "figure:f1.png", "generates", "pyfig",
                {"file": "plot.py", "line": line}, 0.9,
            )
        except BaseException as exc:  # noqa: BLE001 -- surfaced via `errors`, not swallowed
            errors.append(exc)
        finally:
            worker_conn.close()

    threads = [threading.Thread(target=worker, args=(line,)) for line in (10, 20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"concurrent upsert_edge raised: {errors!r}"

    verify_conn = db.connect(db_path)
    try:
        edges = db.query_edges(verify_conn, src="commit:aaa", dst="figure:f1.png", type="generates")
        assert len(edges) == 1
        occurrences = edges[0]["evidence"]["occurrences"]
        assert {o["line"] for o in occurrences} == {10, 20}
    finally:
        verify_conn.close()


# --- delete_node / delete_edges_for_node (F2 orphan-cleanup primitives) ----


def test_delete_edges_for_node_removes_edges_on_either_side(conn):
    db.upsert_node(conn, "claim:paper.tex#aaa", "claim")
    db.upsert_node(conn, "experiment:run1", "experiment")
    db.upsert_node(conn, "experiment:run2", "experiment")
    db.upsert_edge(conn, "claim:paper.tex#aaa", "experiment:run1", "backed_by", "claims", {"line": 1}, 1.0, status="pending")
    db.upsert_edge(conn, "claim:paper.tex#aaa", "experiment:run2", "backed_by", "claims", {"line": 1}, 1.0, status="pending")

    removed = db.delete_edges_for_node(conn, "claim:paper.tex#aaa")

    assert removed == 2
    assert db.query_edges(conn, src="claim:paper.tex#aaa") == []


def test_delete_edges_for_node_scoped_to_extractor_leaves_other_extractors_alone(conn):
    # F2's orphan cleanup must only ever touch the edges its own extractor
    # produced -- never another extractor's judgement on the same node.
    db.upsert_node(conn, "claim:paper.tex#aaa", "claim")
    db.upsert_node(conn, "experiment:run1", "experiment")
    db.upsert_edge(conn, "claim:paper.tex#aaa", "experiment:run1", "backed_by", "claims", {"line": 1}, 1.0, status="pending")
    db.upsert_edge(conn, "claim:paper.tex#aaa", "experiment:run1", "backed_by", "7b-judge", {"line": 1}, 0.8, status="pending")

    removed = db.delete_edges_for_node(conn, "claim:paper.tex#aaa", extractor="claims")

    assert removed == 1
    remaining = db.query_edges(conn, src="claim:paper.tex#aaa")
    assert len(remaining) == 1 and remaining[0]["extractor"] == "7b-judge"


def test_delete_node_removes_the_node(conn):
    db.upsert_node(conn, "claim:paper.tex#aaa", "claim")
    db.delete_node(conn, "claim:paper.tex#aaa")
    assert db.get_node(conn, "claim:paper.tex#aaa") is None


def test_delete_node_is_a_noop_for_a_missing_id(conn):
    db.delete_node(conn, "claim:does-not-exist")  # must not raise


def test_delete_node_after_delete_edges_for_node_does_not_violate_foreign_keys(conn):
    # foreign_keys=ON (see connect()) means edges.src/dst reference
    # nodes.id with no CASCADE -- deleting a node with edges still pointing
    # at it must fail; deleting the edges first must then let it succeed.
    db.upsert_node(conn, "claim:paper.tex#aaa", "claim")
    db.upsert_node(conn, "experiment:run1", "experiment")
    db.upsert_edge(conn, "claim:paper.tex#aaa", "experiment:run1", "backed_by", "claims", {"line": 1}, 1.0, status="pending")

    with pytest.raises(sqlite3.IntegrityError):
        db.delete_node(conn, "claim:paper.tex#aaa")

    db.delete_edges_for_node(conn, "claim:paper.tex#aaa")
    db.delete_node(conn, "claim:paper.tex#aaa")
    assert db.get_node(conn, "claim:paper.tex#aaa") is None
