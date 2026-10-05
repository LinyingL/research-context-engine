-- Migration 0006: the index's copy of the variable definition cards
-- (DESIGN.md 9.11; task V5 phase 8).
--
-- A card lives in the project, in `.rce/variables/<id>/`: the
-- researcher's version files, RCE's append-only `log.toml`, and the
-- frozen copies. The index keeps, derived and rebuildable like
-- everything else here:
--
--   variable_cards            per card (keyed by its id case-folded, since
--                             ids compare case-folded): the id as written,
--                             a copy of the card's files as last trusted
--                             (`data`: version texts and frozen copies,
--                             JSON) for the app, and the card's last trust
--                             decision (`status`, JSON). A card with a
--                             status but no copy was never trusted here.
--   applied_variable_entries  the copy of every log entry the index has
--                             applied, per card -- 9.3's safety net, which
--                             makes a log that lost entries (an `abandoned`
--                             as much as a `confirmed`) a question rather
--                             than the truth. Never a second authority.
--
-- No node type is promised to later phases (9.11, "In the index").

CREATE TABLE variable_cards (
    card TEXT PRIMARY KEY,
    id TEXT NOT NULL,
    data TEXT,
    status TEXT
);

CREATE TABLE applied_variable_entries (
    card TEXT NOT NULL,
    id TEXT NOT NULL,
    seq INTEGER,
    data TEXT NOT NULL,
    PRIMARY KEY (card, id)
);
