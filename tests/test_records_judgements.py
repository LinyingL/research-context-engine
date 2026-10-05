"""The judgment ledger drives the index (DESIGN.md 9.1, 9.3, 9.6; task V5
phase 4): `rce.records.judgements` -- the one write path, the applier, the
SHRUNK question, the review list -- and the 9.9 acceptance scenarios this
phase makes possible (#5, #6, #8 a-e, #11, #12; #10 lives in
tests/test_records_two_writers.py's style below, as a subprocess test)."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest

from rce import db
from rce import project as project_identity
from rce.ingest import pipeline
from rce.records import files as record_files
from rce.records import judgements
from rce.records import ledger as ledger_mod
from rce.records.identity import read_identity
from rce.records.situation import index_db_path, write_guard

READ = ("script:s.py", "dataset:data/in.csv", "reads", "dataflow")
WRITE = ("script:s.py", "dataset:data/out.csv", "writes", "dataflow")
SCRIPT = (
    "import pandas as pd\n"
    "df = pd.read_csv('data/in.csv')\n"
    "df.to_csv('data/out.csv')\n"
)


def _project(tmp_path, script=SCRIPT):
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "data" / "in.csv").write_text("a\n1\n")
    (root / "data" / "out.csv").write_text("a\n1\n")
    (root / "s.py").write_text(script)
    init = project_identity.init_project(root)
    _scan(root)
    return root, init.identity.id


def _conn(root):
    return db.connect(index_db_path(read_identity(root).identity.id))


def _scan(root):
    with write_guard(root):
        conn = _conn(root)
        try:
            pipeline.ingest_sources(conn, root)
        finally:
            conn.close()


def _status(root, key):
    conn = _conn(root)
    try:
        return db.edge_statuses(conn).get(tuple(key), (None, None))[0]
    finally:
        conn.close()


def _state(root, key):
    conn = _conn(root)
    try:
        return db.judgement_states(conn).get(tuple(key))
    finally:
        conn.close()


def _ledger_bytes(root):
    path = ledger_mod.judgements_path(root)
    return path.read_bytes() if path.exists() else None


def _entries(root):
    return ledger_mod.load_judgements(root).ledger.entries


# -- the one write path --------------------------------------------------------


def test_confirm_writes_the_ledger_first_raises_the_flag_and_applies(tmp_path):
    root, _ = _project(tmp_path)
    assert _status(root, READ) == "auto"
    judged = judgements.judge(root, READ, "confirmed", via="cli", note="对")
    assert judged.status == "confirmed"
    (entry,) = _entries(root)
    assert entry.get("verdict") == "confirmed" and entry.get("via") == "cli" and entry.get("note") == "对"
    assert entry.get("basis") == {"calls": ["read_csv"]}
    assert entry.get("basis_recorded") == judgements.AT_JUDGMENT
    assert read_identity(root).identity.ledger is True
    assert _status(root, READ) == "confirmed"
    assert _state(root, READ)["outcome"] == "applied"


def test_a_hand_drawn_link_is_refused_and_nothing_is_written(tmp_path):
    root, _ = _project(tmp_path)
    with pytest.raises(judgements.JudgementRefused) as exc:
        judgements.judge(root, ("script:s.py", "dataset:data/in.csv", "reads", "mapping"), "confirmed", via="mcp")
    assert exc.value.code == "mapping"
    assert _ledger_bytes(root) is None


def test_an_unknown_link_is_refused(tmp_path):
    root, _ = _project(tmp_path)
    with pytest.raises(judgements.JudgementRefused) as exc:
        judgements.judge(root, ("script:s.py", "dataset:nope.csv", "reads", "dataflow"), "confirmed", via="cli")
    assert exc.value.code == "no_such_link"
    assert _ledger_bytes(root) is None


def test_withdrawn_returns_the_link_to_the_machine_status(tmp_path):
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "rejected", via="cli")
    assert _status(root, READ) == "rejected"
    judgements.judge(root, READ, "withdrawn", via="cli")
    assert _status(root, READ) == "auto"
    assert _state(root, READ) is None


def test_undo_only_rejected_refuses_to_undo_a_confirmation(tmp_path):
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="canvas")
    before = _ledger_bytes(root)
    with pytest.raises(judgements.JudgementRefused) as exc:
        judgements.judge(root, READ, "undone", via="canvas", undo_only="rejected")
    assert exc.value.code == "not_rejected"
    assert _ledger_bytes(root) == before


def test_a_human_status_with_no_judgment_behind_it_is_put_back_to_the_machine(tmp_path):
    """9.1: whatever the index knows about a human judgment, it learned
    from the record -- a status written straight into the index does not
    survive the next application."""
    root, _ = _project(tmp_path)
    conn = _conn(root)
    try:
        db.set_edge_status(conn, *READ, "confirmed")
        judgements.apply_ledger(conn, root)
    finally:
        conn.close()
    assert _status(root, READ) == "auto"


def test_review_links_are_not_counted_as_pending(tmp_path):
    root, _ = _project(tmp_path)
    conn = _conn(root)
    try:
        db.upsert_node(conn, "dataset:data/x.csv", "dataset", title="x")
        db.upsert_edge(conn, "script:s.py", "dataset:data/x.csv", "reads", "dataflow", {"file": "s.py"}, 1.0, "pending")
        assert len(db.pending_edges(conn)) == 1
        db.write_judgement_state(
            conn, statuses={},
            states={("script:s.py", "dataset:data/x.csv", "reads", "dataflow"): {"outcome": "review", "reason": "not_produced"}},
            applied=None,
        )
        assert db.pending_edges(conn) == []
    finally:
        conn.close()


# -- 9.9 #12: history ---------------------------------------------------------------


def _clock(start):
    moments = iter(start)
    return lambda: next(moments)


def test_scenario_12_history_in_append_order_and_the_clock_never_decides(tmp_path):
    """9.9 #12. Confirm; reject; undo -- confirmed; confirm again with a
    note; withdraw -- the machine's. Five entries in order. The clock is set
    back an hour between two of them: the later act still wins."""
    root, _ = _project(tmp_path)
    t0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    judgements.judge(root, READ, "confirmed", via="canvas", now=lambda: t0)
    judgements.judge(root, READ, "rejected", via="canvas", now=lambda: t0 + timedelta(minutes=1))
    assert _status(root, READ) == "rejected"
    # The clock is set back an hour: the undo is stamped BEFORE the reject.
    judgements.judge(root, READ, "undone", via="canvas", now=lambda: t0 - timedelta(hours=1))
    assert _status(root, READ) == "confirmed"
    judgements.judge(root, READ, "confirmed", via="canvas", note="再看一遍，没问题", now=lambda: t0 - timedelta(minutes=30))
    assert _status(root, READ) == "confirmed"
    judgements.judge(root, READ, "withdrawn", via="canvas", now=lambda: t0 - timedelta(minutes=59))
    assert _status(root, READ) == "auto"
    entries = _entries(root)
    assert [e.get("verdict") for e in entries] == ["confirmed", "rejected", "undone", "confirmed", "withdrawn"]
    assert [e.seq for e in entries] == [1, 2, 3, 4, 5]
    hist = judgements.history(root, READ)
    assert [h["verdict"] for h in hist] == ["confirmed", "rejected", "undone", "confirmed", "withdrawn"]
    assert [h["cancelled"] for h in hist] == [False, True, False, False, False]


def _merge_two_copies(root):
    """Two machines appended to copies of the same file after the same
    entry, and a sync merged them: the second copy's entry repeats seq 2."""
    path = ledger_mod.judgements_path(root)
    text = path.read_text(encoding="utf-8")
    other = (
        "\n[[judgement]]\n"
        'id = "j-00000000000000000000000000000002"\n'
        "seq = 2\n"
        'at = "2026-10-05T09:00:00+02:00"\n'
        'verdict = "confirmed"\n'
        f'src = "{READ[0]}"\ndst = "{READ[1]}"\ntype = "{READ[2]}"\nextractor = "{READ[3]}"\n'
        'via = "canvas"\n'
    )
    path.write_text(text + other, encoding="utf-8")


def test_scenario_12_merged_copies_are_a_conflict_settled_by_a_new_entry(tmp_path):
    """9.9 #12, second half: two copies that each appended after the same
    entry are merged; the link shows 「记录冲突，待处理」 at the machine's
    status, nothing is decided by time, and a new entry settles it."""
    root, _ = _project(tmp_path)
    judgements.judge(root, WRITE, "confirmed", via="canvas")  # seq 1, the common entry
    judgements.judge(root, READ, "rejected", via="canvas")  # seq 2 on this machine
    _merge_two_copies(root)  # seq 2 on the other machine: confirmed
    conn = _conn(root)
    try:
        judgements.apply_ledger(conn, root)
        items = judgements.review_items(conn)
    finally:
        conn.close()
    assert _status(root, READ) == "auto"
    state = _state(root, READ)
    assert state["outcome"] == "conflict" and state["reason"] == judgements.RECORD_CONFLICT
    conflict = [i for i in items["review"] if i["outcome"] == "conflict"]
    assert len(conflict) == 1 and conflict[0]["label"] == "记录冲突，待处理"
    branches = conflict[0]["detail"]["branches"]
    assert sorted(e["verdict"] for b in branches for e in b) == ["confirmed", "rejected"]
    # An undo cannot settle it; a new judgment does.
    with pytest.raises(judgements.JudgementRefused):
        judgements.judge(root, READ, "undone", via="canvas")
    judgements.judge(root, READ, "rejected", via="canvas")
    assert _status(root, READ) == "rejected"
    assert _state(root, READ)["outcome"] == "applied"


# -- 9.9 #8: evidence changes -----------------------------------------------------------


def test_scenario_8a_insert_lines_rename_variable_and_alias_the_judgment_applies(tmp_path):
    """9.9 #8(a): lines inserted above the judged call, the receiving
    variable renamed, the import aliased -- the judgment applies, nothing
    is flagged, nothing is written to the ledger."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "rejected", via="canvas")
    before = _ledger_bytes(root)
    (root / "s.py").write_text(
        "# a comment\n\nimport pandas as pandas_lib\n\n\nframe = pandas_lib.read_csv('data/in.csv')\n"
        "frame.to_csv('data/out.csv')\n"
    )
    _scan(root)
    assert _status(root, READ) == "rejected"
    assert _state(root, READ)["outcome"] == "applied"
    assert _ledger_bytes(root) == before
    conn = _conn(root)
    try:
        assert judgements.review_items(conn)["count"] == 0
    finally:
        conn.close()


def test_scenario_8b_changed_function_is_under_review_and_still_holds_settles_it(tmp_path):
    """9.9 #8(b): change which function is called -- 「待复核 · 依据已变化」,
    old verdict and basis visible, the link at the machine's status and
    marked; 「仍然成立」 clears it and the ledger shows both entries."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "rejected", via="canvas", note="旧面板")
    (root / "s.py").write_text("import pandas as pd\ndf = pd.read_json('data/in.csv')\ndf.to_csv('data/out.csv')\n")
    _scan(root)
    assert _status(root, READ) == "auto"
    conn = _conn(root)
    try:
        items = judgements.review_items(conn)
        flags = judgements.link_flags(conn).for_key(READ)
        pending = db.pending_edges(conn)
    finally:
        conn.close()
    (item,) = items["review"]
    assert item["reason"] == judgements.BASIS_CHANGED and item["label"] == "依据已变化"
    assert item["verdict"] == "rejected" and item["note"] == "旧面板" and item["at"]
    assert item["basis"] == {"calls": ["read_csv"]} and item["basis_now"] == {"calls": ["read_json"]}
    assert flags["review"] is True and flags["judgement"]["label"] == "依据已变化"
    assert all(judgements.key_of(e) != READ for e in pending)
    # 「仍然成立」: the same verdict again, on the basis as it is now.
    judgements.judge(root, READ, "rejected", via="canvas")
    assert _status(root, READ) == "rejected"
    assert _state(root, READ)["outcome"] == "applied"
    entries = _entries(root)
    assert [e.get("basis") for e in entries] == [{"calls": ["read_csv"]}, {"calls": ["read_json"]}]


def test_scenario_8c_deleted_call_is_not_produced_and_comes_back_by_itself(tmp_path):
    """9.9 #8(c), the call half: delete the call -- 「机器不再得出这条关联」;
    put it back -- the judgment applies again with no new entry."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="canvas")
    before = _ledger_bytes(root)
    (root / "s.py").write_text("import pandas as pd\ndf = pd.DataFrame()\ndf.to_csv('data/out.csv')\n")
    _scan(root)
    state = _state(root, READ)
    assert state["outcome"] == "review" and state["reason"] == judgements.NOT_PRODUCED
    assert _status(root, READ) == "auto"
    (root / "s.py").write_text(SCRIPT)
    _scan(root)
    assert _status(root, READ) == "confirmed"
    assert _state(root, READ)["outcome"] == "applied"
    assert _ledger_bytes(root) == before


def _metric_project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "paper.md").write_text("## 结果\n\n准确率为 87.3%。其余部分不变。\n")
    _metric(root, "0.873")
    project_identity.init_project(root)
    _scan(root)
    conn = _conn(root)
    try:
        (claim,) = db.get_nodes_by_type(conn, "claim")
    finally:
        conn.close()
    return root, (claim["id"], "experiment:run1", "backed_by", "claims")


def _metric(root, value):
    run_dir = root / "mlruns" / "0" / "run1"
    (run_dir / "metrics").mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.yaml").write_text("experiment_id: '0'\nrun_id: run1\nstatus: FINISHED\n")
    (run_dir / "metrics" / "accuracy").write_text(f"1700000000000 {value} 0\n")


def test_scenario_8c_metric_that_no_longer_rounds_is_not_produced_and_comes_back(tmp_path):
    """9.9 #8(c), the metric half: a claim–metric link confirmed at 0.873
    is not carried over to 0.95; back at a value that rounds to 87.3 it
    applies again with no new entry."""
    root, claim = _metric_project(tmp_path)
    assert _status(root, claim) == "pending"
    judgements.judge(root, claim, "confirmed", via="canvas")
    assert _status(root, claim) == "confirmed"
    before = _ledger_bytes(root)
    _metric(root, "0.95")
    _scan(root)
    state = _state(root, claim)
    assert state["outcome"] == "review" and state["reason"] == judgements.NOT_PRODUCED
    assert state["basis"]["metrics"] == {"accuracy": "0.873"}
    assert _status(root, claim) == "pending"
    conn = _conn(root)
    try:
        assert all(judgements.key_of(e) != claim for e in db.pending_edges(conn))  # 待复核, not 待确认
    finally:
        conn.close()
    _metric(root, "0.8731")
    _scan(root)
    assert _status(root, claim) == "confirmed"
    assert _ledger_bytes(root) == before


def test_scenario_8d_renamed_script_is_under_review_with_its_candidate(tmp_path):
    """9.9 #8(d): rename the script -- the old judgment is under review with
    its reason, the new read is listed beside it as a candidate and carries
    the hint; nothing is carried across until the researcher clicks."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "rejected", via="canvas")
    (root / "s.py").rename(root / "t.py")
    _scan(root)
    new_read = ("script:t.py", "dataset:data/in.csv", "reads", "dataflow")
    state = _state(root, READ)
    assert state["outcome"] == "review" and state["reason"] == judgements.ENDPOINT_GONE
    assert [judgements.key_of(c) for c in state["candidates"]] == [new_read]
    assert _status(root, new_read) == "auto"  # nothing carried across
    conn = _conn(root)
    try:
        flags = judgements.link_flags(conn)
    finally:
        conn.close()
    assert flags.for_key(new_read)["candidate_hint"] == judgements.CANDIDATE_HINT
    assert flags.for_key(READ)["review"] is True
    # The researcher's click: a new entry on the new link.
    judgements.judge(root, new_read, "rejected", via="canvas")
    assert _status(root, new_read) == "rejected"


def test_scenario_8e_unparseable_script_changes_nothing_and_is_reported(tmp_path):
    """9.9 #8(e), unparseable: nothing comes under review, the judgment is
    unchanged and the source is reported."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "rejected", via="canvas")
    before = _ledger_bytes(root)
    (root / "s.py").write_text("import pandas as pd\ndf = pd.read_table('data/in.csv'\n")
    _scan(root)
    assert _status(root, READ) == "rejected"
    conn = _conn(root)
    try:
        items = judgements.review_items(conn)
    finally:
        conn.close()
    assert items["count"] == 0
    assert [judgements.key_of(i) for i in items["source_unreadable"]] == [READ]
    assert items["source_unreadable"][0]["source_status"] == "unparseable"
    assert _ledger_bytes(root) == before


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions, non-root")
def test_scenario_8e_unreadable_script_changes_nothing_and_is_reported(tmp_path):
    """9.9 #8(e), unreadable."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="canvas")
    (root / "s.py").chmod(0)
    try:
        _scan(root)
        assert _status(root, READ) == "confirmed"
        state = _state(root, READ)
        assert state["outcome"] == "applied" and state["source_status"] == "unreadable"
        conn = _conn(root)
        try:
            assert judgements.review_items(conn)["count"] == 0
        finally:
            conn.close()
    finally:
        (root / "s.py").chmod(stat.S_IRUSR | stat.S_IWUSR)


# -- 9.9 #6 and #5 ------------------------------------------------------------------------


def _human_state(root):
    conn = _conn(root)
    try:
        statuses = {k: v[0] for k, v in db.edge_statuses(conn).items()}
        states = {k: (v["outcome"], v["reason"], v["entry_id"]) for k, v in db.judgement_states(conn).items()}
        return statuses, states
    finally:
        conn.close()


def test_scenario_6_every_scan_three_times_changes_nothing(tmp_path):
    """9.9 #6: run every scan three times -- the ledger is byte-identical,
    the index's human state is identical, nothing comes under review."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="canvas")
    judgements.judge(root, WRITE, "confirmed", via="canvas")
    judgements.judge(root, WRITE, "rejected", via="canvas")
    judgements.judge(root, WRITE, "undone", via="canvas")
    before_bytes, before_state = _ledger_bytes(root), _human_state(root)
    for _ in range(3):
        _scan(root)
        with write_guard(root):
            conn = _conn(root)
            try:
                pipeline.ingest_records(conn, root)
            finally:
                conn.close()
        assert _ledger_bytes(root) == before_bytes
        assert _human_state(root) == before_state
    conn = _conn(root)
    try:
        assert judgements.review_items(conn)["count"] == 0
    finally:
        conn.close()


def test_scenario_5_delete_the_index_and_reopen_all_judgments_are_back(tmp_path):
    """9.9 #5: delete ~/.rce/graphs/<id>/ and open the project: the index is
    built from the sources and the record, every judgment applied, and
    `verify` finds nothing to report."""
    root, project_id = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="canvas")
    judgements.judge(root, WRITE, "rejected", via="canvas")
    shutil.rmtree(index_db_path(project_id).parent)
    opened = project_identity.open_project(root)
    assert opened.created_index
    assert _status(root, READ) == "confirmed"
    assert _status(root, WRITE) == "rejected"
    conn = _conn(root)
    try:
        assert judgements.verify(conn, root) == []
    finally:
        conn.close()


# -- 9.9 #11: a record RCE cannot trust ----------------------------------------------------


def _three_judgments(root):
    judgements.judge(root, READ, "confirmed", via="canvas")
    judgements.judge(root, WRITE, "rejected", via="canvas")
    judgements.judge(root, WRITE, "undone", via="canvas")


def _click_writes_nothing(root):
    """A click on confirm or reject writes NOTHING, and says why."""
    path = ledger_mod.judgements_path(root)
    before = path.read_bytes() if path.exists() else None
    for verdict in ("confirmed", "rejected"):
        with pytest.raises(judgements.JudgementRefused) as exc:
            judgements.judge(root, READ, verdict, via="canvas")
        assert exc.value.code == "untrusted" and exc.value.message_zh
    assert (path.read_bytes() if path.exists() else None) == before
    return exc.value


def _rescan_and_expect_kept(root):
    _scan(root)
    assert _status(root, READ) == "confirmed"
    assert _status(root, WRITE) == "auto"


@pytest.mark.parametrize("damage", ["unparseable", "removed"])
def test_scenario_11_unparseable_or_removed_ledger_keeps_the_index_and_refuses_writes(tmp_path, damage):
    """9.9 #11: unparseable, removed -- the index keeps what it had, the
    app is told what is wrong, a click writes nothing, repairing the file
    restores normal operation."""
    root, _ = _project(tmp_path)
    _three_judgments(root)
    path = ledger_mod.judgements_path(root)
    good = path.read_bytes()
    if damage == "unparseable":
        path.write_bytes(good + b"\n[[judgement]\nverdict = \n")
    else:
        path.unlink()
    _rescan_and_expect_kept(root)
    refused = _click_writes_nothing(root)
    assert refused.message_zh == "判断记录文件当前无法读取，请先恢复它"
    conn = _conn(root)
    try:
        ledger_state = judgements.review_items(conn)["ledger"]
    finally:
        conn.close()
    assert ledger_state["state"] == "refuse_writes"
    path.write_bytes(good)  # repaired
    judgements.judge(root, READ, "rejected", via="canvas")
    assert _status(root, READ) == "rejected"


@pytest.mark.parametrize("damage", ["zero_bytes", "truncated"])
def test_scenario_11_zero_byte_or_truncated_ledger_asks_the_question(tmp_path, damage):
    """9.9 #11: zero bytes, truncated at an entry boundary -- the index keeps
    what it had, a click writes nothing, and the 9.3 question is asked."""
    root, _ = _project(tmp_path)
    _three_judgments(root)
    path = ledger_mod.judgements_path(root)
    good = path.read_bytes()
    if damage == "zero_bytes":
        path.write_bytes(b"")
        missing = 3
    else:
        text = good.decode("utf-8")
        path.write_bytes(text[: text.rindex("\n[[judgement]]")].encode("utf-8") + b"\n")
        missing = 1
    _rescan_and_expect_kept(root)
    refused = _click_writes_nothing(root)
    assert refused.decision.verdict.value == "shrunk"
    assert refused.message_zh == f"记录文件比图谱少了 {missing} 条判断"
    conn = _conn(root)
    try:
        assert len(judgements.review_items(conn)["ledger"]["missing"]) == missing
    finally:
        conn.close()
    path.write_bytes(good)  # repaired
    _scan(root)
    judgements.judge(root, READ, "rejected", via="canvas")
    assert _status(root, READ) == "rejected"


def test_shrunk_answer_restore_appends_the_missing_entries_as_recovered(tmp_path):
    """9.3 「把缺少的补回文件」: the missing entries are appended again with
    via = "recovered", their content kept, an undo re-pointed at the
    restored entry it cancels."""
    root, _ = _project(tmp_path)
    _three_judgments(root)
    path = ledger_mod.judgements_path(root)
    path.write_bytes(b"")
    answered = judgements.answer_shrunk(root, judgements.ANSWER_RESTORE)
    assert len(answered.appended) == 3
    entries = _entries(root)
    assert [e.get("via") for e in entries] == ["recovered"] * 3
    assert [e.get("verdict") for e in entries] == ["confirmed", "rejected", "undone"]
    assert entries[2].get("undoes") == entries[1].id
    assert all(e.get("recovered_from") for e in entries)
    assert _status(root, READ) == "confirmed" and _status(root, WRITE) == "auto"
    judgements.judge(root, READ, "rejected", via="canvas")  # writes work again
    assert _status(root, READ) == "rejected"


def test_scenario_7_restore_a_copy_of_rce_and_take_the_file_as_truth(tmp_path):
    """9.9 #7: copy .rce/ aside, judge further, restore the copy: RCE says
    the file has N fewer judgments and asks; 「以文件为准」 gives exactly the
    copy's records."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="canvas")
    aside = tmp_path / "aside"
    shutil.copytree(root / ".rce", aside)
    judgements.judge(root, WRITE, "rejected", via="canvas")
    judgements.judge(root, READ, "rejected", via="canvas")
    shutil.rmtree(root / ".rce")
    shutil.copytree(aside, root / ".rce")
    _scan(root)
    assert _status(root, READ) == "rejected"  # nothing changed before the answer
    conn = _conn(root)
    try:
        assert judgements.review_items(conn)["ledger"]["message"] == "记录文件比图谱少了 2 条判断"
    finally:
        conn.close()
    judgements.answer_shrunk(root, judgements.ANSWER_FILE)
    assert _status(root, READ) == "confirmed" and _status(root, WRITE) == "auto"
    assert len(_entries(root)) == 1
    with pytest.raises(judgements.JudgementRefused) as exc:
        judgements.answer_shrunk(root, judgements.ANSWER_FILE)
    assert exc.value.code == "no_question"


def test_a_conflict_copy_beside_the_ledger_refuses_writes(tmp_path):
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="canvas")
    path = ledger_mod.judgements_path(root)
    shutil.copy(path, path.with_name("judgements 2.toml"))
    refused = _click_writes_nothing(root)
    assert refused.decision.verdict.value == "conflict_copy"


def test_entries_the_index_never_saw_are_simply_applied(tmp_path):
    """9.3: the mirrored question -- the file has entries the index never
    saw (a judgment made on another Mac) -- is not a question."""
    root, project_id = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="canvas")
    path = ledger_mod.judgements_path(root)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text + "\n[[judgement]]\n" 'id = "j-00000000000000000000000000000009"\nseq = 2\n'
        'at = "2026-10-05T10:00:00+02:00"\nverdict = "rejected"\n'
        f'src = "{WRITE[0]}"\ndst = "{WRITE[1]}"\ntype = "{WRITE[2]}"\nextractor = "{WRITE[3]}"\nvia = "cli"\n'
        '[judgement.basis]\ncalls = ["to_csv"]\n',
        encoding="utf-8",
    )
    _scan(root)
    assert _status(root, WRITE) == "rejected"


# -- 9.9 #10: two writers -----------------------------------------------------------------

_TWO_WRITERS = r"""
import sys
from pathlib import Path
from rce import db, paths
from rce.records import judgements
from rce.webapp import canvas
root, tag, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
key = ("script:s.py", "dataset:data/in.csv" if tag == "a" else "dataset:data/out.csv",
       "reads" if tag == "a" else "writes", "dataflow")
canvas.view_card_ids = lambda conn, project_root, scope: {f"{t}-{i}" for t in "ab" for i in range(n)}
conn = db.connect(paths.graph_db_path(root))
for i in range(n):
    judgements.judge(root, key, "confirmed" if i % 2 else "rejected", via="cli", note=f"{tag}{i}")
    canvas.save_layout(conn, root, {"scope": "all", "positions": {f"{tag}-{i}": [float(i), 1.0]}})
conn.close()
"""


def test_scenario_10_two_processes_judgments_and_positions_all_land(tmp_path):
    """9.9 #10: two processes each make 300 judgments and 300 position
    changes on one project at once: every one is in the record afterwards,
    in one unbroken seq order, and nothing raises."""
    import subprocess

    root, _ = _project(tmp_path)
    n = 300
    procs = [
        subprocess.Popen([sys.executable, "-c", _TWO_WRITERS, str(root), tag, str(n)], stderr=subprocess.PIPE)
        for tag in ("a", "b")
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=600)
        assert proc.returncode == 0, err.decode()
    entries = _entries(root)
    assert len(entries) == 2 * n
    assert [e.seq for e in entries] == list(range(1, 2 * n + 1))
    assert sorted(e.get("note") for e in entries) == sorted(f"{t}{i}" for t in "ab" for i in range(n))
    from rce.webapp import canvas

    assert len(canvas.load_views(root)["all"]["positions"]) == 2 * n
    # The last act of each process stands (i = n-1 is odd: confirmed).
    assert _status(root, READ) == "confirmed" and _status(root, WRITE) == "confirmed"
