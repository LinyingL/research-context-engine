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
    """The holder is found (/proc on Linux, lsof on macOS) and named by its
    whole command line up to the cap -- not cut at 80 columns, as `ps`
    writing to a pipe does on Linux (CI run 37268751080)."""
    root = _folder(tmp_path)
    old = build_pre_v5_index(root)
    argv = [sys.executable, "-c", "import sys,time; f=open(sys.argv[1],'rb'); print('ok', flush=True); time.sleep(60)", str(old)]
    holder = subprocess.Popen(argv, stdout=subprocess.PIPE)
    try:
        assert holder.stdout.readline().strip() == b"ok"
        assert cli.main(["migrate", "--yes", str(root)]) == 1
        out = capsys.readouterr().out
    finally:
        holder.kill()
        holder.wait()
    shown = " ".join(argv)
    shown = shown if len(shown) <= migration.COMMAND_LIMIT else shown[: migration.COMMAND_LIMIT - 3] + "..."
    assert f"process {holder.pid} (has graph.db open: {shown})" in out


def _fake_proc(tmp_path: Path, processes: dict[int, tuple[bytes | None, list[Path | str]]]) -> Path:
    """A /proc tree: `<pid>/cmdline` (None: none) and `<pid>/fd/<n>` links."""
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)  # not a pid: passed over
    (proc / "meminfo").write_text("")
    for pid, (cmdline, fds) in processes.items():
        entry = proc / str(pid)
        (entry / "fd").mkdir(parents=True)
        (entry / "comm").write_text(f"comm{pid}\n")
        if cmdline is not None:
            (entry / "cmdline").write_bytes(cmdline)
        for n, target in enumerate(fds):
            os.symlink(str(target), entry / "fd" / str(n))
    return proc


def test_the_proc_reader_names_the_processes_holding_the_index(tmp_path):
    """9.12: where there is no lsof the holder is found by its /proc/<pid>/fd
    links and named by /proc/<pid>/cmdline -- covered here on any platform
    with a fake /proc."""
    data = tmp_path / "data"
    data.mkdir()
    db_file = data / "graph.db"
    db_file.write_bytes(b"x")
    wal = data / "graph.db-wal"
    wal.write_bytes(b"x")
    (tmp_path / "alias").symlink_to(data)
    long_arg = "y" * 300
    proc = _fake_proc(tmp_path, {
        101: (b"/usr/bin/python3\0-c\0import time; time.sleep(60)\0", ["socket:[123]", "/dev/null", db_file]),
        102: (b"sqlite3\0" + str(tmp_path / "alias" / "graph.db").encode() + b"\0", [tmp_path / "alias" / "graph.db"]),
        103: (b"", [wal]),                                   # empty cmdline: its comm
        104: (b"vim\0notes.md\0", [tmp_path / "other.txt"]),  # holds something else
        105: (b"me\0", [db_file]),                           # this process
        106: (b"long\0" + long_arg.encode() + b"\0", [db_file]),
    })
    os.mkdir(proc / "107")  # vanished / unreadable: no fd directory
    found = migration.proc_holders([db_file, wal, data / "graph.db-shm"], proc, me=105)
    assert set(found) == {101, 102, 103, 106}
    assert found[101] == "/usr/bin/python3 -c import time; time.sleep(60)"
    assert found[102].startswith("sqlite3 ")
    assert found[103] == "comm103"
    assert len(found[106]) == migration.COMMAND_LIMIT and found[106].endswith("...")
    assert migration._proc_command(104, proc) == "vim notes.md"
    assert migration._proc_command(999, proc) is None
    # nothing to scan: not a /proc (macOS) -- None, so lsof or the note takes over
    assert migration.proc_holders([db_file], tmp_path / "no-proc") is None
    empty = tmp_path / "empty-proc"
    empty.mkdir()
    assert migration.proc_holders([db_file], empty) is None


def test_on_linux_the_holders_come_from_proc_without_lsof(tmp_path, monkeypatch):
    """With a /proc to read, lsof is never needed (CI's Linux, a container
    with no lsof); with neither, it cannot be told (None)."""
    db_file = tmp_path / "graph.db"
    db_file.write_bytes(b"x")
    proc = _fake_proc(tmp_path, {4242: (b"python3\0hold.py\0", [db_file])})
    monkeypatch.setattr(migration, "PROC_ROOT", proc)
    monkeypatch.setattr(migration, "_lsof", lambda: None)
    assert migration.holders([db_file]) == {4242: "python3 hold.py"}
    monkeypatch.setattr(migration, "PROC_ROOT", tmp_path / "none")
    assert migration.holders([db_file]) is None


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


TRAILING_PATH_SHAPES = [
    # the exact shapes that failed on CPython 3.11 (CI run 37268751080): an
    # optional positional, an option, then the project path
    (["variable", "abandon", "topicshift", "--note", "no stable relation", "/p"], {"path": "/p"}),
    (["variable", "answer", "rv", "file", "--missing", "a,b", "/p"], {"path": "/p"}),
    (["variable", "confirm", "topicshift", "--attest", "unknown", "/p"], {"path": "/p"}),
    (["confirm", "a", "b", "c", "d", "--status", "confirmed", "/p"],
     {"src": "a", "dst": "b", "type": "c", "extractor": "d", "path": "/p"}),
    # the link args split around an option: the later ones fill what is still open, in order
    (["confirm", "a", "b", "--status", "confirmed", "c", "d", "/p"],
     {"src": "a", "dst": "b", "type": "c", "extractor": "d", "path": "/p"}),
    (["confirm", "--index", "1", "--status", "confirmed", "/p"], {"path": "/p", "src": None}),
    (["status", "/p"], {"path": "/p"}),
    # a `--` separator before the trailing path (3.11 left it over): dropped,
    # and what follows it is plain, even a path that starts with "-"
    (["variable", "abandon", "t", "--note", "x", "--", "/p"], {"path": "/p"}),
    (["variable", "settle", "t", "--keep", "e1", "--", "/p"], {"path": "/p"}),
    (["variable", "answer", "rv", "file", "--missing", "a", "--", "/p"], {"path": "/p"}),
    (["confirm", "a", "b", "c", "d", "--status", "confirmed", "--", "/p"],
     {"src": "a", "dst": "b", "type": "c", "extractor": "d", "path": "/p"}),
    (["confirm", "a", "b", "--status", "confirmed", "--", "c", "d", "/p"],
     {"src": "a", "dst": "b", "type": "c", "extractor": "d", "path": "/p"}),
    (["variable", "abandon", "t", "--note", "x", "--", "-p"], {"path": "-p"}),
]


@pytest.mark.parametrize("argv,expected", TRAILING_PATH_SHAPES, ids=lambda a: " ".join(a) if isinstance(a, list) else "")
def test_the_trailing_path_is_read_the_same_on_every_python(argv, expected):
    """9.12: "RCE runs on Python 3.11 ... as it says it does" -- the path
    convention does not depend on a later argparse (`cli.PathParser`)."""
    args = cli.build_parser().parse_args(argv)
    cli._settle_path(args)
    for name, value in expected.items():
        assert getattr(args, name) == value, (argv, name)


@pytest.mark.parametrize("argv", [
    ["status", "/a", "/b"],                                              # one path too many
    ["variable", "abandon", "t", "--note", "x", "/p", "/q"],
    ["confirm", "a", "b", "c", "d", "--status", "confirmed", "/p", "/q"],
    ["status", "/p", "--bogus"],                                         # an unknown option
    ["variable", "abandon", "t", "--note", "x", "--bogus", "/p"],
    ["variable", "abandon", "t", "--note", "x", "--", "/p", "/q"],     # still one path too many
], ids=" ".join)
def test_what_the_parser_refused_it_still_refuses(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err


def test_the_leftover_rule_itself_with_the_older_argparse_shape():
    """`PathParser` given what argparse 3.11 hands back (the optional
    positional consumed empty, the path left over) -- driven directly, so
    the rule is covered whichever Python runs the suite."""
    parser = cli.PathParser(prog="t")
    parser.add_argument("id")
    parser.add_argument("path", nargs="?", default=None)
    parser.add_argument("--note")
    import argparse as _argparse

    def older(self, args=None, namespace=None):
        ns = _argparse.Namespace(id=args[0], path=None, note=None)
        rest, i = [], 1
        while i < len(args):
            if args[i] == "--note":
                ns.note = args[i + 1]
                i += 2
            else:
                rest.append(args[i])
                i += 1
        return ns, rest

    original = _argparse.ArgumentParser.parse_known_args
    _argparse.ArgumentParser.parse_known_args = older
    try:
        ns, extras = parser.parse_known_args(["t", "--note", "x", "/p"])
        assert (ns.path, extras) == ("/p", [])
        ns, extras = parser.parse_known_args(["t", "--note", "x", "/p", "/q"])
        assert (ns.path, extras) == (None, ["/p", "/q"])
        ns, extras = parser.parse_known_args(["t", "--note", "x", "/p", "-z"])
        assert (ns.path, extras) == (None, ["/p", "-z"])
        ns, extras = parser.parse_known_args(["t", "--note", "x", "--", "/p"])
        assert (ns.path, extras) == ("/p", [])
        ns, extras = parser.parse_known_args(["t", "--note", "x", "--", "-p"])
        assert (ns.path, extras) == ("-p", [])
        ns, extras = parser.parse_known_args(["t", "--note", "x", "--", "/p", "/q"])
        assert (ns.path, extras) == (None, ["--", "/p", "/q"])
        ns, extras = parser.parse_known_args(["t", "--note", "x", "--"])
        assert (ns.path, extras) == (None, ["--"])
    finally:
        _argparse.ArgumentParser.parse_known_args = original


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
    # README is RCE's own signpost, rewritten on adoption (9.12); the records are compared.
    before = {p.name: p.read_bytes() for p in (root / ".rce").iterdir() if p.is_file() and p.name not in ("project.toml", "README")}
    _lose_identity(root)
    shutil.rmtree(paths.index_dir(pid))
    assert cli.main(["project", "adopt", str(root)]) == 0
    capsys.readouterr()
    new = identity.read_identity(root).identity
    assert new.id != pid and new.ledger is True and new.forked_from is None
    after = {p.name: p.read_bytes() for p in (root / ".rce").iterdir() if p.is_file() and p.name not in ("project.toml", "README")}
    assert str(paths.index_dir(new.id)) in (root / ".rce" / "README").read_text(encoding="utf-8")
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
def test_lost_identity_is_recognised_by_its_snapshot_alone(tmp_path, capsys, answer):
    """9.12 (acceptance, 2026-10-05): no project.toml and no record files,
    but a snapshot of project.toml under .rce/backups/ -- the folder is
    asked (restore / new identity); `rce init` refuses with the same
    question instead of minting a fresh id; and each answer works."""
    root, pid = _project(tmp_path)
    assert list((root / ".rce" / "backups").glob("project.toml.*"))  # init kept one
    assert not any(os.path.lexists(root / ".rce" / n) for n in situation.V5_RECORD_NAMES)
    (root / ".rce" / "project.toml").unlink()
    c = situation.classify(root)
    assert (c.situation, c.reason) == (Situation.LOST_ID, "identity_snapshot_only")
    assert c.answers == ("restore", "adopt") and c.extra["records"] == []
    payload = c.payload()
    assert payload["snapshot"]["project_id"] == pid
    assert payload["answer_labels"] == {"restore": "从备份恢复项目身份文件", "adopt": "建立新身份"}
    assert ".rce/backups/" in payload["message"]
    assert cli.main(["init", str(root)]) == 1
    err = capsys.readouterr().err
    assert "snapshot" in err and "rce project restore" in err and "rce project adopt" in err
    assert "rce project other" not in err
    assert not (root / ".rce" / "project.toml").exists()  # nothing minted
    served = server.served_for(root)
    assert served.blocked["situation"] == "lost_id" and served.blocked["answers"] == ["restore", "adopt"]
    assert cli.main(["project", answer, str(root)]) == 0
    capsys.readouterr()
    now = identity.read_identity(root).identity
    assert (now.id == pid) is (answer == "restore")
    assert situation.classify(root).situation is Situation.NORMAL


def test_an_answer_the_snapshot_only_question_does_not_offer_is_refused(tmp_path, capsys):
    """Only the answers offered are acted on: a snapshot-only lost identity
    offers restore and adopt, so `rce project other` (and fork, claim) is
    refused, nothing minted."""
    root, _pid = _project(tmp_path)
    (root / ".rce" / "project.toml").unlink()
    assert situation.classify(root).answers == ("restore", "adopt")
    for answer in ("other", "fork", "claim"):
        assert cli.main(["project", answer, str(root)]) == 1, answer
        assert "nothing written" in capsys.readouterr().err
    with pytest.raises(project_identity.AnswerRefused, match="restore / adopt"):
        project_identity.other(root)
    assert not (root / ".rce" / "project.toml").exists()


def test_a_never_initialised_folder_is_still_not_a_project(tmp_path):
    """The snapshot rule does not reach folders that never had an identity:
    an empty .rce/backups/ (or none) is NOT_A_PROJECT, and `rce init` works."""
    root = tmp_path / "fresh"
    (root / ".rce" / "backups").mkdir(parents=True)
    assert situation.classify(root).situation is Situation.NOT_A_PROJECT
    assert cli.main(["init", str(root)]) == 0


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
