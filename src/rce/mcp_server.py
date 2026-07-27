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
is the sole write path (via db.set_edge_status); its description tells the
calling assistant to invoke it only on an explicit human confirm/reject ask.
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

from rce import db, query

RCE_DIRNAME = ".rce"
DB_FILENAME = "graph.db"


class McpServerError(Exception):
    """User-facing error; caught once in main() -> "Error: <msg>" on stderr, exit 1."""


def _require_db(project_root: Path) -> Path:
    path = project_root / RCE_DIRNAME / DB_FILENAME
    if not path.exists():
        raise McpServerError(
            f"no RCE project at {project_root} (missing {RCE_DIRNAME}/{DB_FILENAME}); "
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
            f"status={hop['status']}{location_note}) evidence={evidence}"
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
    return {"nodes": node_counts, "edges": edge_counts, "pending": len(db.pending_edges(conn))}


def format_status_text(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "RCE graph status:",
            "  Nodes: " + " ".join(f"{k}={v}" for k, v in summary["nodes"].items()),
            "  Edges: " + " ".join(f"{k}={v}" for k, v in summary["edges"].items()),
            f"  Pending confirmation queue: {summary['pending']}",
        ]
    )


def confirm_edge(conn: Connection, src: str, dst: str, type: str, extractor: str, new_status: str) -> str:
    """The human confirm/reject channel -- mirrors db.set_edge_status's
    human-only contract. Explicit "no such edge" message (no-op) when the
    (src, dst, type, extractor) row doesn't exist."""
    if type not in db.EDGE_TYPES:
        raise ValueError(f"unknown edge type: {type!r}")
    if new_status not in db.EDGE_STATUSES:
        raise ValueError(f"unknown edge status: {new_status!r}")
    existing = [e for e in db.query_edges(conn, src=src, dst=dst, type=type) if e["extractor"] == extractor]
    if not existing:
        return f"No such edge: {src} --{type}--> {dst} (extractor={extractor}); nothing changed."
    db.set_edge_status(conn, src, dst, type, extractor, new_status)
    return f"Edge {src} --{type}--> {dst} (extractor={extractor}) status set to {new_status!r}."


# -- FastMCP server assembly --------------------------------------------------


def build_server(project_root: str | Path) -> FastMCP:
    """Register the four tools against project_root's .rce/graph.db."""
    root = Path(project_root).resolve()
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
    def rce_confirm_edge(src: str, dst: str, type: str, extractor: str, new_status: str) -> str:
        """Human confirmation/rejection channel for exactly one edge. Call
        this ONLY when the user has explicitly asked to confirm, reject, or
        correct the status of a specific edge (e.g. "confirm that figure 4
        is backed by run xyz") -- never speculatively or during routine
        tracing. new_status must be one of: confirmed/rejected/pending/auto.
        States explicitly when no matching edge exists."""
        with _connect(root) as conn:
            return confirm_edge(conn, src, dst, type, extractor, new_status)

    return mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rce mcp", description="Run the RCE MCP stdio server.")
    parser.add_argument("--path", default=".", help="project root containing .rce/graph.db (default: '.')")
    args = parser.parse_args(argv)
    project_root = Path(args.path).resolve()
    try:
        _require_db(project_root)
        server = build_server(project_root)
    except McpServerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
