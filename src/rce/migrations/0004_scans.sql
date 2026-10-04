-- Migration 0004: what each scan saw (DESIGN.md 9.6, "What a scan must
-- report"; task V5 phase 3).
--
-- A judgment is a statement about particular evidence, so a later phase
-- must be able to ask, per link: did the latest scan that actually read
-- this link's source produce it, and on what basis? The accumulated
-- `evidence.occurrences` cannot answer that (it only ever grows), so each
-- scan stamps what IT saw, beside the evidence, never inside it:
--
--   edges.scan_basis    canonical JSON (sorted keys) of the facts the link
--                       rested on in the scan that last produced it, per the
--                       9.6 table (rce.ingest.scan.basis); NULL = never
--                       produced by a recording scan
--   edges.scan_seen     id of the scan that last produced the link
--   edges.scan_source   the source (file, tracking store, commit list,
--                       check) that produced it in that scan
--   edges.scan_appeared the scan in which the current unbroken run of
--                       productions began (9.6 "No transfer, but a prompt")
--   edges.scan_lost     the first scan that read scan_source and did not
--                       produce the link (NULL while it is produced)
--
-- `scans` is one row per scan (full or partial); `scan_sources` is the
-- latest status of every (extractor, source) any scan reported --
-- read_and_parsed / absent / unreadable / unparseable -- with
-- `observed_scan`, the latest scan whose status was an observation
-- (read_and_parsed or absent). A scan only ever writes the rows of the
-- sources it read; everything else keeps its previous state.
-- `node_sources` records which source produced a node in which scan
-- (endpoint presence). `removed_edges` keeps the scan stamps of a link an
-- orphan cleanup deleted (9.1), so a deleted link can still be described.
--
-- Additive only: no existing row changes, nothing is rebuilt.

ALTER TABLE edges ADD COLUMN scan_basis TEXT;

ALTER TABLE edges ADD COLUMN scan_seen INTEGER;

ALTER TABLE edges ADD COLUMN scan_source TEXT;

ALTER TABLE edges ADD COLUMN scan_appeared INTEGER;

ALTER TABLE edges ADD COLUMN scan_lost INTEGER;

CREATE INDEX idx_edges_scan_source ON edges(extractor, scan_source);

CREATE INDEX idx_edges_scan_appeared ON edges(scan_appeared);

CREATE TABLE scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    outcome TEXT NOT NULL DEFAULT 'running' CHECK (outcome IN ('running', 'finished', 'failed')),
    extractors TEXT NOT NULL DEFAULT '[]',
    source_counts TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE scan_sources (
    extractor TEXT NOT NULL,
    source TEXT NOT NULL,
    last_scan INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('read_and_parsed', 'absent', 'unreadable', 'unparseable')),
    observed_scan INTEGER,
    PRIMARY KEY (extractor, source)
);

CREATE TABLE node_sources (
    node_id TEXT NOT NULL,
    extractor TEXT NOT NULL,
    source TEXT NOT NULL,
    scan_seen INTEGER NOT NULL,
    PRIMARY KEY (node_id, extractor, source)
);

CREATE INDEX idx_node_sources_source ON node_sources(extractor, source);

CREATE TABLE removed_edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    type TEXT NOT NULL,
    extractor TEXT NOT NULL,
    scan_basis TEXT,
    scan_seen INTEGER,
    scan_source TEXT,
    scan_appeared INTEGER,
    scan_lost INTEGER,
    removed_at TEXT NOT NULL,
    PRIMARY KEY (src, dst, type, extractor)
);
