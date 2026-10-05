"""What RCE does with a variable card (DESIGN.md 9.11; task V5 phase 8): the
one write path for every act on a card, the checks a confirmation makes,
the index's copy of the cards, and what readers show.

Every act, whatever the surface (CLI now, the app later), goes through
`_writing`: under the project lock with the identity re-checked
(`situation.write_guard`, `human=True` -- a pre-V5 project is read-only, a
moved one writes nothing), the card is read again and must be trusted --
readable, its log trusted by the files alone (`variables.read_card`) and
not SHRUNK against the index's copy of what it applied (9.3, through
`trust.assess_ledger`, the same rules as the judgment ledger). Writes to
THAT card are refused otherwise; other cards are untouched. The record is
written first and the index second (`apply_cards`).

Confirming: snapshot first, entry last
--------------------------------------

`confirm` turns the draft into a definition results may rely on, in this
order and no other:

1. parse and validate the version file (a confirmation needs a name,
   meaning, unit, granularity, an input, a formula and a reason); pin each
   `variable = "<id>@v<n>"` input to the entry it relies on (only a
   confirmed version can be referred to);
2. the checks, BY PARSING THE SCRIPT NOW with the dataflow parser (not by
   asking the index, which keeps links a script stopped producing): does
   this parse write the output and read each declared dataset. Each is
   已核对, 不符 (the script parsed and does no such thing), or 未核对 with
   its reason. No outcome blocks confirmation;
3. what is on disk, as observations: the output and each input --
   sha256 and size below 50 MB, size and mtime above; a file still in the
   cloud is 未核对 and is not downloaded;
4. the code copy (`_code/<sha256>.<ext>`, skipped when it is already
   there with that hash) and the frozen copy (`frozen/<content
   hash>.toml`, the version file's bytes) written durably, read back and
   verified;
5. ONLY THEN the `confirmed` entry naming them, with `attested` -- the
   researcher's own answer to "was the output as it stands built with this
   definition" (yes | no | unknown). RCE never derives it from a matching
   hash (Section 0, "kept material is not a relation").

A crash anywhere leaves at worst copies no entry refers to (`rce records
--clean` removes them), never an entry naming a copy that is not there.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import logging
import os
import posixpath
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Callable, Iterable, Iterator, Mapping

from rce import db, paths
from rce.records import files
from rce.records import ledger as ledger_mod
from rce.records import variables as V
from rce.records.files import RecordState
from rce.records.identity import IdentityState, ProjectIdentity, read_identity
from rce.records.ledger import LedgerEntry, LedgerLoad, LedgerWriteRefused
from rce.records.situation import READ_NOW, index_db_path, write_guard
from rce.records.trust import Trust, TrustDecision, assess_ledger

logger = logging.getLogger(__name__)

Fault = Callable[[str], None]
Clock = Callable[[], datetime]

RECORD_STATUS_NAME = "variables"

# 未核对 reasons (product language, 8.8).
R_NO_SCRIPT = "未填写实现脚本"
R_NO_OUTPUT = "未填写输出文件"
R_SCRIPT_ABSENT = "找不到脚本文件"
R_FILE_ABSENT = "找不到这个文件"
R_IN_CLOUD = "文件仍在云端"
R_SCRIPT_UNREADABLE = "脚本无法读取"
R_UNREADABLE = "文件无法读取"
R_SCRIPT_UNPARSEABLE = "脚本无法解析"
R_SCRIPT_KIND = "这种脚本无法解析"
R_OUTSIDE = "路径不在项目文件夹内"
R_MAPPING_ONLY = "这条关联只存在于你手画的连线中"
R_NOT_CSV = "输出文件不是 CSV"
R_HEADER = "无法读取 CSV 表头"
M_NO_WRITE = "脚本没有写出这个文件"
M_NO_READ = "脚本没有读取这个文件"
M_NO_FIELD = "表头里没有这个字段"

DEAD_CARD_ONLY = "卡片已弃用，attempts.toml 未列入"
DEAD_ATTEMPTS_ONLY = "attempts.toml 列为已弃用，卡片未弃用"

TEMPLATE = """\
# 变量定义卡 — 版本 {n}
# 这个文件由你书写，RCE 从不改写它。RCE 知道的一切都记在同目录的 log.toml 里。
# 自由文本请写在 '''…''' 里（字面字符串）：公式里的反斜杠和引号会原样保留。
# 只能使用下面列出的键；写错位置的一行（例如滑到了别的标题下）会被指出行号。
# 草稿可以不完整；确认（rce variable confirm）需要：名称、含义、单位、粒度、
# 至少一个输入、构建公式和决策理由。

name    = ''                 # 例：'TopicShift（叙事更替）'
aliases = []                 # 尝试表里用过的写法，例：["TopicShift"]

# 含义
meaning     = ''''''
unit        = ''
granularity = ''             # 例：'月'

# 输入 — 一个数据文件（项目内的相对路径），或另一个变量的固定版本
# 例：variable = "returns@v1"；每个输入写一个 [[input]]
[[input]]
dataset      = ''
fields       = []
data_version = ''''''

# 构建口径
[construction]
formula     = ''''''
filter      = ''''''
aggregation = ''''''
missing     = ''''''
transform   = ''''''
params      = ''''''

# 实现依据
[implementation]
script       = ''
output       = ''
field        = ''
code_version = ''''''        # 可选，用你自己的话

# 人工决策
[decision]
why        = ''''''
decided_by = ''
adopted_on = ''              # 采用这个定义的日期，可以早于这张卡
"""


class CardRefused(V.VariableError):
    """Nothing was written. `code`: invalid, exists, no_such_card,
    untrusted (the card may not be written now; `decision` says why),
    no_index, no_draft, incomplete, unresolvable, draft_open, no_question,
    question_open, copy_missing, nothing_confirmed, already_abandoned,
    not_abandoned, would_lose, question_changed, changed, region_missing."""

    def __init__(self, code: str, message: str, *, message_zh: str | None = None, decision: TrustDecision | None = None) -> None:
        super().__init__(code, message, message_zh=message_zh)
        self.decision = decision


# -- small helpers -----------------------------------------------------------------------


#: `expected_content` not given: the act is not tied to a shown text (the
#: CLI reads the card at the moment it acts).
ANY_CONTENT = object()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _confined(root: Path, rel: str) -> Path | None:
    """`rel` under the project root, resolved; None when it leaves it."""
    candidate = root / rel
    try:
        candidate.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return candidate


def _identity_now(root: Path) -> ProjectIdentity | None:
    got = read_identity(root)
    return got.identity if got.state is IdentityState.PRESENT else None


def _create_exclusively(path: Path, data: bytes) -> None:
    """`data` at `path`, only if nothing is there (temp file + link), synced."""
    tmp = files.temp_path_for(path)
    try:
        files.write_new_file(tmp, data)
        try:
            os.link(tmp, path)
        except FileExistsError as exc:
            raise CardRefused("exists", f"{path} already exists; nothing written") from exc
        except OSError:
            try:
                files.write_new_file(path, data)
            except FileExistsError as exc:
                raise CardRefused("exists", f"{path} already exists; nothing written") from exc
        files._fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _copy_dir(root: Path, directory: Path) -> Path:
    """A directory that kept copies go into (`_code/`, a card's `frozen/`),
    created inside the project if need be. Refused when it is a symlink --
    as `card_dirs` and `rce records --clean` treat one: a copy written
    through it would live wherever the link points, and the entry naming it
    would claim it is in the project."""
    try:
        files.ensure_dir_within(root, directory)
    except files.RecordFileError as exc:
        raise CardRefused("invalid", f"not recorded: {exc}") from exc
    if directory.is_symlink():
        raise CardRefused("invalid", f"not recorded: {directory} is a symbolic link; the copies must live in the project")
    return directory


def _read_back(path: Path, data: bytes) -> None:
    got = files.read_record(path)
    # By bytes: a researcher's file kept as they saved it (even not UTF-8,
    # 「另存为新版本」 of a re-encoded file) reads back as those bytes.
    if got.data is None or got.data != data:
        raise CardRefused("changed", f"{path} does not read back as written; nothing recorded")


# -- the index's copy and the trust decision ----------------------------------------------


def _ensure_tables(conn: Connection) -> None:
    if not db.has_variable_tables(conn):
        db.migrate(conn)


def applied_copy(conn: Connection, card: V.Card) -> dict[str, Mapping[str, Any]]:
    """The index's copy of the entries it applied for `card`, `{id: data}`;
    an entry still in the file with the same content is the file's own
    object (neither missing nor changed) -- `judgements.applied_copy`'s rule."""
    from rce.records.judgements import entry_json  # noqa: PLC0415 -- judgements imports this module lazily

    rows = db.applied_variable_rows(conn, card.key)
    in_file = {e.id: e.data for e in card.entries}
    out: dict[str, Mapping[str, Any]] = {}
    for entry_id, stored in rows.items():
        mine = in_file.get(entry_id)
        out[entry_id] = mine if mine is not None and entry_json(mine) == stored else json.loads(stored)
    return out


def assess_card(conn: Connection | None, root: Path, card: V.Card, identity: ProjectIdentity | None) -> TrustDecision:
    """May `card` be written and obeyed (9.3 for its log)? The files' own
    verdict first (`card.state`), then the index's copy: a log lacking
    entries the index applied is SHRUNK."""
    if identity is None:
        return TrustDecision(Trust.REFUSE_WRITES, reason="no_identity")
    if identity.migrating_from is not None:
        return TrustDecision(Trust.REFUSE_WRITES, reason="migrating", detail=identity.migrating_from)
    if card.state == "unreadable":
        verdict = Trust.CONFLICT_COPY if card.reason == "conflict_copy" else Trust.REFUSE_WRITES
        return TrustDecision(verdict, reason=card.reason, detail=card.detail)
    if card.state == "frozen" and card.reason not in ("missing", "empty_but_expected"):
        return TrustDecision(Trust.REFUSE_WRITES, reason=card.reason, detail=card.detail, line=card.line)
    assert card.log is not None
    applied = applied_copy(conn, card) if conn is not None else {}
    if card.log.state is RecordState.ABSENT and applied:
        # A log removed (or a card directory gone) while the index holds
        # what it applied: asked about, never obeyed (9.11 scenario 15).
        return assess_ledger(identity, card.log, applied, expected=False)
    return assess_ledger(identity, card.log, applied, expected=card.log_expected)


def _message(decision: TrustDecision) -> str | None:
    if decision.reason is None:
        return None
    return V.MESSAGES.get(decision.reason, "").format(n=len(decision.missing)) or None


def _summary(data: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("id", "seq", "at", "act", "version", "via", "attested", "content", "previous", "corrects", "frozen", "note",
            "keeps", "settles")
    return {k: V.jsonable(data[k]) for k in keep if k in data}


def status_payload(card: V.Card, decision: TrustDecision) -> dict[str, Any]:
    return {
        "id": card.id,
        "state": decision.verdict.value,
        "reason": decision.reason,
        "message": _message(decision),
        "detail": decision.detail,
        "line": decision.line,
        "missing": [_summary(m) for m in decision.missing],
        "card_state": card.state,
        "directory_missing": not card.directory.is_dir(),
    }


def _card_copy(card: V.Card) -> dict[str, Any]:
    """What the index keeps of a trusted card: the text of each version file
    and of each frozen copy (so a card whose directory vanished can have its
    frozen copies put back by 「把缺少的补回文件」)."""
    versions = {
        str(n): (v.data.decode("utf-8", "replace") if v.data is not None else None)
        for n, v in sorted(card.versions.items())
    }
    frozen: dict[str, str] = {}
    for e in card.entries:
        rel = e.get("frozen")
        if isinstance(rel, str) and rel not in frozen:
            got = files.read_record(card.directory / rel)
            if got.state is RecordState.PRESENT and got.text is not None:
                frozen[rel] = got.data.decode("utf-8") if got.data is not None else got.text
    return {"id": card.id, "versions": versions, "frozen": frozen, "in_use": card.in_use, "draft": card.draft,
            "abandoned": card.abandoned is not None}


def _cards_in_view(conn: Connection | None, root: Path) -> list[V.Card] | None:
    """Every card on disk, plus every card the index knows whose directory
    is gone (read as a card with no log -- SHRUNK against the copy).
    None when `.rce/variables/` cannot be listed."""
    dirs = V.card_dirs(root)
    if dirs is None:
        return None
    cards = [V.read_card(root, d, siblings=dirs) for d in dirs]
    seen = {c.key for c in cards}
    if conn is not None:
        for key, row in db.variable_cards(conn).items():
            if key not in seen:
                cards.append(V.read_card(root, V.variables_dir(root) / row["id"], siblings=dirs))
    return cards


def apply_cards(conn: Connection, project_root: str | Path, *, identity: ProjectIdentity | None = None,
                full_for: set[str] | None = None) -> dict[str, Any]:
    """Refresh the index's copy of every card from its files (the record
    first, the index second): a trusted card's copy and applied entries are
    replaced; an untrusted one keeps what the index had, and its decision is
    stored for the views. A gone card the index never applied anything of is
    forgotten. Called under the project lock (from
    `judgements.apply_ledger`, i.e. at the end of every scan, by the watcher
    and after every record write). Then stage (b): every trusted card's
    version in use is compared with its implementation now
    (`implementation.refresh`; `full_for` are card keys whose large inputs
    are hashed, 「完整比对」). Returns {card id: status payload}."""
    from rce.records.judgements import entry_json  # noqa: PLC0415

    root = Path(project_root)
    _ensure_tables(conn)
    identity = identity if identity is not None else _identity_now(root)
    cards = _cards_in_view(conn, root)
    if cards is None:
        return {}
    out: dict[str, Any] = {}
    trusted: set[str] = set()
    for card in cards:
        decision = assess_card(conn, root, card, identity)
        status = status_payload(card, decision)
        out[card.id] = status
        gone = not card.directory.is_dir()
        if gone and not db.applied_variable_rows(conn, card.key):
            db.forget_variable_card(conn, card.key)
            continue
        if decision.may_apply and not gone:
            trusted.add(card.key)
            applied = {e.id: (e.seq, entry_json(e.data)) for e in card.entries}
            db.write_variable_card(conn, card.key, card.id, status=status, data=_card_copy(card), applied=applied)
        else:
            db.write_variable_card(conn, card.key, card.id, status=status)
    db.set_record_status(conn, RECORD_STATUS_NAME, {"cards": out})
    from rce.records import implementation  # noqa: PLC0415 -- implementation imports this module lazily

    try:
        implementation.refresh(conn, root, cards, trusted=trusted, full_for=full_for)
    except Exception:  # noqa: BLE001 -- a comparison never stops the record being applied
        logger.exception("comparing the variable cards' implementations of %s failed", root)
    return out


def verify(conn: Connection, project_root: str | Path) -> list[str]:
    """`rce records --verify` for the cards: the index holds, for every card
    that can be trusted, exactly the log entries the file holds -- and every
    frozen copy and code copy an entry names is there (9.11: an entry whose
    copy is missing is 「确认记录引用的副本缺失」, never a passing record)."""
    root = Path(project_root)
    identity = _identity_now(root)
    cards = _cards_in_view(conn, root) or []
    problems: list[str] = []
    for card in cards:
        decision = assess_card(conn, root, card, identity)
        if not decision.may_apply:
            problems.append(f"variable card {card.id}: cannot be applied now ({decision.reason}: {decision.detail or ''})")
            continue
        stored = set(db.applied_variable_rows(conn, card.key))
        in_file = {e.id for e in card.entries}
        if stored != in_file:
            problems.append(
                f"variable card {card.id}: the index's copy differs from log.toml "
                f"({len(in_file - stored)} not applied, {len(stored - in_file)} extra)"
            )
        if card.key not in db.variable_cards(conn) and card.directory.is_dir():
            problems.append(f"variable card {card.id}: not in the index")
        problems += [f"variable card {card.id}: {p}" for p in missing_copies(card)]
    return problems


def missing_copies(card: V.Card) -> list[str]:
    """Each copy an entry of `card` names that is not on disk (a sync that
    has not delivered it, a hand deletion)."""
    out = []
    for e in card.entries:
        frozen = e.get("frozen")
        if isinstance(frozen, str) and not (card.directory / frozen).is_file():
            out.append(f"entry {e.id} ({e.get('act')} v{e.get('version')}) names {frozen}, which is not there")
        code = V.code_copy_of(e)
        if code is not None and not (card.directory.parent / code).is_file():
            out.append(f"entry {e.id} ({e.get('act')} v{e.get('version')}) names the code copy {code}, which is not there")
    return out


def rebuild_questions(conn: Connection, project_root: str | Path, identity: ProjectIdentity) -> list[str]:
    """Why a rebuild may not start (9.3): a card whose log has fewer entries
    than this index applied -- a fresh index would obey the shrunk file and
    answer 「以文件为准」 for the researcher."""
    root = Path(project_root)
    out = []
    for card in _cards_in_view(conn, root) or []:
        decision = assess_card(conn, root, card, identity)
        if decision.verdict is Trust.SHRUNK:
            out.append(
                f"variable card {card.id}: log.toml has {len(decision.missing)} entr(y/ies) fewer than this index "
                f"applied; answer that first ('rce variable answer {card.id} file|restore')"
            )
    return out


# -- the one write path --------------------------------------------------------------------


@dataclass
class _Writing:
    held: Any
    root: Path
    card: V.Card
    conn: Connection
    identity: ProjectIdentity
    decision: TrustDecision


def _open_index(identity: ProjectIdentity) -> Connection:
    path = index_db_path(identity.id)
    if not path.exists():
        raise CardRefused("no_index", f"the index of project {identity.id} is missing ({path}); reopen the project")
    conn = db.connect(path)
    _ensure_tables(conn)
    return conn


@contextlib.contextmanager
def _writing(
    project_root: str | Path, card_id: str, expected_id: Any, timeout: float | None, *, allow_shrunk: bool = False,
    allow_conflict: bool = False,
) -> Iterator[_Writing]:
    root = Path(project_root)
    with write_guard(root, expected_id, human=True, timeout=timeout) as held:
        identity = _identity_now(root)
        if identity is None:
            raise CardRefused("untrusted", f"{root} has no readable project identity -- nothing written",
                              message_zh=V.MESSAGES["no_identity"])
        conn = _open_index(identity)
        try:
            found = V.find_card_dirs(root, card_id)
            if found:
                card = V.read_card(root, found[0])
            else:
                known = db.variable_cards(conn).get(V.card_key(card_id))
                if known is None:
                    raise CardRefused("no_such_card", f"there is no variable card {card_id!r}")
                card = V.read_card(root, V.variables_dir(root) / known["id"])
            decision = assess_card(conn, root, card, identity)
            if allow_conflict and card.state == "frozen" and card.reason == "conflict":
                # Settling (9.12) is the one write a card in conflict takes;
                # every other 9.3 rule still holds for its log.
                decision = assess_ledger(identity, card.log, applied_copy(conn, card), expected=card.log_expected)
            if not decision.may_write and not (allow_shrunk and decision.verdict is Trust.SHRUNK):
                db.write_variable_card(conn, card.key, card.id, status=status_payload(card, decision))
                raise CardRefused(
                    "untrusted",
                    f"variable card {card.id} may not be written now ({decision.reason}: "
                    f"{decision.detail or _message(decision) or ''}) -- nothing written",
                    message_zh=_message(decision), decision=decision,
                )
            yield _Writing(held, root, card, conn, identity, decision)
            apply_cards(conn, root, identity=identity)
        finally:
            conn.close()


def _append(w: _Writing, fields_list: list[dict[str, Any]], *, now: Clock | None) -> list[LedgerEntry]:
    try:
        return ledger_mod.append_many(
            w.card.directory / V.LOG_FILENAME, V.CARD_LOG_SCHEMA, fields_list,
            lock=w.held, project_root=w.root, create=w.decision.may_create or w.card.log.state is RecordState.ABSENT,
            now=now, snapshot_subdir=V.snapshot_subdir(w.card.id),
        )
    except LedgerWriteRefused as exc:
        raise CardRefused("invalid", f"not recorded: {exc}") from exc


# -- new, revise ---------------------------------------------------------------------------


def template(number: int = 1) -> bytes:
    return TEMPLATE.format(n=number).encode("utf-8")


def new_card(project_root: str | Path, card_id: str, *, expected_id: Any = READ_NOW, timeout: float | None = None) -> Path:
    """`rce variable new <id>`: the directory, created exclusively, and
    `v1.toml` from the commented template. Refuses an id already present --
    up to letter case -- in `.rce/variables/`, in the index's copy, or in
    the snapshots (`.rce/backups/variables/`)."""
    problem = V.id_problem(card_id)
    if problem:
        raise CardRefused("invalid", f"{card_id!r}: {problem}")
    root = Path(project_root)
    key = V.card_key(card_id)
    with write_guard(root, expected_id, human=True, timeout=timeout):
        identity = _identity_now(root)
        if identity is None:
            raise CardRefused("untrusted", f"{root} has no readable project identity -- nothing written")
        if V.card_dirs(root) is None:
            raise CardRefused("untrusted", f"{V.variables_dir(root)} cannot be listed -- nothing written")
        taken = [p.name for p in V.find_card_dirs(root, card_id)]
        if taken:
            raise CardRefused("exists", f"a variable card {taken[0]!r} already exists (ids compare case-folded)")
        index = index_db_path(identity.id)
        if index.exists():
            conn = db.connect(index)
            try:
                known = db.variable_cards(conn).get(key)
            finally:
                conn.close()
            if known is not None:
                raise CardRefused("exists", f"the index still holds a variable card {known['id']!r}; answer its question first")
        snaps = paths.project_rce_dir(root) / files.BACKUPS_DIRNAME / V.SNAPSHOT_SUBDIR
        if snaps.is_dir() and any(V.card_key(p.name) == key for p in snaps.iterdir()):
            raise CardRefused("exists", f"snapshots of a variable card {card_id!r} exist in {snaps}; that id was used")
        try:
            files.ensure_dir_within(root, V.variables_dir(root))
            directory = V.variables_dir(root) / card_id
            directory.mkdir()
        except FileExistsError as exc:
            raise CardRefused("exists", f"{card_id!r} already exists") from exc
        except files.RecordFileError as exc:
            raise CardRefused("invalid", str(exc)) from exc
        path = directory / "v1.toml"
        _create_exclusively(path, template(1))
        return path


def revise(project_root: str | Path, card_id: str, *, expected_id: Any = READ_NOW, timeout: float | None = None) -> Path:
    """`rce variable revise <id>`: the current confirmed version file copied
    byte for byte to the next number, as the draft. Refused while a draft is
    open, and while the version to copy was edited after its confirmation
    (answer that question first)."""
    with _writing(project_root, card_id, expected_id, timeout) as w:
        card = w.card
        if card.draft is not None:
            raise CardRefused("draft_open", f"draft v{card.draft} is open; confirm it first (a card has one draft)",
                              message_zh=f"已有草稿 v{card.draft}，请先确认它")
        confirmed = sorted(n for n, v in card.versions.items() if v.entry is not None)
        if not confirmed:
            raise CardRefused("nothing_confirmed", f"{card.id} has no confirmed version yet; edit the draft instead")
        source = card.in_use if card.in_use is not None else confirmed[-1]
        view = card.versions[source]
        if view.question:
            raise CardRefused("question_open", f"v{source} was changed after it was confirmed; answer that first",
                              message_zh=V.QUESTION_EDITED.format(n=source))
        if view.file_state == RecordState.PRESENT.value and view.data is not None:
            data = view.data
        else:
            frozen = _verified_frozen(card, view)
            if frozen is None:
                raise CardRefused("copy_missing", f"v{source}.toml is not readable and its frozen copy is missing",
                                  message_zh=V.COPY_MISSING)
            data = frozen
        target = card.directory / f"v{card.next_number}.toml"
        _create_exclusively(target, data)
        _read_back(target, data)
        return target


def _verified_frozen(card: V.Card, view: V.VersionView) -> bytes | None:
    """The frozen copy of `view`'s current entry, if it is there and still
    has the content the entry froze."""
    path = view.frozen_path
    if path is None:
        return None
    got = files.read_record(path)
    if got.state is not RecordState.PRESENT:
        return None
    try:
        ok = V.content_hash(V.parse_version(got.text or "")) == view.entry.get("content")
    except V.VersionInvalid:
        return None
    return got.data if ok else None


# -- checks and observations -----------------------------------------------------------------


def _parse_script(root: Path, rel: str) -> tuple[str | None, list[Any]]:
    """(reason it could not be parsed or None, calls)."""
    from rce.ingest import dataflow  # noqa: PLC0415 -- ingest is heavier; only confirmations need it
    from rce.ingest import scan as scan_mod  # noqa: PLC0415

    suffix = posixpath.splitext(rel)[1].lower()
    scanner = {".py": dataflow.scan_py_file, ".r": dataflow.scan_r_file, ".rmd": dataflow.scan_rmd_file}.get(suffix)
    if scanner is None:
        return R_SCRIPT_KIND, []
    outcome = scanner(root, rel)
    if outcome.status == scan_mod.UNREADABLE:
        return R_SCRIPT_UNREADABLE, []
    if outcome.status != scan_mod.READ_AND_PARSED:
        return R_SCRIPT_UNPARSEABLE, []
    resolved = []
    for call in outcome.calls:
        target = dataflow._resolve_target(rel, call.literal, root)
        if target is not None:
            resolved.append((call.kind, target[0], call.bare_name))
    return None, resolved


def _mapped(root: Path, script: str, target: str, kind: str) -> bool:
    """Whether the researcher drew this link by hand (`.rce/mappings.toml`)
    -- their statement, not a check."""
    from rce.ingest import mappings as mappings_ingest  # noqa: PLC0415

    try:
        loaded = mappings_ingest.load_mappings(root)
    except mappings_ingest.MappingsFileError:
        return False
    types = ("writes", "generates") if kind == "write" else ("reads",)
    for m in loaded.mappings:
        if m.type in types and m.src_id == f"script:{script}" and m.dst_id.split(":", 1)[-1] == target:
            return True
    return False


def _link_check(root: Path, script: str | None, parse: tuple[str | None, list[Any]] | None, target: str,
                kind: str) -> dict[str, Any]:
    if not script:
        return {"result": V.UNCHECKED, "reason": R_NO_SCRIPT}
    assert parse is not None
    reason, calls = parse
    if reason is not None:
        return {"result": V.UNCHECKED, "reason": reason}
    norm = posixpath.normpath(target)
    names = sorted({name for k, path, name in calls if k == kind and path == norm})
    if names:
        return {"result": V.CHECKED, "call": ", ".join(names)}
    if _mapped(root, script, norm, kind):
        return {"result": V.UNCHECKED, "reason": R_MAPPING_ONLY}
    return {"result": V.MISMATCH, "reason": M_NO_WRITE if kind == "write" else M_NO_READ}


def _field_check(root: Path, output: str | None, field_name: str) -> dict[str, Any]:
    if not output:
        return {"result": V.UNCHECKED, "reason": R_NO_OUTPUT}
    if posixpath.splitext(output)[1].lower() != ".csv":
        return {"result": V.UNCHECKED, "reason": R_NOT_CSV}
    path = _confined(root, output)
    if path is None:
        return {"result": V.UNCHECKED, "reason": R_OUTSIDE}
    if not path.exists():
        return {"result": V.UNCHECKED, "reason": R_FILE_ABSENT}
    if paths.is_dataless(path):
        return {"result": V.UNCHECKED, "reason": R_IN_CLOUD}
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            header = next(csv.reader(handle))
    except Exception:  # noqa: BLE001 -- any failure is 未核对 (9.11)
        return {"result": V.UNCHECKED, "reason": R_HEADER}
    if field_name in [h.strip() for h in header]:
        return {"result": V.CHECKED}
    return {"result": V.MISMATCH, "reason": M_NO_FIELD}


def fingerprint(root: Path, rel: str, *, full: bool = False) -> dict[str, Any]:
    """An observation of one file now (9.11): sha256 + size below 50 MB,
    size + mtime above -- plus the sha256 when `full` (an input at
    confirmation: the scan compares a large input by size, and 「完整比对」
    later needs the content hash to compare with); a file in the cloud is
    not downloaded."""
    path = _confined(root, rel)
    if path is None:
        return {"result": V.UNCHECKED, "reason": R_OUTSIDE}
    try:
        st = path.stat()
    except OSError:
        return {"result": V.UNCHECKED, "reason": R_FILE_ABSENT}
    if paths.is_dataless(path):
        return {"result": V.UNCHECKED, "reason": R_IN_CLOUD}
    large: dict[str, Any] = {}
    if st.st_size >= V.LARGE_FILE_BYTES:
        mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="microseconds")
        if not full:
            return {"size": st.st_size, "mtime": mtime}
        large = {"mtime": mtime}
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return {"result": V.UNCHECKED, "reason": R_UNREADABLE}
    return {"sha256": digest.hexdigest(), "size": st.st_size, **large}


@dataclass
class _Inspection:
    checked: dict[str, Any]
    observed: dict[str, Any]
    script_bytes: bytes | None
    script_copy: str | None


def inspect(root: Path, content: Mapping[str, Any]) -> _Inspection:
    """The checks and observations of a confirmation (module docstring,
    steps 2 and 3). Reads only; never downloads a file from the cloud."""
    impl = content.get("implementation") or {}
    script = (impl.get("script") or "").strip() or None
    output = (impl.get("output") or "").strip() or None
    field_name = (impl.get("field") or "").strip() or None
    checked: dict[str, Any] = {}
    script_bytes: bytes | None = None
    copy: str | None = None
    parse: tuple[str | None, list[Any]] | None = None
    if script is None:
        checked["script"] = {"result": V.UNCHECKED, "reason": R_NO_SCRIPT}
    else:
        path = _confined(root, script)
        if path is None:
            checked["script"] = {"result": V.UNCHECKED, "reason": R_OUTSIDE}
            parse = (R_OUTSIDE, [])
        elif not path.is_file():
            checked["script"] = {"result": V.UNCHECKED, "reason": R_SCRIPT_ABSENT}
            parse = (R_SCRIPT_ABSENT, [])
        elif paths.is_dataless(path):
            checked["script"] = {"result": V.UNCHECKED, "reason": R_IN_CLOUD}
            parse = (R_IN_CLOUD, [])
        else:
            try:
                script_bytes = path.read_bytes()
            except OSError:
                checked["script"] = {"result": V.UNCHECKED, "reason": R_SCRIPT_UNREADABLE}
                parse = (R_SCRIPT_UNREADABLE, [])
            else:
                sha = _sha256(script_bytes)
                copy = f"{V.CODE_DIRNAME}/{sha}{Path(script).suffix}"
                checked["script"] = {"result": V.CHECKED, "sha256": sha, "size": len(script_bytes), "copy": copy,
                                     **_code_fingerprint(script, script_bytes, impl)}
                parse = _parse_script(root, script)
    checked["writes"] = (
        {"result": V.UNCHECKED, "reason": R_NO_OUTPUT} if output is None else _link_check(root, script, parse, output, "write")
    )
    reads = []
    for item in content.get("input") or []:
        dataset = (item.get("dataset") or "").strip()
        if dataset:
            reads.append({"dataset": dataset, **_link_check(root, script, parse, dataset, "read")})
    if reads:
        checked["reads"] = reads
    if field_name is not None:
        checked["field"] = _field_check(root, output, field_name)
    observed: dict[str, Any] = {}
    if output is not None:
        observed["output"] = {"path": output, **fingerprint(root, output)}
    inputs = [
        {"dataset": d, **fingerprint(root, d, full=True)}
        for d in ((i.get("dataset") or "").strip() for i in content.get("input") or []) if d
    ]
    if inputs:
        observed["inputs"] = inputs
    return _Inspection(checked, observed, script_bytes, copy)


def _code_fingerprint(script: str, data: bytes, impl: Mapping[str, Any]) -> dict[str, Any]:
    """The hash of the script's code as stage (b) compares it (comments and
    blank lines removed; the named chunk or function only), recorded beside
    the raw hash so a comparison still works when the code copy is gone.
    Nothing when the code cannot be read that way (no outcome blocks)."""
    from rce.records import implementation  # noqa: PLC0415

    chunk, function = implementation.region_of(impl)
    try:
        code = implementation.code_hash(data.decode("utf-8-sig"), Path(script).suffix, chunk=chunk, function=function)
    except (implementation.Unparseable, implementation.RegionMissing, UnicodeDecodeError):
        return {}
    region = implementation.region_text(chunk, function)
    return {"code": code, **({"region": region} if region else {})}


def _pin_upstream(root: Path, card: V.Card, content: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Each `variable = "<id>@v<n>"` input as the reference it relies on
    (only a confirmed version can be referred to)."""
    pinned = []
    for i, item in enumerate(content.get("input") or [], start=1):
        text = (item.get("variable") or "").strip()
        if not text:
            continue
        ref = V.resolve_text(root, text)
        if ref is None:
            raise CardRefused(
                "unresolvable", f"input {i} names {text!r}, which is not a confirmed version that can be referred to",
                message_zh=V.UNRESOLVABLE,
            )
        if V.card_key(ref.variable) == card.key:
            raise CardRefused("invalid", f"input {i} names this variable itself")
        pinned.append({"variable": ref.variable, "version": ref.version, "entry": ref.entry, "content": ref.content,
                       "ref": ref.label})
    return pinned or None


def _write_copies(w: _Writing, view_bytes: bytes, content_h: str, ins: _Inspection, fault: Fault | None) -> str:
    """Step 4: the code copy, then the frozen copy, each durable, read back
    and verified. Returns the frozen copy's path relative to the card."""
    root, card = w.root, w.card
    if ins.script_bytes is not None and ins.script_copy is not None:
        code = root / paths.RCE_DIRNAME / V.VARIABLES_DIRNAME / ins.script_copy
        _copy_dir(root, code.parent)
        existing = files.read_record(code)
        if existing.state is not RecordState.PRESENT or _sha256(existing.data or b"") != _sha256(ins.script_bytes):
            files.durable_write(code, ins.script_bytes)
        _read_back(code, ins.script_bytes)
        if fault is not None:
            fault("after_code_copy")
    frozen_dir = _copy_dir(root, card.directory / V.FROZEN_DIRNAME)
    hexd = content_h.removeprefix(V.CONTENT_PREFIX)
    target = frozen_dir / f"{hexd}.toml"
    existing = files.read_record(target)
    if existing.state is RecordState.PRESENT and existing.data != view_bytes:
        # Same content, other bytes (another version with different
        # comments): keep both, never overwrite a frozen copy.
        target = frozen_dir / f"{hexd}-{_sha256(view_bytes)[:12]}.toml"
        existing = files.read_record(target)
    if existing.state is not RecordState.PRESENT:
        files.durable_write(target, view_bytes)
    _read_back(target, view_bytes)
    got = files.read_record(target)
    if V.content_hash(V.parse_version(got.text or "")) != content_h:
        raise CardRefused("changed", f"{target} does not hold the confirmed content; nothing recorded")
    if fault is not None:
        fault("after_frozen_copy")
    return f"{V.FROZEN_DIRNAME}/{target.name}"


# -- confirm -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Confirmed:
    card: V.Card
    entry: LedgerEntry
    reference: V.Reference


def confirm(
    project_root: str | Path,
    card_id: str,
    *,
    attested: str,
    expected_content: str | None | object = ANY_CONTENT,
    via: str = "cli",
    expected_id: Any = READ_NOW,
    timeout: float | None = None,
    now: Clock | None = None,
    fault: Fault | None = None,
) -> Confirmed:
    """Confirm the card's draft (module docstring, "snapshot first, entry
    last"). `attested` is the researcher's answer to "was the output as it
    stands built with this definition" -- asked by every surface, never
    defaulted here; RCE never sets it from a hash. `expected_content` ties
    the confirmation (and that answer) to the draft the researcher was
    shown (9.12, "an answer belongs to the question that was shown"): its
    content hash, or None for a draft that could not be read; if the draft
    differs now, `question_changed` and nothing is written."""
    if attested not in V.ATTESTED:
        raise CardRefused("invalid", f"attested must be one of {', '.join(V.ATTESTED)}, got {attested!r}")
    with _writing(project_root, card_id, expected_id, timeout) as w:
        card = w.card
        if card.draft is None:
            orphans = [n for n, v in card.versions.items() if v.status == "orphan_draft"]
            extra = f" (v{orphans[0]}.toml is unconfirmed but not the highest version)" if orphans else ""
            raise CardRefused("no_draft", f"{card.id} has no draft to confirm{extra}; 'rce variable revise' opens one")
        view = card.versions[card.draft]
        _same_as_shown(view, expected_content)
        data, content = _version_now(view)
        missing = V.missing_for_confirm(content)
        if missing:
            raise CardRefused("incomplete", f"v{view.number} cannot be confirmed yet; it lacks: {', '.join(missing)}")
        upstream = _pin_upstream(w.root, card, content)
        ins = inspect(w.root, content)
        h = V.content_hash(content)
        frozen = _write_copies(w, data, h, ins, fault)
        entry = _append(w, [{
            "act": "confirmed", "version": view.number, "via": via, "attested": attested, "content": h,
            "frozen": frozen, "upstream": upstream, "checked": ins.checked, "observed": ins.observed or None,
        }], now=now)[0]
        if fault is not None:
            fault("after_entry")
        ref = V.Reference(card.id, view.number, entry.id, h)
    return Confirmed(V.read_card(w.root, card.directory), entry, ref)


def _same_as_shown(view: V.VersionView, expected: str | None | object) -> None:
    """9.12: an answer belongs to the question that was shown. The version
    file must still hold the content (hash) the page showed."""
    if expected is ANY_CONTENT:
        return
    if view.content_hash != expected:
        raise CardRefused(
            "question_changed", f"v{view.number}.toml changed since it was shown; look at it again -- nothing written",
            message_zh=f"v{view.number} 的文件在你查看之后又被改动了，请重新查看后再回答",
        )


def _version_now(view: V.VersionView) -> tuple[bytes, dict[str, Any]]:
    got = files.read_record(view.path)
    if got.state is RecordState.ABSENT:
        raise CardRefused("invalid", f"{view.path.name} is not there")
    if got.state is not RecordState.PRESENT:
        raise CardRefused("invalid", f"{view.path.name} cannot be read now ({got.state.value}: {got.error})")
    try:
        content = V.parse_version(got.text or "")
    except V.VersionInvalid as exc:
        raise CardRefused("invalid", f"{view.path.name}: {exc}") from exc
    return got.data or b"", content


# -- the edited-in-place question ----------------------------------------------------------------


@dataclass(frozen=True)
class EditAnswered:
    answer: str
    version: int
    new_version: int | None = None
    entry: LedgerEntry | None = None


def _question_version(card: V.Card, version: int | None) -> V.VersionView:
    if version is None:
        if len(card.questions) != 1:
            if not card.questions:
                raise CardRefused("no_question", f"no confirmed version of {card.id} was changed after its confirmation")
            raise CardRefused("invalid", f"versions {', '.join(f'v{n}' for n in card.questions)} were changed; name one")
        version = card.questions[0]
    view = card.versions.get(version)
    if view is None or not view.question:
        raise CardRefused("no_question", f"v{version} of {card.id} was not changed after its confirmation")
    return view


def answer_edited(
    project_root: str | Path,
    card_id: str,
    answer: str,
    *,
    version: int | None = None,
    expected_content: str | None | object = ANY_CONTENT,
    via: str = "cli",
    expected_id: Any = READ_NOW,
    timeout: float | None = None,
    now: Clock | None = None,
    fault: Fault | None = None,
) -> EditAnswered:
    """Answer 「v<n> 的定义在确认后被改动了」 (or, for a file that is gone,
    「v<n> 的版本文件在确认后不见了」). `expected_content` ties the answer to
    the file the question was raised for (`_same_as_shown`).

    「另存为新版本」 (`new`): the edited bytes -- whatever they are, even a
    file no longer valid or not UTF-8 -- become draft v<next> (created
    exclusively, read back), and only then is v<n>.toml put back byte for
    byte from its frozen copy. Refused while another draft is open, or when
    the frozen copy is missing (「确认记录引用的副本缺失」). A draft that
    already holds exactly the edited bytes is the same answer interrupted
    after its first write: it is finished, not refused. A file that is gone
    has no edit to keep: it is put back from its frozen copy.

    「这是更正」 (`correct`): a `corrected` entry with both hashes, the checks
    made again, and a new frozen copy written first -- only for a file that
    reads as a version (there is nothing else to record as the wording)."""
    if answer not in (V.ANSWER_NEW, V.ANSWER_CORRECT):
        raise CardRefused("invalid", f"answer must be {V.ANSWER_NEW} or {V.ANSWER_CORRECT}, got {answer!r}")
    with _writing(project_root, card_id, expected_id, timeout) as w:
        card = w.card
        view = _question_version(card, version)
        _same_as_shown(view, expected_content)
        if answer == V.ANSWER_NEW:
            frozen = _verified_frozen(card, view)
            if frozen is None:
                raise CardRefused("copy_missing", f"the frozen copy of v{view.number} is missing; it cannot be put back",
                                  message_zh=V.COPY_MISSING)
            if view.question_kind == "absent":
                _create_exclusively(view.path, frozen)
                _read_back(view.path, frozen)
                return EditAnswered(answer, view.number)
            edited = files.read_record(view.path)
            if edited.data is None:
                raise CardRefused("invalid", f"{view.path.name} cannot be read now ({edited.state.value}: {edited.error})")
            draft = card.versions.get(card.draft) if card.draft is not None else None
            if draft is not None and draft.data == edited.data:
                number = draft.number  # saved by this same answer before it was interrupted
            elif draft is not None:
                raise CardRefused("draft_open", f"draft v{card.draft} is open; confirm it first, then answer again",
                                  message_zh=f"已有草稿 v{card.draft}，请先确认它再回答")
            else:
                number = card.next_number
                target = card.directory / f"v{number}.toml"
                _create_exclusively(target, edited.data)
                _read_back(target, edited.data)
            if fault is not None:
                fault("after_new_version")
            again = files.read_record(view.path)
            if again.data != edited.data:
                raise CardRefused("changed", f"{view.path.name} changed while answering; it was not put back "
                                  f"(the earlier edit is kept in v{number}.toml)")
            files.durable_write(view.path, frozen)
            _read_back(view.path, frozen)
            return EditAnswered(answer, view.number, new_version=number)
        data, content = _version_now(view)
        missing = V.missing_for_confirm(content)
        if missing:
            raise CardRefused("incomplete", f"the corrected v{view.number} lacks: {', '.join(missing)}")
        upstream = _pin_upstream(w.root, card, content)
        ins = inspect(w.root, content)
        h = V.content_hash(content)
        frozen_rel = _write_copies(w, data, h, ins, fault)
        entry = _append(w, [{
            "act": "corrected", "version": view.number, "via": via, "content": h,
            "previous": str(view.entry.get("content")), "corrects": view.entry.id, "frozen": frozen_rel,
            "upstream": upstream, "checked": ins.checked, "observed": ins.observed or None,
        }], now=now)[0]
        return EditAnswered(answer, view.number, entry=entry)


# -- stage (b): the implementation moved under a confirmed version ------------------------------


def _write_code_copy(w: _Writing, data: bytes, copy: str) -> None:
    """A code copy, written durably and read back (snapshot first, entry last)."""
    code = w.root / paths.RCE_DIRNAME / V.VARIABLES_DIRNAME / copy
    _copy_dir(w.root, code.parent)
    existing = files.read_record(code)
    if existing.state is not RecordState.PRESENT or existing.data != data:
        files.durable_write(code, data)
    _read_back(code, data)


def reaffirm(
    project_root: str | Path,
    card_id: str,
    *,
    version: int,
    signature: str,
    data_version: str | None = None,
    note: str | None = None,
    via: str = "cli",
    expected_id: Any = READ_NOW,
    timeout: float | None = None,
    now: Clock | None = None,
    fault: Fault | None = None,
) -> LedgerEntry:
    """「口径未变」: the researcher's answer that the definition in use still
    holds after its implementation moved (9.11 stage (b)). Appends one
    `reaffirmed` entry with the new fingerprints -- the script's raw and
    code hashes and a fresh code copy (written and verified first), each
    input's fingerprint -- the coverage they were taken under, the reasons
    answered, and, after an input change, the researcher's new
    `data_version` note (required then).

    `signature` ties the answer to the comparison that was shown (9.12): if
    the files moved again since, `question_changed`, nothing written."""
    from rce.records import implementation  # noqa: PLC0415

    if note is not None and not isinstance(note, str):
        raise CardRefused("invalid", "note must be text")
    with _writing(project_root, card_id, expected_id, timeout) as w:
        card = w.card
        if card.in_use != version:
            raise CardRefused("no_question", f"v{version} of {card.id} is not the version in use; nothing to answer")
        result = implementation.compare(w.root, card, implementation.load_cache(w.conn))
        if result is None or not result["reasons"]:
            raise CardRefused("no_question", f"the implementation of {card.id} v{version} has not moved since it was "
                              "confirmed or last reaffirmed; there is nothing to answer")
        if result["signature"] != signature:
            raise CardRefused("question_changed", f"the implementation of {card.id} changed again since the question "
                              "was shown; look again")
        reasons = list(result["reasons"])
        if set(reasons) & {implementation.CHUNK_MISSING, implementation.FUNCTION_MISSING}:
            # The region the version names is not in the script: a
            # reaffirmation could never settle that (the next comparison
            # looks for it again). The card's text names it -- corrected
            # there (「这是更正」), or the definition moves on (「口径已变」).
            raise CardRefused(
                "region_missing", f"the chunk or function {card.id} v{version} names cannot be found; correct the card "
                "('chunk'/'function', then answer 'correct') or open the next draft",
                message_zh="找不到这一版指定的代码块或函数，无法记为口径未变：请在卡片里改正 chunk / function 后回答「这是更正」，或选择「口径已变」",
            )
        if implementation.INPUT_CHANGED in reasons and not (data_version or "").strip():
            raise CardRefused("incomplete", "the input data changed: write the new data_version note",
                              message_zh="输入数据变了：请写下新的数据版本说明")
        script = result["script"]
        fields: dict[str, Any] = {
            "act": "reaffirmed", "version": version, "via": via, "reaffirms": result["baseline"],
            "reasons": reasons,
            "data_version": (data_version or "").strip() or None,
            "note": (note or "").strip() or None,
        }
        coverage: dict[str, Any] = {}
        now_script = script.get("now")
        if now_script and script.get("state") in ("same", "changed"):
            _write_code_copy(w, script["bytes"], now_script["copy"])
            if fault is not None:
                fault("after_code_copy")
            fields["checked"] = {"script": dict(now_script)}
            coverage["script"] = implementation.RECORDED_REGION if script.get("region") else implementation.RECORDED_FULL
        inputs = []
        input_coverage = []
        for item in result["inputs"]:
            fp = item.get("now")
            if not fp:
                continue
            # A hash 「完整比对」 took at this size and mtime is the one the
            # answer was given about (it is in the comparison's signature).
            fp = {k: v for k, v in fp.items() if k != "remembered"}
            inputs.append(fp)
            input_coverage.append({"dataset": item["dataset"], "coverage": implementation.RECORDED_FULL if "sha256" in fp
                                   else implementation.RECORDED_SIZE})
        if inputs:
            fields["observed"] = {"inputs": inputs}
            coverage["inputs"] = input_coverage
        fields["coverage"] = coverage or None
        return _append(w, [fields], now=now)[0]


def full_compare(project_root: str | Path, card_id: str, *, expected_id: Any = READ_NOW,
                 timeout: float | None = None) -> dict[str, Any] | None:
    """「完整比对」: hash the card's large inputs now and compare them by
    content (9.11). Writes the index only (its comparison and its cache of
    hashes), under the project lock; returns the card's comparison."""
    from rce.records import implementation  # noqa: PLC0415

    root = Path(project_root)
    with write_guard(root, expected_id, human=False, timeout=timeout):
        identity = _identity_now(root)
        if identity is None:
            raise CardRefused("untrusted", f"{root} has no readable project identity", message_zh=V.MESSAGES["no_identity"])
        conn = _open_index(identity)
        try:
            found = V.find_card_dirs(root, card_id)
            if not found:
                raise CardRefused("no_such_card", f"there is no variable card {card_id!r}")
            apply_cards(conn, root, identity=identity, full_for={V.card_key(found[0].name)})
            return implementation.stored(conn).get(found[0].name)
        finally:
            conn.close()


# -- abandon, revive -----------------------------------------------------------------------------


def _card_act(act: str, project_root: str | Path, card_id: str, note: str, *, via: str, expected_id: Any,
              timeout: float | None, now: Clock | None) -> LedgerEntry:
    if not isinstance(note, str) or not note.strip():
        raise CardRefused("invalid", f"{act} needs the reason, in your words (--note)")
    with _writing(project_root, card_id, expected_id, timeout) as w:
        if act == "abandoned" and w.card.abandoned is not None:
            raise CardRefused("already_abandoned", f"{w.card.id} is already abandoned")
        if act == "revived" and w.card.abandoned is None:
            raise CardRefused("not_abandoned", f"{w.card.id} is not abandoned")
        return _append(w, [{"act": act, "via": via, "note": note.strip()}], now=now)[0]


def abandon(project_root: str | Path, card_id: str, *, note: str, via: str = "cli", expected_id: Any = READ_NOW,
            timeout: float | None = None, now: Clock | None = None) -> LedgerEntry:
    """Record, in the researcher's words and with a date, why a variable
    died: no version is in use while abandoned; every version still resolves."""
    return _card_act("abandoned", project_root, card_id, note, via=via, expected_id=expected_id, timeout=timeout, now=now)


def revive(project_root: str | Path, card_id: str, *, note: str, via: str = "cli", expected_id: Any = READ_NOW,
           timeout: float | None = None, now: Clock | None = None) -> LedgerEntry:
    return _card_act("revived", project_root, card_id, note, via=via, expected_id=expected_id, timeout=timeout, now=now)


# -- two histories: settled by naming what stands (9.12) -------------------------------------------


@dataclass(frozen=True)
class Settled:
    entry: LedgerEntry
    keeps: tuple[str, ...]
    not_in_force: tuple[str, ...]
    card: V.Card


def settle(
    project_root: str | Path,
    card_id: str,
    keeps: Iterable[str],
    *,
    expected_shown: Iterable[str] | None = None,
    note: str | None = None,
    via: str = "cli",
    expected_id: Any = READ_NOW,
    timeout: float | None = None,
    now: Clock | None = None,
) -> Settled:
    """「以这一条为准」 (`rce variable settle <id> --keep <entry id>...`,
    DESIGN.md 9.12): a card whose log holds two merged histories is settled
    by naming, for each version number in dispute, the one confirmation
    that stands. Appends one `settled` entry -- `settles` (the anomalous
    entries, assigned by the ledger engine) and `keeps` -- after which the
    card is no longer frozen, in-use and superseded are derived from what
    stands, and every other entry of the two histories stays in the file,
    not in force. Nothing is chosen by position or time: a disputed version
    left without a kept entry, or given two, is refused; so is an entry
    that is not a confirmation in dispute. `expected_shown` ties the answer
    to the entries the question showed (9.12, "an answer belongs to the
    question that was shown")."""
    wanted = list(keeps)
    if len(set(wanted)) != len(wanted):
        raise CardRefused("invalid", "an entry is named twice in --keep")
    with _writing(project_root, card_id, expected_id, timeout, allow_conflict=True) as w:
        card = w.card
        dispute = V.dispute_of(card)
        if dispute is None:
            raise CardRefused("no_conflict", f"the log of {card.id} holds no unsettled second history; nothing to settle",
                              message_zh="这张卡的记录里没有需要处理的两份历史")
        if expected_shown is not None and sorted(set(expected_shown)) != list(dispute.shown):
            raise CardRefused("question_changed", f"the log of {card.id} changed since the question was shown; "
                              "look at it again -- nothing written",
                              message_zh="记录文件在你查看之后又变了，请重新查看后再选择")
        chosen: dict[int, str] = {}
        for entry_id in wanted:
            number = dispute.version_of(entry_id)
            if number is None:
                raise CardRefused("invalid", f"{entry_id} is not a confirmation in dispute in {card.id}'s log "
                                  f"(the disputed versions: {', '.join(f'v{n}' for n in dispute.disputed)})",
                                  message_zh="选中的记录不是有争议的那几条确认之一")
            if number in chosen:
                raise CardRefused("invalid", f"v{number} is given two entries that stand ({chosen[number]}, {entry_id}); "
                                  "name exactly one", message_zh=f"v{number} 只能以一条确认为准")
            chosen[number] = entry_id
        left = [n for n in dispute.disputed if n not in chosen]
        if left:
            raise CardRefused("invalid", f"name the entry that stands for {', '.join(f'v{n}' for n in left)} too "
                              "(nothing is chosen by position or by time)",
                              message_zh="每个有争议的版本都要选一条为准：" + "、".join(f"v{n}" for n in left))
        kept = tuple(chosen[n] for n in sorted(chosen))
        fields: dict[str, Any] = {"act": V.SETTLED_ACT, "keeps": list(kept), "via": via}
        if note:
            fields["note"] = note
        entry = _append(w, [fields], now=now)[0]
        after = V.read_card(w.root, card.directory)
    return Settled(entry, kept, tuple(sorted(after.not_in_force)), after)


def dispute_payload(card: V.Card) -> dict[str, Any] | None:
    """The question a conflicted card asks, for the CLI (`--json`) and the
    page: what both histories share, each history, and for each version in
    dispute the confirmations that may stand."""
    dispute = V.dispute_of(card)
    if dispute is None:
        return None

    def item(e: LedgerEntry) -> dict[str, Any]:
        return {**_summary(e.data), "attested": V.jsonable(e.get("attested")) if e.get("act") == "confirmed" else None}

    return {
        "anomalies": list(dispute.anomalies),
        "common": [item(e) for e in dispute.common],
        "branches": [[item(e) for e in b] for b in dispute.branches],
        "versions": [{"version": n, "candidates": [e.id for e in dispute.candidates[n]]} for n in dispute.disputed],
        "shown": list(dispute.shown),
    }


# -- the SHRUNK question for a card's log (9.3) ----------------------------------------------------


@dataclass(frozen=True)
class ShrunkAnswered:
    answer: str
    missing: tuple[Mapping[str, Any], ...]
    appended: tuple[LedgerEntry, ...] = ()
    restored_copies: tuple[str, ...] = ()


def answer_shrunk(
    project_root: str | Path,
    card_id: str,
    answer: str,
    *,
    expected_missing: Iterable[str] | None = None,
    via: str = "cli",
    expected_id: Any = READ_NOW,
    timeout: float | None = None,
    now: Clock | None = None,
) -> ShrunkAnswered:
    """Answer 「变量卡的记录文件比图谱少了 N 条记录」 for one card.

    「以文件为准」 (`file`): the missing entries are dropped from the index's
    copy -- refused (`would_lose`) when the file as it stands could still
    not be applied. Every lost confirmation or correction whose wording the
    file no longer holds (no entry with that version AND that content)
    leaves one `removed` entry behind, naming it (`removes`), with its
    content hash and frozen copy -- so the wording stays readable and
    `rce records --clean` keeps the copy. That holds when another history's
    entry now holds the same number (two Macs each confirmed a `v2`): the
    file's `v2` stands, and the lost one's text is still kept. A number
    that only the lost entries used is not given to the next revision. A
    card whose directory is gone is forgotten by the index.

    「把缺少的补回文件」 (`restore`): missing frozen copies are put back from
    the index's copy first (verified against their hash), then every
    missing entry is appended again in one write, `via = "recovered"`.

    `expected_missing` ties the answer to the question shown (9.12)."""
    if answer not in ("file", "restore"):
        raise CardRefused("invalid", f"answer must be file or restore, got {answer!r}")
    with _writing(project_root, card_id, expected_id, timeout, allow_shrunk=True) as w:
        decision = w.decision
        if decision.verdict is not Trust.SHRUNK:
            raise CardRefused("no_question", f"the log of {w.card.id} has not lost entries; there is nothing to answer")
        missing = decision.missing
        ids = [str(m["id"]) for m in missing]
        if expected_missing is not None and sorted(set(map(str, expected_missing))) != sorted(ids):
            raise CardRefused("question_changed", f"the log of {w.card.id} changed since the question was shown; look again")
        card = w.card
        if answer == "file":
            if not card.directory.is_dir():
                db.forget_variable_card(w.conn, card.key)
                return ShrunkAnswered(answer, tuple(missing))
            remaining = {k: v for k, v in applied_copy(w.conn, card).items() if k not in set(ids)}
            after = assess_ledger(w.identity, card.log, remaining, expected=card.log_expected)
            if not after.may_apply:
                raise CardRefused(
                    "would_lose",
                    f"taking log.toml as it is would leave a card RCE cannot apply ({after.reason}); the "
                    f"{len(ids)} missing entr(y/ies) exist only in the index's copy -- restore the file, or answer restore",
                    decision=after,
                )
            numbers = {int(e.get("version")) for e in card.entries if e.get("act") in V.VERSION_ACTS}
            kept = {
                (int(e.get("version")), str(e.get("content")))
                for e in card.entries if e.get("act") in V.VERSION_ACTS and e.get("content")
            }
            lost = [
                m for m in missing
                if m.get("act") in ("confirmed", "corrected") and isinstance(m.get("version"), int)
                and (int(m["version"]), str(m.get("content"))) not in kept
            ]
            # The text of a version the file no longer holds stays readable:
            # its frozen copy is put back from the index's copy (RCE's own
            # file, verified against its hash) and named by the entry below.
            _restore_frozen(w, lost)
            removed: list[dict[str, Any]] = []
            for m in lost:
                n, content = int(m["version"]), str(m.get("content"))
                if any(r["version"] == n and r["content"] == content for r in removed):
                    continue
                note = (f"以文件为准：这条 v{n} 的记录不在文件里（文件里的 v{n} 是另一份记录），当时的文字保留在冻结副本中"
                        if n in numbers else "以文件为准：确认记录不在文件里，此编号不再使用")
                entry: dict[str, Any] = {"act": "removed", "version": n, "via": via, "content": content,
                                         "removes": str(m["id"]), "note": note}
                if isinstance(m.get("frozen"), str) and (card.directory / m["frozen"]).is_file():
                    entry["frozen"] = m["frozen"]
                removed.append(entry)
            appended = _append(w, removed, now=now) if removed else []
            db.forget_variable_card(w.conn, card.key, ids)
            return ShrunkAnswered(answer, tuple(missing), tuple(appended))
        restored = _restore_frozen(w, missing)
        batch = []
        for old in missing:
            fields = {k: v for k, v in old.items() if k not in ("id", "seq", "at", "settles", "via")}
            fields["via"] = "recovered"
            fields["recovered_from"] = str(old["id"])
            if old.get("at") is not None:
                fields["recovered_at"] = str(V.jsonable(old["at"]))
            batch.append(fields)
        if not card.directory.is_dir():
            files.ensure_dir_within(w.root, card.directory)
        appended = _append(w, batch, now=now)
        db.forget_variable_card(w.conn, card.key, ids)
        return ShrunkAnswered(answer, tuple(missing), tuple(appended), tuple(restored))


def _restore_frozen(w: _Writing, missing: Iterable[Mapping[str, Any]]) -> list[str]:
    copy = (db.variable_cards(w.conn).get(w.card.key) or {}).get("data") or {}
    texts = copy.get("frozen") or {}
    restored = []
    for m in missing:
        rel = m.get("frozen")
        if not isinstance(rel, str) or rel not in texts:
            continue
        target = w.card.directory / rel
        if target.exists():
            continue
        data = texts[rel].encode("utf-8")
        try:
            ok = V.content_hash(V.parse_version(texts[rel])) == m.get("content")
        except V.VersionInvalid:
            ok = False
        if not ok:
            continue
        _copy_dir(w.root, target.parent)
        files.durable_write(target, data)
        _read_back(target, data)
        restored.append(rel)
    return restored


# -- dead variables (9.11) ---------------------------------------------------------------------------


def _aliases(card: V.Card) -> list[str]:
    for number in sorted(card.versions, reverse=True):
        view = card.versions[number]
        if view.entry is not None and view.content is not None and not view.question:
            return list(view.content.get("aliases") or [])
    if card.draft is not None and card.versions[card.draft].content is not None:
        return list(card.versions[card.draft].content.get("aliases") or [])
    return []


def dead_variable_flags(project_root: str | Path, cards: list[V.Card] | None = None) -> list[dict[str, Any]]:
    """The disagreement, in either direction, between abandoned cards and
    `dead_variables` in `.rce/attempts.toml` -- matched through `aliases`
    with the existing rule (a dead entry is a case-insensitive substring).
    `attempts.toml` stays the only input to the revived-dead-variable check;
    nothing here changes it. [] when there is no usable attempts config."""
    from rce.ingest import attempts as attempts_ingest  # noqa: PLC0415

    root = Path(project_root)
    try:
        config = attempts_ingest.load_config(root)
    except attempts_ingest.AttemptsConfigError:
        return []
    dead = list(config.dead_variables or [])
    flags = []
    for card in cards if cards is not None else V.read_cards(root):
        if not card.readable:
            continue
        aliases = _aliases(card)
        matched = [d for d in dead if any(d.lower() in a.lower() for a in aliases)]
        if card.abandoned is not None and not matched:
            flags.append({"card": card.id, "direction": "card_only", "message": DEAD_CARD_ONLY, "dead": []})
        elif card.abandoned is None and matched:
            flags.append({"card": card.id, "direction": "attempts_only", "message": DEAD_ATTEMPTS_ONLY, "dead": matched})
    return flags


# -- what readers show --------------------------------------------------------------------------------


def _frozen_content(card: V.Card, view: V.VersionView) -> dict[str, Any] | None:
    data = _verified_frozen(card, view)
    return None if data is None else V.parse_version(data.decode("utf-8").removeprefix("\ufeff"))


def question_text(view: V.VersionView) -> str | None:
    if not view.question:
        return None
    template = {"absent": V.QUESTION_ABSENT, "unreadable": V.QUESTION_UNREADABLE}.get(view.question_kind or "",
                                                                                   V.QUESTION_EDITED)
    return template.format(n=view.number)


def question_answers(view: V.VersionView) -> tuple[list[str], dict[str, str]]:
    """The answers that can be given: 「这是更正」 only for a file that reads
    as a version; a file that is gone can only be put back (「按冻结副本放
    回」); one that cannot be read can only have its bytes kept as the next
    draft and the frozen text put back (9.12)."""
    if view.question_kind == "absent":
        return [V.ANSWER_NEW], dict(V.ABSENT_ANSWER_LABELS)
    if view.question_kind == "unreadable":
        return [V.ANSWER_NEW], dict(V.UNREADABLE_ANSWER_LABELS)
    if view.question_kind == "edited":
        return [V.ANSWER_NEW, V.ANSWER_CORRECT], dict(V.ANSWER_LABELS)
    return [V.ANSWER_NEW], {V.ANSWER_NEW: V.ANSWER_LABELS[V.ANSWER_NEW]}


def version_payload(root: Path, card: V.Card, view: V.VersionView) -> dict[str, Any]:
    entry = view.entry
    # The definition in force: for a version edited after its confirmation,
    # or whose file is gone or unreadable, the frozen text -- an edit is
    # shown, not obeyed.
    in_force = _frozen_content(card, view) if (view.question or (view.content is None and entry is not None)) else view.content
    out: dict[str, Any] = {
        "version": view.number,
        "file": view.path.name,
        "file_state": view.file_state,
        "error": view.error,
        "line": view.line,
        "status": view.status,
        "label": V.DRAFT_LABEL if view.status == "draft" else V.UNKNOWN_STATUS_LABEL if view.status == "unknown" else None,
        "content": in_force,
        "content_from_frozen": in_force is not None and in_force is not view.content,
        "edited_content": view.content if view.question else None,
        "content_hash": view.content_hash,
        "question": question_text(view),
        "question_kind": view.question_kind,
        "copy_missing": V.COPY_MISSING if (view.copy_missing or view.code_missing) else None,
        "frozen_missing": view.copy_missing,
        "code_missing": view.code_missing,
        "superseded_by": None if view.superseded_by is None else {"version": view.superseded_by[0], "at": view.superseded_by[1]},
        "upstream_notes": V.upstream_notes(root, entry),
    }
    if entry is not None:
        confirmed = view.confirmed
        out.update(
            entry=_summary(entry.data),
            confirmed_at=(confirmed or entry).at,
            # The attestation is the researcher's answer AT CONFIRMATION, about
            # the confirmed text and the output then on disk; it is shown with
            # that confirmation's own checks and observations, never beside a
            # correction's (9.11: RCE never attaches it to anything else).
            attested=(confirmed.get("attested", "unknown") if confirmed is not None else None),
            attested_at=(confirmed.at if confirmed is not None else None),
            attested_checked=V.jsonable(confirmed.get("checked") or {}) if confirmed is not None else {},
            attested_observed=V.jsonable(confirmed.get("observed") or {}) if confirmed is not None else {},
            # The checks and observations of the entry in force, and WHEN they
            # were made: at the confirmation, or at the correction.
            checked_act=entry.get("act"),
            checked_at=entry.at,
            checked=V.jsonable(entry.get("checked") or {}),
            observed=V.jsonable(entry.get("observed") or {}),
            reference=V.Reference(card.id, view.number, entry.id, str(entry.get("content"))).payload(),
        )
    return out


def card_payload(project_root: str | Path, card: V.Card, decision: TrustDecision | None = None,
                 dead: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One card as the CLI (`--json`) and a later app view read it."""
    root = Path(project_root)
    out: dict[str, Any] = {
        "id": card.id,
        "state": card.state,
        "reason": card.reason,
        "message": card.message,
        "detail": card.detail,
        "problems": list(card.problems),
        "in_use": card.in_use,
        "draft": card.draft,
        "next_version": card.next_number if card.readable else None,
        "abandoned": None if card.abandoned is None else _summary(card.abandoned.data),
        "questions": [
            {"version": n, "kind": card.versions[n].question_kind, "message": question_text(card.versions[n]),
             "content_hash": card.versions[n].content_hash,
             "answers": question_answers(card.versions[n])[0], "answer_labels": question_answers(card.versions[n])[1]}
            for n in card.questions
        ],
        "versions": [version_payload(root, card, card.versions[n]) for n in sorted(card.versions)],
        "history": [{**_summary(e.data), "in_force": e.id not in card.not_in_force} for e in card.entries],
        "dispute": dispute_payload(card) if card.reason == "conflict" else None,
        "dead_flags": [f for f in (dead or []) if f["card"] == card.id],
    }
    if decision is not None:
        out["trust"] = {"state": decision.verdict.value, "reason": decision.reason, "message": _message(decision),
                        "detail": decision.detail, "missing": [_summary(m) for m in decision.missing]}
    return out


def overview(conn: Connection | None, project_root: str | Path) -> list[dict[str, Any]]:
    """Every card (and every card the index knows whose directory is gone),
    with its trust decision against the index when there is one."""
    root = Path(project_root)
    identity = _identity_now(root)
    cards = _cards_in_view(conn, root) or []
    dead = dead_variable_flags(root, cards)
    return [card_payload(root, c, assess_card(conn, root, c, identity), dead) for c in cards]
