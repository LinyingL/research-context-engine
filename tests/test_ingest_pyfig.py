"""Tests for rce.ingest.pyfig (T6). Builds a small fake repo tree under
tmp_path (no real git needed -- this module only needs a root dir + relative
paths + an explicit image inventory, mirroring tests/test_ingest_latex.py).
"""

from rce import db
from rce.ingest import pyfig

FAKE_SHA = "a" * 40


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
    (tmp_path / "figs").mkdir()
    (tmp_path / "figs" / "plot.png").write_bytes(b"\x89PNG")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "local.png").write_bytes(b"\x89PNG")
    (tmp_path / "scripts" / "gen.py").write_text(
        "import matplotlib.pyplot as plt\n"
        "name = 'plot'\n"
        "plt.savefig('figs/plot.png')\n"  # resolves relative to repo root
        "plt.savefig('local.png')\n"  # only resolves relative to scripts/
        "plt.savefig(f'{name}.png')\n"  # f-string -- skip
        "plt.savefig('no_such_file.png')\n"  # not a tracked image -- skip
    )
    known_images = ["figs/plot.png", "scripts/local.png"]

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        db.upsert_node(conn, f"commit:{FAKE_SHA}", "commit", title="c1")

        for _ in range(2):  # second run proves idempotency
            with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
                counts = pyfig.ingest_pyfig_repo(
                    conn, tmp_path, ["scripts/gen.py"], known_images, FAKE_SHA
                )
            assert counts == {"generates": 2}

        edges = db.query_edges(conn, src=f"commit:{FAKE_SHA}", type="generates")
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


def test_no_head_sha_skips_entire_scan(tmp_path):
    (tmp_path / "plot.png").write_bytes(b"\x89PNG")
    (tmp_path / "plot.py").write_text("plt.savefig('plot.png')\n")
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        counts = pyfig.ingest_pyfig_repo(conn, tmp_path, ["plot.py"], ["plot.png"], None)
        assert counts == {"generates": 0}
        assert db.query_edges(conn, type="generates") == []
    finally:
        conn.close()
