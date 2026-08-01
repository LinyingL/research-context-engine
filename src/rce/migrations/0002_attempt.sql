-- Migration 0002: add the `attempt` node type and the `uses` edge type
-- (DESIGN.md section 4, ontology v1).
--
-- `attempt` represents one row of a researcher's manually-maintained
-- "attempt timeline" (a project-level record of research paths tried,
-- kept as ordinary Markdown -- see DESIGN.md section 4 for the id
-- convention and the attrs/human_fields split). `uses` is the edge
-- `attempt --uses--> commit`: the last commit to touch a script file the
-- attempt depends on, used for verdict-staleness checks.
--
-- SQLite has no `ALTER TABLE ... ADD CHECK` / `DROP CHECK`, so widening the
-- `type` CHECK on `nodes` and `edges` requires the standard 12-step
-- rebuild (https://www.sqlite.org/lang_altertable.html section 7): create
-- a new table with the desired schema, copy the data across, drop the old
-- table, rename the new one into place, then recreate its indexes.
--
-- This file's statements run inside the one BEGIN/COMMIT transaction
-- db.migrate() already wraps every migration file in (see db.py), so a
-- mid-script failure rolls the whole rebuild back rather than leaving
-- `nodes`/`edges` half-renamed. db.migrate() also brackets that same
-- transaction with PRAGMA foreign_keys OFF/ON (a SQLite requirement: the
-- pragma is a no-op once a transaction is already open, and DROP TABLE on
-- `nodes` while `edges` still references it by name fails with enforcement
-- on) and runs PRAGMA foreign_key_check before committing, so this file
-- itself contains no PRAGMA foreign_keys statements of its own.
--
-- Applying this on a database that only ever ran 0001 (the normal case for
-- an existing user's project) must not lose the rows already in `nodes`/
-- `edges` -- see tests/test_db.py::test_0002_migration_preserves_data_from_0001_only_db.

CREATE TABLE nodes_new (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN (
        'project', 'experiment', 'commit', 'figure',
        'section', 'claim', 'reference', 'contributor', 'attempt'
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
        'cites', 'authored_by', 'backed_by', 'supports', 'uses'
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
