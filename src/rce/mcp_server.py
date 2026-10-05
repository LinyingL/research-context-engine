"""RCE's MCP stdio server (T5) -- the product's primary interface
(DESIGN.md section 1/7: the user's existing AI assistant is the front
end; this is what it talks to).

Built on the official `mcp` SDK's FastMCP wrapper rather than hand-rolling
JSON-RPC framing (constitution section 0, Occam rule 1). Runtime dependency
"mcp" approved by Owner 2026-07-22.

Each tool is a thin wrapper: open a connection, call one of the plain
functions below (which take `conn` directly -- that's what the test suite
calls, no stdio/client involved), format as text, close the connection.
Every tool states explicitly when its result is empty/unknown, per the
constitution's "无边如实返回空结构，禁止编造" -- never invented. rce_confirm_edge
is the sole write tool; since V5 (DESIGN.md 9.1, 9.8) it records the act in
the project's `.rce/judgements.toml` through `rce.records.judgements.judge`
-- the one human write path every surface uses -- and the index follows.
Its verdicts are the ledger's (confirmed / rejected / withdrawn / undone),
and like the canvas it refuses a hand-drawn link. Its description tells
the calling assistant to invoke it only on an explicit human ask. A link
whose judgment is under review or in conflict is marked in every tool's
output (9.6 "Where it shows").
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from mcp.server.fastmcp import FastMCP

from rce import db, paths, query
from rce import project as project_identity
from rce.records import judgements
from rce.records import lock as records_lock
from rce.records import situation as records_situation

# Kept as module attributes for callers quoting them; the definitions live
# in rce.paths (DESIGN.md section 8.10 rule 1).
RCE_DIRNAME = paths.RCE_DIRNAME
DB_FILENAME = paths.DB_FILENAME


class McpServerError(Exception):
    """User-facing error; caught once in main() -> "Error: <msg>" on stderr, exit 1."""


def _require_db(project_root: Path) -> Path:
    """This subsystem's copy of the gate (each raises its own error type),
    resolving through `rce.paths.resolve_graph_db` like every other copy:
    the graph is at `~/.rce/graphs/<id>/graph.db`, and a legacy in-project
    one is migrated here on first touch -- an MCP client may well be the
    first thing to open a project after the upgrade."""
    try:
        path = paths.resolve_graph_db(project_root)
    except paths.GraphMigrationError as exc:
        raise McpServerError(str(exc)) from exc
    if not path.exists():
        raise McpServerError(
            f"no RCE project at {project_root} (missing its graph at {path}); "
            f"run 'rce init {project_root}' first"
        )
    return path


@contextmanager
def _connect(project_root: Path):
    """Short-lived per-call connection shared by all four tools below --
    nothing is held open across calls."""
    conn = db.connect(_require_db(project_root))
    try:
        yield conn
    finally:
        conn.close()


# -- plain, directly-testable implementations (no FastMCP/stdio involved) ---


def trace_result(conn: Connection, node_id: str, max_hops: int = 4) -> dict[str, Any]:
    return query.trace(conn, node_id, max_hops=max_hops)


def format_trace_text(node_id: str, result: dict[str, Any]) -> str:
    """Human-readable trace text. Each hop's `source_location` (query-time-
    resolved claim line -- see rce.query.claim_source_location/trace) is
    surfaced as `file:line` right in the summary line when present, not
    just left buried in the raw `evidence` JSON dump -- the full structured
    `result` (source_location included) is also appended separately by the
    `rce_trace` tool below, for a scripted consumer."""
    if not result["found"]:
        return f"No such node: {node_id}"
    if not result["hops"]:
        return f"Node {node_id} exists but has no provenance edges to trace."
    lines = [f"Provenance trace for {node_id}:"]
    for hop in result["hops"]:
        evidence = json.dumps(hop["evidence"], sort_keys=True)
        location = hop.get("source_location")
        location_note = f", source_location={location['file']}:{location['line']}" if location else ""
        lines.append(
            f"  [{hop['depth']}] {hop['src']} --{hop['type']}--> {hop['dst']} "
            f"(extractor={hop['extractor']}, confidence={hop['confidence']:.2f}, "
            f"status={hop['status']}{location_note}){judgements.review_marker(hop)} evidence={evidence}"
        )
    return "\n".join(lines)


def find_nodes(conn: Connection, text: str, node_type: str | None = None) -> list[dict[str, Any]]:
    """Case-insensitive substring match against node id/title, optionally
    restricted to one node type. ValueError on an unknown node_type."""
    if node_type is not None and node_type not in db.NODE_TYPES:
        raise ValueError(f"unknown node type: {node_type!r}")
    needle = text.lower()
    types = [node_type] if node_type else sorted(db.NODE_TYPES)
    return [
        node
        for t in types
        for node in db.get_nodes_by_type(conn, t)
        if needle in node["id"].lower() or (node["title"] and needle in node["title"].lower())
    ]


def format_find_text(text: str, node_type: str | None, matches: list[dict[str, Any]]) -> str:
    if not matches:
        filt = f" (type={node_type})" if node_type else ""
        return f"No nodes matching {text!r}{filt}."
    lines = [f"Found {len(matches)} node(s) matching {text!r}:"]
    for node in matches:
        title = f' "{node["title"]}"' if node["title"] else ""
        lines.append(f"  {node['id']} ({node['type']}){title}")
    return "\n".join(lines)


def status_summary(conn: Connection) -> dict[str, Any]:
    node_counts = {t: len(db.get_nodes_by_type(conn, t)) for t in sorted(db.NODE_TYPES)}
    edge_counts = {t: 0 for t in sorted(db.EDGE_TYPES)}
    for edge in db.query_edges(conn):
        edge_counts[edge["type"]] += 1
    return {
        "nodes": node_counts, "edges": edge_counts, "pending": len(db.pending_edges(conn)),
        "review": judgements.review_count(conn),
    }


def format_status_text(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "RCE graph status:",
            "  Nodes: " + " ".join(f"{k}={v}" for k, v in summary["nodes"].items()),
            "  Edges: " + " ".join(f"{k}={v}" for k, v in summary["edges"].items()),
            f"  Pending confirmation queue: {summary['pending']}",
            f"  Judgments under review (see `rce review`): {summary.get('review', 0)}",
        ]
    )


CONFIRM_VERDICTS = ("confirmed", "rejected", "withdrawn", "undone")


def confirm_edge(
    root: str | Path,
    project_id: str | None,
    src: str,
    dst: str,
    type: str,  # noqa: A002 -- the tool's own parameter name
    extractor: str,
    new_status: str,
    note: str | None = None,
) -> str:
    """The human act on one machine link, through the one write path
    (`rce.records.judgements.judge`, `via = "mcp"`): written to the ledger
    first, then applied. ValueError for an unknown edge type or verdict; a
    refusal (a hand-drawn link, no such edge, a ledger that cannot be
    trusted, a moved or pre-V5 project) is the tool's answer, not an
    exception -- nothing was written."""
    if type not in db.EDGE_TYPES:
        raise ValueError(f"unknown edge type: {type!r}")
    if new_status not in CONFIRM_VERDICTS:
        raise ValueError(f"unknown verdict: {new_status!r} (one of {', '.join(CONFIRM_VERDICTS)})")
    label = f"{src} --{type}--> {dst} (extractor={extractor})"
    try:
        judged = judgements.judge(
            Path(root), (src, dst, type, extractor), new_status, via="mcp", note=note, expected_id=project_id,
        )
    except judgements.JudgementRefused as exc:
        if exc.code == "no_such_link":
            return f"No such edge: {label}; nothing changed."
        why = f" ({exc.message_zh})" if exc.message_zh else ""
        return f"Not written: {exc}{why}"
    except records_situation.WriteRefused as exc:
        return f"Not written: {exc}"
    except records_lock.ProjectLockError as exc:
        return f"Not written: could not take the project lock ({exc})"
    state = judged.state
    tail = ""
    if state is not None and state["outcome"] != "applied":
        reason = state.get("reason") or state["outcome"]
        tail = f" Not applied: {reason} ({judgements.REASON_LABELS.get(reason, '')}) -- see `rce review`."
    return (
        f"Edge {label}: recorded {new_status!r} in .rce/judgements.toml ({judged.entry.id}); "
        f"status set to {judged.status!r}.{tail}"
    )


# -- FastMCP server assembly --------------------------------------------------


def build_server(project_root: str | Path, project_id: str | None = None) -> FastMCP:
    """Register the four tools against project_root's graph (resolved by
    rce.paths -- outside the project since DESIGN.md section 8.10 rule 1).
    `project_id` is the id `main` opened the project with (None: read it
    from the folder now)."""
    root = Path(project_root).resolve()
    if project_id is None:
        got = records_situation.read_identity(root)
        project_id = got.identity.id if got.identity is not None else None
    mcp = FastMCP("rce")

    @mcp.tool()
    def rce_trace(node_id: str) -> str:
        """Return the full provenance/evidence chain for a node (e.g. a
        figure, claim, or experiment id): how it traces back to the
        commit(s)/experiment(s) that produced it, and forward to the paper
        section(s)/reference(s) that cite or rely on it. Each hop lists its
        edge type, extractor, confidence, status, and evidence. Ends with a
        structured JSON block. States explicitly when the node is unknown or
        has no edges -- never invents a chain."""
        with _connect(root) as conn:
            result = trace_result(conn, node_id)
        return format_trace_text(node_id, result) + "\n\nJSON:\n" + json.dumps(result, sort_keys=True)

    @mcp.tool()
    def rce_find(text: str, node_type: str | None = None) -> str:
        """Search graph nodes by case-insensitive substring match against id
        or title. Use this BEFORE rce_trace whenever the user gives a vague
        description instead of an exact node id (e.g. "Figure 4",
        "result.png") -- rce_find locates the candidate id(s), then
        rce_trace explains their provenance. Optional node_type filters to
        one of: project/experiment/commit/figure/section/claim/reference/
        contributor. States explicitly when nothing matches."""
        with _connect(root) as conn:
            matches = find_nodes(conn, text, node_type)
        return format_find_text(text, node_type, matches)

    @mcp.tool()
    def rce_status() -> str:
        """Report whole-graph node and edge counts by type, plus the size of
        the pending human-confirmation queue."""
        with _connect(root) as conn:
            summary = status_summary(conn)
        return format_status_text(summary)

    @mcp.tool()
    def rce_confirm_edge(
        src: str, dst: str, type: str, extractor: str, new_status: str, note: str | None = None,
    ) -> str:
        """Human judgment channel for exactly one machine-extracted edge,
        recorded in the project's .rce/judgements.toml. Call this ONLY when
        the user has explicitly asked to confirm, reject, withdraw or undo
        a judgment on a specific edge (e.g. "confirm that figure 4 is
        backed by run xyz") -- never speculatively or during routine
        tracing. new_status must be one of: confirmed / rejected /
        withdrawn (back to the machine's status) / undone (takes back the
        last act). `note` is optional. Hand-drawn links (extractor
        "mapping") are refused: they live in .rce/mappings.toml. States
        explicitly when no matching edge exists or nothing was written."""
        return confirm_edge(root, project_id, src, dst, type, extractor, new_status, note)

    return mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rce mcp", description="Run the RCE MCP stdio server.")
    parser.add_argument(
        "--path", default=".",
        help="project root of an initialized RCE project (default: '.'); the graph itself "
             "lives at ~/.rce/graphs/<id>/graph.db, see rce.paths",
    )
    args = parser.parse_args(argv)
    project_root = Path(args.path).resolve()
    try:
        # The identity check first (DESIGN.md 9.4): a copy, a home that
        # cannot be checked, a lost or unreadable identity stops here and
        # nothing is written; a moved project is adopted.
        if not project_root.is_dir():
            raise McpServerError(f"{project_root} is not a directory")
        try:
            opened = project_identity.open_project(project_root)
        except (project_identity.ProjectBlocked, project_identity.AnswerRefused) as exc:
            raise McpServerError(str(exc)) from exc
        _require_db(project_root)
        server = build_server(project_root, opened.project_id)
    except McpServerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
