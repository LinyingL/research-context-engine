"""The trust rules of DESIGN.md section 9.3, as pure functions: what a
writer and a reader of a ledger must do, given what the identity file
says, what reading the ledger found, and what the index says it applied.

"A ledger RCE cannot trust is never built upon." The failure this guards
against is concrete: one click on a morning when the sync service is
mid-transfer must not found a new one-entry ledger that the next read
then obeys, retracting everything else. So:

- **missing though expected** (`ledger = true` in `project.toml`), **in the
  cloud**, **unreadable**, **invalid**, or **a conflict copy beside it**:
  REFUSE_WRITES (CONFLICT_COPY for the last). The index keeps the human
  state it had; nothing is applied from the file and nothing is written to
  it. A missing ledger that should exist is never re-created.
- **readable but lacking entries the index applied** (a sync service kept
  the other machine's file; a truncation that still parses; a zero-byte
  download; a restore from backup): SHRUNK, with the missing entries. RCE
  changes nothing and asks; until the researcher answers, writes are
  refused too -- an append would make the shrunk file look current.
- **readable with entries the index never saw**: not a question; OK, with
  those entries listed for the caller to apply.

What the index "applied" is a copy, `{entry id: entry data}`, kept as a
safety net and never as a second authority: it decides only whether to
*ask*. An applied entry whose id is in the file with different content (a
hand edit of one entry) is not missing; it is reported as `changed`, and
the file's version is the one to apply.

The identity side: without a readable identity there is no project to
write for (9.4), and while `migrating_from` is set only the migration
itself writes (9.5). Both are REFUSE_WRITES.

These functions read nothing and write nothing; the caller passes in what
it read, under the project lock, and acts on the decision.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping

from rce.records.files import RecordState
from rce.records.identity import ProjectIdentity
from rce.records.ledger import LedgerLoad


class Trust(str, enum.Enum):
    OK = "ok"
    REFUSE_WRITES = "refuse_writes"
    SHRUNK = "shrunk"
    CONFLICT_COPY = "conflict_copy"


# Product language for the app (DESIGN.md 8.8); the CLI words its own.
MESSAGES = {
    "no_identity": "项目身份文件无法读取",
    "migrating": "项目正在迁移，迁移完成前不能写入人工记录",
    "missing": "判断记录文件当前无法读取，请先恢复它",
    "dataless": "记录文件正在从云端下载…",
    "unreadable": "判断记录文件当前无法读取，请先恢复它",
    "invalid": "判断记录文件当前无法读取，请先恢复它",
    "empty_but_expected": "判断记录文件当前无法读取，请先恢复它",
    "conflict_copy": "判断记录文件旁有同步冲突副本，请先处理",
    "shrunk": "记录文件比图谱少了 {n} 条判断",
}


@dataclass(frozen=True)
class TrustDecision:
    verdict: Trust
    reason: str | None = None
    detail: str | None = None
    line: int | None = None
    missing: tuple[Mapping[str, Any], ...] = ()
    unseen: tuple[Mapping[str, Any], ...] = ()
    changed: tuple[Mapping[str, Any], ...] = ()
    may_create: bool = False
    set_ledger_flag: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def may_write(self) -> bool:
        return self.verdict is Trust.OK

    @property
    def may_apply(self) -> bool:
        """Whether the index may take its human state from the file now."""
        return self.verdict is Trust.OK

    @property
    def message(self) -> str | None:
        if self.reason is None:
            return None
        return MESSAGES[self.reason].format(n=len(self.missing))


def _refuse(reason: str, detail: str | None = None, line: int | None = None) -> TrustDecision:
    return TrustDecision(Trust.REFUSE_WRITES, reason=reason, detail=detail, line=line)


def _seq_order(entry: Mapping[str, Any]) -> tuple[int, int]:
    seq = entry.get("seq")
    return (0, seq) if isinstance(seq, int) else (1, 0)


def assess_ledger(
    identity: ProjectIdentity | None,
    loaded: LedgerLoad,
    applied: Mapping[str, Mapping[str, Any]],
    *,
    for_migration: bool = False,
) -> TrustDecision:
    """Decide whether the ledger may be written and obeyed.

    `identity` is the project's identity as read (None when it could not be
    read or there is none); `loaded` is `rce.records.ledger.load_ledger`'s
    result; `applied` is the index's copy of the entries it applied, by id
    (empty for a brand-new index). `for_migration` is passed only by the
    migration (9.5), the one writer allowed while `migrating_from` is set."""
    if identity is None:
        return _refuse("no_identity")
    if identity.migrating_from is not None and not for_migration:
        return _refuse("migrating", identity.migrating_from)
    if loaded.conflict_copies:
        names = ", ".join(p.name for p in loaded.conflict_copies)
        return TrustDecision(Trust.CONFLICT_COPY, reason="conflict_copy", detail=names)
    expected = identity.ledger

    if loaded.state is RecordState.DATALESS:
        return _refuse("dataless", loaded.error)
    if loaded.state is RecordState.UNREADABLE:
        return _refuse("unreadable", loaded.error)
    if loaded.state is RecordState.INVALID:
        return _refuse("invalid", loaded.error, loaded.line)
    if loaded.state is RecordState.ABSENT:
        if expected:
            return _refuse("missing", "project.toml records a ledger, and the file is not there")
        if applied:
            missing = tuple(sorted(applied.values(), key=_seq_order))
            return TrustDecision(Trust.SHRUNK, reason="shrunk", missing=missing)
        return TrustDecision(Trust.OK, may_create=True)

    ledger = loaded.ledger
    assert ledger is not None  # PRESENT always carries one
    in_file = {e.id: e.data for e in ledger.entries}
    missing = tuple(sorted((v for k, v in applied.items() if k not in in_file), key=_seq_order))
    if missing:
        return TrustDecision(Trust.SHRUNK, reason="shrunk", missing=missing)
    if expected and not ledger.entries:
        return _refuse("empty_but_expected", "project.toml records a ledger, and the file holds no entries")
    unseen = tuple(e.data for e in ledger.entries if e.id not in applied)
    changed = tuple(e.data for e in ledger.entries if e.id in applied and dict(applied[e.id]) != dict(e.data))
    return TrustDecision(
        Trust.OK,
        unseen=unseen,
        changed=changed,
        set_ledger_flag=bool(ledger.entries) and not expected,
    )
