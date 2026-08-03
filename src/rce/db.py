"""SQLite storage layer for RCE's provenance graph.

Responsibility: own schema migrations and the node/edge upsert contract.
Every other module must read and write the graph exclusively through the
functions in this file -- no other module should run raw SQL against the
nodes/edges tables directly.

Schema-level invariant (DESIGN.md section 4): `nodes.human_fields` is
owned by humans only (confirmation/correction/annotation). Machine ingestion
(`upsert_node`) must never overwrite it -- see the SQL in `upsert_node` and
the enforcement test in tests/test_db.py.

Deterministic node ID conventions (caller's responsibility to construct, not
enforced by this layer -- see DESIGN.md section 4):
    project:<name>              commit:<sha>
    experiment:<run_id>         figure:<repo-relative path>
    section:<tex file>#<slug>   claim:<file>#<hash>
    ref:<lowercase bibkey>      contributor:<lowercase email>
    attempt:<source md file, repo-relative path>#<# column value>
    script:<repo-relative path> dataset:<repo-relative path>

Migration 0003 (task W2, data lineage): `script` is any .py/.R/.Rmd source
file rce.ingest.dataflow scanned; `dataset` is a tabular/data file one of its
calls targets. An image-extension target reuses the existing `figure` node
type instead of a third node type -- see rce.ingest.dataflow's module
docstring. `reads`/`writes` are both `script -> dataset` (or `script ->
figure` for an image write).

Constitutional note on `attempt` nodes (DESIGN.md section 4, migration
0002): an attempt's `#`/time/variable-description/referenced-step-number/
source-file-and-line fields are machine-parsed facts about the row and
belong in `attrs`. `verdict` and `result` are a human's judgement call
recorded in prose (e.g. "☠️ 伪"/"✅ 现行") -- they must go in
`human_fields`, set only via `set_human_fields`, never in `attrs`. A
machine re-parse of the same Markdown row can suggest an updated `attrs`
every time; it must never carry an opinion about `verdict`/`result`
alongside it, for the same reason `upsert_node` structurally cannot write
`human_fields` at all (see `upsert_node` below).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# upsert_edge wraps its read-then-write in one BEGIN IMMEDIATE transaction
# (see upsert_edge). Under contention -- another connection already holding
# the write lock -- SQLite's own busy_timeout (the `timeout` argument to
# sqlite3.connect(), 5s by default) transparently retries for a while before
# raising sqlite3.OperationalError("database is locked"). This is a small,
# bounded application-level retry on top of that, for the rare case the
# busy_timeout itself is exceeded (e.g. a slow disk or an unusually long
# competing transaction).
_UPSERT_EDGE_MAX_ATTEMPTS = 5
_UPSERT_EDGE_RETRY_DELAY_SECONDS = 0.05

# Cap on how many distinct evidence occurrences one edge accumulates (T10:
# candidate-1 testbed found a figure \included twice in the same section
# silently lost the first occurrence's evidence to the UNIQUE(src,dst,type,
# extractor) upsert -- see upsert_edge/_merge_edge_evidence). Past this,
# the oldest occurrence is dropped and the drop is logged, never silent.
_MAX_EDGE_EVIDENCE_OCCURRENCES = 20

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
        # Added in migration 0002 (DESIGN.md section 4): one row of a
        # researcher's manually-maintained attempt timeline. See the
        # attrs/human_fields boundary note in this module's docstring above.
        "attempt",
        # Added in migration 0003 (DESIGN.md section 4, task W2): a source
        # file (.py/.R/.Rmd) and a tabular/data file, for data-lineage
        # `reads`/`writes` edges -- see rce.ingest.dataflow.
        "script",
        "dataset",
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
        # Added in migration 0002 (DESIGN.md section 4): `attempt --uses-->
        # commit`, the last commit to touch a script file the attempt
        # depends on -- deterministic, used for verdict-staleness checks.
        "uses",
        # Added in migration 0003 (DESIGN.md section 4, task W2):
        # `script --reads--> dataset` and `script --writes--> dataset` (or
        # `--writes--> figure` for an image target) -- data lineage, written
        # by rce.ingest.dataflow.
        "reads",
        "writes",
    }
)

EDGE_STATUSES = frozenset({"auto", "pending", "confirmed", "rejected"})

# Statuses a machine extractor may write via upsert_edge. 'confirmed' and
# 'rejected' are human-only verdicts -- a machine path must never conjure
# them out of thin air; those two values are only ever set through
# set_edge_status (DESIGN.md section 4: "any edge's confirm/reject ...
# status fields are human-write only").
_MACHINE_EDGE_STATUSES = frozenset({"auto", "pending"})


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


def _split_migration_script(script: str) -> list[str]:
    """Split a migration file into individual statements for transactional apply.

    `sqlite3.Cursor.executescript()` cannot participate in an explicit
    transaction -- it forces an implicit commit before it runs and then
    executes each statement in autocommit mode, so a mid-script failure
    leaves earlier DDL permanently committed with no matching
    schema_migrations row (see the migrate() docstring). Running statements
    one at a time via conn.execute() inside an explicit transaction avoids
    that, which requires splitting the script ourselves first.

    Only handles what our hand-written DDL migrations actually contain:
    statements terminated by ';', comments on their own '--' line (including
    ones with a ';' inside the comment text, e.g. "TEXT; SQLite has...").
    Not a general SQL parser -- migrations must stick to that shape.
    """
    code_lines = (
        line for line in script.splitlines() if not line.strip().startswith("--")
    )
    return [stmt.strip() for stmt in "\n".join(code_lines).split(";") if stmt.strip()]


def migrate(conn: sqlite3.Connection, migrations_dir: str | Path | None = None) -> list[int]:
    """Apply any migration .sql files not yet recorded in schema_migrations.

    Migration files are named `<version>_description.sql` and are applied in
    ascending version order. Each file's statements plus its
    schema_migrations row are applied in a single explicit transaction: if
    any statement fails, the whole file rolls back (SQLite DDL is
    transactional), so a version is never left partially applied with no
    record of it -- a retry after fixing the problem starts from the same
    clean pre-migration state instead of hitting "table already exists".
    Returns the list of version numbers newly applied (empty if the schema
    was already current -- safe to call on every startup).

    Each file's transaction is bracketed with `PRAGMA foreign_keys = OFF` /
    `= ON` (migration 0002: the standard SQLite 12-step table rebuild used to
    widen a `CHECK` constraint needs to `DROP TABLE nodes` while `edges`
    still holds a live foreign key to it by name -- see
    migrations/0002_attempt.sql). The pragma is toggled *outside* the
    transaction on both ends -- "no-op within a transaction" is documented
    SQLite behaviour, so flipping it after `BEGIN` would silently fail to
    take effect. `PRAGMA foreign_key_check` runs just before the
    schema_migrations row is inserted, so a rebuild that left a dangling
    reference fails loudly and rolls back like any other statement failure,
    rather than silently shipping a corrupt foreign key.
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
        statements = _split_migration_script(path.read_text())
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            for statement in statements:
                conn.execute(statement)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"migration {version} left dangling foreign keys: {violations!r}"
                )
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        except Exception:
            conn.rollback()
            # Restored after rollback, not in a `finally` before it: toggling
            # while the (now-aborted) transaction was still open would be
            # the same no-op this whole comment is about.
            conn.execute("PRAGMA foreign_keys = ON")
            raise
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
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


def get_nodes_by_type(conn: sqlite3.Connection, type: str) -> list[dict[str, Any]]:
    """All nodes of a given type, decoded like get_node -- lets an extractor
    match existing nodes by content without raw SQL of its own (e.g.
    rce.ingest.mlflow matching artifact basenames against figure: nodes)."""
    rows = conn.execute("SELECT * FROM nodes WHERE type = ?", (type,)).fetchall()
    nodes = []
    for row in rows:
        node = dict(row)
        node["attrs"] = json.loads(node["attrs"])
        node["human_fields"] = json.loads(node["human_fields"])
        nodes.append(node)
    return nodes


def _merge_edge_evidence(
    existing_evidence_json: str | None,
    new_evidence: dict[str, Any],
    edge_attrs: dict[str, Any] | None = None,
) -> str:
    """Fold `new_evidence` into an edge's evidence, returning the encoded JSON.

    Every edge's evidence is stored as `{"occurrences": [dict, ...]}` --
    even a first-ever occurrence uses this shape, so every reader has one
    structure to handle (T10: candidate-1 testbed regression -- a figure
    \\included twice in the same section used to lose the first occurrence's
    evidence outright, because the old ON CONFLICT branch overwrote
    `evidence` wholesale). A pre-T10 row stored evidence as a bare dict with
    no wrapper; this is a read-time migration only (schema untouched, Occam
    rule 4) -- such a row is treated as a single legacy occurrence rather
    than requiring a schema/data migration.

    Dedupes by content: an evidence dict equal to one already present is not
    appended again, so a repeated idempotent re-ingest of the same line does
    not grow the list. Caps at `_MAX_EDGE_EVIDENCE_OCCURRENCES`, dropping the
    oldest occurrences and logging a warning when the cap is exceeded --
    never silently, and never unbounded.

    Sibling keys other than "occurrences" -- `semantic_review` (written by
    `set_edge_semantic_review` for the S2 semantic judge) and whatever
    `edge_attrs` below folds in -- are carried forward unchanged whenever
    present on the existing row and not touched by this call. This function
    only ever *appends* to `occurrences`; every other top-level key is
    passed through as-is unless `edge_attrs` explicitly overwrites it, so a
    routine re-ingest (the only caller of upsert_edge, hence of this
    function) can never silently erase a semantic-layer annotation living
    beside it. This is why the semantic layer's own writes go through
    `set_edge_semantic_review` instead of reusing this function directly --
    that function updates only the `semantic_review` key and leaves
    `occurrences` (and any other sibling) exactly as this function last left
    them.

    `edge_attrs` (DESIGN.md section 4, T-blocker fix 2026-07-27): sibling
    keys describing the whole edge/claim rather than the one occurrence
    just written -- e.g. `candidate_count`, the number of (experiment,
    metric) pairs a claim matches in total, which is a global fact about
    the claim, not about any single matched pair. These are merged with a
    plain dict `.update()` -- overwritten wholesale to their latest value on
    every call, never accumulated -- because unlike `occurrences` a global
    count has no history worth keeping, only a current value. This is also
    why such a fact must never be folded into `new_evidence` (the
    occurrence) instead: `new_evidence not in occurrences` dedupes by whole-
    dict equality, so a field that changes on every unrelated re-ingest
    (candidate_count grows every time an unrelated new experiment starts
    matching the same claim) would make an otherwise-identical occurrence
    look "new" every time and mint a fresh entry forever -- exactly the bug
    `rce.ingest.claims` used to have before `candidate_count` moved here.
    """
    extra: dict[str, Any] = {}
    if existing_evidence_json is None:
        occurrences: list[Any] = []
    else:
        existing = json.loads(existing_evidence_json)
        if isinstance(existing, dict) and isinstance(existing.get("occurrences"), list):
            occurrences = list(existing["occurrences"])
            extra = {k: v for k, v in existing.items() if k != "occurrences"}
        else:
            occurrences = [existing]  # legacy bare-evidence row, pre-T10

    if new_evidence not in occurrences:
        occurrences.append(new_evidence)

    if len(occurrences) > _MAX_EDGE_EVIDENCE_OCCURRENCES:
        dropped = len(occurrences) - _MAX_EDGE_EVIDENCE_OCCURRENCES
        occurrences = occurrences[dropped:]
        logger.warning(
            "edge evidence occurrences exceeded cap of %d; dropped %d oldest entr%s",
            _MAX_EDGE_EVIDENCE_OCCURRENCES, dropped, "y" if dropped == 1 else "ies",
        )

    if edge_attrs:
        extra.update(edge_attrs)  # overwrite to latest value, never accumulate

    return json.dumps({"occurrences": occurrences, **extra})


def upsert_edge(
    conn: sqlite3.Connection,
    src: str,
    dst: str,
    type: str,
    extractor: str,
    evidence: dict[str, Any],
    confidence: float,
    status: str = "auto",
    *,
    edge_attrs: dict[str, Any] | None = None,
) -> None:
    """Insert or update an edge, keyed on (src, dst, type, extractor).

    Idempotent: re-running the same extractor over the same pair updates the
    existing row rather than duplicating it (see the UNIQUE constraint in
    migrations/0001_init.sql) -- but unlike confidence/status, `evidence` is
    never overwritten wholesale. It accumulates as
    `{"occurrences": [...]}`; see `_merge_edge_evidence` for the merge/dedup/
    cap rules (T10).

    `edge_attrs` (DESIGN.md section 4, T-blocker fix 2026-07-27): optional
    sibling keys stored alongside `occurrences` in the same evidence dict --
    e.g. `candidate_count` on a `backed_by` edge, a global fact about the
    whole claim/edge, not about the one occurrence this call is writing.
    Overwritten wholesale to the latest value on every call, never
    accumulated (unlike `occurrences`) -- see `_merge_edge_evidence`. Keeping
    such facts out of `evidence` (the occurrence argument) matters: that
    dict's identity is what dedup compares by, so a value that changes on
    every unrelated re-ingest would make an otherwise-unchanged occurrence
    look "new" forever.

    `status` here is restricted to _MACHINE_EDGE_STATUSES ('auto'/'pending')
    -- a machine path must never conjure a 'confirmed' or 'rejected' verdict
    out of thin air, so passing either raises ValueError; use
    set_edge_status for those. This mirrors the human_fields protection on
    nodes (set_human_fields is the only path that writes it).

    Even with that restriction, an existing row's status only moves to the
    incoming value when its *current* status is still machine-owned ('auto'
    or 'pending'); once a human has moved it to 'confirmed' or 'rejected' via
    set_edge_status, a routine re-ingest by the same extractor must not
    silently reopen or reset it. evidence/confidence are machine-owned and
    keep updating regardless -- see
    tests/test_db.py::test_reingest_never_overwrites_confirmed_edge_status.

    Atomicity (T-blocker fix): the SELECT above and the following INSERT/
    UPDATE run inside one `BEGIN IMMEDIATE` transaction, not as two
    autocommitted statements. `BEGIN IMMEDIATE` grabs SQLite's write lock up
    front, before the SELECT even runs, so no other connection can write
    this exact (src, dst, type, extractor) row between our read and our
    write. Without this, the two-step "SELECT here, decide, write there"
    was racy in two concrete ways: (1) two connections upserting the same
    brand-new edge concurrently could both see `existing is None` and both
    attempt the INSERT -- the loser crashed on the
    UNIQUE(src,dst,type,extractor) constraint instead of merging; (2) a
    human's set_edge_status() landing on a different connection between our
    SELECT and our UPDATE was silently clobbered by this call's own write,
    computed from the by-then-stale `existing["status"]` it had already
    read -- reopening a confirmed/rejected edge is exactly what the
    status-preservation logic above exists to prevent. See
    tests/test_db.py::test_upsert_edge_survives_concurrent_human_confirm and
    ::test_upsert_edge_concurrent_first_write_does_not_raise_integrity_error.

    Every edge must carry non-empty evidence -- "no edge without evidence"
    is a hard invariant (DESIGN.md section 4/2). A placeholder like
    `{}` is not evidence, so it is rejected here (and by the CHECK
    constraint in migrations/0001_init.sql as a second, DB-level guard).

    `confidence` must be within [0.0, 1.0] -- enforced in Python only (no
    migration/CHECK constraint added: Occam rule 4, a range check needs no
    schema change to enforce).
    """
    if type not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {type!r}")
    if status not in _MACHINE_EDGE_STATUSES:
        raise ValueError(
            f"upsert_edge only accepts machine-owned statuses {sorted(_MACHINE_EDGE_STATUSES)!r}, "
            f"got {status!r}; use set_edge_status for confirmed/rejected"
        )
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be within [0.0, 1.0], got {confidence!r}")
    if not evidence:
        raise ValueError("edge requires non-empty evidence")
    now = _now()

    last_error: sqlite3.OperationalError | None = None
    for attempt in range(_UPSERT_EDGE_MAX_ATTEMPTS):
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # Could not even acquire the write lock yet -- another
            # connection's transaction is in the way; back off and retry.
            last_error = exc
            time.sleep(_UPSERT_EDGE_RETRY_DELAY_SECONDS * (attempt + 1))
            continue
        try:
            existing = conn.execute(
                "SELECT evidence, status FROM edges WHERE src = ? AND dst = ? AND type = ? AND extractor = ?",
                (src, dst, type, extractor),
            ).fetchone()
            # Called with exactly 2 positional args when edge_attrs is falsy
            # (the overwhelmingly common case) rather than always passing a
            # 3rd `None` -- tests/test_db.py's concurrency tests mock this
            # exact function with a 2-arg stand-in
            # (test_upsert_edge_survives_concurrent_human_confirm's
            # paused_merge), so preserving the original call shape whenever
            # edge_attrs isn't in play keeps that mock working unchanged.
            if existing is None:
                if edge_attrs:
                    evidence_json = _merge_edge_evidence(None, evidence, edge_attrs)
                else:
                    evidence_json = _merge_edge_evidence(None, evidence)
                conn.execute(
                    """
                    INSERT INTO edges (src, dst, type, extractor, evidence, confidence, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (src, dst, type, extractor, evidence_json, confidence, status, now, now),
                )
            else:
                if edge_attrs:
                    evidence_json = _merge_edge_evidence(existing["evidence"], evidence, edge_attrs)
                else:
                    evidence_json = _merge_edge_evidence(existing["evidence"], evidence)
                effective_status = existing["status"] if existing["status"] in ("confirmed", "rejected") else status
                conn.execute(
                    """
                    UPDATE edges SET evidence = ?, confidence = ?, status = ?, updated_at = ?
                    WHERE src = ? AND dst = ? AND type = ? AND extractor = ?
                    """,
                    (evidence_json, confidence, effective_status, now, src, dst, type, extractor),
                )
        except sqlite3.OperationalError as exc:
            conn.rollback()
            last_error = exc
            time.sleep(_UPSERT_EDGE_RETRY_DELAY_SECONDS * (attempt + 1))
            continue
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            return
    assert last_error is not None
    raise last_error


def set_edge_status(
    conn: sqlite3.Connection,
    src: str,
    dst: str,
    type: str,
    extractor: str,
    status: str,
) -> None:
    """Human-only write path for an edge's status (confirm/reject/correct).

    Symmetric with set_human_fields on the node side: this is the sole path
    allowed to move a status to or from 'confirmed'/'rejected'. Unlike
    upsert_edge it accepts any of the four EDGE_STATUSES, including moving
    an edge back out of 'confirmed'/'rejected' -- a human is allowed to
    correct their own earlier confirm/reject mistake; a machine re-ingest
    (upsert_edge) is not (see upsert_edge's status restriction above).

    No-op if the (src, dst, type, extractor) row does not exist, matching
    set_human_fields's behavior for an unknown node_id.
    """
    if status not in EDGE_STATUSES:
        raise ValueError(f"unknown edge status: {status!r}")
    conn.execute(
        """
        UPDATE edges SET status = ?, updated_at = ?
        WHERE src = ? AND dst = ? AND type = ? AND extractor = ?
        """,
        (status, _now(), src, dst, type, extractor),
    )
    conn.commit()


def _apply_semantic_review(existing_evidence_json: str, semantic_review: dict[str, Any]) -> str:
    """Fold `semantic_review` into an edge's evidence as a sibling key next to
    `occurrences`, returning the encoded JSON.

    Split out from `set_edge_semantic_review` for the same reason
    `_merge_edge_evidence` is split out of `upsert_edge`: it gives a
    concurrency test a precise seam to patch (simulate the SELECT-to-UPDATE
    window taking a while) without reaching into SQL string internals -- see
    tests/test_db.py::test_set_edge_semantic_review_survives_concurrent_upsert_edge.

    Carries forward any existing sibling key other than `semantic_review`
    unchanged (currently only `occurrences`), matching `_merge_edge_evidence`'s
    own passthrough so a judge re-run and a routine re-ingest can never
    clobber each other's half of the evidence.
    """
    existing = json.loads(existing_evidence_json)
    if isinstance(existing, dict) and isinstance(existing.get("occurrences"), list):
        evidence = {k: v for k, v in existing.items()}
    else:
        evidence = {"occurrences": [existing] if existing else []}  # legacy bare-evidence row
    evidence["semantic_review"] = semantic_review
    return json.dumps(evidence)


def set_edge_semantic_review(
    conn: sqlite3.Connection,
    src: str,
    dst: str,
    type: str,
    extractor: str,
    semantic_review: dict[str, Any],
) -> None:
    """Machine-annotation write path for the optional semantic layer (S2,
    DESIGN.md section 7): attaches `semantic_review` as a sibling key next
    to `occurrences` inside an edge's evidence JSON.

    Constitutional note (DESIGN.md section 2/4, "humans own judgement"): this
    function is the ONLY way `rce.semantic.judge` is allowed to record a
    model's opinion. It updates `evidence` alone -- it never touches
    `status` or `confidence`, and it does not call `upsert_edge` or
    `set_edge_status`. A model's output is therefore structurally
    annotation, never a verdict: an edge this function is called on stays
    exactly whatever status it already had (in practice always 'pending',
    since that is all the judge ever reviews) -- confirming or rejecting an
    edge remains solely `set_edge_status`'s job, and nothing here can reach
    it even by mistake.

    Overwrites any previous `semantic_review` on this edge (a re-run
    judgement supersedes the old one) but never touches `occurrences` --
    read via `_apply_semantic_review`'s "existing dict, minus
    semantic_review, carried forward" logic, so a judge re-run and a
    routine re-ingest can never clobber each other's half of the evidence.

    Atomicity (Opus-review blocker fix, same bug class as `upsert_edge`'s
    T-blocker fix above): the SELECT and the following UPDATE run inside one
    `BEGIN IMMEDIATE` transaction, with the same bounded retry loop
    `upsert_edge` uses (`_UPSERT_EDGE_MAX_ATTEMPTS`). Before this fix, the
    SELECT and UPDATE were two separate autocommitted statements with no
    transaction around them: a concurrent `upsert_edge` call (e.g. `rce
    ingest` running in another process while a judge run stalls on model
    latency for this edge) could commit a brand-new occurrence in the gap
    between them, and this function's UPDATE would then overwrite `evidence`
    with its own stale in-memory copy, silently destroying that occurrence.
    `BEGIN IMMEDIATE` grabs the write lock before the SELECT even runs, so no
    other connection can write this exact (src, dst, type, extractor) row
    inside that window -- a concurrent upsert_edge either fully precedes
    this transaction or fully follows it. See
    tests/test_db.py::test_set_edge_semantic_review_survives_concurrent_upsert_edge.

    No-op if the (src, dst, type, extractor) row does not exist, matching
    `set_human_fields`/`set_edge_status`'s behavior for an unknown target.
    """
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(_UPSERT_EDGE_MAX_ATTEMPTS):
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            last_error = exc
            time.sleep(_UPSERT_EDGE_RETRY_DELAY_SECONDS * (attempt + 1))
            continue
        try:
            row = conn.execute(
                "SELECT evidence FROM edges WHERE src = ? AND dst = ? AND type = ? AND extractor = ?",
                (src, dst, type, extractor),
            ).fetchone()
            if row is None:
                conn.rollback()
                return
            evidence_json = _apply_semantic_review(row["evidence"], semantic_review)
            conn.execute(
                "UPDATE edges SET evidence = ?, updated_at = ? WHERE src = ? AND dst = ? AND type = ? AND extractor = ?",
                (evidence_json, _now(), src, dst, type, extractor),
            )
        except sqlite3.OperationalError as exc:
            conn.rollback()
            last_error = exc
            time.sleep(_UPSERT_EDGE_RETRY_DELAY_SECONDS * (attempt + 1))
            continue
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            return
    assert last_error is not None
    raise last_error


def delete_edges_for_node(
    conn: sqlite3.Connection, node_id: str, *, extractor: str | None = None
) -> int:
    """Delete edges with `node_id` as src OR dst, optionally scoped to one
    `extractor`. Returns the number of rows deleted.

    Must run before `delete_node` for this node when foreign_keys=ON (the
    default, see `connect`): edges.src/dst reference nodes.id with no ON
    DELETE CASCADE, so a node with edges still pointing at it cannot be
    deleted directly. Extractor-scoped orphan cleanup (F2, see
    rce.ingest.claims) is the first caller -- it must delete only the edges
    its own extractor produced, never another extractor's judgement on the
    same node.
    """
    clauses = ["(src = ? OR dst = ?)"]
    params: list[Any] = [node_id, node_id]
    if extractor is not None:
        clauses.append("extractor = ?")
        params.append(extractor)
    cursor = conn.execute(f"DELETE FROM edges WHERE {' AND '.join(clauses)}", params)
    conn.commit()
    return cursor.rowcount


def delete_node(conn: sqlite3.Connection, node_id: str) -> None:
    """Hard-delete a node by id. No-op if it does not exist.

    This is a machine-side orphan-cleanup primitive (F2, see
    rce.ingest.claims), not a general-purpose deletion API and not a human
    write path -- it carries none of set_human_fields/set_edge_status's
    protections. Callers are responsible for confirming first that no
    human-owned state (a confirmed/rejected edge) depends on this node; the
    FK constraint only guarantees no edges reference it (see
    delete_edges_for_node), not that deleting it was the right call.
    """
    conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
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
