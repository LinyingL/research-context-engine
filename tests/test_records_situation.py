"""DESIGN.md 9.4: the situation table (`rce.records.situation.classify`)
and the write-time re-check (`write_guard`). Every branch, with the old
home's volume and readability probes injected -- "gone is never inferred
from failing to look" is tested by making each look fail."""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from rce import db, paths
from rce.records import identity, situation
from rce.records.identity import IdentityRead, IdentityState
from rce.records.situation import Probes, Situation


def _project(root: Path) -> identity.ProjectIdentity:
    """A V5 project: identity file, index, home.json naming `root`."""
    root.mkdir(parents=True, exist_ok=True)
    ident = identity.create_identity(root)
    situation.write_home(ident.id, root)
    conn = db.connect(situation.index_db_path(ident.id))
    db.migrate(conn)
    conn.close()
    return ident


def test_not_a_project(tmp_path: Path) -> None:
    got = situation.classify(tmp_path)
    assert got.situation is Situation.NOT_A_PROJECT
    assert not got.blocked and got.project_id is None


def test_missing_folder_raises(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        situation.classify(tmp_path / "nope")


def test_normal(tmp_path: Path) -> None:
    ident = _project(tmp_path / "p")
    got = situation.classify(tmp_path / "p")
    assert got.situation is Situation.NORMAL and got.project_id == ident.id and not got.respelled


def test_rename_in_place_is_normal_and_respelled(tmp_path: Path) -> None:
    """9.9 scenario 2: a rename keeps the inode -- not a move, no copy
    question; only the spelling is rewritten by the entry point."""
    _project(tmp_path / "p")
    os.rename(tmp_path / "p", tmp_path / "renamed")
    got = situation.classify(tmp_path / "renamed")
    assert got.situation is Situation.NORMAL and got.respelled


def test_no_index(tmp_path: Path) -> None:
    ident = _project(tmp_path / "p")
    situation.index_db_path(ident.id).unlink()
    got = situation.classify(tmp_path / "p")
    assert got.situation is Situation.NO_INDEX


def test_index_without_home_record_cannot_be_checked(tmp_path: Path) -> None:
    ident = _project(tmp_path / "p")
    situation.home_path(ident.id).unlink()
    got = situation.classify(tmp_path / "p")
    assert got.situation is Situation.CANNOT_CHECK and got.reason == "no_home_record"
    assert "readonly" in got.answers


def test_copy_is_blocked_with_three_answers(tmp_path: Path) -> None:
    _project(tmp_path / "p")
    shutil.copytree(tmp_path / "p", tmp_path / "q")
    got = situation.classify(tmp_path / "q")
    assert got.situation is Situation.COPY and got.blocked
    assert got.answers == ("fork", "claim", "other")
    assert got.payload()["home"] == paths._canonical_path(tmp_path / "p")


def test_moved_when_old_home_confirmed_gone(tmp_path: Path) -> None:
    ident = _project(tmp_path / "a" / "p")
    (tmp_path / "b").mkdir()
    shutil.copytree(tmp_path / "a" / "p", tmp_path / "b" / "p")  # new inode, like a cross-volume move
    shutil.rmtree(tmp_path / "a" / "p")
    got = situation.classify(tmp_path / "b" / "p")
    assert got.situation is Situation.MOVED and got.reason == "old_home_gone" and got.project_id == ident.id


def test_moved_when_whole_parent_is_gone_but_an_ancestor_is_readable(tmp_path: Path) -> None:
    _project(tmp_path / "a" / "deep" / "p")
    shutil.copytree(tmp_path / "a" / "deep" / "p", tmp_path / "p2")
    shutil.rmtree(tmp_path / "a")
    assert situation.classify(tmp_path / "p2").situation is Situation.MOVED


def test_moved_when_old_home_carries_another_id(tmp_path: Path) -> None:
    _project(tmp_path / "p")
    shutil.copytree(tmp_path / "p", tmp_path / "q")
    shutil.rmtree(tmp_path / "p")
    other = _project(tmp_path / "p")  # an unrelated project now at the old path
    got = situation.classify(tmp_path / "q")
    assert got.situation is Situation.MOVED and got.other_id == other.id


def test_moved_when_old_home_holds_a_folder_without_identity(tmp_path: Path) -> None:
    _project(tmp_path / "p")
    shutil.copytree(tmp_path / "p", tmp_path / "q")
    shutil.rmtree(tmp_path / "p")
    (tmp_path / "p").mkdir()
    got = situation.classify(tmp_path / "q")
    assert got.situation is Situation.MOVED and got.reason == "old_home_has_no_id"


def test_unmounted_volume_cannot_be_checked(tmp_path: Path) -> None:
    """9.9 scenario 3, last sentence: the original on a volume that is not
    mounted gives the third answer, never adoption."""
    _project(tmp_path / "p")
    shutil.copytree(tmp_path / "p", tmp_path / "q")
    shutil.rmtree(tmp_path / "p")
    probes = Probes(volume_mounted=lambda _p: False)
    got = situation.classify(tmp_path / "q", probes=probes)
    assert got.situation is Situation.CANNOT_CHECK and got.reason == "volume_unmounted"
    assert got.answers == ("fork", "claim", "other", "readonly")


def test_unreadable_parent_cannot_be_checked(tmp_path: Path) -> None:
    _project(tmp_path / "p")
    shutil.copytree(tmp_path / "p", tmp_path / "q")
    shutil.rmtree(tmp_path / "p")

    def listdir(path):
        raise PermissionError(13, "denied", str(path))

    got = situation.classify(tmp_path / "q", probes=Probes(listdir=listdir))
    assert got.situation is Situation.CANNOT_CHECK and got.reason == "old_home_parent_unreadable"


def test_stat_error_on_old_home_cannot_be_checked(tmp_path: Path) -> None:
    _project(tmp_path / "p")
    shutil.copytree(tmp_path / "p", tmp_path / "q")
    old = paths._canonical_path(tmp_path / "p")

    def stat(path):
        if str(path) == old:
            raise PermissionError(13, "denied", str(path))
        return os.stat(path)

    got = situation.classify(tmp_path / "q", probes=Probes(stat=stat))
    assert got.situation is Situation.CANNOT_CHECK and got.reason == "old_home_unreadable"


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (IdentityState.DATALESS, "old_home_identity_in_cloud"),
        (IdentityState.UNREADABLE, "old_home_identity_unreadable"),
        (IdentityState.CONFLICT_COPY, "old_home_identity_conflict"),
    ],
)
def test_old_home_identity_unreadable_cannot_be_checked(tmp_path: Path, state, reason) -> None:
    _project(tmp_path / "p")
    shutil.copytree(tmp_path / "p", tmp_path / "q")
    old = paths._canonical_path(tmp_path / "p")

    def read(path):
        if str(path) == old:
            return IdentityRead(state, Path(path) / ".rce" / "project.toml", error="x")
        return identity.read_identity(path)

    got = situation.classify(tmp_path / "q", probes=Probes(read_identity=read))
    assert got.situation is Situation.CANNOT_CHECK and got.reason == reason


def test_absent_identity_in_unlistable_old_home_cannot_be_checked(tmp_path: Path) -> None:
    _project(tmp_path / "p")
    shutil.copytree(tmp_path / "p", tmp_path / "q")
    old = paths._canonical_path(tmp_path / "p")

    def read(path):
        if str(path) == old:
            return IdentityRead(IdentityState.ABSENT, Path(path) / ".rce" / "project.toml")
        return identity.read_identity(path)

    def listdir(path):
        if str(path) == old:
            raise PermissionError(13, "denied", str(path))
        return os.listdir(path)

    got = situation.classify(tmp_path / "q", probes=Probes(read_identity=read, listdir=listdir))
    assert got.situation is Situation.CANNOT_CHECK


def test_lost_id_when_v5_records_but_no_identity(tmp_path: Path) -> None:
    (tmp_path / ".rce").mkdir()
    (tmp_path / ".rce" / "judgements.toml").write_text("")
    got = situation.classify(tmp_path)
    # 9.12: adopt and other; restore only when a snapshot of project.toml exists.
    assert got.situation is Situation.LOST_ID and got.blocked and got.answers == ("adopt", "other")
    assert got.payload()["records"] == ["judgements.toml"] and got.payload()["snapshot"] is None


def test_attempts_and_mappings_alone_are_not_a_lost_id(tmp_path: Path) -> None:
    (tmp_path / ".rce").mkdir()
    (tmp_path / ".rce" / "attempts.toml").write_text("")
    (tmp_path / ".rce" / "mappings.toml").write_text("")
    assert situation.classify(tmp_path).situation is Situation.NOT_A_PROJECT


@pytest.mark.parametrize("how", ["unparseable", "conflict_copy"])
def test_unreadable_identity(tmp_path: Path, how: str) -> None:
    ident = _project(tmp_path / "p")
    rce = tmp_path / "p" / ".rce"
    if how == "unparseable":
        (rce / "project.toml").write_text("id = \n")
    else:
        shutil.copy(rce / "project.toml", rce / "project 2.toml")
    got = situation.classify(tmp_path / "p")
    assert got.situation is Situation.UNREADABLE_ID and got.blocked and got.answers == ()
    assert got.message == "项目身份文件无法读取"
    assert ident.id  # the index is untouched either way
    assert situation.index_db_path(ident.id).exists()


def test_legacy_by_path_hash_index(tmp_path: Path) -> None:
    paths.legacy_graph_dir(tmp_path).mkdir(parents=True)
    paths.legacy_index_db_path(tmp_path).write_bytes(b"")
    got = situation.classify(tmp_path)
    assert got.situation is Situation.LEGACY and got.needs_migration and not got.blocked


def test_legacy_by_in_project_graph(tmp_path: Path) -> None:
    (tmp_path / ".rce").mkdir()
    (tmp_path / ".rce" / "graph.db").write_bytes(b"")
    assert situation.classify(tmp_path).situation is Situation.LEGACY


def test_migrating_identity_needs_migration(tmp_path: Path) -> None:
    ident = _project(tmp_path / "p")
    identity.set_flag(tmp_path / "p", ident, "migrating_from", "abc")
    got = situation.classify(tmp_path / "p")
    assert got.situation is Situation.NORMAL and got.needs_migration


# -- write_guard / check_still_home ----------------------------------------------


def test_write_guard_passes_for_the_home(tmp_path: Path) -> None:
    ident = _project(tmp_path / "p")
    with situation.write_guard(tmp_path / "p", ident.id, human=True) as held:
        assert held.is_held()


def test_write_guard_refuses_a_moved_folder_and_creates_nothing(tmp_path: Path) -> None:
    """9.9 scenario 1 (while an engine serves it): the folder moved away;
    the write is refused and the old path is not re-created."""
    ident = _project(tmp_path / "p")
    os.rename(tmp_path / "p", tmp_path / "moved")
    with pytest.raises(situation.ProjectMovedError):
        with situation.write_guard(tmp_path / "p", ident.id, human=True):
            pytest.fail("must not get here")
    assert not (tmp_path / "p").exists()


def test_write_guard_refuses_after_a_claim_elsewhere(tmp_path: Path) -> None:
    ident = _project(tmp_path / "p")
    shutil.copytree(tmp_path / "p", tmp_path / "q")
    situation.write_home(ident.id, tmp_path / "q")  # q became the home
    with pytest.raises(situation.ProjectMovedError):
        with situation.write_guard(tmp_path / "p", ident.id):
            pass


def test_write_guard_refuses_another_id(tmp_path: Path) -> None:
    ident = _project(tmp_path / "p")
    other = identity.new_project_id()
    with pytest.raises(situation.ProjectMovedError):
        with situation.write_guard(tmp_path / "p", other):
            pass
    assert ident.id != other


def test_write_guard_refuses_every_write_to_a_legacy_project(tmp_path: Path) -> None:
    """9.12 (acceptance, 2026-10-05): a pre-V5 project is frozen until it is
    migrated, scans included -- an index write is refused like a human one.
    A never-indexed folder (no old store to protect) is not."""
    paths.legacy_graph_dir(tmp_path).mkdir(parents=True)
    paths.legacy_index_db_path(tmp_path).write_bytes(b"")
    for human in (True, False):
        with pytest.raises(situation.NeedsMigrationError, match="migrate first") as err:
            with situation.write_guard(tmp_path, human=human):
                pass
        assert err.value.state == "needs_migration"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    with situation.write_guard(fresh, human=False):
        pass


def test_write_guard_human_refuses_unfinished_migration(tmp_path: Path) -> None:
    ident = _project(tmp_path / "p")
    ident = identity.set_flag(tmp_path / "p", ident, "migrating_from", "abc")
    with pytest.raises(situation.NeedsMigrationError):
        with situation.write_guard(tmp_path / "p", ident.id, human=True):
            pass


def test_read_home_rejects_garbage(tmp_path: Path) -> None:
    ident = _project(tmp_path / "p")
    situation.home_path(ident.id).write_text("[1, 2]")
    assert situation.read_home(ident.id) is None
    home = situation.write_home(ident.id, tmp_path / "p")
    assert situation.read_home(ident.id) == replace(home)


def test_index_dir_refuses_a_non_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        paths.index_dir("../escape")


def test_write_guard_lets_a_never_indexed_folder_write_its_own_files(tmp_path: Path) -> None:
    """No id and no pre-V5 index: nothing to migrate, nothing to protect."""
    with situation.write_guard(tmp_path, human=True):
        pass
