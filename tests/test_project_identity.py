"""DESIGN.md 9.4 at the entry points (task V5, phase 2): the identity check
every surface makes first, the answers to its questions, the registry
keyed by id, and the write-time re-check -- through the CLI, the HTTP
server and the MCP server, against real folders that are moved, renamed,
copied and reused.

The acceptance scenarios of 9.9 are named in each test's docstring. "All
records present" here means what this phase can show: the index's human
state (the confirm, the reject, the confirm-reject-undo), the hand-drawn
link with its note, the attempt verdict and the arranged canvas view.
Judgments that travel with a *copy* into a new index arrive with the
ledger (phase 4); the tests that need it say so."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from rce import cli, db, paths
from rce import project as project_identity
from rce.ingest import mappings as mappings_ingest
from rce.records import identity, situation
from rce.records.situation import Probes, Situation
from rce.webapp import canvas
from rce.webapp import registry
from rce.webapp import server

READ = ("script:a.py", "dataset:data.csv", "reads", "dataflow")
WRITE = ("script:a.py", "dataset:out.csv", "writes", "dataflow")
UNDONE = ("script:b.py", "dataset:data.csv", "reads", "dataflow")

_CONFIG = "\n".join([
    'file = "map.md"', 'heading = "H"', "", "[columns]", 'id = "#"', 'date = "date"',
    'description = "desc"', 'variables = "vars"', 'result = "result"', 'verdict = "verdict"', "",
])
_MAP = (
    "## H\n\n| # | date | desc | vars | result | verdict |\n|---|---|---|---|---|---|\n"
    "| 1 | 2026-01-01 | first | v | r | ✅ |\n"
)


def _pid(root: Path) -> str:
    got = identity.read_identity(root)
    assert got.identity is not None, got
    return got.identity.id


def _make(root: Path) -> str:
    """The 9.9 fixture: one confirmed machine link, one rejected, one
    confirmed-then-rejected-then-undone, a hand-drawn link with a note, an
    attempt table with a verdict, a canvas view arranged by hand."""
    root.mkdir(parents=True)
    (root / "data.csv").write_text("a\n")
    (root / "out.csv").write_text("a\n")
    (root / "a.py").write_text('import pandas as pd\ndf = pd.read_csv("data.csv")\ndf.to_csv("out.csv")\n')
    (root / "b.py").write_text('import pandas as pd\npd.read_csv("data.csv")\n')
    (root / "map.md").write_text(_MAP, encoding="utf-8")
    assert cli.main(["init", str(root)]) == 0
    (root / ".rce" / "attempts.toml").write_text(_CONFIG)
    assert cli.main(["ingest", str(root)]) == 0
    assert cli.main(["attempts", str(root)]) == 0
    for edge, status in ((READ, "confirmed"), (WRITE, "rejected"), (UNDONE, "confirmed")):
        assert cli.main(["confirm", *edge, "--status", status, "--path", str(root)]) == 0
    with situation.write_guard(root, _pid(root), human=True):
        conn = db.connect(paths.graph_db_path(root))
        try:
            db.reject_edge_remembering(conn, *UNDONE)
            db.restore_rejected_edge(conn, *UNDONE)
        finally:
            conn.close()
    mappings_ingest.add_mapping(root, "a.py", "fig.png", "generates", note="手画的")
    assert cli.main(["mappings", str(root)]) == 0
    conn = db.connect(paths.graph_db_path(root))
    try:
        canvas.save_layout(conn, root, {"scope": "all", "positions": {"script:a.py": [10.0, 20.0]}})
    finally:
        conn.close()
    return _pid(root)


def _records(root: Path) -> dict[str, Any]:
    conn = db.connect(paths.graph_db_path(root))
    try:
        def status(edge):
            rows = [e for e in db.query_edges(conn, src=edge[0], dst=edge[1], type=edge[2]) if e["extractor"] == edge[3]]
            return rows[0]["status"] if rows else None

        mapping = [e for e in db.query_edges(conn, type="generates") if e["extractor"] == "mapping"]
        attempt = db.get_node(conn, "attempt:map.md#1")
        return {
            "confirmed": status(READ),
            "rejected": status(WRITE),
            "undone": status(UNDONE),
            "mapping": [(e["src"], e["dst"], e["status"]) for e in mapping],
            "mapping_note": "手画的" in (root / ".rce" / "mappings.toml").read_text(encoding="utf-8"),
            "verdict": (attempt or {}).get("human_fields", {}).get("verdict"),
            "arranged": canvas.load_views(root).get("all", {}).get("positions"),
        }
    finally:
        conn.close()


EXPECTED = {
    "confirmed": "confirmed",
    "rejected": "rejected",
    "undone": "confirmed",
    "mapping": [("script:a.py", "figure:fig.png", "confirmed")],
    "mapping_note": True,
    "verdict": "✅",
    "arranged": {"script:a.py": [10.0, 20.0]},
}


def _move(src: Path, dst: Path) -> None:
    """A move to another volume: a new inode, the old folder gone."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, symlinks=True)
    shutil.rmtree(src)


def _snapshot_tree(*dirs: Path) -> dict[str, bytes]:
    out = {}
    for d in dirs:
        if not d.exists():
            continue
        for path in sorted(d.rglob("*")):
            if path.is_file():
                out[str(path)] = path.read_bytes()
    return out


@contextmanager
def _serving(root: Path, served: server.ServedProject | None = None) -> Iterator[tuple[str, server.RceHTTPServer]]:
    httpd = server.build_server(root, 0, served=served)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(base: str, path: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(base + path) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(base: str, path: str, body: dict) -> tuple[int, Any]:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# -- rce init ---------------------------------------------------------------------


def test_init_creates_the_identity_and_the_index_under_it(tmp_path: Path, capsys) -> None:
    project = tmp_path / "p"
    project.mkdir()
    assert cli.main(["init", str(project)]) == 0
    pid = _pid(project)
    assert pid in capsys.readouterr().out
    assert paths.graph_db_path(project) == paths.index_dir(pid) / "graph.db"
    assert situation.read_home(pid).canonical_path == paths._canonical_path(project)
    assert not paths.legacy_graph_dir(project).exists()  # no path-hash index any more
    conn = db.connect(paths.graph_db_path(project))
    try:
        assert db.get_node(conn, f"project:{pid}")["attrs"]["path"] == str(project)
    finally:
        conn.close()
    assert cli.main(["init", str(project)]) == 0 and _pid(project) == pid  # idempotent


def test_init_on_a_copy_is_refused_and_writes_nothing(tmp_path: Path, capsys) -> None:
    _make(tmp_path / "p")
    shutil.copytree(tmp_path / "p", tmp_path / "q")
    before = _snapshot_tree(tmp_path / "q", paths.rce_home())
    assert cli.main(["init", str(tmp_path / "q")]) == 1
    assert "rce project fork" in capsys.readouterr().err
    assert _snapshot_tree(tmp_path / "q", paths.rce_home()) == before


# -- 9.9 scenario 1: move ------------------------------------------------------------


def test_scenario_1_move_opened_from_the_cli(tmp_path: Path, fake_home: Path) -> None:
    """9.9 scenario 1 (CLI): all records present at the new path; one
    registry entry, at the new path; the old path is not servable; the
    index directory did not change."""
    old, new = tmp_path / "a" / "p", tmp_path / "b" / "p"
    pid = _make(old)
    registry.register(old, pid)
    assert _records(old) == EXPECTED
    index_files = sorted(p.name for p in paths.index_dir(pid).iterdir())
    db_inode = os.stat(paths.index_dir(pid) / "graph.db").st_ino

    _move(old, new)
    assert situation.classify(new).situation is Situation.MOVED
    assert cli.main(["status", "--path", str(new)]) == 0

    assert _records(new) == EXPECTED
    assert registry.load() == [{"id": pid, "path": str(new), "label": "p"}]
    assert sorted(p.name for p in paths.index_dir(pid).iterdir()) == index_files
    assert os.stat(paths.index_dir(pid) / "graph.db").st_ino == db_inode
    assert situation.read_home(pid).canonical_path == paths._canonical_path(new)
    served = server.served_for(old, expected_id=pid)
    assert served.blocked and served.blocked["situation"] == "missing"


def test_scenario_1_move_reattached_from_the_app(tmp_path: Path, fake_home: Path) -> None:
    """9.9 scenario 1 (app): the app starts with no path, its last project's
    folder is gone -- it starts anyway and says so; 「选择新位置…」 adopts the
    chosen folder only if it carries that id."""
    old, new = tmp_path / "a" / "p", tmp_path / "b" / "p"
    pid = _make(old)
    registry.register(old, pid)
    _move(old, new)
    other = tmp_path / "other"
    _make(other)

    calls = []
    import rce.webapp.server as server_module

    original = server_module.serve
    server_module.serve = lambda root, port, open_browser=True, served=None: calls.append(served)
    try:
        assert cli.main(["serve", "--no-browser"]) == 0  # starts anyway
    finally:
        server_module.serve = original
    served = calls[0]
    assert served.blocked["situation"] == "missing" and served.blocked["project_id"] == pid

    with _serving(old, served) as (base, _httpd):
        status, body = _get(base, "/api/summary")
        assert status == 409 and body["state"] == "project_blocked"
        assert body["situation"]["message"] == "找不到项目文件夹（可能已移动）"
        status, body = _get(base, "/api/projects")
        assert body["projects"][0]["missing"] is True and body["projects"][0]["id"] == pid

        status, body = _post(base, "/api/projects/locate", {"id": pid, "path": str(other)})
        assert status == 409 and body["state"] == "not_this_project"
        status, body = _post(base, "/api/projects/locate", {"id": pid, "path": "relative/p"})
        assert status == 400

        status, body = _post(base, "/api/projects/locate", {"id": pid, "path": str(new)})
        assert status == 200 and body["project_id"] == pid and body["blocked"] is None
        status, body = _get(base, "/api/summary")
        assert status == 200 and body["project_id"] == pid and body["project_root"] == str(new)
    assert _records(new) == EXPECTED
    assert [e for e in registry.load() if e["id"] == pid] == [{"id": pid, "path": str(new), "label": "p"}]


def test_scenario_1_moved_while_served_writes_nothing_and_says_reopen(tmp_path: Path) -> None:
    """9.9 scenario 1, last sentence: moved in Finder while an engine
    serves it, a click writes nothing at the old path, and the page is
    told 「项目已移动或已在别处认领，请重新打开」 (state project_moved)."""
    root = tmp_path / "p"
    pid = _make(root)
    with _serving(root) as (base, _httpd):
        _status, cv = _get(base, "/api/canvas?scope=all")
        os.rename(root, tmp_path / "moved")
        attempts = [
            ("/api/edges/reject", dict(zip(("src", "dst", "type", "extractor"), READ))),
            ("/api/canvas/layout", {"project": cv["project"], "scope": "all", "positions": {"script:a.py": [1, 2]}}),
            ("/api/mappings/add", {"from": "b.py", "to": "fig2.png", "type": "generates"}),
            ("/api/attempts/write", {"op": "append", "number": "2", "fields": {"date": "2026-01-02"}}),
        ]
        for path, body in attempts:
            status, answer = _post(base, path, body)
            assert (status, answer.get("state")) == (409, "project_moved"), (path, answer)
    assert not root.exists()  # nothing re-created at the old path
    moved = tmp_path / "moved"
    assert situation.classify(moved).situation is Situation.NORMAL  # a rename in place
    assert _records(moved) == EXPECTED
    assert pid == _pid(moved)


# -- 9.9 scenario 2: rename -------------------------------------------------------------


def test_scenario_2_rename_is_adopted_without_a_copy_question(tmp_path: Path, fake_home: Path) -> None:
    """9.9 scenario 2: as scenario 1, no copy question."""
    root = tmp_path / "p"
    pid = _make(root)
    registry.register(root, pid)
    renamed = tmp_path / "renamed"
    os.rename(root, renamed)
    assert cli.main(["status", "--path", str(renamed)]) == 0
    assert _records(renamed) == EXPECTED
    assert registry.load() == [{"id": pid, "path": str(renamed), "label": "renamed"}]
    assert situation.read_home(pid).canonical_path == paths._canonical_path(renamed)


def test_scenario_2_case_only_rename(tmp_path: Path, fake_home: Path) -> None:
    """9.9 scenario 2, a change of letter case only (on a case-insensitive
    volume, where it is the same folder): no copy question; the registry
    shows the new spelling."""
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("case-sensitive file system")
    root = tmp_path / "proj"
    pid = _make(root)
    registry.register(root, pid)
    upper = tmp_path / "PROJ"
    os.rename(root, upper)
    assert situation.classify(root).situation is Situation.NORMAL  # old spelling: same folder
    assert cli.main(["status", "--path", str(upper)]) == 0
    assert _records(upper) == EXPECTED
    assert registry.load()[0]["path"].endswith("/PROJ")
    assert situation.read_home(pid).canonical_path.endswith("/PROJ")


# -- 9.9 scenario 3: copy ------------------------------------------------------------------


def test_scenario_3_copy_is_blocked_and_nothing_is_written(tmp_path: Path, fake_home: Path, capsys) -> None:
    """9.9 scenario 3: opening a copy is blocked, with the answers, and
    nothing is written -- the registry included -- from the CLI, from
    `rce serve`, from the HTTP server, from the MCP server."""
    p, q = tmp_path / "p", tmp_path / "q"
    pid = _make(p)
    registry.register(p, pid)
    shutil.copytree(p, q)
    before = _snapshot_tree(q, paths.rce_home())

    assert cli.main(["status", "--path", str(q)]) == 1
    err = capsys.readouterr().err
    for answer in ("rce project fork", "rce project claim", "rce project other"):
        assert answer in err
    assert cli.main(["ingest", str(q)]) == 1
    assert cli.main(["confirm", *READ, "--status", "rejected", "--path", str(q)]) == 1

    served = server.served_for(q, register=True)
    assert served.blocked["situation"] == "copy"
    assert served.blocked["answers"] == ["fork", "claim", "other"]
    with _serving(q, served) as (base, _httpd):
        status, body = _get(base, "/api/summary")
        assert status == 409 and body["state"] == "project_blocked" and body["situation"]["situation"] == "copy"
        assert _get(base, "/api/projects")[0] == 200
        status, body = _post(base, "/api/canvas/layout", {"project": str(q), "scope": "all"})
        assert (status, body["state"]) == (409, "project_blocked")
        status, body = _post(base, "/api/project/resolve", {"answer": "readonly"})
        assert (status, body["state"]) == (409, "answer_refused")  # not offered for a copy

    pytest.importorskip("mcp")
    from rce import mcp_server

    assert mcp_server.main(["--path", str(q)]) == 1
    assert _snapshot_tree(q, paths.rce_home()) == before


def test_scenario_3_fork(tmp_path: Path, fake_home: Path, capsys) -> None:
    """9.9 scenario 3, fork: a new id with `forked_from`; its own index;
    the original's records untouched; a later judgment in the copy does
    not appear in the original. (The copy's own index carries the confirms
    and rejects once the ledger travels with it -- phase 4.)"""
    p, q = tmp_path / "p", tmp_path / "q"
    pid = _make(p)
    shutil.copytree(p, q)
    assert cli.main(["project", "fork", str(q)]) == 0
    qid = _pid(q)
    assert qid != pid and identity.read_identity(q).identity.forked_from == pid
    assert paths.graph_db_path(q) == paths.index_dir(qid) / "graph.db"
    assert situation.classify(q).situation is Situation.NORMAL
    assert situation.classify(p).situation is Situation.NORMAL
    rec_q = _records(q)
    assert rec_q["mapping"] == EXPECTED["mapping"] and rec_q["verdict"] == "✅"

    assert cli.main(["confirm", *READ, "--status", "rejected", "--path", str(q)]) == 0
    assert _records(q)["confirmed"] == "rejected"
    assert _records(p) == EXPECTED
    assert cli.main(["project", "fork", str(q)]) == 1  # no question stands any more


def test_fork_warns_when_the_identity_file_is_tracked_by_git(tmp_path: Path, capsys) -> None:
    p, q = tmp_path / "p", tmp_path / "q"
    _make(p)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", ".rce/project.toml"], ["commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=p, check=True, env=env)
    shutil.copytree(p, q)
    capsys.readouterr()
    assert cli.main(["project", "fork", str(q)]) == 0
    assert "tracked by git" in capsys.readouterr().out


def test_scenario_3_claim(tmp_path: Path, fake_home: Path) -> None:
    """9.9 scenario 3, claim: the index is rebuilt from the claiming
    folder (the previous one kept aside), its home is now here, and the
    original is asked on its next open."""
    p, q = tmp_path / "p", tmp_path / "q"
    pid = _make(p)
    registry.register(p, pid)
    shutil.copytree(p, q)
    (q / "c.py").write_text('import pandas as pd\npd.read_csv("data.csv")\n')  # only in the claimer
    assert cli.main(["project", "claim", str(q)]) == 0
    assert _pid(q) == pid
    assert situation.classify(q).situation is Situation.NORMAL
    assert situation.read_home(pid).canonical_path == paths._canonical_path(q)
    replaced = list((paths.rce_home() / "graphs" / ".replaced").iterdir())
    assert len(replaced) == 1 and replaced[0].name.startswith(pid)
    conn = db.connect(paths.graph_db_path(q))
    try:
        assert db.get_node(conn, "script:c.py") is not None  # scanned from THIS folder
        mapping = [e for e in db.query_edges(conn, type="generates") if e["extractor"] == "mapping"]
        assert mapping and db.get_node(conn, "attempt:map.md#1") is not None
    finally:
        conn.close()
    assert situation.classify(p).situation is Situation.COPY
    assert cli.main(["status", "--path", str(p)]) == 1
    assert registry.find(pid)["path"] == str(q)


def test_scenario_3_original_on_an_unmounted_volume(tmp_path: Path, fake_home: Path) -> None:
    """9.9 scenario 3, last sentence: with the original on a volume that is
    not mounted, the third answer -- read-only -- and no adoption."""
    p, q = tmp_path / "p", tmp_path / "q"
    pid = _make(p)
    _move(p, q)  # p is gone; but its volume "is not mounted", so that cannot be known
    unmounted = Probes(volume_mounted=lambda _path: False)
    home_before = situation.home_path(pid).read_bytes()

    served = server.served_for(q, probes=unmounted)
    assert served.blocked["situation"] == "cannot_check" and "readonly" in served.blocked["answers"]
    with _serving(q, served) as (base, _httpd):
        status, body = _post(base, "/api/project/resolve", {"answer": "readonly"})
        assert status == 200 and body["read_only"] is True
        status, summary = _get(base, "/api/summary")
        assert status == 200 and summary["read_only"] is True
        assert summary["nodes"]["script"] == 2
        _status, cv = _get(base, "/api/canvas?scope=all")
        status, body = _post(base, "/api/canvas/layout", {"project": cv["project"], "scope": "all"})
        assert (status, body["state"]) == (409, "read_only")
    assert situation.home_path(pid).read_bytes() == home_before  # not adopted


def test_resolve_fork_over_http(tmp_path: Path, fake_home: Path) -> None:
    """9.9 scenario 3 through the app: the blocked page's 「作为独立分支继续」."""
    p, q = tmp_path / "p", tmp_path / "q"
    pid = _make(p)
    shutil.copytree(p, q)
    with _serving(q) as (base, _httpd):
        status, body = _post(base, "/api/project/resolve", {"answer": "fork"})
        assert status == 200 and body["previous_id"] == pid and body["project_id"] != pid
        assert body["build_error"] is None
        status, summary = _get(base, "/api/summary")
        assert status == 200 and summary["project_id"] == body["project_id"]
    assert registry.find(body["project_id"])["path"] == str(q)
    assert registry.find(pid) is None  # the original was never registered by this


def test_other_moves_copied_records_aside(tmp_path: Path) -> None:
    """「这是另一个项目」: new id, no forked_from; the copied ledger,
    arrangement and variable cards go to .rce/backups/."""
    p, q = tmp_path / "p", tmp_path / "q"
    pid = _make(p)
    (p / ".rce" / "judgements.toml").write_text("# ledger\n")
    (p / ".rce" / "canvas.json").write_text("{}")
    (p / ".rce" / "variables").mkdir()
    shutil.copytree(p, q)
    with _serving(q) as (base, _httpd):
        status, body = _post(base, "/api/project/resolve", {"answer": "other"})
    assert status == 200, body
    ident = identity.read_identity(q).identity
    assert ident.id != pid and ident.forked_from is None and ident.ledger is False
    assert sorted(body["moved_aside"]) == ["canvas.json", "judgements.toml", "variables"]
    aside = list((q / ".rce" / "backups").glob("from-another-project-*"))
    assert len(aside) == 1 and (aside[0] / "judgements.toml").read_text() == "# ledger\n"
    assert not (q / ".rce" / "judgements.toml").exists()
    assert (p / ".rce" / "judgements.toml").exists()  # the original untouched


def test_lost_identity_is_blocked_and_other_answers_it(tmp_path: Path, capsys) -> None:
    root = tmp_path / "p"
    _make(root)
    (root / ".rce" / "judgements.toml").write_text("")
    (root / ".rce" / "project.toml").unlink()
    assert cli.main(["status", "--path", str(root)]) == 1
    assert "Restore .rce/project.toml" in capsys.readouterr().err
    assert cli.main(["init", str(root)]) == 1  # never minted silently over records
    assert not (root / ".rce" / "project.toml").exists()
    assert cli.main(["project", "other", str(root)]) == 0
    assert situation.classify(root).situation is Situation.NORMAL
    assert list((root / ".rce" / "backups").glob("from-another-project-*/judgements.toml"))


def test_unreadable_identity_stops_everything(tmp_path: Path, fake_home: Path, capsys) -> None:
    root = tmp_path / "p"
    pid = _make(root)
    (root / ".rce" / "project.toml").write_text("id = [\n")
    before = _snapshot_tree(root, paths.rce_home())
    assert cli.main(["status", "--path", str(root)]) == 1
    assert "cannot be read" in capsys.readouterr().err
    served = server.served_for(root, register=True)
    assert served.blocked["situation"] == "unreadable_id" and served.blocked["answers"] == []
    assert _snapshot_tree(root, paths.rce_home()) == before
    assert pid


def test_no_index_builds_one_for_the_id(tmp_path: Path) -> None:
    """Restored, cloned, synced from another Mac, or the index deleted: a
    new index for the id, home here, built from the sources and the record
    (9.9 #5 is in tests/test_records_judgements.py)."""
    root = tmp_path / "p"
    pid = _make(root)
    shutil.rmtree(paths.index_dir(pid))
    assert situation.classify(root).situation is Situation.NO_INDEX
    assert cli.main(["status", "--path", str(root)]) == 0
    assert situation.classify(root).situation is Situation.NORMAL
    conn = db.connect(paths.graph_db_path(root))
    try:
        assert db.get_node(conn, f"project:{pid}") is not None
    finally:
        conn.close()


# -- 9.9 scenario 4: path reuse -------------------------------------------------------------


def test_scenario_4_path_reuse_after_v5_inherits_nothing(tmp_path: Path, fake_home: Path) -> None:
    """9.9 scenario 4 (after V5): move the project away, create an
    unrelated project at the old path -- no judgments, no arrangement, its
    own id; and the moved project keeps all of its records."""
    root, away = tmp_path / "paper", tmp_path / "archive" / "paper"
    pid = _make(root)
    registry.register(root, pid)
    _move(root, away)
    root.mkdir()
    (root / "a.py").write_text('import pandas as pd\npd.read_csv("data.csv")\n')
    (root / "data.csv").write_text("x\n")
    assert cli.main(["init", str(root)]) == 0
    assert cli.main(["ingest", str(root)]) == 0
    new_id = _pid(root)
    assert new_id != pid
    conn = db.connect(paths.graph_db_path(root))
    try:
        assert [e for e in db.query_edges(conn) if e["status"] in ("confirmed", "rejected")] == []
    finally:
        conn.close()
    assert canvas.load_views(root) == {}
    # The old registry entry is not this folder's: shown as missing.
    old_entry = registry.find(pid)
    assert server.entry_state(old_entry)["missing"] is True
    assert situation.classify(away).situation is Situation.MOVED
    assert cli.main(["status", "--path", str(away)]) == 0
    assert _records(away) == EXPECTED


# -- pre-V5 projects: readable, human records refused ------------------------------------


def _legacy(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "a.py").write_text('import pandas as pd\npd.read_csv("data.csv")\n')
    (root / "data.csv").write_text("x\n")
    paths.legacy_graph_dir(root).mkdir(parents=True)
    conn = db.connect(paths.legacy_index_db_path(root))
    try:
        db.migrate(conn)
    finally:
        conn.close()


def test_pre_v5_project_is_readable_and_refuses_human_records(tmp_path: Path, capsys) -> None:
    root = tmp_path / "old"
    _legacy(root)
    assert situation.classify(root).situation is Situation.LEGACY
    assert cli.main(["ingest", str(root)]) == 0  # scans still write the old index
    assert cli.main(["confirm", *READ, "--status", "confirmed", "--path", str(root)]) == 1
    assert "before V5" in capsys.readouterr().err
    with pytest.raises(situation.NeedsMigrationError):
        mappings_ingest.add_mapping(root, "a.py", "f.png", "generates")
    with _serving(root) as (base, _httpd):
        status, summary = _get(base, "/api/summary")
        assert status == 200 and summary["needs_migration"] is True and summary["project_id"] is None
        status, body = _post(base, "/api/mappings/add", {"from": "a.py", "to": "f.png", "type": "generates"})
        assert (status, body["state"]) == (409, "needs_migration")
        status, body = _post(base, "/api/edges/reject", dict(zip(("src", "dst", "type", "extractor"), READ)))
        assert (status, body["state"]) == (409, "needs_migration")
    assert not (root / ".rce" / "mappings.toml").exists()
    assert not (root / ".rce" / "project.toml").exists()


# -- 9.9 scenario 10: two writers --------------------------------------------------------------

_CANVAS_WRITER = r"""
import sys
from pathlib import Path
from rce import db, paths
from rce.webapp import canvas
root, tag, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
# Every key is a card of the view (the 9.0 experiment's critical section).
canvas.view_card_ids = lambda conn, project_root, scope: {f"{t}-{i}" for t in "ab" for i in range(n)}
conn = db.connect(paths.graph_db_path(root))
for i in range(n):
    canvas.save_layout(conn, root, {"scope": "all", "positions": {f"{tag}-{i}": [float(i), 1.0]}})
conn.close()
"""

_MAPPING_WRITER = r"""
import sys
from pathlib import Path
from rce.ingest import mappings
root, tag, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
for i in range(n):
    mappings.add_mapping(root, f"{tag}{i}.py", f"{tag}{i}.png", "generates")
"""

_ATTEMPT_WRITER = r"""
import sys
from pathlib import Path
from rce.webapp import mapedit
root, tag, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
for i in range(n):
    mapedit.apply_edit(root, "append", f"{tag}{i}", {"date": "2026-01-02", "description": tag})
"""


def _two_processes(script: str, root: Path, n: int) -> None:
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(root), tag, str(n)], stderr=subprocess.PIPE)
        for tag in ("a", "b")
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=300)
        assert proc.returncode == 0, err.decode()  # nothing raised


def test_scenario_10_two_writers_canvas_positions(tmp_path: Path) -> None:
    """9.9 scenario 10 for the arrangement: two processes each make 300
    position changes at once; every one is in the record afterwards."""
    root = tmp_path / "p"
    root.mkdir()
    project_identity.init_project(root)
    _two_processes(_CANVAS_WRITER, root, 300)
    positions = canvas.load_views(root)["all"]["positions"]
    assert len(positions) == 600


def test_scenario_10_two_writers_mappings_and_attempt_rows(tmp_path: Path) -> None:
    """9.9 scenario 10 for the two record files the app already writes:
    hand-drawn links and attempt rows from two processes at once all land."""
    root = tmp_path / "p"
    root.mkdir()
    (root / "map.md").write_text(_MAP, encoding="utf-8")
    project_identity.init_project(root)
    (root / ".rce" / "attempts.toml").write_text(_CONFIG)
    _two_processes(_MAPPING_WRITER, root, 40)
    _two_processes(_ATTEMPT_WRITER, root, 25)
    loaded = mappings_ingest.load_mappings(root)
    assert len(loaded.mappings) == 80
    table = (root / "map.md").read_text(encoding="utf-8")
    assert all(f"| {t}{i} |" in table for t in "ab" for i in range(25))
