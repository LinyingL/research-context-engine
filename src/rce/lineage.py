"""Read-only lineage report over `rce.ingest.dataflow`'s graph (task W4).

`rce lineage` is a query surface, not an extractor: it writes nothing to the
database and re-parses no source file. Everything it reports comes from
`script --reads/writes--> dataset|figure` edges `rce ingest` already wrote
(DESIGN.md section 4, migration 0003) -- run `rce ingest` first, same as
`rce query`/`rce trace`/`rce status`.

Four blocks, each answering a distinct provenance question a paper submission
raises:

1. **Orphan inputs** (`orphans`) -- a `dataset` read by >=1 script but written
   by none. Scoped to `dataset` only (not `figure`): the real-world question
   this answers is "where did this input data come from", and a figure is
   normally an output, never an input a script re-reads.
2. **Lineage chains** (`chains`) -- a `dataset` or `figure` with both a writer
   and a reader recorded: the "produced here, consumed there" chain a
   reviewer asks for. `figure` is included here (unlike orphans/duplicates)
   because a generated plot is exactly as traceable a chain link as a
   generated dataset.
3. **Broken links** (`broken_links`) -- any `reads`/`writes` occurrence whose
   evidence carries `missing: true` (`rce.ingest.dataflow`'s own flag for "the
   script wants to read/write a file that isn't on disk"). Applies to both
   node types; a script can point at a nonexistent target either way.
4. **Duplicate copies** (`duplicates`) -- a read `dataset` whose basename also
   exists at other paths in the project tree, scoped to `dataset` only for
   the same reason as orphans: "which of the 4 copies of theme_counts.csv did
   the script actually read" is a data-provenance question, not a figure one.

Never guesses which occurrence, script, or file is "the" answer -- every
finding lists every matching occurrence/path it actually found, sorted for
stable, diffable output, and an empty block is simply the true, honest
answer "none of this pattern exist" rather than a placeholder.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from rce import db

# Directories never worth walking for a duplicate-basename scan: version
# control internals, this tool's own state, and common tooling caches --
# mirrors rce.ingest.files._NOISE_DIRS (not imported from there: that list
# exists to build a *categorized source inventory*; this one wants literally
# every real file on disk, so the two lists serve different callers even
# though today's values happen to coincide). Every dot-prefixed directory is
# *also* skipped below regardless of this set, same rationale as
# rce.ingest.files: a project's own tool caches are not enumerable by a fixed
# list.
_NOISE_DIRS = frozenset(
    {".git", ".rce", "__pycache__", ".venv", "node_modules", ".Rproj.user", "_cache", ".ipynb_checkpoints"}
)


def _occurrences(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """An edge's evidence, unwrapped to its list of occurrence dicts.

    Every `reads`/`writes` edge is written via `db.upsert_edge` (never
    `set_edge_semantic_review`), so in practice this is always the
    `{"occurrences": [...]}` shape -- but the defensive fallback matches
    every other evidence reader in this codebase (see
    `rce.cli._format_evidence_summary`) for the same pre-T10 legacy-row
    reason, cheap insurance rather than a real expected case here.
    """
    occurrences = evidence.get("occurrences")
    if not isinstance(occurrences, list):
        occurrences = [evidence]
    return occurrences


def _target_path_and_type(conn: Connection, target_id: str) -> tuple[str, str | None]:
    """A target node's repo-relative path (its `title`, set by
    `ingest_dataflow_repo` to exactly this) and its node type ("dataset" or
    "figure"). Falls back to stripping the id's own `<type>:` prefix only if
    the node is somehow missing (defensive -- every `reads`/`writes` edge's
    dst is upserted in the same call that writes the edge, so this should
    not happen in practice)."""
    node = db.get_node(conn, target_id)
    if node is not None:
        return node["title"] or target_id, node["type"]
    _, _, path = target_id.partition(":")
    return path or target_id, None


def _collect_by_target(edges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Every `(file, line, callee, ...)` occurrence across a list of edges,
    grouped by the edge's `dst` (the target node id) -- one edge per distinct
    script, so a target read/written by several scripts spans several edges
    here, each possibly itself carrying several occurrences (several call
    sites in that one script)."""
    by_target: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        by_target.setdefault(edge["dst"], []).extend(_occurrences(edge["evidence"]))
    return by_target


def _sorted_occurrences(occurrences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(occurrences, key=lambda occ: (occ.get("file") or "", occ.get("line") or 0))


def _reader_entry(occ: dict[str, Any]) -> dict[str, Any]:
    return {"script": occ.get("file"), "line": occ.get("line"), "callee": occ.get("callee")}


def _scan_basenames(project_root: Path) -> dict[str, list[str]]:
    """Every real (non-symlink) file under `project_root`, grouped by
    basename -> sorted repo-relative paths -- the input to duplicate-copy
    detection (block 4). Skips the same noise/hidden directories
    `rce.ingest.files.list_source_files` skips (see `_NOISE_DIRS` above), and
    never follows a symlink, matching that module's own guard against
    reporting a path that doesn't lead to a distinct real file.
    """
    by_basename: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(project_root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _NOISE_DIRS and not d.startswith("."))
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            full_path = Path(dirpath) / filename
            if full_path.is_symlink():
                continue
            rel = full_path.relative_to(project_root).as_posix()
            by_basename.setdefault(filename, []).append(rel)
    return by_basename


def build_lineage_report(conn: Connection, project_root: str | Path) -> dict[str, Any]:
    """The full four-block lineage report (task W4), as a plain JSON-ready
    dict:

        {
          "scanned": {"scripts": int, "reads_edges": int, "writes_edges": int,
                      "targets": int},
          "orphans": [{"target": id, "path": str, "readers": [
                          {"script": str, "line": int, "callee": str}, ...]}],
          "chains": [{"target": id, "path": str,
                      "writers": [...], "readers": [...]}],
          "broken_links": [{"script": str, "line": int, "callee": str,
                             "kind": "reads"|"writes", "target": str}],
          "duplicates": [{"target": id, "path": str,
                           "other_copies": [str, ...]}],
        }

    Every list is sorted by path (then script/line within a target) so two
    runs over an unchanged graph produce byte-identical output. `scanned`
    always reflects what this run actually looked at, even when every block
    below it is empty -- the caller (`rce.cli`) uses it to explain a truly
    empty result rather than printing a bare success line (DESIGN.md
    section 0: a missing finding is a normal outcome, but it must still be
    stated, not silently indistinguishable from "nothing was checked").
    """
    project_root = Path(project_root)
    reads_edges = db.query_edges(conn, type="reads")
    writes_edges = db.query_edges(conn, type="writes")
    readers_by_target = _collect_by_target(reads_edges)
    writers_by_target = _collect_by_target(writes_edges)
    all_targets = sorted(set(readers_by_target) | set(writers_by_target))

    orphans: list[dict[str, Any]] = []
    chains: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    basenames: dict[str, list[str]] | None = None  # lazy: only walked if a candidate exists

    for target_id in all_targets:
        readers = _sorted_occurrences(readers_by_target.get(target_id, []))
        writers = _sorted_occurrences(writers_by_target.get(target_id, []))
        path, node_type = _target_path_and_type(conn, target_id)

        if readers and writers:
            chains.append({
                "target": target_id, "path": path,
                "writers": [_reader_entry(o) for o in writers],
                "readers": [_reader_entry(o) for o in readers],
            })
        elif readers and not writers and node_type == "dataset":
            # Orphan inputs are scoped to `dataset` only -- a `figure` read
            # back by a script but never written by one is not the
            # "unexplained input data" finding this block exists for; see
            # module docstring.
            orphans.append({
                "target": target_id, "path": path,
                "readers": [_reader_entry(o) for o in readers],
            })

        if readers and node_type == "dataset":
            # Duplicate-copy detection (block 4), scoped to `dataset` like
            # orphans -- also only for a target that is actually *read*
            # somewhere, matching the real question ("which copy did the
            # script that reads it actually see").
            if basenames is None:
                basenames = _scan_basenames(project_root)
            others = sorted(p for p in basenames.get(posixpath.basename(path), []) if p != path)
            if others:
                duplicates.append({"target": target_id, "path": path, "other_copies": others})

    broken_links: list[dict[str, Any]] = []
    for edges, kind in ((reads_edges, "reads"), (writes_edges, "writes")):
        for edge in edges:
            target_path, _ = _target_path_and_type(conn, edge["dst"])
            for occ in _occurrences(edge["evidence"]):
                if occ.get("missing"):
                    broken_links.append({
                        "script": occ.get("file"), "line": occ.get("line"),
                        "callee": occ.get("callee"), "kind": kind, "target": target_path,
                    })
    broken_links.sort(key=lambda b: (b["script"] or "", b["line"] or 0))

    return {
        "scanned": {
            "scripts": len(db.get_nodes_by_type(conn, "script")),
            "reads_edges": len(reads_edges),
            "writes_edges": len(writes_edges),
            "targets": len(all_targets),
        },
        "orphans": orphans,
        "chains": chains,
        "broken_links": broken_links,
        "duplicates": duplicates,
    }
