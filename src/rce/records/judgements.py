"""The judgment ledger drives the index (DESIGN.md 9.1, 9.3, 9.6; task V5
phase 4): the ONE write path for a human verdict on a machine link, the
applier that derives the index's human state from the ledger, the answer
to 9.3's "the file has fewer judgments than the index" question, and what
readers need to mark a link under review.

One write path
--------------

`judge` is what every surface calls -- `rce confirm`, MCP `confirm_edge`,
`POST /api/edges/reject|restore`, `POST /api/judgements`. Under the project
lock and after the write-time identity re-check (`situation.write_guard`,
`human=True`), in this order and no other (phase 1's handoff):

1. read the ledger and decide whether it may be trusted
   (`trust.assess_ledger`, against the index's copy of what it applied);
   a ledger that is missing-though-expected, in the cloud, unreadable,
   invalid, beside a conflict copy, or SHRUNK and unanswered refuses the
   write -- nothing is written anywhere;
2. record the basis: the link's basis as the latest scan of a readable
   source produced it (`basis_recorded = "at-judgment"`); a link that scan
   did not produce is still judged, on the last basis known
   (`"last-known"`), and the applier puts it under review at once;
3. append the entry (`ledger.append_judgement`: seq under the lock, a
   snapshot the first time the file changes each day);
4. raise `ledger = true` in `project.toml` on the first entry -- AFTER the
   append, so a crash between leaves a file with no flag (reads as OK),
   never a flag pointing at a missing file (refuses forever);
5. only then update the index (`apply_ledger`).

No surface writes `edges.status` for a human verdict any more; the applier
is the only writer of a derived human status.

The applier
-----------

`apply_ledger` recomputes the WHOLE human state of the index from the
ledger whenever the ledger can be trusted: after every write, at the end of
every scan, when a watcher sees the file change, and on first sight of a
project. For each link whose ledger state is a verdict (9.6):

- same basis as the latest scan of its readable source -> APPLIED
  (`edges.status` = the verdict);
- otherwise NOT applied: the link is at the machine's own status
  (`edges.machine_status`, migration 0005) under review, with exactly one
  reason -- 「依据已变化」 (still produced, on another basis), 「机器不再得出
  这条关联」 (both ends in the scan, the link not), 「关联的一端不在本次扫描
  结果里」 -- and, when it stopped being produced, the candidates of 9.6's
  "no transfer, but a prompt";
- a source the scan could not read (unreadable / unparseable / never
  scanned) leaves the previous state of that judgment untouched and is
  reported as 「来源文件暂不可读」 -- not a review. A *new* judgment on such
  a link applies when it was recorded on the last basis the index knows
  (which is what `judge` records), and is otherwise held at the machine's
  status, reported the same way;
- a key in ledger conflict (two histories merged) is 「记录冲突，待处理」 at
  the machine's status until a new entry settles it;
- withdrawn, every act undone, or never judged -> the machine's status. A
  non-mapping link at a human status with no judgment behind it is put
  back to the machine's status too: whatever the index knows about a
  human judgment, it learned from the record (9.1).

A judgment whose link the index does not hold at all is kept and listed;
the index gets no fake edge.

An untrusted ledger changes nothing: the index keeps the human state it
had, and the decision is stored (`record_status`) for the views.

The safety net (9.3)
--------------------

The index keeps a copy of every entry it applied (`applied_judgements`).
A readable ledger lacking some of them is SHRUNK: nothing is applied, every
write is refused, and the question 「记录文件比图谱少了 N 条判断」 has two
answers (`answer_shrunk`): 「以文件为准」 drops the missing entries from the
copy and recomputes; 「把缺少的补回文件」 appends them again, `via =
"recovered"`, their content kept (`recovered_from` / `recovered_at` name
the entry they restore -- unknown keys a reader keeps).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Callable, Iterable, Mapping

from rce import db
from rce.ingest import scan as scan_mod
from rce.records import ledger as ledger_mod
from rce.records.files import RecordState
from rce.records.identity import IdentityState, ProjectIdentity, read_identity, set_flag
from rce.records.ledger import (
    JUDGEMENT_SCHEMA,
    MAPPING_EXTRACTOR,
    VERDICTS,
    LedgerEntry,
    LedgerLoad,
    LedgerWriteRefused,
    judgement_status,
    load_judgements,
    parse_ledger,
)
from rce.records.situation import READ_NOW, index_db_path, write_guard
from rce.records.trust import Trust, TrustDecision, assess_ledger

logger = logging.getLogger(__name__)

Key = tuple[str, str, str, str]

RECORD_STATUS_NAME = "judgements"

BASIS_CHANGED = "basis_changed"
NOT_PRODUCED = "not_produced"
ENDPOINT_GONE = "endpoint_gone"
SOURCE_UNREADABLE = "source_unreadable"
RECORD_CONFLICT = "record_conflict"
NOT_IN_INDEX = "not_in_index"

#: Product language (8.8) for each reason; the CLI prints the code beside it.
REASON_LABELS = {
    BASIS_CHANGED: "依据已变化",
    NOT_PRODUCED: "机器不再得出这条关联",
    ENDPOINT_GONE: "关联的一端不在本次扫描结果里",
    SOURCE_UNREADABLE: "来源文件暂不可读",
    RECORD_CONFLICT: "记录冲突，待处理",
    NOT_IN_INDEX: "图谱里还没有这条关联",
}
REVIEW_LABEL = "待复核"
CANDIDATE_HINT = "可能对应一条待复核的旧判断"

#: `basis_recorded` values `judge` writes.
AT_JUDGMENT = "at-judgment"
LAST_KNOWN = "last-known"

HUMAN_VERDICTS = ("confirmed", "rejected")


class JudgementRefused(Exception):
    """Nothing was written. `code` is machine-readable: `untrusted` (the
    ledger may not be written now; `decision` says why, `message` is its
    Chinese sentence), `mapping` (a hand-drawn link), `no_such_link`,
    `nothing_to_undo` / `not_rejected` (an undo with nothing, or not a
    reject, to take back), `no_index`, `no_question`, `invalid`."""

    def __init__(self, code: str, message: str, *, decision: TrustDecision | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.decision = decision

    @property
    def message_zh(self) -> str | None:
        return self.decision.message if self.decision is not None else None


# -- the applied copy -----------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def entry_json(data: Mapping[str, Any]) -> str:
    """An entry as the index stores its copy: canonical JSON (a TOML date a
    hand edit wrote becomes its ISO text)."""
    return json.dumps(_jsonable(data), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def applied_copy(conn: Connection, loaded: LedgerLoad | None = None) -> dict[str, Mapping[str, Any]]:
    """The index's copy of the entries it applied, `{id: data}`, in the
    shape `trust.assess_ledger` compares: an entry still in the file with
    the same content is given as the file's own object (so it is neither
    missing nor `changed`)."""
    rows = db.applied_judgement_rows(conn)
    in_file: dict[str, Mapping[str, Any]] = {}
    if loaded is not None and loaded.ledger is not None:
        in_file = {e.id: e.data for e in loaded.ledger.entries}
    copy: dict[str, Mapping[str, Any]] = {}
    for entry_id, stored in rows.items():
        mine = in_file.get(entry_id)
        copy[entry_id] = mine if mine is not None and entry_json(mine) == stored else json.loads(stored)
    return copy


def _summary(data: Mapping[str, Any]) -> dict[str, Any]:
    """What a person needs to recognise an entry: no basis, everything else."""
    keep = ("id", "seq", "at", "verdict", "src", "dst", "type", "extractor", "via", "note", "undoes")
    return {k: _jsonable(data[k]) for k in keep if k in data}


def status_payload(decision: TrustDecision, loaded: LedgerLoad) -> dict[str, Any]:
    """The trust decision as the views and `rce records` show it."""
    return {
        "state": decision.verdict.value,
        "reason": decision.reason,
        "message": decision.message,
        "detail": decision.detail,
        "line": decision.line,
        "file_state": loaded.state.value,
        "missing": [_summary(m) for m in decision.missing],
        "conflict_copies": [p.name for p in loaded.conflict_copies],
    }


def assess(conn: Connection, project_root: str | Path, identity: ProjectIdentity | None) -> tuple[LedgerLoad, TrustDecision]:
    """Read the ledger and decide (9.3) -- reads only."""
    loaded = load_judgements(project_root)
    return loaded, assess_ledger(identity, loaded, applied_copy(conn, loaded))


# -- the applier ----------------------------------------------------------------------


def key_of(edge: Mapping[str, Any]) -> Key:
    return edge["src"], edge["dst"], edge["type"], edge["extractor"]


def _edge(key: Key) -> dict[str, str]:
    return dict(zip(("src", "dst", "type", "extractor"), key))


def _machine(key: Key, row: tuple[str, str | None] | None) -> str:
    """The machine's own status for a link: `machine_status`, or for a row
    that never got one, its status when that is still machine-owned, else
    what a machine extractor writes for that extractor."""
    if row is not None:
        status, machine = row
        if machine is not None:
            return machine
        if status in ("auto", "pending"):
            return status
    return "pending" if key[3] == "claims" else "auto"


def _basis_of(entry: LedgerEntry) -> dict[str, Any]:
    """A ledger entry's basis for comparison; none written reads as the
    link's identity alone (`{}`), so a hand entry without one applies to an
    identity-only link and is reviewed on any other."""
    basis = entry.get("basis")
    return dict(_jsonable(basis)) if isinstance(basis, Mapping) else {}


def _same_basis(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    return db.canonical_basis(dict(a or {})) == db.canonical_basis(dict(b or {}))


def _judged_item(entry: LedgerEntry, outcome: str, reason: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "reason": reason,
        "verdict": entry.get("verdict"),
        "entry_id": entry.id,
        "at": entry.at,
        "note": entry.get("note"),
        "basis": _basis_of(entry) if entry.get("basis") is not None else None,
        **extra,
    }


def _candidates(conn: Connection, key: Key) -> list[dict[str, Any]]:
    return [
        {k: c[k] for k in ("src", "dst", "type", "extractor", "status")}
        for c in scan_mod.new_links_like(conn, _edge(key))
    ]


def _evaluate(
    conn: Connection,
    key: Key,
    entry: LedgerEntry,
    current_status: str | None,
    previous: Mapping[str, Any] | None,
    scanned: bool,
) -> tuple[dict[str, Any], str | None]:
    """One judged link's outcome per 9.6, and the status the edge should
    have (None: leave it as it is)."""
    verdict = entry.get("verdict")
    edge = _edge(key)
    row = db.edge_scan_row(conn, *key)
    source_status = scan_mod.source_status(conn, edge)
    if row is None:
        # Neither a live edge nor the stamps of a removed one: the index
        # has never held this link (a fresh index, a migrated judgment).
        ends = scan_mod.endpoints_present(conn, edge) if scanned else None
        if ends is False:
            return _judged_item(entry, "review", ENDPOINT_GONE, source_status=source_status), None
        if ends is True:
            return _judged_item(entry, "review", NOT_PRODUCED, source_status=source_status), None
        return _judged_item(entry, "not_in_index", NOT_IN_INDEX, source_status=source_status), None

    if source_status in scan_mod.FAILED or source_status == scan_mod.NOT_SCANNED:
        if previous is not None and previous.get("entry_id") == entry.id and previous["outcome"] in ("applied", "review", "held"):
            kept = dict(previous)
            kept["source_status"] = source_status
            kept["detail"] = {**(previous.get("detail") or {}), "source_unreadable": True}
            status = verdict if previous["outcome"] == "applied" else current_status
            return kept, status
        last = scan_mod.last_basis(conn, edge)
        if _same_basis(_basis_of(entry), last):
            item = _judged_item(entry, "applied", None, source_status=source_status, basis_now=last)
            if source_status != scan_mod.NOT_SCANNED:
                item["detail"] = {"source_unreadable": True}
            return item, verdict
        return _judged_item(
            entry, "held", SOURCE_UNREADABLE, source_status=source_status, basis_now=last,
            detail={"source_unreadable": True},
        ), None

    if scan_mod.produced_in_latest_scan(conn, edge):
        now = scan_mod.current_basis(conn, edge)
        if _same_basis(_basis_of(entry), now):
            return _judged_item(entry, "applied", None, source_status=source_status, basis_now=now), verdict
        return _judged_item(entry, "review", BASIS_CHANGED, source_status=source_status, basis_now=now), None
    ends = scan_mod.endpoints_present(conn, edge)
    reason = ENDPOINT_GONE if ends is False else NOT_PRODUCED
    return _judged_item(
        entry, "review", reason, source_status=source_status, basis_now=None,
        candidates=_candidates(conn, key),
    ), None


def _conflict_item(state: ledger_mod.KeyState) -> dict[str, Any]:
    conflict = state.conflict
    assert conflict is not None
    last = state.history[-1] if state.history else None
    return {
        "outcome": "conflict",
        "reason": RECORD_CONFLICT,
        "verdict": None,
        "entry_id": last.id if last else None,
        "at": last.at if last else None,
        "note": last.get("note") if last else None,
        "basis": None,
        "detail": {
            "common": [_summary(e.data) for e in conflict.common],
            "branches": [[_summary(e.data) for e in branch] for branch in conflict.branches],
        },
    }


@dataclass
class Plan:
    """What the index's human state should be: `statuses` per edge,
    `states` per judged link, `applied` the copy to keep."""

    statuses: dict[Key, str]
    states: dict[Key, dict[str, Any]]
    applied: dict[str, tuple[int | None, str]]


def compute_plan(conn: Connection, ledger: ledger_mod.Ledger) -> Plan:
    """The index's human state the trusted `ledger` implies (reads only)."""
    previous = db.judgement_states(conn)
    edges = db.edge_statuses(conn)
    scanned = db.has_finished_scan(conn)
    statuses: dict[Key, str] = {}
    states: dict[Key, dict[str, Any]] = {}
    for key in ledger.keys():
        key = tuple(key)  # type: ignore[assignment]
        state = ledger.state(key)
        verdict = judgement_status(state)
        row = edges.get(key)
        if verdict is None:
            if row is not None:
                statuses[key] = _machine(key, row)
            continue
        if verdict == "conflict":
            states[key] = _conflict_item(state)
            if row is not None:
                statuses[key] = _machine(key, row)
            continue
        assert state.entry is not None
        item, status = _evaluate(conn, key, state.entry, row[0] if row else None, previous.get(key), scanned)
        states[key] = item
        if row is not None:
            statuses[key] = status if status is not None else _machine(key, row)
    for key, row in edges.items():
        if key not in statuses:
            statuses[key] = _machine(key, row)
    applied = {e.id: (e.seq, entry_json(e.data)) for e in ledger.entries}
    return Plan(statuses=statuses, states=states, applied=applied)


@dataclass(frozen=True)
class ApplyResult:
    """`applied` is False when nothing was changed: no V5 identity, or a
    ledger that may not be trusted (`decision` says why)."""

    applied: bool
    decision: TrustDecision | None = None
    review: int = 0
    conflict: int = 0
    held: int = 0


def _identity_now(project_root: Path) -> ProjectIdentity | None:
    got = read_identity(project_root)
    return got.identity if got.state is IdentityState.PRESENT else None


def apply_ledger(conn: Connection, project_root: str | Path, *, identity: ProjectIdentity | None = None) -> ApplyResult:
    """Recompute the index's whole human state from the ledger (module
    docstring). The caller holds the project lock (it is a scan's, a
    watcher's or `judge`'s last step). Without a V5 identity there is no
    ledger to apply and nothing happens."""
    root = Path(project_root)
    if identity is None:
        identity = _identity_now(root)
        if identity is None:
            return ApplyResult(applied=False)
    loaded, decision = assess(conn, root, identity)
    db.set_record_status(conn, RECORD_STATUS_NAME, status_payload(decision, loaded))
    if not decision.may_apply:
        logger.warning("RCE: %s not applied (%s: %s)", loaded.path, decision.reason, decision.detail)
        return ApplyResult(applied=False, decision=decision)
    ledger = loaded.ledger if loaded.ledger is not None else parse_ledger("", JUDGEMENT_SCHEMA)
    plan = compute_plan(conn, ledger)
    db.write_judgement_state(conn, statuses=plan.statuses, states=plan.states, applied=plan.applied)
    outcomes = [s["outcome"] for s in plan.states.values()]
    return ApplyResult(
        applied=True, decision=decision, review=outcomes.count("review"),
        conflict=outcomes.count("conflict"), held=outcomes.count("held"),
    )


def apply_after_scan(conn: Connection, project_root: str | Path, echo: Callable[[str], None] = lambda _l: None) -> ApplyResult | None:
    """`apply_ledger` as the last step of a scan: a failure is reported and
    contained (the scan itself landed), never raised into the scan's caller."""
    try:
        result = apply_ledger(conn, project_root)
    except Exception as exc:  # noqa: BLE001 -- the scan's own result must stand
        logger.exception("applying the judgment ledger of %s failed", project_root)
        echo(f"  judgements: not applied ({exc})")
        return None
    if result.decision is not None and not result.applied:
        echo(f"  judgements: not applied -- {result.decision.reason} ({result.decision.detail or ''})")
    elif result.applied and (result.review or result.conflict):
        echo(f"  judgements: {result.review} under review, {result.conflict} in conflict (see 'rce review')")
    return result


def verify(conn: Connection, project_root: str | Path) -> list[str]:
    """`rce records --verify`: per link, is the index's human state what the
    record implies? Returns the mismatches (English), empty when it is."""
    root = Path(project_root)
    identity = _identity_now(root)
    if identity is None:
        return [f"{root} has no readable project identity; there is no record to verify against"]
    loaded, decision = assess(conn, root, identity)
    if not decision.may_apply:
        return [f"the judgment ledger cannot be applied now ({decision.reason}: {decision.detail or ''})"]
    ledger = loaded.ledger if loaded.ledger is not None else parse_ledger("", JUDGEMENT_SCHEMA)
    plan = compute_plan(conn, ledger)
    problems = []
    edges = db.edge_statuses(conn)
    for key, status in sorted(plan.statuses.items()):
        have = edges.get(key, (None, None))[0]
        if have != status:
            problems.append(f"{_label(key)}: index status {have!r}, the record implies {status!r}")
    have_states = db.judgement_states(conn)
    for key in sorted(set(plan.states) | set(have_states)):
        want, have_item = plan.states.get(key), have_states.get(key)
        want_t = None if want is None else (want["outcome"], want.get("reason"), want.get("entry_id"))
        have_t = None if have_item is None else (have_item["outcome"], have_item.get("reason"), have_item.get("entry_id"))
        if want_t != have_t:
            problems.append(f"{_label(key)}: index state {have_t!r}, the record implies {want_t!r}")
    stored = db.applied_judgement_rows(conn)
    if set(stored) != set(plan.applied):
        problems.append(
            f"the index's copy of applied entries differs from the file "
            f"({len(set(plan.applied) - set(stored))} not applied, {len(set(stored) - set(plan.applied))} extra)"
        )
    return problems


def _label(key: Key) -> str:
    src, dst, type_, extractor = key
    return f"{src} --{type_}--> {dst} ({extractor})"


# -- the one write path -----------------------------------------------------------------


@dataclass(frozen=True)
class Judged:
    entry: LedgerEntry
    result: ApplyResult
    state: dict[str, Any] | None
    status: str | None


def _open_index(identity: ProjectIdentity) -> Connection:
    path = index_db_path(identity.id)
    if not path.exists():
        raise JudgementRefused("no_index", f"the index of project {identity.id} is missing ({path}); reopen the project")
    return db.connect(path)


def _refuse_untrusted(conn: Connection, loaded: LedgerLoad, decision: TrustDecision) -> None:
    db.set_record_status(conn, RECORD_STATUS_NAME, status_payload(decision, loaded))
    raise JudgementRefused(
        "untrusted",
        f"the judgment ledger may not be written now ({decision.reason}: {decision.detail or decision.message}) -- nothing written",
        decision=decision,
    )


def _held_identity(project_root: Path) -> ProjectIdentity:
    identity = _identity_now(project_root)
    if identity is None:
        raise JudgementRefused(
            "untrusted", f"{project_root} has no readable project identity -- nothing written",
            decision=assess_ledger(None, LedgerLoad(RecordState.ABSENT, project_root), {}),
        )
    return identity


def judge(
    project_root: str | Path,
    key: Iterable[str],
    verdict: str,
    *,
    via: str,
    note: str | None = None,
    expected_id: Any = READ_NOW,
    timeout: float | None = None,
    now: Callable[[], datetime] | None = None,
    undo_only: str | None = None,
    unless_standing: bool = False,
) -> Judged:
    """Record one human act on one machine link and reflect it in the index
    (module docstring, "One write path"). `verdict` is confirmed |
    rejected | withdrawn | undone; `undone` takes back the link's last act
    (`undo_only="rejected"`: only if that act is a reject -- the canvas's
    「撤销」 of 「标记为错误提取」). `unless_standing=True` writes nothing
    when the same verdict already stands and is applied (a second click on
    「标记为错误提取」); a judgment under review is always written -- that
    is 「仍然成立」. Raises `JudgementRefused`, or the
    write guard's `WriteRefused` / `ProjectLockTimeout`; nothing is written
    then."""
    root = Path(project_root)
    key = tuple(key)  # type: ignore[assignment]
    if len(key) != 4 or not all(isinstance(k, str) and k for k in key):
        raise JudgementRefused("invalid", "a link is named by four non-empty strings: src, dst, type, extractor")
    src, dst, type_, extractor = key
    if extractor == MAPPING_EXTRACTOR:
        raise JudgementRefused(
            "mapping",
            "this is a hand-drawn link: its one authority is .rce/mappings.toml -- edit or delete the mapping instead",
        )
    if verdict not in VERDICTS:
        raise JudgementRefused("invalid", f"verdict must be one of {', '.join(sorted(VERDICTS))}, got {verdict!r}")
    if note is not None and not note.strip():
        note = None
    with write_guard(root, expected_id, human=True, timeout=timeout) as held:
        identity = _held_identity(root)
        conn = _open_index(identity)
        try:
            loaded, decision = assess(conn, root, identity)
            if not decision.may_write:
                _refuse_untrusted(conn, loaded, decision)
            ledger = loaded.ledger if loaded.ledger is not None else parse_ledger("", JUDGEMENT_SCHEMA)
            state = ledger.state(key)
            row = db.edge_scan_row(conn, *key)
            if row is None and not state.history:
                raise JudgementRefused("no_such_link", f"no such edge: {_label(key)}")
            basis: dict[str, Any] | None = None
            recorded: str | None = None
            if verdict in HUMAN_VERDICTS and row is not None:
                current = scan_mod.current_basis(conn, _edge(key))
                if current is not None:
                    basis, recorded = current, AT_JUDGMENT
                else:
                    basis = scan_mod.last_basis(conn, _edge(key))
                    recorded = LAST_KNOWN if basis is not None else None
            if unless_standing and state.entry is not None and state.entry.get("verdict") == verdict:
                applied = db.judgement_states(conn).get(key)
                if applied is not None and applied["outcome"] == "applied" and applied.get("entry_id") == state.entry.id:
                    status = db.edge_statuses(conn).get(key, (None, None))[0]
                    return Judged(entry=state.entry, result=ApplyResult(applied=False, decision=decision), state=applied, status=status)
            if verdict == "undone":
                if state.conflict is not None:
                    raise JudgementRefused("invalid", "this link's record is in conflict; settle it with a new judgment, not an undo")
                if undo_only is not None and (state.entry is None or state.entry.get("verdict") != undo_only):
                    raise JudgementRefused("not_rejected", f"{_label(key)} is not {undo_only} by a judgment -- nothing to undo")
                if state.entry is None:
                    raise JudgementRefused("nothing_to_undo", f"there is no act on {_label(key)} to undo")
            try:
                entry = ledger_mod.append_judgement(
                    root, lock=held, verdict=verdict, src=src, dst=dst, type=type_, extractor=extractor,
                    via=via, note=note, basis=basis, basis_recorded=recorded,
                    create=decision.may_create, now=now,
                )
            except LedgerWriteRefused as exc:
                raise JudgementRefused("invalid", str(exc)) from exc
            if not identity.ledger:
                identity = set_flag(root, identity, "ledger", True)
            result = apply_ledger(conn, root, identity=identity)
            states = db.judgement_states(conn)
            status = db.edge_statuses(conn).get(key, (None, None))[0]
            return Judged(entry=entry, result=result, state=states.get(key), status=status)
        finally:
            conn.close()


# -- the SHRUNK question (9.3) -------------------------------------------------------------

ANSWER_FILE = "file"  # 以文件为准
ANSWER_RESTORE = "restore"  # 把缺少的补回文件
ANSWERS = (ANSWER_FILE, ANSWER_RESTORE)


@dataclass(frozen=True)
class Answered:
    answer: str
    missing: tuple[Mapping[str, Any], ...]
    appended: tuple[LedgerEntry, ...] = ()
    result: ApplyResult | None = None


def answer_shrunk(
    project_root: str | Path,
    answer: str,
    *,
    expected_id: Any = READ_NOW,
    timeout: float | None = None,
    now: Callable[[], datetime] | None = None,
) -> Answered:
    """Answer 「记录文件比图谱少了 N 条判断」 (module docstring). Refused
    (`no_question`) unless the ledger is SHRUNK right now."""
    if answer not in ANSWERS:
        raise JudgementRefused("invalid", f"answer must be one of {', '.join(ANSWERS)}, got {answer!r}")
    root = Path(project_root)
    with write_guard(root, expected_id, human=True, timeout=timeout) as held:
        identity = _held_identity(root)
        conn = _open_index(identity)
        try:
            loaded, decision = assess(conn, root, identity)
            if decision.verdict is not Trust.SHRUNK:
                raise JudgementRefused("no_question", "the judgment ledger has not shrunk; there is nothing to answer")
            missing = decision.missing
            appended: list[LedgerEntry] = []
            if answer == ANSWER_FILE:
                db.forget_applied_judgements(conn, [str(m["id"]) for m in missing])
            else:
                renamed: dict[str, str] = {}
                create = loaded.state is RecordState.ABSENT
                for old in missing:
                    fields = {
                        k: v for k, v in old.items()
                        if k not in ("id", "seq", "at", "settles", "via", "undoes")
                    }
                    fields["via"] = "recovered"
                    fields["recovered_from"] = str(old["id"])
                    if old.get("at") is not None:
                        fields["recovered_at"] = str(_jsonable(old["at"]))
                    if old.get("undoes"):
                        fields["undoes"] = renamed.get(str(old["undoes"]), str(old["undoes"]))
                    try:
                        entry = ledger_mod.append(
                            ledger_mod.judgements_path(root), JUDGEMENT_SCHEMA, fields,
                            lock=held, project_root=root, create=create, now=now,
                        )
                    except LedgerWriteRefused as exc:
                        raise JudgementRefused("invalid", f"could not restore entry {old['id']}: {exc}") from exc
                    create = False
                    renamed[str(old["id"])] = entry.id
                    appended.append(entry)
                db.forget_applied_judgements(conn, list(renamed))
                if appended and not identity.ledger:
                    identity = set_flag(root, identity, "ledger", True)
            result = apply_ledger(conn, root, identity=identity)
            return Answered(answer=answer, missing=tuple(missing), appended=tuple(appended), result=result)
        finally:
            conn.close()


# -- what readers show ----------------------------------------------------------------------


def _public_item(key: Key, item: Mapping[str, Any]) -> dict[str, Any]:
    reason = item.get("reason")
    return {
        "src": key[0], "dst": key[1], "type": key[2], "extractor": key[3],
        "outcome": item["outcome"],
        "reason": reason,
        "label": REASON_LABELS.get(reason) if reason else None,
        "verdict": item.get("verdict"),
        "entry_id": item.get("entry_id"),
        "at": item.get("at"),
        "note": item.get("note"),
        "basis": item.get("basis"),
        "basis_now": item.get("basis_now"),
        "candidates": item.get("candidates") or [],
        "source_status": item.get("source_status"),
        "detail": item.get("detail") or {},
    }


def review_items(conn: Connection) -> dict[str, Any]:
    """The list of 9.6 (`rce review`, `GET /api/review`): links under review
    and in conflict (counted in 待复核), then judgments held or kept
    because their source could not be read, and judgments whose link the
    index does not hold -- plus the ledger's trust state."""
    states = db.judgement_states(conn)
    review, held, absent = [], [], []
    for key, item in states.items():
        public = _public_item(key, item)
        if item["outcome"] in db.WAITING_OUTCOMES:
            review.append(public)
        elif item["outcome"] == "held" or (item.get("detail") or {}).get("source_unreadable"):
            held.append(public)
        elif item["outcome"] == "not_in_index":
            absent.append(public)
    return {
        "review": review,
        "count": len(review),
        "source_unreadable": held,
        "not_in_index": absent,
        "ledger": db.get_record_status(conn, RECORD_STATUS_NAME),
    }


@dataclass
class LinkFlags:
    """Per-link marks every reader adds (9.6 "Where it shows"): a link under
    review or in conflict is never shown as applied."""

    states: dict[Key, dict[str, Any]] = field(default_factory=dict)
    candidates: set[Key] = field(default_factory=set)

    def for_key(self, key: Key) -> dict[str, Any]:
        item = self.states.get(tuple(key))  # type: ignore[arg-type]
        waiting = item is not None and item["outcome"] in db.WAITING_OUTCOMES
        flags: dict[str, Any] = {
            "review": waiting and item["outcome"] == "review",
            "conflict": item is not None and item["outcome"] == "conflict",
            "judgement": None,
            "candidate_hint": CANDIDATE_HINT if tuple(key) in self.candidates else None,
        }
        if item is not None:
            flags["judgement"] = {
                "outcome": item["outcome"],
                "reason": item.get("reason"),
                "label": REASON_LABELS.get(item.get("reason") or ""),
                "verdict": item.get("verdict"),
                "at": item.get("at"),
                "note": item.get("note"),
            }
        return flags

    def annotate(self, edge: dict[str, Any]) -> dict[str, Any]:
        edge.update(self.for_key(key_of(edge)))
        return edge


def review_marker(flags: Mapping[str, Any]) -> str:
    """The text mark a CLI or MCP reader puts on a link under review or in
    conflict ("" otherwise): such a link is at the machine's status and
    must not pass for an ordinary one (9.6)."""
    judgement = flags.get("judgement") or {}
    if flags.get("conflict"):
        return f" [record conflict: {REASON_LABELS[RECORD_CONFLICT]} -- see 'rce review']"
    if flags.get("review"):
        reason = judgement.get("reason") or ""
        return (
            f" [under review: {reason} {REASON_LABELS.get(reason, '')}; was {judgement.get('verdict')} "
            f"-- see 'rce review']"
        )
    if flags.get("candidate_hint"):
        return f" [{flags['candidate_hint']}]"
    return ""


def link_flags(conn: Connection) -> LinkFlags:
    states = db.judgement_states(conn)
    candidates: set[Key] = set()
    for item in states.values():
        if item["outcome"] == "review":
            for c in item.get("candidates") or []:
                candidates.add(key_of(c))
    return LinkFlags(states=states, candidates=candidates)


def review_count(conn: Connection) -> int:
    return len(db.waiting_judgement_keys(conn))


def history(project_root: str | Path, key: Iterable[str]) -> list[dict[str, Any]] | None:
    """Every entry for one link in file order (the app's history), or None
    when the ledger cannot be read."""
    loaded = load_judgements(project_root)
    if loaded.state is RecordState.ABSENT:
        return []
    if loaded.ledger is None:
        return None
    ledger = loaded.ledger
    return [
        {**_summary(e.data), "cancelled": ledger.is_cancelled(e)}
        for e in ledger.history(tuple(key))
    ]
