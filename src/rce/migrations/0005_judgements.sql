-- Migration 0005: the judgment ledger drives the index (DESIGN.md 9.1,
-- 9.3, 9.6; task V5 phase 4).
--
-- Since V5 a human verdict on a machine link lives in the project's
-- `.rce/judgements.toml`; the index only DERIVES `edges.status` from it
-- (`rce.records.judgements.apply_ledger`). Three things the index needs
-- for that, all derived, all rebuilt by deleting the index:
--
--   edges.machine_status   what the machine itself says about the link
--                          ('auto' or 'pending'), written by every
--                          upsert_edge. When a judgment is not applied
--                          (withdrawn, under review, in conflict), the link
--                          is shown at THIS status -- before 0005 a confirm
--                          overwrote it and it was gone. Backfilled for
--                          existing rows: their machine status where it is
--                          still machine-owned, else the status a machine
--                          extractor writes for that extractor (claims
--                          candidates are 'pending', everything else
--                          'auto'). Mapping links keep NULL: their truth
--                          is `.rce/mappings.toml`, never the ledger.
--   applied_judgements     the copy of every ledger entry the index has
--                          applied (id + full content, JSON) -- the safety
--                          net of 9.3 that makes a SHRUNK file visible. A
--                          safety net, never a second authority.
--   judgement_state        per judged link, what the last application
--                          decided: applied, under review (with exactly
--                          one reason), in conflict, held because its
--                          source could not be read, or not in the index.
--                          Views and the CLI read it; `pending_edges`
--                          leaves links under review out of 待确认.
--   record_status          the last trust decision per record file
--                          (judgements: ok / refuse_writes / shrunk /
--                          conflict_copy), for the watcher's status and
--                          `GET /api/review`.

ALTER TABLE edges ADD COLUMN machine_status TEXT CHECK (machine_status IS NULL OR machine_status IN ('auto', 'pending'));

UPDATE edges SET machine_status = CASE
    WHEN status IN ('auto', 'pending') THEN status
    WHEN extractor = 'claims' THEN 'pending'
    ELSE 'auto'
END
WHERE extractor != 'mapping';

CREATE TABLE applied_judgements (
    id TEXT PRIMARY KEY,
    seq INTEGER,
    data TEXT NOT NULL
);

CREATE TABLE judgement_state (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    type TEXT NOT NULL,
    extractor TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('applied', 'review', 'conflict', 'held', 'not_in_index')),
    reason TEXT,
    verdict TEXT,
    entry_id TEXT,
    at TEXT,
    note TEXT,
    basis TEXT,
    basis_now TEXT,
    candidates TEXT NOT NULL DEFAULT '[]',
    source_status TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (src, dst, type, extractor)
);

CREATE INDEX idx_judgement_state_outcome ON judgement_state(outcome);

CREATE TABLE record_status (
    name TEXT PRIMARY KEY,
    state TEXT NOT NULL
);
