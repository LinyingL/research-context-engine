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

from rce import db, paths
from rce.webapp import watcher


# -- fixture project builders --------------------------------------------------


def _init_project(project_root: Path) -> None:
    """Same shape as tests/test_webapp_server.py's own copy -- test modules
    don't share fixtures across files in this codebase."""
    rce_dir = project_root / ".rce"
    rce_dir.mkdir(parents=True, exist_ok=True)
    # The graph lives outside the project (DESIGN.md section 8.10 rule 1).
    paths.ensure_graph_dir(project_root)
    conn = db.connect(paths.graph_db_path(project_root))
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
    conn = db.connect(paths.graph_db_path(project_root))
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
    conn = db.connect(paths.graph_db_path(tmp_path))
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
    """If the graph vanishes mid-serve, the watcher must refuse rather than
    let sqlite conjure a fresh empty database where the real one was -- the
    failure is reported, not papered over (DESIGN.md section 8.10 rule 2)."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    paths.graph_db_path(tmp_path).unlink()

    _write_map(tmp_path, [_row("1"), _row("2")])

    assert w.poll_once() is False  # nothing ingested; the graph is gone
    assert "graph.db" in (w.status_payload()["last_error"] or "")
    assert not paths.graph_db_path(tmp_path).exists()


# -- section 8.10 rule 2: a vanished graph degrades, it does not spam ---------


def test_vanished_graph_is_logged_once_not_once_per_poll(tmp_path, caplog):
    """The observed failure this rule exists for: the same error, with a
    traceback, once per 2-second poll, forever. One line for the outage --
    and the later polls are silent, not merely quieter."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    paths.graph_db_path(tmp_path).unlink()

    with caplog.at_level("WARNING", logger="rce.webapp.watcher"):
        for _ in range(5):
            assert w.poll_once() is False

    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is None  # a warning, not a traceback
    assert "paused" in caplog.records[0].getMessage()


def test_vanished_graph_bumps_the_generation_exactly_once(tmp_path):
    """One bump so every open page re-fetches and lands on the degraded
    header state -- and then stillness, so the page is not re-fetching
    every two seconds against a project that cannot answer."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    before = w.status_payload()["generation"]
    paths.graph_db_path(tmp_path).unlink()

    w.poll_once()
    after_first = w.status_payload()["generation"]
    for _ in range(3):
        w.poll_once()

    assert after_first == before + 1
    assert w.status_payload()["generation"] == after_first


def test_vanished_graph_stops_reingesting_even_as_files_keep_changing(tmp_path):
    """"Stops re-ingesting that root until the file reappears": edits during
    the outage change the watch set, and still nothing is attempted."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    paths.graph_db_path(tmp_path).unlink()

    for n in ("2", "3", "4"):
        _write_map(tmp_path, [_row("1"), _row(n)])
        assert w.poll_once() is False

    assert not paths.graph_db_path(tmp_path).exists()  # nothing conjured, ever


def test_graph_reappearing_resumes_ingestion_and_clears_the_error(tmp_path):
    """The recovery half: the baseline was deliberately left untouched
    during the outage, so the edit made while the graph was away is still a
    visible change when it returns -- and is ingested then."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    graph = paths.graph_db_path(tmp_path)
    saved = graph.read_bytes()
    graph.unlink()
    _write_map(tmp_path, [_row("1"), _row("2")])
    w.poll_once()
    assert w.status_payload()["last_error"]

    graph.write_bytes(saved)  # the file comes back

    assert w.poll_once() is True
    assert w.status_payload()["last_error"] is None
    assert _attempt_numbers(tmp_path) == ["1", "2"]


def test_a_switch_lets_the_new_root_report_its_own_missing_graph(tmp_path):
    """The once-only log is remembered per root: switching to another
    project whose graph is also gone must not be silenced by the first
    one's outage."""
    first, second = tmp_path / "a", tmp_path / "b"
    _make_project(first)
    _make_project(second)
    current = {"root": first}
    w = watcher.ProjectWatcher(lambda: current["root"])
    w.poll_once()
    paths.graph_db_path(first).unlink()
    w.poll_once()
    generation_after_first = w.status_payload()["generation"]

    current["root"] = second
    w.retarget()
    paths.graph_db_path(second).unlink()
    w.poll_once()

    assert str(second) in (w.status_payload()["last_error"] or "")
    assert w.status_payload()["generation"] > generation_after_first


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


# -- record_external_change (UI write path, task V3 phase 3) -------------------


def test_record_external_change_bumps_generation_and_absorbs_the_edit(tmp_path):
    """A UI write edits the map and re-ingests on its own, then calls
    record_external_change: the generation bumps (open pages re-fetch),
    and the next poll must NOT re-detect the same edit -- the baseline was
    re-taken from disk as part of recording it."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()  # baseline

    _write_map(tmp_path, [_row("1"), _row("2")])  # what apply_edit would have written
    assert w.record_external_change() == 2

    assert w.status_payload() == {"generation": 2, "refreshing": False, "last_error": None}
    assert w.poll_once() is False  # absorbed -- no second ingest of the same change
    assert w.status_payload()["generation"] == 2


def test_record_external_change_keeps_pending_steps_change_visible(tmp_path):
    """Regression: record_external_change used to re-baseline the ENTIRE
    watch set from disk, so a step-file change that landed after the last
    poll but before the UI write was absorbed without ever being
    dataflow-ingested -- and no later map-only save repaired it, because a
    map write never re-runs the dataflow half. Only the map/config half
    may be absorbed; the steps entries carry over from the old baseline,
    so the next poll still detects the step change and runs dataflow."""
    _make_project(tmp_path)
    (tmp_path / "steps" / "1-run.py").write_text("x = 1\n")
    w = _mk_watcher(tmp_path)
    w.poll_once()  # baseline (includes the step file)

    # The step edit lands in the gap between the last poll and a UI write...
    (tmp_path / "steps" / "1-run.py").write_text(
        'import pandas as pd\npd.read_csv("data/in.csv")\n'
    )
    # ...then the UI write confirms: map edited + attempts re-ingested by
    # apply_edit (not simulated here -- the point is what the WATCHER owes).
    _write_map(tmp_path, [_row("1"), _row("2")])
    w.record_external_change()

    assert w.poll_once() is True  # the pending step change is still a change
    conn = db.connect(paths.graph_db_path(tmp_path))
    try:
        # The dataflow half ran: the script node and its reads edge exist.
        assert db.get_node(conn, "script:steps/1-run.py") is not None
        reads = db.query_edges(conn, src="script:steps/1-run.py", type="reads")
        assert [e["dst"] for e in reads] == ["dataset:data/in.csv"]
    finally:
        conn.close()
    assert w.poll_once() is False  # and it was absorbed by that poll, once


def test_record_external_change_records_and_clears_ingest_errors(tmp_path):
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()

    w.record_external_change(error="boom")
    assert w.status_payload()["last_error"] == "boom"

    w.record_external_change()  # the next good write clears it, like a good poll
    assert w.status_payload() == {"generation": 3, "refreshing": False, "last_error": None}


def test_ingest_lock_is_the_poll_cycles_own_lock(tmp_path):
    """The property must hand out the very lock poll_once ingests under --
    a copy would let a UI write and a watcher ingest interleave. Holding it
    still lets a *change-free* poll complete (the lock guards ingest, not
    snapshotting), which is exactly the granularity the server relies on."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    with w.ingest_lock:
        assert w.ingest_lock.locked()
        assert w.poll_once() is False  # no change -> never reaches the ingest lock


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


# -- DESIGN.md section 8.5: .rce/mappings.toml joins the watch set ---------------


_MAPPING = '[[mapping]]\nfrom = "a.py"\nto = "f.png"\ntype = "generates"\n'


def _mapping_edges(project_root: Path) -> list[dict]:
    conn = db.connect(paths.graph_db_path(project_root))
    try:
        return [e for e in db.query_edges(conn) if e["extractor"] == "mapping"]
    finally:
        conn.close()


def test_take_snapshot_watches_the_mappings_file_even_without_attempts_config(tmp_path):
    _init_project(tmp_path)
    (tmp_path / ".rce" / "mappings.toml").write_text(_MAPPING)
    snap = watcher.take_snapshot(tmp_path)
    assert set(snap.files) == {str(tmp_path / ".rce" / "mappings.toml")}


def test_mappings_edit_reingests_mappings_without_an_attempts_config(tmp_path):
    """A project with hand-drawn links but no attempt timeline: a mappings
    change must run the mappings ingest alone -- running the attempts half
    too would fail on the missing config and surface a bogus error."""
    _init_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()

    (tmp_path / ".rce" / "mappings.toml").write_text(_MAPPING)
    assert w.poll_once() is True
    assert w.status_payload() == {"generation": 2, "refreshing": False, "last_error": None}
    (edge,) = _mapping_edges(tmp_path)
    assert edge["status"] == "confirmed"

    (tmp_path / ".rce" / "mappings.toml").write_text("# emptied\n")
    assert w.poll_once() is True
    assert _mapping_edges(tmp_path) == []


def test_mappings_only_edit_does_not_rerun_attempts(tmp_path, monkeypatch):
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    calls = []
    monkeypatch.setattr(
        watcher.attempts_ingest, "ingest_attempts_repo", lambda *a, **k: calls.append(a) or {},
    )
    (tmp_path / ".rce" / "mappings.toml").write_text(_MAPPING)
    assert w.poll_once() is True
    assert calls == [] and len(_mapping_edges(tmp_path)) == 1


def test_broken_mappings_save_is_contained_and_deletes_nothing(tmp_path):
    """A half-saved (unparseable) mappings file is a contained failure like
    a half-saved table: last_error set, the generation bumps, polling
    continues, the existing mapping edge survives (failing to read the
    file is not evidence its entries were deleted), and the next good save
    clears the error."""
    _init_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    (tmp_path / ".rce" / "mappings.toml").write_text(_MAPPING)
    w.poll_once()

    (tmp_path / ".rce" / "mappings.toml").write_text("[[mapping]\nfrom = ")
    assert w.poll_once() is True
    status = w.status_payload()
    assert status["generation"] == 3 and "not valid TOML" in (status["last_error"] or "")
    assert len(_mapping_edges(tmp_path)) == 1

    (tmp_path / ".rce" / "mappings.toml").write_text(_MAPPING + "\n")
    assert w.poll_once() is True
    assert w.status_payload()["last_error"] is None


def test_attempts_failure_does_not_keep_a_mapping_out_of_the_graph(tmp_path):
    """Both files change in one poll; the attempts half fails (heading
    renamed) -- the mappings half still runs, and the error reported is
    the attempts one, unchanged."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    broken = (tmp_path / "map.md").read_text().replace("## H", "## renamed")
    (tmp_path / "map.md").write_text(broken)
    (tmp_path / ".rce" / "mappings.toml").write_text(_MAPPING)

    assert w.poll_once() is True
    assert "heading" in (w.status_payload()["last_error"] or "")
    assert len(_mapping_edges(tmp_path)) == 1


def test_refused_mapping_entries_are_not_a_watcher_error(tmp_path):
    _init_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    (tmp_path / ".rce" / "mappings.toml").write_text(
        _MAPPING + '\n[[mapping]]\nfrom = "a.py"\nto = "x.docx"\ntype = "generates"\n'
    )
    assert w.poll_once() is True
    assert w.status_payload()["last_error"] is None
    assert len(_mapping_edges(tmp_path)) == 1


# -- canvas writes (DESIGN.md section 8.5): absorb= and bump_generation -------


def test_record_external_change_absorb_keeps_a_pending_map_save_visible(tmp_path):
    """A canvas mapping write re-ingests ONLY the mappings file. An
    attempts-map save that landed after the last poll but before that
    write has not been attempts-ingested, so absorbing it into the baseline
    would lose it for good; with `absorb={mappings file}` the next poll
    still sees it and ingests the new row."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    _write_map(tmp_path, [_row("1"), _row("2")])  # not ingested by anyone yet
    (tmp_path / ".rce" / "mappings.toml").write_text(_MAPPING)  # the "write"

    w.record_external_change(absorb={str(tmp_path / ".rce" / "mappings.toml")})

    assert _attempt_numbers(tmp_path) == []
    assert w.poll_once() is True
    assert _attempt_numbers(tmp_path) == ["1", "2"]


def test_record_external_change_absorb_does_absorb_the_named_file(tmp_path):
    """The absorbed file itself is NOT re-detected: the write already
    ingested it, so the next poll has nothing to do."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    (tmp_path / ".rce" / "mappings.toml").write_text(_MAPPING)

    w.record_external_change(absorb={str(tmp_path / ".rce" / "mappings.toml")})

    assert w.poll_once() is False


def test_bump_generation_touches_neither_baseline_nor_error(tmp_path):
    """An edge-status change (标记为错误提取 / restore) re-ingests nothing:
    the generation moves so pages re-fetch, a pending file change stays
    pending, and an earlier ingest error is not silently cleared."""
    _make_project(tmp_path)
    w = _mk_watcher(tmp_path)
    w.poll_once()
    broken = (tmp_path / "map.md").read_text().replace("## H", "## renamed")
    (tmp_path / "map.md").write_text(broken)
    w.poll_once()
    error = w.status_payload()["last_error"]
    assert error
    _write_map(tmp_path, [_row("1"), _row("2")])
    generation = w.status_payload()["generation"]

    assert w.bump_generation() == generation + 1

    assert w.status_payload()["last_error"] == error
    assert w.poll_once() is True
    assert _attempt_numbers(tmp_path) == ["1", "2"]


# -- first-sight mappings sync (adversarial review of the V4 work) --------------


def _ingest_mappings_now(project_root: Path) -> None:
    conn = db.connect(paths.graph_db_path(project_root))
    try:
        from rce.ingest import mappings as mappings_ingest

        mappings_ingest.ingest_mappings(conn, project_root)
    finally:
        conn.close()


def test_first_poll_syncs_mappings_edited_while_the_app_was_closed(tmp_path):
    """The mappings file is the only truth for hand-drawn links and `rce
    ingest` does not read it: an entry removed (or added) while nothing was
    running must reach the graph on the first poll, not wait for the file
    to be touched again."""
    _init_project(tmp_path)
    (tmp_path / ".rce" / "mappings.toml").write_text(_MAPPING)
    _ingest_mappings_now(tmp_path)  # the graph as the last session left it
    # ... then, app closed, the researcher swaps the entry by hand / git pull
    (tmp_path / ".rce" / "mappings.toml").write_text(
        '[[mapping]]\nfrom = "b.py"\nto = "g.png"\ntype = "generates"\n'
    )
    w = _mk_watcher(tmp_path)

    assert w.poll_once() is True
    assert [(e["src"], e["dst"]) for e in _mapping_edges(tmp_path)] == [("script:b.py", "figure:g.png")]
    assert w.status_payload() == {"generation": 2, "refreshing": False, "last_error": None}
    assert w.poll_once() is False  # synced once; later polls are ordinary


def test_first_poll_sync_of_an_up_to_date_graph_costs_no_generation(tmp_path):
    _init_project(tmp_path)
    (tmp_path / ".rce" / "mappings.toml").write_text(_MAPPING)
    _ingest_mappings_now(tmp_path)
    w = _mk_watcher(tmp_path)
    assert w.poll_once() is False
    assert w.status_payload()["generation"] == 1


def test_first_poll_sync_failure_is_contained_and_retried(tmp_path):
    _init_project(tmp_path)
    (tmp_path / ".rce" / "mappings.toml").write_text("[[mapping]\nfrom = ")
    w = _mk_watcher(tmp_path)
    assert w.poll_once() is False
    status = w.status_payload()
    assert "not valid TOML" in (status["last_error"] or "") and status["generation"] == 2
    w.poll_once()
    assert w.status_payload()["generation"] == 2  # a still-failing retry bumps nothing more

    (tmp_path / ".rce" / "mappings.toml").write_text(_MAPPING)
    assert w.poll_once() is True
    assert w.status_payload()["last_error"] is None
    assert len(_mapping_edges(tmp_path)) == 1


def test_switching_back_resyncs_the_mappings_of_the_new_root(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        _init_project(root)
    root = {"now": a}
    w = watcher.ProjectWatcher(lambda: root["now"], interval=0.01)
    w.poll_once()
    root["now"] = b
    w.retarget()
    (b / ".rce" / "mappings.toml").write_text(_MAPPING)
    w.poll_once()
    assert len(_mapping_edges(b)) == 1
