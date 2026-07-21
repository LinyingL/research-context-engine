"""SQLite storage layer for RCE's provenance graph.

Responsibility: own schema migrations and the node/edge upsert contract.
Every other module must read and write the graph exclusively through the
functions in this file -- no other module should run raw SQL against the
nodes/edges tables directly.

Schema-level invariant (HANDOFF-SPEC.md section 4): `nodes.human_fields` is
owned by humans only (confirmation/correction/annotation). Machine ingestion
(`upsert_node`) must never overwrite it -- see the SQL in `upsert_node` and
the enforcement test in tests/test_db.py.

Deterministic node ID conventions (caller's responsibility to construct, not
enforced by this layer -- see HANDOFF-SPEC.md section 4):
    project:<name>              commit:<sha>
    experiment:<run_id>         figure:<repo-relative path>
    section:<tex file>#<slug>   claim:<file>#<hash>
    ref:<bibkey>                contributor:<lowercase email>
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

NODE_TYPES = frozenset(
    {
        "project",
        "experiment",
        "commit",
        "figure",
        "section",
        "claim",
        "reference",
        "contributor",
    }
)

EDGE_TYPES = frozenset(
    {
        "implements",
        "produces",
        "generates",
        "includes",
        "cites",
        "authored_by",
        "backed_by",
        "supports",
    }
)

EDGE_STATUSES = frozenset({"auto", "pending", "confirmed", "rejected"})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the pragmas the schema depends on.

    Foreign keys are off by default in SQLite and WAL is not the default
    journal mode -- both must be set per-connection, so every caller must go
    through this function rather than calling sqlite3.connect directly.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection, migrations_dir: str | Path | None = None) -> list[int]:
    """Apply any migration .sql files not yet recorded in schema_migrations.

    Migration files are named `<version>_description.sql` and are applied in
    ascending version order, each as its own committed step. Returns the list
    of version numbers newly applied (empty if the schema was already
    current -- safe to call on every startup).
    """
    directory = Path(migrations_dir) if migrations_dir else DEFAULT_MIGRATIONS_DIR
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    newly_applied: list[int] = []
    for path in sorted(directory.glob("*.sql")):
        version = int(path.stem.split("_", 1)[0])
        if version in applied:
            continue
        conn.executescript(path.read_text())
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        conn.commit()
        newly_applied.append(version)
    return newly_applied


def upsert_node(
    conn: sqlite3.Connection,
    node_id: str,
    type: str,
    title: str | None = None,
    attrs: dict[str, Any] | None = None,
) -> None:
    """Insert or update a node from a machine extractor (deterministic or 7B).

    Idempotent on `node_id`. Deliberately never writes `human_fields`: the
    UPDATE branch's column list omits it, so a re-ingest cannot clobber human
    corrections/annotations regardless of what `attrs` contains.
    """
    if type not in NODE_TYPES:
        raise ValueError(f"unknown node type: {type!r}")
    attrs_json = json.dumps(attrs or {})
    now = _now()
    conn.execute(
        """
        INSERT INTO nodes (id, type, title, attrs, human_fields, created_at, updated_at)
        VALUES (?, ?, ?, ?, '{}', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            type = excluded.type,
            title = excluded.title,
            attrs = excluded.attrs,
            updated_at = excluded.updated_at
        """,
        (node_id, type, title, attrs_json, now, now),
    )
    conn.commit()


def set_human_fields(conn: sqlite3.Connection, node_id: str, human_fields: dict[str, Any]) -> None:
    """Human-only write path for a node's human_fields (confirm/correct/annotate).

    This is the sole way human_fields is ever written; upsert_node never
    touches it.
    """
    conn.execute(
        "UPDATE nodes SET human_fields = ?, updated_at = ? WHERE id = ?",
        (json.dumps(human_fields), _now(), node_id),
    )
    conn.commit()


def get_node(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        return None
    node = dict(row)
    node["attrs"] = json.loads(node["attrs"])
    node["human_fields"] = json.loads(node["human_fields"])
    return node


def upsert_edge(
    conn: sqlite3.Connection,
    src: str,
    dst: str,
    type: str,
    extractor: str,
    evidence: dict[str, Any],
    confidence: float,
    status: str = "auto",
) -> None:
    """Insert or update an edge, keyed on (src, dst, type, extractor).

    Idempotent: re-running the same extractor over the same pair with a new
    confidence/evidence/status updates the existing row rather than
    duplicating it (see the UNIQUE constraint in migrations/0001_init.sql).
    """
    if type not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {type!r}")
    if status not in EDGE_STATUSES:
        raise ValueError(f"unknown edge status: {status!r}")
    evidence_json = json.dumps(evidence)
    now = _now()
    conn.execute(
        """
        INSERT INTO edges (src, dst, type, extractor, evidence, confidence, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(src, dst, type, extractor) DO UPDATE SET
            evidence = excluded.evidence,
            confidence = excluded.confidence,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (src, dst, type, extractor, evidence_json, confidence, status, now, now),
    )
    conn.commit()


def query_edges(
    conn: sqlite3.Connection,
    src: str | None = None,
    dst: str | None = None,
    type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Filter edges by any combination of src/dst/type/status."""
    clauses = []
    params: list[Any] = []
    for column, value in (("src", src), ("dst", dst), ("type", type), ("status", status)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM edges {where}", params).fetchall()
    results = []
    for row in rows:
        edge = dict(row)
        edge["evidence"] = json.loads(edge["evidence"])
        results.append(edge)
    return results


def pending_edges(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The confirmation queue: edges awaiting human review (status='pending')."""
    return query_edges(conn, status="pending")
