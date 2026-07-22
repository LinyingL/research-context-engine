"""Tests for rce.ingest.mlflow. Hand-builds an MLflow FileStore tree under
tmp_path (no mlflow package dependency) alongside a real git+LaTeX fixture
(via rce.ingest.git/rce.ingest.latex) so the `implements`/`produces` edges
have a real Commit/Figure node to match against.
"""

import subprocess
from pathlib import Path

import pytest

from rce import db
from rce.ingest import git as git_ingest
from rce.ingest import latex as latex_ingest
from rce.ingest import mlflow as mlflow_ingest

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

@pytest.fixture
def repo_sha(tmp_path, conn) -> str:
    """git+LaTeX repo pre-ingested into `conn`, giving commit:<sha> and
    figure:overview.png for the connector edges; returns HEAD sha."""
    repo = tmp_path / "paper_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "overview.png").write_bytes(b"\x89PNG")
    (repo / "paper.tex").write_text("\\section{Intro}\n\\includegraphics{overview.png}\n")
    _git(repo, "add", "paper.tex", "overview.png")
    _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "add paper")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    git_ingest.ingest_git_repo(conn, repo)
    latex_ingest.ingest_latex_repo(conn, repo, ["paper.tex"], [])
    return sha

def _write(dir_path: Path, files: dict[str, str]) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (dir_path / name).write_text(content)

def _build_run(mlruns_root: Path, run_id: str, sha: str, exp_id: str = "0") -> Path:
    """Well-formed run: meta.yaml + params/metrics/tags + one image artifact."""
    run_dir = mlruns_root / exp_id / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.yaml").write_text(
        f"experiment_id: '{exp_id}'\nrun_id: {run_id}\nrun_name: golden-run\nstatus: FINISHED\n"
    )
    _write(run_dir / "params", {"lr": "0.01"})
    _write(run_dir / "metrics", {"accuracy": "0 0.5 0\n1700000100000 0.87 1\n"})
    _write(run_dir / "tags", {"mlflow.runName": "golden-run", "mlflow.source.git.commit": sha})
    (run_dir / "artifacts").mkdir()
    (run_dir / "artifacts" / "overview.png").write_bytes(b"\x89PNG")
    return run_dir

def test_ingest_creates_node_and_both_edges_skips_corrupted_run(tmp_path, conn, repo_sha):
    mlruns = tmp_path / "mlruns"
    _build_run(mlruns, "run_a", repo_sha)
    (mlruns / "0" / "run_broken" / "params").mkdir(parents=True)  # no meta.yaml -> corrupted

    counts = mlflow_ingest.ingest_mlflow_dir(conn, mlruns)
    assert counts == {"experiments": 1, "implements": 1, "produces": 1}
    node = db.get_node(conn, "experiment:run_a")
    assert node["type"] == "experiment" and node["title"] == "golden-run"
    assert node["attrs"]["status"] == "FINISHED"
    assert node["attrs"]["params"] == {"lr": "0.01"}
    assert node["attrs"]["metrics"] == {"accuracy": 0.87}
    assert node["attrs"]["tags"] == {
        "mlflow.runName": "golden-run", "mlflow.source.git.commit": repo_sha,
    }
    assert db.get_node(conn, "experiment:run_broken") is None  # corrupted run skipped
    implements = db.query_edges(conn, src=f"commit:{repo_sha}", dst="experiment:run_a", type="implements")
    assert len(implements) == 1
    edge = implements[0]
    assert edge["extractor"] == "mlflow" and edge["confidence"] == 1.0 and edge["status"] == "auto"
    # (T10) db.upsert_edge now wraps evidence as {"occurrences": [...]}.
    assert edge["evidence"] == {"occurrences": [{"run_id": "run_a", "sha": repo_sha}]}

    produces = db.query_edges(conn, src="experiment:run_a", dst="figure:overview.png", type="produces")
    assert len(produces) == 1
    assert produces[0]["evidence"] == {"occurrences": [{"run_id": "run_a", "artifact_path": "overview.png"}]}

def test_ingest_conservatively_skips_unresolvable_connectors(tmp_path, conn):
    fake_sha = "d" * 40  # absent from the graph: no commit was ever ingested here
    mlruns = tmp_path / "mlruns"
    _build_run(mlruns, "run_b", fake_sha)
    db.upsert_node(conn, "figure:a/overview.png", "figure", title="a/overview.png")
    db.upsert_node(conn, "figure:b/overview.png", "figure", title="b/overview.png")  # ambiguous basename

    counts = mlflow_ingest.ingest_mlflow_dir(conn, mlruns)
    assert counts["implements"] == 0 and counts["produces"] == 0
    assert db.get_node(conn, f"commit:{fake_sha}") is None  # never a placeholder
    assert db.query_edges(conn, type="implements") == []
    assert db.query_edges(conn, type="produces") == []

def test_ingest_is_idempotent_on_repeat_run(tmp_path, conn, repo_sha):
    mlruns = tmp_path / "mlruns"
    _build_run(mlruns, "run_a", repo_sha)

    first = mlflow_ingest.ingest_mlflow_dir(conn, mlruns)
    second = mlflow_ingest.ingest_mlflow_dir(conn, mlruns)
    assert first == second == {"experiments": 1, "implements": 1, "produces": 1}
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='experiment'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM edges WHERE type='implements'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM edges WHERE type='produces'").fetchone()[0] == 1


# -- T5.5 review item 3: unified skip verbosity + experiment-level tags/ --


def test_sha_not_in_graph_skip_is_logged_at_warning_not_info(tmp_path, conn, caplog):
    # Was logger.info -- must now be logger.warning so the CLI's shared
    # warning-based skip counter (rce.ingest logger hierarchy) picks it up.
    fake_sha = "e" * 40
    mlruns = tmp_path / "mlruns"
    _build_run(mlruns, "run_b", fake_sha)

    with caplog.at_level("INFO", logger="rce.ingest.mlflow"):
        counts = mlflow_ingest.ingest_mlflow_dir(conn, mlruns)

    assert counts["implements"] == 0
    skip_records = [r for r in caplog.records if "not found in graph" in r.message]
    assert len(skip_records) == 1
    assert skip_records[0].levelname == "WARNING"


def test_experiment_level_tags_dir_is_silently_skipped_not_reported_corrupted(tmp_path, conn, repo_sha, caplog):
    # mlruns/<exp_id>/tags/ is MLflow's own experiment-tag storage, not a run
    # dir -- it has no meta.yaml, so before this fix it was misreported as a
    # "corrupted run" (a WARNING). It must now be silently skipped instead.
    mlruns = tmp_path / "mlruns"
    _build_run(mlruns, "run_a", repo_sha)
    _write(mlruns / "0" / "tags", {"mlflow.note.content": "some experiment-level note"})

    with caplog.at_level("WARNING", logger="rce.ingest.mlflow"):
        counts = mlflow_ingest.ingest_mlflow_dir(conn, mlruns)

    assert counts == {"experiments": 1, "implements": 1, "produces": 1}  # tags/ contributes nothing
    assert db.get_node(conn, "experiment:tags") is None  # never mistaken for a run
    assert not any("corrupted" in r.message or "no meta.yaml" in r.message for r in caplog.records)


# -- T10: summarized visibility for runs with no git commit tag at all -----


def _build_run_without_git_tag(mlruns_root: Path, run_id: str, exp_id: str = "0") -> Path:
    """A well-formed run with no mlflow.source.git.commit tag at all -- the
    real testbed failure mode (32/32 runs), distinct from _build_run's
    tagged-sha-not-in-graph case tested above."""
    run_dir = mlruns_root / exp_id / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.yaml").write_text(
        f"experiment_id: '{exp_id}'\nrun_id: {run_id}\nrun_name: no-tag-run\nstatus: FINISHED\n"
    )
    _write(run_dir / "tags", {"mlflow.runName": "no-tag-run"})  # no git.commit tag
    return run_dir


def test_runs_with_no_git_tag_are_summarized_once_not_per_run(tmp_path, conn, repo_sha, caplog):
    # Before this fix, a run missing the tag entirely was completely
    # silent -- no edge, no log line. Must now produce exactly one summary
    # warning covering all such runs, not one line per run (log-spam guard).
    mlruns = tmp_path / "mlruns"
    _build_run(mlruns, "run_a", repo_sha)  # has the tag -- must not count
    _build_run_without_git_tag(mlruns, "run_no_tag_1")
    _build_run_without_git_tag(mlruns, "run_no_tag_2")

    with caplog.at_level("WARNING", logger="rce.ingest.mlflow"):
        counts = mlflow_ingest.ingest_mlflow_dir(conn, mlruns)

    assert counts["experiments"] == 3
    assert counts["implements"] == 1  # only run_a
    summary_records = [r for r in caplog.records if "no git commit tag" in r.message]
    assert len(summary_records) == 1  # one summary line, not per-run
    assert summary_records[0].message == (
        "2 of 3 runs have no git commit tag; implements edges cannot be built"
    )


def test_no_summary_warning_when_all_runs_have_git_tag(tmp_path, conn, repo_sha, caplog):
    mlruns = tmp_path / "mlruns"
    _build_run(mlruns, "run_a", repo_sha)

    with caplog.at_level("WARNING", logger="rce.ingest.mlflow"):
        mlflow_ingest.ingest_mlflow_dir(conn, mlruns)

    assert not any("no git commit tag" in r.message for r in caplog.records)
