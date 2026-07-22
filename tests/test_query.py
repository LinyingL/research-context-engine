"""Tests for rce.query.trace (T5): multi-hop provenance traversal directly
against a migrated SQLite fixture (tests/conftest.py's `conn`) -- no MCP
server, no client involved.
"""

from rce import db, query


def _mk_nodes(conn, *pairs):
    for node_id, node_type in pairs:
        db.upsert_node(conn, node_id, node_type)


def _mk(conn, src, dst, edge_type, extractor="test", evidence=None, confidence=0.9, status="auto"):
    db.upsert_edge(
        conn, src, dst, edge_type, extractor, evidence or {"note": f"{src}->{dst}"}, confidence, status=status,
    )


def test_trace_missing_node_returns_empty_structure_not_error(conn):
    result = query.trace(conn, "figure:does-not-exist.png")
    assert result == {"node_id": "figure:does-not-exist.png", "found": False, "hops": []}


def test_trace_existing_node_with_no_edges_returns_found_empty_hops(conn):
    db.upsert_node(conn, "project:solo", "project")
    result = query.trace(conn, "project:solo")
    assert result == {"node_id": "project:solo", "found": True, "hops": []}


def test_trace_walks_upstream_and_paper_side_from_figure(conn):
    """Figure sits between upstream (Commit/Experiment) and paper-side
    (Section, then Reference two hops out)."""
    _mk_nodes(
        conn,
        ("commit:abc", "commit"), ("experiment:run1", "experiment"), ("figure:overview.png", "figure"),
        ("section:paper.tex#intro", "section"), ("reference:smith2020", "reference"),
        ("contributor:alice@example.com", "contributor"),
    )
    _mk(conn, "commit:abc", "experiment:run1", "implements")
    _mk(conn, "experiment:run1", "figure:overview.png", "produces")
    _mk(conn, "section:paper.tex#intro", "figure:overview.png", "includes")
    _mk(conn, "section:paper.tex#intro", "reference:smith2020", "cites")
    _mk(conn, "commit:abc", "contributor:alice@example.com", "authored_by")

    result = query.trace(conn, "figure:overview.png")
    assert result["found"] is True
    by_type = {(h["src"], h["dst"], h["type"]): h["depth"] for h in result["hops"]}
    # direct hops (depth 1)
    assert by_type[("experiment:run1", "figure:overview.png", "produces")] == 1
    assert by_type[("section:paper.tex#intro", "figure:overview.png", "includes")] == 1
    # two hops out: upstream through experiment to commit, paper-side through section to reference
    assert by_type[("commit:abc", "experiment:run1", "implements")] == 2
    assert by_type[("section:paper.tex#intro", "reference:smith2020", "cites")] == 2

    # each hop carries the full evidence-chain fields, verbatim.
    produces_hop = next(h for h in result["hops"] if h["type"] == "produces")
    assert produces_hop["extractor"] == "test" and produces_hop["status"] == "auto"
    assert produces_hop["confidence"] == 0.9
    # (T10) db.upsert_edge now wraps evidence as {"occurrences": [...]}.
    assert produces_hop["evidence"] == {"occurrences": [{"note": "experiment:run1->figure:overview.png"}]}


def test_trace_respects_max_hops(conn):
    _mk_nodes(conn, ("commit:abc", "commit"), ("experiment:run1", "experiment"), ("figure:overview.png", "figure"))
    _mk(conn, "commit:abc", "experiment:run1", "implements")
    _mk(conn, "experiment:run1", "figure:overview.png", "produces")

    result = query.trace(conn, "figure:overview.png", max_hops=1)
    assert {h["type"] for h in result["hops"]} == {"produces"}  # implements is 2 hops away, excluded


def test_trace_cycle_protection_terminates(conn):
    """A Figure that both includes-from and supports-to the same Section
    (a realistic 2-cycle) must not loop forever and must not double-count."""
    _mk_nodes(conn, ("figure:overview.png", "figure"), ("section:paper.tex#intro", "section"))
    _mk(conn, "section:paper.tex#intro", "figure:overview.png", "includes")
    _mk(conn, "figure:overview.png", "section:paper.tex#intro", "supports")

    result = query.trace(conn, "figure:overview.png", max_hops=4)
    # Only the two direct edges are ever reachable; visited-set stops the
    # bounce back and forth instead of re-walking it at every remaining depth.
    assert len(result["hops"]) == 2
    assert sorted(h["type"] for h in result["hops"]) == ["includes", "supports"]


def test_trace_collects_multiple_extractors_on_same_edge_pair(conn):
    _mk_nodes(conn, ("commit:abc", "commit"), ("figure:overview.png", "figure"))
    _mk(conn, "commit:abc", "figure:overview.png", "generates", extractor="pyfig-ast")
    _mk(conn, "commit:abc", "figure:overview.png", "generates", extractor="manual-note")

    result = query.trace(conn, "figure:overview.png")
    assert {h["extractor"] for h in result["hops"]} == {"pyfig-ast", "manual-note"}
