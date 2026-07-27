"""Tests for rce.mcp_server (T5): plain functions (trace_result/find_nodes/
status_summary/confirm_edge + their format_* renderers) called directly
against tests/conftest.py's `conn` fixture -- no stdio/client/network.
build_server()/main() get a thin smoke test only (tool registration/schema,
and main()'s missing-project error path) without starting the stdio loop.
"""

import asyncio

import pytest

from rce import db, mcp_server


def _mk(conn, src, dst, edge_type, extractor="test", evidence=None, confidence=0.9, status="auto"):
    db.upsert_edge(
        conn, src, dst, edge_type, extractor, evidence or {"note": f"{src}->{dst}"}, confidence, status=status,
    )


def test_trace_result_missing_node(conn):
    result = mcp_server.trace_result(conn, "figure:missing.png")
    assert result["found"] is False
    assert "No such node" in mcp_server.format_trace_text("figure:missing.png", result)


def test_trace_result_and_text_for_real_chain(conn):
    db.upsert_node(conn, "experiment:run1", "experiment")
    db.upsert_node(conn, "figure:overview.png", "figure")
    _mk(conn, "experiment:run1", "figure:overview.png", "produces", extractor="mlflow")

    result = mcp_server.trace_result(conn, "figure:overview.png")
    assert result["found"] is True and len(result["hops"]) == 1
    text = mcp_server.format_trace_text("figure:overview.png", result)
    assert "experiment:run1 --produces--> figure:overview.png" in text
    assert "extractor=mlflow" in text and "confidence=0.90" in text


def test_trace_result_node_with_no_edges_says_so(conn):
    db.upsert_node(conn, "project:solo", "project")
    result = mcp_server.trace_result(conn, "project:solo")
    assert "no provenance edges" in mcp_server.format_trace_text("project:solo", result)


def test_trace_result_and_text_show_current_claim_line_for_backed_by(conn):
    """Regression (2026-07-27): rce_trace must recover a backed_by hop's
    claim line via `hop["source_location"]` (rce.query.trace, see
    rce.query.claim_source_location) even though rce.ingest.claims no
    longer writes "line" into the edge's persisted evidence at all -- a
    prior commit fixed this only for `rce status --pending`'s own display,
    silently leaving the MCP `rce_trace` tool (and `rce trace`/`--json`)
    without the line."""
    db.upsert_node(
        conn, "claim:paper.tex#abc", "claim",
        attrs={"tex_path": "paper.tex", "line": 2, "sentence": "We reach 87.3% accuracy."},
    )
    db.upsert_node(conn, "experiment:run_a", "experiment")
    _mk(
        conn, "claim:paper.tex#abc", "experiment:run_a", "backed_by", extractor="claims",
        evidence={"file": "paper.tex", "metric": "accuracy", "metric_value": 0.873},
        status="pending",
    )

    result = mcp_server.trace_result(conn, "experiment:run_a")
    hop = next(h for h in result["hops"] if h["type"] == "backed_by")
    assert hop["source_location"] == {"file": "paper.tex", "line": 2}
    assert "line" not in hop["evidence"]["occurrences"][0]

    text = mcp_server.format_trace_text("experiment:run_a", result)
    assert "source_location=paper.tex:2" in text


def test_find_nodes_case_insensitive_substring(conn):
    db.upsert_node(conn, "figure:Overview.png", "figure", title="Overview Figure")
    db.upsert_node(conn, "figure:other.png", "figure", title="Something else")
    matches = mcp_server.find_nodes(conn, "overview")
    assert [m["id"] for m in matches] == ["figure:Overview.png"]


def test_find_nodes_filters_by_type(conn):
    db.upsert_node(conn, "figure:result.png", "figure")
    db.upsert_node(conn, "experiment:result_run", "experiment")
    matches = mcp_server.find_nodes(conn, "result", node_type="experiment")
    assert [m["id"] for m in matches] == ["experiment:result_run"]


def test_find_nodes_rejects_unknown_type(conn):
    with pytest.raises(ValueError):
        mcp_server.find_nodes(conn, "x", node_type="meeting")


def test_find_nodes_no_match_says_so(conn):
    db.upsert_node(conn, "figure:a.png", "figure")
    matches = mcp_server.find_nodes(conn, "zzz-nope")
    assert matches == []
    assert "No nodes matching" in mcp_server.format_find_text("zzz-nope", None, matches)


def test_status_summary_counts_and_pending_queue(conn):
    db.upsert_node(conn, "claim:paper.tex#abc", "claim")
    db.upsert_node(conn, "experiment:run1", "experiment")
    _mk(conn, "claim:paper.tex#abc", "experiment:run1", "backed_by", extractor="7b-judge", status="pending")

    summary = mcp_server.status_summary(conn)
    assert summary["nodes"]["claim"] == 1 and summary["nodes"]["experiment"] == 1
    assert summary["edges"]["backed_by"] == 1 and summary["pending"] == 1
    assert "Pending confirmation queue: 1" in mcp_server.format_status_text(summary)


def test_confirm_edge_moves_pending_to_confirmed(conn):
    db.upsert_node(conn, "claim:paper.tex#abc", "claim")
    db.upsert_node(conn, "experiment:run1", "experiment")
    _mk(conn, "claim:paper.tex#abc", "experiment:run1", "backed_by", extractor="7b-judge", status="pending")

    message = mcp_server.confirm_edge(
        conn, "claim:paper.tex#abc", "experiment:run1", "backed_by", "7b-judge", "confirmed",
    )
    assert "status set to 'confirmed'" in message
    edges = db.query_edges(conn, src="claim:paper.tex#abc", dst="experiment:run1", type="backed_by")
    assert edges[0]["status"] == "confirmed"


def test_confirm_edge_no_such_edge_says_so(conn):
    message = mcp_server.confirm_edge(
        conn, "claim:missing", "experiment:missing", "backed_by", "7b-judge", "confirmed",
    )
    assert "No such edge" in message
    assert db.query_edges(conn) == []


def test_confirm_edge_rejects_unknown_type_and_status(conn):
    db.upsert_node(conn, "claim:x", "claim")
    db.upsert_node(conn, "experiment:y", "experiment")
    with pytest.raises(ValueError):
        mcp_server.confirm_edge(conn, "claim:x", "experiment:y", "haunts", "test", "confirmed")
    with pytest.raises(ValueError):
        mcp_server.confirm_edge(conn, "claim:x", "experiment:y", "backed_by", "test", "in_review")


def test_build_server_registers_tools_with_legal_schemas(tmp_path):
    project = tmp_path / "proj"
    (project / ".rce").mkdir(parents=True)
    conn = db.connect(project / ".rce" / "graph.db")
    try:
        db.migrate(conn)
    finally:
        conn.close()

    tools = asyncio.run(mcp_server.build_server(project).list_tools())
    assert {t.name for t in tools} == {"rce_trace", "rce_find", "rce_status", "rce_confirm_edge"}
    for tool in tools:
        assert tool.description and tool.inputSchema["type"] == "object" and "properties" in tool.inputSchema

    trace_tool = next(t for t in tools if t.name == "rce_trace")
    assert "node_id" in trace_tool.inputSchema["required"]
    confirm_tool = next(t for t in tools if t.name == "rce_confirm_edge")
    assert set(confirm_tool.inputSchema["required"]) == {"src", "dst", "type", "extractor", "new_status"}


def test_main_reports_clear_error_for_missing_project(tmp_path, capsys):
    project = tmp_path / "no-such-project"
    project.mkdir()
    assert mcp_server.main(["--path", str(project)]) == 1
    err = capsys.readouterr().err
    assert "Error" in err and "rce init" in err
