"""Tests for rce.records.trust (DESIGN.md section 9.3): when a ledger may
be written and obeyed. Scenario numbers are 9.9's; these cover the record
half of each (the index and app halves come with later phases)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rce import paths
from rce.records import identity as ident
from rce.records import ledger as L
from rce.records import lock
from rce.records.trust import Trust, assess_ledger

READS = dict(src="script:a.Rmd", dst="dataset:p.csv", type="reads", extractor="dataflow")
WRITES = dict(src="script:b.py", dst="dataset:q.csv", type="writes", extractor="dataflow")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _identity(project, *, ledger=False):
    created = ident.create_identity(project)
    if ledger:
        created = ident.set_flag(project, created, "ledger", True)
    return created


def _judge(project, identity, verdict, link=READS, create=True):
    with lock.project_lock(project, identity.id) as held:
        return L.append_judgement(project, lock=held, verdict=verdict, via="cli", create=create, **link)


def _assess(project, identity, applied):
    return assess_ledger(identity, L.load_judgements(project), applied)


def test_a_new_project_may_found_its_ledger(project):
    identity = _identity(project)
    decision = _assess(project, identity, {})
    assert decision.verdict is Trust.OK and decision.may_write and decision.may_create


def test_first_entry_asks_for_the_flag(project):
    identity = _identity(project)
    _judge(project, identity, "confirmed")
    decision = _assess(project, identity, {})
    assert decision.verdict is Trust.OK and decision.set_ledger_flag
    assert not decision.may_create
    assert [e["verdict"] for e in decision.unseen] == ["confirmed"]


def test_no_identity_or_mid_migration_refuses(project):
    decision = assess_ledger(None, L.load_judgements(project), {})
    assert decision.verdict is Trust.REFUSE_WRITES and decision.reason == "no_identity"
    identity = _identity(project)
    migrating = ident.set_flag(project, identity, "migrating_from", "graphs/0123456789abcdef")
    assert _assess(project, migrating, {}).reason == "migrating"
    assert assess_ledger(migrating, L.load_judgements(project), {}, for_migration=True).may_write


def test_missing_though_expected_is_never_recreated(project):
    """9.9 scenario 11: remove the ledger."""
    identity = _identity(project, ledger=True)
    decision = _assess(project, identity, {})
    assert decision.verdict is Trust.REFUSE_WRITES and decision.reason == "missing"
    assert not decision.may_create and decision.message == "判断记录文件当前无法读取，请先恢复它"


def test_unparseable_refuses_and_names_the_line(project):
    """9.9 scenario 11: make the ledger unparseable."""
    identity = _identity(project)
    entry = _judge(project, identity, "confirmed")
    identity = ident.set_flag(project, identity, "ledger", True)
    path = L.judgements_path(project)
    path.write_text(path.read_text() + "[[judgement]\n")
    decision = _assess(project, identity, {entry.id: entry.data})
    assert decision.verdict is Trust.REFUSE_WRITES and decision.reason == "invalid"
    assert decision.line is not None and not decision.may_apply


def test_dataless_refuses(project, monkeypatch):
    identity = _identity(project)
    entry = _judge(project, identity, "confirmed")
    monkeypatch.setattr(paths, "is_dataless", lambda p: True)
    monkeypatch.setattr(paths, "_request_download", lambda p: None)
    decision = _assess(project, identity, {entry.id: entry.data})
    assert decision.verdict is Trust.REFUSE_WRITES and decision.reason == "dataless"
    assert decision.message == "记录文件正在从云端下载…"


def test_undecodable_refuses(project):
    identity = _identity(project, ledger=True)
    L.judgements_path(project).write_bytes(b"\xff\xfe")
    assert _assess(project, identity, {}).reason == "unreadable"


def test_zero_bytes_with_applied_entries_asks(project):
    """9.9 scenario 11: zero bytes -- the 9.3 question."""
    identity = _identity(project)
    entry = _judge(project, identity, "confirmed")
    identity = ident.set_flag(project, identity, "ledger", True)
    L.judgements_path(project).write_bytes(b"")
    decision = _assess(project, identity, {entry.id: entry.data})
    assert decision.verdict is Trust.SHRUNK and not decision.may_write
    assert [e["id"] for e in decision.missing] == [entry.id]
    assert decision.message == "记录文件比图谱少了 1 条判断"


def test_zero_bytes_with_nothing_applied_still_refuses(project):
    """A brand-new index with a zero-byte ledger the identity says had
    entries: nothing to compare against, so nothing is built upon."""
    identity = _identity(project, ledger=True)
    L.judgements_path(project).parent.mkdir(exist_ok=True)
    L.judgements_path(project).write_bytes(b"")
    decision = _assess(project, identity, {})
    assert decision.verdict is Trust.REFUSE_WRITES and decision.reason == "empty_but_expected"


def test_truncated_at_an_entry_boundary_asks_with_the_missing_entries(project):
    """9.9 scenario 11 (truncated) and scenario 7 (restore an older copy):
    the file is readable but lacks entries the index applied."""
    identity = _identity(project)
    first = _judge(project, identity, "confirmed")
    identity = ident.set_flag(project, identity, "ledger", True)
    path = L.judgements_path(project)
    backup = path.read_bytes()
    second = _judge(project, identity, "rejected", create=False)
    third = _judge(project, identity, "confirmed", WRITES, create=False)
    applied = {e.id: e.data for e in (first, second, third)}
    path.write_bytes(backup)
    decision = _assess(project, identity, applied)
    assert decision.verdict is Trust.SHRUNK
    assert [e["id"] for e in decision.missing] == [second.id, third.id]
    assert decision.message == "记录文件比图谱少了 2 条判断"


def test_absent_without_the_flag_but_with_applied_entries_asks(project):
    identity = _identity(project)
    entry = _judge(project, identity, "confirmed")
    L.judgements_path(project).unlink()
    decision = _assess(project, identity, {entry.id: entry.data})
    assert decision.verdict is Trust.SHRUNK and not decision.may_create


def test_entries_the_index_never_saw_are_simply_applied(project):
    """9.9 scenario 7, second half: a restore on a machine whose index has
    fewer entries is not a question."""
    identity = _identity(project)
    first = _judge(project, identity, "confirmed")
    identity = ident.set_flag(project, identity, "ledger", True)
    second = _judge(project, identity, "rejected", create=False)
    decision = _assess(project, identity, {first.id: first.data})
    assert decision.verdict is Trust.OK and [e["id"] for e in decision.unseen] == [second.id]


def test_a_hand_edited_entry_is_reported_as_changed(project):
    identity = _identity(project)
    entry = _judge(project, identity, "confirmed")
    stale = {**entry.data, "note": "old"}
    decision = _assess(project, identity, {entry.id: stale})
    assert decision.verdict is Trust.OK and [e["id"] for e in decision.changed] == [entry.id]


def test_a_conflict_copy_blocks(project):
    identity = _identity(project)
    entry = _judge(project, identity, "confirmed")
    (project / ".rce" / "judgements 2.toml").write_text("")
    decision = _assess(project, identity, {entry.id: entry.data})
    assert decision.verdict is Trust.CONFLICT_COPY and not decision.may_write
    assert "judgements 2.toml" in decision.detail
