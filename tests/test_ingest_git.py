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


# -- non-ASCII path bug fix ----------------------------------------------------
# Real repro: git's default core.quotepath=true octal-escapes and
# double-quotes any path containing non-ASCII bytes for `git ls-files` /
# `git log --name-only`'s ordinary (non -z) output, e.g. a Chinese filename
# comes back as '"2-\344\277\241...\346\240\207\347\255\276\347\272\247.py"'
# -- a string that matches no real path on disk. Every test below explicitly
# sets core.quotepath=true (rather than relying on git's default, which a
# machine's global gitconfig could already override) so this is a genuine
# regression guard, not an accidental pass.

def test_list_source_files_recognizes_non_ascii_paths_with_quotepath_true(repo, conn):
    _git(repo, "config", "core.quotepath", "true")
    (repo / "图表").mkdir()
    (repo / "图表" / "结果图.png").write_bytes(b"\x89PNG")
    (repo / "another space 图.png").write_bytes(b"\x89PNG")
    (repo / "论文.tex").write_text(r"\section{Intro}")
    (repo / "2-信息熵_标签级.py").write_text("print('hi')\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "中文提交")

    inventory = git_ingest.list_source_files(repo)
    assert inventory["tex"] == ["论文.tex"]
    assert sorted(inventory["image"]) == sorted(["图表/结果图.png", "another space 图.png"])
    assert inventory["py"] == ["2-信息熵_标签级.py"]
    # never the octal-escaped, double-quoted string quotepath=true produces
    for paths in inventory.values():
        assert not any(p.startswith('"') for p in paths)


def test_read_commits_preserves_non_ascii_filenames_with_quotepath_true(repo):
    _git(repo, "config", "core.quotepath", "true")
    sha = _commit(repo, "图-1.py", "print('x')\n", "中文提交信息", "作者", "author@example.com")
    [commit] = git_ingest.read_commits(repo)
    assert commit.sha == sha
    assert commit.files == ("图-1.py",)
    assert commit.message == "中文提交信息"


def test_has_undecodable_bytes_detects_only_surrogate_escapes():
    assert git_ingest._has_undecodable_bytes("图表/结果图.png") is False
    assert git_ingest._has_undecodable_bytes("plain_ascii.py") is False
    assert git_ingest._has_undecodable_bytes("bad\udcff.py") is True


def test_list_source_files_skips_and_logs_undecodable_path(monkeypatch, tmp_path, caplog):
    """A path byte sequence that isn't valid UTF-8 (surrogateescape-decoded
    by _run_git into a lone surrogate) must be skipped + logged individually
    -- never silently dropped with no trace, and never allowed to take the
    rest of the listing down with it."""
    good = "ascii.png"
    bad = "bad\udcff.py"  # simulates a non-UTF-8-decodable path byte sequence
    monkeypatch.setattr(
        git_ingest, "_run_git", lambda repo_path, args: f"{good}\x00{bad}\x00"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.git"):
        inventory = git_ingest.list_source_files(tmp_path)
    assert inventory["image"] == ["ascii.png"]
    assert inventory["py"] == []
    assert any("not valid UTF-8" in r.message for r in caplog.records)


def test_read_commits_skips_and_logs_undecodable_changed_file(monkeypatch, tmp_path, caplog):
    good = "ascii.py"
    bad = "bad\udcff.py"
    fake_record = (
        git_ingest._RECORD_SEP
        + git_ingest._FIELD_SEP.join(["a" * 40, "Name", "e@example.com", "2026-01-01T00:00:00+00:00"])
        + git_ingest._FIELD_SEP + "msg\n" + git_ingest._FIELD_SEP
        + f"\x00{good}\x00{bad}\x00"
    )
    monkeypatch.setattr(git_ingest, "_run_git", lambda repo_path, args: fake_record)
    with caplog.at_level("WARNING", logger="rce.ingest.git"):
        [commit] = git_ingest.read_commits(tmp_path)
    assert commit.files == (good,)
    assert any("not valid UTF-8" in r.message for r in caplog.records)
