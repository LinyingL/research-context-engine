"""Tests for rce.webapp.watcher (task V3 phase 2): the auto-refresh polling
watcher owned by the web server.

Almost everything here drives `ProjectWatcher.poll_once()` directly -- the
watcher was designed so one call runs one full snapshot -> compare ->
re-ingest -> generation-bump cycle deterministically, with no background
thread and no sleeping. One real-thread test at the bottom covers `start`/
`stop` themselves (a tiny interval, a generous deadline, and the baseline
established synchronously beforehand so the test never races the first
poll). Every fixture project lives in tmp_path; nothing here touches a real
research project.
"""

from __future__ import annotations

import time
from pathlib import Path

from rce import db
from rce.webapp import watcher


# -- fixture project builders --------------------------------------------------


def _init_project(project_root: Path) -> None:
    """Same shape as tests/test_webapp_server.py's own copy -- test modules
    don't share fixtures across files in this codebase."""
    rce_dir = project_root / ".rce"
    rce_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(rce_dir / "graph.db")
    try:
        db.migrate(conn)
    finally:
        conn.close()


_CONFIG = """\
file = "map.md"
heading = "H"
steps_dir = "steps"

[columns]
id = "#"
date = "date"
description = "desc"
variables = "vars"
result = "result"
verdict = "verdict"
"""

_MAP_HEADER = (
    "## H\n"
    "\n"
    "| # | date | desc | vars | result | verdict |\n"
    "|---|------|------|------|--------|---------|\n"
)


def _write_map(project_root: Path, rows: list[str]) -> None:
    """`rows` are pre-formatted `| ... |` table lines; the heading/header/
    separator scaffolding around them matches `_CONFIG` above."""
    (project_root / "map.md").write_text(_MAP_HEADER + "\n".join(rows) + "\n")


def _row(number: str, desc: str = "d") -> str:
    return f"| {number} | 2026-01-01 | {desc} | v | r | ✅ |"


def _make_project(project_root: Path, rows: list[str] | None = None) -> None:
    """An initialized project with a working attempts config, a one-row map
    (unless told otherwise), and an empty steps dir -- the smallest project
    the watcher has anything to watch in."""
    _init_project(project_root)
    (project_root / ".rce" / "attempts.toml").write_text(_CONFIG)
    (project_root / "steps").mkdir()
    _write_map(project_root, rows if rows is not None else [_row("1")])


def _attempt_numbers(project_root: Path) -> list[str]:
    conn = db.connect(project_root / ".rce" / "graph.db")
    try:
        nodes = db.get_nodes_by_type(conn, "attempt")
        return sorted(n["attrs"]["number"] for n in nodes)
    finally:
        conn.close()


def _mk_watcher(project_root: Path) -> watcher.ProjectWatcher:
    # interval is irrelevant to poll_once(); a tiny one documents that these
    # tests never depend on production's 2s cadence.
    return watcher.ProjectWatcher(lambda: project_root, interval=0.01)


# -- baseline / no-change behavior --------------------------------------------


def test_first_poll_establishes_baseline_without_ingest_or_bump(tmp_path):
    """Serving a project is not evidence it changed: the first poll only
    records what is on disk -- no ingest runs (the graph stays empty) and
    the generation stays at its starting value of 1."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)

    assert w.poll_once() is False

    assert w.status_payload() == {"generation": 1, "refreshing": False, "last_error": None}
    assert _attempt_numbers(tmp_path) == []


def test_unchanged_files_never_bump_generation(tmp_path):
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()

    assert w.poll_once() is False
    assert w.poll_once() is False

    assert w.status_payload()["generation"] == 1


def test_status_payload_shape_is_the_endpoint_contract(tmp_path):
    """`GET /api/generation` returns this dict verbatim, so its exact keys
    and starting values are the contract, not an implementation detail."""
    _make_project(tmp_path)
    payload = _mk_watcher(tmp_path).status_payload()
    assert payload == {"generation": 1, "refreshing": False, "last_error": None}


# -- change detection + re-ingest ----------------------------------------------


def test_map_edit_bumps_generation_and_reingests_attempts(tmp_path):
    """The core loop end to end: touch the map file, and the next poll both
    bumps the generation and makes the new row visible in the graph -- the
    same graph /api/tree is derived from."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()  # baseline

    _write_map(tmp_path, [_row("1"), _row("2", "new attempt")])

    assert w.poll_once() is True
    assert w.status_payload()["generation"] == 2
    assert w.status_payload()["last_error"] is None
    assert _attempt_numbers(tmp_path) == ["1", "2"]


def test_config_edit_triggers_reingest(tmp_path):
    """.rce/attempts.toml is itself a watched file: editing it (here, just
    rewriting it with a trailing comment -- same parse result, different
    bytes) is a change like any other."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()

    (tmp_path / ".rce" / "attempts.toml").write_text(_CONFIG + "\n# edited\n")

    assert w.poll_once() is True
    assert w.status_payload()["generation"] == 2
    assert _attempt_numbers(tmp_path) == ["1"]  # the map's row got ingested


def test_deleting_the_map_file_is_detected_as_a_change(tmp_path):
    """A watched file vanishing is a set-of-names change, not a silent
    no-op -- the generation bumps and the failure to re-ingest (the config
    now points at a missing file) surfaces as last_error, never a crash.
    (rce.ingest.attempts treats an unreadable source as all-zero counts,
    so this particular shape re-ingests to nothing rather than erroring --
    the point here is only that deletion is *seen*.)"""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()

    (tmp_path / "map.md").unlink()

    assert w.poll_once() is True
    assert w.status_payload()["generation"] == 2


# -- steps_dir: dataflow re-ingest, one level only ----------------------------


def test_steps_dir_change_triggers_dataflow_ingest(tmp_path):
    """A new step script appearing is a dataflow-relevant change: the next
    poll re-runs the same dataflow ingest `rce ingest` would, so the
    script's reads edge lands in the graph the tree/lineage views read."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()

    (tmp_path / "steps" / "1-run.py").write_text(
        'import pandas as pd\npd.read_csv("data/in.csv")\n'
    )

    assert w.poll_once() is True
    conn = db.connect(tmp_path / ".rce" / "graph.db")
    try:
        script = db.get_node(conn, "script:steps/1-run.py")
        assert script is not None
        reads = db.query_edges(conn, src="script:steps/1-run.py", type="reads")
        assert [e["dst"] for e in reads] == ["dataset:data/in.csv"]
    finally:
        conn.close()
    assert w.status_payload() == {"generation": 2, "refreshing": False, "last_error": None}


def test_map_only_edit_does_not_rerun_dataflow(tmp_path, monkeypatch):
    """The dataflow half only runs when the change actually touched
    steps_dir -- an ordinary map-row edit re-ingests attempts alone."""
    _make_project(tmp_path)
    (tmp_path / "steps" / "1-run.py").write_text("x = 1\n")
    w = _mk_watcher(tmp_path)
    w.poll_once()  # baseline (includes the step file)

    dataflow_calls: list[Path] = []
    monkeypatch.setattr(
        watcher.dataflow_ingest, "ingest_dataflow_repo",
        lambda conn, root, py, r, rmd: dataflow_calls.append(Path(root)) or {"reads": 0, "writes": 0},
    )

    _write_map(tmp_path, [_row("1"), _row("2")])
    assert w.poll_once() is True
    assert dataflow_calls == []

    (tmp_path / "steps" / "1-run.py").write_text("x = 2\n")
    assert w.poll_once() is True
    assert dataflow_calls == [tmp_path]


def test_steps_dir_is_watched_one_level_deep_only(tmp_path):
    """The watch set is bounded (module docstring): a file inside a
    *subdirectory* of steps_dir is never statted, so editing it is not a
    change -- no unbounded recursion into whatever the user nests there."""
    _make_project(tmp_path)
    sub = tmp_path / "steps" / "sub"
    sub.mkdir()
    (sub / "deep.py").write_text("x = 1\n")
    w = _mk_watcher(tmp_path)
    w.poll_once()

    (sub / "deep.py").write_text("x = 2\n")

    assert w.poll_once() is False
    assert w.status_payload()["generation"] == 1


# -- failure containment -------------------------------------------------------


def test_broken_table_edit_surfaces_last_error_and_keeps_polling(tmp_path):
    """The user saving a half-edited map (here: the heading renamed out
    from under the config, `AttemptsTableNotFoundError` territory) must not
    kill the watcher: the failure lands in last_error, the generation still
    bumps (the file really changed), the previously ingested rows are left
    untouched, and a later good save both re-ingests and clears the error."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    _write_map(tmp_path, [_row("1"), _row("2")])
    w.poll_once()  # good ingest first, so there is state a bad save could hurt
    assert _attempt_numbers(tmp_path) == ["1", "2"]

    broken = (tmp_path / "map.md").read_text().replace("## H", "## renamed")
    (tmp_path / "map.md").write_text(broken)

    assert w.poll_once() is True
    status = w.status_payload()
    assert status["generation"] == 3
    assert status["last_error"] is not None and "heading" in status["last_error"]
    assert _attempt_numbers(tmp_path) == ["1", "2"]  # nothing was deleted

    # ...and the next good save recovers on its own: error cleared, new
    # content ingested, no restart of anything required.
    _write_map(tmp_path, [_row("1"), _row("2"), _row("3")])
    assert w.poll_once() is True
    status = w.status_payload()
    assert status == {"generation": 4, "refreshing": False, "last_error": None}
    assert _attempt_numbers(tmp_path) == ["1", "2", "3"]


def test_missing_graph_db_surfaces_last_error_instead_of_creating_one(tmp_path):
    """If graph.db vanishes mid-serve, the re-ingest must refuse rather
    than let sqlite conjure a fresh empty database inside a project that is
    no longer initialized -- the failure is reported, not papered over."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    (tmp_path / ".rce" / "graph.db").unlink()

    _write_map(tmp_path, [_row("1"), _row("2")])

    assert w.poll_once() is True
    assert "graph.db" in (w.status_payload()["last_error"] or "")
    assert not (tmp_path / ".rce" / "graph.db").exists()


def test_project_without_config_polls_quietly(tmp_path):
    """No .rce/attempts.toml at all: the watch set degrades to the (absent)
    config path -- nothing to compare, nothing to ingest, no error spam."""
    _init_project(tmp_path)
    w = _mk_watcher(tmp_path)

    assert w.poll_once() is False
    assert w.poll_once() is False
    assert w.status_payload() == {"generation": 1, "refreshing": False, "last_error": None}


def test_config_created_later_is_picked_up(tmp_path):
    """The config file appearing after the watcher started polling is a
    set-of-names change like any other -- the poll after it lands both
    re-shapes the watch set and runs the first ingest."""
    _init_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()  # baseline: empty watch set

    (tmp_path / ".rce" / "attempts.toml").write_text(_CONFIG)
    (tmp_path / "steps").mkdir()
    _write_map(tmp_path, [_row("1")])

    assert w.poll_once() is True
    assert w.status_payload()["last_error"] is None
    assert _attempt_numbers(tmp_path) == ["1"]


# -- retarget (project switch) -------------------------------------------------


def test_retarget_bumps_generation_and_clears_error(tmp_path):
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    broken = (tmp_path / "map.md").read_text().replace("## H", "## renamed")
    (tmp_path / "map.md").write_text(broken)
    w.poll_once()
    assert w.status_payload()["last_error"] is not None

    w.retarget()

    status = w.status_payload()
    assert status["generation"] == 3  # the change's bump + the retarget's bump
    assert status["last_error"] is None


def test_poll_after_retarget_rebaselines_without_ingesting(tmp_path):
    """After a switch, the first poll against the (possibly new) root only
    re-establishes the baseline -- switching projects is not evidence
    anything in the target project changed, so no ingest and no bump."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    w.retarget()

    assert w.poll_once() is False
    assert w.status_payload()["generation"] == 2  # only the retarget's own bump
    assert _attempt_numbers(tmp_path) == []


def test_retarget_follows_a_changed_root(tmp_path):
    """The watcher reads its root through the server's accessor each poll:
    after a switch the very next cycle snapshots the *new* project, and a
    subsequent edit there is what triggers ingest -- of the new project."""
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    _make_project(proj_a)
    _make_project(proj_b, rows=[_row("10")])
    current = {"root": proj_a}
    w = watcher.ProjectWatcher(lambda: current["root"], interval=0.01)
    w.poll_once()  # baseline on A

    current["root"] = proj_b
    w.retarget()
    assert w.poll_once() is False  # re-baseline on B, no ingest

    _write_map(proj_b, [_row("10"), _row("11")])
    assert w.poll_once() is True
    assert _attempt_numbers(proj_b) == ["10", "11"]
    assert _attempt_numbers(proj_a) == []  # A was never ingested by any of this


# -- snapshot helper -----------------------------------------------------------


def test_take_snapshot_watches_config_source_and_steps_files(tmp_path):
    _make_project(tmp_path)
    (tmp_path / "steps" / "1-run.py").write_text("x = 1\n")

    snap = watcher.take_snapshot(tmp_path)

    assert set(snap.files) == {
        str(tmp_path / ".rce" / "attempts.toml"),
        str(tmp_path / "map.md"),
        str(tmp_path / "steps" / "1-run.py"),
    }
    assert snap.steps_paths == frozenset({str(tmp_path / "steps" / "1-run.py")})


def test_take_snapshot_without_config_watches_only_the_config_path(tmp_path):
    _init_project(tmp_path)
    snap = watcher.take_snapshot(tmp_path)
    assert snap.files == {} and snap.steps_paths == frozenset()


# -- the real background thread ------------------------------------------------


def test_background_thread_detects_change_then_stops_cleanly(tmp_path):
    """One real-thread pass over start()/stop(): baseline established
    synchronously first (so the edit below can never race the first poll
    into the baseline), then a fast-interval thread must notice the edit
    within a generous deadline, and stop() must join it."""
    _make_project(tmp_path)
    w = watcher.ProjectWatcher(lambda: tmp_path, interval=0.02)
    w.poll_once()  # deterministic baseline, before any thread exists
    w.start()
    try:
        _write_map(tmp_path, [_row("1"), _row("2")])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if w.status_payload()["generation"] >= 2:
                break
            time.sleep(0.02)
        assert w.status_payload()["generation"] >= 2
        assert _attempt_numbers(tmp_path) == ["1", "2"]
    finally:
        w.stop()
    assert w._thread is None  # stop() joined and forgot the thread


def test_start_is_idempotent_and_stop_without_start_is_safe(tmp_path):
    _make_project(tmp_path)
    w = watcher.ProjectWatcher(lambda: tmp_path, interval=0.02)
    w.stop()  # never started -- must be a no-op, server_close relies on this
    w.start()
    first_thread = w._thread
    w.start()  # second start while alive: same thread, no doubling
    assert w._thread is first_thread
    w.stop()
    assert w._thread is None
