"""Tests for rce.ingest.git. Builds real throwaway repos under tmp_path via
subprocess `git` calls (no mocking) so tests exercise the actual `git log`
output format this module parses.
"""

import subprocess
from pathlib import Path

import pytest

from rce import db
from rce.ingest import git as git_ingest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo: Path, filename: str, content: str, message: str, name: str, email: str) -> str:
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", message)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "demo_repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q")
    return repo_dir


# -- read_commits -------------------------------------------------------------
def test_read_commits_extracts_sha_author_message_files(repo):
    sha1 = _commit(repo, "a.py", "print('a')\n", "first commit", "Alice", "Alice@Example.com")
    sha2 = _commit(repo, "b.py", "print('b')\n", "second commit\n\nmore detail", "Bob", "bob@example.com")
    commits = git_ingest.read_commits(repo)
    assert [c.sha for c in commits] == [sha1, sha2]
    first, second = commits
    assert first.author_name == "Alice"
    assert first.author_email == "Alice@Example.com"
    assert first.message == "first commit"
    assert first.files == ("a.py",)
    assert second.message == "second commit\n\nmore detail"
    assert second.files == ("b.py",)

def test_read_commits_on_unborn_repo_returns_empty(tmp_path):
    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()
    _git(empty_repo, "init", "-q")
    assert git_ingest.read_commits(empty_repo) == []

def test_read_commits_on_non_repo_raises(tmp_path):
    not_repo = tmp_path / "not_a_repo"
    not_repo.mkdir()
    with pytest.raises(git_ingest.GitIngestError):
        git_ingest.read_commits(not_repo)


# -- ingest_git_repo ----------------------------------------------------------
def test_ingest_creates_commit_and_contributor_and_edge(repo, conn):
    sha = _commit(repo, "a.py", "print('a')\n", "first commit", "Alice", "alice@example.com")
    ingested = git_ingest.ingest_git_repo(conn, repo)
    assert ingested == 1
    commit_node = db.get_node(conn, f"commit:{sha}")
    assert commit_node["type"] == "commit"
    assert commit_node["title"] == "first commit"
    assert commit_node["attrs"]["author_email"] == "alice@example.com"
    contributor_node = db.get_node(conn, "contributor:alice@example.com")
    assert contributor_node["type"] == "contributor"
    edges = db.query_edges(
        conn, src=f"commit:{sha}", dst="contributor:alice@example.com", type="authored_by"
    )
    assert len(edges) == 1
    edge = edges[0]
    assert edge["extractor"] == "git"
    assert edge["confidence"] == 1.0
    assert edge["status"] == "auto"
    # (T10) db.upsert_edge now wraps evidence as {"occurrences": [...]}.
    assert edge["evidence"] == {"occurrences": [{"sha": sha}]}

def test_ingest_lowercases_contributor_identity(repo, conn):
    _commit(repo, "a.py", "x\n", "msg", "Alice", "Alice@Example.COM")
    git_ingest.ingest_git_repo(conn, repo)
    assert db.get_node(conn, "contributor:alice@example.com") is not None
    assert db.get_node(conn, "contributor:Alice@Example.COM") is None

def test_ingest_two_authors_creates_two_contributors(repo, conn):
    _commit(repo, "a.py", "a\n", "c1", "Alice", "alice@example.com")
    _commit(repo, "b.py", "b\n", "c2", "Bob", "bob@example.com")
    git_ingest.ingest_git_repo(conn, repo)
    contributors = {
        row["id"] for row in conn.execute("SELECT id FROM nodes WHERE type='contributor'")
    }
    assert contributors == {"contributor:alice@example.com", "contributor:bob@example.com"}

def test_ingest_is_idempotent_on_repeat_run(repo, conn):
    _commit(repo, "a.py", "a\n", "c1", "Alice", "alice@example.com")
    _commit(repo, "b.py", "b\n", "c2", "Bob", "bob@example.com")
    first_run = git_ingest.ingest_git_repo(conn, repo)
    second_run = git_ingest.ingest_git_repo(conn, repo)
    assert first_run == second_run == 2
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='commit'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='contributor'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM edges WHERE type='authored_by'").fetchone()[0] == 2


# -- list_source_files (no graph nodes) ---------------------------------------
def test_list_source_files_groups_tracked_files_and_writes_no_nodes(repo, conn):
    (repo / "paper.tex").write_text(r"\documentclass{article}")
    (repo / "refs.bib").write_text("@article{a,}")
    (repo / "fig1.png").write_bytes(b"\x89PNG")
    (repo / "notes.md").write_text("not a tracked source category")
    _git(repo, "add", "paper.tex", "refs.bib", "fig1.png", "notes.md")
    _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "add sources")
    inventory = git_ingest.list_source_files(repo)
    assert inventory["tex"] == ["paper.tex"]
    assert inventory["bib"] == ["refs.bib"]
    assert inventory["image"] == ["fig1.png"]
    assert "notes.md" not in (inventory["tex"] + inventory["bib"] + inventory["image"])
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
