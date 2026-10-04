"""Tests for rce.ingest.scan -- what a scan must report (DESIGN.md 9.6,
task V5 phase 3): per-source status, per-scan basis, and the queries the
judgment ledger will ask. Numbers in docstrings name the 9.9 acceptance
scenario a test covers the scan half of (the judgment half is phase 4).
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from rce import db
from rce.ingest import dataflow
from rce.ingest import scan as scan_mod


def _edge(src, dst, type_="reads", extractor="dataflow"):
    return {"src": src, "dst": dst, "type": type_, "extractor": extractor}


def _row(conn, edge):
    return db.edge_scan_row(conn, edge["src"], edge["dst"], edge["type"], edge["extractor"])


def _dataflow_scan(conn, root):
    """A dataflow scan over every .py/.R/.Rmd file under `root`, with the
    filesystem inventory recorded -- what the watcher's dataflow slice runs."""
    from rce.ingest import files as files_ingest

    inventory = files_ingest.list_source_files(root)
    with scan_mod.scan(conn, "test") as sc:
        sc.inventory(inventory)
        dataflow.ingest_dataflow_repo(conn, root, inventory["py"], inventory["r"], inventory["rmd"], scan=sc)
    return sc


READ = _edge("script:a.py", "dataset:data/in.csv")


def _project(tmp_path, script="import pandas as pd\ndf = pd.read_csv('data/in.csv')\n"):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "in.csv").write_text("x\n1\n")
    (tmp_path / "a.py").write_text(script)
    return tmp_path


# -- basis ---------------------------------------------------------------------


def test_bare_call_name_drops_receiver_and_package_but_keeps_r_dots():
    assert scan_mod.bare_call_name("pd.read_csv") == "read_csv"
    assert scan_mod.bare_call_name("df.to_csv") == "to_csv"
    assert scan_mod.bare_call_name("open") == "open"
    assert scan_mod.bare_call_name("read.csv", language="r") == "read.csv"
    assert scan_mod.bare_call_name("haven::read_dta", language="r") == "read_dta"


def test_basis_table_per_extractor():
    assert scan_mod.basis("dataflow", "reads", call="read_csv") == {"calls": ["read_csv"]}
    assert scan_mod.basis("pyfig", "generates", call="savefig") == {"calls": ["savefig"]}
    assert scan_mod.basis("mlflow", "produces", artifact="plots/f.png") == {"artifacts": ["plots/f.png"]}
    assert scan_mod.basis("wandb", "produces", artifact="media/f.png") == {"artifacts": ["media/f.png"]}
    claims = scan_mod.basis("claims", "backed_by", sentence="acc is 87.3%.", number="87.3%", metrics={"acc": "0.873"})
    assert claims == {"sentence": "acc is 87.3%.", "number": "87.3%", "metrics": {"acc": "0.873"}}
    for extractor, type_ in (("latex", "includes"), ("latex", "cites"), ("mdpaper", "includes"),
                             ("git", "authored_by"), ("attempts_consistency", "uses"),
                             ("mlflow", "implements")):
        assert scan_mod.basis(extractor, type_) == {}


def test_canonical_basis_sorts_keys_and_merge_unions_lists():
    assert db.canonical_basis({"b": 1, "a": ["x"]}) == '{"a":["x"],"b":1}'
    assert db.canonical_basis({}) == "{}"
    assert db.canonical_basis(None) is None
    merged = db.merge_basis({"calls": ["read_csv"], "metrics": {"a": "1"}}, {"calls": ["open", "read_csv"], "metrics": {"b": "2"}})
    assert merged == {"calls": ["open", "read_csv"], "metrics": {"a": "1", "b": "2"}}


# -- dataflow: per-source status -----------------------------------------------


def test_dataflow_reports_read_and_parsed_and_stamps_basis(conn, tmp_path):
    root = _project(tmp_path)
    sc = _dataflow_scan(conn, root)
    assert scan_mod.source_status(conn, READ) == scan_mod.READ_AND_PARSED
    assert scan_mod.produced_in_latest_scan(conn, READ) is True
    assert scan_mod.current_basis(conn, READ) == {"calls": ["read_csv"]}
    assert scan_mod.endpoints_present(conn, READ) is True
    scan_row = db.get_scan(conn, sc.id)
    assert scan_row["outcome"] == "finished"
    assert "dataflow" in scan_row["extractors"]
    assert scan_row["source_counts"]["dataflow"] == {"read_and_parsed": 1}


def test_python_syntax_error_is_unparseable_not_no_calls(conn, tmp_path):
    """9.9 scenario 8(e), unparseable: nothing comes under review -- the
    source reports UNPARSEABLE and the link keeps its previous state."""
    root = _project(tmp_path)
    _dataflow_scan(conn, root)
    first = _row(conn, READ)
    (root / "a.py").write_text("def broken(:\n    pd.read_csv('data/in.csv')\n")
    _dataflow_scan(conn, root)
    assert scan_mod.source_status(conn, READ) == scan_mod.UNPARSEABLE
    assert scan_mod.produced_in_latest_scan(conn, READ) is None
    after = _row(conn, READ)
    assert (after["scan_seen"], after["scan_basis"], after["scan_lost"]) == (
        first["scan_seen"], first["scan_basis"], None,
    )
    # Distinguished from a file with no calls at all:
    (root / "a.py").write_text("x = 1\n")
    _dataflow_scan(conn, root)
    assert scan_mod.source_status(conn, READ) == scan_mod.READ_AND_PARSED
    assert scan_mod.produced_in_latest_scan(conn, READ) is False


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions, non-root")
def test_unreadable_script_reports_unreadable_and_keeps_previous_state(conn, tmp_path):
    """9.9 scenario 8(e), unreadable."""
    root = _project(tmp_path)
    _dataflow_scan(conn, root)
    first = _row(conn, READ)
    (root / "a.py").chmod(0)
    try:
        _dataflow_scan(conn, root)
        assert scan_mod.source_status(conn, READ) == scan_mod.UNREADABLE
        assert scan_mod.produced_in_latest_scan(conn, READ) is None
        assert _row(conn, READ)["scan_seen"] == first["scan_seen"]
        assert scan_mod.endpoints_present(conn, READ) is True
    finally:
        (root / "a.py").chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_r_unbalanced_parentheses_make_the_file_unparseable(conn, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "in.csv").write_text("x\n")
    (tmp_path / "an.R").write_text('d <- read.csv("data/in.csv")\nwrite.csv(d, "data/out.csv"\n')
    outcome = dataflow.scan_r_file(tmp_path, "an.R")
    assert outcome.status == scan_mod.UNPARSEABLE
    assert [c.callee for c in outcome.calls] == ["read.csv"]  # still written, as before
    _dataflow_scan(conn, tmp_path)
    edge = _edge("script:an.R", "dataset:data/in.csv")
    assert db.query_edges(conn, src="script:an.R")  # views unchanged
    assert scan_mod.source_status(conn, edge) == scan_mod.NOT_SCANNED  # never stamped
    assert _row(conn, edge)["scan_seen"] is None


def test_rmd_reports_read_and_parsed_with_r_bare_names(conn, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "panel.dta").write_text("")
    (tmp_path / "r.Rmd").write_text('text\n```{r}\nx <- haven::read_dta("data/panel.dta")\n```\n')
    _dataflow_scan(conn, tmp_path)
    edge = _edge("script:r.Rmd", "dataset:data/panel.dta")
    assert scan_mod.source_status(conn, edge) == scan_mod.READ_AND_PARSED
    assert scan_mod.current_basis(conn, edge) == {"calls": ["read_dta"]}


# -- dataflow: basis stability (9.9 scenario 8a/8b) -------------------------------


def test_basis_survives_line_insertion_receiver_rename_and_import_alias(conn, tmp_path):
    """9.9 scenario 8(a): lines inserted above, the receiving variable and
    the import alias renamed -- the basis is unchanged, the link produced."""
    root = _project(tmp_path)
    _dataflow_scan(conn, root)
    before = _row(conn, READ)["scan_basis"]
    for script in (
        "# a comment\n\nimport pandas as pd\ndf = pd.read_csv('data/in.csv')\n",
        "# a comment\n\nimport pandas as pd\nframe = pd.read_csv('data/in.csv')\n",
        "# a comment\n\nimport pandas as pandas_lib\nframe = pandas_lib.read_csv('data/in.csv')\n",
    ):
        (root / "a.py").write_text(script)
        _dataflow_scan(conn, root)
        assert _row(conn, READ)["scan_basis"] == before
        assert scan_mod.produced_in_latest_scan(conn, READ) is True
        assert _row(conn, READ)["scan_lost"] is None
    assert len(db.query_edges(conn, src="script:a.py")[0]["evidence"]["occurrences"]) > 1  # unchanged accumulation


def test_basis_changes_when_the_called_function_changes(conn, tmp_path):
    """9.9 scenario 8(b): a different function reads the same file."""
    root = _project(tmp_path)
    _dataflow_scan(conn, root)
    (root / "a.py").write_text("text = open('data/in.csv').read()\n")
    _dataflow_scan(conn, root)
    assert scan_mod.produced_in_latest_scan(conn, READ) is True
    assert scan_mod.current_basis(conn, READ) == {"calls": ["open"]}


def test_two_calls_in_one_scan_merge_into_one_basis(conn, tmp_path):
    root = _project(tmp_path, "import pandas as pd\na = pd.read_csv('data/in.csv')\nb = open('data/in.csv')\n")
    _dataflow_scan(conn, root)
    assert scan_mod.current_basis(conn, READ) == {"calls": ["open", "read_csv"]}


def test_deleted_call_is_not_produced_while_both_ends_are_present(conn, tmp_path):
    """9.9 scenario 8(c): the call is removed -- both ends are still in the
    scan (via the inventory), the link is not; putting it back produces it
    again on the same basis."""
    root = _project(tmp_path)
    _dataflow_scan(conn, root)
    basis_before = _row(conn, READ)["scan_basis"]
    (root / "a.py").write_text("import pandas as pd\n")
    lost_scan = _dataflow_scan(conn, root)
    assert scan_mod.source_status(conn, READ) == scan_mod.READ_AND_PARSED
    assert scan_mod.produced_in_latest_scan(conn, READ) is False
    assert scan_mod.current_basis(conn, READ) is None
    assert scan_mod.endpoints_present(conn, READ) is True
    assert _row(conn, READ)["scan_lost"] == lost_scan.id
    assert db.query_edges(conn, src="script:a.py")  # 9.6: views unchanged, the link stays
    (root / "a.py").write_text("import pandas as pd\ndf = pd.read_csv('data/in.csv')\n")
    back = _dataflow_scan(conn, root)
    assert scan_mod.produced_in_latest_scan(conn, READ) is True
    assert _row(conn, READ)["scan_basis"] == basis_before
    assert _row(conn, READ)["scan_lost"] is None
    assert _row(conn, READ)["scan_appeared"] == back.id


def test_renamed_script_is_absent_and_its_new_read_is_a_candidate(conn, tmp_path):
    """9.9 scenario 8(d): rename the script -- the old link's source is
    ABSENT (an observation), one end is not in the scan, and the new read
    of the same file with the same basis is offered as a candidate."""
    root = _project(tmp_path)
    _dataflow_scan(conn, root)
    (root / "a.py").rename(root / "b.py")
    renamed = _dataflow_scan(conn, root)
    assert scan_mod.source_status(conn, READ) == scan_mod.ABSENT
    assert scan_mod.produced_in_latest_scan(conn, READ) is False
    assert scan_mod.endpoints_present(conn, READ) is False
    assert scan_mod.node_present(conn, "script:a.py") is False
    assert scan_mod.node_present(conn, "dataset:data/in.csv") is True
    new_read = _edge("script:b.py", "dataset:data/in.csv")
    assert _row(conn, new_read)["scan_appeared"] == renamed.id
    candidates = scan_mod.new_links_like(conn, READ)
    assert [(c["src"], c["dst"]) for c in candidates] == [("script:b.py", "dataset:data/in.csv")]
    # Nothing is carried across: the old link is untouched in the index.
    assert db.query_edges(conn, src="script:a.py")


def test_candidates_need_the_same_basis(conn, tmp_path):
    root = _project(tmp_path)
    _dataflow_scan(conn, root)
    (root / "a.py").rename(root / "b.py")
    (root / "b.py").write_text("t = open('data/in.csv')\n")
    _dataflow_scan(conn, root)
    assert scan_mod.new_links_like(conn, READ) == []


def test_repeated_full_scans_are_idempotent_on_scan_basis(conn, tmp_path):
    """9.9 scenario 6, the scan half: three scans, identical bases, nothing lost."""
    root = _project(tmp_path, "import pandas as pd\nd = pd.read_csv('data/in.csv')\nd.to_csv('data/out.csv')\n")
    snapshots = []
    for _ in range(3):
        _dataflow_scan(conn, root)
        snapshots.append(sorted(
            (e["src"], e["dst"], e["type"], e["scan_basis"], e["scan_lost"], e["scan_appeared"])
            for e in db.query_edges(conn)
        ))
    assert snapshots[0] == snapshots[1] == snapshots[2]
    assert all(lost is None for *_, lost, _appeared in snapshots[0])


def test_partial_scan_leaves_unrelated_sources_untouched(conn, tmp_path):
    """A scan speaks only for the sources it read: a dataflow scan of one
    file says nothing about another file's links."""
    root = _project(tmp_path)
    (root / "c.py").write_text("open('data/in.csv')\n")
    _dataflow_scan(conn, root)
    other = _edge("script:c.py", "dataset:data/in.csv")
    before = _row(conn, other)
    status_before = db.scan_source_row(conn, "dataflow", "c.py")
    (root / "a.py").write_text("x = 1\n")
    dataflow.ingest_dataflow_repo(conn, root, ["a.py"], [], [])  # its own, partial scan
    assert _row(conn, other) == before
    assert db.scan_source_row(conn, "dataflow", "c.py") == status_before
    assert scan_mod.produced_in_latest_scan(conn, other) is True
    assert scan_mod.produced_in_latest_scan(conn, READ) is False


def test_a_failed_scan_records_only_failure_statuses(conn, tmp_path):
    root = _project(tmp_path)
    _dataflow_scan(conn, root)
    with pytest.raises(RuntimeError):
        with scan_mod.scan(conn, "boom") as sc:
            sc.ran("dataflow")
            sc.source("dataflow", "a.py", scan_mod.READ_AND_PARSED)
            sc.source("dataflow", "z.py", scan_mod.UNREADABLE)
            raise RuntimeError("mid-scan failure")
    assert db.get_scan(conn, sc.id)["outcome"] == "failed"
    assert db.scan_source_row(conn, "dataflow", "a.py")["last_scan"] != sc.id
    assert db.scan_source_row(conn, "dataflow", "z.py")["status"] == scan_mod.UNREADABLE
    assert scan_mod.produced_in_latest_scan(conn, READ) is True


def test_scan_stamps_never_touch_evidence_or_status(conn, tmp_path):
    root = _project(tmp_path)
    _dataflow_scan(conn, root)
    db.set_edge_status(conn, *READ.values(), "confirmed")
    (root / "a.py").write_text("x = 1\n")
    _dataflow_scan(conn, root)
    edge = db.query_edges(conn, src="script:a.py")[0]
    assert edge["status"] == "confirmed"
    assert set(edge["evidence"]) == {"occurrences"}
    assert json.loads(_row(conn, READ)["scan_basis"]) == {"calls": ["read_csv"]}


# -- the full pipeline: claims, mlflow, latex, mdpaper ---------------------------

from rce.ingest import pipeline  # noqa: E402


def _metric_run(root, run_id="run1", value="0.873", meta=True):
    run_dir = root / "mlruns" / "0" / run_id
    (run_dir / "metrics").mkdir(parents=True, exist_ok=True)
    if meta:
        (run_dir / "meta.yaml").write_text(f"experiment_id: '0'\nrun_id: {run_id}\nstatus: FINISHED\n")
    (run_dir / "metrics" / "accuracy").write_text(f"1700000000000 {value} 0\n")
    return run_dir


def _paper(root, body="## 结果\n\n准确率为 87.3%。其余部分不变。\n"):
    (root / "paper.md").write_text(body)


def _claim_edge(conn):
    [claim] = db.get_nodes_by_type(conn, "claim")
    return _edge(claim["id"], "experiment:run1", "backed_by", "claims")


def test_claims_basis_and_compound_source_from_one_ingest_run(conn, tmp_path):
    _paper(tmp_path)
    _metric_run(tmp_path)
    pipeline.ingest_sources(conn, tmp_path)
    edge = _claim_edge(conn)
    assert scan_mod.source_status(conn, edge) == scan_mod.READ_AND_PARSED
    assert _row(conn, edge)["scan_source"] == scan_mod.claims_source("paper.md", "mlflow:mlruns")
    assert scan_mod.current_basis(conn, edge) == {
        "sentence": "准确率为 87.3%。", "number": "87.3%", "metrics": {"accuracy": "0.873"},
    }
    assert scan_mod.node_present(conn, "experiment:run1") is True
    assert db.scan_source_row(conn, "mlflow", "mlflow:mlruns")["status"] == scan_mod.READ_AND_PARSED
    assert db.scan_source_row(conn, "mdpaper", "paper.md")["status"] == scan_mod.READ_AND_PARSED


def test_metric_that_stops_rounding_makes_the_link_not_produced_with_both_ends_present(conn, tmp_path):
    """9.9 scenario 8(c), the metric half: change the metric so it no longer
    rounds to the claim's number -- both ends are in the scan, the link is
    not; put it back -- produced again on the same basis."""
    _paper(tmp_path)
    _metric_run(tmp_path)
    pipeline.ingest_sources(conn, tmp_path)
    edge = _claim_edge(conn)
    basis_before = _row(conn, edge)["scan_basis"]
    _metric_run(tmp_path, value="0.95")
    pipeline.ingest_sources(conn, tmp_path)
    assert scan_mod.source_status(conn, edge) == scan_mod.READ_AND_PARSED
    assert scan_mod.produced_in_latest_scan(conn, edge) is False
    assert scan_mod.endpoints_present(conn, edge) is True
    assert db.query_edges(conn, src=edge["src"], type="backed_by")  # views unchanged
    _metric_run(tmp_path, value="0.8731")
    pipeline.ingest_sources(conn, tmp_path)
    assert scan_mod.produced_in_latest_scan(conn, edge) is True
    assert _row(conn, edge)["scan_basis"] == basis_before  # rounded to the printed precision


def test_claims_against_experiments_not_read_this_run_keep_their_state(conn, tmp_path):
    """9.6: the claims basis is computed only against experiments read in
    the same ingest run -- a claims-only scan speaks for no backed_by link."""
    _paper(tmp_path)
    _metric_run(tmp_path)
    pipeline.ingest_sources(conn, tmp_path)
    edge = _claim_edge(conn)
    before = _row(conn, edge)
    from rce.ingest import mdpaper

    mdpaper.ingest_md_repo(conn, tmp_path, ["paper.md"])  # its own scan: no store read
    assert _row(conn, edge) == before
    assert scan_mod.produced_in_latest_scan(conn, edge) is True


def test_corrupt_tracking_run_makes_the_store_and_its_claims_unparseable(conn, tmp_path):
    _paper(tmp_path)
    _metric_run(tmp_path)
    pipeline.ingest_sources(conn, tmp_path)
    edge = _claim_edge(conn)
    _metric_run(tmp_path, run_id="run_broken", meta=False)
    _metric_run(tmp_path, value="0.95")
    pipeline.ingest_sources(conn, tmp_path)
    assert db.scan_source_row(conn, "mlflow", "mlflow:mlruns")["status"] == scan_mod.UNPARSEABLE
    assert scan_mod.source_status(conn, edge) == scan_mod.UNPARSEABLE
    assert scan_mod.produced_in_latest_scan(conn, edge) is None


def test_reworded_claim_is_removed_and_its_stamps_kept(conn, tmp_path):
    """9.1: the orphan claim goes; 9.6 still knows what its link rested on
    and when it stopped being produced."""
    _paper(tmp_path, "## 结果\n\n准确率为 87.3%。\n")
    _metric_run(tmp_path)
    pipeline.ingest_sources(conn, tmp_path)
    edge = _claim_edge(conn)
    _paper(tmp_path, "## 结果\n\n模型准确率达到 87.3%。\n")
    pipeline.ingest_sources(conn, tmp_path)
    assert db.get_node(conn, edge["src"]) is None
    kept = _row(conn, edge)
    assert kept["removed"] is True and kept["scan_lost"] is not None
    assert scan_mod.produced_in_latest_scan(conn, edge) is False
    assert scan_mod.endpoints_present(conn, edge) is False
    assert scan_mod.last_basis(conn, edge)["sentence"] == "准确率为 87.3%。"


def test_latex_reports_per_file_and_stamps_identity_basis(conn, tmp_path):
    (tmp_path / "fig.png").write_bytes(b"\x89PNG")
    (tmp_path / "main.tex").write_text("\\section{Intro}\n\\includegraphics{fig.png}\n\\cite{key}\n")
    (tmp_path / "refs.bib").write_text("@article{key, title={T}}\n")
    pipeline.ingest_sources(conn, tmp_path)
    include = _edge("section:main.tex#intro", "figure:fig.png", "includes", "latex")
    cite = _edge("section:main.tex#intro", "ref:key", "cites", "latex")
    for edge in (include, cite):
        assert scan_mod.source_status(conn, edge) == scan_mod.READ_AND_PARSED
        assert scan_mod.current_basis(conn, edge) == {}
        assert scan_mod.endpoints_present(conn, edge) is True
    assert db.scan_source_row(conn, "latex", "refs.bib")["status"] == scan_mod.READ_AND_PARSED


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions, non-root")
def test_unreadable_tex_and_md_report_unreadable(conn, tmp_path):
    (tmp_path / "main.tex").write_text("\\section{Intro}\n\\cite{key}\n")
    _paper(tmp_path)
    _metric_run(tmp_path)
    pipeline.ingest_sources(conn, tmp_path)
    claim_edge = _claim_edge(conn)
    cite = _edge("section:main.tex#intro", "ref:key", "cites", "latex")
    (tmp_path / "main.tex").chmod(0)
    (tmp_path / "paper.md").chmod(0)
    try:
        pipeline.ingest_sources(conn, tmp_path)
    finally:
        (tmp_path / "main.tex").chmod(stat.S_IRUSR | stat.S_IWUSR)
        (tmp_path / "paper.md").chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert scan_mod.source_status(conn, cite) == scan_mod.UNREADABLE
    assert scan_mod.source_status(conn, claim_edge) == scan_mod.UNREADABLE
    assert db.scan_source_row(conn, "mdpaper", "paper.md")["status"] == scan_mod.UNREADABLE
    assert db.get_node(conn, claim_edge["src"]) is not None  # not cleaned up: not read
    assert scan_mod.produced_in_latest_scan(conn, claim_edge) is None


def test_missing_mlruns_directory_is_an_unreadable_store(conn, tmp_path):
    from rce.ingest import mlflow

    mlflow.ingest_mlflow_dir(conn, tmp_path / "nowhere", source_key="mlflow:nowhere")
    assert db.scan_source_row(conn, "mlflow", "mlflow:nowhere")["status"] == scan_mod.UNREADABLE


def test_wandb_store_reports_and_stamps_artifact_basis(conn):
    from rce.ingest import wandb

    db.upsert_node(conn, "figure:plots/f.png", "figure")
    runs = [{"name": "r1", "files": {"edges": [{"node": {"name": "media/f.png"}}]}}]
    wandb.transform_runs(conn, runs, store="wandb:me/proj")
    edge = _edge("experiment:r1", "figure:plots/f.png", "produces", "wandb")
    assert scan_mod.source_status(conn, edge) == scan_mod.READ_AND_PARSED
    assert scan_mod.current_basis(conn, edge) == {"artifacts": ["media/f.png"]}
    wandb.transform_runs(conn, [*runs, {"displayName": "no id"}], store="wandb:me/proj")
    assert scan_mod.source_status(conn, edge) == scan_mod.UNPARSEABLE


def test_full_pipeline_three_times_is_idempotent_on_scan_basis(conn, tmp_path):
    """9.9 scenario 6, the scan half, over every extractor the fixture feeds."""
    _paper(tmp_path)
    _metric_run(tmp_path)
    (tmp_path / "fig.png").write_bytes(b"\x89PNG")
    (tmp_path / "main.tex").write_text("\\section{Intro}\n\\includegraphics{fig.png}\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "in.csv").write_text("x\n")
    (tmp_path / "a.py").write_text("import pandas as pd\npd.read_csv('data/in.csv')\n")
    seen = []
    for _ in range(3):
        pipeline.ingest_sources(conn, tmp_path)
        seen.append(sorted(
            (e["src"], e["dst"], e["type"], e["extractor"], e["scan_basis"], e["scan_source"], e["scan_lost"])
            for e in db.query_edges(conn)
        ))
    assert seen[0] == seen[1] == seen[2]
    assert all(row[4] is not None and row[6] is None for row in seen[0])


# -- git and pyfig ---------------------------------------------------------------


def _git(repo, *args):
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True,
    )


def test_git_and_pyfig_report_sources_and_uncommitted_savefig_is_unparseable(conn, tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "fig.png").write_bytes(b"\x89PNG")
    (tmp_path / "gen.py").write_text("import matplotlib.pyplot as plt\nplt.savefig('fig.png')\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "first")
    pipeline.ingest_sources(conn, tmp_path)
    [generates] = [e for e in db.query_edges(conn, type="generates")]
    assert scan_mod.source_status(conn, generates) == scan_mod.READ_AND_PARSED
    assert scan_mod.current_basis(conn, generates) == {"calls": ["savefig"]}
    [authored] = db.query_edges(conn, type="authored_by")
    assert _row(conn, authored)["scan_source"] == scan_mod.GIT_SOURCE
    assert scan_mod.source_status(conn, authored) == scan_mod.READ_AND_PARSED
    # An uncommitted edit of the savefig line cannot be attributed: the scan
    # does not speak for gen.py's generates links (not "no longer produced").
    (tmp_path / "gen.py").write_text("import matplotlib.pyplot as plt\nplt.savefig('fig.png', dpi=300)\n")
    pipeline.ingest_sources(conn, tmp_path)
    assert scan_mod.source_status(conn, generates) == scan_mod.UNPARSEABLE
    assert scan_mod.produced_in_latest_scan(conn, generates) is None


# -- attempts, the stale-verdict check, mappings (the watcher's partial scans) -----

ATTEMPTS_MD = """## Timeline

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-07 | step 1 | x | y | ok |
"""


def _attempts_config(root):
    from rce.ingest import attempts

    (root / "map.md").write_text(ATTEMPTS_MD)
    return attempts.AttemptsConfig(
        file="map.md", heading="Timeline",
        columns={"id": "#", "date": "Date", "description": "Path", "variables": "Variables",
                 "result": "Result", "verdict": "Verdict"},
    )


def test_attempts_partial_scan_reports_its_table_and_nothing_else(conn, tmp_path):
    from rce.ingest import attempts

    root = _project(tmp_path)
    _dataflow_scan(conn, root)
    before = _row(conn, READ)
    config = _attempts_config(root)
    attempts.ingest_attempts_repo(conn, root, config)
    assert db.scan_source_row(conn, "attempts", "map.md")["status"] == scan_mod.READ_AND_PARSED
    assert scan_mod.node_present(conn, "attempt:map.md#1") is True
    assert _row(conn, READ) == before
    (root / "map.md").write_text("no table here\n")
    with pytest.raises(attempts.AttemptsTableNotFoundError):
        attempts.ingest_attempts_repo(conn, root, config)
    assert db.scan_source_row(conn, "attempts", "map.md")["status"] == scan_mod.UNPARSEABLE
    assert scan_mod.node_present(conn, "attempt:map.md#1") is True  # previous state kept
    (root / "map.md").unlink()
    attempts.ingest_attempts_repo(conn, root, config)
    assert db.scan_source_row(conn, "attempts", "map.md")["status"] == scan_mod.UNREADABLE


def test_mappings_partial_scan_reports_the_file(conn, tmp_path):
    from rce.ingest import mappings

    root = _project(tmp_path)
    (root / ".rce").mkdir()
    mappings.ingest_mappings(conn, root)  # no file: nothing reported
    assert db.scan_source_row(conn, "mapping", ".rce/mappings.toml") is None
    (root / ".rce" / "mappings.toml").write_text(
        '[[mapping]]\nfrom = "data/in.csv"\nto = "a.py"\ntype = "reads"\n'
    )
    mappings.ingest_mappings(conn, root)
    edge = _edge("script:a.py", "dataset:data/in.csv", "reads", "mapping")
    assert scan_mod.source_status(conn, edge) == scan_mod.READ_AND_PARSED
    assert scan_mod.current_basis(conn, edge) == {}
    (root / ".rce" / "mappings.toml").write_text("not = [toml\n")
    with pytest.raises(mappings.MappingsParseError):
        mappings.ingest_mappings(conn, root)
    assert scan_mod.source_status(conn, edge) == scan_mod.UNPARSEABLE


def test_stale_verdict_check_reports_the_check_source(conn, tmp_path):
    from rce import consistency
    from rce.ingest import attempts

    _git(tmp_path, "init", "-q")
    (tmp_path / "steps").mkdir()
    (tmp_path / "steps" / "1-a.py").write_text("x = 1\n")
    md = ATTEMPTS_MD.replace("step 1", "step (1)")
    (tmp_path / "map.md").write_text(md)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "first")
    config = attempts.AttemptsConfig(
        file="map.md", heading="Timeline", steps_dir="steps",
        columns={"id": "#", "date": "Date", "description": "Path", "variables": "Variables",
                 "result": "Result", "verdict": "Verdict"},
    )
    attempts.ingest_attempts_repo(conn, tmp_path, config)
    consistency.check_stale_verdicts(conn, tmp_path, config)
    uses = db.query_edges(conn, type="uses")
    assert uses, "the fixture must produce a uses link"
    assert scan_mod.source_status(conn, uses[0]) == scan_mod.READ_AND_PARSED
    assert scan_mod.current_basis(conn, uses[0]) == {}


def test_inventory_marks_a_removed_file_absent_only_for_extractors_that_ran(conn, tmp_path):
    root = _project(tmp_path)
    (root / "fig.png").write_bytes(b"\x89PNG")
    (root / "main.tex").write_text("\\section{Intro}\n\\includegraphics{fig.png}\n")
    pipeline.ingest_sources(conn, root)
    (root / "main.tex").unlink()
    _dataflow_scan(conn, root)  # a partial scan: inventory + dataflow only
    assert db.scan_source_row(conn, scan_mod.INVENTORY, "main.tex")["status"] == scan_mod.ABSENT
    assert db.scan_source_row(conn, "latex", "main.tex")["status"] == scan_mod.READ_AND_PARSED
    pipeline.ingest_sources(conn, root)
    assert db.scan_source_row(conn, "latex", "main.tex")["status"] == scan_mod.ABSENT
