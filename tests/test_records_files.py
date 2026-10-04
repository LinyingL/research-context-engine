"""Tests for rce.records.files (DESIGN.md sections 9.2, 9.3, 9.7): the
durable write, the append-only write, daily snapshots, the four-way read
and conflict-copy detection."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rce import paths
from rce.records import files
from rce.records.files import RecordState


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rce").mkdir(parents=True)
    return root


# -- durable_write -------------------------------------------------------------


def test_durable_write_replaces_and_leaves_no_temp(project):
    target = project / ".rce" / "x.toml"
    files.durable_write(target, b"one")
    files.durable_write(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in target.parent.iterdir()] == ["x.toml"]


def test_temp_names_are_unique_per_write(project):
    target = project / ".rce" / "x.toml"
    names = {files.temp_path_for(target).name for _ in range(50)}
    assert len(names) == 50
    assert all(str(os.getpid()) in n for n in names)


def test_durable_write_never_recreates_a_vanished_folder(tmp_path):
    with pytest.raises(files.RecordFileError):
        files.durable_write(tmp_path / "gone" / ".rce" / "x.toml", b"x")
    assert not (tmp_path / "gone").exists()


def test_failure_before_replace_keeps_the_old_file(project, monkeypatch):
    target = project / ".rce" / "x.toml"
    target.write_bytes(b"old")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(files.os, "replace", boom)
    with pytest.raises(OSError):
        files.durable_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert [p.name for p in target.parent.iterdir()] == ["x.toml"]


_KILL_CHILD = """
import os, sys
from pathlib import Path
from rce.records import files
def die(*args):
    os._exit(137)          # the process is killed between tmp write and replace
files.os.replace = die
files.append_bytes(Path(sys.argv[1]), b"[[judgement]]\\nid = 'new'\\n")
"""


def test_kill_between_tmp_write_and_replace_keeps_the_old_file(project):
    """Fault injection: a real process dies after its temp file is fully
    written and synced but before the rename. The record is intact."""
    target = project / ".rce" / "judgements.toml"
    original = b"# header\n[[judgement]]\nid = 'a'\n"
    target.write_bytes(original)
    proc = subprocess.run([sys.executable, "-c", _KILL_CHILD, str(target)], env=os.environ.copy())
    assert proc.returncode == 137
    assert target.read_bytes() == original
    leftovers = [p.name for p in target.parent.iterdir() if p.name != "judgements.toml"]
    assert len(leftovers) == 1 and leftovers[0].endswith(".rce-tmp")
    assert files.conflict_copies(target) == []  # a leftover temp is not a conflict copy


# -- append_bytes ------------------------------------------------------------


def test_append_keeps_existing_bytes_exactly(project):
    target = project / ".rce" / "j.toml"
    old = "# 注释 kept\nunknown = 1\r\n[[judgement]]\nid = 'a'  # trailing\n".encode()
    target.write_bytes(old)
    result = files.append_bytes(target, b"[[judgement]]\nid = 'b'\n")
    assert result.startswith(old)
    assert target.read_bytes() == old + b"[[judgement]]\nid = 'b'\n"


def test_append_supplies_a_missing_trailing_newline(project):
    target = project / ".rce" / "j.toml"
    target.write_bytes(b"x = 1")
    files.append_bytes(target, b"y = 2\n")
    assert target.read_bytes() == b"x = 1\ny = 2\n"


def test_append_refuses_a_missing_file_unless_create(project):
    target = project / ".rce" / "j.toml"
    with pytest.raises(files.RecordFileError):
        files.append_bytes(target, b"x = 1\n")
    assert not target.exists()
    files.append_bytes(target, b"x = 1\n", create=True)
    assert target.read_bytes() == b"x = 1\n"


def test_append_refuses_when_the_file_changed_since_it_was_read(project):
    target = project / ".rce" / "j.toml"
    target.write_bytes(b"a = 1\n")
    with pytest.raises(files.RecordFileError):
        files.append_bytes(target, b"b = 2\n", expected_old=b"something else\n")
    assert target.read_bytes() == b"a = 1\n"


def test_append_refuses_a_dataless_file(project, monkeypatch):
    target = project / ".rce" / "j.toml"
    target.write_bytes(b"a = 1\n")
    monkeypatch.setattr(paths, "is_dataless", lambda p: True)
    monkeypatch.setattr(paths, "_request_download", lambda p: None)
    with pytest.raises(files.RecordFileError):
        files.append_bytes(target, b"b = 2\n")
    assert target.read_bytes() == b"a = 1\n"


# -- read_record ---------------------------------------------------------------


def test_read_record_four_answers(project, monkeypatch):
    target = project / ".rce" / "j.toml"
    assert files.read_record(target).state is RecordState.ABSENT

    target.write_bytes("é = 1\n".encode())
    got = files.read_record(target)
    assert got.state is RecordState.PRESENT and got.text == "é = 1\n"

    target.write_bytes(b"\xff\xfe bad")
    assert files.read_record(target).state is RecordState.UNREADABLE

    requested = []
    monkeypatch.setattr(paths, "is_dataless", lambda p: True)
    monkeypatch.setattr(paths, "_request_download", lambda p: requested.append(p))
    assert files.read_record(target).state is RecordState.DATALESS
    assert requested == [target]


def test_read_record_zero_bytes_is_present_and_empty(project):
    target = project / ".rce" / "j.toml"
    target.write_bytes(b"")
    got = files.read_record(target)
    assert got.state is RecordState.PRESENT and got.data == b"" and got.text == ""


def test_icloud_placeholder_counts_as_dataless(project, monkeypatch):
    monkeypatch.setattr(paths, "_request_download", lambda p: None)
    (project / ".rce" / ".j.toml.icloud").write_bytes(b"")
    assert files.read_record(project / ".rce" / "j.toml").state is RecordState.DATALESS


def test_bom_is_dropped_from_text_only(project):
    target = project / ".rce" / "j.toml"
    target.write_bytes(b"\xef\xbb\xbfa = 1\n")
    got = files.read_record(target)
    assert got.text == "a = 1\n" and got.data.startswith(b"\xef\xbb\xbf")


# -- conflict copies ------------------------------------------------------------


def test_conflict_copies_are_reported_never_touched(project):
    rce = project / ".rce"
    target = rce / "judgements.toml"
    target.write_bytes(b"")
    names = [
        "judgements 2.toml",
        "judgements (1).toml",
        "judgements (Linying's conflicted copy 2026-10-05).toml",
        "Judgements 3.toml",
    ]
    for n in names:
        (rce / n).write_bytes(b"x")
    for n in ["judgements.toml.bak", "judgements2.toml", "mappings 2.toml", "judgements 2.txt"]:
        (rce / n).write_bytes(b"x")
    found = sorted(p.name for p in files.conflict_copies(target))
    assert found == sorted(names)
    for n in names:
        assert (rce / n).exists()


# -- snapshots ------------------------------------------------------------------

TZ = timezone(timedelta(hours=2))


def _clock(*moments):
    it = iter(moments)
    return lambda: next(it)


def test_one_snapshot_per_day_not_per_write(project):
    target = project / ".rce" / "judgements.toml"
    day1 = datetime(2026, 10, 4, 9, 0, tzinfo=TZ)
    for i in range(30):
        target.write_bytes(f"v{i}\n".encode())
        files.snapshot_if_first_change_today(project, target, now=lambda: day1 + timedelta(minutes=i))
    snaps = sorted((project / ".rce" / "backups").iterdir())
    assert len(snaps) == 1 and snaps[0].read_bytes() == b"v0\n"
    assert snaps[0].name.startswith("judgements.toml.") and snaps[0].name.endswith("Z.toml")

    # next day, a change -> one more; no change since the newest -> none
    day2 = day1 + timedelta(days=1)
    assert files.snapshot_if_first_change_today(project, target, now=lambda: day2) is not None
    target.write_bytes(b"v29\n")
    day3 = day1 + timedelta(days=2)
    assert files.snapshot_if_first_change_today(project, target, now=lambda: day3) is None
    assert len(list((project / ".rce" / "backups").iterdir())) == 2


def test_snapshots_keep_newest_twenty_per_file(project):
    target = project / ".rce" / "judgements.toml"
    other = project / ".rce" / "mappings.toml"
    other.write_bytes(b"m\n")
    files.snapshot_now(project, other, now=lambda: datetime(2026, 1, 1, tzinfo=TZ))
    start = datetime(2026, 1, 1, 12, tzinfo=TZ)
    for i in range(25):
        target.write_bytes(f"v{i}\n".encode())
        files.snapshot_if_first_change_today(project, target, now=lambda: start + timedelta(days=i))
    names = sorted(p.name for p in (project / ".rce" / "backups").iterdir())
    mine = [n for n in names if n.startswith("judgements.toml.")]
    assert len(mine) == 20
    assert any(n.startswith("mappings.toml.") for n in names)  # other files' budget untouched
    newest = files.newest_snapshot(project, target)
    assert newest.read_bytes() == b"v24\n"


def test_snapshot_now_ignores_the_day_rule_and_subdir_is_confined(project):
    target = project / ".rce" / "canvas.json"
    target.write_bytes(b"{}")
    moment = datetime(2026, 10, 4, 9, tzinfo=TZ)
    a = files.snapshot_now(project, target, now=lambda: moment)
    b = files.snapshot_now(project, target, now=lambda: moment)
    assert a != b and a.exists() and b.exists()
    v = files.snapshot_now(project, target, subdir="variables/topicshift", now=lambda: moment)
    assert v.parent == project / ".rce" / "backups" / "variables" / "topicshift"
    for bad in ("..", "../x", "/etc"):
        with pytest.raises(files.RecordFileError):
            files.snapshot_now(project, target, subdir=bad, now=lambda: moment)


def test_snapshot_refuses_a_file_outside_the_project(project, tmp_path):
    outside = tmp_path / "elsewhere.toml"
    outside.write_bytes(b"x")
    with pytest.raises(files.RecordFileError):
        files.snapshot_now(project, outside)


def test_no_snapshot_of_a_file_in_the_cloud(project, monkeypatch):
    target = project / ".rce" / "judgements.toml"
    target.write_bytes(b"x")
    monkeypatch.setattr(paths, "is_dataless", lambda p: True)
    monkeypatch.setattr(paths, "_request_download", lambda p: None)
    assert files.snapshot_if_first_change_today(project, target) is None
    assert files.snapshot_now(project, target) is None


def test_ensure_dir_within_creates_below_the_root_only(tmp_path):
    """DESIGN.md 9.4: record writers create `.rce/` only inside a folder
    that exists; they never re-create a folder that has gone."""
    root = tmp_path / "p"
    root.mkdir()
    target = root / ".rce" / "backups" / "variables" / "x"
    assert files.ensure_dir_within(root, target) == target and target.is_dir()
    gone = tmp_path / "gone"
    with pytest.raises(files.RecordFileError):
        files.ensure_dir_within(gone, gone / ".rce" / "backups")
    assert not gone.exists()
    with pytest.raises(files.RecordFileError):
        files.ensure_dir_within(root, tmp_path / "elsewhere")


def test_a_snapshot_of_a_moved_project_recreates_nothing(tmp_path):
    root = tmp_path / "p"
    (root / ".rce").mkdir(parents=True)
    record = root / ".rce" / "j.toml"
    record.write_text("x")
    (tmp_path / "moved").mkdir()
    os.rename(root, tmp_path / "moved" / "p")
    assert files.snapshot_now(root, record) is None  # unreadable: nothing to copy
    assert not root.exists()
