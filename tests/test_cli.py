"""Tests for rce.cli (T4): init -> ingest -> status -> query -> trace via
cli.main() against a real git+LaTeX+.bib+MLflow fixture (subprocess `git`,
no mocking).
"""

import json
import subprocess
from pathlib import Path

import pytest

from rce import cli, db


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _write(dir_path: Path, files: dict[str, str]) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (dir_path / name).write_text(content)


@pytest.fixture
def paper_repo(tmp_path: Path) -> tuple[Path, str]:
    """git+LaTeX+.bib repo (one figure, one .bib entry, both included/cited)
    plus a matching MLflow run under <repo>/mlruns for default-path detection."""
    repo = tmp_path / "paper_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "overview.png").write_bytes(b"\x89PNG")
    (repo / "refs.bib").write_text(
        "@article{smith2020,\n title={A Paper},\n author={Smith},\n year={2020},\n}\n"
    )
    (repo / "paper.tex").write_text(
        "\\section{Intro}\n\\includegraphics{overview.png}\nAs shown in \\citep{smith2020}.\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Alice", "-c", "user.email=alice@example.com", "commit", "-m", "add paper")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    run_dir = repo / "mlruns" / "0" / "run_a"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.yaml").write_text(
        "experiment_id: '0'\nrun_id: run_a\nrun_name: golden-run\nstatus: FINISHED\n"
    )
    _write(run_dir / "tags", {"mlflow.runName": "golden-run", "mlflow.source.git.commit": sha})
    (run_dir / "artifacts").mkdir()
    (run_dir / "artifacts" / "overview.png").write_bytes(b"\x89PNG")
    return repo, sha


@pytest.fixture
def claim_repo(tmp_path: Path) -> Path:
    """One quantitative claim (87.3% accuracy) + a matching MLflow metric --
    ingest produces exactly one pending `backed_by` edge (kept separate
    from `paper_repo`, whose tests assert an *empty* pending queue)."""
    repo = tmp_path / "claim_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "paper.tex").write_text("\\section{Results}\nOur model achieves 87.3\\% accuracy.\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=A", "-c", "user.email=a@example.com", "commit", "-m", "add paper")
    run_dir = repo / "mlruns" / "0" / "run_a"
    (run_dir / "metrics").mkdir(parents=True)
    (run_dir / "meta.yaml").write_text("experiment_id: '0'\nrun_id: run_a\nstatus: FINISHED\n")
    (run_dir / "metrics" / "accuracy").write_text("0 0.873 0\n")
    return repo


def test_init_creates_db_and_project_node_idempotently(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()

    assert cli.main(["init", str(project)]) == 0
    assert cli.main(["init", str(project)]) == 0  # idempotent: no duplicate node, no error

    conn = db.connect(project / ".rce" / "graph.db")
    try:
        node = db.get_node(conn, f"project:{project.name}")
        assert node is not None and node["type"] == "project"
        count = conn.execute("SELECT COUNT(*) FROM nodes WHERE type='project'").fetchone()[0]
        assert count == 1
    finally:
        conn.close()
    assert "Initialized RCE project" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv", [["ingest", "PROJECT"], ["status"], ["query", "commit:deadbeef"], ["lineage"]]
)
def test_commands_without_init_report_clear_error(tmp_path, monkeypatch, capsys, argv):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    argv = [a if a != "PROJECT" else str(project) for a in argv]

    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "Error" in err and "rce init" in err


def test_ingest_on_non_git_repo_degrades_gracefully(tmp_path, capsys):
    """W1: a project root that is not a git repository at all must not abort
    ingest. Real background: a researcher's project root is routinely never
    `git init`ed. `rce ingest` prints one clear explanatory line, skips
    commit/contributor nodes (no history to read), and every other
    extractor still runs -- here against a filesystem-scanned inventory
    (rce.ingest.files) instead of `git ls-files`. pyfig additionally has no
    commit source node to attach a `generates` edge to, so it degrades
    separately (see rce.ingest.pyfig.ingest_pyfig_repo) and reports
    generates=0 rather than crashing or fabricating a commit."""
    project = tmp_path / "proj"
    project.mkdir()  # deliberately never `git init`ed
    (project / "overview.png").write_bytes(b"\x89PNG")
    (project / "refs.bib").write_text(
        "@article{smith2020,\n title={A Paper},\n author={Smith},\n year={2020},\n}\n"
    )
    (project / "paper.tex").write_text(
        "\\section{Intro}\n\\includegraphics{overview.png}\nAs shown in \\citep{smith2020}.\n"
    )
    (project / "plot.py").write_text(
        "import matplotlib.pyplot as plt\nplt.savefig('overview.png')\n"
    )
    cli.main(["init", str(project)])
    capsys.readouterr()

    assert cli.main(["ingest", str(project)]) == 0
    out = capsys.readouterr().out
    assert (
        "no git repository -- commit/contributor nodes unavailable; "
        "using filesystem scan for the file inventory" in out
    )
    assert all(s in out for s in ("sections=1", "figures=1", "cites=1"))
    # pyfig cannot resolve a commit source node without git -- no generates edge.
    assert "generates=0" in out

    conn = db.connect(project / ".rce" / "graph.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='commit'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='contributor'").fetchone()[0] == 0
        assert db.get_node(conn, "figure:overview.png") is not None
        assert db.get_node(conn, "ref:smith2020") is not None
        assert db.query_edges(conn, type="generates") == []
    finally:
        conn.close()


def test_ingest_on_non_git_repo_skips_noise_directories(tmp_path, capsys):
    """W1: the filesystem-scan fallback must not pull in noise directories
    (build caches, dependency trees) that a git-tracked inventory would
    never have surfaced either."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "ghost.py").write_text("print('should not be scanned')\n")
    (project / "real.py").write_text("import matplotlib.pyplot as plt\n")
    cli.main(["init", str(project)])
    capsys.readouterr()

    assert cli.main(["ingest", str(project)]) == 0
    out = capsys.readouterr().out
    assert "pyfig: 1 .py scanned" in out


# -- task W2: rce.ingest.dataflow wired into `rce ingest`, after latex --


def test_ingest_reports_dataflow_summary_and_creates_lineage_nodes(tmp_path, capsys):
    """W2: cli.py orchestrates rce.ingest.dataflow -- runs even with no git
    repository at all (its edges never reference a Commit node, unlike
    pyfig's), and both a read of an existing file and a write of a
    not-yet-created one show up in the graph."""
    project = tmp_path / "proj"
    project.mkdir()  # deliberately never `git init`ed, like W1's own tests above
    (project / "data").mkdir()
    (project / "data" / "raw.csv").write_text("a,b\n1,2\n")
    (project / "gen.py").write_text(
        "import pandas as pd\n"
        "df = pd.read_csv('data/raw.csv')\n"
        "df.to_csv('data/out.csv')\n"
    )
    cli.main(["init", str(project)])
    capsys.readouterr()

    assert cli.main(["ingest", str(project)]) == 0
    out = capsys.readouterr().out
    assert "dataflow: 1 .py, 0 .R, 0 .Rmd scanned -> reads=1 writes=1" in out

    conn = db.connect(project / ".rce" / "graph.db")
    try:
        assert db.get_node(conn, "script:gen.py")["type"] == "script"
        assert db.get_node(conn, "dataset:data/raw.csv") is not None
        assert db.get_node(conn, "dataset:data/out.csv") is not None
        write_edges = db.query_edges(conn, type="writes")
        assert len(write_edges) == 1
        assert write_edges[0]["evidence"]["occurrences"][0]["missing"] is True
    finally:
        conn.close()


# -- task W3: rce.ingest.mdpaper wired into `rce ingest`, after wandb/before claims --


def test_ingest_reports_mdpaper_summary_and_creates_claim_backed_by_edge(tmp_path, capsys):
    """W3: cli.py orchestrates rce.ingest.mdpaper -- a .md paper's heading
    becomes a section, and its quantitative claim gets a pending backed_by
    edge once a matching MLflow metric exists, exactly like a .tex claim
    would (same shared write path, see rce.ingest.claims.ingest_parsed_claims).
    A README.md alongside it must be counted as skipped, not ingested."""
    project = tmp_path / "proj"
    project.mkdir()  # deliberately never `git init`ed, like W1/W2's own tests above
    (project / "paper.md").write_text("# Results\n\nOur model achieves 87.3% accuracy.\n")
    (project / "README.md").write_text("# Not a paper\n\nJust a readme.\n")
    run_dir = project / "mlruns" / "0" / "run_a"
    (run_dir / "metrics").mkdir(parents=True)
    (run_dir / "meta.yaml").write_text("experiment_id: '0'\nrun_id: run_a\nstatus: FINISHED\n")
    (run_dir / "metrics" / "accuracy").write_text("0 0.873 0\n")
    cli.main(["init", str(project)])
    capsys.readouterr()

    assert cli.main(["ingest", str(project)]) == 0
    out = capsys.readouterr().out
    assert "mdpaper: 2 .md scanned (1 skipped as README/CHANGELOG/LICENSE)" in out
    assert all(s in out for s in ("sections=1", "figures=0", "claims=1", "candidates=1"))

    conn = db.connect(project / ".rce" / "graph.db")
    try:
        assert db.get_node(conn, "section:paper.md#results")["type"] == "section"
        assert db.get_node(conn, "section:README.md#not-a-paper") is None  # README never ingested
        edges = db.query_edges(conn, type="backed_by")
        assert len(edges) == 1
        assert edges[0]["dst"] == "experiment:run_a" and edges[0]["status"] == "pending"
    finally:
        conn.close()


# -- T5.5 review item 5: `rce init` gitignore tip --


def test_init_prints_gitignore_tip(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()

    assert cli.main(["init", str(project)]) == 0

    out = capsys.readouterr().out
    assert ".rce/" in out and ".gitignore" in out


# -- T5.5 review item 4: list_source_files' GitIngestError must be caught too --


def test_ingest_catches_git_ingest_error_from_list_source_files(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")  # unborn repo: ingest_git_repo succeeds with 0 commits
    cli.main(["init", str(project)])
    capsys.readouterr()

    def _boom(_repo_path):
        raise cli.git_ingest.GitIngestError("simulated ls-files failure")

    monkeypatch.setattr(cli.git_ingest, "list_source_files", _boom)

    # Before the fix this call sat outside the try/except GitIngestError
    # block, so it propagated as an unhandled exception instead of the
    # standard "Error: git ingestion failed: ..." / exit code 1.
    assert cli.main(["ingest", str(project)]) == 1
    assert "git ingestion failed" in capsys.readouterr().err


# -- T5.5 review item 2: cli wires the git-tracked image inventory through --


def test_ingest_skips_ghost_figure_not_tracked_by_git(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    (project / "paper.tex").write_text("\\section{Intro}\n\\includegraphics{ghost.png}\n")
    _git(project, "add", "paper.tex")  # ghost.png itself is never added/tracked
    _git(project, "-c", "user.name=A", "-c", "user.email=a@example.com", "commit", "-m", "x")

    cli.main(["init", str(project)])
    capsys.readouterr()

    assert cli.main(["ingest", str(project)]) == 0
    out = capsys.readouterr().out
    assert "figures=0" in out
    assert "Skipped/unresolved during this run (see logs): 1" in out

    conn = db.connect(project / ".rce" / "graph.db")
    try:
        assert db.get_node(conn, "figure:ghost.png") is None
    finally:
        conn.close()


def test_full_pipeline_init_ingest_status_query(paper_repo, monkeypatch, capsys):
    repo, _sha = paper_repo

    assert cli.main(["init", str(repo)]) == 0
    capsys.readouterr()

    assert cli.main(["ingest", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "git: 1 commit(s) ingested" in out
    assert all(s in out for s in ("sections=1", "figures=1", "cites=1"))
    assert all(s in out for s in ("experiments=1", "implements=1", "produces=1"))
    assert "Skipped/unresolved during this run" in out

    monkeypatch.chdir(repo)

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert all(s in out for s in ("commit=1", "figure=1", "section=1", "reference=1", "experiment=1"))
    assert "Pending confirmation queue: 0" in out

    assert cli.main(["query", "figure:overview.png"]) == 0
    out = capsys.readouterr().out
    assert "Node: figure:overview.png (figure)" in out
    assert "Incoming edges (2):" in out  # latex `includes` + mlflow `produces`
    assert "extractor=latex" in out and "extractor=mlflow" in out
    assert "Outgoing edges (0):" in out

    assert cli.main(["query", "figure:does-not-exist.png"]) == 1
    assert "No such node: figure:does-not-exist.png" in capsys.readouterr().err


# -- README blocker fix (2026-07-26): status/query/trace must be runnable
# from a cwd other than the project root, via --path, exactly like the
# README's Quick start block -- without this, the third Quick start line
# (`rce trace ...`) fails with "no RCE project at <cwd>" when run verbatim.


def test_status_query_trace_work_via_path_flag_from_other_cwd(paper_repo, tmp_path, monkeypatch, capsys):
    repo, _sha = paper_repo
    assert cli.main(["init", str(repo)]) == 0
    assert cli.main(["ingest", str(repo)]) == 0
    capsys.readouterr()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert cli.main(["status", "--path", str(repo)]) == 0
    assert "figure=1" in capsys.readouterr().out

    assert cli.main(["query", "figure:overview.png", "--path", str(repo)]) == 0
    assert "Node: figure:overview.png (figure)" in capsys.readouterr().out

    assert cli.main(["trace", "figure:overview.png", "--path", str(repo), "--hops", "4"]) == 0
    assert "Provenance trace for figure:overview.png" in capsys.readouterr().out


# -- Owner ruling 2026-07-22: `rce trace` gives non-MCP users full multi-hop
# provenance (reuses rce.query.trace(), no logic duplicated here) --


def test_trace_human_readable_shows_indented_evidence_chain(paper_repo, monkeypatch, capsys):
    repo, _sha = paper_repo
    cli.main(["init", str(repo)])
    cli.main(["ingest", str(repo)])
    capsys.readouterr()
    monkeypatch.chdir(repo)

    assert cli.main(["trace", "figure:overview.png"]) == 0
    out = capsys.readouterr().out
    assert "Provenance trace for figure:overview.png (max_hops=4):" in out
    assert "section:paper.tex#intro --includes--> figure:overview.png" in out
    assert "extractor=latex" in out and "confidence=1.00" in out and "status=auto" in out
    # occurrences expanded to a readable "file:line" form, not a raw JSON blob
    assert "evidence: paper.tex:2" in out


def test_trace_json_outputs_structured_result(paper_repo, monkeypatch, capsys):
    repo, _sha = paper_repo
    cli.main(["init", str(repo)])
    cli.main(["ingest", str(repo)])
    capsys.readouterr()
    monkeypatch.chdir(repo)

    assert cli.main(["trace", "figure:overview.png", "--hops", "2", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["found"] is True
    assert any(
        h["type"] == "includes" and h["dst"] == "figure:overview.png" for h in result["hops"]
    )


def test_trace_shows_current_claim_line_for_backed_by_edge(claim_repo, monkeypatch, capsys):
    """Regression (2026-07-27): a prior commit backfilled the claim's
    current line into `rce status --pending`'s own display only, silently
    regressing `rce trace` (both human text and --json) versus the commit
    before that one -- which had the line baked directly into the
    `backed_by` edge's evidence. Both now read `hop["source_location"]`,
    injected uniformly by rce.query.trace (see rce.query.claim_source_location)."""
    cli.main(["init", str(claim_repo)])
    cli.main(["ingest", str(claim_repo)])
    capsys.readouterr()
    monkeypatch.chdir(claim_repo)

    assert cli.main(["trace", "experiment:run_a"]) == 0
    out = capsys.readouterr().out
    assert "--backed_by--> experiment:run_a" in out
    assert "paper.tex:2" in out

    assert cli.main(["trace", "experiment:run_a", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    hop = next(h for h in result["hops"] if h["type"] == "backed_by")
    assert hop["source_location"] == {"file": "paper.tex", "line": 2}
    assert "line" not in hop["evidence"]["occurrences"][0]  # never persisted, query-time only


def test_trace_missing_node_reports_clear_error(paper_repo, monkeypatch, capsys):
    repo, _sha = paper_repo
    cli.main(["init", str(repo)])
    capsys.readouterr()
    monkeypatch.chdir(repo)

    assert cli.main(["trace", "figure:does-not-exist.png"]) == 1
    assert "No such node: figure:does-not-exist.png" in capsys.readouterr().err


def test_trace_node_with_no_edges_says_so_not_fabricated(paper_repo, monkeypatch, capsys):
    repo, _sha = paper_repo
    cli.main(["init", str(repo)])
    capsys.readouterr()
    monkeypatch.chdir(repo)

    assert cli.main(["trace", f"project:{repo.name}"]) == 0
    assert "no provenance edges recorded" in capsys.readouterr().out


# -- T-blocker fix (2026-07-26): `--hops 0` (or negative) must never silently
# report "no provenance edges recorded" for a node that demonstrably has
# edges -- it must be rejected outright, before any traversal runs --


def test_trace_rejects_hops_zero_on_a_node_that_has_edges(paper_repo, monkeypatch, capsys):
    repo, _sha = paper_repo
    cli.main(["init", str(repo)])
    cli.main(["ingest", str(repo)])
    capsys.readouterr()
    monkeypatch.chdir(repo)

    # Sanity check first: this node genuinely has provenance edges (latex
    # `includes` + mlflow `produces`), so a "no edges recorded" verdict for
    # it at any --hops value would be a fabricated statement.
    assert cli.main(["query", "figure:overview.png"]) == 0
    assert "Incoming edges (2):" in capsys.readouterr().out

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["trace", "figure:overview.png", "--hops", "0"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--hops" in err and "must be >= 1" in err


def test_trace_rejects_negative_hops(paper_repo, monkeypatch, capsys):
    repo, _sha = paper_repo
    cli.main(["init", str(repo)])
    capsys.readouterr()
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["trace", "figure:overview.png", "--hops", "-1"])
    assert excinfo.value.code == 2


def test_trace_json_includes_max_hops_for_scripted_consumers(paper_repo, monkeypatch, capsys):
    repo, _sha = paper_repo
    cli.main(["init", str(repo)])
    cli.main(["ingest", str(repo)])
    capsys.readouterr()
    monkeypatch.chdir(repo)

    assert cli.main(["trace", "figure:overview.png", "--hops", "2", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["max_hops"] == 2


# -- Owner ruling 2026-07-22: `mcp` is optional; missing the extra must not
# affect any other subcommand and must fail with a clear, actionable message --


def test_mcp_command_reports_clear_error_when_mcp_extra_not_installed(monkeypatch, capsys):
    def _boom():
        raise ImportError("No module named 'mcp'")

    monkeypatch.setattr(cli, "_import_mcp_server", _boom)

    assert cli.main(["mcp", "--path", "."]) == 1
    err = capsys.readouterr().err
    assert "Error" in err and 'pip install "rce[mcp]"' in err


# -- F3: status --pending / confirm -- human confirmation path, no mcp extra required --


def _sole_pending_edge(project_root: Path) -> dict:
    conn = db.connect(project_root / ".rce" / "graph.db")
    try:
        return db.pending_edges(conn)[0]
    finally:
        conn.close()


def test_status_pending_lists_details_and_is_backward_compatible(claim_repo, capsys):
    cli.main(["init", str(claim_repo)])
    cli.main(["ingest", str(claim_repo)])
    capsys.readouterr()

    # Backward compatibility: no --pending -> output unchanged from before F3.
    assert cli.main(["status", "--path", str(claim_repo)]) == 0
    out = capsys.readouterr().out
    assert "Pending confirmation queue: 1" in out and "-->" not in out

    assert cli.main(["status", "--path", str(claim_repo), "--pending"]) == 0
    out = capsys.readouterr().out
    assert "Pending confirmation queue (1):" in out
    assert "claim:paper.tex#" in out and "--backed_by--> experiment:run_a" in out
    assert "extractor=claims" in out and "confidence=1.00" in out and "paper.tex:2" in out


def test_status_pending_omits_metric_field_for_legacy_semantic_review_without_it(claim_repo, capsys):
    """A `semantic_review` written before the metric-attribution fix
    (0de0603) has no "metric" key at all -- displaying it must omit the
    field entirely, not print the misleading `metric=None`."""
    cli.main(["init", str(claim_repo)])
    cli.main(["ingest", str(claim_repo)])
    capsys.readouterr()

    conn = db.connect(claim_repo / ".rce" / "graph.db")
    try:
        edge = db.pending_edges(conn)[0]
        db.set_edge_semantic_review(
            conn, edge["src"], edge["dst"], edge["type"], edge["extractor"],
            {"related": True, "reason": "looks fine", "model": "legacy-model"},  # no "metric" key
        )
    finally:
        conn.close()

    assert cli.main(["status", "--path", str(claim_repo), "--pending"]) == 0
    out = capsys.readouterr().out
    assert "metric=None" not in out
    assert "related=True" in out and "reason='looks fine'" in out


def test_status_pending_empty_queue_reports_empty(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    cli.main(["init", str(project)])
    capsys.readouterr()

    assert cli.main(["status", "--path", str(project), "--pending"]) == 0
    out = capsys.readouterr().out
    assert "Pending confirmation queue (0):" in out and "(empty)" in out


def test_confirm_missing_args_then_no_such_edge_then_success_then_index_out_of_range(claim_repo, capsys):
    cli.main(["init", str(claim_repo)])
    cli.main(["ingest", str(claim_repo)])
    capsys.readouterr()

    assert cli.main(
        ["confirm", "claim:nope#0", "experiment:nope", "--status", "confirmed", "--path", str(claim_repo)]
    ) == 1
    assert "requires either all four positional args" in capsys.readouterr().err

    assert cli.main(
        ["confirm", "claim:nope#0", "experiment:nope", "backed_by", "claims",
         "--status", "confirmed", "--path", str(claim_repo)]
    ) == 1
    assert "no such edge" in capsys.readouterr().err

    edge = _sole_pending_edge(claim_repo)
    assert cli.main(
        ["confirm", edge["src"], edge["dst"], edge["type"], edge["extractor"],
         "--status", "confirmed", "--path", str(claim_repo)]
    ) == 0
    assert "pending -> confirmed" in capsys.readouterr().out

    # Queue is now empty (that edge was just confirmed) -- --index 1 must be
    # reported as out of range, never crash or silently pick another edge.
    assert cli.main(["confirm", "--index", "1", "--status", "rejected", "--path", str(claim_repo)]) == 1
    assert "out of range" in capsys.readouterr().err


def test_confirm_then_reingest_never_overwrites_human_judgement(claim_repo, capsys):
    """End-to-end: confirm via the CLI, then re-ingest -- the verdict must survive."""
    cli.main(["init", str(claim_repo)])
    cli.main(["ingest", str(claim_repo)])
    capsys.readouterr()
    edge = _sole_pending_edge(claim_repo)

    cli.main(
        ["confirm", edge["src"], edge["dst"], edge["type"], edge["extractor"],
         "--status", "confirmed", "--path", str(claim_repo)]
    )
    capsys.readouterr()

    assert cli.main(["ingest", str(claim_repo)]) == 0
    capsys.readouterr()

    assert cli.main(["status", "--path", str(claim_repo), "--pending"]) == 0
    assert "Pending confirmation queue (0):" in capsys.readouterr().out

    conn = db.connect(claim_repo / ".rce" / "graph.db")
    try:
        updated = [e for e in db.query_edges(conn, src=edge["src"], dst=edge["dst"], type=edge["type"])
                   if e["extractor"] == edge["extractor"]][0]
        assert updated["status"] == "confirmed"
    finally:
        conn.close()


# -- task W4: `rce lineage` -- read-only four-block report over W2's dataflow graph --


@pytest.fixture
def lineage_project(tmp_path: Path) -> Path:
    """A project exercising all four `rce lineage` blocks at once:

    - `data/orphan_input.csv`: read by gen.py, written by nothing -- orphan.
    - `data/topicshift_monthly.csv`: written by gen.py, read by analysis.Rmd,
      and pre-created on disk so *both* occurrences are missing=False --
      a clean chain, deliberately kept free of the broken-link finding
      below so block 2 has one unambiguous, non-overlapping example. It
      also has a same-named copy under archive/, for block 4.
    - `data/gone_missing.csv`: read by gen.py but never created on disk and
      never written by anything -- both an orphan (block 1) AND a broken
      link (block 3) at once, a deliberately realistic overlap (a script
      reading a file nobody regenerates) rather than an artifact to avoid.

    Deliberately never `git init`ed (like the W1/W2 CLI tests above) --
    dataflow needs no git repository, and this keeps the fixture minimal.
    """
    project = tmp_path / "proj"
    _write(project, {
        "gen.py": (
            "import pandas as pd\n"
            "raw = pd.read_csv('data/orphan_input.csv')\n"  # orphan: no writer anywhere
            "raw.to_csv('data/topicshift_monthly.csv')\n"  # chain writer (target pre-exists below)
            "missing_df = pd.read_csv('data/gone_missing.csv')\n"  # orphan + broken: never created
        ),
        "analysis.Rmd": (
            "```{r}\n"
            "d <- read_csv(\"data/topicshift_monthly.csv\")\n"  # chain reader
            "```\n"
        ),
    })
    (project / "data").mkdir()
    (project / "data" / "orphan_input.csv").write_text("a,b\n1,2\n")
    (project / "data" / "topicshift_monthly.csv").write_text("a,b\n3,4\n")
    (project / "archive").mkdir()
    (project / "archive" / "topicshift_monthly.csv").write_text("dup\n")  # duplicate-named copy
    return project


def test_lineage_reports_all_four_blocks_and_exits_1(lineage_project, capsys):
    cli.main(["init", str(lineage_project)])
    cli.main(["ingest", str(lineage_project)])
    capsys.readouterr()

    assert cli.main(["lineage", str(lineage_project)]) == 1
    out = capsys.readouterr().out

    assert (
        "scanned 2 script node(s) -> 3 reads edge(s), 1 writes edge(s) "
        "across 3 dataset/figure target(s)" in out
    )

    assert "Orphan inputs -- read but never written (2):" in out
    assert "data/orphan_input.csv" in out
    assert "data/gone_missing.csv" in out
    assert "<- read by gen.py:2 (pd.read_csv)" in out
    assert "<- read by gen.py:4 (pd.read_csv)" in out

    assert "Lineage chains (1):" in out
    assert "data/topicshift_monthly.csv" in out
    assert "<- written by gen.py:3 (raw.to_csv)" in out
    assert "-> read by analysis.Rmd:2 (read_csv)" in out

    assert "Broken links -- evidence.missing=true (1):" in out
    assert "gen.py:4 reads data/gone_missing.csv (not found on disk)" in out

    assert "Duplicate copies (1):" in out
    assert "data/topicshift_monthly.csv (this is the copy actually read)" in out
    assert "also exists at: archive/topicshift_monthly.csv" in out


def test_lineage_orphans_flag_prints_only_block_one(lineage_project, capsys):
    cli.main(["init", str(lineage_project)])
    cli.main(["ingest", str(lineage_project)])
    capsys.readouterr()

    assert cli.main(["lineage", str(lineage_project), "--orphans"]) == 1
    out = capsys.readouterr().out

    assert "Orphan inputs -- read but never written (2):" in out
    assert "data/orphan_input.csv" in out
    assert "data/gone_missing.csv" in out
    for absent in ("Lineage chains", "Broken links", "Duplicate copies"):
        assert absent not in out


def test_lineage_json_output_matches_all_four_blocks(lineage_project, capsys):
    cli.main(["init", str(lineage_project)])
    cli.main(["ingest", str(lineage_project)])
    capsys.readouterr()

    assert cli.main(["lineage", str(lineage_project), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["scanned"] == {
        "scripts": 2, "reads_edges": 3, "writes_edges": 1, "targets": 3,
    }
    assert [o["path"] for o in payload["orphans"]] == [
        "data/gone_missing.csv", "data/orphan_input.csv",
    ]
    assert [c["path"] for c in payload["chains"]] == ["data/topicshift_monthly.csv"]
    assert payload["broken_links"] == [{
        "script": "gen.py", "line": 4, "callee": "pd.read_csv",
        "kind": "reads", "target": "data/gone_missing.csv",
    }]
    assert payload["duplicates"] == [{
        "target": "dataset:data/topicshift_monthly.csv",
        "path": "data/topicshift_monthly.csv",
        "other_copies": ["archive/topicshift_monthly.csv"],
    }]


def test_lineage_json_orphans_flag_narrows_payload(lineage_project, capsys):
    cli.main(["init", str(lineage_project)])
    cli.main(["ingest", str(lineage_project)])
    capsys.readouterr()

    cli.main(["lineage", str(lineage_project), "--orphans", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"scanned", "orphans"}


def test_lineage_empty_project_explains_scan_scope_instead_of_bare_ok(tmp_path, capsys):
    """The product red line this task exists to enforce: an empty result
    must never print as a bare status line -- it must say what was scanned
    and why nothing came of it."""
    project = tmp_path / "proj"
    project.mkdir()
    cli.main(["init", str(project)])
    cli.main(["ingest", str(project)])  # no .py/.R/.Rmd at all -- dataflow finds nothing
    capsys.readouterr()

    assert cli.main(["lineage", str(project)]) == 0
    out = capsys.readouterr().out
    assert "scanned 0 script node(s) -> 0 reads edge(s), 0 writes edge(s)" in out
    assert "Nothing to report" in out
    assert out.strip() != "OK"


def test_lineage_orphans_flag_empty_also_explains_scope(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    cli.main(["init", str(project)])
    cli.main(["ingest", str(project)])
    capsys.readouterr()

    assert cli.main(["lineage", str(project), "--orphans"]) == 0
    out = capsys.readouterr().out
    assert "No orphan inputs found" in out
    assert "scanned 0 script node(s)" in out


def test_lineage_clean_chain_with_no_orphans_or_broken_links_exits_0(tmp_path, capsys):
    """A project with a real, fully-resolved chain and nothing else wrong
    exits 0 -- duplicates/chains are informational, only orphans and broken
    links represent an actual gap (per the task's own exit-code rule).
    `clean.csv` is synthesized (no read call at all), not read from another
    tracked file, and pre-created on disk before ingest -- so the only
    dataset in this graph has a writer and a reader and nothing else."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "data").mkdir()
    (project / "data" / "clean.csv").write_text("a\n1\n2\n")
    (project / "gen.py").write_text(
        "import pandas as pd\n"
        "df = pd.DataFrame({'a': [1, 2]})\n"
        "df.to_csv('data/clean.csv')\n"
    )
    (project / "report.py").write_text("import pandas as pd\npd.read_csv('data/clean.csv')\n")
    cli.main(["init", str(project)])
    cli.main(["ingest", str(project)])
    capsys.readouterr()

    assert cli.main(["lineage", str(project)]) == 0
    out = capsys.readouterr().out
    assert "Lineage chains (1):" in out
    assert "data/clean.csv" in out
    assert "Nothing to report" not in out
    assert "Orphan inputs" not in out
    assert "Broken links" not in out
