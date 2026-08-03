-- Migration 0003: add `script`/`dataset` node types and `reads`/`writes`
-- edge types (DESIGN.md section 4, ontology v2 -- task W2, data lineage).
--
-- `script` is any .py/.R/.Rmd source file the dataflow extractor
-- (rce.ingest.dataflow) scanned for read/write calls; `dataset` is a
-- tabular/data file (rce.ingest.git.DATA_EXTENSIONS) such a call targets.
-- An image-extension target reuses the existing `figure` node type instead
-- of a third node type -- see rce.ingest.dataflow's module docstring for why
-- `savefig()`/`ggsave()` writes are modeled as `script --writes--> figure`
-- rather than inventing a parallel "image" node.
--
-- `reads`/`writes` are both `script -> dataset` (or `script -> figure` for
-- an image write) -- deterministic, written by rce.ingest.dataflow only.
--
-- Same standard SQLite 12-step CHECK-widening rebuild as migration 0002 (see
-- that file's header comment for the full rationale); this file's statements
-- run inside the one BEGIN/COMMIT + foreign_keys OFF/ON bracket db.migrate()
-- already wraps every migration file in, so a mid-script failure rolls the
-- whole rebuild back rather than leaving `nodes`/`edges` half-renamed.
--
-- Applying this on a database that already ran 0001+0002 (the normal case
-- for an existing user's project) must not lose the rows already in
-- `nodes`/`edges` -- see
-- tests/test_db.py::test_0003_migration_preserves_data_from_0001_0002_db.

CREATE TABLE nodes_new (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN (
        'project', 'experiment', 'commit', 'figure',
        'section', 'claim', 'reference', 'contributor', 'attempt',
        'script', 'dataset'
    )),
    title TEXT,
    attrs TEXT NOT NULL DEFAULT '{}',
    human_fields TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO nodes_new SELECT * FROM nodes;

DROP TABLE nodes;

ALTER TABLE nodes_new RENAME TO nodes;

CREATE INDEX idx_nodes_type ON nodes(type);

CREATE TABLE edges_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src TEXT NOT NULL REFERENCES nodes(id),
    dst TEXT NOT NULL REFERENCES nodes(id),
    type TEXT NOT NULL CHECK (type IN (
        'implements', 'produces', 'generates', 'includes',
        'cites', 'authored_by', 'backed_by', 'supports', 'uses',
        'reads', 'writes'
    )),
    extractor TEXT NOT NULL,
    evidence TEXT NOT NULL CHECK (evidence NOT IN ('{}', '', 'null')),
    confidence REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('auto', 'pending', 'confirmed', 'rejected')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (src, dst, type, extractor)
);

INSERT INTO edges_new SELECT * FROM edges;

DROP TABLE edges;

ALTER TABLE edges_new RENAME TO edges;

CREATE INDEX idx_edges_src ON edges(src);
CREATE INDEX idx_edges_dst ON edges(dst);
CREATE INDEX idx_edges_status ON edges(status);
