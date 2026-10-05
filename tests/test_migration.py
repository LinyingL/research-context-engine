"""Migrating what exists (DESIGN.md 9.5; task V5 phase 5): discovery, the
match, 「这不是这个项目的」, the export with its bases, the rebuild that
reconciles against the old index's own count, and retirement -- the 9.9
acceptance scenarios #9 (in full, on a hand-built pre-V5 index) and the
pre-V5 half of #4.

The fixture index is built with the pre-V5 schema only (migrations
0001-0003: no scan stamps, no judgment tables), holding exactly what 9.9
#9 lists."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from rce import cli, db, migration, paths
from rce.records import identity, judgements
from rce.records import ledger as ledger_mod
from rce.records import situation
from rce.records.situation import Situation, index_db_path
from rce.webapp import canvas, registry

READ = ("script:s.py", "dataset:data/in.csv", "reads", "dataflow")
WRITE = ("script:s.py", "dataset:data/out.csv", "writes", "dataflow")
READ_B = ("script:s.py", "dataset:data/b.csv", "reads", "dataflow")
TWO_BASES = ("script:s2.py", "dataset:data/c.csv", "reads", "dataflow")
GONE = ("script:s.py", "dataset:data/gone.csv", "reads", "dataflow")
ORPHAN_CLAIM = ("claim:paper.tex#deadbeefdeadbeef", "experiment:run1", "backed_by", "claims")
ORPHAN_ATTEMPT = ("attempt:map.md#9", "commit:abc123", "uses", "attempts_consistency")
MAPPING = ("script:s.py", "figure:fig.png", "generates", "mapping")

SCRIPT = (
    "import pandas as pd\n"
    "df = pd.read_csv('data/in.csv')\n"
    "b = pd.read_csv('data/b.csv')\n"
    "df.to_csv('data/out.csv')\n"
)
SCRIPT2 = "import pandas as pd\nc = pd.read_csv('data/c.csv')\n"
MAP = (
    "# 项目地图\n\n## 尝试\n\n"
    "| # | 日期 | 路径 | 变量 | 结果 | 结论 |\n"
    "|---|---|---|---|---|---|\n"
    "| 1 | 2026-07-01 | 第一条路 | X | 0.5 | ✅ |\n"
)
ATTEMPTS_TOML = (
    'file = "map.md"\nheading = "尝试"\n\n[columns]\n'
    'id = "#"\ndate = "日期"\ndescription = "路径"\nvariables = "变量"\nresult = "结果"\nverdict = "结论"\n'
)
MAPPINGS = '[[mapping]]\nfrom = "s.py"\nto = "fig.png"\ntype = "generates"\nnote = "手画的"\n'
VIEWS = {"views": {"all": {"positions": {"script:s.py": [10.0, 20.0]}, "viewport": None}}}


def _folder(base: Path, name: str = "proj") -> Path:
    root = base / name
    (root / "data").mkdir(parents=True)
    for f in ("in.csv", "out.csv", "b.csv", "c.csv", "gone.csv"):
        (root / "data" / f).write_text("a\n1\n")
    (root / "s.py").write_text(SCRIPT)
    (root / "s2.py").write_text(SCRIPT2)
    (root / "fig.png").write_bytes(b"png")
    (root / "map.md").write_text(MAP)
    (root / ".rce").mkdir()
    (root / ".rce" / "attempts.toml").write_text(ATTEMPTS_TOML)
    (root / ".rce" / "mappings.toml").write_text(MAPPINGS)
    return root


def _pre_v5_schema(conn) -> None:
    src = Path(db.__file__).parent / "migrations"
    old = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent / "_mig"
    old.mkdir(exist_ok=True)
    for f in sorted(src.glob("000[123]_*.sql")):
        shutil.copy(f, old / f.name)
    db.migrate(conn, old)
    shutil.rmtree(old)


def _edge(conn, key, callee=None, file=None, status="auto", **evidence):
    src, dst, type_, extractor = key
    for node in (src, dst):
        db.upsert_node(conn, node, node.split(":", 1)[0], title=node.split(":", 1)[1])
    if callee is not None:
        evidence = {"file": file or src.split(":", 1)[1], "line": 2, "callee": callee}
    db.upsert_edge(conn, src, dst, type_, extractor=extractor, evidence=evidence or {"file": "x"}, confidence=1.0, status=status)


def build_pre_v5_index(root: Path, *, key_dir: Path | None = None, contradict: bool = False) -> Path:
    """A hand-built pre-V5 index at this folder's path hash (or `key_dir`),
    holding exactly 9.9 #9's list."""
    directory = key_dir or paths.legacy_graph_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / "graph.db"
    conn = db.connect(db_path)
    try:
        _pre_v5_schema(conn)
        db.upsert_node(conn, f"project:{root.name}", "project", title=root.name, attrs={"path": str(root)})
        _edge(conn, READ, "pd.read_csv")
        _edge(conn, WRITE, "df.to_csv")
        _edge(conn, READ_B, "pd.read_csv")
        _edge(conn, TWO_BASES, "pd.read_csv")
        db.upsert_edge(conn, *TWO_BASES[:3], extractor="dataflow",
                       evidence={"file": "s2.py", "line": 3, "callee": "open"}, confidence=1.0, status="auto")
        _edge(conn, GONE, "pd.read_csv")
        db.upsert_node(conn, ORPHAN_CLAIM[0], "claim", title="精度为 0.87", attrs={"sentence": "精度为 0.87", "precision_decimals": 2})
        db.upsert_node(conn, ORPHAN_CLAIM[1], "experiment", title="run1")
        db.upsert_edge(conn, *ORPHAN_CLAIM[:3], extractor="claims",
                       evidence={"file": "paper.tex", "metric": "acc", "metric_value": 0.873, "claim_raw": "0.87", "claim_value": 0.87},
                       confidence=1.0, status="pending")
        db.upsert_node(conn, ORPHAN_ATTEMPT[0], "attempt", title="删掉的尝试", attrs={"source_file": "map.md", "number": "9"})
        db.upsert_node(conn, ORPHAN_ATTEMPT[1], "commit", title="abc123")
        db.upsert_edge(conn, *ORPHAN_ATTEMPT[:3], extractor="attempts_consistency",
                       evidence={"script": "s.py", "commit_time": "2026-07-01"}, confidence=1.0, status="auto")
        for node in MAPPING[:2]:
            db.upsert_node(conn, node, node.split(":", 1)[0], title=node.split(":", 1)[1])
        db.upsert_edge(conn, *MAPPING[:3], extractor="mapping", human_source=True,
                       evidence={"file": ".rce/mappings.toml", "source": "human"}, confidence=1.0, status="auto")
        # The judgments, as pre-V5 code recorded them.
        db.set_edge_status(conn, *READ, "rejected" if contradict else "confirmed")
        db.set_edge_status(conn, *WRITE, "confirmed")
        db.reject_edge_remembering(conn, *WRITE)          # remembers a prior confirmation
        db.reject_edge_remembering(conn, *READ_B)         # remembers "auto"
        db.set_edge_status(conn, *TWO_BASES, "confirmed")
        db.set_edge_status(conn, *GONE, "confirmed")
        db.set_edge_status(conn, *ORPHAN_CLAIM, "confirmed")
        db.set_edge_status(conn, *ORPHAN_ATTEMPT, "rejected")
        db.set_edge_status(conn, *MAPPING, "confirmed")
        # A preserved orphan: the attempt row is gone from map.md, the claim from the paper.
        db.upsert_node(conn, "attempt:map.md#1", "attempt", title="第一条路", attrs={"source_file": "map.md", "number": "1"})
        db.set_human_fields(conn, "attempt:map.md#1", {"verdict": "❌", "result": "0.4"})  # a stale mirror
    finally:
        conn.close()
    (directory / "canvas.json").write_text(json.dumps(VIEWS))
    return db_path


@pytest.fixture(autouse=True)
def _no_engine(monkeypatch):
    monkeypatch.setenv(migration.ENGINE_PORT_ENV, "0")


def _entries(root):
    loaded = ledger_mod.load_judgements(root)
    return [] if loaded.ledger is None else [dict(e.data) for e in loaded.ledger.entries]


def _state(root, key):
    conn = db.connect(index_db_path(identity.read_identity(root).identity.id))
    try:
        return db.judgement_states(conn).get(key), db.edge_statuses(conn).get(key, (None,))[0]
    finally:
        conn.close()


def _pre_v5(tmp_path) -> tuple[Path, Path]:
    root = _folder(tmp_path)
    return root, build_pre_v5_index(root)


# -- discovery, listing, the match --------------------------------------------------------


def test_discovery_on_open_and_the_list(tmp_path, capsys):
    root, old = _pre_v5(tmp_path)
    assert situation.classify(root).situation is Situation.LEGACY
    assert paths.legacy_sources(root) == [(f"graphs/{paths.legacy_graph_id(root)}", old)]
    assert migration.waiting_payload(root)["waiting"][0]["db_path"] == str(old)
    listed = migration.list_old_indexes()
    assert len(listed) == 1
    item = listed[0]
    assert (item.recorded_path, item.path_exists) == (str(root), True)
    assert (item.confirmed, item.rejected, item.remembered, item.arranged_views, item.mapping_links) == (4, 3, 2, 1, 1)
    assert cli.main(["migrate", "--list"]) == 0
    out = capsys.readouterr().out
    assert str(root) in out and "confirmed 4" in out and "rejected 3" in out
    assert cli.main(["status", "--path", str(root)]) == 0
    assert "Legacy records waiting" in capsys.readouterr().out


def test_without_yes_nothing_is_written_and_the_match_is_shown(tmp_path, capsys):
    root, old = _pre_v5(tmp_path)
    before = sorted(p.name for p in (root / ".rce").iterdir())
    old_bytes = old.read_bytes()
    assert cli.main(["migrate", str(root)]) == 1
    out = capsys.readouterr().out
    assert "4 of 7" in out and "--yes" in out
    assert sorted(p.name for p in (root / ".rce").iterdir()) == before
    assert old.read_bytes() == old_bytes
    assert not list((paths.rce_home() / "graphs").glob(".scratch-*"))


def test_scenario_4_pre_v5_path_reuse_low_match_declines_and_inherits_nothing(tmp_path, capsys):
    """9.9 #4, pre-V5 half: a folder reusing an old project's path is shown
    the old index with its low match, declines, and inherits nothing."""
    original = _folder(tmp_path)
    old = build_pre_v5_index(original)
    shutil.rmtree(original)
    reuse = tmp_path / "proj"
    reuse.mkdir()
    (reuse / "notes.py").write_text("print('unrelated')\n")
    [view] = migration.preview(reuse)
    assert view.index.judged == 7 and view.produced == 0 and view.match == 0
    assert cli.main(["migrate", "--not-mine", str(reuse)]) == 0
    assert old.exists()  # untouched
    assert paths.legacy_sources(reuse) == []
    assert situation.classify(reuse).situation is Situation.NOT_A_PROJECT
    assert not paths.graph_db_path(reuse).exists()  # the declined index is never served as this folder's
    assert cli.main(["init", str(reuse)]) == 0
    capsys.readouterr()
    assert cli.main(["ingest", str(reuse)]) == 0
    conn = db.connect(paths.graph_db_path(reuse))
    try:
        assert not [e for e in db.query_edges(conn) if e["status"] in ("confirmed", "rejected")]
        assert db.get_node(conn, READ[0]) is None
    finally:
        conn.close()
    assert _entries(reuse) == []
    assert canvas.layout_record(reuse).state == "absent"
    assert [i.key for i in migration.list_old_indexes()] == [f"graphs/{paths.legacy_graph_id(reuse)}"]  # still listed


# -- scenario 9 --------------------------------------------------------------------------------


def test_scenario_9_migrate_balances_from_the_old_count_and_retires_afterwards(tmp_path, capsys):
    """9.9 #9: the tally starts from the old index's own count and balances
    with nothing unmatched; the two-bases link is under review, not
    re-certified; the hand-drawn link is skipped; the arrangement is
    carried; the old index is retired only afterwards."""
    root, old = _pre_v5(tmp_path)
    registry.register(root)
    assert cli.main(["migrate", "--yes", str(root)]) == 0
    out = capsys.readouterr().out
    assert "7 judged link(s)" in out and "retired" in out
    ident = identity.read_identity(root).identity
    assert ident.migrating_from is None and ident.ledger is True
    assert not old.parent.exists()
    retired = list((paths.rce_home() / "graphs" / ".retired").iterdir())
    assert len(retired) == 1 and (retired[0] / "graph.db").exists()
    assert registry.load()[0]["id"] == ident.id

    entries = _entries(root)
    assert all(e["via"] == "migrated" for e in entries)
    assert not [e for e in entries if e["extractor"] == "mapping"]
    write = [e["verdict"] for e in entries if (e["src"], e["dst"], e["type"], e["extractor"]) == WRITE]
    assert write == ["confirmed", "rejected"]
    assert len(entries) == 8

    assert _state(root, READ) == (_state(root, READ)[0], "confirmed")
    assert _state(root, READ)[0]["outcome"] == "applied"
    read_entry = next(e for e in entries if e["src"] == READ[0] and e["dst"] == READ[1])
    assert read_entry["basis_recorded"] == "at-migration" and read_entry["basis"] == {"calls": ["read_csv"]}
    assert _state(root, WRITE)[1] == "rejected" and _state(root, READ_B)[1] == "rejected"
    two, status = _state(root, TWO_BASES)
    assert two["outcome"] == "review" and two["reason"] == judgements.BASIS_CHANGED and status == "auto"
    two_entry = next(e for e in entries if e["src"] == TWO_BASES[0])
    assert two_entry["basis_recorded"] == "old-index" and len(two_entry["basis"]["old_index"]) == 2
    for key in (GONE, ORPHAN_CLAIM, ORPHAN_ATTEMPT):
        item, _status = _state(root, key)
        assert item["outcome"] == "review" and item["reason"] in (judgements.NOT_PRODUCED, judgements.ENDPOINT_GONE)
    assert canvas.layout_record(root).views["all"]["positions"]["script:s.py"] == [10.0, 20.0]
    assert cli.main(["records", "--verify", str(root)]) == 0


def test_scenario_9_stale_mirror_is_listed_not_a_failure(tmp_path):
    root, _old = _pre_v5(tmp_path)
    [result] = migration.migrate(root, yes=True)
    assert result.ok
    assert any("attempt:map.md#1" in s for s in result.tally.stale_mirror)
    assert result.tally.balanced and result.tally.m == 7
    assert (result.tally.applied, result.tally.review_other, result.tally.review_not_produced) == (3, 1, 3)


def test_scenario_9_exporting_twice_adds_nothing(tmp_path):
    root, old = _pre_v5(tmp_path)
    keep = tmp_path / "keep"
    shutil.copytree(old.parent, keep)
    [first] = migration.migrate(root, yes=True)
    before = ledger_mod.judgements_path(root).read_bytes()
    shutil.copytree(keep, old.parent)  # the same index again (restored from a backup)
    [again] = migration.migrate(root, yes=True)
    assert again.ok and again.exported.appended == 0
    assert ledger_mod.judgements_path(root).read_bytes() == before


@pytest.mark.parametrize("point", [
    "after_identity", "after_scan", "export:1", "export:4", "after_export", "after_verify", "after_install", "after_retire",
])
def test_scenario_9_killed_between_steps_retires_nothing_and_resumes(tmp_path, point):
    """9.9 #9: a kill between each pair of steps (fault injection): nothing
    is retired, the old index still serves, and a retry resumes and
    duplicates nothing."""
    root, old = _pre_v5(tmp_path)

    def crash(at):
        if at == point:
            raise KeyboardInterrupt(at)

    with pytest.raises(KeyboardInterrupt):
        migration.migrate(root, yes=True, fault=crash)
    if point != "after_retire":
        assert old.exists()  # nothing retired
        c = situation.classify(root)
        assert c.needs_migration
        served = paths.graph_db_path(root)  # the old index keeps serving until the new one is installed
        assert served == (index_db_path(c.project_id) if point == "after_install" else old)
        with pytest.raises(situation.NeedsMigrationError):
            judgements.judge(root, READ, "confirmed", via="cli")
    [done] = migration.migrate(root)  # resumes without asking again
    assert done.ok and done.resumed
    entries = _entries(root)
    assert len(entries) == 8 and len({e["id"] for e in entries}) == 8
    assert identity.read_identity(root).identity.migrating_from is None
    assert not old.exists()
    assert cli.main(["records", "--verify", str(root)]) == 0


def test_scenario_9_verification_made_to_fail_on_purpose(tmp_path, monkeypatch, capsys):
    """9.9 #9: a tally that does not balance retires nothing, removes the
    half-built index, and leaves the old one serving; a retry once the
    cause is gone duplicates nothing."""
    root, old = _pre_v5(tmp_path)
    real = migration.reconcile

    def broken(*args, **kwargs):
        tally = real(*args, **kwargs)
        tally.unmatched.append("made to fail on purpose")
        return tally

    monkeypatch.setattr(migration, "reconcile", broken)
    assert cli.main(["migrate", "--yes", str(root)]) == 1
    out = capsys.readouterr().out
    assert "made to fail on purpose" in out and "nothing was retired" in out
    pid = identity.read_identity(root).identity.id
    assert old.exists() and not index_db_path(pid).exists()
    assert not (paths.index_dir(pid) / "graph.db.rebuild").exists()
    assert paths.graph_db_path(root) == old
    n = len(_entries(root))
    monkeypatch.setattr(migration, "reconcile", real)
    [done] = migration.migrate(root)
    assert done.ok and len(_entries(root)) == n


def test_scenario_9_an_unreadable_source_stops_the_migration(tmp_path):
    root, old = _pre_v5(tmp_path)
    os.chmod(root / "s.py", 0)
    try:
        [result] = migration.migrate(root, yes=True)
    finally:
        os.chmod(root / "s.py", 0o644)
    assert not result.ok and result.tally.unreadable_sources
    assert old.exists()
    [done] = migration.migrate(root)
    assert done.ok


def test_scenario_9_a_real_sigkill_between_steps(tmp_path):
    """9.9 #9 with a real kill: the process is SIGKILLed after the export,
    then a fresh process resumes."""
    root, old = _pre_v5(tmp_path)
    code = textwrap.dedent(f"""
        import os, signal
        from pathlib import Path
        from rce import migration
        def kill(point):
            if point == "after_export":
                os.kill(os.getpid(), signal.SIGKILL)
        migration.migrate(Path({str(root)!r}), yes=True, fault=kill)
    """)
    env = {**os.environ, migration.ENGINE_PORT_ENV: "0"}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=120)
    assert done.returncode == -signal.SIGKILL
    assert old.exists() and situation.classify(root).needs_migration
    n = len(_entries(root))
    assert n == 8
    [result] = migration.migrate(root)
    assert result.ok and len(_entries(root)) == n and not old.exists()


def test_scenario_9_retire_refused_while_another_process_holds_the_old_index(tmp_path, capsys):
    root, old = _pre_v5(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys,time; f=open(sys.argv[1],'rb'); print('ok', flush=True); time.sleep(60)", str(old)],
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout.readline().strip() == b"ok"
        assert cli.main(["migrate", "--yes", str(root)]) == 1
        assert migration.PLEASE_QUIT in capsys.readouterr().out
        assert old.exists() and situation.classify(root).needs_migration
    finally:
        holder.kill()
        holder.wait()
    [done] = migration.migrate(root)
    assert done.ok and not old.exists()


def test_retire_refused_while_an_engine_answers(tmp_path, monkeypatch):
    root, old = _pre_v5(tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    monkeypatch.setenv(migration.ENGINE_PORT_ENV, str(listener.getsockname()[1]))
    try:
        [result] = migration.migrate(root, yes=True)
    finally:
        listener.close()
    assert not result.ok and migration.PLEASE_QUIT in result.stopped and old.exists()


def test_scenario_9_a_second_old_index_merges_and_its_contradiction_is_a_record_conflict(tmp_path):
    """9.9 #9, last sentence: another machine's index for the same project
    merges; its one contradicting verdict comes under review as a record
    conflict, decided by nobody but the researcher."""
    root, _old = _pre_v5(tmp_path)
    [first] = migration.migrate(root, yes=True)
    assert first.ok
    other = paths.rce_home() / "graphs" / "0123456789abcdef"
    build_pre_v5_index(root, key_dir=other, contradict=True)
    assert cli.main(["migrate", "--yes", "--from", str(other), str(root)]) == 0
    entries = _entries(root)
    added = [e for e in entries if e.get("migrated_from") == "graphs/0123456789abcdef"]
    assert len(added) == 1 and added[0]["verdict"] == "rejected" and added[0]["contradicts"]
    item, status = _state(root, READ)
    assert item["outcome"] == "conflict" and item["reason"] == judgements.RECORD_CONFLICT and status == "auto"
    assert judgements.review_count(db.connect(index_db_path(identity.read_identity(root).identity.id))) >= 1
    judgements.judge(root, READ, "confirmed", via="cli")  # the researcher settles it
    item, status = _state(root, READ)
    assert item["outcome"] == "applied" and status == "confirmed"


# -- the pre-V5 read-only rule, end to end --------------------------------------------------


def test_a_pre_v5_project_refuses_every_human_record_and_reads_from_the_old_index(tmp_path):
    from rce.webapp import server
    from test_records_surfaces import _call

    root, old = _pre_v5(tmp_path)
    httpd = server.build_server(root, 0)
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, summary = _call(base, "GET", "/api/summary")
        assert status == 200 and summary["needs_migration"] and summary["migration"]["waiting"]
        body = dict(zip(("src", "dst", "type", "extractor"), READ))
        for path, payload in (
            ("/api/judgements", {**body, "verdict": "confirmed"}),
            ("/api/edges/reject", body),
            ("/api/mappings/add", {"from": "data/in.csv", "to": "s2.py", "type": "reads"}),
            ("/api/canvas/layout", {"project": str(root), "scope": "all", "positions": {}}),
            ("/api/attempts/write", {"op": "update", "number": "1", "fields": {"verdict": "❌"}}),
        ):
            status, reply = _call(base, "POST", path, payload)
            assert status == 409 and reply["state"] == "needs_migration", (path, reply)
        status, info = _call(base, "GET", "/api/migration")
        assert status == 200 and info["previews"][0]["judged"] == 7
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert not (root / ".rce" / "judgements.toml").exists()
    assert (root / ".rce" / "mappings.toml").read_text() == MAPPINGS
    assert (root / "map.md").read_text() == MAP


def test_http_migration_run_migrates_and_switches(tmp_path):
    from rce.webapp import server
    from test_records_surfaces import _call
    import threading

    root, old = _pre_v5(tmp_path)
    httpd = server.build_server(root, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, reply = _call(base, "POST", "/api/migration/run", {"answer": "maybe"})
        assert status == 400
        status, reply = _call(base, "POST", "/api/migration/run", {"answer": "migrate"})
        assert status == 200 and reply["results"][0]["ok"], reply
        status, summary = _call(base, "GET", "/api/summary")
        assert status == 200 and not summary["needs_migration"] and summary["review"] == 4
        body = dict(zip(("src", "dst", "type", "extractor"), TWO_BASES))
        status, reply = _call(base, "POST", "/api/judgements", {**body, "verdict": "confirmed"})
        assert status == 200 and reply["status"] == "confirmed"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert not old.exists()


def test_http_migration_not_mine(tmp_path):
    from rce.webapp import server
    from test_records_surfaces import _call
    import threading

    root, old = _pre_v5(tmp_path)
    httpd = server.build_server(root, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, reply = _call(base, "POST", "/api/migration/run", {"answer": "not_mine"})
        assert status == 200 and reply["declined"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert old.exists() and paths.legacy_sources(root) == []


def test_a_stranded_index_is_listed_and_migrated_with_from(tmp_path, capsys):
    """9.5 "What is looked for": an index stranded by a move before V5 is
    not this path's, so nothing waits for the moved folder; `--list` shows
    it with the path it recorded (gone), and `--from` migrates it."""
    original = _folder(tmp_path, "before")
    old = build_pre_v5_index(original)
    moved = tmp_path / "after"
    shutil.copytree(original, moved)
    shutil.rmtree(original)
    assert paths.legacy_sources(moved) == []
    assert cli.main(["migrate", str(moved)]) == 1
    assert "no pre-V5 index waits" in capsys.readouterr().err
    assert cli.main(["migrate", "--list"]) == 0
    assert "[missing]" in capsys.readouterr().out
    assert cli.main(["migrate", "--from", str(old.parent), str(moved)]) == 1  # shown, not done
    assert cli.main(["migrate", "--yes", "--from", str(old.parent), str(moved)]) == 0
    assert not old.exists()
    assert _state(moved, READ)[1] == "confirmed"


def test_cli_human_writes_refuse_a_pre_v5_project(tmp_path, capsys):
    root, _old = _pre_v5(tmp_path)
    assert cli.main(["confirm", *READ, "--status", "confirmed", "--path", str(root)]) == 1
    assert "migrat" in capsys.readouterr().err
    assert not (root / ".rce" / "judgements.toml").exists()
    assert cli.main(["init", str(root)]) == 1  # giving it an id is what `rce migrate` does
    assert identity.read_identity(root).state.value == "absent"
