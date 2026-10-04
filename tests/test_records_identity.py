"""Tests for rce.records.identity (DESIGN.md section 9.4, the identity file
only): created exclusively, read with four distinguishable outcomes,
rewritten only by compare-and-swap."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from rce import paths
from rce.records import identity as ident
from rce.records.identity import IdentityState


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


def test_create_then_read(project):
    created = ident.create_identity(project, today=date(2026, 10, 4))
    assert ident.PROJECT_ID_RE.match(created.id)
    assert created.created == "2026-10-04" and created.ledger is False
    got = ident.read_identity(project)
    assert got.state is IdentityState.PRESENT and got.identity == created
    text = ident.identity_path(project).read_text()
    assert text.startswith("#") and 'id      = "p-' in text and "ledger  = false" in text


def test_ids_are_random(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert ident.create_identity(project).id != ident.create_identity(other).id


def test_create_is_exclusive(project):
    first = ident.create_identity(project)
    with pytest.raises(ident.IdentityExistsError):
        ident.create_identity(project)
    assert ident.read_identity(project).identity == first


def test_create_refuses_over_an_unreadable_file(project):
    path = ident.identity_path(project)
    path.parent.mkdir()
    path.write_text("garbage = [")
    with pytest.raises(ident.IdentityExistsError):
        ident.create_identity(project)
    assert path.read_text() == "garbage = ["


def test_create_never_recreates_a_vanished_folder(tmp_path):
    with pytest.raises(ident.IdentityError):
        ident.create_identity(tmp_path / "gone")
    assert not (tmp_path / "gone").exists()


_RACE_CHILD = """
import sys
from rce.records import identity
try:
    print(identity.create_identity(sys.argv[1]).id)
except identity.IdentityExistsError:
    print("exists")
"""


def test_two_processes_racing_to_create_one_wins(project):
    procs = [
        subprocess.Popen([sys.executable, "-c", _RACE_CHILD, str(project)], stdout=subprocess.PIPE, env=os.environ.copy())
        for _ in range(4)
    ]
    outs = sorted(p.communicate(timeout=30)[0].decode().strip() for p in procs)
    winners = [o for o in outs if o != "exists"]
    assert len(winners) == 1
    assert ident.read_identity(project).identity.id == winners[0]


def test_read_absent_dataless_unreadable_conflict(project, monkeypatch):
    assert ident.read_identity(project).state is IdentityState.ABSENT
    created = ident.create_identity(project)
    path = ident.identity_path(project)

    monkeypatch.setattr(paths, "is_dataless", lambda p: True)
    monkeypatch.setattr(paths, "_request_download", lambda p: None)
    assert ident.read_identity(project).state is IdentityState.DATALESS
    monkeypatch.undo()

    (path.parent / "project 2.toml").write_text(path.read_text())
    got = ident.read_identity(project)
    assert got.state is IdentityState.CONFLICT_COPY and got.identity == created
    assert got.conflict_copies == (path.parent / "project 2.toml",)
    (path.parent / "project 2.toml").unlink()

    for bad, line in [
        ('id = "p-xyz"\ncreated = "2026-10-04"\n', 1),
        ('id = "p-' + "a" * 32 + '"\ncreated = "2026-10-04"\nledger = "yes"\n', 3),
        ('id = "p-' + "a" * 32 + '"\ncreated = "2026-10-04"\ncolour = "red"\n', 3),
        ('created = "2026-10-04"\n', None),
        ("id = \n", 1),
    ]:
        path.write_text(bad)
        got = ident.read_identity(project)
        assert got.state is IdentityState.UNREADABLE, bad
        assert got.line == line, (bad, got.error)

    path.write_bytes(b"\xff\xfe")
    assert ident.read_identity(project).state is IdentityState.UNREADABLE


def test_a_bare_toml_date_is_accepted(project):
    path = ident.identity_path(project)
    path.parent.mkdir()
    path.write_text('id = "p-' + "b" * 32 + '"\ncreated = 2026-10-04\n')
    got = ident.read_identity(project)
    assert got.state is IdentityState.PRESENT and got.identity.created == "2026-10-04"


def test_set_flag_rewrites_durably_and_only_raises_ledger(project):
    first = ident.create_identity(project)
    flagged = ident.set_flag(project, first, "ledger", True)
    assert flagged.ledger and ident.read_identity(project).identity == flagged
    with pytest.raises(ident.IdentityError):
        ident.set_flag(project, flagged, "ledger", False)
    migrating = ident.set_flag(project, flagged, "migrating_from", "graphs/abc123")
    done = ident.set_flag(project, migrating, "migrating_from", None)
    assert done == flagged
    with pytest.raises(ident.IdentityError):
        ident.set_flag(project, done, "id", "p-" + "c" * 32)
    with pytest.raises(ident.IdentityError):
        ident.set_flag(project, done, "forked_from", "not-an-id")
    # a snapshot was taken before the rewrites
    assert any((project / ".rce" / "backups").iterdir())


def test_rewrite_is_compare_and_swap(project):
    first = ident.create_identity(project)
    ident.set_flag(project, first, "ledger", True)
    with pytest.raises(ident.IdentityError):  # stale `expected`
        ident.set_flag(project, first, "migrating_from", "x")
    assert ident.read_identity(project).identity.migrating_from is None


def test_rewrite_refused_while_a_conflict_copy_exists(project):
    first = ident.create_identity(project)
    (project / ".rce" / "project (1).toml").write_text("x")
    with pytest.raises(ident.IdentityError):
        ident.set_flag(project, first, "ledger", True)


def test_replace_identity_for_a_fork(project):
    first = ident.create_identity(project)
    forked = ident.ProjectIdentity(id=ident.new_project_id(), created="2026-10-05", ledger=first.ledger, forked_from=first.id)
    ident.replace_identity(project, first, forked)
    assert ident.read_identity(project).identity == forked
    backups = list((project / ".rce" / "backups").iterdir())
    assert any(first.id in b.read_text() for b in backups)
