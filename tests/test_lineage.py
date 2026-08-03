"""Tests for rce.lineage (task W4): the read-only, four-block report over
`rce.ingest.dataflow`'s `script --reads/writes--> dataset|figure` edges.

Edges are built directly via db.upsert_node/upsert_edge rather than through
`rce.ingest.dataflow` itself -- this module's own contract (scoping,
sorting, empty-block behavior) is independent of how the edges got there,
and `tests/test_ingest_dataflow.py` already covers the extractor. Only the
duplicate-copy tests (block 4) need real files on `tmp_path`, since that
block walks the actual filesystem.
"""

from rce import db, lineage


def _add(conn, script, path, node_type, edge_type, line, callee, missing=False):
    """Write one script->target reads/writes edge, upserting both endpoint
    nodes first -- mirrors exactly what `rce.ingest.dataflow.
    ingest_dataflow_repo` itself writes for one recognized call site."""
    target_id = f"{node_type}:{path}"
    script_id = f"script:{script}"
    db.upsert_node(conn, script_id, "script", title=script)
    db.upsert_node(conn, target_id, node_type, title=path)
    evidence = {"file": script, "line": line, "callee": callee}
    if missing:
        evidence["missing"] = True
    db.upsert_edge(
        conn, script_id, target_id, edge_type, extractor="dataflow",
        evidence=evidence, confidence=1.0, status="auto",
    )
    return target_id


# ---------------------------------------------------------------------------
# Block 1: orphan inputs
# ---------------------------------------------------------------------------


def test_dataset_read_but_never_written_is_an_orphan(conn, tmp_path):
    _add(conn, "scripts/analyze.py", "data/raw.csv", "dataset", "reads", 10, "pd.read_csv")
    report = lineage.build_lineage_report(conn, tmp_path)
    assert [o["path"] for o in report["orphans"]] == ["data/raw.csv"]
    assert report["orphans"][0]["readers"] == [
        {"script": "scripts/analyze.py", "line": 10, "callee": "pd.read_csv"}
    ]
    assert report["chains"] == []


def test_figure_read_but_never_written_is_not_an_orphan(conn, tmp_path):
    """Orphans are scoped to `dataset` only -- see module docstring."""
    _add(conn, "scripts/x.py", "figs/plot.png", "figure", "reads", 5, "open")
    report = lineage.build_lineage_report(conn, tmp_path)
    assert report["orphans"] == []


def test_write_only_dataset_is_neither_orphan_nor_chain(conn, tmp_path):
    _add(conn, "scripts/gen.py", "data/out.csv", "dataset", "writes", 20, "to_csv")
    report = lineage.build_lineage_report(conn, tmp_path)
    assert report["orphans"] == []
    assert report["chains"] == []
    assert report["scanned"]["targets"] == 1


# ---------------------------------------------------------------------------
# Block 2: lineage chains
# ---------------------------------------------------------------------------


def test_chain_lists_every_writer_and_reader_sorted_by_script_then_line(conn, tmp_path):
    path = "data/topicshift_monthly.csv"
    _add(conn, "16-topicshift.py", path, "dataset", "writes", 87, "to_csv")
    _add(conn, "17-pricing.Rmd", path, "dataset", "reads", 38, "read_csv")
    _add(conn, "05-earlier.py", path, "dataset", "reads", 5, "read_csv")

    report = lineage.build_lineage_report(conn, tmp_path)
    assert len(report["chains"]) == 1
    chain = report["chains"][0]
    assert chain["path"] == path
    assert chain["writers"] == [{"script": "16-topicshift.py", "line": 87, "callee": "to_csv"}]
    assert chain["readers"] == [
        {"script": "05-earlier.py", "line": 5, "callee": "read_csv"},
        {"script": "17-pricing.Rmd", "line": 38, "callee": "read_csv"},
    ]
    assert report["orphans"] == []


def test_figure_with_writer_and_reader_is_a_chain(conn, tmp_path):
    _add(conn, "gen.py", "figs/plot.png", "figure", "writes", 12, "savefig")
    _add(conn, "report.Rmd", "figs/plot.png", "figure", "reads", 3, "include_graphics")
    report = lineage.build_lineage_report(conn, tmp_path)
    assert [c["path"] for c in report["chains"]] == ["figs/plot.png"]


# ---------------------------------------------------------------------------
# Block 3: broken links (evidence.missing == True)
# ---------------------------------------------------------------------------


def test_broken_links_cover_both_reads_and_writes_sorted(conn, tmp_path):
    _add(conn, "b.py", "data/out.csv", "dataset", "writes", 9, "to_csv", missing=True)
    _add(conn, "a.py", "data/gone.csv", "dataset", "reads", 3, "read_csv", missing=True)
    _add(conn, "c.py", "data/ok.csv", "dataset", "reads", 1, "read_csv")  # not missing

    report = lineage.build_lineage_report(conn, tmp_path)
    assert report["broken_links"] == [
        {"script": "a.py", "line": 3, "callee": "read_csv", "kind": "reads", "target": "data/gone.csv"},
        {"script": "b.py", "line": 9, "callee": "to_csv", "kind": "writes", "target": "data/out.csv"},
    ]


def test_no_broken_links_when_nothing_is_flagged_missing(conn, tmp_path):
    _add(conn, "a.py", "data/ok.csv", "dataset", "reads", 1, "read_csv")
    report = lineage.build_lineage_report(conn, tmp_path)
    assert report["broken_links"] == []


# ---------------------------------------------------------------------------
# Block 4: duplicate copies
# ---------------------------------------------------------------------------


def test_duplicate_copies_lists_every_other_same_named_file(conn, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "theme_counts.csv").write_text("x\n")
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "theme_counts.csv").write_text("y\n")
    (tmp_path / "backup").mkdir()
    (tmp_path / "backup" / "theme_counts.csv").write_text("z\n")
    _add(conn, "scripts/read.py", "data/theme_counts.csv", "dataset", "reads", 4, "read_csv")

    report = lineage.build_lineage_report(conn, tmp_path)
    assert report["duplicates"] == [{
        "target": "dataset:data/theme_counts.csv",
        "path": "data/theme_counts.csv",
        "other_copies": ["archive/theme_counts.csv", "backup/theme_counts.csv"],
    }]


def test_no_duplicate_entry_when_file_is_unique(conn, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "unique.csv").write_text("x\n")
    _add(conn, "scripts/read.py", "data/unique.csv", "dataset", "reads", 4, "read_csv")
    report = lineage.build_lineage_report(conn, tmp_path)
    assert report["duplicates"] == []


def test_duplicate_scan_is_scoped_to_dataset_not_figure(conn, tmp_path):
    (tmp_path / "figs").mkdir()
    (tmp_path / "figs" / "plot.png").write_bytes(b"\x89PNG")
    (tmp_path / "figs2").mkdir()
    (tmp_path / "figs2" / "plot.png").write_bytes(b"\x89PNG")
    _add(conn, "scripts/read.py", "figs/plot.png", "figure", "reads", 4, "open")
    report = lineage.build_lineage_report(conn, tmp_path)
    assert report["duplicates"] == []


def test_duplicate_scan_skips_noise_and_hidden_directories(conn, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.csv").write_text("a\n")
    (tmp_path / ".rce").mkdir()
    (tmp_path / ".rce" / "x.csv").write_text("b\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.csv").write_text("c\n")
    _add(conn, "scripts/read.py", "data/x.csv", "dataset", "reads", 1, "read_csv")
    report = lineage.build_lineage_report(conn, tmp_path)
    assert report["duplicates"] == []


# ---------------------------------------------------------------------------
# Whole-report shape: empty graph, scan counters
# ---------------------------------------------------------------------------


def test_empty_graph_reports_zero_scan_and_every_block_empty(conn, tmp_path):
    report = lineage.build_lineage_report(conn, tmp_path)
    assert report == {
        "scanned": {"scripts": 0, "reads_edges": 0, "writes_edges": 0, "targets": 0},
        "orphans": [], "chains": [], "broken_links": [], "duplicates": [],
    }


def test_scanned_counts_reflect_scripts_and_edges(conn, tmp_path):
    _add(conn, "a.py", "data/x.csv", "dataset", "reads", 1, "read_csv")
    _add(conn, "b.py", "data/x.csv", "dataset", "writes", 2, "to_csv")
    _add(conn, "a.py", "data/y.csv", "dataset", "writes", 3, "to_csv")

    report = lineage.build_lineage_report(conn, tmp_path)
    assert report["scanned"] == {
        "scripts": 2, "reads_edges": 1, "writes_edges": 2, "targets": 2,
    }
