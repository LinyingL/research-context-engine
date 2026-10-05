"""Regression tests for the adversarial review of task V5 (DESIGN.md
section 9), one test (or a few) per confirmed finding. Each names the
finding it pins and, where one applies, the 9.9 scenario it extends."""

from __future__ import annotations

import shutil

import pytest

from rce import db
from rce.records import judgements
from rce.records import ledger as ledger_mod
from rce.records.identity import read_identity
from rce.records.trust import Trust

from test_records_judgements import READ, WRITE, _conn, _entries, _project, _scan, _state, _status


def _decision(root):
    conn = _conn(root)
    try:
        return judgements.assess(conn, root, read_identity(root).identity)[1]
    finally:
        conn.close()


def _apply(root):
    conn = _conn(root)
    try:
        return judgements.apply_ledger(conn, root)
    finally:
        conn.close()


def _confirm_reject_undo_then_restore_first(tmp_path):
    """A link confirmed, rejected by mistake, undone (so: confirmed); then
    the ledger is restored to the copy holding only the confirmation."""
    root, _ = _project(tmp_path)
    path = ledger_mod.judgements_path(root)
    judgements.judge(root, READ, "confirmed", via="cli")
    backup = path.read_bytes()
    judgements.judge(root, READ, "rejected", via="cli")
    judgements.judge(root, READ, "undone", via="cli")
    path.write_bytes(backup)
    assert _apply(root).decision.verdict is Trust.SHRUNK
    return root, path


# -- finding: 「把缺少的补回文件」 was not atomic ----------------------------------------


def test_restore_crash_after_the_append_does_not_ask_again_nor_duplicate(tmp_path, monkeypatch):
    """9.9 #7 / #11 (review finding 1): killed after the missing entries
    were appended and before the index forgot their old ids, the next look
    is not a question any more (each restored entry names the one it
    restores) and the link is what the researcher had: confirmed."""
    root, path = _confirm_reject_undo_then_restore_first(tmp_path)

    def killed(*_args, **_kwargs):
        raise SystemExit("killed between the append and the index update")

    real = db.forget_applied_judgements
    monkeypatch.setattr(db, "forget_applied_judgements", killed)
    with pytest.raises(SystemExit):
        judgements.answer_shrunk(root, judgements.ANSWER_RESTORE)
    monkeypatch.setattr(db, "forget_applied_judgements", real)
    assert [(e.get("verdict"), e.get("via")) for e in _entries(root)] == [
        ("confirmed", "cli"), ("rejected", "recovered"), ("undone", "recovered"),
    ]
    assert _decision(root).verdict is Trust.OK
    _apply(root)
    with pytest.raises(judgements.JudgementRefused) as refused:
        judgements.answer_shrunk(root, judgements.ANSWER_RESTORE)
    assert refused.value.code == "no_question"
    assert _status(root, READ) == "confirmed" and len(_entries(root)) == 3


def test_restore_is_all_or_nothing(tmp_path, monkeypatch):
    """Review finding 1: an entry of the restore refused part-way writes
    none of them -- the file is byte-identical and the question stands."""
    root, path = _confirm_reject_undo_then_restore_first(tmp_path)
    before = path.read_bytes()
    real = ledger_mod._prepare_entry
    calls = {"n": 0}

    def refuse_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ledger_mod.LedgerWriteRefused("refused on purpose")
        return real(*args, **kwargs)

    monkeypatch.setattr(ledger_mod, "_prepare_entry", refuse_second)
    with pytest.raises(judgements.JudgementRefused):
        judgements.answer_shrunk(root, judgements.ANSWER_RESTORE)
    monkeypatch.setattr(ledger_mod, "_prepare_entry", real)
    assert path.read_bytes() == before
    decision = _decision(root)
    assert decision.verdict is Trust.SHRUNK and len(decision.missing) == 2
    judgements.answer_shrunk(root, judgements.ANSWER_RESTORE)
    assert _status(root, READ) == "confirmed"
    assert [e.get("verdict") for e in _entries(root)] == ["confirmed", "rejected", "undone"]


def test_a_half_restored_file_from_an_older_rce_is_completed_not_duplicated(tmp_path):
    """Review finding 1: a file already holding SOME restored entries (as a
    one-by-one restore killed part-way left it) is completed: only the rest
    is appended, and the restored undo cancels the restored reject."""
    root, path = _confirm_reject_undo_then_restore_first(tmp_path)
    missing = _decision(root).missing
    from rce.records.situation import write_guard

    with write_guard(root) as held:
        first = {k: v for k, v in missing[0].items() if k not in ("id", "seq", "at", "via")}
        ledger_mod.append(path, ledger_mod.JUDGEMENT_SCHEMA, {**first, "via": "recovered", "recovered_from": missing[0]["id"]},
                          lock=held, project_root=root)
    decision = _decision(root)
    assert decision.verdict is Trust.SHRUNK and len(decision.missing) == 1
    judgements.answer_shrunk(root, judgements.ANSWER_RESTORE)
    assert [(e.get("verdict"), e.get("via")) for e in _entries(root)] == [
        ("confirmed", "cli"), ("rejected", "recovered"), ("undone", "recovered"),
    ]
    assert _status(root, READ) == "confirmed" and _state(root, READ)["outcome"] == "applied"


# -- finding: 「以文件为准」 on a zero-byte ledger erased the only copy ---------------------


def test_zero_byte_ledger_take_the_file_is_refused_and_restore_still_works(tmp_path):
    """9.9 #11 (review finding 5): a zero-byte ledger in a project whose
    project.toml records one asks the question; 「以文件为准」 would forget
    the only copy and still leave every write refused, so it is refused and
    changes nothing; 「把缺少的补回文件」 restores normal operation."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="cli", note="重要的备注")
    judgements.judge(root, WRITE, "rejected", via="cli")
    path = ledger_mod.judgements_path(root)
    path.write_bytes(b"")
    assert _apply(root).decision.verdict is Trust.SHRUNK
    with pytest.raises(judgements.JudgementRefused) as refused:
        judgements.answer_shrunk(root, judgements.ANSWER_FILE)
    assert refused.value.code == "would_lose"
    conn = _conn(root)
    try:
        assert len(db.applied_judgement_rows(conn)) == 2
    finally:
        conn.close()
    assert path.read_bytes() == b""
    judgements.answer_shrunk(root, judgements.ANSWER_RESTORE)
    assert _status(root, READ) == "confirmed" and _status(root, WRITE) == "rejected"
    assert [e.get("note") for e in _entries(root)] == ["重要的备注", None]
    judgements.judge(root, READ, "withdrawn", via="cli")  # writes work again


# -- finding: the answer was not tied to the question --------------------------------------


def test_answer_refused_when_the_missing_set_changed_since_the_question(tmp_path):
    """Review finding 7: an answer names the missing ids it was shown; the
    file changing before the click refuses it and drops nothing."""
    root, _ = _project(tmp_path)
    path = ledger_mod.judgements_path(root)
    judgements.judge(root, READ, "confirmed", via="cli")
    one = path.read_bytes()
    judgements.judge(root, WRITE, "rejected", via="cli")
    two = path.read_bytes()
    judgements.judge(root, READ, "rejected", via="cli")
    path.write_bytes(two)
    shown = [m["id"] for m in _decision(root).missing]
    path.write_bytes(one)
    with pytest.raises(judgements.JudgementRefused) as refused:
        judgements.answer_shrunk(root, judgements.ANSWER_FILE, expected_missing=shown)
    assert refused.value.code == "question_changed"
    assert len(_decision(root).missing) == 2  # nothing forgotten
    now_shown = [m["id"] for m in _decision(root).missing]
    judgements.answer_shrunk(root, judgements.ANSWER_FILE, expected_missing=now_shown)
    assert _status(root, READ) == "confirmed" and _status(root, WRITE) == "auto"


# -- finding: `rce rebuild` silently answered the 9.3 question ---------------------------


def test_rebuild_is_blocked_while_the_shrink_question_is_open(tmp_path):
    """9.8 / 9.3 (review finding 2): confirm, withdraw, restore the copy
    holding only the confirmation. The question is open; `rce rebuild`
    refuses to swap (a fresh index would obey the shrunk file) and the
    withdrawal survives in the index's copy. Answered, it rebuilds."""
    from rce import rebuild as rebuild_mod

    root, _ = _project(tmp_path)
    path = ledger_mod.judgements_path(root)
    judgements.judge(root, READ, "confirmed", via="cli")
    backup = path.read_bytes()
    judgements.judge(root, READ, "withdrawn", via="cli")
    path.write_bytes(backup)
    assert _apply(root).decision.verdict is Trust.SHRUNK
    result = rebuild_mod.rebuild(root)
    assert not result.swapped and any("fewer than this index applied" in b for b in result.blocked)
    assert _status(root, READ) == "auto"
    decision = _decision(root)
    assert decision.verdict is Trust.SHRUNK and len(decision.missing) == 1
    judgements.answer_shrunk(root, judgements.ANSWER_RESTORE, expected_missing=[m["id"] for m in decision.missing])
    assert rebuild_mod.rebuild(root).swapped
    assert _status(root, READ) == "auto"


# -- finding: a claim killed before its rebuild left the other folder's index ------------


def test_claim_killed_before_the_rebuild_is_finished_on_the_next_open(tmp_path, monkeypatch):
    """9.4 / 9.9 #3 (review finding 3): `rce project claim` killed between
    moving the home here and rebuilding the index must not leave this
    folder served by the OTHER folder's index. The next open finishes the
    claim: the index is this folder's, and no shrink question offers to
    append the other folder's judgment into this ledger."""
    from rce import paths
    from rce import project as project_mod
    from rce import rebuild as rebuild_mod
    from rce.records import situation

    a, _pid = _project(tmp_path)
    judgements.judge(a, READ, "confirmed", via="cli")
    b = tmp_path / "copy"
    shutil.copytree(a, b)
    judgements.judge(a, WRITE, "rejected", via="cli")
    assert situation.classify(b).situation is situation.Situation.COPY

    real = rebuild_mod.rebuild

    def killed(*_args, **_kwargs):
        raise SystemExit("killed before the rebuild")

    monkeypatch.setattr(rebuild_mod, "rebuild", killed)
    with pytest.raises(SystemExit):
        project_mod.claim(b)
    monkeypatch.setattr(rebuild_mod, "rebuild", real)
    pid = read_identity(b).identity.id
    assert situation.read_home(pid).claim_pending

    opened = project_mod.open_project(b)
    assert opened.situation is situation.Situation.NORMAL
    assert not situation.read_home(pid).claim_pending
    conn = db.connect(paths.graph_db_path(b))
    try:
        node = db.get_node(conn, project_mod.project_node_id(pid))
        assert node["attrs"]["path"] == str(b)
    finally:
        conn.close()
    assert _decision(b).verdict is Trust.OK
    assert _status(b, READ) == "confirmed" and _status(b, WRITE) == "auto"
    assert [e.get("verdict") for e in _entries(b)] == ["confirmed"]


# -- finding: renamed aside + copied back made both folders "the home" -------------------


def test_original_renamed_aside_and_copy_put_back_is_a_copy_question(tmp_path):
    """9.4 / 9.9 #3 (review finding 9): `mv Proj Proj-old && cp -R Proj-old
    Proj`. The copy sits at the home's path, the original is the home's
    directory by inode: two live folders, one identity. The original is
    not adopted as a respelling with the copy's scans and judgments in its
    index -- it is asked the copy question, and a write for it is refused."""
    from rce import cli
    from rce.records import situation

    root, pid = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="cli")
    aside = tmp_path / "proj-old"
    root.rename(aside)
    shutil.copytree(aside, root)
    judgements.judge(root, READ, "rejected", via="cli")  # in the copy, at the home's path
    c = situation.classify(aside)
    assert c.situation is situation.Situation.COPY and c.reason == "home_path_holds_a_copy"
    assert cli.main(["status", "--path", str(aside)]) == 1
    with pytest.raises(situation.ProjectMovedError):
        with situation.write_guard(aside, pid):
            pass
    assert [e.get("verdict") for e in _entries(aside)] == ["confirmed"]
    assert situation.read_home(pid).canonical_path != str(aside)


# -- migration findings ----------------------------------------------------------------------


def _migration_fixture(tmp_path):
    from test_migration import _folder, build_pre_v5_index

    root = _folder(tmp_path)
    return root, build_pre_v5_index(root)


def test_a_contradicting_two_entry_verdict_resumes_after_a_kill(tmp_path):
    """9.5 step 1 / 9.9 #9 (review finding 4): a second machine's index
    holds READ confirmed then rejected (a reject that remembered the
    confirmation). It contradicts the ledger and is exported as ONE entry;
    killed after the export, the retry recognises that form, balances, and
    adds nothing."""
    from test_migration import READ as M_READ
    from test_migration import build_pre_v5_index

    from rce import migration, paths
    from rce.records import situation

    root, _old = _migration_fixture(tmp_path)
    [first] = migration.migrate(root, yes=True)
    assert first.ok
    other = paths.rce_home() / "graphs" / "0123456789abcdef"
    db_path = build_pre_v5_index(root, key_dir=other)
    conn = db.connect(db_path)
    try:
        db.reject_edge_remembering(conn, *M_READ)  # confirmed -> rejected, remembering "confirmed"
    finally:
        conn.close()

    def crash(at):
        if at == "after_export":
            raise KeyboardInterrupt(at)

    with pytest.raises(KeyboardInterrupt):
        migration.migrate(root, yes=True, from_dir=other, fault=crash)
    [done] = migration.migrate(root)
    assert done.ok and done.tally.balanced and not done.tally.unmatched, done.tally.lines()
    added = [e for e in _entries(root) if e.get("migrated_from") == "graphs/0123456789abcdef"]
    assert [(e.get("verdict"), bool(e.get("contradicts"))) for e in added] == [("rejected", True)]
    assert not situation.classify(root).needs_migration


def test_old_index_changed_while_retire_waited_is_recorded_and_the_migration_finishes(tmp_path):
    """9.5 step 5 / 9.9 #9 (review finding 10): retire refused while another
    process holds the old index; meanwhile an old engine flips a judged
    link. The retry does not wedge on "does not match": the later click is
    kept as a later migrated entry, the tally balances, the old index is
    retired, and human records are writable again."""
    from test_migration import READ_B

    from rce import migration
    from rce.records import situation

    root, old = _migration_fixture(tmp_path)
    [stopped] = migration.migrate(root, yes=True, holder_probe=lambda _files: {4242})
    assert not stopped.ok and migration.PLEASE_QUIT in stopped.stopped
    conn = db.connect(old)
    try:
        db.set_edge_status(conn, *READ_B, "confirmed")  # an old engine's click
    finally:
        conn.close()
    [done] = migration.migrate(root, holder_probe=lambda _files: set())
    assert done.ok and done.tally.balanced, done.tally.lines()
    assert done.exported.changed and "confirmed" in done.exported.changed[0]
    verdicts = [e.get("verdict") for e in _entries(root) if tuple(e.key) == READ_B]
    assert verdicts == ["rejected", "confirmed"]
    assert _status(root, READ_B) == "confirmed"
    assert not situation.classify(root).needs_migration and not old.exists()
    [again] = migration.migrate(root, yes=True, from_dir=None) if migration.pending_key(root) else [None]
    assert again is None


def test_tally_message_after_install_names_the_index_that_serves(tmp_path, monkeypatch):
    """Review finding 10: once a new index was installed, a resume that does
    not balance does not claim "the old index keeps serving"."""
    from rce import migration

    root, _old = _migration_fixture(tmp_path)
    [stopped] = migration.migrate(root, yes=True, holder_probe=lambda _files: {4242})
    assert not stopped.ok
    monkeypatch.setattr(migration.Tally, "balanced", property(lambda self: False))
    [again] = migration.migrate(root, holder_probe=lambda _files: set())
    assert "the new index installed by an earlier attempt keeps serving" in again.stopped


def test_migrating_from_a_projects_own_dot_rce_moves_only_the_database(tmp_path):
    """9.5 step 5 (review finding 8): `rce migrate --from <proj>/.rce`
    retires the old index -- graph.db and its sidecars -- and never the
    folder it sits in, which holds the researcher's records."""
    from test_migration import ATTEMPTS_TOML, MAPPINGS, _folder, build_pre_v5_index

    from rce import cli, paths

    root = _folder(tmp_path)
    build_pre_v5_index(root, key_dir=root / ".rce")
    (root / ".rce" / "canvas.json").unlink()  # pre-8.10 kept no arrangement in the project
    assert cli.main(["migrate", "--yes", "--from", str(root / ".rce"), str(root)]) == 0
    rce_dir = root / ".rce"
    assert (rce_dir / "project.toml").is_file() and (rce_dir / "judgements.toml").is_file()
    assert (rce_dir / "attempts.toml").read_text() == ATTEMPTS_TOML
    assert (rce_dir / "mappings.toml").read_text() == MAPPINGS
    assert not (rce_dir / "graph.db").exists()
    retired = list((paths.rce_home() / "graphs" / ".retired").iterdir())
    assert len(retired) == 1 and {p.name for p in retired[0].iterdir()} <= {"graph.db", "graph.db-wal", "graph.db-shm"}
    assert cli.main(["records", "--verify", str(root)]) == 0


def test_migrating_from_another_folders_dot_rce_leaves_its_records(tmp_path):
    """Review finding 8: a stranded pre-8.10 copy in ANOTHER folder's
    `.rce/` -- its hand-written attempts.toml and mappings.toml stay where
    they are; only its graph.db is retired."""
    from test_migration import ATTEMPTS_TOML, MAPPINGS, _folder, build_pre_v5_index

    from rce import cli

    root = _folder(tmp_path)
    elsewhere = _folder(tmp_path, "elsewhere")
    build_pre_v5_index(root, key_dir=elsewhere / ".rce")
    assert cli.main(["migrate", "--yes", "--from", str(elsewhere / ".rce"), str(root)]) == 0
    assert (elsewhere / ".rce" / "attempts.toml").read_text() == ATTEMPTS_TOML
    assert (elsewhere / ".rce" / "mappings.toml").read_text() == MAPPINGS
    assert not (elsewhere / ".rce" / "graph.db").exists()


# -- 9.12 (acceptance, 2026-10-05): a pre-V5 project is frozen until migrated, scans included

def _db_bytes(path):
    import hashlib

    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.parent.glob(path.name + "*"))}


def test_pre_v5_project_is_frozen_scans_included_until_migrated(tmp_path, capsys):
    """9.12 (supersedes review finding 11's workaround): a pre-V5 project
    opens for reading, but nothing new lands in its old index before the
    migration reads its own count. `rce ingest`, `rce mappings`, `rce
    attempts` (with and without --check) and `rce judge` refuse with
    "migrate first" and exit non-zero; the old index is byte-identical
    afterwards; reads still work; and the engine's watcher does not poll
    such a project (no re-ingest into the old index, no error chip)."""
    from rce import cli
    from rce.ingest import scan as scan_mod
    from rce.webapp import server as server_mod

    root, old = _migration_fixture(tmp_path)
    before = _db_bytes(old)
    for command in (["ingest", str(root)], ["mappings", str(root)], ["attempts", str(root)],
                    ["attempts", "--check", str(root)], ["judge", str(root)]):
        assert cli.main(command) == 1, command
        err = capsys.readouterr().err
        assert "migrate first: rce migrate" in err and "Nothing written" in err, err
    assert _db_bytes(old) == before
    # Reading works, from the old index, and writes nothing into it.
    for command in (["status", str(root)], ["review", str(root)], ["records", str(root)], ["query", "s.py", str(root)]):
        assert cli.main(command) in (0, 1), command
        assert "Traceback" not in capsys.readouterr().err
    assert cli.main(["status", str(root)]) == 0
    assert _db_bytes(old) == before
    # The backstop: an index without scan reports is never scanned.
    conn = db.connect(old)
    try:
        with pytest.raises(scan_mod.PreScanReportsIndex, match="migrate first"):
            with scan_mod.scan(conn, "test"):
                pass
    finally:
        conn.close()
    # The watcher: inactive for the frozen project, whatever changes.
    httpd = server_mod.build_server(root, 0)
    try:
        assert httpd.get_served().needs_migration
        w = httpd.watcher
        assert w.poll_once() is False
        (root / "map.md").write_text((root / "map.md").read_text() + "| 2 | 2026-07-02 | 第二条路 | Y | 0.6 | ❌ |\n")
        assert w.poll_once() is False
        assert w.status_payload()["last_error"] is None
    finally:
        httpd.server_close()
    assert _db_bytes(old) == before


def test_pre_v5_project_review_is_read_by_the_app_without_an_error(tmp_path):
    """9.12 "Reading works", in the app too: GET /api/review on a project
    frozen until it is migrated answers (an empty list -- its old index
    keeps no review), not a 500 from a column the old index does not have,
    and writes nothing into the old index."""
    import http.client
    import json
    import threading

    from rce.webapp import server as server_mod

    root, old = _migration_fixture(tmp_path)
    before = _db_bytes(old)
    httpd = server_mod.build_server(root, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        assert httpd.get_served().needs_migration
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
        try:
            conn.request("GET", "/api/review")
            resp = conn.getresponse()
            status, body = resp.status, json.loads(resp.read())
        finally:
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert status == 200, body
    assert body["count"] == 0 and body["review"] == [] and body["cards"]["count"] == 0
    assert _db_bytes(old) == before


def test_pre_v5_index_is_writable_again_once_migrated(tmp_path, capsys):
    """The freeze lasts exactly until the migration: afterwards `rce ingest`
    runs against the new index."""
    from rce import cli

    root, _old = _migration_fixture(tmp_path)
    assert cli.main(["ingest", str(root)]) == 1
    capsys.readouterr()
    assert cli.main(["migrate", "--yes", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["ingest", str(root)]) == 0, capsys.readouterr().err


# -- finding: a link the index never held was reviewed without asking whether
#    its source was read --------------------------------------------------------------------


def test_fresh_index_unparseable_source_holds_the_judgment_not_reviews_it(tmp_path):
    """9.9 #5 / #8(e) (review finding 12, case 1): the index is deleted
    while s.py does not parse. The rebuilt index never held the links; its
    scan could not read s.py, so both judgments are 「来源文件暂不可读」 --
    kept, not 待复核 -- and apply again once s.py parses."""
    from rce import paths
    from rce import project as project_mod

    root, pid = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="cli")
    judgements.judge(root, WRITE, "rejected", via="cli")
    good = (root / "s.py").read_text()
    (root / "s.py").write_text("import pandas as pd\ndf = pd.read_csv('data/in.csv'\n")
    shutil.rmtree(paths.index_dir(pid))
    project_mod.open_project(root)
    for key in (READ, WRITE):
        state = _state(root, key)
        assert (state["outcome"], state["reason"]) == ("held", judgements.SOURCE_UNREADABLE), state
    conn = _conn(root)
    try:
        assert judgements.review_count(conn) == 0
    finally:
        conn.close()
    (root / "s.py").write_text(good)
    _scan(root)
    assert _status(root, READ) == "confirmed" and _status(root, WRITE) == "rejected"


def test_fresh_index_that_never_read_the_tracking_store_does_not_review_the_claim(tmp_path):
    """Review finding 12, case 2: a claim judged against an MLflow store
    outside the project (`--mlruns`). The index is rebuilt on open, which
    does not read that store: the experiment cannot be told absent, so the
    judgment is not put under review; reading the store again applies it."""
    from test_records_judgements import _metric

    from rce import paths
    from rce import project as project_mod
    from rce.ingest import pipeline
    from rce.records.situation import write_guard

    root = tmp_path / "proj"
    root.mkdir()
    (root / "paper.md").write_text("## 结果\n\n准确率为 87.3%。其余部分不变。\n")
    ext = tmp_path / "ext"
    _metric(ext, "0.873")
    init = project_mod.init_project(root)

    def scan_with_store():
        with write_guard(root):
            conn = _conn(root)
            try:
                pipeline.ingest_sources(conn, root, mlruns=str(ext / "mlruns"))
            finally:
                conn.close()

    scan_with_store()
    conn = _conn(root)
    try:
        (claim,) = db.get_nodes_by_type(conn, "claim")
    finally:
        conn.close()
    key = (claim["id"], "experiment:run1", "backed_by", "claims")
    judgements.judge(root, key, "confirmed", via="cli")
    shutil.rmtree(paths.index_dir(init.identity.id))
    project_mod.open_project(root)
    assert _state(root, key)["outcome"] == "not_in_index"
    scan_with_store()
    assert _state(root, key)["outcome"] == "applied" and _status(root, key) == "confirmed"


def test_migration_with_an_unparseable_script_stops_on_source_not_readable(tmp_path):
    """9.5 step 4 / 9.9 #9 (review finding 12, case 3): s.py does not parse
    at migration time. Its judged links are 「来源文件暂不可读」, which is not
    zero, so the migration stops and nothing is retired -- an unread file
    must not be mistaken for a vanished link."""
    from rce import migration

    root, old = _migration_fixture(tmp_path)
    (root / "s.py").write_text("import pandas as pd\ndf = pd.read_csv('data/in.csv'\n")
    [result] = migration.migrate(root, yes=True)
    assert not result.ok and result.tally.unreadable >= 3, result.tally.lines()
    assert old.exists()


# -- finding: 「仍然成立」 / the opposite verdict could not settle a not-produced review ----


def test_still_holds_and_opposite_verdict_settle_a_not_produced_review(tmp_path):
    """9.6 / 9.9 #8(c) (review finding 13): a confirmed read whose call was
    deleted is 「待复核 · 机器不再得出这条关联」. The opposite verdict settles
    it, recorded on the basis as it is now (not produced); so does
    「仍然成立」. Put the call back: the last judgment applies again by
    itself, on the basis it was last produced on."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="cli")
    good = (root / "s.py").read_text()
    (root / "s.py").write_text("import pandas as pd\ndf = None\ndf.to_csv('data/out.csv')\n")
    _scan(root)
    assert _state(root, READ)["reason"] == judgements.NOT_PRODUCED
    judged = judgements.judge(root, READ, "rejected", via="cli", note="really gone")
    assert judged.entry.get("basis_recorded") == judgements.ON_ABSENCE
    assert _state(root, READ)["outcome"] == "applied" and _status(root, READ) == "rejected"
    conn = _conn(root)
    try:
        assert judgements.review_count(conn) == 0
    finally:
        conn.close()
    judgements.judge(root, READ, "confirmed", via="cli")  # 「仍然成立」 after all
    assert _state(root, READ)["outcome"] == "applied" and _status(root, READ) == "confirmed"
    _scan(root)  # a rescan changes nothing
    assert _status(root, READ) == "confirmed"
    (root / "s.py").write_text(good)
    _scan(root)
    assert _state(root, READ)["outcome"] == "applied" and _status(root, READ) == "confirmed"


def test_renamed_script_review_can_be_settled_by_a_verdict(tmp_path):
    """Review finding 13, the 8(d) case: the old link's review closes when
    the researcher judges it, not only when they withdraw it."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "rejected", via="cli")
    (root / "s.py").rename(root / "t.py")
    _scan(root)
    assert _state(root, READ)["reason"] == judgements.ENDPOINT_GONE
    judgements.judge(root, READ, "rejected", via="cli")
    assert _state(root, READ)["outcome"] == "applied"


# -- finding: a gitignored dataset made a removed call look like a removed file ----------


def test_removed_call_on_an_ignored_dataset_is_not_produced(tmp_path):
    """9.6 / 9.9 #8(c) (review finding 17): in a git project whose data/ is
    ignored, the dataset is never in the inventory. Deleting the call is
    「机器不再得出这条关联」 -- the file is untouched on disk -- not
    「关联的一端不在本次扫描结果里」."""
    import subprocess

    root, _ = _project(tmp_path)
    (root / ".gitignore").write_text("data/\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    import os

    env = {**os.environ, **env}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, check=True, env=env)
    _scan(root)
    judgements.judge(root, READ, "confirmed", via="cli")
    assert _state(root, READ)["outcome"] == "applied"
    (root / "s.py").write_text("import pandas as pd\ndf = None\ndf.to_csv('data/out.csv')\n")
    subprocess.run(["git", "commit", "-qam", "drop the read"], cwd=root, check=True, env=env)
    _scan(root)
    state = _state(root, READ)
    assert (state["outcome"], state["reason"]) == ("review", judgements.NOT_PRODUCED), state


# -- finding: a half-read MLflow run was reported as read ---------------------------------


@pytest.mark.parametrize("damage", ["empty", "unreadable"])
def test_a_damaged_metric_file_makes_the_store_unparseable_not_a_review(tmp_path, damage):
    """9.6 / 9.9 #8(e) (review finding 14): a metric file emptied mid-write,
    or one that cannot be read, means the run was not fully read. The store
    is reported unparseable (no traceback), and the claim judgment resting
    on it is 「来源文件暂不可读」 -- unchanged, not 待复核."""
    import os
    import stat as stat_mod

    from test_records_judgements import _metric_project

    root, claim = _metric_project(tmp_path)
    judgements.judge(root, claim, "confirmed", via="cli")
    metric = root / "mlruns" / "0" / "run1" / "metrics" / "accuracy"
    if damage == "empty":
        metric.write_text("")
    else:
        os.chmod(metric, 0)
    try:
        if damage == "unreadable" and os.access(metric, os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
        _scan(root)
    finally:
        os.chmod(metric, stat_mod.S_IRUSR | stat_mod.S_IWUSR)
    conn = _conn(root)
    try:
        statuses = {(r["extractor"], r["status"]) for r in db.all_scan_sources(conn) if r["extractor"] in ("mlflow", "claims") and "mlflow:" in r["source"]}
    finally:
        conn.close()
    assert ("mlflow", "unparseable") in statuses and ("claims", "unparseable") in statuses
    state = _state(root, claim)
    assert state["outcome"] == "applied" and (state.get("detail") or {}).get("source_unreadable"), state
    assert _status(root, claim) == "confirmed"


# -- finding: a tracked file renamed in the working tree read as "unreadable" ------------


def test_git_tracked_script_renamed_in_the_working_tree_is_absent_not_unreadable(tmp_path):
    """9.6 / 9.9 #8(d) (review finding 18): in a git project, `mv s.py
    t.py` without committing. `git ls-files` still lists s.py; it is not in
    the working tree, so it is ABSENT -- the judgment comes under review
    (not stuck in 「来源文件暂不可读」), and `rce rebuild` is not blocked."""
    import os
    import subprocess

    from rce import rebuild as rebuild_mod

    root, _ = _project(tmp_path)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, check=True, env=env)
    _scan(root)
    judgements.judge(root, READ, "confirmed", via="cli")
    (root / "s.py").rename(root / "t.py")
    _scan(root)
    state = _state(root, READ)
    assert state["outcome"] == "review" and state["reason"] == judgements.ENDPOINT_GONE, state
    result = rebuild_mod.rebuild(root)
    assert not any("source not readable" in b for b in result.blocked), result.blocked


# -- finding: hand edits seen by the watcher were never snapshotted ----------------------


def test_watcher_snapshots_hand_edited_record_files_once_a_day(tmp_path):
    """9.2 (review finding 6): "a snapshot the first time RCE sees the file
    changed each day -- which is what covers hand edits". The watcher sees
    the record files on first sight and on every change: each changed
    record file -- the ledger, mappings.toml, attempts.toml, the attempt
    table -- gets one snapshot that day, with the bytes RCE saw."""
    from rce.ingest import mappings as mappings_ingest
    from rce.webapp import watcher as watcher_mod

    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="cli")
    mappings_ingest.add_mapping(root, "data/in.csv", "s.py", "reads", note="手画的")
    (root / "map.md").write_text("## H\n\n| # | date | desc | vars | result | verdict |\n|---|---|---|---|---|---|\n"
                                 "| 1 | 2026-01-01 | first | v | r | ✅ |\n", encoding="utf-8")
    (root / ".rce" / "attempts.toml").write_text(
        'file = "map.md"\nheading = "H"\n\n[columns]\nid = "#"\ndate = "date"\ndescription = "desc"\n'
        'variables = "vars"\nresult = "result"\nverdict = "verdict"\n'
    )
    backups = root / ".rce" / "backups"
    shutil.rmtree(backups, ignore_errors=True)
    w = watcher_mod.ProjectWatcher(lambda: root, interval=0.01)
    w.poll_once()  # first sight: every record file is seen
    names = sorted(p.name for p in backups.iterdir())
    for stem in ("judgements", "mappings", "attempts", "map"):
        assert any(n.startswith(stem) for n in names), (stem, names)
    ledger = ledger_mod.judgements_path(root)
    ledger.write_bytes(ledger.read_bytes() + "# 我的批注\n".encode())
    mapping = root / ".rce" / "mappings.toml"
    mapping.write_text(mapping.read_text(encoding="utf-8") + "# 手改\n", encoding="utf-8")
    w.poll_once()
    after = sorted(p.name for p in backups.iterdir())
    assert after == names  # already snapshotted today: a review session never rotates the last good copy
    shutil.rmtree(backups)
    ledger.write_bytes(ledger.read_bytes() + "# 第二条批注\n".encode())
    w.poll_once()
    [snap] = [p for p in backups.iterdir() if p.name.startswith("judgements")]
    assert snap.read_bytes() == ledger.read_bytes()


# -- defect (i), acceptance 2026-10-05: no card and no kind printed twice, in any state ---------


def _printed_twice(text: str) -> list[str]:
    heads = [line.split(":")[0] for line in text.splitlines() if line.startswith("  ") and not line.startswith("   ")]
    return sorted({h for h in heads if heads.count(h) > 1})


@pytest.mark.parametrize("state", ["legacy", "migrating", "migrated", "fresh"])
def test_variable_list_and_records_print_each_card_and_kind_once(tmp_path, capsys, state):
    """`rce variable list` and `rce records` -- legacy, a migration stopped
    before retiring, migrated (with cards made after it, two of them equal
    but for a suffix), and a fresh project: each card and each kind of
    record is one line."""
    from test_migration import _folder, build_pre_v5_index

    from rce import cli, migration
    from rce.records import cards

    if state == "fresh":
        root = tmp_path / "fresh"
        root.mkdir()
        assert cli.main(["init", str(root)]) == 0
    else:
        root = _folder(tmp_path)
        build_pre_v5_index(root)
        if state == "migrating":
            [stopped] = migration.migrate(root, yes=True, holder_probe=lambda _files: {4242})
            assert not stopped.ok
        if state == "migrated":
            assert cli.main(["migrate", "--yes", str(root)]) == 0
    if state in ("migrated", "fresh"):
        cards.new_card(root, "topicshift")
        cards.new_card(root, "TopicShift2")
    capsys.readouterr()
    for command in (["variable", "list", str(root)], ["records", str(root)]):
        assert cli.main(command) == 0, command
        out = capsys.readouterr().out
        assert _printed_twice(out) == [], out
    # At the source, not only after the listing's own guard: one row per
    # kind, one card per id, each problem once.
    from rce import inventory, paths

    db_path = paths.graph_db_path(root)
    conn = db.connect(db_path) if db_path.exists() else None
    try:
        rows = inventory.inventory(conn, root)
        found = cards.overview(conn, root)
    finally:
        if conn is not None:
            conn.close()
    kinds = [r.kind for r in rows]
    assert len(kinds) == len(set(kinds)), kinds
    assert all(len(r.problems) == len(set(r.problems)) for r in rows), [r.problems for r in rows]
    ids = [c["id"] for c in found]
    assert len(ids) == len(set(ids)), ids


# -- defect (ii), acceptance 2026-10-05: a low match caused by a moved or copied project ---------


def test_migrate_preview_says_how_many_scripts_reach_outside_the_folder(tmp_path, capsys):
    """`rce migrate` on a folder whose scripts hard-code absolute paths
    OUTSIDE it (the project was copied away from where its scripts point):
    the judged links' endpoints are not produced, and one line says how many
    scripts read or write paths outside this folder -- so the low match is
    recognisable as that. A folder whose scripts stay inside says nothing."""
    from test_migration import _folder, build_pre_v5_index

    from rce import cli

    root = _folder(tmp_path)
    build_pre_v5_index(root)
    elsewhere = tmp_path / "the-old-place"
    (root / "s.py").write_text(
        "import pandas as pd\n"
        f"df = pd.read_csv('{elsewhere}/data/in.csv')\n"
        f"df.to_csv('{elsewhere}/data/out.csv')\n"
    )
    (root / "s2.py").write_text(f"import pandas as pd\npd.read_csv('{elsewhere}/data/c.csv')\n")
    shutil.rmtree(root / "data")  # the copy brought the scripts, not the data
    cli.main(["migrate", str(root)])  # the preview: nothing written
    out = capsys.readouterr().out
    assert "match: a scan of" in out
    assert "2 script(s) in this folder read or write absolute paths outside it" in out, out

    inside = _folder(tmp_path / "inside")
    build_pre_v5_index(inside)
    cli.main(["migrate", str(inside)])
    out = capsys.readouterr().out
    assert "match: a scan of" in out and "outside it" not in out


# -- follow-up: `rce rebuild` builds an index, so it rewrites .rce/README (9.12)


def test_readme_is_rewritten_by_a_rebuild(tmp_path):
    """`rce rebuild` builds an index too: afterwards `.rce/README` lists the
    record files the folder holds now -- a card directory created since,
    not a record file deleted since."""
    from rce import cli, paths

    root, _pid = _project(tmp_path)
    readme = root / ".rce" / "README"
    (root / ".rce" / "mappings.toml").write_text("", encoding="utf-8")
    paths.write_project_readme(root)
    assert "mappings.toml" in readme.read_text(encoding="utf-8")
    assert cli.main(["variable", "new", "topicshift", str(root)]) == 0
    assert "variables/" not in readme.read_text(encoding="utf-8")
    (root / ".rce" / "mappings.toml").unlink()
    assert cli.main(["rebuild", str(root)]) == 0
    text = readme.read_text(encoding="utf-8")
    assert "mappings.toml" not in text and "variables/" in text
