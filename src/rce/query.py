"""Multi-hop provenance tracing over the RCE graph (T5).

Given a node id, walks outward along the provenance-semantic edge types and
returns a flat, evidence-carrying hop list -- the code behind the product's
north-star query ("where did this result come from?"). Independent of MCP:
rce.mcp_server calls trace(), but so could the CLI or a future web view.
Reads only through rce.db's public functions (get_node/query_edges) -- no
raw SQL here, per db.py's module contract.

HANDOFF-SPEC.md section 4 groups the traversable types as "upstream" (source
direction: implements/produces/generates, e.g. Commit->Experiment->Figure)
and "paper-side" (citation direction: includes/cites/backed_by/supports,
e.g. Section->Figure, Section->Reference); authored_by rides along
incidentally since Contributor nodes are dead ends. Rather than hard-coding
a direction per type (which would require knowing which node type the start
is), trace() looks at edges of these types touching the current node in
*either* direction and follows to the other endpoint -- that's what makes
the walk bidirectional without the caller needing to know which side of an
edge type the start node sits on.
"""

from __future__ import annotations

from typing import Any

from rce import db

# Edge types that make up a provenance chain (HANDOFF-SPEC.md section 4).
UPSTREAM_EDGE_TYPES = frozenset({"implements", "produces", "generates"})
PAPER_EDGE_TYPES = frozenset({"includes", "cites", "backed_by", "supports"})
TRACE_EDGE_TYPES = UPSTREAM_EDGE_TYPES | PAPER_EDGE_TYPES | {"authored_by"}


def trace(conn, node_id: str, max_hops: int = 4) -> dict[str, Any]:
    """Walk the provenance graph outward from node_id, breadth-first.

    Returns {"node_id", "found": bool, "hops": [{"depth", "src", "dst",
    "type", "extractor", "confidence", "status", "evidence"}, ...]}.
    "found" is False (hops empty) if node_id doesn't exist -- never
    fabricated. If the node exists with no provenance edges, found=True and
    hops=[].

    Two separate sets guard against cycles, not one: `visited` (nodes)
    controls BFS *expansion* -- once reached, a node is never re-expanded
    into, so a real cycle (e.g. Figure --supports--> Section that also
    --includes--> the same Figure) terminates. `recorded` (edge keys) only
    dedupes hops -- so two distinct edges landing on the same already-visited
    neighbor (two extractors both asserting the same edge type, or the
    cycle's reverse edge) are still both reported as evidence.
    """
    if db.get_node(conn, node_id) is None:
        return {"node_id": node_id, "found": False, "hops": []}

    hops: list[dict[str, Any]] = []
    visited = {node_id}
    recorded: set[tuple[str, str, str, str]] = set()
    frontier = {node_id}

    for depth in range(1, max_hops + 1):
        if not frontier:
            break
        next_frontier: set[str] = set()
        for current in frontier:
            touching = db.query_edges(conn, src=current) + db.query_edges(conn, dst=current)
            for edge in touching:
                if edge["type"] not in TRACE_EDGE_TYPES:
                    continue
                edge_key = (edge["src"], edge["dst"], edge["type"], edge["extractor"])
                if edge_key in recorded:
                    continue
                recorded.add(edge_key)
                hops.append({"depth": depth, **{k: edge[k] for k in
                    ("src", "dst", "type", "extractor", "confidence", "status", "evidence")}})
                neighbor = edge["dst"] if edge["src"] == current else edge["src"]
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.add(neighbor)
        frontier = next_frontier

    return {"node_id": node_id, "found": True, "hops": hops}
