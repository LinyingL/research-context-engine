"""DESIGN.md Section 10 (task V6), phase A: adding a project from the app --
the engine (`rce.addproject`), the server endpoints, the registry, and the
command line. Each test names the 10.8 acceptance scenario it covers.

Every test runs with `RCE_HOME` in a throwaway directory (conftest) and a
throwaway `HOME` where a test needs the home folder's top-level folders.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from rce import addproject, cli, db, paths
from rce import project as project_identity
from rce.ingest import files as files_ingest
from rce.ingest import scan as scan_mod
from rce.records import identity as identity_mod
from rce.records import judgements
from rce.records import lock as records_lock
from rce.records import situation as records_situation
from rce.webapp import registry, server


# -- helpers ----------------------------------------------------------------------


def _tree(root: Path, *, skip_rce: bool = True) -> dict[str, tuple[bytes, int]]:
    """Every file under `root` (outside `.rce/` unless asked): its bytes
    and its modification time in nanoseconds -- 10.8 #2's comparison."""
    found: dict[str, tuple[bytes, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if skip_rce and rel_dir.parts[:1] == (".rce",):
            dirnames[:] = []
            continue
        for name in filenames:
            full = Path(dirpath) / name
            rel = (rel_dir / name).as_posix()
            if skip_rce and rel.startswith(".rce/"):
                continue
            found[rel] = (full.read_bytes(), os.lstat(full).st_mtime_ns)
    return found


def _home_state() -> dict[str, tuple[bytes, int]]:
    """Everything under RCE_HOME except the lock files (taking a lock is
    not a write of state)."""
    home = paths.rce_home()
    if not home.exists():
        return {}
    return {k: v for k, v in _tree(home, skip_rce=False).items() if not k.startswith("locks/")}


def _research_folder(root: Path) -> Path:
    """A small research folder: scripts, data, drafts, images, other."""
    (root / "code").mkdir(parents=True)
    (root / "code" / "clean.py").write_text('import pandas as pd\npd.read_csv("data/raw.csv")\n')
    (root / "code" / "model.R").write_text('x <- read.csv("data/raw.csv")\n')
    (root / "data").mkdir()
    (root / "data" / "raw.csv").write_text("a,b\n1,2\n")
    (root / "paper.md").write_text("# Results\n\nSome text.\n")
    (root / "paper.tex").write_text("\\section{Intro}\n")
    (root / "fig.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "notes.txt").write_text("not read\n")
    (root / ".hidden").write_text("skipped\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"\0")
    return root


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        check=True, capture_output=True,
    )


def _legacy(root: Path) -> None:
    """A pre-V5 project: no identity, an index at its path hash (as
    tests/test_project_identity.py builds one)."""
    root.mkdir(parents=True)
    (root / "a.py").write_text('import pandas as pd\npd.read_csv("data.csv")\n')
    (root / "data.csv").write_text("x\n")
    paths.legacy_graph_dir(root).mkdir(parents=True)
    conn = db.connect(paths.legacy_index_db_path(root))
    try:
        db.migrate(conn)
    finally:
        conn.close()


def _inventory_read(project_id: str) -> set[str]:
    """What the scan read: the inventory's sources, READ_AND_PARSED."""
    conn = db.connect(records_situation.index_db_path(project_id))
    try:
        return {r["source"] for r in db.scan_sources_of(conn, scan_mod.INVENTORY) if r["status"] == scan_mod.READ_AND_PARSED}
    finally:
        conn.close()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway home folder with the top-level folders 10.2 names."""
    home = tmp_path / "home"
    for name in ("Documents", "Desktop", "Downloads", "Library/Mobile Documents/com~apple~CloudDocs"):
        (home / name).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


class Live:
    def __init__(self, httpd: server.RceHTTPServer) -> None:
        self.httpd = httpd
        self.port = httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def get(self, path: str) -> tuple[int, Any]:
        try:
            with urllib.request.urlopen(self.base + path) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def post(self, path: str, body: dict | None = None) -> tuple[int, Any]:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body or {}).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def raw(self, method: str, path: str, headers: dict[str, str], body: dict | None = None) -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        try:
            conn.request(method, path, body=json.dumps(body or {}).encode("utf-8"), headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, (json.loads(data) if data else None)
        finally:
            conn.close()

    def wait_scan(self, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, gen = self.get("/api/generation")
            if gen.get("scanning") is None and gen.get("last_scan") is not None:
                return gen
            time.sleep(0.05)
        raise AssertionError("the scan did not finish")


def _live(project: Path | None) -> tuple[Live, threading.Thread]:
    httpd = server.build_server(project, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return Live(httpd), thread


@pytest.fixture
def no_project_server():
    live, thread = _live(None)
    try:
        yield live
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def project_server(tmp_path: Path):
    project = tmp_path / "served"
    project.mkdir()
    project_identity.init_project(project)
    registry.register(Path(paths._canonical_path(project)), records_situation.classify(project).project_id)
    live, thread = _live(project)
    try:
        yield live, project
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()
        thread.join(timeout=5)


# -- 10.8 scenario 1 and the no-project state ------------------------------------------


def test_no_project_state_answers_every_project_endpoint_with_409(no_project_server):
    """10.8 scenario 1 (first launch) / 10.1: with no project the engine
    serves anyway; the project list is empty with current null, and every
    project endpoint answers 409 `no_project` with a Chinese message."""
    live = no_project_server
    status, listing = live.get("/api/projects")
    assert status == 200 and listing["projects"] == [] and listing["current"] is None
    status, engine = live.get("/api/engine")
    assert status == 200 and engine["project_root"] is None
    status, gen = live.get("/api/generation")
    assert status == 200 and gen["scanning"] is None and gen["last_scan"] is None
    for path in ("/api/summary", "/api/tree", "/api/canvas", "/api/file?path=x", "/api/records", "/api/review"):
        status, body = live.get(path)
        assert status == 409 and body["state"] == "no_project", path
        assert body["error"] == server.NO_PROJECT_MESSAGE
    for path in ("/api/attempts/write", "/api/projects/rescan", "/api/project/resolve", "/api/canvas/layout", "/api/open"):
        status, body = live.post(path, {"path": "x"})
        assert status == 409 and body["state"] == "no_project", path


def test_first_launch_adds_a_folder_scans_it_with_progress_and_fills_the_views(no_project_server, tmp_path):
    """10.8 scenario 1: from the no-project page, inspect then add a folder;
    it becomes the served project, scans in the background with progress
    in /api/generation, and the views fill when it ends."""
    live = no_project_server
    folder = _research_folder(tmp_path / "study")
    status, insp = live.post("/api/projects/inspect", {"path": str(folder)})
    assert status == 200 and insp["kind"] == "new_folder" and insp["can_add"]
    status, added = live.post("/api/projects/add", {"path": str(folder), "label": "我的研究", "inspected": insp["inspected"]})
    assert status == 200, added
    assert added["kind"] == "new_folder" and added["scanning"] is True
    assert added["current"] == paths._canonical_path(folder)
    gen = live.wait_scan()
    assert gen["last_scan"]["ok"] is True and gen["last_scan"]["unreadable_sources"] == []
    _, listing = live.get("/api/projects")
    assert listing["current_id"] == added["project_id"]
    assert [p["label"] for p in listing["projects"]] == ["我的研究"]
    status, summary = live.get("/api/summary")
    assert status == 200 and summary["nodes"]["script"] >= 2


# -- 10.8 scenario 2: a new folder, not a git repository --------------------------------


def test_new_folder_preview_counts_equal_what_the_first_scan_reads(tmp_path):
    """10.8 scenario 2: the preview's counts come from the scan's own
    inventory (the walk); they equal what the first scan in fact reads."""
    folder = _research_folder(tmp_path / "study")
    insp = addproject.inspect(folder)
    assert insp.kind == addproject.NEW_FOLDER
    pv = insp.preview
    assert pv.source == "walk"
    assert pv.counts == {"scripts": 2, "data": 1, "drafts": 2, "images": 1, "other": 1}
    assert pv.to_scan == 6 and not pv.large and not pv.truncated and pv.dataless == 0
    assert pv.inventory == {c: len(v) for c, v in files_ingest.list_source_files(folder).items()}
    added = addproject.add(folder, label="Study", inspected=insp.token)
    addproject.rescan(added.root)
    assert len(_inventory_read(added.project_id)) == pv.to_scan


def test_new_folder_after_adding_holds_only_rce_and_every_other_file_is_unchanged(tmp_path):
    """10.8 scenario 2: afterwards the folder holds `.rce/project.toml` and
    `.rce/README` and is otherwise unchanged -- every other file's bytes and
    modification time compared before and after; the registry has the
    chosen display name; nothing is under review."""
    folder = _research_folder(tmp_path / "study")
    before = _tree(folder)
    before_names = sorted(p.name for p in folder.iterdir())
    insp = addproject.inspect(folder)
    added = addproject.add(folder, label="  我的研究  ", inspected=insp.token)
    report = addproject.rescan(added.root)
    assert report.ok
    assert _tree(folder) == before
    assert sorted(p.name for p in folder.iterdir()) == sorted(before_names + [".rce"])
    assert (folder / ".rce" / "project.toml").is_file() and (folder / ".rce" / "README").is_file()
    assert set(os.listdir(folder / ".rce")) <= {"project.toml", "README", "backups"}
    [entry] = registry.load()
    assert entry["label"] == "我的研究" and entry["id"] == added.project_id
    conn = db.connect(records_situation.index_db_path(added.project_id))
    try:
        assert judgements.review_count(conn) == 0
    finally:
        conn.close()


def test_inspecting_writes_nothing_anywhere_and_opens_no_file(tmp_path, monkeypatch):
    """10.2 / 10.6: inspecting writes nothing anywhere (the folder and RCE's
    home byte-for-byte unchanged, no registry) and never opens a file of
    the folder."""
    folder = _research_folder(tmp_path / "study")
    before, home_before = _tree(folder, skip_rce=False), _home_state()
    opened: list[str] = []
    real_open = open

    def watching_open(file, *args, **kwargs):
        if str(file).startswith(str(folder)) or str(file).startswith(paths._canonical_path(folder)):
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", watching_open)
    monkeypatch.setattr("io.open", watching_open)  # what pathlib's read_bytes calls
    insp = addproject.inspect(folder)
    monkeypatch.setattr("builtins.open", real_open)
    monkeypatch.setattr("io.open", real_open)
    assert insp.kind == addproject.NEW_FOLDER
    assert opened == []
    assert _tree(folder, skip_rce=False) == before and not (folder / ".rce").exists()
    assert _home_state() == home_before and not registry.registry_path().exists()


def test_preview_counts_cloud_files_and_says_large_and_truncated(tmp_path, monkeypatch):
    """10.2 / 10.6: files still in the cloud are counted (by `st_flags`,
    never read); above 5,000 files to scan it says so; the listing stops
    at 100,000 entries and says so."""
    folder = tmp_path / "big"
    folder.mkdir()
    for i in range(12):
        (folder / f"s{i}.py").write_text("")
    (folder / "x.txt").write_text("")
    monkeypatch.setattr(addproject, "_is_dataless", lambda full: full.name in ("s1.py", "s2.py"))
    monkeypatch.setattr(addproject, "LARGE_THRESHOLD", 10)
    insp = addproject.inspect(folder)
    assert insp.preview.dataless == 2 and insp.preview.large and not insp.preview.truncated
    monkeypatch.setattr(addproject, "ENTRY_CAP", 5)
    insp = addproject.inspect(folder)
    assert insp.preview.truncated and insp.preview.to_scan + insp.preview.other == 5


def test_preview_never_follows_a_symlink_out_of_the_folder(tmp_path):
    """10.6: the walk below the chosen folder follows no symlink."""
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "x.py").write_text("")
    (outside / "y.py").write_text("")
    folder = tmp_path / "study"
    folder.mkdir()
    (folder / "a.py").write_text("")
    os.symlink(outside / "deep", folder / "linked_dir")
    os.symlink(outside / "y.py", folder / "linked.py")
    insp = addproject.inspect(folder)
    assert insp.preview.to_scan == 1 and insp.preview.other == 0


# -- 10.8 scenario 3: a git repository with untracked files -----------------------------


def test_git_repository_preview_names_tracked_and_untracked_and_the_scan_reads_exactly_the_tracked(tmp_path):
    """10.8 scenario 3: the preview names the tracked count and the
    untracked count, and the scan reads exactly the tracked files."""
    folder = _research_folder(tmp_path / "repo")
    (folder / ".gitignore").write_text("ignored.csv\n")
    _git(folder, "init", "-q")
    _git(folder, "add", "code/clean.py", "data/raw.csv", "paper.md", ".gitignore", "notes.txt")
    _git(folder, "commit", "-qm", "init")
    (folder / "ignored.csv").write_text("x\n")
    insp = addproject.inspect(folder)
    pv = insp.preview
    assert insp.kind == addproject.NEW_FOLDER and pv.source == "git"
    # tracked: clean.py, raw.csv, paper.md, .gitignore, notes.txt
    assert pv.tracked == 5 and pv.to_scan == 3 and pv.other == 2
    # untracked, not ignored: model.R, paper.tex, fig.png, .hidden, __pycache__/x.pyc
    assert pv.untracked == 5
    added = addproject.add(folder, label=None, inspected=insp.token)
    assert added.label == "repo"
    addproject.rescan(added.root)
    assert _inventory_read(added.project_id) == {"code/clean.py", "data/raw.csv", "paper.md"}


# -- 10.8 scenario 4: already in the list ------------------------------------------------


def test_already_in_the_list_is_offered_the_switch_and_nothing_is_written(project_server, tmp_path):
    """10.8 scenario 4: a folder already in the list is offered 「切换过去」;
    adding it writes nothing and switches nothing."""
    live, project = project_server
    other = tmp_path / "other"
    other.mkdir()
    project_identity.init_project(other)
    registry.register(Path(paths._canonical_path(other)), records_situation.classify(other).project_id, label="Other")
    status, insp = live.post("/api/projects/inspect", {"path": str(other)})
    assert insp["kind"] == "already_registered" and insp["entry"]["label"] == "Other"
    assert insp["message"] == "这个项目已经在列表里" and not insp["can_add"]
    reg_before, home_before = registry.registry_path().read_bytes(), _home_state()
    status, added = live.post("/api/projects/add", {"path": str(other), "label": "X", "inspected": insp["inspected"]})
    assert status == 200 and added["kind"] == "already_registered" and added["current"] is None
    assert registry.registry_path().read_bytes() == reg_before and _home_state() == home_before
    _, listing = live.get("/api/projects")
    assert listing["current"] == str(project)


# -- 10.8 scenario 5: a copy of a V5 project --------------------------------------------


def _copy_of_a_project(tmp_path: Path) -> tuple[Path, Path]:
    original = tmp_path / "original"
    original.mkdir()
    (original / "a.py").write_text("")
    project_identity.init_project(original)
    copy = tmp_path / "copy"
    shutil.copytree(original, copy)
    return original, copy


def test_copy_of_a_v5_project_returns_its_question_and_writes_nothing(tmp_path):
    """10.8 scenario 5: the 9.4 question and its answers, exactly as the
    identity check reports them; adding writes nothing, the registry
    included, until it is answered."""
    _original, copy = _copy_of_a_project(tmp_path)
    insp = addproject.inspect(copy)
    assert insp.kind == addproject.RCE_PROJECT and not insp.can_add
    c = records_situation.classify(copy)
    assert insp.payload()["situation"] == c.payload()
    assert insp.payload()["situation"]["answers"] == ["fork", "claim", "other"]
    before, home_before = _tree(copy, skip_rce=False), _home_state()
    added = addproject.add(copy, label="Copy", inspected=insp.token)
    assert added.blocked and added.entry is None and not added.registered
    assert _tree(copy, skip_rce=False) == before and _home_state() == home_before
    assert not registry.registry_path().exists()


def test_copy_added_from_the_app_is_served_blocked_and_answering_registers_it_under_the_chosen_name(
    no_project_server, tmp_path,
):
    """10.8 scenario 5: the engine serves the copy's question; nothing is
    registered until it is answered; the answer, through the existing
    /api/project/resolve, registers it -- under the name chosen."""
    live = no_project_server
    original, copy = _copy_of_a_project(tmp_path)
    _, insp = live.post("/api/projects/inspect", {"path": str(copy)})
    assert insp["situation"]["situation"] == "copy"
    status, added = live.post("/api/projects/add", {"path": str(copy), "label": "分支", "inspected": insp["inspected"]})
    assert status == 200 and added["blocked"]["situation"] == "copy" and added["registered"] is False
    assert registry.load() == []
    status, body = live.get("/api/summary")
    assert status == 409 and body["state"] == "project_blocked"
    status, answered = live.post("/api/project/resolve", {"answer": "fork"})
    assert status == 200 and answered["blocked"] is None
    [entry] = registry.load()
    assert entry["label"] == "分支" and entry["id"] == answered["project_id"] != records_situation.classify(original).project_id
    status, _ = live.get("/api/summary")
    assert status == 200


# -- 10.8 scenario 6: a project from before V5 --------------------------------------------


def test_pre_v5_project_is_registered_and_opened_frozen_and_no_scan_writes_into_its_old_index(
    no_project_server, tmp_path,
):
    """10.8 scenario 6: registered and opened; the migration banner shows
    (needs_migration); a rescan is refused and the old index is untouched."""
    live = no_project_server
    old = tmp_path / "old"
    _legacy(old)
    legacy_db = paths.legacy_index_db_path(old)
    db_before = (legacy_db.read_bytes(), legacy_db.stat().st_mtime_ns)
    _, insp = live.post("/api/projects/inspect", {"path": str(old)})
    assert insp["kind"] == "pre_v5" and insp["can_add"]
    assert insp["message"] == "这是旧版 RCE 项目：加入后需要先迁移，才能记录判断"
    status, added = live.post("/api/projects/add", {"path": str(old), "label": "旧项目", "inspected": insp["inspected"]})
    assert status == 200 and added["kind"] == "pre_v5" and added["scanning"] is False
    [entry] = registry.load()
    assert entry["label"] == "旧项目" and entry["id"] is None
    _, listing = live.get("/api/projects")
    assert listing["needs_migration"] is True and listing["current"] == entry["path"]
    status, body = live.post("/api/projects/rescan", {})
    assert status == 409 and body["state"] == "frozen" and body["error"] == addproject.SCAN_MESSAGES["frozen"]
    with pytest.raises(addproject.ScanRefused) as refused:
        addproject.rescan(old)
    assert refused.value.code == "frozen"
    assert (legacy_db.read_bytes(), legacy_db.stat().st_mtime_ns) == db_before
    assert not (old / ".rce").exists()


# -- 10.8 scenario 7: refusals -------------------------------------------------------------


@pytest.mark.parametrize("where", ["root", "home", "documents", "desktop", "downloads", "library", "icloud"])
def test_top_level_folders_are_refused_when_chosen_exactly(fake_home, where):
    """10.8 scenario 7: `/`, the home folder, ~/Documents (and the other
    top-level folders 10.2 names), each refused with its sentence."""
    target = {
        "root": Path("/"), "home": fake_home, "documents": fake_home / "Documents",
        "desktop": fake_home / "Desktop", "downloads": fake_home / "Downloads",
        "library": fake_home / "Library",
        "icloud": fake_home / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
    }[where]
    home_before = _home_state()
    insp = addproject.inspect(target)
    assert insp.kind == addproject.REFUSED and insp.refusal.code == "top_level"
    assert insp.message == "请选择具体的项目文件夹，而不是「文稿」这样的总文件夹"
    with pytest.raises(addproject.AddRefused) as refused:
        addproject.add(target, label=None, inspected=insp.token)
    assert refused.value.code == "top_level"
    assert _home_state() == home_before and not registry.registry_path().exists()


def test_top_level_folders_match_exactly_so_a_project_inside_them_is_addable(fake_home):
    """10.2: the top-level folders are matched exactly -- a project folder
    inside ~/Documents is addable."""
    project = fake_home / "Documents" / "thesis"
    project.mkdir()
    (project / "a.py").write_text("")
    assert addproject.inspect(project).kind == addproject.NEW_FOLDER


def test_tmp_test_folders_remain_addable():
    """10.2: /tmp is refused only as itself; a folder below /private/tmp
    (and below /private/var/folders, where pytest's tmp_path lives) is
    addable."""
    if not Path("/private/tmp").is_dir():
        pytest.skip("no /private/tmp on this platform")
    folder = Path(tempfile.mkdtemp(dir="/private/tmp"))
    try:
        assert addproject.inspect(folder).kind == addproject.NEW_FOLDER
        assert addproject.inspect("/private/tmp").refusal.code == "top_level"
        assert addproject.inspect("/tmp").refusal.code == "top_level"
    finally:
        shutil.rmtree(folder)


@pytest.mark.parametrize("system", ["/usr/local", "/System/Library", "/etc", "/Library/Application Support", "/bin"])
def test_system_folders_are_refused_by_containment(system):
    """10.8 scenario 7: anything inside /System, /Library, /Applications,
    /usr, /bin, /sbin, /etc."""
    if not Path(system).is_dir():
        pytest.skip(f"{system} is not on this machine")
    insp = addproject.inspect(system)
    assert insp.kind == addproject.REFUSED and insp.refusal.code in ("system_folder", "top_level")


def test_a_file_and_a_missing_path_are_refused(tmp_path):
    """10.8 scenario 7: a file, a missing path."""
    (tmp_path / "f.txt").write_text("x")
    insp = addproject.inspect(tmp_path / "f.txt")
    assert insp.refusal.code == "not_a_folder" and insp.message == "这不是一个文件夹"
    insp = addproject.inspect(tmp_path / "nope")
    assert insp.refusal.code == "missing" and insp.message == "这个文件夹不存在"
    assert addproject.inspect("").refusal.code == "missing"


def test_rce_home_and_anything_inside_it_are_refused(tmp_path):
    """10.8 scenario 7: `~/.rce` (RCE_HOME) or anything inside it."""
    home = paths.rce_home()
    (home / "graphs").mkdir(parents=True, exist_ok=True)
    for target in (home, home / "graphs"):
        insp = addproject.inspect(target)
        assert insp.refusal.code == "rce_home"


def test_inside_or_containing_a_registered_project_is_refused_and_a_gone_entry_does_not_count(tmp_path):
    """10.8 scenario 7: a folder inside a registered project, a folder
    containing one -- compared as canonical paths; an entry whose folder
    is gone does not count."""
    outer = tmp_path / "outer"
    project = outer / "proj"
    (project / "sub").mkdir(parents=True)
    project_identity.init_project(project)
    registry.register(Path(paths._canonical_path(project)), records_situation.classify(project).project_id, label="主项目")
    reg_before = registry.registry_path().read_bytes()
    insp = addproject.inspect(project / "sub")
    assert insp.refusal.code == "inside_project" and insp.message == "这个文件夹在项目「主项目」里面"
    insp = addproject.inspect(outer)
    assert insp.refusal.code == "contains_project" and insp.message == "这个文件夹里已经有项目「主项目」"
    # Spelled through a symlink: the same canonical folder, the same refusal.
    os.symlink(project, tmp_path / "alias")
    assert addproject.inspect(tmp_path / "alias" / "sub").refusal.code == "inside_project"
    with pytest.raises(addproject.AddRefused):
        addproject.add(outer, label=None, inspected=insp.token)
    assert registry.registry_path().read_bytes() == reg_before
    # A gone entry does not count.
    gone = outer / "gone"
    gone.mkdir()
    registry.register(gone)
    gone.rmdir()
    registry.remove(str(project.resolve()))
    registry.remove(paths._canonical_path(project))
    assert addproject.inspect(outer).kind == addproject.NEW_FOLDER


def test_a_symlink_that_resolves_into_a_refused_place_is_refused(fake_home, tmp_path):
    """10.6 / 10.8 scenario 7: the chosen path is resolved, symlinks
    followed, and the resolved folder is the one checked."""
    os.symlink(fake_home / "Documents", tmp_path / "docs-link")
    insp = addproject.inspect(tmp_path / "docs-link")
    assert insp.refusal.code == "top_level"
    os.symlink(paths.rce_home(), tmp_path / "home-link")
    assert addproject.inspect(tmp_path / "home-link").refusal.code == "rce_home"


def test_refusals_over_http_carry_the_sentence_and_write_nothing(no_project_server, fake_home, tmp_path):
    """10.8 scenario 7, through the endpoint: a refusal is a 200 inspection
    with its Chinese sentence; adding it anyway is 409 with that code."""
    live = no_project_server
    _, insp = live.post("/api/projects/inspect", {"path": "~/Documents"})
    assert insp["kind"] == "refused" and insp["refusal"]["code"] == "top_level"
    assert insp["message"] == "请选择具体的项目文件夹，而不是「文稿」这样的总文件夹" and not insp["can_add"]
    status, body = live.post("/api/projects/add", {"path": "~/Documents", "label": "x", "inspected": insp["inspected"]})
    assert status == 409 and body["state"] == "top_level" and body["error"] == insp["message"]
    status, body = live.post("/api/projects/inspect", {"path": "relative/path"})
    assert status == 400 and body["state"] == "bad_path"
    assert not registry.registry_path().exists()


# -- 10.8 scenario 8: the folder changed between look and write -----------------------------


def test_a_project_toml_appearing_after_inspecting_refuses_the_add(no_project_server, tmp_path):
    """10.8 scenario 8: a `project.toml` appears after inspecting; adding
    is refused (`inspected_changed`), the fresh inspection comes back, and
    nothing is written."""
    live = no_project_server
    folder = _research_folder(tmp_path / "study")
    _, insp = live.post("/api/projects/inspect", {"path": str(folder)})
    identity_mod.create_identity(folder)  # someone else initialised it meanwhile
    toml_before = (folder / ".rce" / "project.toml").read_bytes()
    status, body = live.post("/api/projects/add", {"path": str(folder), "label": "x", "inspected": insp["inspected"]})
    assert status == 409 and body["state"] == "inspected_changed"
    assert body["error"] == addproject.INSPECTED_CHANGED_MESSAGE
    assert body["inspection"]["kind"] == "rce_project"
    assert registry.load() == [] and (folder / ".rce" / "project.toml").read_bytes() == toml_before
    assert not (paths.rce_home() / "graphs").exists()


def test_a_token_from_another_folder_or_made_up_is_refused(tmp_path):
    """10.2: the token names what was seen; it is not accepted for another
    folder, nor guessable from the path."""
    a = _research_folder(tmp_path / "a")
    b = _research_folder(tmp_path / "b")
    token_a = addproject.inspect(a).token
    for token in (token_a, hashlib.sha256(str(b).encode()).hexdigest(), ""):
        with pytest.raises(addproject.AddRefused) as refused:
            addproject.add(b, label=None, inspected=token)
        assert refused.value.code == "inspected_changed"
    assert not (b / ".rce").exists() and registry.load() == []


# -- 10.8 scenario 9: the list ----------------------------------------------------------------


def test_rename_remove_another_and_remove_the_open_one(project_server, tmp_path):
    """10.8 scenario 9: rename (the label only); remove another project;
    remove the open one -- the next opens, then the no-project state; the
    folders and their records are untouched."""
    live, project = project_server
    other = tmp_path / "other"
    other.mkdir()
    project_identity.init_project(other)
    other_id = records_situation.classify(other).project_id
    third = tmp_path / "third"
    third.mkdir()
    project_identity.init_project(third)
    registry.register(Path(paths._canonical_path(third)), records_situation.classify(third).project_id)
    registry.register(Path(paths._canonical_path(other)), other_id)
    served_id = records_situation.classify(project).project_id
    trees = {p: _tree(p, skip_rce=False) for p in (project, other, third)}

    status, body = live.post("/api/projects/rename", {"id": served_id, "label": "  新名字 "})
    assert status == 200 and body["entry"]["label"] == "新名字" and project.name == "served"
    status, body = live.post("/api/projects/rename", {"id": served_id, "label": "a\nb"})
    assert status == 400 and body["state"] == "bad_label" and body["error"] == "显示名称只能有一行"
    status, body = live.post("/api/projects/rename", {"id": "p-" + "0" * 32, "label": "x"})
    assert status == 403 and body["state"] == "unknown_project"

    status, body = live.post("/api/projects/remove", {"path": paths._canonical_path(third)})
    assert status == 200 and body == {"removed": paths._canonical_path(third)}
    _, listing = live.get("/api/projects")
    assert listing["current_id"] == served_id

    served_entry = next(e for e in registry.load() if e["id"] == served_id)
    status, body = live.post("/api/projects/remove", {"path": served_entry["path"]})
    assert status == 200 and body["project_id"] == other_id and body["no_project"] is False
    _, listing = live.get("/api/projects")
    assert listing["current_id"] == other_id

    status, body = live.post("/api/projects/remove", {"path": registry.load()[0]["path"]})
    assert status == 200 and body["no_project"] is True and body["current"] is None
    _, listing = live.get("/api/projects")
    assert listing["projects"] == [] and listing["current"] is None
    assert {p: _tree(p, skip_rce=False) for p in (project, other, third)} == trees


def test_registry_rename_changes_the_label_only_and_cleans_it(tmp_path):
    """10.4: label only, never the folder; trimmed, one line, capped."""
    folder = tmp_path / "p"
    folder.mkdir()
    registry.register(folder)
    path = str(folder.resolve())
    assert registry.rename(path, "  Name ")["label"] == "Name"
    [entry] = registry.load()
    assert entry == {"id": None, "path": path, "label": "Name"} and folder.is_dir()
    for bad, code in (("", "empty"), ("   ", "empty"), ("a\rb", "multiline"), ("x" * 101, "too_long"), (None, "empty")):
        with pytest.raises(registry.LabelError) as err:
            registry.rename(path, bad)
        assert err.value.code == code
    assert registry.rename("/not/registered", "x") is None


# -- 10.8 scenario 10: rescan ------------------------------------------------------------------


def test_rescan_from_the_menu_reports_progress_and_a_second_request_is_refused(project_server, monkeypatch):
    """10.8 scenario 10: the full scan in the background with its progress
    line in /api/generation; a second request while it runs is refused."""
    live, project = project_server
    (project / "a.py").write_text('import pandas as pd\npd.read_csv("d.csv")\n')
    release = threading.Event()
    entered = threading.Event()
    real = addproject.pipeline.ingest_sources

    def slow_sources(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(addproject.pipeline, "ingest_sources", slow_sources)
    status, body = live.post("/api/projects/rescan", {})
    assert status == 200 and body["scanning"]["m"] == 4
    assert entered.wait(5)
    _, gen = live.get("/api/generation")
    assert gen["scanning"]["step"] == "sources" and gen["scanning"]["n"] == 1 and gen["scanning"]["m"] == 4
    assert gen["scanning"]["label"] == addproject.STEP_LABELS["sources"] and gen["scanning"]["started"]
    status, body = live.post("/api/projects/rescan", {})
    assert status == 409 and body["state"] == "scan_running" and body["error"] == "这个项目正在扫描，请等它结束"
    generation_before = gen["generation"]
    release.set()
    gen = live.wait_scan()
    assert gen["last_scan"]["ok"] is True and gen["last_scan"]["finished"]
    assert gen["generation"] > generation_before
    _, summary = live.get("/api/summary")
    assert summary["nodes"]["script"] == 1


def test_rescan_is_refused_while_another_process_holds_the_project_lock(tmp_path, monkeypatch):
    """10.3: one scan at a time per project, across processes too: the
    project lock held elsewhere for longer than a short wait is
    `scan_running`, and nothing is scanned."""
    folder = tmp_path / "p"
    folder.mkdir()
    project_identity.init_project(folder)
    project_id = records_situation.classify(folder).project_id
    monkeypatch.setattr(addproject, "SCAN_LOCK_TIMEOUT_S", 0.2)
    holding, done = threading.Event(), threading.Event()

    def hold():
        with records_lock.project_lock(folder, project_id):
            holding.set()
            done.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holding.wait(5)
        with pytest.raises(addproject.ScanRefused) as refused:
            addproject.rescan(folder)
        assert refused.value.code == "scan_running"
    finally:
        done.set()
        holder.join(5)
    assert not addproject.scanning(folder)
    assert addproject.rescan(folder).ok  # the slot was released


def test_rescan_runs_the_attempt_check_and_mappings_in_order(tmp_path):
    """10.3: the same calls rce ingest, rce attempts --check, rce mappings
    and the record's application make, in that order."""
    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "map.md").write_text(
        "## H\n\n| # | date | desc | vars | result | verdict |\n|---|---|---|---|---|---|\n"
        "| 1 | 2026-01-01 | d | v | r | ok |\n"
    )
    project_identity.init_project(folder)
    (folder / ".rce" / "attempts.toml").write_text(
        'file = "map.md"\nheading = "H"\n\n[columns]\nid = "#"\ndate = "date"\ndescription = "desc"\n'
        'variables = "vars"\nresult = "result"\nverdict = "verdict"\n'
    )
    steps: list[tuple[str, int, int]] = []
    report = addproject.rescan(folder, progress=lambda *a: steps.append(a))
    assert steps == [("sources", 1, 4), ("attempts", 2, 4), ("mappings", 3, 4), ("judgements", 4, 4)]
    assert report.ok and report.findings is not None
    conn = db.connect(records_situation.index_db_path(records_situation.classify(folder).project_id))
    try:
        assert [n["id"] for n in db.get_nodes_by_type(conn, "attempt")] == ["attempt:map.md#1"]
    finally:
        conn.close()


# -- 10.8 scenario 11: origin ------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["/api/projects/inspect", "/api/projects/add", "/api/projects/rescan", "/api/projects/rename"])
@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Host": "evil.example:80"},
])
def test_new_endpoints_refuse_cross_origin_and_wrong_host_and_write_nothing(project_server, tmp_path, endpoint, headers):
    """10.8 scenario 11: cross-origin and wrong-Host requests to every new
    endpoint: 403, and nothing written."""
    live, project = project_server
    folder = _research_folder(tmp_path / "victim")
    insp = addproject.inspect(folder)
    served_id = records_situation.classify(project).project_id
    body = {"path": str(folder), "label": "x", "inspected": insp.token, "id": served_id}
    sent = {"Host": f"127.0.0.1:{live.port}", "Content-Type": "application/json", **headers}
    reg_before, home_before = registry.registry_path().read_bytes(), _home_state()
    status, reply = live.raw("POST", endpoint, sent, body)
    assert status == 403
    assert registry.registry_path().read_bytes() == reg_before and _home_state() == home_before
    assert not (folder / ".rce").exists()
    _, gen = live.get("/api/generation")
    assert gen["scanning"] is None


# -- 10.8 scenario 12: waiting for the system --------------------------------------------------


def test_a_blocking_listing_answers_waiting_within_the_deadline_and_completes_later(tmp_path, monkeypatch):
    """10.8 scenario 12: an inspection whose listing blocks answers with the
    waiting state within the deadline; asked again while it still runs,
    no second listing starts; once it returns, the answer is complete."""
    folder = _research_folder(tmp_path / "held")
    release = threading.Event()
    calls: list[str] = []
    real = files_ingest.iter_files

    def blocking_listing(root):
        calls.append(str(root))
        release.wait(10)
        return real(root)

    monkeypatch.setattr(files_ingest, "iter_files", blocking_listing)
    started = time.monotonic()
    first = addproject.inspect(folder, deadline=0.3)
    assert first.kind == addproject.WAITING_PERMISSION
    assert time.monotonic() - started < 2.0
    assert first.payload()["message"] == "正在等待系统授权访问这个文件夹……如果系统询问，请点“允许”"
    second = addproject.inspect(folder, deadline=0.2)
    assert second.kind == addproject.WAITING_PERMISSION and len(calls) == 1
    with pytest.raises(addproject.AddRefused) as refused:
        addproject.add(folder, label=None, inspected=first.token, deadline=0.1)
    assert refused.value.code == "waiting_permission" and len(calls) == 1
    release.set()
    third = addproject.inspect(folder, deadline=5)
    assert third.kind == addproject.NEW_FOLDER and third.preview.to_scan == 6
    assert len(calls) == 1
    assert not (folder / ".rce").exists()


def test_waiting_over_http_and_then_the_answer(no_project_server, tmp_path, monkeypatch):
    """10.8 scenario 12, through the endpoint: the page asks again until
    the listing returns."""
    live = no_project_server
    folder = _research_folder(tmp_path / "held")
    release = threading.Event()
    real = files_ingest.iter_files
    monkeypatch.setattr(files_ingest, "iter_files", lambda root: (release.wait(10), real(root))[1])
    monkeypatch.setattr(addproject, "DEFAULT_DEADLINE_S", 0.2)
    status, body = live.post("/api/projects/inspect", {"path": str(folder)})
    assert status == 200 and body["kind"] == "waiting_permission"
    release.set()
    status, body = live.post("/api/projects/inspect", {"path": str(folder)})
    assert status == 200 and body["kind"] == "new_folder"


# -- the command line (10.5) ---------------------------------------------------------------------


def test_cli_projects_add_without_yes_prints_the_preview_and_writes_nothing(tmp_path, capsys):
    """10.5 (10.8 scenario 2 on the command line): the preview, in English,
    and nothing written."""
    folder = _research_folder(tmp_path / "study")
    before, home_before = _tree(folder, skip_rce=False), _home_state()
    assert cli.main(["projects", "add", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "To scan: 6 file(s)" in out and "Nothing written" in out
    assert _tree(folder, skip_rce=False) == before and _home_state() == home_before


def test_cli_projects_add_yes_adds_and_scans_in_the_foreground_then_rename_and_list(tmp_path, capsys):
    """10.5: --yes adds and runs the first scan with progress lines; rename
    changes the label; list shows labels. Both path conventions."""
    folder = _research_folder(tmp_path / "study")
    assert cli.main(["projects", "add", "--path", str(folder), "--label", "Study", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "[1/4] every source extractor" in out and "[4/4] the judgment record applied" in out
    [entry] = registry.load()
    assert entry["label"] == "Study"
    assert len(_inventory_read(entry["id"])) == 6
    assert cli.main(["projects", "rename", entry["id"], "研究"]) == 0
    assert cli.main(["projects", "rename", "--path", str(folder), "Again"]) == 0
    capsys.readouterr()
    assert cli.main(["projects", "list"]) == 0
    assert "Again" in capsys.readouterr().out
    assert cli.main(["projects", "add", str(folder)]) == 0
    assert "already in the project list" in capsys.readouterr().out


def test_cli_projects_add_refuses_and_blocks_writing_nothing(fake_home, tmp_path, capsys):
    """10.5 / 10.8 scenarios 5 and 7 on the command line."""
    assert cli.main(["projects", "add", str(fake_home / "Documents"), "--yes"]) == 1
    assert "top-level" in capsys.readouterr().err
    _original, copy = _copy_of_a_project(tmp_path)
    assert cli.main(["projects", "add", str(copy), "--yes"]) == 1
    assert "rce project fork" in capsys.readouterr().out
    assert not registry.registry_path().exists()
