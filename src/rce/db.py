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

# Extractor names that belong to a human-authored source, never to a
# machine extractor (DESIGN.md section 8.5): `mapping` edges are derived
# from `.rce/mappings.toml`, a file the researcher writes by hand (or the
# canvas writes on their behalf), and "no machine extractor may write
# `extractor = "mapping"`". Enforced at this one boundary rather than by
# convention: `upsert_edge` refuses these names unless the caller passes
# `human_source=True` -- which only `rce.ingest.mappings` does -- and
# `delete_edges_for_node` leaves them alone unless asked for by name, so a
# machine orphan cleanup that clears "every edge on this node" can never
# take a human assertion down with it. A keyword flag, not a capability
# token: the threat is an accidental copy-paste of the extractor name into
# a machine path, which a loud ValueError catches, not a hostile caller.
HUMAN_EXTRACTORS = frozenset({"mapping"})


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


def canonical_basis(basis: dict[str, Any] | None) -> str | None:
    """The comparable form of a scan basis (DESIGN.md 9.6): JSON with sorted
    keys and no insignificant whitespace, non-ASCII kept as written (a
    Chinese sentence stays readable in the index). Two bases are equal iff
    their canonical forms are equal strings. None stays None ("no basis
    recorded"), which is different from `{}` ("the link's identity alone")."""
    if basis is None:
        return None
    return json.dumps(basis, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def merge_basis(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Fold a second production of the same link IN THE SAME SCAN into its
    basis: lists are unioned and sorted (two calls named `read_csv` and
    `open` both produced the read), dicts merge key by key (claims: one
    more matching metric of that experiment), any other value takes the
    newer one. Never used across scans -- a new scan replaces the basis."""
    merged = dict(old)
    for key, value in new.items():
        prior = merged.get(key)
        if isinstance(prior, list) and isinstance(value, list):
            merged[key] = sorted(set(prior) | set(value))
        elif isinstance(prior, dict) and isinstance(value, dict):
            merged[key] = merge_basis(prior, value)
        else:
            merged[key] = value
    return merged


def _has_scan_stamps(conn: sqlite3.Connection) -> bool:
    """Whether this index has migration 0004's scan columns. An index is
    migrated on open (`rce.project`), so this is False only for a
    connection a caller migrated partially on purpose (the migration
    tests); every write path then behaves exactly as before 0004."""
    try:
        conn.execute("SELECT scan_seen FROM edges LIMIT 0")
    except sqlite3.OperationalError:
        return False
    return True


def _has_machine_status(conn: sqlite3.Connection) -> bool:
    """Whether this index has migration 0005 (`edges.machine_status`, the
    judgment tables) -- False only for a partially migrated test index."""
    try:
        conn.execute("SELECT machine_status FROM edges LIMIT 0")
    except sqlite3.OperationalError:
        return False
    return True


def _scan_columns(
    existing: sqlite3.Row | None,
    scan_id: int,
    scan_source: str,
    basis: dict[str, Any],
    prior_scan: int | None,
) -> tuple[str, int, str, int]:
    """(scan_basis, scan_seen, scan_source, scan_appeared) for a link
    produced in scan `scan_id` (`upsert_edge`'s scan keywords). A second
    production in the same scan merges into the basis; a production in a
    new scan replaces it. `scan_appeared` restarts at this scan when the
    link was not produced by the previous scan that observed its source
    (`prior_scan`) -- a link that comes back, or a brand-new one -- and is
    kept while the productions are unbroken."""
    if existing is not None and existing["scan_seen"] == scan_id and existing["scan_basis"] is not None:
        merged = merge_basis(json.loads(existing["scan_basis"]), basis)
        return (
            canonical_basis(merged) or "{}", scan_id,
            existing["scan_source"] or scan_source, existing["scan_appeared"] or scan_id,
        )
    continuous = (
        existing is not None
        and existing["scan_seen"] is not None
        and prior_scan is not None
        and existing["scan_seen"] >= prior_scan
        and existing["scan_appeared"] is not None
    )
    appeared = existing["scan_appeared"] if continuous else scan_id
    return canonical_basis(basis) or "{}", scan_id, scan_source, appeared


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
    human_source: bool = False,
    scan_id: int | None = None,
    scan_source: str | None = None,
    basis: dict[str, Any] | None = None,
    prior_scan: int | None = None,
) -> None:
    """Insert or update an edge, keyed on (src, dst, type, extractor).

    `extractor` must not be one of `HUMAN_EXTRACTORS` unless
    `human_source=True` (DESIGN.md section 8.5; see `HUMAN_EXTRACTORS`).
    Even then `status` stays machine-restricted: the human-sourced ingest
    still reaches 'confirmed' only through `set_edge_status`.

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

    Scan stamps (DESIGN.md 9.6, migration 0004): a caller inside a scan that
    actually read the link's source passes `scan_id`, `scan_source` and
    `basis` (`rce.ingest.scan.Scan.mark` builds them, `prior_scan`
    included). They record what THIS scan saw -- `scan_basis`,
    `scan_seen`, `scan_source`, `scan_appeared`, and `scan_lost` cleared --
    beside the evidence, never inside it: the accumulated `occurrences`
    above are unchanged by any of this. Without `scan_id` the stamps are
    left exactly as they were (a source the scan could not read speaks
    for nothing). A brand-new row also drops any `removed_edges` stamp left
    by an earlier orphan cleanup of the same link.
    """
    if scan_id is not None and (scan_source is None or basis is None):
        raise ValueError("a scan stamp needs scan_source and basis alongside scan_id")
    if type not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {type!r}")
    if extractor in HUMAN_EXTRACTORS and not human_source:
        raise ValueError(
            f"extractor {extractor!r} is reserved for a human-authored source "
            "(DESIGN.md section 8.5) -- a machine extractor must not write it"
        )
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
            stamped = _has_scan_stamps(conn)
            columns = (
                "evidence, status, scan_basis, scan_seen, scan_source, scan_appeared"
                if stamped else "evidence, status"
            )
            existing = conn.execute(
                f"SELECT {columns} FROM edges WHERE src = ? AND dst = ? AND type = ? AND extractor = ?",
                (src, dst, type, extractor),
            ).fetchone()
            if scan_id is not None and not stamped:
                raise ValueError("this index predates migration 0004; it cannot record scan stamps")
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
            if existing is None and stamped:
                conn.execute(
                    "DELETE FROM removed_edges WHERE src = ? AND dst = ? AND type = ? AND extractor = ?",
                    (src, dst, type, extractor),
                )
            if extractor not in HUMAN_EXTRACTORS and _has_machine_status(conn):
                # Migration 0005: what the machine says, kept apart from
                # the status a human verdict may have set -- the applier
                # falls back to it when a judgment is not applied.
                conn.execute(
                    "UPDATE edges SET machine_status = ? WHERE src = ? AND dst = ? AND type = ? AND extractor = ?",
                    (status, src, dst, type, extractor),
                )
            if scan_id is not None:
                assert scan_source is not None and basis is not None
                stamps = _scan_columns(existing, scan_id, scan_source, basis, prior_scan)
                conn.execute(
                    """
                    UPDATE edges SET scan_basis = ?, scan_seen = ?, scan_source = ?,
                        scan_appeared = ?, scan_lost = NULL
                    WHERE src = ? AND dst = ? AND type = ? AND extractor = ?
                    """,
                    (*stamps, src, dst, type, extractor),
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

    Since V5 (DESIGN.md 9.1) no surface calls this for a verdict on a
    machine link: a judgment is appended to `.rce/judgements.toml` and the
    index's status is DERIVED from it by `rce.records.judgements` (through
    `write_judgement_state`). What remains is the mapping ingest, whose
    hand-drawn links have their one authority in `.rce/mappings.toml`.

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


# Sibling key (next to `occurrences`) in a rejected edge's evidence: the
# status it had at the moment a human rejected it, so undoing the reject
# restores exactly that -- see `reject_edge_remembering`. A sibling key, not
# an occurrence, so `_merge_edge_evidence` carries it through any re-ingest.
STATUS_BEFORE_REJECT_KEY = "status_before_reject"
# What a restore falls back to when no prior status was recorded (an edge
# rejected by `rce reject`, which keeps no memory): the status every
# machine extractor writes.
DEFAULT_RESTORED_STATUS = "auto"


def _edge_status_txn(
    conn: sqlite3.Connection,
    src: str,
    dst: str,
    type: str,
    extractor: str,
    decide: Any,
) -> str | None:
    """Run `decide(status, evidence_dict) -> (new_status, new_evidence) |
    None` for one edge inside a single `BEGIN IMMEDIATE` transaction (same
    shape and retry loop as `set_edge_semantic_review`), so the read of the
    current status/evidence and the write of the new ones cannot be split
    by a concurrent re-ingest's `upsert_edge`. Returns the new status, or
    None when the edge does not exist or `decide` declined."""
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
                "SELECT status, evidence FROM edges WHERE src = ? AND dst = ? AND type = ? AND extractor = ?",
                (src, dst, type, extractor),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            evidence = json.loads(row["evidence"]) if row["evidence"] else {}
            if not isinstance(evidence, dict) or not isinstance(evidence.get("occurrences"), list):
                evidence = {"occurrences": [evidence] if evidence else []}  # legacy bare row
            decision = decide(row["status"], evidence)
            if decision is None:
                conn.rollback()
                return None
            new_status, new_evidence = decision
            if new_status not in EDGE_STATUSES:
                raise ValueError(f"unknown edge status: {new_status!r}")
            conn.execute(
                "UPDATE edges SET status = ?, evidence = ?, updated_at = ? "
                "WHERE src = ? AND dst = ? AND type = ? AND extractor = ?",
                (new_status, json.dumps(new_evidence), _now(), src, dst, type, extractor),
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
            return new_status
    assert last_error is not None
    raise last_error


def reject_edge_remembering(
    conn: sqlite3.Connection, src: str, dst: str, type: str, extractor: str
) -> str | None:
    """Pre-V5 (kept for indexes and tests of that era; since V5 no surface
    calls it -- a reject is a ledger entry, its undo an `undone` entry).
    Human-only: mark an edge `rejected`, recording the status it had
    (`STATUS_BEFORE_REJECT_KEY`) so `restore_rejected_edge` can undo the
    reject exactly -- the canvas's 「标记为错误提取」 and its 「撤销」.

    Why the memory (adversarial review of the V4 work): restore used to put
    every link back at "auto". A machine link the researcher had confirmed
    (`rce confirm`, MCP `confirm_edge`) lost that confirmation to a mis-
    click plus undo -- a human judgement silently replaced by a machine
    status, which Section 4 forbids and no re-ingest would ever repair.

    Idempotent: rejecting an already-rejected edge changes nothing and
    keeps the original memory (a second click must not record "rejected"
    as the prior status). Same human-only standing as `set_edge_status`;
    returns the new status, or None for an unknown edge."""

    def decide(status: str, evidence: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        if status == "rejected":
            return status, evidence
        return "rejected", {**evidence, STATUS_BEFORE_REJECT_KEY: status}

    return _edge_status_txn(conn, src, dst, type, extractor, decide)


def restore_rejected_edge(
    conn: sqlite3.Connection, src: str, dst: str, type: str, extractor: str
) -> str | None:
    """Pre-V5, like `reject_edge_remembering`. Human-only undo of
    `reject_edge_remembering`: put a rejected edge
    back at the status recorded when it was rejected (`confirmed` stays
    confirmed), or `DEFAULT_RESTORED_STATUS` when none was recorded, and
    drop the memory. Returns the restored status; None when the edge does
    not exist or is not currently rejected (the caller's 409)."""

    def decide(status: str, evidence: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        if status != "rejected":
            return None
        remaining = dict(evidence)
        prior = remaining.pop(STATUS_BEFORE_REJECT_KEY, DEFAULT_RESTORED_STATUS)
        if prior not in EDGE_STATUSES or prior == "rejected":
            prior = DEFAULT_RESTORED_STATUS
        return prior, remaining

    return _edge_status_txn(conn, src, dst, type, extractor, decide)


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


def _remember_removed_edges(conn: sqlite3.Connection, where: str, params: list[Any]) -> None:
    """Keep the scan stamps of edges about to be deleted (migration 0004,
    `removed_edges`): an orphan cleanup now removes a link a human judged
    (DESIGN.md 9.1), and the review of that judgment (9.6) still needs to
    say when the link stopped being produced and on what basis it last
    was. Only links some scan stamped are remembered; a no-op on an index
    that predates 0004."""
    if not _has_scan_stamps(conn):
        return
    conn.execute(
        f"""
        INSERT OR REPLACE INTO removed_edges
            (src, dst, type, extractor, scan_basis, scan_seen, scan_source,
             scan_appeared, scan_lost, removed_at)
        SELECT src, dst, type, extractor, scan_basis, scan_seen, scan_source,
             scan_appeared, scan_lost, ?
        FROM edges WHERE ({where}) AND scan_seen IS NOT NULL
        """,
        [_now(), *params],
    )


def holds_unrecorded_judgment(conn: sqlite3.Connection, node_id: str, *, extractor: str | None = None) -> bool:
    """Whether deleting `node_id` would destroy a judgment that exists
    nowhere else: this index predates the judgment ledger (no migration
    0005 -- a pre-V5 index, kept read-only for human records until `rce
    migrate` moves its judgments into the record, 9.5) and an edge touching
    the node (of `extractor`, if given) is confirmed or rejected. Orphan
    cleanup keeps such a node, as pre-V5 code did; in a V5 index the ledger
    keeps the judgment and the orphan goes (9.1)."""
    if _has_machine_status(conn):
        return False
    clause = " AND extractor = ?" if extractor is not None else ""
    params: list[Any] = [node_id, node_id] + ([extractor] if extractor is not None else [])
    row = conn.execute(
        f"SELECT 1 FROM edges WHERE (src = ? OR dst = ?) AND status IN ('confirmed', 'rejected'){clause} LIMIT 1",
        params,
    ).fetchone()
    return row is not None


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

    With `extractor=None`, edges from a `HUMAN_EXTRACTORS` source are NOT
    deleted (DESIGN.md section 8.5: "no machine re-ingest may remove ...
    a `mapping` edge"). They go only when asked for by name, which only
    the source's own resync does. A node still carrying such an edge then
    cannot be `delete_node`d (the foreign key refuses) -- a loud failure
    instead of a silently erased human assertion.
    """
    clauses = ["(src = ? OR dst = ?)"]
    params: list[Any] = [node_id, node_id]
    if extractor is not None:
        clauses.append("extractor = ?")
        params.append(extractor)
    else:
        placeholders = ", ".join("?" for _ in HUMAN_EXTRACTORS)
        clauses.append(f"extractor NOT IN ({placeholders})")
        params.extend(sorted(HUMAN_EXTRACTORS))
    where = " AND ".join(clauses)
    _remember_removed_edges(conn, where, params)
    cursor = conn.execute(f"DELETE FROM edges WHERE {where}", params)
    conn.commit()
    return cursor.rowcount


def delete_edge(conn: sqlite3.Connection, src: str, dst: str, type: str, extractor: str) -> int:
    """Delete exactly one edge by its identity (src, dst, type, extractor);
    returns the number of rows deleted (0 or 1). The resync primitive for
    a human-authored source (`rce.ingest.mappings`): its entry left the
    file, so its edge goes -- and only that edge, never another
    extractor's judgement on the same pair."""
    where = "src = ? AND dst = ? AND type = ? AND extractor = ?"
    _remember_removed_edges(conn, where, [src, dst, type, extractor])
    cursor = conn.execute(f"DELETE FROM edges WHERE {where}", (src, dst, type, extractor))
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
    """The confirmation queue: edges awaiting human review (status='pending').

    A link whose old judgment is under review or in conflict (DESIGN.md
    9.6, migration 0005's `judgement_state`) is shown at the machine's
    status -- often 'pending' -- but it is counted in 待复核, never in
    待确认: it is not a new candidate, it is an old judgment waiting."""
    edges = query_edges(conn, status="pending")
    waiting = waiting_judgement_keys(conn)
    if not waiting:
        return edges
    return [e for e in edges if (e["src"], e["dst"], e["type"], e["extractor"]) not in waiting]


# -- the judgment ledger's derived state (DESIGN.md 9.3/9.6, migration 0005) ------
#
# The SQL half of `rce.records.judgements`: that module decides, from the
# ledger and the scan stamps, what the index's human state is; these
# functions only store and fetch it.

JUDGEMENT_OUTCOMES = frozenset({"applied", "review", "conflict", "held", "not_in_index"})
#: Outcomes counted in 待复核 (the researcher has to act).
WAITING_OUTCOMES = frozenset({"review", "conflict"})
_JUDGEMENT_STATE_JSON = ("basis", "basis_now", "candidates", "detail")


def waiting_judgement_keys(conn: sqlite3.Connection) -> set[tuple[str, str, str, str]]:
    """Keys of links under review or in conflict ({} on an index before 0005)."""
    if not _has_machine_status(conn):
        return set()
    rows = conn.execute(
        "SELECT src, dst, type, extractor FROM judgement_state WHERE outcome IN ('review', 'conflict')"
    ).fetchall()
    return {(r["src"], r["dst"], r["type"], r["extractor"]) for r in rows}


def judgement_states(conn: sqlite3.Connection) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Every row of `judgement_state`, keyed by link, JSON columns decoded."""
    if not _has_machine_status(conn):
        return {}
    found = {}
    for row in conn.execute("SELECT * FROM judgement_state ORDER BY src, dst, type, extractor").fetchall():
        item = dict(row)
        for column in _JUDGEMENT_STATE_JSON:
            item[column] = json.loads(item[column]) if item[column] is not None else None
        found[(item["src"], item["dst"], item["type"], item["extractor"])] = item
    return found


def edge_statuses(conn: sqlite3.Connection) -> dict[tuple[str, str, str, str], tuple[str, str | None]]:
    """{key: (status, machine_status)} for every non-mapping edge."""
    placeholders = ", ".join("?" for _ in HUMAN_EXTRACTORS)
    rows = conn.execute(
        f"SELECT src, dst, type, extractor, status, machine_status FROM edges WHERE extractor NOT IN ({placeholders})",
        sorted(HUMAN_EXTRACTORS),
    ).fetchall()
    return {(r["src"], r["dst"], r["type"], r["extractor"]): (r["status"], r["machine_status"]) for r in rows}


def applied_judgement_rows(conn: sqlite3.Connection) -> dict[str, str]:
    """{entry id: the entry's JSON as the index applied it}."""
    if not _has_machine_status(conn):
        return {}
    return {r["id"]: r["data"] for r in conn.execute("SELECT id, data FROM applied_judgements").fetchall()}


def write_judgement_state(
    conn: sqlite3.Connection,
    *,
    statuses: dict[tuple[str, str, str, str], str],
    states: dict[tuple[str, str, str, str], dict[str, Any]],
    applied: dict[str, tuple[int | None, str]] | None,
) -> None:
    """The applier's one write, in one transaction: each edge's derived
    `status` (only rows whose status differs are touched, so a rescan with
    nothing new writes nothing), the whole `judgement_state` table, and --
    when `applied` is given -- the whole applied copy ({id: (seq, json)})."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = _now()
        for (src, dst, type_, extractor), status in statuses.items():
            if status not in EDGE_STATUSES:
                raise ValueError(f"unknown edge status: {status!r}")
            conn.execute(
                "UPDATE edges SET status = ?, updated_at = ? "
                "WHERE src = ? AND dst = ? AND type = ? AND extractor = ? AND status != ?",
                (status, now, src, dst, type_, extractor, status),
            )
        conn.execute("DELETE FROM judgement_state")
        for (src, dst, type_, extractor), item in states.items():
            if item["outcome"] not in JUDGEMENT_OUTCOMES:
                raise ValueError(f"unknown judgement outcome: {item['outcome']!r}")
            conn.execute(
                """
                INSERT INTO judgement_state (src, dst, type, extractor, outcome, reason, verdict,
                    entry_id, at, note, basis, basis_now, candidates, source_status, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    src, dst, type_, extractor, item["outcome"], item.get("reason"), item.get("verdict"),
                    item.get("entry_id"), item.get("at"), item.get("note"),
                    None if item.get("basis") is None else json.dumps(item["basis"], ensure_ascii=False, sort_keys=True),
                    None if item.get("basis_now") is None else json.dumps(item["basis_now"], ensure_ascii=False, sort_keys=True),
                    json.dumps(item.get("candidates") or [], ensure_ascii=False, sort_keys=True),
                    item.get("source_status"),
                    json.dumps(item.get("detail") or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
        if applied is not None:
            conn.execute("DELETE FROM applied_judgements")
            conn.executemany(
                "INSERT INTO applied_judgements (id, seq, data) VALUES (?, ?, ?)",
                [(entry_id, seq, data) for entry_id, (seq, data) in applied.items()],
            )
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def forget_applied_judgements(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Drop entries from the applied copy (9.3's 「以文件为准」, and the old
    ids of entries 「把缺少的补回文件」 re-appended under new ids)."""
    conn.executemany("DELETE FROM applied_judgements WHERE id = ?", [(i,) for i in ids])
    conn.commit()


def get_record_status(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    if not _has_machine_status(conn):
        return None
    row = conn.execute("SELECT state FROM record_status WHERE name = ?", (name,)).fetchone()
    return None if row is None else json.loads(row["state"])


def set_record_status(conn: sqlite3.Connection, name: str, state: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO record_status (name, state) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET state = excluded.state",
        (name, json.dumps(state, ensure_ascii=False, sort_keys=True)),
    )
    conn.commit()


# -- variable definition cards (DESIGN.md 9.11, migration 0006) ---------------------
# The SQL half of `rce.records.cards`: that module decides what the copy
# holds; these functions store and fetch it.


def has_variable_tables(conn: sqlite3.Connection) -> bool:
    """Whether this index has migration 0006 (an index made before it is
    migrated by the first card application, which holds the lock)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'applied_variable_entries'"
    ).fetchone()
    return row is not None


def variable_cards(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """{card key: {"id", "data", "status"}} -- JSON columns decoded."""
    if not has_variable_tables(conn):
        return {}
    found = {}
    for row in conn.execute("SELECT card, id, data, status FROM variable_cards ORDER BY card").fetchall():
        found[row["card"]] = {
            "id": row["id"],
            "data": None if row["data"] is None else json.loads(row["data"]),
            "status": None if row["status"] is None else json.loads(row["status"]),
        }
    return found


def applied_variable_rows(conn: sqlite3.Connection, card: str) -> dict[str, str]:
    """{entry id: the entry's JSON as the index applied it} for one card."""
    if not has_variable_tables(conn):
        return {}
    rows = conn.execute("SELECT id, data FROM applied_variable_entries WHERE card = ?", (card,)).fetchall()
    return {r["id"]: r["data"] for r in rows}


def write_variable_card(
    conn: sqlite3.Connection,
    card: str,
    card_id: str,
    *,
    status: dict[str, Any],
    data: dict[str, Any] | None = None,
    applied: dict[str, tuple[int | None, str]] | None = None,
) -> None:
    """One card's copy, in one transaction: its status always; its `data`
    and its whole applied copy ({id: (seq, json)}) only when given -- an
    untrusted card keeps what the index had."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO variable_cards (card, id, data, status) VALUES (?, ?, NULL, ?) "
            "ON CONFLICT(card) DO UPDATE SET id = excluded.id, status = excluded.status",
            (card, card_id, json.dumps(status, ensure_ascii=False, sort_keys=True)),
        )
        if data is not None:
            conn.execute(
                "UPDATE variable_cards SET data = ? WHERE card = ?",
                (json.dumps(data, ensure_ascii=False, sort_keys=True), card),
            )
        if applied is not None:
            conn.execute("DELETE FROM applied_variable_entries WHERE card = ?", (card,))
            conn.executemany(
                "INSERT INTO applied_variable_entries (card, id, seq, data) VALUES (?, ?, ?, ?)",
                [(card, entry_id, seq, blob) for entry_id, (seq, blob) in applied.items()],
            )
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def forget_variable_card(conn: sqlite3.Connection, card: str, ids: list[str] | None = None) -> None:
    """Drop entries from one card's applied copy (9.3's 「以文件为准」); with
    `ids` None, the card's whole copy (a card whose directory is gone and
    whose missing entries the researcher let go)."""
    if ids is None:
        conn.execute("DELETE FROM applied_variable_entries WHERE card = ?", (card,))
        conn.execute("DELETE FROM variable_cards WHERE card = ?", (card,))
    else:
        conn.executemany("DELETE FROM applied_variable_entries WHERE card = ? AND id = ?", [(card, i) for i in ids])
    conn.commit()


def has_finished_scan(conn: sqlite3.Connection) -> bool:
    """Whether any scan of this index ran to the end."""
    try:
        return conn.execute("SELECT 1 FROM scans WHERE outcome = 'finished' LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False


# -- scans (DESIGN.md 9.6, migration 0004) ---------------------------------------
#
# The SQL half of `rce.ingest.scan`: that module decides what a scan saw;
# these functions only store and fetch it, keeping raw SQL in this file.

SCAN_SOURCE_STATUSES = frozenset({"read_and_parsed", "absent", "unreadable", "unparseable"})
OBSERVING_SCAN_STATUSES = frozenset({"read_and_parsed", "absent"})


def begin_scan(conn: sqlite3.Connection, label: str) -> int:
    """Open a `scans` row (outcome 'running') and return its id."""
    cursor = conn.execute("INSERT INTO scans (label, started) VALUES (?, ?)", (label, _now()))
    conn.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def finish_scan(
    conn: sqlite3.Connection,
    scan_id: int,
    *,
    outcome: str,
    extractors: list[str],
    sources: list[tuple[str, str, str]],
    nodes: list[tuple[str, str, str]],
) -> None:
    """Record, in one transaction, what scan `scan_id` saw: each
    `(extractor, source, status)` replaces that source's latest status (an
    observing status also moves `observed_scan` here; a failure status
    leaves it); each `(node_id, extractor, source)` says the source
    produced that node in this scan; and every link last produced by an
    observed source in an earlier scan, and not since, gets `scan_lost =
    scan_id` (once -- the first such scan). Sources not listed keep their
    previous state."""
    counts: dict[str, dict[str, int]] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for extractor, source, status in sources:
            if status not in SCAN_SOURCE_STATUSES:
                raise ValueError(f"unknown scan source status: {status!r}")
            observed = scan_id if status in OBSERVING_SCAN_STATUSES else None
            conn.execute(
                """
                INSERT INTO scan_sources (extractor, source, last_scan, status, observed_scan)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(extractor, source) DO UPDATE SET
                    last_scan = excluded.last_scan,
                    status = excluded.status,
                    observed_scan = COALESCE(excluded.observed_scan, scan_sources.observed_scan)
                """,
                (extractor, source, scan_id, status, observed),
            )
            by_status = counts.setdefault(extractor, {})
            by_status[status] = by_status.get(status, 0) + 1
            if observed is None:
                continue
            for table in ("edges", "removed_edges"):
                conn.execute(
                    f"""
                    UPDATE {table} SET scan_lost = ?
                    WHERE extractor = ? AND scan_source = ? AND scan_seen IS NOT NULL
                      AND scan_seen < ? AND scan_lost IS NULL
                    """,
                    (scan_id, extractor, source, scan_id),
                )
        conn.executemany(
            """
            INSERT INTO node_sources (node_id, extractor, source, scan_seen) VALUES (?, ?, ?, ?)
            ON CONFLICT(node_id, extractor, source) DO UPDATE SET scan_seen = excluded.scan_seen
            """,
            [(node_id, extractor, source, scan_id) for node_id, extractor, source in nodes],
        )
        conn.execute(
            "UPDATE scans SET finished = ?, outcome = ?, extractors = ?, source_counts = ? WHERE id = ?",
            (_now(), outcome, json.dumps(sorted(set(extractors))), json.dumps(counts, sort_keys=True), scan_id),
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def get_scan(conn: sqlite3.Connection, scan_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if row is None:
        return None
    scan = dict(row)
    scan["extractors"] = json.loads(scan["extractors"])
    scan["source_counts"] = json.loads(scan["source_counts"])
    return scan


def scan_source_row(conn: sqlite3.Connection, extractor: str, source: str) -> dict[str, Any] | None:
    """The latest status of one source, or None if no scan ever reported it."""
    row = conn.execute(
        "SELECT * FROM scan_sources WHERE extractor = ? AND source = ?", (extractor, source),
    ).fetchone()
    return None if row is None else dict(row)


def scan_sources_of(conn: sqlite3.Connection, extractor: str) -> list[dict[str, Any]]:
    """Every source any scan ever reported for `extractor`."""
    rows = conn.execute("SELECT * FROM scan_sources WHERE extractor = ?", (extractor,)).fetchall()
    return [dict(row) for row in rows]


def all_scan_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every (extractor, source) any scan of this index reported, with its
    latest status ([] on an index from before migration 0004)."""
    try:
        rows = conn.execute("SELECT * FROM scan_sources ORDER BY extractor, source").fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def node_source_rows(conn: sqlite3.Connection, node_id: str) -> list[dict[str, Any]]:
    """Which sources produced `node_id` and in which scan, each joined with
    that source's latest observing scan (`observed_scan`, None if the
    source's row is gone)."""
    rows = conn.execute(
        """
        SELECT ns.node_id, ns.extractor, ns.source, ns.scan_seen,
               ss.observed_scan, ss.status
        FROM node_sources ns
        LEFT JOIN scan_sources ss ON ss.extractor = ns.extractor AND ss.source = ns.source
        WHERE ns.node_id = ?
        """,
        (node_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def edge_scan_row(
    conn: sqlite3.Connection, src: str, dst: str, type: str, extractor: str,
) -> dict[str, Any] | None:
    """A link's scan stamps: from `edges` (`removed: False`), else from the
    stamps an orphan cleanup kept in `removed_edges` (`removed: True`),
    else None."""
    key = (src, dst, type, extractor)
    where = "src = ? AND dst = ? AND type = ? AND extractor = ?"
    row = conn.execute(
        f"SELECT src, dst, type, extractor, status, scan_basis, scan_seen, scan_source, "
        f"scan_appeared, scan_lost FROM edges WHERE {where}",
        key,
    ).fetchone()
    if row is not None:
        return {**dict(row), "removed": False}
    row = conn.execute(
        f"SELECT src, dst, type, extractor, scan_basis, scan_seen, scan_source, "
        f"scan_appeared, scan_lost FROM removed_edges WHERE {where}",
        key,
    ).fetchone()
    if row is not None:
        return {**dict(row), "status": None, "removed": True}
    return None


def edges_appeared_in_scan(
    conn: sqlite3.Connection, scan_id: int, type: str, extractor: str, scan_basis: str | None,
) -> list[dict[str, Any]]:
    """Links whose current run of productions began in scan `scan_id`, of
    one type and extractor, on exactly the canonical basis `scan_basis`
    (None: on any basis -- the claims rule of DESIGN.md 9.12)."""
    basis_clause = "" if scan_basis is None else " AND scan_basis = ?"
    rows = conn.execute(
        f"""
        SELECT src, dst, type, extractor, status, scan_basis, scan_seen, scan_source,
               scan_appeared, scan_lost
        FROM edges
        WHERE scan_appeared = ? AND type = ? AND extractor = ?{basis_clause}
        ORDER BY src, dst
        """,
        (scan_id, type, extractor) + (() if scan_basis is None else (scan_basis,)),
    ).fetchall()
    return [dict(row) for row in rows]
