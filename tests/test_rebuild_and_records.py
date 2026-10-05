"""`rce rebuild`, `rce records` / `--verify` / `--clean` (DESIGN.md 9.2, 9.8;
task V5 phase 5), and the 9.9 acceptance scenarios they make possible:
#5 (rebuild on a healthy project, and blocked by an unreadable source) and
#7 (backup and restore)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from rce import cli, db, inventory, paths
from rce import rebuild as rebuild_mod
from rce.records import identity, judgements
from rce.records import ledger as ledger_mod
from rce.records.situation import index_db_path

from test_project_identity import EXPECTED, READ, UNDONE, WRITE, _make, _pid, _records


def _human(root: Path) -> dict:
    conn = db.connect(index_db_path(_pid(root)))
    try:
        return {"statuses": db.edge_statuses(conn), "states": {k: (v["outcome"], v.get("entry_id")) for k, v in db.judgement_states(conn).items()}}
    finally:
        conn.close()


# -- scenario 5 ------------------------------------------------------------------------------


def test_scenario_5_rebuild_on_a_healthy_project_is_clean(tmp_path, capsys):
    """9.9 #5: `rce rebuild` on a healthy project -- all records present,
    `rce records --verify` passes, and the per-link comparison is clean;
    the previous index is kept one generation."""
    root = tmp_path / "p"
    pid = _make(root)
    before = _human(root)
    capsys.readouterr()
    assert cli.main(["rebuild", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Swapped in the new index" in out and "failure" not in out
    assert _records(root) == EXPECTED
    assert _human(root) == before
    assert (paths.index_dir(pid) / "graph.db.prev").exists()
    assert not (paths.index_dir(pid) / "graph.db.rebuild").exists()
    assert cli.main(["records", "--verify", str(root)]) == 0
    assert cli.main(["rebuild", str(root)]) == 0  # again: one generation kept, not two
    assert sorted(p.name for p in paths.index_dir(pid).glob("graph.db*")) == ["graph.db", "graph.db.prev"]


def test_scenario_5_delete_the_index_and_open_all_records_present(tmp_path):
    """9.9 #5, first half: delete `~/.rce/graphs/<id>/` and open the
    project -- all records present, `rce records --verify` passes."""
    root = tmp_path / "p"
    pid = _make(root)
    shutil.rmtree(paths.index_dir(pid))
    assert cli.main(["status", "--path", str(root)]) == 0
    assert _records(root) == EXPECTED
    assert cli.main(["records", "--verify", str(root)]) == 0


def test_scenario_5_an_unreadable_source_blocks_the_swap(tmp_path, capsys):
    """9.9 #5 / 9.8: any source the rebuild scan cannot read blocks the swap
    and is listed; the current index is untouched."""
    root = tmp_path / "p"
    pid = _make(root)
    target = paths.index_dir(pid) / "graph.db"
    before = target.read_bytes()
    os.chmod(root / "a.py", 0)
    try:
        capsys.readouterr()
        assert cli.main(["rebuild", str(root)]) == 1
    finally:
        os.chmod(root / "a.py", 0o644)
    out = capsys.readouterr().out
    assert "source not readable: dataflow: a.py" in out and "Not swapped" in out
    assert target.read_bytes() == before
    assert not (paths.index_dir(pid) / "graph.db.rebuild").exists()
    assert not (paths.index_dir(pid) / "graph.db.prev").exists()
    assert _records(root) == EXPECTED


def test_a_judgment_lost_on_an_unchanged_source_is_a_failure(tmp_path, monkeypatch):
    """9.8: on unchanged sources, a judgment applied before and not applied
    after is a failure -- and nothing is swapped."""
    root = tmp_path / "p"
    pid = _make(root)
    real = judgements.apply_ledger

    def forgets(conn, project_root, **kwargs):
        result = real(conn, project_root, **kwargs)
        conn.execute("DELETE FROM judgement_state WHERE src = ? AND dst = ?", READ[:2])
        conn.execute("UPDATE edges SET status = 'auto' WHERE src = ? AND dst = ? AND type = ?", READ[:3])
        conn.commit()
        return result

    monkeypatch.setattr(judgements, "apply_ledger", forgets)
    result = rebuild_mod.rebuild(root)
    assert not result.swapped
    assert any("dataset:data.csv" in f and "unchanged source" in f for f in result.failures)
    monkeypatch.setattr(judgements, "apply_ledger", real)
    assert _records(root) == EXPECTED
    assert not (paths.index_dir(pid) / "graph.db.rebuild").exists()


def test_a_changed_source_is_not_a_failure(tmp_path):
    """A judgment that stops applying because its source changed is the
    review of 9.6, not a rebuild failure."""
    root = tmp_path / "p"
    _make(root)
    (root / "a.py").write_text('import pandas as pd\ndf = pd.read_table("data.csv")\ndf.to_csv("out.csv")\n')
    result = rebuild_mod.rebuild(root)
    assert result.swapped and not result.failures and result.tally["changed_source"] == 1
    conn = db.connect(index_db_path(_pid(root)))
    try:
        assert db.judgement_states(conn)[READ]["outcome"] == "review"
    finally:
        conn.close()


@pytest.mark.parametrize("point", ["before_install", "before_swap"])
def test_a_crash_during_the_rebuild_leaves_one_index_never_neither(tmp_path, point):
    root = tmp_path / "p"
    pid = _make(root)
    target = paths.index_dir(pid) / "graph.db"

    def crash(at):
        if at == point:
            raise KeyboardInterrupt(at)

    with pytest.raises(KeyboardInterrupt):
        rebuild_mod.rebuild(root, fault=crash)
    assert target.exists()
    assert _records(root) == EXPECTED
    result = rebuild_mod.rebuild(root)  # the leftovers are a crashed attempt's, and are cleared
    assert result.swapped and _records(root) == EXPECTED


def test_rebuild_refuses_a_pre_v5_or_migrating_project(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    paths.legacy_graph_dir(root).mkdir(parents=True)
    db.connect(paths.legacy_index_db_path(root)).close()
    with pytest.raises(rebuild_mod.RebuildRefused, match="rce migrate"):
        rebuild_mod.rebuild(root)
    other = tmp_path / "plain"
    other.mkdir()
    with pytest.raises(rebuild_mod.RebuildRefused, match="rce init"):
        rebuild_mod.rebuild(other)


# -- scenario 7 -----------------------------------------------------------------------------


def test_scenario_7_restore_dot_rce_over_newer_judgments_asks_and_file_wins(tmp_path, capsys):
    """9.9 #7: copy `.rce/` aside, make further judgments, restore the copy
    over `.rce/`: RCE reports that the file has N fewer judgments than the
    index and asks; 「以文件为准」 gives exactly the copy's records."""
    root = tmp_path / "p"
    _make(root)
    aside = tmp_path / "aside"
    shutil.copytree(root / ".rce", aside)
    copy_records = _records(root)
    judgements.judge(root, READ, "rejected", via="cli")
    judgements.judge(root, UNDONE, "withdrawn", via="cli")
    assert _records(root)["confirmed"] == "rejected"
    shutil.rmtree(root / ".rce")
    shutil.copytree(aside, root / ".rce")

    capsys.readouterr()
    assert cli.main(["records", str(root)]) == 0
    out = capsys.readouterr().out
    assert "shrunk: the file lacks 2 entr(y/ies)" in out
    assert _records(root)["confirmed"] == "rejected"  # nothing changed while the question stands
    with pytest.raises(judgements.JudgementRefused):
        judgements.judge(root, WRITE, "confirmed", via="cli")
    assert cli.main(["records", "--answer", "file", str(root)]) == 0
    assert _records(root) == copy_records
    assert (root / ".rce" / "judgements.toml").read_bytes() == (aside / "judgements.toml").read_bytes()
    assert cli.main(["records", "--verify", str(root)]) == 0


def test_scenario_7_whole_folder_restore_on_a_machine_with_no_index(tmp_path):
    """9.9 #7, second half: restore the whole project folder from an earlier
    copy on a machine with no index for it -- the same records, no
    question."""
    root = tmp_path / "p"
    pid = _make(root)
    earlier = tmp_path / "earlier"
    shutil.copytree(root, earlier)
    expected = _records(root)
    judgements.judge(root, READ, "rejected", via="cli")
    shutil.rmtree(root)
    shutil.rmtree(paths.index_dir(pid))  # "a machine with no index for it"
    shutil.copytree(earlier, root)
    assert cli.main(["status", "--path", str(root)]) == 0
    assert _records(root) == expected
    conn = db.connect(index_db_path(pid))
    try:
        assert db.get_record_status(conn, judgements.RECORD_STATUS_NAME)["state"] == "ok"
    finally:
        conn.close()
    assert cli.main(["records", "--verify", str(root)]) == 0


# -- rce records ------------------------------------------------------------------------------


def test_records_inventory_lists_every_kind_with_counts_and_problems(tmp_path, capsys):
    root = tmp_path / "p"
    _make(root)
    (root / ".rce" / "judgements 2.toml").write_text("")
    capsys.readouterr()
    assert cli.main(["records", str(root)]) == 0
    out = capsys.readouterr().out
    for kind in ("Confirm/reject of machine links", "Hand-drawn links", "Attempt verdicts",
                 "Attempt-table configuration", "Canvas arrangement", "Variable definition cards"):
        assert kind in out
    assert "1 link(s), 1 with a note" in out
    assert "1 attempt(s), 1 with a verdict" in out
    assert "judgements 2.toml" in out  # the conflict copy is a trust problem


def test_records_verify_finds_a_mirror_that_differs_from_its_file(tmp_path, capsys):
    root = tmp_path / "p"
    _make(root)
    conn = db.connect(index_db_path(_pid(root)))
    try:
        db.set_human_fields(conn, "attempt:map.md#1", {"verdict": "❌", "result": "r"})
        root_mapping = [e for e in db.query_edges(conn) if e["extractor"] == "mapping"][0]
        db.delete_edge(conn, root_mapping["src"], root_mapping["dst"], root_mapping["type"], "mapping")
    finally:
        conn.close()
    capsys.readouterr()
    assert cli.main(["records", "--verify", str(root)]) == 1
    out = capsys.readouterr().out
    assert "attempt attempt:map.md#1" in out and "in mappings.toml, not in the index" in out


def _card(root: Path, name: str, log: str) -> Path:
    card = root / ".rce" / "variables" / name
    (card / "frozen").mkdir(parents=True)
    (card / "log.toml").write_text(log)
    return card


def test_records_clean_removes_only_copies_nothing_refers_to(tmp_path, capsys):
    """9.8 / 9.11: `rce records --clean` -- dry run by default, `--yes`
    deletes; a referenced copy is never removed, and a card whose log
    cannot be read keeps everything (and so does the shared code store)."""
    root = tmp_path / "p"
    _make(root)
    card = _card(root, "topicshift", '[[entry]]\nid = "v-1"\nact = "confirmed"\nfrozen = "frozen/aaa.toml"\n'
                 '[entry.checked]\nscript = { result = "已核对", copy = "_code/bbb.py" }\n')
    (card / "frozen" / "aaa.toml").write_text("kept")
    (card / "frozen" / "zzz.toml").write_text("left over by a crash")
    code = root / ".rce" / "variables" / "_code"
    code.mkdir()
    (code / "bbb.py").write_text("kept")
    (code / "ccc.py").write_text("left over")

    capsys.readouterr()
    assert cli.main(["records", "--clean", str(root)]) == 0
    out = capsys.readouterr().out
    assert "variables/topicshift/frozen/zzz.toml" in out and "variables/_code/ccc.py" in out
    assert "aaa.toml" not in out and "bbb.py" not in out
    assert (card / "frozen" / "zzz.toml").exists()  # dry run
    assert cli.main(["records", "--clean", "--yes", str(root)]) == 0
    assert not (card / "frozen" / "zzz.toml").exists() and not (code / "ccc.py").exists()
    assert (card / "frozen" / "aaa.toml").exists() and (code / "bbb.py").exists()

    broken = _card(root, "rv", "[[entry]\nthis is not toml")
    (broken / "frozen" / "x.toml").write_text("?")
    (code / "ddd.py").write_text("?")
    report = inventory.clean(root, apply=True)
    assert report.removed == []
    assert ".rce/variables/rv/frozen" in report.undecidable and ".rce/variables/_code" in report.undecidable
    assert (broken / "frozen" / "x.toml").exists() and (code / "ddd.py").exists()


def test_records_clean_is_a_record_write_refused_on_a_pre_v5_project(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    paths.legacy_graph_dir(root).mkdir(parents=True)
    conn = db.connect(paths.legacy_index_db_path(root))
    db.migrate(conn)
    conn.close()
    assert cli.main(["records", "--clean", "--yes", str(root)]) == 1
