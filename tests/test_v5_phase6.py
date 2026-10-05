"""V5 phase 6: the rulings of DESIGN.md 9.12 not yet in the code, and three
defects found in acceptance on real data.

- (a) the retire guard: only a process that actually holds the old index
  blocks its retirement -- an engine serving THIS project (asked through
  `GET /api/engine`, compared canonically), or any process with the
  database open; another engine on another project and another RCE home
  does not (9.12 "Migration"; 9.9 #9's "not while another process holds
  it");
- (b) one path convention: every subcommand takes the project as a
  positional path or as `--path` (9.12 "The command line");
- (c) `rce records` lists each kind of record exactly once;
- the three answers to a lost identity file, with HTTP parity (9.12
  "Identity");
- candidates for claims without the basis clause (9.12 "Evidence"; 9.9 #8(d)
  for a reworded claim);
- "exactly one basis" as the SET of call names (9.12 "Migration"; 9.9 #9);
- a migrated entry's time is never shown as the judgment's (9.12);
- the shrink answer bound to the question shown, on the CLI too (9.12
  "The ledger"; 9.9 #7, #11).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from rce import cli, db, migration, paths
from rce import project as project_identity
from rce.records import identity, judgements
from rce.records import ledger as ledger_mod
from rce.records import situation
from rce.records.situation import Situation, index_db_path
from rce.webapp import server

sys.path.insert(0, str(Path(__file__).parent))
from test_migration import READ as M_READ  # noqa: E402
from test_migration import TWO_BASES, _folder, build_pre_v5_index  # noqa: E402
from test_records_judgements import READ, WRITE, _project, _scan, _state, _status, _three_judgments  # noqa: E402
from test_records_surfaces import _call  # noqa: E402


# -- helpers ----------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port not in (7357, 7399)
    return port


def _start_engine(root: Path, rce_home: Path) -> tuple[subprocess.Popen, int]:
    """A real `rce serve` in its own process, with its own RCE_HOME."""
    port = _free_port()
    env = {**os.environ, "RCE_HOME": str(rce_home), "RCE_ENGINE_PORT": "0"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "rce.cli", "serve", str(root), "--port", str(port), "--no-browser"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"the engine exited with {proc.returncode}")
        if migration.ask_engine(port, timeout=1.0) is not None:
            return proc, port
        time.sleep(0.2)
    proc.kill()
    raise AssertionError("the engine never answered")


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _live(root: Path, served: server.ServedProject | None = None):
    httpd = server.build_server(root, 0, served=served)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, f"http://127.0.0.1:{httpd.server_address[1]}"


def _shutdown(httpd, thread) -> None:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


# -- (a) the retire guard ------------------------------------------------------------------


def test_engine_answers_which_project_it_serves(tmp_path):
    """`GET /api/engine`: the V5 shape the retire guard asks for, without
    opening the index."""
    root, pid = _project(tmp_path)
    httpd, thread, base = _live(root)
    try:
        status, answer = _call(base, "GET", "/api/engine")
    finally:
        _shutdown(httpd, thread)
    assert status == 200
    assert answer["engine"] == "rce" and answer["version"] == 5 and answer["pid"] == os.getpid()
    assert answer["project_id"] == pid and Path(answer["project_root"]) == root
    assert answer["graph_path"] == str(index_db_path(pid)) and answer["rce_home"] == str(paths.rce_home())


def test_scenario_9_an_engine_on_another_project_and_another_home_does_not_block(tmp_path, monkeypatch, capsys):
    """Defect (a), 9.12: the researcher's app open on a DIFFERENT project
    with a DIFFERENT RCE home -- a real second engine -- does not stop the
    migration from retiring the old index."""
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "a.py").write_text("print(1)\n")
    env = {**os.environ, "RCE_HOME": str(other_home), "RCE_ENGINE_PORT": "0"}
    subprocess.run([sys.executable, "-m", "rce.cli", "init", str(other)], env=env, check=True, capture_output=True)
    root = _folder(tmp_path)
    old = build_pre_v5_index(root)
    proc, port = _start_engine(other, other_home)
    try:
        monkeypatch.setenv(migration.ENGINE_PORT_ENV, str(port))
        assert cli.main(["migrate", "--yes", str(root)]) == 0, capsys.readouterr()
        out = capsys.readouterr().out
    finally:
        _stop(proc)
    assert "retired" in out and not old.exists()
    assert not situation.classify(root).needs_migration


def test_scenario_9_an_engine_serving_this_project_blocks_and_is_named(tmp_path, monkeypatch, capsys):
    """9.12: an engine serving THIS project holds its old index -- the stop
    message names it (pid, what it is); once it has quit, `rce migrate`
    resumes and retires."""
    root = _folder(tmp_path)
    old = build_pre_v5_index(root)
    proc, port = _start_engine(root, paths.rce_home())
    try:
        monkeypatch.setenv(migration.ENGINE_PORT_ENV, str(port))
        assert cli.main(["migrate", "--yes", str(root)]) == 1
        out = capsys.readouterr().out
        assert migration.PLEASE_QUIT in out
        assert f"process {proc.pid} (the RCE engine on port {port}, serving {root}" in out
        assert old.exists() and situation.classify(root).needs_migration
    finally:
        _stop(proc)
    assert cli.main(["migrate", str(root)]) == 0  # resumes, no second yes
    assert not old.exists()


def test_an_engine_that_does_not_answer_in_the_v5_shape_blocks(tmp_path, monkeypatch):
    """9.12: an engine that cannot say which project it serves (code from
    before V5) blocks; the message says so."""
    root, old = _folder(tmp_path), None
    old = build_pre_v5_index(root)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    monkeypatch.setenv(migration.ENGINE_PORT_ENV, str(listener.getsockname()[1]))
    try:
        [result] = migration.migrate(root, yes=True)
    finally:
        listener.close()
    assert not result.ok and "does not say which project it serves" in result.stopped and old.exists()


def test_the_engine_comparison_is_canonical_by_root_id_or_old_index(tmp_path, monkeypatch):
    """`engine_holding` compares canonically: another spelling of this
    folder, this id, or this old index's path all block; anything else,
    and the migrating process itself, do not."""
    root = tmp_path / "Proj"
    root.mkdir()
    db_path = tmp_path / "old" / "graph.db"
    monkeypatch.setattr(migration, "engine_port", lambda: 1)
    monkeypatch.setattr(migration, "engine_running", lambda: True)

    def answering(**fields):
        monkeypatch.setattr(migration, "ask_engine", lambda _p, **_k: {"engine": "rce", "version": 5, "pid": 99999, **fields})

    answering(project_root=str(tmp_path / "Proj" / ".." / "Proj"), project_id=None, graph_path=None)
    assert migration.engine_holding(root, None, db_path).pid == 99999
    answering(project_root=str(tmp_path / "x"), project_id="p-" + "0" * 32, graph_path=None)
    assert migration.engine_holding(root, "p-" + "0" * 32, db_path) is not None
    answering(project_root=str(tmp_path / "x"), project_id=None, graph_path=str(db_path))
    assert migration.engine_holding(root, None, db_path) is not None
    answering(project_root=str(tmp_path / "x"), project_id="p-" + "1" * 32, graph_path=str(tmp_path / "y.db"))
    assert migration.engine_holding(root, "p-" + "0" * 32, db_path) is None
    monkeypatch.setattr(migration, "ask_engine", lambda _p, **_k: {
        "engine": "rce", "version": 5, "pid": os.getpid(), "project_root": str(root)})
    assert migration.engine_holding(root, None, db_path) is None  # the migrating engine itself


def test_where_open_files_cannot_be_told_it_is_said_and_the_engine_check_stands(tmp_path):
    """9.12: no lsof-style check on the platform -- said in the result, and
    the retirement goes ahead on the engine comparison alone."""
    root = _folder(tmp_path)
    old = build_pre_v5_index(root)
    [result] = migration.migrate(root, yes=True, holder_probe=lambda _files: None)
    assert result.ok and not old.exists()
    assert any("cannot be told on this platform" in n for n in result.notes)


def test_a_process_holding_the_old_index_is_named_with_its_command(tmp_path, capsys):
    root = _folder(tmp_path)
    old = build_pre_v5_index(root)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys,time; f=open(sys.argv[1],'rb'); print('ok', flush=True); time.sleep(60)", str(old)],
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout.readline().strip() == b"ok"
        assert cli.main(["migrate", "--yes", str(root)]) == 1
        out = capsys.readouterr().out
    finally:
        holder.kill()
        holder.wait()
    assert f"process {holder.pid} (has graph.db open: " in out and "time.sleep" in out


# -- (b) one path convention ---------------------------------------------------------------


SUBCOMMANDS = [
    ["init"], ["ingest"], ["status"], ["query", "project:x"], ["trace", "project:x"], ["lineage"],
    ["serve"], ["projects", "remove"], ["project", "fork"], ["project", "claim"], ["project", "other"],
    ["project", "adopt"], ["project", "restore"], ["attempts"], ["mappings"],
    ["confirm", "a", "b", "c", "d", "--status", "confirmed"], ["confirm", "--index", "1", "--status", "confirmed"],
    ["review"], ["records"], ["rebuild"], ["migrate"], ["judge"],
]


@pytest.mark.parametrize("argv", SUBCOMMANDS, ids=lambda a: " ".join(a))
def test_every_subcommand_takes_the_project_positionally_or_as_path(argv, capsys):
    """Defect (b), 9.12: both spellings everywhere; both at once is refused;
    the help says so."""
    for given in (argv + ["/some/where"], argv + ["--path", "/some/where"]):
        args = cli.build_parser().parse_args(given)
        cli._settle_path(args)
        assert args.path == "/some/where", given
    args = cli.build_parser().parse_args(argv + ["/some/where", "--path", "/else"])
    with pytest.raises(cli.CliError, match="not both"):
        cli._settle_path(args)
    with pytest.raises(SystemExit):
        cli.main(argv[:2 if argv[0] == "project" or argv[0] == "projects" else 1] + ["--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--path PATH" in help_text and "or give it as --path" in help_text


def test_existing_forms_still_work_and_the_new_ones_too(tmp_path, capsys):
    """Nothing that worked breaks: `status --path`, `records <path>`; the new
    spellings run too, including `confirm --index N <path>`."""
    root, _pid = _project(tmp_path)
    assert cli.main(["status", "--path", str(root)]) == 0
    assert cli.main(["status", str(root)]) == 0
    assert cli.main(["records", str(root)]) == 0
    assert cli.main(["records", "--path", str(root)]) == 0
    assert cli.main(["review", "--path", str(root)]) == 0
    assert cli.main(["query", READ[0], str(root)]) == 0
    assert cli.main(["confirm", "--index", "1", "--from-status", "auto", "--status", "rejected", str(root)]) == 0
    assert "recorded rejected" in capsys.readouterr().out
    assert cli.main(["review", str(root), "--path", str(root)]) == 1
    assert "either positionally or via --path" in capsys.readouterr().err


def test_mcp_takes_the_project_both_ways_and_refuses_both(capsys):
    from rce import mcp_server

    assert mcp_server.main(["/a", "--path", "/b"]) == 1
    assert "not both" in capsys.readouterr().err


# -- (c) each kind of record once ----------------------------------------------------------


def _kinds(out: str) -> list[str]:
    return [line.split(":", 1)[0].strip() for line in out.splitlines() if line.startswith("  ") and not line.startswith("    ")]


def test_records_lists_each_kind_exactly_once(tmp_path, capsys):
    """Defect (c): every kind of human labor is one line, whatever the
    state -- a fresh project, cards present, a shrunk ledger, --verify."""
    root, _pid = _project(tmp_path)
    (root / ".rce" / "variables" / "topicshift").mkdir(parents=True)
    _three_judgments(root)
    for argv in (["records", str(root)], ["records", "--verify", str(root)]):
        cli.main(argv)
        kinds = [k for k in _kinds(capsys.readouterr().out) if not k.startswith(("The judgment", "answer", "or"))]
        assert kinds.count("Variable definition cards") == 1
        assert len(kinds) == len(set(kinds)), kinds
    ledger_mod.judgements_path(root).write_bytes(b"")
    cli.main(["records", str(root)])
    out = capsys.readouterr().out
    assert out.count("Variable definition cards") == 1


def test_records_lines_never_repeat_a_kind_even_if_the_inventory_did(tmp_path, monkeypatch):
    from rce import inventory

    root, _pid = _project(tmp_path)
    real = inventory.inventory
    monkeypatch.setattr(inventory, "inventory", lambda conn, r: [*real(conn, r), real(conn, r)[-1]])
    lines = cli.records_inventory_lines(None, root)
    assert sum(1 for line in lines if "Variable definition cards" in line) == 1


# -- a lost identity file: three answers, CLI and HTTP -------------------------------------


def _lose_identity(root: Path) -> None:
    (root / ".rce" / "project.toml").unlink()
    assert situation.classify(root).situation is Situation.LOST_ID


def test_lost_identity_restore_puts_the_snapshot_back(tmp_path, capsys):
    """9.12: 「从备份恢复项目身份文件」 -- the same id, every record and the
    index as they were; the ledger flag is set again."""
    root, pid = _project(tmp_path)
    _three_judgments(root)
    _lose_identity(root)
    c = situation.classify(root)
    assert c.answers == ("restore", "adopt", "other")
    assert c.payload()["snapshot"]["project_id"] == pid and c.payload()["answer_labels"]["restore"]
    before = ledger_mod.judgements_path(root).read_bytes()
    assert cli.main(["project", "restore", str(root)]) == 0
    out = capsys.readouterr().out
    assert f"project {pid}" in out and "Opened: normal" in out
    restored = identity.read_identity(root).identity
    assert restored.id == pid and restored.ledger is True
    assert ledger_mod.judgements_path(root).read_bytes() == before
    assert _status(root, READ) == "confirmed"
    assert cli.main(["records", "--verify", str(root)]) == 0


def test_lost_identity_restore_after_a_fork_restores_the_forked_identity(tmp_path):
    """The newest snapshot is the identity as last written: a fork's new id,
    never the original's it replaced."""
    root, pid = _project(tmp_path)
    _three_judgments(root)  # a record, so a lost identity is a question and not "not a project"
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    assert situation.classify(copy).situation is Situation.COPY
    forked = project_identity.fork(copy).identity
    _lose_identity(copy)
    answered = project_identity.restore(copy)
    assert answered.identity.id == forked.id != pid and answered.identity.forked_from == pid
    assert answered.situation_after == "normal"


def test_lost_identity_adopt_keeps_every_record_under_a_new_id(tmp_path, capsys):
    """9.12: 「沿用这些记录，建立新身份」 -- a new id, every record kept
    where it is, a fresh index built from them."""
    root, pid = _project(tmp_path)
    _three_judgments(root)
    (root / ".rce" / "canvas.json").write_text('{"views": {"all": {"positions": {"script:s.py": [1, 2]}}}}')
    before = {p.name: p.read_bytes() for p in (root / ".rce").iterdir() if p.is_file() and p.name != "project.toml"}
    _lose_identity(root)
    shutil.rmtree(paths.index_dir(pid))
    assert cli.main(["project", "adopt", str(root)]) == 0
    capsys.readouterr()
    new = identity.read_identity(root).identity
    assert new.id != pid and new.ledger is True and new.forked_from is None
    after = {p.name: p.read_bytes() for p in (root / ".rce").iterdir() if p.is_file() and p.name != "project.toml"}
    assert after == before  # every record kept, byte for byte
    assert index_db_path(new.id).exists()
    assert _status(root, READ) == "confirmed" and _status(root, WRITE) == "auto"
    assert cli.main(["records", "--verify", str(root)]) == 0


def test_restore_is_not_offered_without_a_snapshot(tmp_path):
    root, _pid = _project(tmp_path)
    shutil.rmtree(root / ".rce" / "backups")
    _three_judgments(root)
    for snap in (root / ".rce" / "backups").glob("project.toml.*"):
        snap.unlink()
    _lose_identity(root)
    assert situation.classify(root).answers == ("adopt", "other")
    with pytest.raises(project_identity.AnswerRefused):
        project_identity.restore(root)
    assert not (root / ".rce" / "project.toml").exists()


@pytest.mark.parametrize("answer", ["restore", "adopt"])
def test_http_resolve_answers_a_lost_identity(tmp_path, answer):
    """HTTP parity: `POST /api/project/resolve {answer: restore|adopt}`."""
    root, pid = _project(tmp_path)
    _three_judgments(root)
    _lose_identity(root)
    served = server.served_for(root)
    assert served.blocked["situation"] == "lost_id" and served.blocked["answers"] == ["restore", "adopt", "other"]
    httpd, thread, base = _live(root, served)
    try:
        status, reply = _call(base, "POST", "/api/project/resolve", {"answer": "fork"})
        assert status == 409 and reply["state"] == "answer_refused"
        status, reply = _call(base, "POST", "/api/project/resolve", {"answer": answer})
        assert status == 200 and reply["ok"] and reply["blocked"] is None, reply
        status, summary = _call(base, "GET", "/api/summary")
        assert status == 200 and summary["project_id"] == reply["project_id"]
    finally:
        _shutdown(httpd, thread)
    if answer == "restore":
        assert reply["project_id"] == pid and reply["restored_from"].startswith(".rce/backups/project.toml.")
    else:
        assert reply["project_id"] != pid
    assert _status(root, READ) == "confirmed"


# -- candidates for claims (9.12) ----------------------------------------------------------


def test_scenario_8d_a_reworded_claim_offers_the_new_claim_to_the_same_experiment(tmp_path):
    """9.12 "Candidates": for claims the basis clause is dropped -- the new
    backed_by link from the same file to the same experiment is offered."""
    from test_records_judgements import _conn, _metric_project

    root, claim = _metric_project(tmp_path)
    judgements.judge(root, claim, "confirmed", via="canvas")
    (root / "paper.md").write_text("## 结果\n\n准确率达到 87.3%。其余部分不变。\n")
    _scan(root)
    state = _state(root, claim)
    assert state["outcome"] == "review" and state["reason"] in (judgements.ENDPOINT_GONE, judgements.NOT_PRODUCED)
    [cand] = state["candidates"]
    assert cand["src"] != claim[0] and cand["src"].startswith("claim:paper.md#") and cand["dst"] == claim[1]
    conn = _conn(root)
    try:
        assert judgements.link_flags(conn).for_key(judgements.key_of(cand))["candidate_hint"] == judgements.CANDIDATE_HINT
    finally:
        conn.close()
    assert _status(root, judgements.key_of(cand)) == "pending"  # a prompt, nothing carried across


# -- "exactly one basis" is a set (9.12) ---------------------------------------------------


def test_scenario_9_a_link_produced_through_two_calls_is_not_sent_to_review(tmp_path):
    """9.12: the SET of call names over the old index's occurrences equals
    the fresh scan's basis -> recorded at-migration and applied."""
    root = _folder(tmp_path)
    (root / "s2.py").write_text("import pandas as pd\nc = pd.read_csv('data/c.csv')\nf = open('data/c.csv')\n")
    build_pre_v5_index(root)
    [result] = migration.migrate(root, yes=True)
    assert result.ok, result.tally.lines()
    entry = next(e for e in ledger_mod.load_judgements(root).ledger.entries if e.get("src") == TWO_BASES[0])
    assert entry.get("basis_recorded") == "at-migration" and dict(entry.get("basis")) == {"calls": ["open", "read_csv"]}
    conn = db.connect(index_db_path(identity.read_identity(root).identity.id))
    try:
        assert db.judgement_states(conn)[TWO_BASES]["outcome"] == "applied"
        assert db.edge_statuses(conn)[TWO_BASES][0] == "confirmed"
    finally:
        conn.close()


# -- a migrated entry's time (9.12) --------------------------------------------------------


def test_a_migrated_judgment_is_never_shown_with_the_migration_time_as_its_time(tmp_path, capsys):
    """9.12: 「迁移自旧索引（原判断时间未知）」 -- `rce review` and every
    reader get the flag, not the migration time as the judgment's."""
    root = _folder(tmp_path)
    build_pre_v5_index(root)
    [result] = migration.migrate(root, yes=True)
    assert result.ok
    assert cli.main(["review", str(root)]) == 0
    out = capsys.readouterr().out
    assert "was: confirmed (migrated from the old index; original judgment time unknown)" in out
    assert "was: confirmed at " not in out
    conn = db.connect(index_db_path(identity.read_identity(root).identity.id))
    try:
        items = judgements.review_items(conn)["review"]
        flags = judgements.link_flags(conn)
    finally:
        conn.close()
    assert items and all(i["migrated"] and i["at_label"] == judgements.MIGRATED_AT_LABEL for i in items)
    assert flags.for_key(TWO_BASES)["judgement"]["at_label"] == "迁移自旧索引（原判断时间未知）"
    history = judgements.history(root, M_READ)
    assert history[0]["migrated"] is True
    judgements.judge(root, TWO_BASES, "confirmed", via="cli")  # 仍然成立: the researcher's own act
    assert judgements.history(root, TWO_BASES)[-1]["migrated"] is False


# -- the shrink answer belongs to the question shown, on the CLI too (9.12) ----------------


def test_cli_shrink_answer_is_bound_to_the_entries_shown(tmp_path, capsys):
    """9.9 #11 / 9.12: `rce records` shows the question with the missing
    entries' ids and the commands that answer exactly it; an answer naming
    other entries does nothing."""
    root, _pid = _project(tmp_path)
    _three_judgments(root)
    path = ledger_mod.judgements_path(root)
    path.write_bytes(b"")
    _scan(root)
    assert cli.main(["records", str(root)]) == 0
    out = capsys.readouterr().out
    ids = [line.split(":", 1)[0].strip() for line in out.splitlines() if line.strip().startswith("j-")]
    assert len(ids) == 3 and f"--missing {','.join(ids)}" in out
    assert cli.main(["records", "--answer", "restore", "--missing", ids[0], str(root)]) == 1
    assert "changed since the question was shown" in capsys.readouterr().err
    assert path.read_bytes() == b""
    assert cli.main(["records", "--answer", "restore", "--missing", ",".join(ids), str(root)]) == 0
    assert len(ledger_mod.load_judgements(root).ledger.entries) == 3
    assert cli.main(["records", "--missing", ids[0], str(root)]) == 1
