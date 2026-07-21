"""Tests for rce.ingest.pyfig (T6). parse_py_file needs only a directory +
relative path (no git), mirroring tests/test_ingest_latex.py. ingest_pyfig_repo
now resolves each edge's src via `git blame` (batch3-fix), so those tests use
real throwaway repos via subprocess `git`, following tests/test_ingest_git.py's
pattern.
"""

import subprocess
from pathlib import Path

from rce import db
from rce.ingest import git as git_ingest
from rce.ingest import pyfig


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit_all(repo: Path, message: str) -> str:
    """Stage everything and commit; returns the new commit's SHA."""
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", message)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_parse_py_file_finds_literal_calls_and_skips_fstring(tmp_path, caplog):
    (tmp_path / "plot.py").write_text(
        "import matplotlib.pyplot as plt\n"
        "i = 3\n"
        "plt.savefig('figs/plot.png')\n"
        "fig.savefig(f'figs/out_{i}.png')\n"  # f-string -- must be skipped
        "savefig('bare.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "plot.py")

    assert [(c.callee, c.literal, c.line) for c in calls] == [
        ("plt.savefig", "figs/plot.png", 3),
        ("savefig", "bare.png", 5),
    ]
    assert any("not guessing" in r.message for r in caplog.records)


def test_ingest_pyfig_repo_literal_fstring_missing_file_and_idempotent(tmp_path, caplog):
    # One file exercising every required case: a legit literal call
    # (resolves at repo root), a second legit call resolved only via the
    # script-directory fallback, an f-string call (must be skipped), and a
    # literal pointing at a file the repo doesn't actually have (must be
    # skipped, never guessed into a node).
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "figs").mkdir()
    (repo / "figs" / "plot.png").write_bytes(b"\x89PNG")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "local.png").write_bytes(b"\x89PNG")
    (repo / "scripts" / "gen.py").write_text(
        "import matplotlib.pyplot as plt\n"
        "name = 'plot'\n"
        "plt.savefig('figs/plot.png')\n"  # resolves relative to repo root
        "plt.savefig('local.png')\n"  # only resolves relative to scripts/
        "plt.savefig(f'{name}.png')\n"  # f-string -- skip
        "plt.savefig('no_such_file.png')\n"  # not a tracked image -- skip
    )
    gen_sha = _commit_all(repo, "add gen.py and figures")
    known_images = ["figs/plot.png", "scripts/local.png"]

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        git_ingest.ingest_git_repo(conn, repo)  # creates the Commit node the FK needs

        for _ in range(2):  # second run proves idempotency
            with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
                counts = pyfig.ingest_pyfig_repo(conn, repo, ["scripts/gen.py"], known_images)
            assert counts == {"generates": 2}

        edges = db.query_edges(conn, src=f"commit:{gen_sha}", type="generates")
        assert {e["dst"] for e in edges} == {"figure:figs/plot.png", "figure:scripts/local.png"}
        root_edge = next(e for e in edges if e["dst"] == "figure:figs/plot.png")
        assert root_edge["extractor"] == "pyfig" and root_edge["confidence"] == 1.0
        assert root_edge["status"] == "auto"
        assert root_edge["evidence"] == {"file": "scripts/gen.py", "line": 3, "callee": "plt.savefig"}
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE type='generates'").fetchone()[0] == 2

        assert db.get_node(conn, "figure:no_such_file.png") is None
        assert any("not guessing" in r.message for r in caplog.records)
        assert any(
            "does not resolve to a tracked repo image" in r.message and "no_such_file.png" in r.message
            for r in caplog.records
        )
    finally:
        conn.close()


def test_generates_edge_pinned_to_introducing_commit_not_head(tmp_path):
    """Regression test for batch3-fix: src used to be the repo's HEAD at
    ingestion time (a stable dst `figure:<path>` paired with a src that
    drifts every time HEAD moves), so ingest -> unrelated commit -> re-ingest
    produced TWO generates edges for the same unchanged figure/script. src
    must stay pinned to the commit that introduced the savefig(...) line, so
    a re-ingest after unrelated history never accumulates a second edge.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "figs").mkdir()
    (repo / "figs" / "plot.png").write_bytes(b"\x89PNG")
    (repo / "gen.py").write_text(
        "import matplotlib.pyplot as plt\nplt.savefig('figs/plot.png')\n"
    )
    gen_sha = _commit_all(repo, "add gen.py and figure")
    known_images = ["figs/plot.png"]

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        git_ingest.ingest_git_repo(conn, repo)
        first = pyfig.ingest_pyfig_repo(conn, repo, ["gen.py"], known_images)
        assert first == {"generates": 1}

        # An unrelated commit moves HEAD without touching gen.py at all.
        (repo / "README.md").write_text("unrelated change\n")
        _commit_all(repo, "unrelated commit")
        git_ingest.ingest_git_repo(conn, repo)  # picks up the new HEAD commit node
        second = pyfig.ingest_pyfig_repo(conn, repo, ["gen.py"], known_images)
        assert second == {"generates": 1}

        all_generates = db.query_edges(conn, type="generates")
        assert len(all_generates) == 1  # not 2 -- no stale edge left behind
        assert all_generates[0]["src"] == f"commit:{gen_sha}"
        assert all_generates[0]["dst"] == "figure:figs/plot.png"
    finally:
        conn.close()


def test_uncommitted_savefig_line_is_skipped_not_guessed(tmp_path, caplog):
    """A savefig(...) line with only a local, uncommitted edit has no real
    commit to attribute it to yet -- git blame reports the all-zero
    pseudo-sha for it, which must be skipped and logged, never guessed at.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "figs").mkdir()
    (repo / "figs" / "plot.png").write_bytes(b"\x89PNG")
    (repo / "figs" / "other.png").write_bytes(b"\x89PNG")
    (repo / "gen.py").write_text("import matplotlib.pyplot as plt\n")
    _commit_all(repo, "add empty gen.py and figures")
    # Append a new, never-committed savefig line.
    with (repo / "gen.py").open("a") as f:
        f.write("plt.savefig('figs/other.png')\n")
    known_images = ["figs/plot.png", "figs/other.png"]

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        git_ingest.ingest_git_repo(conn, repo)
        with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
            counts = pyfig.ingest_pyfig_repo(conn, repo, ["gen.py"], known_images)
        assert counts == {"generates": 0}
        assert db.query_edges(conn, type="generates") == []
        assert any("cannot be attributed to a commit" in r.message for r in caplog.records)
    finally:
        conn.close()


def test_unborn_repo_skips_entire_scan(tmp_path, caplog):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")  # no commits yet
    (repo / "plot.png").write_bytes(b"\x89PNG")
    (repo / "plot.py").write_text("plt.savefig('plot.png')\n")
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
            counts = pyfig.ingest_pyfig_repo(conn, repo, ["plot.py"], ["plot.png"])
        assert counts == {"generates": 0}
        assert db.query_edges(conn, type="generates") == []
        assert any("unborn repo" in r.message for r in caplog.records)
    finally:
        conn.close()
