"""Tests for rce.records.ledger (DESIGN.md section 9.3): the append-only
ledger engine and the judgment ledger. Scenario numbers are 9.9's."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rce import paths
from rce.records import files, lock
from rce.records import ledger as L

PID = "p-" + "cd" * 16
TZ = timezone(timedelta(hours=2))

READS = dict(src="script:a/17.Rmd", dst="dataset:a/Data/panel.csv", type="reads", extractor="dataflow")
WRITES = dict(src="script:a/16.py", dst="dataset:a/Data/ts.csv", type="writes", extractor="dataflow")
CLAIM = dict(src="claim:paper.md#0123456789abcdef", dst="experiment:run1", type="backed_by", extractor="claims")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rce").mkdir(parents=True)
    return root


def _key(link: dict) -> tuple:
    return (link["src"], link["dst"], link["type"], link["extractor"])


def judge(project, verdict, link=READS, *, create=True, **kw):
    kw.setdefault("via", "canvas")
    with lock.project_lock(project, PID) as held:
        return L.append_judgement(project, lock=held, verdict=verdict, create=create, **link, **kw)


def load(project) -> L.Ledger:
    got = L.load_judgements(project)
    assert got.state is files.RecordState.PRESENT, got.error
    return got.ledger


# -- format and round trip -----------------------------------------------------


def test_first_entry_founds_the_file_with_its_header(project):
    entry = judge(project, "rejected", note="这里读的是旧版面板", basis={"calls": ["read.csv"]})
    text = L.judgements_path(project).read_text()
    assert text.startswith("# 你对机器提取结果的判断。")
    assert "[[judgement]]" in text and "[judgement.basis]" in text
    assert entry.id.startswith("j-") and len(entry.id) == 34
    assert entry.seq == 1 and entry.get("verdict") == "rejected"
    data = tomllib.loads(text)["judgement"][0]
    assert list(data)[:5] == ["id", "seq", "at", "verdict", "src"]
    assert data["basis"] == {"calls": ["read.csv"]}
    assert datetime.fromisoformat(data["at"]).tzinfo is not None


HOSTILE = [
    'quote " and backslash \\ and \\" both',
    "C:\\path\\to\\file",
    "tab\there",
    "ctrl \x01\x02\x1b\x7f end",
    "中文：叙事更替（TopicShift）",
    "emoji 🧪 and combining é",
    "'''triple single'''",
    '"""triple double"""',
    "# not a comment",
    "] [[judgement]] [",
    "",
    " leading and trailing ",
]


def test_hostile_strings_round_trip_exactly(project):
    for i, s in enumerate(HOSTILE):
        link = {**READS, "dst": f"dataset:{s or 'x'}#{i}"}
        judge(project, "confirmed", link, note=s or None,
              basis={"calls": [s, "read.csv"], "sentence": s, "n": i, "x": 0.873, s or "k": "v"})
    ledger = load(project)
    assert len(ledger.entries) == len(HOSTILE)
    for i, (s, e) in enumerate(zip(HOSTILE, ledger.entries)):
        assert e.get("dst") == f"dataset:{s or 'x'}#{i}"
        assert e.get("note") == (s or None)
        assert e.get("basis") == {"calls": [s, "read.csv"], "sentence": s, "n": i, "x": 0.873, s or "k": "v"}


def test_nested_basis_tables_and_numbers(project):
    basis = {"sentence": "AUC 为 0.87", "number": "0.87", "metrics": {"auc": 0.87, "runs": 3, "names": ["auc"]}}
    judge(project, "confirmed", CLAIM, basis=basis, basis_recorded="at-judgement")
    e = load(project).entries[0]
    assert e.get("basis") == basis and e.get("basis_recorded") == "at-judgement"


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\u2028b", "a\u2029b", "a\x85b", "a\x0bb", "a\x1cb"])
def test_line_separators_are_refused(project, bad):
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "confirmed", note=bad)
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "confirmed", basis={"calls": [bad]})
    assert not L.judgements_path(project).exists()


@pytest.mark.parametrize("basis", [{"x": True}, {"x": float("nan")}, {"x": [1, 2]}, {"x": None}, {"a": {"b": {"c": {"d": 1}}}}])
def test_basis_values_outside_the_schema_are_refused(project, basis):
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "confirmed", basis=basis)


def test_mapping_links_are_refused_on_write_and_on_read(project):
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "confirmed", {**READS, "extractor": "mapping"})
    L.judgements_path(project).write_text(
        '[[judgement]]\nid = "h1"\nverdict = "confirmed"\nsrc = "a"\ndst = "b"\ntype = "reads"\nextractor = "mapping"\n'
    )
    got = L.load_judgements(project)
    assert got.state is files.RecordState.INVALID and got.line == 1


def test_bad_verdict_and_via_are_refused(project):
    for kw in ({"verdict": "pending"}, {"verdict": "auto"}):
        with pytest.raises(L.LedgerWriteRefused):
            judge(project, kw["verdict"])
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "confirmed", via="web")
    with pytest.raises(L.LedgerWriteRefused):
        with lock.project_lock(project, PID) as held:
            L.append(L.judgements_path(project), L.JUDGEMENT_SCHEMA, {"verdict": "confirmed", "seq": 9, **READS, "via": "cli"},
                     lock=held, project_root=project, create=True)


def test_existing_text_comments_and_unknown_keys_are_never_reemitted(project):
    path = L.judgements_path(project)
    original = (
        "# 我自己的注释\n"
        "[[judgement]]   # hand-written\n"
        "id = 'hand-1'\n"
        "verdict = 'confirmed'\n"
        "src = 'script:x.py'\ndst = 'dataset:y.csv'\ntype = 'reads'\nextractor = 'dataflow'\n"
        "my_own_key = [1, 2]\n"
        "at = 2026-10-01T10:00:00+02:00"  # no trailing newline, unquoted datetime
    ).encode()
    path.write_bytes(original)
    judge(project, "rejected", create=False)
    data = path.read_bytes()
    assert data.startswith(original)
    ledger = load(project)
    assert [e.id for e in ledger.entries][0] == "hand-1"
    assert ledger.entries[0].get("my_own_key") == [1, 2]
    assert ledger.entries[1].seq == 1  # hand entry has no seq: numbering starts at 1
    assert ledger.anomalies == ()


# -- order, undo, withdraw ---------------------------------------------------------


def test_history_scenario_12(project):
    """9.9 scenario 12: confirm; reject; undo -> confirmed; confirm again
    with a note; withdraw -> the machine's. Five entries in order."""
    k = _key(READS)
    judge(project, "confirmed")
    judge(project, "rejected")
    assert L.judgement_status(load(project).state(k)) == "rejected"
    undo = judge(project, "undone")
    ledger = load(project)
    assert L.judgement_status(ledger.state(k)) == "confirmed"
    assert undo.get("undoes") == ledger.entries[1].id
    judge(project, "confirmed", note="复核过")
    judge(project, "withdrawn")
    ledger = load(project)
    state = ledger.state(k)
    assert L.judgement_status(state) is None and state.entry.get("verdict") == "withdrawn"
    assert [e.get("verdict") for e in state.history] == ["confirmed", "rejected", "undone", "confirmed", "withdrawn"]
    assert [e.seq for e in state.history] == [1, 2, 3, 4, 5]


def test_clock_set_back_between_two_acts_does_not_reorder(project):
    """9.9 scenario 12: set the system clock back an hour between two acts;
    the later act still wins."""
    t0 = datetime(2026, 10, 4, 21, 15, tzinfo=TZ)
    judge(project, "confirmed", now=lambda: t0)
    judge(project, "withdrawn", now=lambda: t0 - timedelta(hours=1))
    ledger = load(project)
    state = ledger.state(_key(READS))
    assert state.entry.get("verdict") == "withdrawn"
    assert L.judgement_status(state) is None
    assert ledger.entries[1].at < ledger.entries[0].at  # the clock went back; order did not


def test_undo_takes_back_only_the_last_act(project):
    first = judge(project, "confirmed")
    judge(project, "rejected")
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "undone", undoes=first.id)
    judge(project, "undone")
    judge(project, "undone")  # now undoes the confirmation: nothing stands
    state = load(project).state(_key(READS))
    assert state.entry is None and L.judgement_status(state) is None
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "undone")  # nothing left to undo


def test_undo_of_a_withdrawal_restores_the_verdict(project):
    judge(project, "rejected")
    judge(project, "withdrawn")
    judge(project, "undone")
    assert L.judgement_status(load(project).state(_key(READS))) == "rejected"


def test_keys_are_independent(project):
    judge(project, "confirmed", READS)
    judge(project, "rejected", WRITES)
    judge(project, "undone", READS)
    ledger = load(project)
    assert L.judgement_status(ledger.state(_key(READS))) is None
    assert L.judgement_status(ledger.state(_key(WRITES))) == "rejected"


# -- validation of hand-edited files --------------------------------------------------


def _entry(id_, verdict="confirmed", seq=None, link=READS, **extra):
    lines = ["[[judgement]]", f'id = "{id_}"']
    if seq is not None:
        lines.append(f"seq = {seq}")
    lines.append(f'verdict = "{verdict}"')
    for k, v in {**link, **extra}.items():
        lines.append(f'{k} = "{v}"')
    return "\n".join(lines) + "\n"


def test_an_invalid_entry_makes_the_whole_file_unreadable_naming_its_line(project):
    path = L.judgements_path(project)
    path.write_text("# c\n" + _entry("a", seq=1) + "\n" + _entry("b", verdict="maybe", seq=2))
    got = L.load_judgements(project)
    assert got.state is files.RecordState.INVALID and got.ledger is None
    assert got.line == 11 and "line 11" in got.error


def test_unparseable_toml_names_its_line(project):
    L.judgements_path(project).write_text(_entry("a", seq=1) + 'note = "unterminated\n')
    got = L.load_judgements(project)
    assert got.state is files.RecordState.INVALID and got.line == 9


def test_duplicate_ids(project):
    path = L.judgements_path(project)
    path.write_text(_entry("a", seq=1) + _entry("a", seq=1))
    assert len(load(project).entries) == 1  # identical: a merge duplicated it
    path.write_text(_entry("a", seq=1) + _entry("a", verdict="rejected", seq=2))
    got = L.load_judgements(project)
    assert got.state is files.RecordState.INVALID and got.line == 9


def test_undo_must_name_an_earlier_entry_of_the_same_link(project):
    path = L.judgements_path(project)
    path.write_text(_entry("u", verdict="undone", seq=1, undoes="a") + _entry("a", seq=2))
    assert L.load_judgements(project).state is files.RecordState.INVALID
    path.write_text(_entry("a", seq=1, link=WRITES) + _entry("u", verdict="undone", seq=2, undoes="a"))
    assert L.load_judgements(project).state is files.RecordState.INVALID
    path.write_text(_entry("a", seq=1) + _entry("u", verdict="undone", seq=2, undoes="a")
                    + _entry("v", verdict="undone", seq=3, undoes="u"))
    assert L.load_judgements(project).state is files.RecordState.INVALID
    path.write_text(_entry("a", seq=1, undoes="x"))
    assert L.load_judgements(project).state is files.RecordState.INVALID


def test_hand_written_entries_without_seq_take_their_place_by_position(project):
    path = L.judgements_path(project)
    path.write_text(_entry("a", seq=1) + _entry("h", verdict="rejected") + _entry("b", seq=2, link=WRITES))
    ledger = load(project)
    assert ledger.anomalies == ()
    assert L.judgement_status(ledger.state(_key(READS))) == "rejected"


# -- two histories (scenario 12, second half) ------------------------------------------


def _diverged(project) -> tuple[Path, list[str]]:
    """Two copies that each appended after the same entry, then merged
    (concatenated, as a sync service or `cat` would)."""
    base = judge(project, "confirmed", READS)
    path = L.judgements_path(project)
    common = path.read_bytes()
    a1 = judge(project, "rejected", READS)
    a2 = judge(project, "confirmed", WRITES)
    copy_a = path.read_bytes()
    path.write_bytes(common)
    b1 = judge(project, "withdrawn", READS)
    b2 = judge(project, "rejected", CLAIM)
    copy_b = path.read_bytes()
    path.write_bytes(copy_a + copy_b[len(common):])
    return path, [base.id, a1.id, a2.id, b1.id, b2.id]


def test_merged_copies_are_a_conflict_and_nothing_is_decided(project):
    _path, ids = _diverged(project)
    ledger = load(project)
    assert [a.entry.id for a in ledger.anomalies] == [ids[3]]
    conflicts = ledger.conflicts()
    assert set(conflicts) == {_key(READS), _key(WRITES), _key(CLAIM)}
    for key in conflicts:
        state = ledger.state(key)
        assert state.entry is None and L.judgement_status(state) == "conflict"
    c = conflicts[_key(READS)]
    assert [e.id for e in c.common] == [ids[0]]
    assert [[e.id for e in b] for b in c.branches] == [[ids[1]], [ids[3]]]


def test_a_new_entry_settles_its_link_only(project):
    _path, ids = _diverged(project)
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "undone", READS, create=False)
    settle = judge(project, "confirmed", READS, create=False, note="两边都看过")
    assert settle.settles == (ids[3],)
    ledger = load(project)
    assert L.judgement_status(ledger.state(_key(READS))) == "confirmed"
    assert L.judgement_status(ledger.state(_key(WRITES))) == "conflict"
    assert L.judgement_status(ledger.state(_key(CLAIM))) == "conflict"
    # a later, ordinary act on the settled link is just an act
    judge(project, "rejected", READS, create=False)
    assert L.judgement_status(load(project).state(_key(READS))) == "rejected"


def test_a_link_first_judged_after_the_merge_is_not_in_conflict(project):
    _diverged(project)
    other = dict(src="script:z.py", dst="dataset:z.csv", type="reads", extractor="dataflow")
    judge(project, "confirmed", other, create=False)
    assert L.judgement_status(load(project).state(_key(other))) == "confirmed"


def test_undoing_the_settling_entry_reopens_the_conflict(project):
    _diverged(project)
    judge(project, "confirmed", READS, create=False)
    judge(project, "undone", READS, create=False)
    assert L.judgement_status(load(project).state(_key(READS))) == "conflict"


def test_branch_entries_with_higher_numbers_do_not_settle(project):
    """The second copy's own later entries come after the anomaly with ever
    higher seq; position alone must not make them a settlement."""
    judge(project, "confirmed", READS)
    path = L.judgements_path(project)
    common = path.read_bytes()
    judge(project, "rejected", READS)
    copy_a = path.read_bytes()
    path.write_bytes(common)
    for verdict in ("withdrawn", "confirmed", "rejected"):
        judge(project, verdict, READS, create=False)
    copy_b = path.read_bytes()
    path.write_bytes(copy_a + copy_b[len(common):])
    assert L.judgement_status(load(project).state(_key(READS))) == "conflict"


# -- refusals -------------------------------------------------------------------------


def test_append_requires_the_lock(project):
    with lock.project_lock(project, PID) as held:
        pass
    with pytest.raises(lock.ProjectLockError):
        L.append_judgement(project, lock=held, verdict="confirmed", via="cli", create=True, **READS)
    assert not L.judgements_path(project).exists()


def test_append_never_founds_a_file_unless_told(project):
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "confirmed", create=False)
    assert not L.judgements_path(project).exists()


def test_append_refuses_unreadable_unparseable_dataless(project, monkeypatch):
    path = L.judgements_path(project)
    path.write_text("[[judgement]\n")
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "confirmed")
    assert path.read_text() == "[[judgement]\n"

    path.write_bytes(b"\xff\xfe")
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "confirmed")
    assert path.read_bytes() == b"\xff\xfe"

    path.write_text(_entry("a", seq=1))
    monkeypatch.setattr(paths, "is_dataless", lambda p: True)
    monkeypatch.setattr(paths, "_request_download", lambda p: None)
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "confirmed")
    monkeypatch.undo()
    assert path.read_text() == _entry("a", seq=1)


def test_append_refuses_beside_a_conflict_copy(project):
    judge(project, "confirmed")
    (project / ".rce" / "judgements 2.toml").write_text("")
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "rejected", create=False)
    assert len(load(project).entries) == 1


def test_an_append_that_would_not_read_back_writes_nothing(project):
    path = L.judgements_path(project)
    original = "judgement = []\n"  # an inline array: [[judgement]] cannot follow it
    path.write_text(original)
    with pytest.raises(L.LedgerWriteRefused):
        judge(project, "confirmed", create=False)
    assert path.read_text() == original


def test_snapshot_before_entry_once_a_day(project):
    day = datetime(2026, 10, 4, 9, tzinfo=TZ)
    judge(project, "confirmed", now=lambda: day)  # founding: nothing to snapshot
    backups = project / ".rce" / "backups"
    assert not backups.exists()
    first_bytes = L.judgements_path(project).read_bytes()
    for i in range(10):
        judge(project, "rejected" if i % 2 == 0 else "confirmed", create=False, now=lambda: day + timedelta(minutes=i))
    snaps = list(backups.iterdir())
    assert len(snaps) == 1 and snaps[0].read_bytes() == first_bytes


# -- two writers (scenario 10) ---------------------------------------------------------

_WRITER = """
import sys
from rce.records import lock, ledger
project, tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
for i in range(n):
    with lock.project_lock(project, {pid!r}) as held:
        ledger.append_judgement(project, lock=held, verdict="confirmed", via="cli", create=True,
                                src=f"script:{{tag}}.py", dst=f"dataset:{{i}}.csv", type="reads", extractor="dataflow")
"""


def test_two_processes_append_300_each(project):
    """9.9 scenario 10 (the judgment half): two processes each make 300
    judgments at once; every one is in the record and nothing raises."""
    code = _WRITER.format(pid=PID)
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(project), tag, "300"], env=os.environ.copy(), stderr=subprocess.PIPE)
        for tag in ("a", "b")
    ]
    for p in procs:
        _out, err = p.communicate(timeout=300)
        assert p.returncode == 0, err.decode()
    ledger = load(project)
    assert len(ledger.entries) == 600
    seqs = [e.seq for e in ledger.entries]
    assert seqs == list(range(1, 601))
    assert ledger.anomalies == ()
    assert {e.get("src") for e in ledger.entries} == {"script:a.py", "script:b.py"}
    assert len({e.id for e in ledger.entries}) == 600


def test_line_numbers_count_toml_lines_not_unicode_separators(project):
    """A raw U+2028 inside a hand-written string is legal TOML and is not a
    line break; the line named for a later bad entry must not shift."""
    path = L.judgements_path(project)
    path.write_text(_entry("a", seq=1, note="x y") + _entry("b", verdict="maybe", seq=2))
    got = L.load_judgements(project)
    assert got.state is files.RecordState.INVALID and got.line == 10
