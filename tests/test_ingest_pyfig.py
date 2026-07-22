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
        "name = str('plot')\n"  # RHS not a plain string literal -- not T9-foldable
        "plt.savefig('figs/plot.png')\n"  # resolves relative to repo root
        "plt.savefig('local.png')\n"  # only resolves relative to scripts/
        "plt.savefig(f'{name}.png')\n"  # f-string over a non-foldable name -- skip
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
        # (T10) db.upsert_edge now wraps evidence as {"occurrences": [...]};
        # re-running with identical evidence (idempotency) dedupes to one.
        assert root_edge["evidence"] == {
            "occurrences": [{"file": "scripts/gen.py", "line": 3, "callee": "plt.savefig"}]
        }
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


# --- T9: same-file module constant folding -----------------------------------


def test_module_constant_fstring_folds_successfully(tmp_path):
    """T9: a module-level string constant inside an f-string folds to a real
    path; the generates edge's evidence records `folded_from`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "figs").mkdir()
    (repo / "figs" / "loss.png").write_bytes(b"\x89PNG")
    (repo / "gen.py").write_text(
        "import matplotlib.pyplot as plt\n"
        "SAVE_DIR = 'figs'\n"
        "plt.savefig(f'{SAVE_DIR}/loss.png')\n"
    )
    gen_sha = _commit_all(repo, "add gen.py with module constant f-string")
    known_images = ["figs/loss.png"]

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        git_ingest.ingest_git_repo(conn, repo)
        counts = pyfig.ingest_pyfig_repo(conn, repo, ["gen.py"], known_images)
        assert counts == {"generates": 1}

        edges = db.query_edges(conn, src=f"commit:{gen_sha}", type="generates")
        assert len(edges) == 1
        assert edges[0]["dst"] == "figure:figs/loss.png"
        # (T10) evidence lives under the single occurrence's dict now.
        assert edges[0]["evidence"]["occurrences"][0]["folded_from"] == "f'{SAVE_DIR}/loss.png'"
    finally:
        conn.close()


def test_parse_py_file_skips_fstring_with_loop_variable(tmp_path, caplog):
    """A for-loop-bound name is not a top-level Assign target -- never
    folded; the f-string call site is skipped and logged as pre-T9."""
    (tmp_path / "gen.py").write_text(
        "for i in range(1):\n"
        "    plt.savefig(f'figs/{i}.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


def test_parse_py_file_skips_reassigned_module_constant(tmp_path, caplog):
    """A name assigned twice at module level is ambiguous and must not
    fold (HANDOFF-SPEC.md T9: "重复赋值的名字不折叠")."""
    (tmp_path / "gen.py").write_text(
        "SAVE_DIR = 'figs'\n"
        "SAVE_DIR = 'figs2'\n"
        "plt.savefig(f'{SAVE_DIR}/loss.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


def test_parse_py_file_folds_os_path_join_mixed_forms(tmp_path):
    """os.path.join(...) folds when every argument is either a plain
    string literal or a foldable module-level constant (T9)."""
    (tmp_path / "gen.py").write_text(
        "import os\n"
        "SAVE_DIR = 'out'\n"
        "plt.savefig(os.path.join(SAVE_DIR, 'loss.png'))\n"
    )
    calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert [(c.literal, c.folded_from) for c in calls] == [
        ("out/loss.png", "os.path.join(SAVE_DIR, 'loss.png')"),
    ]


def test_folded_path_not_a_tracked_image_is_still_caught(tmp_path, caplog):
    """A folded path must still pass the tracked-image verification --
    folding is not a free pass around the ghost-figure guard."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "figs").mkdir()
    (repo / "figs" / "other.png").write_bytes(b"\x89PNG")  # exists, but not the folded target
    (repo / "gen.py").write_text(
        "import matplotlib.pyplot as plt\n"
        "SAVE_DIR = 'figs'\n"
        "plt.savefig(f'{SAVE_DIR}/missing.png')\n"
    )
    _commit_all(repo, "add gen.py with folded path to a missing file")
    known_images = ["figs/other.png"]

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        git_ingest.ingest_git_repo(conn, repo)
        with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
            counts = pyfig.ingest_pyfig_repo(conn, repo, ["gen.py"], known_images)
        assert counts == {"generates": 0}
        assert db.query_edges(conn, type="generates") == []
        assert any(
            "does not resolve to a tracked repo image" in r.message and "missing.png" in r.message
            for r in caplog.records
        )
    finally:
        conn.close()
