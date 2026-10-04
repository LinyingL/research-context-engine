"""Human records outlive the index (DESIGN.md section 9, task V5).

The record (人工记录) lives inside the project, in `.rce/`, as plain files
the researcher can read, diff, commit and back up; the index (`graph.db`
under `~/.rce/graphs/`) is derived from the project's sources plus the
record and may be deleted at any time. This package is the record's core,
a library with no knowledge of the CLI, the server or ingest -- those are
wired to it by later phases, through these modules only:

- `lock`     -- the cross-process project lock (9.7)
- `files`    -- durable writes, append-only record writes, daily snapshots,
                reading a record file that may be absent / in the cloud /
                unreadable, sync conflict copies (9.2, 9.7)
- `identity` -- `.rce/project.toml` (9.4, the file only)
- `ledger`   -- the generic append-only ledger engine and the judgment
                schema (9.3)
- `trust`    -- the pure trust rules a writer and a reader consult first
                (9.3)
- `situation` -- which project a folder is (the 9.4 situation table),
                `home.json`, and the write-time identity re-check every
                record and index write makes under the project lock
"""
