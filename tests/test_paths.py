"""Tests for rce.paths (DESIGN.md section 8.10 resilience rule 1): the one
module that says where a project's derived state lives.

Three groups, matching the three things that module is responsible for:
the location algebra (`RCE_HOME`, the stable id, the graph/canvas paths),
the one-time legacy migration (copy -> verify -> delete, and what happens
when the verify fails), and the macOS dataless probe.

The `isolated_rce_home` autouse fixture in conftest.py already points
`RCE_HOME` at a throwaway directory for every test here, so nothing below
can reach the developer's real `~/.rce`; tests that need to observe the
*unset* behaviour delenv it explicitly.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from rce import db, paths


def _mk_legacy_graph(project_root: Path, *, rows: int = 3) -> Path:
    """A real, fully-migrated graph at the OLD in-project location, with
    enough content that a truncated or empty copy would be obvious."""
    legacy = paths.legacy_graph_db_path(project_root)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(legacy)
    try:
        db.migrate(conn)
        for i in range(rows):
            db.upsert_node(conn, f"figure:f{i}.png", "figure", title=f"f{i}.png")
    finally:
        conn.close()
    return legacy


def _node_ids(db_path: Path) -> list[str]:
    conn = db.connect(db_path)
    try:
        return sorted(row["id"] for row in conn.execute("SELECT id FROM nodes"))
    finally:
        conn.close()


# -- rce_home / the stable id / the derived locations -------------------------


def test_rce_home_honours_the_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path / "elsewhere"))
    assert paths.rce_home() == tmp_path / "elsewhere"


def test_rce_home_falls_back_to_dot_rce_under_the_user_home(monkeypatch):
    monkeypatch.delenv(paths.HOME_ENV_VAR, raising=False)
    assert paths.rce_home() == Path.home() / ".rce"


def test_rce_home_is_read_per_call_not_cached_at_import(tmp_path, monkeypatch):
    """The whole reason the test suite can isolate itself: a setenv after
    this module was imported must still be honoured."""
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path / "a"))
    first = paths.rce_home()
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path / "b"))
    assert first != paths.rce_home() == tmp_path / "b"


def test_graph_db_path_is_under_the_rce_home_never_the_project(tmp_path, isolated_rce_home):
    project = tmp_path / "proj"
    project.mkdir()
    graph = paths.graph_db_path(project)
    assert graph.name == "graph.db"
    assert graph.parent.parent == isolated_rce_home / "graphs"
    assert isolated_rce_home not in project.parents  # the two really are disjoint
    assert str(project) not in str(graph)


def test_legacy_graph_id_is_stable_and_path_derived(tmp_path):
    """Pre-V5 identity: the path hash. Different paths gave different ids,
    which is why a moved project lost its graph and a new project at an
    old path inherited one (DESIGN.md 9.0). Kept because the pre-V5
    indexes on disk are found by it; a V5 project is keyed by its id
    (`test_a_project_with_an_id_is_indexed_under_its_id`)."""
    project = tmp_path / "proj"
    project.mkdir()
    assert paths.legacy_graph_id(project) == paths.legacy_graph_id(project)
    other = tmp_path / "other"
    other.mkdir()
    assert paths.legacy_graph_id(project) != paths.legacy_graph_id(other)


def test_graph_id_is_ascii_and_short_even_for_a_non_ascii_project_name(tmp_path):
    """A researcher's real project is named 默认安全锚_论文流水线; the id must
    not smuggle that (or a 300-char directory name) into a filesystem path
    on another volume."""
    project = tmp_path / "默认安全锚_论文流水线"
    project.mkdir()
    graph_id = paths.legacy_graph_id(project)
    assert graph_id.isascii() and graph_id.isalnum() and len(graph_id) == 16


def test_graph_id_resolves_the_path_so_spellings_share_one_graph(tmp_path, monkeypatch):
    """A relative path, a trailing slash and the absolute path are ONE
    project -- otherwise `rce ingest .` and `rce ingest /full/path` would
    quietly build two different graphs."""
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(tmp_path)
    assert paths.legacy_graph_id("proj") == paths.legacy_graph_id(project)
    assert paths.legacy_graph_id(str(project) + "/") == paths.legacy_graph_id(project)


def test_canvas_state_path_sits_beside_the_graph(tmp_path):
    """Section 8.6: layout state is derived too, so it lives with the
    graph, not in the project the researcher commits."""
    project = tmp_path / "proj"
    project.mkdir()
    canvas = paths.canvas_state_path(project)
    assert canvas.name == "canvas.json"
    assert canvas.parent == paths.graph_db_path(project).parent


def test_graph_dir_is_not_created_merely_by_asking_for_it(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    assert not paths.graph_db_path(project).parent.exists()
    assert paths.ensure_graph_dir(project).exists()


def test_ensure_graph_dir_is_idempotent(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    assert paths.ensure_graph_dir(project) == paths.ensure_graph_dir(project)


# -- graph_exists: the external graph OR an unmigrated legacy one -------------


def test_graph_exists_false_for_a_bare_directory(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    assert not paths.graph_exists(project)


def test_graph_exists_true_for_the_external_graph(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    paths.ensure_graph_dir(project)
    paths.graph_db_path(project).write_bytes(b"")
    assert paths.graph_exists(project)


def test_graph_exists_true_for_an_unmigrated_legacy_graph(tmp_path):
    """A project whose graph has not been moved yet is still initialized --
    refusing to serve it would leave the user unable to trigger the very
    migration that fixes it."""
    project = tmp_path / "proj"
    project.mkdir()
    _mk_legacy_graph(project)
    assert paths.graph_exists(project)


# -- migrate_legacy_graph: copy -> verify -> delete ---------------------------


def test_migration_moves_the_graph_and_keeps_every_row(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project)
    before = _node_ids(legacy)

    moved = paths.migrate_legacy_graph(project)

    assert moved == paths.graph_db_path(project)
    assert _node_ids(moved) == before
    assert not legacy.exists()


def test_migration_removes_the_wal_and_shm_sidecars(tmp_path):
    """`rce.db.connect` puts every graph in WAL mode, so a live legacy
    graph normally has companions; leaving them behind would let a later
    connection inherit a stranger's WAL."""
    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project)
    for suffix in ("-wal", "-shm"):
        legacy.with_name(legacy.name + suffix).write_bytes(b"")

    paths.migrate_legacy_graph(project)

    leftovers = sorted(p.name for p in (project / ".rce").iterdir())
    assert leftovers == ["README"]  # the signpost (8.10 rule 1), nothing of the graph


def test_migration_preserves_commits_still_living_in_the_wal(tmp_path):
    """The reason this uses SQLite's backup API and not shutil.copyfile: a
    WAL-mode database's most recent commits can be entirely in the -wal
    sidecar, and a main-file-only copy drops them silently."""
    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project, rows=1)
    holder = db.connect(legacy)  # kept open, so nothing checkpoints the WAL
    try:
        db.upsert_node(holder, "figure:late.png", "figure", title="late.png")
        assert legacy.with_name(legacy.name + "-wal").exists()

        paths.migrate_legacy_graph(project)
    finally:
        holder.close()

    assert "figure:late.png" in _node_ids(paths.graph_db_path(project))


def test_migration_is_a_no_op_when_there_is_no_legacy_graph(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    assert paths.migrate_legacy_graph(project) is None
    assert not paths.graph_db_path(project).exists()


def test_migration_is_a_no_op_and_touches_nothing_when_both_exist(tmp_path):
    """Two graphs is a situation for a human. The external one is already
    authoritative, so the legacy file is left exactly where it is rather
    than overwriting either side."""
    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project)
    paths.ensure_graph_dir(project)
    paths.graph_db_path(project).write_bytes(b"external")

    assert paths.migrate_legacy_graph(project) is None

    assert legacy.exists()
    assert paths.graph_db_path(project).read_bytes() == b"external"


def test_migration_is_idempotent_across_repeated_first_touches(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _mk_legacy_graph(project)
    assert paths.migrate_legacy_graph(project) is not None
    assert paths.migrate_legacy_graph(project) is None
    assert paths.migrate_legacy_graph(project) is None


def test_migration_refuses_and_keeps_everything_when_the_copy_is_corrupt(tmp_path, monkeypatch):
    """The load-bearing failure case: if `PRAGMA integrity_check` on the
    COPY does not say ok, nothing is deleted, the staging file is cleaned
    up, and the caller hears about it -- never a silently empty graph in
    the new location."""
    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project)
    monkeypatch.setattr(paths, "_backup_database", lambda src, dst: "*** in database main ***")

    with pytest.raises(paths.GraphMigrationError) as excinfo:
        paths.migrate_legacy_graph(project)

    assert "integrity_check" in str(excinfo.value)
    assert str(legacy) in str(excinfo.value)
    assert legacy.exists() and _node_ids(legacy)  # untouched, rows intact
    assert not paths.graph_db_path(project).exists()
    assert list(paths.graph_dir(project).glob("*.migrating*")) == []


def test_migration_refuses_when_the_legacy_file_is_not_a_database(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    legacy = paths.legacy_graph_db_path(project)
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"this is not a sqlite file" * 100)

    with pytest.raises(paths.GraphMigrationError):
        paths.migrate_legacy_graph(project)

    assert legacy.exists()
    assert not paths.graph_db_path(project).exists()


def test_migration_cleans_up_a_previous_crashed_attempt(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _mk_legacy_graph(project)
    paths.ensure_graph_dir(project)
    stale = paths.graph_db_path(project).with_name("graph.db.migrating")
    stale.write_bytes(b"leftover from a crash")

    paths.migrate_legacy_graph(project)

    assert not stale.exists()
    assert _node_ids(paths.graph_db_path(project))


# -- resolve_graph_db: the funnel every _require_db copy uses -----------------


def test_resolve_graph_db_migrates_on_first_touch(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project)

    resolved = paths.resolve_graph_db(project)

    assert resolved == paths.graph_db_path(project) and resolved.exists()
    assert not legacy.exists()


def test_graph_db_path_itself_never_migrates(tmp_path):
    """The pure/impure split: asking where the graph lives (for a status
    line or an error message) must not move anything."""
    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project)

    paths.graph_db_path(project)

    assert legacy.exists()
    assert not paths.graph_db_path(project).exists()


# -- .rce/README: the project's own signpost ----------------------------------


def test_write_project_readme_names_the_graph_directory(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()

    readme = paths.write_project_readme(project)

    assert readme == project / ".rce" / "README"
    text = readme.read_text(encoding="utf-8")
    assert str(paths.graph_dir(project)) in text
    assert "（还没有）" in text  # no record files yet, and none invented


def test_readme_lists_exactly_the_record_files_the_folder_holds(tmp_path):
    """9.12 (acceptance, 2026-10-05): the index's current location and the
    record files this folder holds NOW -- and nothing it does not."""
    project = tmp_path / "proj"
    (project / ".rce" / "variables").mkdir(parents=True)
    (project / ".rce" / "judgements.toml").write_text("")
    (project / ".rce" / "mappings.toml").write_text("")
    text = paths.write_project_readme(project).read_text(encoding="utf-8")
    listed = [line.split()[0] for line in text.splitlines() if line.startswith("  ") and not line.startswith("  /")]
    assert listed == ["judgements.toml", "mappings.toml", "variables/"]
    assert "attempts.toml" not in text and "canvas.json" not in text


def test_migration_leaves_the_same_readme_rce_init_writes(tmp_path):
    """8.10 rule 1 (amended): the researcher who opens `.rce/` after the
    move and finds no `graph.db` finds the answer in the same folder."""
    project = tmp_path / "proj"
    project.mkdir()
    _mk_legacy_graph(project)

    paths.migrate_legacy_graph(project)

    readme = project / ".rce" / "README"
    migrated_text = readme.read_text(encoding="utf-8")
    readme.unlink()
    assert paths.write_project_readme(project).read_text(encoding="utf-8") == migrated_text
    assert str(paths.graph_dir(project)) in migrated_text


def test_a_failed_migration_writes_no_readme(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    _mk_legacy_graph(project)
    monkeypatch.setattr(paths, "_backup_database", lambda src, dst: "*** in database main ***")

    with pytest.raises(paths.GraphMigrationError):
        paths.migrate_legacy_graph(project)

    assert not (project / ".rce" / "README").exists()


def test_a_no_op_migration_writes_no_readme(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".rce").mkdir()
    assert paths.migrate_legacy_graph(project) is None
    assert not (project / ".rce" / "README").exists()


def test_a_readme_that_cannot_be_written_does_not_fail_a_completed_move(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    _mk_legacy_graph(project)

    def _boom(_root):
        raise PermissionError("read-only")

    monkeypatch.setattr(paths, "write_project_readme", _boom)
    assert paths.migrate_legacy_graph(project) == paths.graph_db_path(project)
    assert paths.graph_db_path(project).exists()


def test_write_project_readme_is_rewritten_not_appended(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    paths.write_project_readme(project)
    first = (project / ".rce" / "README").read_text(encoding="utf-8")
    paths.write_project_readme(project)
    assert (project / ".rce" / "README").read_text(encoding="utf-8") == first


# -- is_dataless: the pre-open probe ------------------------------------------


class _FakeStat:
    def __init__(self, flags: int) -> None:
        self.st_flags = flags


def test_is_dataless_true_when_the_flag_is_set(tmp_path, monkeypatch):
    target = tmp_path / "graph.db"
    target.write_bytes(b"")
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setattr(paths.os, "stat", lambda path: _FakeStat(paths.SF_DATALESS))
    assert paths.is_dataless(target)


def test_is_dataless_false_for_an_ordinary_local_file(tmp_path):
    target = tmp_path / "graph.db"
    target.write_bytes(b"")
    assert not paths.is_dataless(target)


def test_is_dataless_false_off_macos_where_the_flag_does_not_exist(tmp_path, monkeypatch):
    target = tmp_path / "graph.db"
    target.write_bytes(b"")
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setattr(paths.os, "stat", lambda path: _FakeStat(paths.SF_DATALESS))
    assert not paths.is_dataless(target)


def test_is_dataless_false_for_a_missing_file(tmp_path, monkeypatch):
    """"Is it evicted" is not the question a missing file answers -- that
    error belongs to whoever tries to open it."""
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert not paths.is_dataless(tmp_path / "never-existed.db")


def test_is_dataless_tolerates_a_stat_result_without_st_flags(tmp_path, monkeypatch):
    class _NoFlags:
        pass

    target = tmp_path / "graph.db"
    target.write_bytes(b"")
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setattr(paths.os, "stat", lambda path: _NoFlags())
    assert not paths.is_dataless(target)


def test_sf_dataless_constant_matches_the_documented_value():
    """Spelled out rather than imported (the `stat` module does not expose
    it); pin the value so a future edit cannot quietly change what is
    probed."""
    assert paths.SF_DATALESS == 0x40000000


def test_migration_error_message_is_actionable(tmp_path):
    """Whatever goes wrong, the message must name both ends and say that
    nothing was deleted -- this is a one-shot move of the user's only
    graph."""
    project = tmp_path / "proj"
    project.mkdir()
    legacy = paths.legacy_graph_db_path(project)
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"not a database" * 100)

    with pytest.raises(paths.GraphMigrationError) as excinfo:
        paths.migrate_legacy_graph(project)

    message = str(excinfo.value)
    assert str(legacy) in message and "Nothing was deleted" in message


def test_backup_database_reports_ok_for_a_healthy_graph(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project)
    destination = tmp_path / "copy.db"
    assert paths._backup_database(legacy, destination) == "ok"
    assert _node_ids(destination) == _node_ids(legacy)


def test_backup_database_raises_for_a_non_database_source(tmp_path):
    source = tmp_path / "junk.db"
    source.write_bytes(b"definitely not sqlite" * 100)
    with pytest.raises(sqlite3.Error):
        paths._backup_database(source, tmp_path / "copy.db")


def test_project_rce_dir_stays_inside_the_project(tmp_path):
    """The researcher-owned half did NOT move: attempts.toml, mappings.toml
    and backups/ are still theirs, still in the project, still committable."""
    project = tmp_path / "proj"
    project.mkdir()
    assert paths.project_rce_dir(project) == project / ".rce"
    assert os.path.commonpath([project, paths.project_rce_dir(project)]) == str(project)


# -- Adversarial review of the V4 work: concurrency, dataless, spelling, log ---


def _migrate_in_subprocess(project: str, rce_home: str, out: str) -> None:
    """Child-process body for the cross-process race below (module-level so
    `multiprocessing`'s spawn start method can import it)."""
    os.environ[paths.HOME_ENV_VAR] = rce_home
    try:
        result = paths.migrate_legacy_graph(project)
        Path(out).write_text("migrated" if result else "none", encoding="utf-8")
    except BaseException as exc:  # noqa: BLE001 -- the test wants every outcome
        Path(out).write_text(f"error:{type(exc).__name__}:{exc}", encoding="utf-8")


def test_concurrent_migrations_in_threads_install_one_verified_copy(tmp_path):
    """Several first touches at once (the server's own handler threads):
    exactly one migrates, the others see nothing left to do -- no raw
    FileNotFoundError from a rename of another migrator's staging file, no
    orphaned staging sidecars, and the installed graph has every row."""
    import threading

    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project, rows=2000)
    before = _node_ids(legacy)
    outcomes: list[object] = []
    barrier = threading.Barrier(4)

    def touch() -> None:
        barrier.wait()
        try:
            outcomes.append(paths.migrate_legacy_graph(project))
        except BaseException as exc:  # noqa: BLE001
            outcomes.append(exc)

    threads = [threading.Thread(target=touch) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [o for o in outcomes if isinstance(o, BaseException)] == []
    assert sum(1 for o in outcomes if o is not None) == 1
    assert _node_ids(paths.graph_db_path(project)) == before
    assert not legacy.exists()
    assert list(paths.graph_dir(project).glob("*.migrating*")) == []


def test_concurrent_migrations_in_processes_install_one_verified_copy(tmp_path, isolated_rce_home):
    """The cross-process case the review reproduced (RCE.app's `rce serve`
    and an MCP client's `rce mcp`, or two CLI commands): the `flock` makes
    them take turns, so one migrates and the others find the work done."""
    import multiprocessing

    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project, rows=3000)
    before = _node_ids(legacy)
    ctx = multiprocessing.get_context("spawn")
    outs = [tmp_path / f"out{i}.txt" for i in range(3)]
    procs = [
        ctx.Process(target=_migrate_in_subprocess, args=(str(project), str(isolated_rce_home), str(o)))
        for o in outs
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)

    results = sorted(o.read_text(encoding="utf-8") for o in outs)
    assert results == ["migrated", "none", "none"], results
    assert _node_ids(paths.graph_db_path(project)) == before
    assert not legacy.exists()
    assert list(paths.graph_dir(project).glob("*.migrating*")) == []


def test_migration_wraps_a_failed_rename_as_a_migration_error(tmp_path, monkeypatch):
    """`os.replace` failing must not escape as a raw OSError: callers turn
    only `GraphMigrationError` into their user-facing error, and the legacy
    graph must still be there."""
    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project)

    def boom(src, dst):
        raise FileNotFoundError(src)

    monkeypatch.setattr(paths.os, "replace", boom)
    with pytest.raises(paths.GraphMigrationError) as excinfo:
        paths.migrate_legacy_graph(project)
    assert "Nothing was deleted" in str(excinfo.value)
    assert legacy.exists() and _node_ids(legacy)
    assert list(paths.graph_dir(project).glob("*.migrating*")) == []


def test_migration_refuses_a_dataless_legacy_graph_without_opening_it(tmp_path, monkeypatch):
    """Section 8.10 rule 1 applies to the migration too: the legacy graph is
    the file that lives in the iCloud-synced project. A dataless one must be
    refused before any `sqlite3.connect`, its download requested in the
    background, and nothing moved."""
    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project)
    requested: list[Path] = []
    monkeypatch.setattr(paths, "is_dataless", lambda p: Path(p) == legacy)
    monkeypatch.setattr(paths, "_request_download", requested.append)

    def must_not_open(src, dst):
        raise AssertionError("the dataless legacy graph was opened")

    monkeypatch.setattr(paths, "_backup_database", must_not_open)
    with pytest.raises(paths.LegacyGraphDatalessError) as excinfo:
        paths.migrate_legacy_graph(project)
    assert isinstance(excinfo.value, paths.GraphMigrationError)  # every caller still handles it
    assert requested == [legacy]
    assert legacy.exists()
    assert not paths.graph_db_path(project).exists()


def test_migration_refuses_when_only_the_wal_is_dataless(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    legacy = _mk_legacy_graph(project)
    wal = legacy.with_name(legacy.name + "-wal")
    monkeypatch.setattr(paths, "is_dataless", lambda p: Path(p) == wal)
    monkeypatch.setattr(paths, "_request_download", lambda p: None)
    with pytest.raises(paths.LegacyGraphDatalessError):
        paths.migrate_legacy_graph(project)
    assert legacy.exists()


def test_request_download_materializes_in_the_background_once(tmp_path):
    """The download request never blocks the caller and is not repeated
    for a file whose request is still in flight."""
    target = tmp_path / "evicted.db"
    target.write_bytes(b"x")
    paths._request_download(target)
    for _ in range(200):
        if str(target) not in paths._DOWNLOADS_REQUESTED:
            break
        import time

        time.sleep(0.01)
    assert str(target) not in paths._DOWNLOADS_REQUESTED


def test_migration_log_line_is_visible_without_verbose(tmp_path, caplog):
    """Section 8.10's "one log line": at WARNING, the lowest level an
    unconfigured `rce` (no `-v`) prints, so the move is never silent."""
    import logging

    project = tmp_path / "proj"
    project.mkdir()
    _mk_legacy_graph(project)
    with caplog.at_level(logging.WARNING, logger="rce.paths"):
        paths.migrate_legacy_graph(project)
    moved = [r for r in caplog.records if "moved this project's graph" in r.getMessage()]
    assert len(moved) == 1 and moved[0].levelno == logging.WARNING


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="APFS case/normalization folding is macOS-only")
def test_graph_id_is_shared_by_case_and_normalization_spellings_on_macos(tmp_path):
    """Two spellings of one directory on the case- and normalization-
    insensitive APFS volume are one project and must share one graph --
    otherwise a migration under one spelling strands the graph for the
    other, which is then told to `rce init` a second, empty one."""
    import unicodedata

    project = tmp_path / "CaseProj"
    project.mkdir()
    if not (tmp_path / "caseproj").exists():
        pytest.skip("this volume is case-sensitive")
    assert paths.legacy_graph_id(tmp_path / "caseproj") == paths.legacy_graph_id(project)
    assert paths.legacy_graph_id(tmp_path / "CASEPROJ" / "..") == paths.legacy_graph_id(tmp_path)

    nfc = unicodedata.normalize("NFC", "é默认安全锚")
    nfd = unicodedata.normalize("NFD", "é默认安全锚")
    (tmp_path / nfc).mkdir()
    assert paths.legacy_graph_id(tmp_path / nfd) == paths.legacy_graph_id(tmp_path / nfc)


def test_graph_id_of_a_not_yet_existing_path_is_still_stable(tmp_path):
    """The canonicalization falls back to `resolve()` when the directory
    cannot be opened, so asking about a path that does not exist yet still
    gives one id."""
    ghost = tmp_path / "not-yet"
    assert paths.legacy_graph_id(ghost) == paths.legacy_graph_id(str(ghost) + "/")


# -- V5 (DESIGN.md 9.4): the index of a project with an id ---------------------


def test_a_project_with_an_id_is_indexed_under_its_id(tmp_path, isolated_rce_home):
    from rce.records import identity

    project = tmp_path / "proj"
    project.mkdir()
    ident = identity.create_identity(project)
    assert paths.graph_dir(project) == isolated_rce_home / "graphs" / ident.id
    assert paths.graph_db_path(project) == paths.index_dir(ident.id) / "graph.db"
    assert paths.canvas_state_path(project).parent == paths.index_dir(ident.id)
    moved = tmp_path / "moved"
    project.rename(moved)
    assert paths.graph_dir(moved) == paths.index_dir(ident.id)  # the id travels; the hash would not


def test_a_folder_without_an_id_keeps_the_legacy_location(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    assert paths.graph_dir(project) == paths.legacy_graph_dir(project)
    assert paths.graph_db_path(project) == paths.legacy_index_db_path(project)


def test_an_unreadable_identity_never_falls_back_to_the_legacy_index(tmp_path):
    project = tmp_path / "proj"
    (project / ".rce").mkdir(parents=True)
    (project / ".rce" / "project.toml").write_text("id = [\n")
    with pytest.raises(paths.IdentityUnavailableError):
        paths.graph_dir(project)
    assert paths.graph_exists(project) is False


def test_an_in_project_graph_inside_a_v5_project_is_not_moved(tmp_path):
    """Only a pre-V5 folder gets the 8.10 move; inside a folder with an id
    such a file is a pre-V5 database for `rce migrate` to list (9.5)."""
    from rce.records import identity

    project = tmp_path / "proj"
    project.mkdir()
    identity.create_identity(project)
    _mk_legacy_graph(project)
    assert paths.migrate_legacy_graph(project) is None
    assert paths.legacy_graph_db_path(project).exists()


def test_index_dir_refuses_anything_but_a_project_id():
    for bad in ("../x", "p-123", "", "path-0123456789abcdef"):
        with pytest.raises(ValueError):
            paths.index_dir(bad)


def test_ensure_project_rce_dir_never_recreates_a_missing_project(tmp_path):
    gone = tmp_path / "gone"
    with pytest.raises(FileNotFoundError):
        paths.ensure_project_rce_dir(gone)
    assert not gone.exists()
    with pytest.raises(FileNotFoundError):
        paths.write_project_readme(gone)
    assert not gone.exists()
