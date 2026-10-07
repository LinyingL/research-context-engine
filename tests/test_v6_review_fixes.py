"""DESIGN.md Section 10 (task V6): regressions for the review of phases A
and B. Each test names the 10.8 acceptance scenario (or the 10.x rule)
whose promise the finding broke.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from rce import addproject, db, paths
from rce import project as project_identity
from rce.records import identity as identity_mod
from rce.records import situation as records_situation
from rce.webapp import registry, server

# Fixtures and helpers of the phase tests, reused as they are.
from test_v6_add_project import _git, _home_state, _tree, fake_home, no_project_server  # noqa: F401
from test_v6_page import NODE, _function, _src, _wording, needs_node


# -- inspecting runs nothing the folder names (10.2, 10.6) ------------------------------


def test_inspecting_a_git_repository_never_runs_its_fsmonitor_hook(tmp_path):
    """10.8 scenario 3 (a git repository) with 10.2's "Inspecting writes
    nothing anywhere": a `core.fsmonitor` command in the folder's own
    `.git/config` is not executed -- not by the preview, not by
    `rce projects add` without --yes, and not by the scan after adding."""
    marker = tmp_path / "PWNED"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("print(1)\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "a.py")
    _git(repo, "config", "core.fsmonitor", f"touch {marker}; false")
    insp = addproject.inspect(repo)
    assert insp.kind == addproject.NEW_FOLDER and insp.preview.tracked == 1
    assert not marker.exists()
    from rce import cli
    assert cli.main(["projects", "add", str(repo)]) == 0
    assert not marker.exists()
    added = addproject.add(repo, label=None, inspected=addproject.inspect(repo).token)
    report = addproject.rescan(added.root)
    assert report.ok and not marker.exists()


# -- a `.rce` that is a symlink (10.6) ------------------------------------------------------


def test_a_rce_symlink_is_refused_and_nothing_is_written_through_it(fake_home, tmp_path):
    """10.8 scenario 7 (refusals) under 10.6, "never follows a symlink out
    of the folder": a folder whose `.rce` points elsewhere (here at
    ~/Documents) is refused with its sentence; adding it writes nothing
    there; and `rce init`'s own identity creation refuses to write through
    such a link."""
    documents = fake_home / "Documents"
    (documents / "thesis.md").write_text("mine\n")
    folder = tmp_path / "w" / "n"
    folder.mkdir(parents=True)
    (folder / "a.py").write_text("")
    os.symlink(documents, folder / ".rce")
    before = _tree(documents, skip_rce=False)
    insp = addproject.inspect(folder)
    assert insp.kind == addproject.REFUSED and insp.refusal.code == "rce_link"
    assert insp.message == addproject.REFUSAL_MESSAGES["rce_link"]
    with pytest.raises(addproject.AddRefused) as refused:
        addproject.add(folder, label=None, inspected=insp.token)
    assert refused.value.code == "rce_link"
    with pytest.raises(identity_mod.IdentityError):
        identity_mod.create_identity(folder)
    assert _tree(documents, skip_rce=False) == before
    assert not registry.registry_path().exists()


# -- no cloud download while looking (10.2) ---------------------------------------------------


class _Spawned:
    """Records every download thread `paths._request_download` starts."""

    def __init__(self, monkeypatch) -> None:
        self.names: list[str] = []
        real = threading.Thread
        spawned = self

        class Recording(real):  # type: ignore[misc, valid-type]
            def __init__(self, *args, **kwargs):
                spawned.names.append(str(kwargs.get("name")))
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(paths.threading, "Thread", Recording)

    @property
    def downloads(self) -> list[str]:
        return [n for n in self.names if n.startswith("rce-download")]


def test_inspecting_never_opens_or_downloads_an_identity_snapshot_in_the_cloud(tmp_path, monkeypatch):
    """10.8 scenario 5 (the 9.4 question in the add dialog) under 10.2's
    "never makes a cloud file download": a folder whose project.toml is gone
    and whose only snapshot of it is still in the cloud is reported as an
    unreadable identity -- never a new folder (9.12) -- without that
    snapshot being opened or its download asked for."""
    folder = tmp_path / "lost"
    folder.mkdir()
    project_identity.init_project(folder)
    (folder / ".rce" / "project.toml").unlink()
    [snapshot] = list((folder / ".rce" / "backups").iterdir())
    assert records_situation.classify(folder).reason == "identity_snapshot_only"
    real = paths.is_dataless
    monkeypatch.setattr(paths, "is_dataless", lambda p: Path(p).name == snapshot.name or real(p))
    opened: list[str] = []
    real_open = open

    def watching_open(file, *args, **kwargs):
        if Path(str(file)).name == snapshot.name:
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    spawned = _Spawned(monkeypatch)
    monkeypatch.setattr("builtins.open", watching_open)
    monkeypatch.setattr("io.open", watching_open)
    try:
        insp = addproject.inspect(folder, deadline=None)
    finally:
        monkeypatch.setattr("builtins.open", real_open)
        monkeypatch.setattr("io.open", real_open)
    assert insp.kind == addproject.RCE_PROJECT and not insp.can_add
    assert insp.classification.situation is records_situation.Situation.UNREADABLE_ID
    assert insp.classification.reason == "identity_snapshot_in_cloud"
    assert opened == [] and spawned.downloads == []


def test_inspecting_a_moved_copy_never_asks_for_the_old_homes_identity_from_the_cloud(tmp_path, monkeypatch):
    """10.8 scenario 5 under 10.2: for a copy whose original's identity
    file is in the cloud, inspecting answers what the identity check
    answers (the original cannot be checked) and asks for no download --
    the identity check itself, outside inspecting, still does."""
    original = tmp_path / "orig"
    original.mkdir()
    project_identity.init_project(original)
    copy = tmp_path / "work2" / "copy"
    shutil.copytree(original, copy)
    old_toml = original / ".rce" / "project.toml"
    real = paths.is_dataless
    monkeypatch.setattr(paths, "is_dataless", lambda p: Path(p) == old_toml or real(p))
    spawned = _Spawned(monkeypatch)
    insp = addproject.inspect(copy, deadline=None)
    assert insp.classification.situation is records_situation.Situation.CANNOT_CHECK
    assert insp.classification.reason == "old_home_identity_in_cloud"
    assert spawned.downloads == []
    assert paths.downloads_allowed()  # suppressed only while inspecting
    records_situation.classify(copy)
    assert spawned.downloads == [f"rce-download:{old_toml.name}"]


# -- what adding writes is what it says (10.2) -------------------------------------------------


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_what_adding_a_new_folder_writes_is_listed_in_full(tmp_path):
    """10.8 scenario 2 with 10.2's "It says what adding writes": the list
    names every file adding creates in the folder -- the identity file, its
    9.12 snapshot in .rce/backups/ and the README -- and nothing else
    appears there."""
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "a.py").write_text("")
    insp = addproject.inspect(folder)
    writes = insp.payload()["writes"]
    assert writes[:3] == [".rce/project.toml", addproject.IDENTITY_SNAPSHOT, ".rce/README"]
    addproject.add(folder, label=None, inspected=insp.token)
    created = _files(folder) - {"a.py"}
    [snap] = [f for f in created if f.startswith(".rce/backups/")]
    assert snap.startswith(".rce/backups/project.toml.") and snap.endswith(".toml")
    assert created == {".rce/project.toml", ".rce/README", snap}


def test_a_pre_v5_folder_whose_graph_opening_moves_says_so(no_project_server, tmp_path, capsys):
    """10.8 scenario 6 with 10.2's "It says what adding writes": a pre-V5
    folder with a pre-8.10 in-project `.rce/graph.db` is said to lose it to
    ~/.rce and to gain the README when it is opened -- which is what then
    happens; one without it is said to be written into the list only."""
    live = no_project_server
    legacy = tmp_path / "legacy"
    (legacy / ".rce").mkdir(parents=True)
    (legacy / "a.py").write_text("")
    conn = db.connect(legacy / ".rce" / "graph.db")
    try:
        db.migrate(conn)
    finally:
        conn.close()
    from rce import cli
    assert cli.main(["projects", "add", str(legacy)]) == 0
    assert "moves its in-project .rce/graph.db" in capsys.readouterr().out
    _, insp = live.post("/api/projects/inspect", {"path": str(legacy)})
    assert insp["kind"] == "pre_v5" and insp["moves_graph"] is True
    assert ".rce/README" in insp["writes"] and any(w.startswith(".rce/graph.db -> ") for w in insp["writes"])
    status, _added = live.post("/api/projects/add", {"path": str(legacy), "label": None, "inspected": insp["inspected"]})
    assert status == 200
    assert _files(legacy) - {"a.py"} == {".rce/README"}
    assert paths.legacy_index_db_path(legacy).exists()


@needs_node
def test_the_page_says_a_pre_v5_graph_moves_and_a_new_folder_gets_a_snapshot():
    """10.8 scenarios 2 and 6: the dialog's sentence follows the engine's
    list -- the snapshot for a new folder, the graph's move for a pre-V5
    folder that has one in-project."""
    out = _wording(
        ("addWritesText", [{"kind": "new_folder"}]),
        ("addWritesText", [{"kind": "pre_v5", "can_add": True, "moves_graph": True}]),
        ("addWritesText", [{"kind": "pre_v5", "can_add": True, "moves_graph": False}]),
    )
    assert ".rce/backups/" in out[0]
    assert "graph.db 会移到 ~/.rce 下" in out[1] and "README" in out[1]
    assert out[2].startswith("加入只会把它写进项目列表。")


# -- the folder written into is the folder checked (10.6) --------------------------------------


def test_a_folder_swapped_for_a_symlink_after_the_check_is_not_written_into(fake_home, tmp_path, monkeypatch):
    """10.8 scenario 8 (the folder changed between look and write) under
    10.6, "the resolved folder is the one checked against the refusals and
    the one written into": the folder replaced by a symlink to ~/Documents
    after add's own re-inspection is refused under the project lock, and
    nothing lands in ~/Documents."""
    documents = fake_home / "Documents"
    (documents / "thesis.md").write_text("mine\n")
    folder = tmp_path / "w" / "n"
    folder.mkdir(parents=True)
    (folder / "a.py").write_text("")
    insp = addproject.inspect(folder)
    before = _tree(documents, skip_rce=False)
    real_init = project_identity.init_project

    def swapping_init(root, **kwargs):
        moved = tmp_path / "w" / "n-aside"
        os.rename(root, moved)
        os.symlink(documents, root)
        return real_init(root, **kwargs)

    monkeypatch.setattr(project_identity, "init_project", swapping_init)
    with pytest.raises(addproject.AddRefused) as refused:
        addproject.add(folder, label=None, inspected=insp.token)
    assert refused.value.code == "inspected_changed"
    assert _tree(documents, skip_rce=False) == before and not (documents / ".rce").exists()
    assert not registry.registry_path().exists()


# -- a NUL in the path (8.8) -----------------------------------------------------------------


def test_a_path_with_a_nul_character_is_refused_in_one_sentence(no_project_server):
    """10.8 scenario 7 (refusals) with 8.8's errors rule: a path holding a
    NUL character is refused with the app's sentence -- not an internal
    error -- and nothing is written."""
    insp = addproject.inspect("/tmp/a\x00b")
    assert insp.kind == addproject.REFUSED and insp.refusal.code == "missing"
    home_before = _home_state()
    status, body = no_project_server.post("/api/projects/inspect", {"path": "/tmp/a\x00b"})
    assert status == 200 and body["kind"] == "refused"
    assert body["message"] == addproject.REFUSAL_MESSAGES["missing"]
    assert _home_state() == home_before


# -- the chosen name survives a second question (10.2) -----------------------------------------


def test_the_chosen_name_survives_an_answer_that_leads_to_a_second_question(no_project_server, tmp_path):
    """10.8 scenario 5 ("each answer then behaves as 9.4 says") with 10.2's
    editable display name: a lost identity restored from its snapshot turns
    out to be a copy; the copy's answer registers the folder under the name
    typed in the add dialog, and the header shows that name meanwhile."""
    live = no_project_server
    original = tmp_path / "orig"
    original.mkdir()
    (original / "a.py").write_text("")
    project_identity.init_project(original)
    registry.register(Path(paths._canonical_path(original)), records_situation.classify(original).project_id)
    lost = tmp_path / "r" / "lost"
    shutil.copytree(original, lost)
    (lost / ".rce" / "project.toml").unlink()
    _, insp = live.post("/api/projects/inspect", {"path": str(lost)})
    assert insp["situation"]["answers"] == ["restore", "adopt"]
    status, added = live.post("/api/projects/add", {"path": str(lost), "label": "丢失的项目", "inspected": insp["inspected"]})
    assert status == 200 and added["blocked"]["situation"] == "lost_id"
    status, answered = live.post("/api/project/resolve", {"answer": "restore"})
    assert status == 200 and answered["blocked"]["situation"] == "copy"
    _, listing = live.get("/api/projects")
    assert listing["current_label"] == "丢失的项目"
    status, answered = live.post("/api/project/resolve", {"answer": "fork"})
    assert status == 200 and answered["blocked"] is None
    entry = next(e for e in registry.load() if e["path"] == paths._canonical_path(lost))
    assert entry["label"] == "丢失的项目"
    load = _src()[_src().index("async function loadProjects()"):]
    assert "data.current_label || basename(current)" in load[: load.index("\n}\n")]


_MATCH_RUNNER = r"""
const fn = require("fs").readFileSync(0, "utf8");
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(([st, entry]) => {
  const state = st;
  return eval("(" + fn + ")")(entry);
})));
"""


@needs_node
def test_a_copy_being_asked_about_is_not_shown_as_its_original():
    """10.8 scenario 5: while the copy's question is open the header does
    not name the original's entry (same id, another folder) -- it shows
    the name chosen in the add dialog; once open, a project is still found
    by its id under another spelling of its folder."""
    fn = _function("matchesCurrent") + "\n}"
    orig = {"id": "p1", "path": "/r/orig"}
    cases = [
        [{"currentPath": "/r/lost", "currentId": "p1", "currentBlocked": True}, orig],
        [{"currentPath": "/r/orig2", "currentId": "p1", "currentBlocked": False}, orig],
        [{"currentPath": "/r/orig", "currentId": "p1", "currentBlocked": True}, orig],
        [{"currentPath": "/r/old", "currentId": None, "currentBlocked": False}, {"id": None, "path": "/r/old"}],
    ]
    out = subprocess.run([NODE, "-e", _MATCH_RUNNER, json.dumps(cases)], input=fn,
                         capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(out.stdout) == [False, True, True, True]


# -- the scan names the files it could not read (10.2, 9.6) -------------------------------------


@needs_node
def test_the_scan_line_names_the_unreadable_files_behind_details_until_dismissed():
    """10.8 scenario 1 (it scans with progress) with 10.2's "Files that
    cannot be read are reported as such": 「扫描完成，有 N 个文件暂时读不了」
    names them behind 「详情」 -- each file once -- and stays until closed."""
    out = _wording(
        ("unreadableFilesText", [["dataflow: code/clean.py", "python: code/clean.py", "r: model.R"]]),
        ("unreadableFilesText", [[]]),
    )
    assert out[0] == "读不了的文件：code/clean.py、model.R。它们恢复可读后，下次扫描会读到。"
    assert out[1] == ""
    done = _function("showScanDone")
    warn = done[done.index("if (last.ok) {"):]
    warn = warn[: warn.index("return;")]
    assert "unreadableFilesText(last.unreadable_sources)" in warn and '"详情"' in warn
    assert "setTimeout" not in warn and "hideScanChip" in warn  # the close button, not a timer


# -- a chosen name survives a move (10.4) ------------------------------------------------------


def test_a_chosen_display_name_survives_the_folder_moving(tmp_path):
    """10.8 scenario 9 (the list: rename) with 10.4's "changes the registry
    label only": re-attaching a moved project keeps a chosen name; a label
    that was only the old folder's name follows the folder (9.4)."""
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    pid = project_identity.init_project(alpha).identity.id
    registry.register(alpha, pid)
    registry.rename(pid, "我的论文")
    moved = tmp_path / "alpha2"
    alpha.rename(moved)
    assert registry.relocate(pid, moved)
    assert registry.find(pid)["label"] == "我的论文"
    registry.register(tmp_path / "alpha3", pid)  # the register() path of a move
    assert registry.find(pid)["label"] == "我的论文"
    beta = tmp_path / "beta"
    beta.mkdir()
    bid = project_identity.init_project(beta).identity.id
    registry.register(beta, bid)
    beta.rename(tmp_path / "beta2")
    registry.relocate(bid, tmp_path / "beta2")
    assert registry.find(bid)["label"] == "beta2"


def test_locating_a_moved_project_keeps_its_chosen_name_over_http(tmp_path):
    """10.8 scenario 9 through 「选择新位置…」 (`/api/projects/locate`)."""
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    pid = project_identity.init_project(alpha).identity.id
    registry.register(Path(paths._canonical_path(alpha)), pid, label="我的论文")
    moved = tmp_path / "alpha2"
    alpha.rename(moved)
    _served, reply = server.locate_payload({"id": pid, "path": str(moved)})
    assert reply["label"] == "我的论文" and registry.find(pid)["label"] == "我的论文"


# -- the no-project page is only the welcome (10.1) -----------------------------------------------


def test_the_no_project_page_hides_the_view_tabs():
    """10.8 scenario 1 (first launch) with 10.1's "the page shows only
    this": no view tabs while there is no project."""
    css = _src()[_src().index("<style>"):_src().index("</style>")]
    assert "body.no-project .tabs { display: none !important; }" in css
