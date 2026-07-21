-- Migration 0001: initial provenance graph schema.
--
-- Node and edge types are fixed by HANDOFF-SPEC.md section 4 (ontology v0).
-- Do not add types here without updating the spec first -- the CHECK
-- constraints below are the enforcement mechanism for that contract.

CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN (
        'project', 'experiment', 'commit', 'figure',
        'section', 'claim', 'reference', 'contributor'
    )),
    title TEXT,
    -- attrs: machine-owned JSON blob (stored as TEXT; SQLite has no native
    -- JSON column type). Freely overwritten by upsert_node.
    attrs TEXT NOT NULL DEFAULT '{}',
    -- human_fields: human-owned JSON blob. Schema-level invariant: machine
    -- ingestion (upsert_node) must never write this column. See db.py.
    human_fields TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_nodes_type ON nodes(type);

CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src TEXT NOT NULL REFERENCES nodes(id),
    dst TEXT NOT NULL REFERENCES nodes(id),
    -- Edge types per HANDOFF-SPEC.md section 4 (deterministic + 7B layers).
    -- 'summarized_as' is intentionally excluded: the spec models it as a
    -- node-attribute summary, not an edge.
    type TEXT NOT NULL CHECK (type IN (
        'implements', 'produces', 'generates', 'includes',
        'cites', 'authored_by', 'backed_by', 'supports'
    )),
    extractor TEXT NOT NULL,
    evidence TEXT NOT NULL,
    confidence REAL NOT NULL,
    -- Confirmation queue is status='pending'; no separate queue table
    -- (architecture decision, HANDOFF-SPEC.md task brief).
    status TEXT NOT NULL CHECK (status IN ('auto', 'pending', 'confirmed', 'rejected')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- UNIQUE(src,dst,type,extractor) makes upsert_edge idempotent: re-running
    -- the same extractor over the same evidence updates in place.
    UNIQUE (src, dst, type, extractor)
);

CREATE INDEX idx_edges_src ON edges(src);
CREATE INDEX idx_edges_dst ON edges(dst);
CREATE INDEX idx_edges_status ON edges(status);
