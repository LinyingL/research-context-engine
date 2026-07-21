"""Tests for rce.cli (T4): init -> ingest -> status -> query via cli.main()
against a real git+LaTeX+.bib+MLflow fixture (subprocess `git`, no mocking).
"""

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


@pytest.mark.parametrize("argv", [["ingest", "PROJECT"], ["status"], ["query", "commit:deadbeef"]])
def test_commands_without_init_report_clear_error(tmp_path, monkeypatch, capsys, argv):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    argv = [a if a != "PROJECT" else str(project) for a in argv]

    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "Error" in err and "rce init" in err


def test_ingest_on_non_git_repo_reports_clear_error(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    cli.main(["init", str(project)])
    capsys.readouterr()

    assert cli.main(["ingest", str(project)]) == 1
    assert "git ingestion failed" in capsys.readouterr().err


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
