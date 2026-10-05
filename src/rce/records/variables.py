"""Variable definition cards (DESIGN.md 9.11; task V5 phase 8): the files,
and how a card is read -- with no index at all.

Layout: what the researcher writes and what RCE records never share a file
---------------------------------------------------------------------------

    .rce/variables/<id>/v<n>.toml        the researcher's text for version n
    .rce/variables/<id>/log.toml         appended by RCE: a 9.3 ledger
    .rce/variables/<id>/frozen/<h>.toml  a byte copy of each version as confirmed
    .rce/variables/_code/<h>.<ext>       a copy of each implementing script

The directory name is the id. Ids compare case-folded (and NFC-normalized):
two directories equal up to case, or a sync conflict copy beside ANY file
of a card, make THAT card unreadable until one is removed; every other card
keeps working.

The version file is the researcher's
------------------------------------

RCE never writes into a `v<n>.toml` (the one exception the design itself
names: 「另存为新版本」 puts a confirmed version's text back from its frozen
copy, byte for byte, after the edited text is safe in the next file --
`rce.records.cards.answer_edited`). It is read strictly: the keys of
9.11's example and nothing else -- plus `chunk` and `function` under
`[implementation]`, which 9.11 names for narrowing a comparison -- so a
line that slid under the wrong heading is an error naming its line. Text
may not hold control characters other than a newline. A draft may be
incomplete; `missing_for_confirm` says what a confirmation needs.

The log is a 9.3 ledger (`rce.records.ledger`, not a fork of it)
---------------------------------------------------------------

Acts: confirmed, reaffirmed, corrected, abandoned, revived, removed,
settled. Every entry speaks about the one card, so the ledger key is empty:
two merged histories (seq repeating) put the whole card in conflict, until
a `settled` entry names what stands (9.12: `keeps`, one confirmation per
version number in dispute; the other entries of both histories stay in the
file, not in force -- nothing is chosen by position or time). A confirmed
version is frozen by the content hash in its own entry: on every read, a
version file whose parsed content differs from it is not obeyed and the
card asks 「v<n> 的定义在确认后被改动了」 -- comments and layout are free, so
editing only those raises nothing. All of this is computed from the files
alone, so it holds after a rebuild, on another Mac, after a restore.

What the index adds (the shrink question of 9.3, for a log that lost
entries the index applied) is `rce.records.cards`' job; this module never
opens the index.

References (9.11)
-----------------

A result points at one confirmed text: `Reference(variable, version,
entry, content)`, written `topicshift@v2·9c1f2a3b`. `resolve` succeeds only
when the card's log holds that entry with that hash; otherwise the
reference is 「引用暂不可解析」 -- it is never re-pointed at whatever is
called `v2` now.
"""

from __future__ import annotations

import functools
import hashlib
import json
import posixpath
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping

from rce import paths
from rce.records import files
from rce.records import ledger as ledger_mod
from rce.records.files import RecordState
from rce.records.ledger import LedgerEntry, LedgerLoad, LedgerSchema

VARIABLES_DIRNAME = "variables"
CODE_DIRNAME = "_code"
FROZEN_DIRNAME = "frozen"
LOG_FILENAME = "log.toml"
#: Snapshots of a card's files go to `.rce/backups/variables/<id>/` (9.11).
SNAPSHOT_SUBDIR = VARIABLES_DIRNAME
#: Above this a file is fingerprinted by size and mtime, not hashed (9.11).
LARGE_FILE_BYTES = 50 * 1024 * 1024

VERSION_FILE_RE = re.compile(r"^v([1-9][0-9]*)\.toml$")
CONTENT_PREFIX = "sha256:"

# -- product language (8.8); the CLI prints its own English beside it ---------------

CHECKED = "已核对"
MISMATCH = "不符"
UNCHECKED = "未核对"
DRAFT_LABEL = "草稿 · 改动不留版本"
QUESTION_EDITED = "v{n} 的定义在确认后被改动了"
QUESTION_ABSENT = "v{n} 的版本文件在确认后不见了"
ANSWER_NEW = "new"
ANSWER_CORRECT = "correct"
ANSWER_LABELS = {ANSWER_NEW: "另存为新版本", ANSWER_CORRECT: "这是更正"}
#: A confirmed version whose file is gone has one answer: put it back from
#: its frozen copy (the restoring half of 「另存为新版本」; there is no edited
#: text to keep).
ABSENT_ANSWER_LABELS = {ANSWER_NEW: "按冻结副本放回"}
#: A confirmed version whose file can no longer be read (not UTF-8, not
#: readable) has no edited TEXT to keep either (9.12, acceptance
#: 2026-10-05): its one answer keeps the bytes as they are as the next
#: draft and puts the frozen text back.
QUESTION_UNREADABLE = "v{n} 的版本文件在确认后无法读取了"
UNREADABLE_ANSWER_LABELS = {ANSWER_NEW: "把这些内容原样存为下一版草稿，并按冻结副本放回"}
#: Shown for every version of a card whose log cannot be trusted (9.11
#: "Safety"): no status is derived from a log RCE does not obey -- above
#: all never 「草稿」, which would say confirmed text may be edited freely.
UNKNOWN_STATUS_LABEL = "记录文件无法使用，这一版的状态暂不可知"
COPY_MISSING = "确认记录引用的副本缺失"
UNRESOLVABLE = "引用暂不可解析"
UPSTREAM_NEWER = "上游 {id} 已有 v{n}"
SUPERSEDED = "已被 v{n} 取代"
MESSAGES = {
    "case_duplicate": "有两个只差大小写的同名变量卡目录，请先移走其中一个",
    "conflict_copy": "变量卡旁有同步冲突副本，请先处理",
    "unlistable": "变量卡目录当前无法读取",
    "missing": "变量卡的记录文件当前无法读取，请先恢复它",
    "empty_but_expected": "变量卡的记录文件当前无法读取，请先恢复它",
    "dataless": "记录文件正在从云端下载…",
    "unreadable": "变量卡的记录文件当前无法读取，请先恢复它",
    "invalid": "变量卡的记录文件当前无法读取，请先恢复它",
    "conflict": "变量卡的记录冲突（两份记录被合并），请先处理",
    "no_identity": "项目身份文件无法读取",
    "migrating": "项目正在迁移，迁移完成前不能写入人工记录",
    "shrunk": "变量卡的记录文件比图谱少了 {n} 条记录",
}


class VariableError(Exception):
    """A card could not be read or acted on as asked. Nothing was written.
    `code` is machine-readable; `message_zh` the product sentence, when
    there is one."""

    def __init__(self, code: str, message: str, *, message_zh: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message_zh = message_zh


class VersionInvalid(Exception):
    """A version file is not a valid version. `line` names where (1-based)."""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message if line is None else f"line {line}: {message}")
        self.reason = message
        self.line = line


# -- ids and places ------------------------------------------------------------------


def card_key(card_id: str) -> str:
    """How ids compare (9.11): case-folded, NFC-normalized."""
    return unicodedata.normalize("NFC", card_id).casefold()


def id_problem(card_id: str) -> str | None:
    """Why `card_id` cannot be a card's id (it becomes a directory name and
    the head of a reference `id@v2·hash`), or None."""
    if not card_id or len(card_id) > 64:
        return "an id is 1 to 64 characters"
    if card_id.startswith(("_", ".", "-")):
        return "an id may not start with '_', '.' or '-'"
    for ch in card_id:
        if not (ch.isalnum() or ch in "_-."):
            return f"an id holds letters, digits, '_', '-' and '.' only (not {ch!r})"
    if ".." in card_id:
        return "an id may not contain '..'"
    return None


def variables_dir(project_root: str | Path) -> Path:
    return paths.project_rce_dir(project_root) / VARIABLES_DIRNAME


def code_dir(project_root: str | Path) -> Path:
    return variables_dir(project_root) / CODE_DIRNAME


def snapshot_subdir(card_id: str) -> str:
    return f"{SNAPSHOT_SUBDIR}/{card_id}"


def card_dirs(project_root: str | Path) -> list[Path] | None:
    """Every card directory (not `_code`, not a symlink), sorted; [] when
    there is no `variables/`; None when it cannot be listed."""
    directory = variables_dir(project_root)
    if not directory.is_dir():
        return []
    try:
        return sorted(
            p for p in directory.iterdir()
            if p.is_dir() and not p.is_symlink() and p.name != CODE_DIRNAME and not p.name.startswith(".")
        )
    except OSError:
        return None


def find_card_dirs(project_root: str | Path, card_id: str) -> list[Path]:
    """The directories whose name equals `card_id` up to case (more than one
    makes the card unreadable)."""
    key = card_key(card_id)
    return [p for p in card_dirs(project_root) or [] if card_key(p.name) == key]


# -- the version file ----------------------------------------------------------------

TEXT_KEYS = ("name", "meaning", "unit", "granularity")
INPUT_KEYS = ("dataset", "variable", "fields", "data_version")
TABLE_KEYS = {
    "construction": ("formula", "filter", "aggregation", "missing", "transform", "params"),
    # `chunk` / `function` narrow the stage-(b) comparison (9.11, "When the
    # implementation moves"); named by the design, so known here.
    "implementation": ("script", "output", "field", "code_version", "chunk", "function"),
    "decision": ("why", "decided_by", "adopted_on"),
}
TOP_KEYS = (*TEXT_KEYS, "aliases", "input", *TABLE_KEYS)

_HEADER_RE = re.compile(r"^\s*(\[\[?)\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")
_KEY_RE = re.compile(r"""^\s*([A-Za-z0-9_\-]+|"[^"]*"|'[^']*')\s*=""")


def _line_of(text: str, table: str, key: str) -> int | None:
    """The line where `key` is written under `table` ("" for the top), for
    an error to name. Multi-line strings are stepped over."""
    return _line_table(text).get((table, key))


@functools.lru_cache(maxsize=8)
def _line_table(text: str) -> dict[tuple[str, str], int]:
    """{(table, key): the first line it is written on}, in one pass over the
    text (a version file is parsed on every read of its card)."""
    found: dict[tuple[str, str], int] = {}
    current = ""
    in_multi: str | None = None
    for n, line in enumerate(text.split("\n"), start=1):
        if in_multi is not None:
            if line.count(in_multi) % 2 == 1:
                in_multi = None
            continue
        header = _HEADER_RE.match(line)
        if header:
            current = header.group(2).strip().strip('"').strip("'")
            continue
        m = _KEY_RE.match(line)
        if m:
            found.setdefault((current, m.group(1).strip('"').strip("'")), n)
        for quote in ("'''", '"""'):
            if line.count(quote) % 2 == 1:
                in_multi = quote
                break
    return found


#: Control characters other than a newline (and the Unicode line/paragraph
#: separators). One regex search, not a Python loop per character: a card
#: is read on every application of the record and by the watcher.
_CONTROL_RE = re.compile("[\x00-\x09\x0b-\x1f\x7f\x85\u2028\u2029]")


def _check_text(value: str, where: str, line: int | None) -> None:
    m = _CONTROL_RE.search(value)
    if m is not None:
        code = ord(m.group(0))
        raise VersionInvalid(f"{where}: a control character (U+{code:04X}) is not allowed in the text", line)


def _str(value: Any, where: str, line: int | None) -> str:
    if not isinstance(value, str):
        raise VersionInvalid(f"{where} must be text (write it as '''…'''), got {type(value).__name__}", line)
    _check_text(value, where, line)
    return value


def _str_list(value: Any, where: str, line: int | None) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise VersionInvalid(f"{where} must be a list of texts, like [\"a\", \"b\"]", line)
    for v in value:
        _check_text(v, where, line)
    return list(value)


def _project_path_problem(value: str) -> str | None:
    if not value:
        return None
    if posixpath.isabs(value) or value.startswith("~") or "\\" in value:
        return "must be a path relative to the project folder, written with '/'"
    norm = posixpath.normpath(value)
    if norm == ".." or norm.startswith("../"):
        return "leaves the project folder"
    return None


REFERENCE_TEXT_RE = re.compile(r"^(?P<id>[^@·\s]+)@v(?P<n>[1-9][0-9]*)(?:·(?P<hash>[0-9a-f]{4,64}))?$")


def parse_version(text: str) -> dict[str, Any]:
    """Parse and validate a version file's text (module docstring). Returns
    its content: what was written, minus comments and layout, with a TOML
    date turned into its ISO text. Raises `VersionInvalid` naming the line."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise VersionInvalid(f"not valid TOML ({exc})", ledger_mod._toml_error_line(exc)) from exc
    content: dict[str, Any] = {}
    for key, value in data.items():
        line = _line_of(text, "", key)
        if key not in TOP_KEYS:
            raise VersionInvalid(f"unknown key {key!r} (a line under the wrong heading?)", line)
        if key in TEXT_KEYS:
            content[key] = _str(value, key, line)
        elif key == "aliases":
            content[key] = _str_list(value, key, line)
        elif key == "input":
            content[key] = _parse_inputs(text, value, line)
        else:
            content[key] = _parse_table(text, key, value, line)
    return content


def _parse_inputs(text: str, value: Any, line: int | None) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise VersionInvalid("write each input as its own [[input]] block", line)
    inputs = []
    for i, item in enumerate(value, start=1):
        where = f"input {i}"
        out: dict[str, Any] = {}
        for key, v in item.items():
            kline = _line_of(text, "input", key)
            if key not in INPUT_KEYS:
                raise VersionInvalid(f"{where}: unknown key {key!r} (a line under the wrong heading?)", kline)
            out[key] = _str_list(v, f"{where}.{key}", kline) if key == "fields" else _str(v, f"{where}.{key}", kline)
        if out.get("dataset") and out.get("variable"):
            raise VersionInvalid(f"{where}: an input is a dataset or a variable, not both", line)
        problem = _project_path_problem(out.get("dataset", ""))
        if problem:
            raise VersionInvalid(f"{where}.dataset {problem}", _line_of(text, "input", "dataset"))
        ref = out.get("variable", "")
        if ref and not REFERENCE_TEXT_RE.match(ref):
            raise VersionInvalid(
                f"{where}.variable must name a pinned version, like \"returns@v1\" (got {ref!r})",
                _line_of(text, "input", "variable"),
            )
        inputs.append(out)
    return inputs


def _parse_table(text: str, table: str, value: Any, line: int | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VersionInvalid(f"{table!r} must be a [{table}] block", line)
    out: dict[str, Any] = {}
    for key, v in value.items():
        kline = _line_of(text, table, key)
        if key not in TABLE_KEYS[table]:
            raise VersionInvalid(f"[{table}]: unknown key {key!r} (a line under the wrong heading?)", kline)
        if table == "decision" and key == "adopted_on" and isinstance(v, (date, datetime)):
            out[key] = v.isoformat()
            continue
        out[key] = _str(v, f"{table}.{key}", kline)
    for key in ("script", "output"):
        if table == "implementation" and out.get(key):
            problem = _project_path_problem(out[key])
            if problem:
                raise VersionInvalid(f"implementation.{key} {problem}", _line_of(text, table, key))
    return out


def canonical_content(content: Mapping[str, Any]) -> str:
    return json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(content: Mapping[str, Any]) -> str:
    """`sha256:<hex>` of the parsed content in canonical form."""
    return CONTENT_PREFIX + hashlib.sha256(canonical_content(content).encode("utf-8")).hexdigest()


def short_hash(value: str) -> str:
    return value.removeprefix(CONTENT_PREFIX)[:8]


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def missing_for_confirm(content: Mapping[str, Any]) -> list[str]:
    """What a draft still lacks before it can be confirmed (9.11): a name,
    meaning, unit, granularity, at least one input, a formula and a reason."""
    missing = [k for k in TEXT_KEYS if _blank(content.get(k))]
    inputs = content.get("input") or []
    if not inputs:
        missing.append("input (at least one [[input]])")
    for i, item in enumerate(inputs, start=1):
        if _blank(item.get("dataset")) and _blank(item.get("variable")):
            missing.append(f"input {i}: dataset or variable")
    if _blank((content.get("construction") or {}).get("formula")):
        missing.append("construction.formula")
    if _blank((content.get("decision") or {}).get("why")):
        missing.append("decision.why")
    return missing


# -- the log (a 9.3 ledger) ------------------------------------------------------------

ACTS = frozenset({"confirmed", "reaffirmed", "corrected", "abandoned", "revived", "removed", "settled"})
#: 9.12 (acceptance, 2026-10-05): two histories in a card's log are settled
#: by naming what stands -- a `settled` entry whose `settles` (assigned by
#: the ledger engine) names the anomalous entries and whose `keeps` names,
#: for each version number in dispute, the one confirmation that stands.
SETTLED_ACT = "settled"
KEEPABLE_ACTS = frozenset({"confirmed", "corrected"})
VERSION_ACTS = frozenset({"confirmed", "reaffirmed", "corrected", "removed"})
CARD_ACTS = frozenset({"abandoned", "revived"})
ATTESTED = ("yes", "no", "unknown")
VIAS = frozenset({"cli", "app", "mcp", "recovered"})

_LOG_HEADER = "# 变量卡的记录。RCE 只追加、不改写；版本文件 v<n>.toml 由你书写，RCE 从不改写它。\n"
_FROZEN_RE = re.compile(r"^frozen/[^/\\]+\.toml$")
_CODE_RE = re.compile(r"^_code/[^/\\]+$")


def _need(entry: Mapping[str, Any], name: str, kind: type, act: str) -> Any:
    value = entry.get(name)
    if isinstance(value, bool) or not isinstance(value, kind) or (isinstance(value, str) and not value):
        raise ValueError(f"a {act!r} entry needs {name!r}")
    return value


def _validate_log_entry(entry: Mapping[str, Any]) -> None:
    act = entry.get("act")
    if act in VERSION_ACTS:
        version = _need(entry, "version", int, act)
        if version < 1:
            raise ValueError(f"'version' must be 1 or more, got {version}")
    elif "version" in entry:
        raise ValueError(f"a {act!r} entry is about the whole variable and names no version")
    if act in ("confirmed", "corrected"):
        content = _need(entry, "content", str, act)
        if not content.startswith(CONTENT_PREFIX):
            raise ValueError(f"'content' must be {CONTENT_PREFIX}<hex>, got {content!r}")
        frozen = _need(entry, "frozen", str, act)
        if not _FROZEN_RE.match(frozen) or ".." in frozen:
            raise ValueError(f"'frozen' must name a file in frozen/, got {frozen!r}")
    if act == "confirmed":
        attested = entry.get("attested", "unknown")
        if attested not in ATTESTED:
            raise ValueError(f"'attested' must be one of {', '.join(ATTESTED)}, got {attested!r}")
    if act == "corrected":
        _need(entry, "previous", str, act)
        _need(entry, "corrects", str, act)
    if act == SETTLED_ACT:
        keeps = entry.get("keeps", [])
        if not isinstance(keeps, list) or not all(isinstance(k, str) and k for k in keeps):
            raise ValueError(f"'keeps' must list the ids of the entries that stand, got {keeps!r}")
    elif "keeps" in entry:
        raise ValueError("only a 'settled' entry names entries it keeps")
    if "removes" in entry and (act != "removed" or not isinstance(entry["removes"], str) or not entry["removes"]):
        raise ValueError("'removes' names the lost entry a 'removed' entry stands for")
    if act in CARD_ACTS:
        note = _need(entry, "note", str, act)
        if not note.strip():
            raise ValueError(f"a {act!r} entry needs the researcher's reason in 'note'")
    elif "note" in entry and not isinstance(entry["note"], str):
        raise ValueError(f"'note' must be text, got {entry['note']!r}")
    via = entry.get("via")
    if via is not None and via not in VIAS:
        raise ValueError(f"'via' must be one of {', '.join(sorted(VIAS))}, got {via!r}")
    checked = entry.get("checked")
    if checked is not None:
        if not isinstance(checked, dict):
            raise ValueError("'checked' must be a table")
        script = checked.get("script")
        if isinstance(script, dict) and "copy" in script:
            copy = script["copy"]
            if not isinstance(copy, str) or not _CODE_RE.match(copy) or ".." in copy:
                raise ValueError(f"'checked.script.copy' must name a file in _code/, got {copy!r}")
    for name in ("observed", "upstream"):
        if name in entry and not isinstance(entry[name], (dict, list)):
            raise ValueError(f"{name!r} must be a table or a list of tables")


def _validate_new_log_entry(entry: Mapping[str, Any]) -> None:
    if entry.get("via") not in VIAS:
        raise ValueError(f"'via' must be one of {', '.join(sorted(VIAS))}, got {entry.get('via')!r}")


CARD_LOG_SCHEMA = LedgerSchema(
    table="entry",
    id_prefix="v-",
    act_field="act",
    acts=ACTS,
    undo_act=None,
    key_fields=(),
    field_order=(
        "id", "seq", "at", "act", "version", "via", "attested", "content", "previous", "corrects", "removes", "frozen",
        "note", "keeps", "reaffirms", "reasons", "data_version", "recovered_from", "recovered_at", "upstream", "checked",
        "observed", "coverage",
    ),
    header=_LOG_HEADER,
    validate=_validate_log_entry,
    validate_new=_validate_new_log_entry,
)


# -- reading a card --------------------------------------------------------------------


@dataclass
class VersionView:
    """One version as read now. `status`: draft, in_use, confirmed (not the
    highest), superseded, removed, orphan_draft (unconfirmed but not the
    highest number), or unknown (the card's log cannot be trusted, so no
    status is derived). `question` is set when a confirmed version's file
    no longer has the content its entry froze; `question_kind` says how:
    edited (parsed, other content), invalid (no longer a valid version),
    unreadable (not UTF-8, or not readable), absent (the file is gone; a
    file still in the cloud asks nothing). `copy_missing`: the frozen copy
    the current entry names is not there; `code_missing`: the code copy it
    names is not there (9.11: either is 「确认记录引用的副本缺失」)."""

    number: int
    path: Path
    file_state: str
    error: str | None = None
    line: int | None = None
    data: bytes | None = None
    content: dict[str, Any] | None = None
    content_hash: str | None = None
    status: str = "draft"
    entry: LedgerEntry | None = None  # the current confirmed / corrected entry
    confirmed: LedgerEntry | None = None  # the first confirmed entry
    superseded_by: tuple[int, str | None] | None = None
    question: bool = False
    question_kind: str | None = None
    copy_missing: bool = False
    code_missing: bool = False
    upstream_notes: tuple[str, ...] = ()

    @property
    def frozen_path(self) -> Path | None:
        if self.entry is None:
            return None
        return self.path.parent / str(self.entry.get("frozen"))


@dataclass
class Card:
    """A card as read from its files (module docstring). `state`:
    - "ok"         the log can be trusted (by the files alone);
    - "unreadable" two directories equal up to case, or a sync conflict
                   copy beside one of its files (`problems` says which);
    - "frozen"     its log is missing though expected, in the cloud,
                   unreadable or invalid, or two histories were merged:
                   nothing in it is obeyed and writes to it are refused.
    `reason` is the machine-readable cause; `message` the product sentence."""

    id: str
    key: str
    directory: Path
    state: str = "ok"
    reason: str | None = None
    detail: str | None = None
    line: int | None = None
    problems: list[str] = field(default_factory=list)
    log: LedgerLoad | None = None
    log_expected: bool = False
    versions: dict[int, VersionView] = field(default_factory=dict)
    numbers_seen: set[int] = field(default_factory=set)
    in_use: int | None = None
    draft: int | None = None
    abandoned: LedgerEntry | None = None
    questions: list[int] = field(default_factory=list)
    #: Entries a `settled` entry left out of force (9.12): still in the
    #: history, shown as 「不再生效」, never derived from.
    not_in_force: set[str] = field(default_factory=set)

    @property
    def message(self) -> str | None:
        return MESSAGES.get(self.reason or "") if self.reason else None

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return self.log.ledger.entries if self.log is not None and self.log.ledger is not None else ()

    @property
    def entries_in_force(self) -> tuple[LedgerEntry, ...]:
        """The entries every status is derived from: all of them, except
        those a settlement left out of force (and the `settled` entries
        themselves, which say which is which)."""
        return tuple(e for e in self.entries if e.id not in self.not_in_force and e.get("act") != SETTLED_ACT)

    @property
    def next_number(self) -> int:
        """Numbers are not reused, as far as RCE can tell (9.11): one more
        than the highest seen in the files or the log."""
        return max(self.numbers_seen, default=0) + 1

    @property
    def readable(self) -> bool:
        return self.state == "ok"


def _version_numbers(directory: Path) -> dict[int, Path]:
    found: dict[int, Path] = {}
    try:
        names = sorted(p.name for p in directory.iterdir())
    except OSError:
        return found
    for name in names:
        m = VERSION_FILE_RE.match(name)
        if m:
            found[int(m.group(1))] = directory / name
    return found


def _conflict_copies(directory: Path, numbers: set[int]) -> list[str]:
    """Sync conflict copies beside any file of the card (9.11)."""
    found: list[str] = []
    candidates = [directory / LOG_FILENAME, *(directory / f"v{n}.toml" for n in sorted(numbers))]
    frozen = directory / FROZEN_DIRNAME
    if frozen.is_dir():
        try:
            candidates += sorted(p for p in frozen.iterdir() if p.is_file())
        except OSError:
            pass
    for path in candidates:
        found += [p.name for p in files.conflict_copies(path)]
    # A copy of a version file whose original is not there any more ("v2 2.toml").
    try:
        for p in directory.iterdir():
            m = re.match(r"^v([1-9][0-9]*)(?: \d+| ?\(\d+\)| \(.*conflict.*\)|[-_ ]conflict[-_ ].*)\.toml$", p.name, re.I)
            if m and p.name not in found:
                found.append(p.name)
    except OSError:
        pass
    return sorted(set(found))


def log_snapshots(project_root: Path, card_id: str) -> list[Path]:
    path = variables_dir(project_root) / card_id / LOG_FILENAME
    try:
        return files.snapshots(project_root, path, snapshot_subdir(card_id))
    except files.RecordFileError:
        return []


def read_version(path: Path) -> VersionView:
    m = VERSION_FILE_RE.match(path.name)
    number = int(m.group(1)) if m else 0
    got = files.read_record(path)
    view = VersionView(number=number, path=path, file_state=got.state.value, error=got.error, data=got.data)
    if got.state is not RecordState.PRESENT:
        return view
    try:
        view.content = parse_version(got.text or "")
    except VersionInvalid as exc:
        view.file_state, view.error, view.line = RecordState.INVALID.value, str(exc), exc.line
        return view
    view.content_hash = content_hash(view.content)
    return view


def read_card(project_root: str | Path, directory: str | Path, *, siblings: list[Path] | None = None) -> Card:
    """Read one card from its files (module docstring). Reads only.
    `siblings` is `card_dirs(project_root)` when the caller has listed it
    already (reading every card lists the directory once, not once per
    card)."""
    root, directory = Path(project_root), Path(directory)
    card = Card(id=directory.name, key=card_key(directory.name), directory=directory)
    listed = card_dirs(root) if siblings is None else siblings
    twins = [p.name for p in listed or [] if card_key(p.name) == card.key and p.name != directory.name]
    numbers = _version_numbers(directory)
    if twins:
        card.state, card.reason = "unreadable", "case_duplicate"
        card.detail = f"{directory.name} and {', '.join(twins)} are the same id up to letter case"
        card.problems.append(card.detail)
        return card
    copies = _conflict_copies(directory, set(numbers))
    if copies:
        card.state, card.reason = "unreadable", "conflict_copy"
        card.detail = f"sync conflict copies beside the card's files: {', '.join(copies)}"
        card.problems.append(card.detail)
        return card
    if not directory.is_dir():
        # A card the index knows whose directory is gone: read as a card
        # with no log, which the index's copy makes a question (9.3). With
        # no index, nothing knows it was there (9.11, said plainly).
        card.log = LedgerLoad(RecordState.ABSENT, directory / LOG_FILENAME)
        card.detail = f"the card's directory {directory.name} is not there"
        card.problems.append(card.detail)
        return card

    card.numbers_seen = set(numbers)
    for number, path in numbers.items():
        card.versions[number] = read_version(path)
    card.log = ledger_mod.load_ledger(directory / LOG_FILENAME, CARD_LOG_SCHEMA)
    # The log must exist once more than one version is on disk (at most one
    # draft), or once a snapshot of it was ever taken. A crash in a first
    # confirmation leaves a frozen copy and no log: that alone expects none.
    card.log_expected = len(numbers) > 1 or bool(log_snapshots(root, directory.name))
    state = card.log.state
    if state is RecordState.ABSENT and card.log_expected:
        card.state, card.reason = "frozen", "missing"
        card.detail = "the card has more than one version or an earlier log, and log.toml is not there"
    elif state in (RecordState.DATALESS, RecordState.UNREADABLE, RecordState.INVALID):
        card.state, card.reason, card.detail, card.line = "frozen", state.value, card.log.error, card.log.line
    elif state is RecordState.PRESENT and card.log_expected and not card.entries:
        card.state, card.reason = "frozen", "empty_but_expected"
        card.detail = "log.toml holds no entries, and the card has more than one version or an earlier log"
    elif card.log.ledger is not None and card.log.ledger.anomalies and card.log.ledger.conflict(()) is not None:
        card.state, card.reason = "frozen", "conflict"
        card.detail = ("two histories of log.toml were merged (seq repeats); nothing picks a winner -- "
                       "settle it by naming what stands: rce variable settle")
    if card.state != "ok":
        card.problems.append(f"{card.reason}: {card.detail}")
        for view in card.versions.values():
            view.status = "unknown"
        return card
    if card.log.ledger is not None and card.log.ledger.anomalies:
        card.not_in_force = settled_out(card.log.ledger)
    _derive(card)
    return card


# -- two histories, and naming what stands (9.12) -----------------------------------------


@dataclass(frozen=True)
class _Settlements:
    """The `settled` entries of a log, read once. `fresh`: for each, the
    anomalies it settled first (those no earlier settlement settled).
    `contested`: pairs (x, y) where y, a later settlement written without
    knowing x (its seq is not above x's: another copy's), settles an
    anomaly x settled -- two copies that each settled the same dispute
    differently. Neither of a contested pair stands; a later settlement
    decides over the region both of them settled."""

    by_id: dict[str, Any]
    fresh: dict[str, tuple[Any, ...]]
    contested: tuple[tuple[LedgerEntry, LedgerEntry], ...]

    @property
    def void(self) -> set[str]:
        return {e.id for pair in self.contested for e in pair}

    def widen(self, start: int, end: int) -> int:
        """`start` moved back over the regions that contested settlements in
        [start, end) settled, until no more is found."""
        while True:
            new = start
            for x, y in self.contested:
                if (start <= x.index < end) or (start <= y.index < end):
                    for a in self.fresh.get(x.id, ()):
                        new = min(new, a.start)
            if new == start:
                return start
            start = new


def _settlements(ledger: Any) -> _Settlements:
    by_id = {a.entry.id: a for a in ledger.anomalies}
    handled: set[str] = set()
    fresh: dict[str, tuple[Any, ...]] = {}
    deciding: list[LedgerEntry] = []
    contested: list[tuple[LedgerEntry, LedgerEntry]] = []
    for e in ledger.entries:
        if e.get("act") != SETTLED_ACT:
            continue
        for x in deciding:
            if (e.seq is not None and x.seq is not None and e.seq <= x.seq
                    and set(e.settles) & {a.entry.id for a in fresh[x.id]}):
                contested.append((x, e))
        mine = tuple(by_id[i] for i in e.settles if i in by_id and i not in handled)
        fresh[e.id] = mine
        handled |= {a.entry.id for a in mine}
        if mine:
            deciding.append(e)
    return _Settlements(by_id=by_id, fresh=fresh, contested=tuple(contested))


def settled_out(ledger: Any, *, before: int | None = None) -> set[str]:
    """The ids a `settled` entry left out of force: every entry of the
    region it settled (from where the two histories diverged up to the
    settlement) except those it keeps. Settlements are taken in file order,
    each over the anomalies no earlier one settled; each decides its whole
    region, over what an earlier one said about it. Two copies that settled
    the same dispute differently (contested, see `_Settlements`) decide
    nothing; the settlement that answers them decides over both their
    regions. `before`: only the settlements at an index below it. Positions
    decide nothing here -- the region is both histories, and only `keeps`
    says what stands."""
    info = _settlements(ledger)
    void = info.void
    out: set[str] = set()
    for e in ledger.entries:
        if before is not None and e.index >= before:
            break
        if e.get("act") != SETTLED_ACT or e.id in void:
            continue
        fresh = info.fresh.get(e.id, ())
        if not fresh:
            continue
        keeps = set(e.get("keeps") or ())
        start = info.widen(min(a.start for a in fresh), e.index)
        region = [x for x in ledger.entries[start:e.index] if x.get("act") != SETTLED_ACT]
        out -= {x.id for x in region}
        out |= {x.id for x in region if x.id not in keeps}
    return out


@dataclass(frozen=True)
class Dispute:
    """What a card whose log holds two merged histories asks (9.12). `common`
    are the entries both histories share, `branches` each history's entries
    after they diverged (file order within each), `candidates` for each
    version number in dispute -- one confirmed or corrected in the diverged
    part -- the confirmations that may stand: the one that stood before the
    histories diverged (if any) and each one made in either history.
    `shown` are the ids of every entry the question shows; an answer is tied
    to them."""

    anomalies: tuple[str, ...]
    common: tuple[LedgerEntry, ...]
    branches: tuple[tuple[LedgerEntry, ...], ...]
    candidates: dict[int, tuple[LedgerEntry, ...]]

    @property
    def region(self) -> tuple[LedgerEntry, ...]:
        return tuple(e for branch in self.branches for e in branch)

    @property
    def shown(self) -> tuple[str, ...]:
        return tuple(sorted({e.id for e in self.region} | {e.id for c in self.candidates.values() for e in c}))

    @property
    def disputed(self) -> list[int]:
        return sorted(self.candidates)

    def version_of(self, entry_id: str) -> int | None:
        for number, found in self.candidates.items():
            if any(e.id == entry_id for e in found):
                return number
        return None


def dispute_of(card: Card) -> Dispute | None:
    """The question a conflicted card asks, or None when its log holds no
    unsettled histories. Reads only. When the histories that diverged hold
    settlements that contest each other (two copies each settled the same
    dispute their own way), the question goes back to what those
    settlements chose between: the region widens over theirs, and the
    confirmations they disagreed on are offered again."""
    ledger = card.log.ledger if card.log is not None else None
    if ledger is None or not ledger.anomalies:
        return None
    conflict = ledger.conflict(())
    if conflict is None:
        return None
    info = _settlements(ledger)
    first = min((e.index for b in conflict.branches for e in b), default=None)
    opened = min(a.start for a in ledger.anomalies if a.entry.id in conflict.anomalies)
    start = info.widen(opened, len(ledger.entries))
    if start < opened:
        open_ids = set(conflict.anomalies)
        split: list[list[LedgerEntry]] = [[]]
        previous: int | None = None
        for e in ledger.entries[start:]:
            if e.get("act") == SETTLED_ACT or (e.settles and open_ids <= set(e.settles)):
                continue
            if e.seq is not None:
                if previous is not None and e.seq <= previous:
                    split.append([])
                previous = e.seq
            split[-1].append(e)
        raw_common, raw_branches, before_index = ledger.entries[:start], tuple(tuple(b) for b in split), start
    else:
        raw_common, raw_branches, before_index = conflict.common, conflict.branches, first
    earlier_out = settled_out(ledger, before=before_index)
    common = tuple(e for e in raw_common if e.id not in earlier_out and e.get("act") != SETTLED_ACT)
    branches = tuple(tuple(e for e in b if e.get("act") != SETTLED_ACT) for b in raw_branches)
    region = [e for b in branches for e in b]
    candidates: dict[int, tuple[LedgerEntry, ...]] = {}
    for number in sorted({int(e.get("version")) for e in region if e.get("act") in KEEPABLE_ACTS}):
        before: LedgerEntry | None = None
        for e in common:
            if e.get("version") != number:
                continue
            if e.get("act") in KEEPABLE_ACTS:
                before = e
            elif e.get("act") == "removed" and not e.get("removes"):
                before = None
        mine = [e for e in region if e.get("version") == number and e.get("act") in KEEPABLE_ACTS]
        candidates[number] = tuple(([before] if before is not None else []) + mine)
    return Dispute(anomalies=conflict.anomalies, common=common, branches=branches, candidates=candidates)


def _derive(card: Card) -> None:
    """Versions, the version in use, the draft, the questions -- from the
    log in file order (the clock decides nothing)."""
    by_version: dict[int, list[LedgerEntry]] = {}
    for e in card.entries_in_force:
        if e.get("act") in VERSION_ACTS:
            by_version.setdefault(int(e.get("version")), []).append(e)
        elif e.get("act") == "abandoned":
            card.abandoned = e
        elif e.get("act") == "revived":
            card.abandoned = None
    # Numbers are not reused (9.11), not even one only a history left out of
    # force by a settlement confirmed.
    card.numbers_seen |= set(by_version) | {int(e.get("version")) for e in card.entries if e.get("act") in VERSION_ACTS}
    confirmed: dict[int, LedgerEntry] = {}
    removed: set[int] = set()
    for number, entries in by_version.items():
        current: LedgerEntry | None = None
        for e in entries:
            act = e.get("act")
            if act in ("confirmed", "corrected"):
                current = e
            elif act == "removed" and not e.get("removes"):
                # A `removed` entry naming the entry it stands for
                # (`removes`) keeps a lost confirmation's wording referenced
                # and its number used; it says nothing about the entries the
                # file holds (another history's v<n>, 「以文件为准」).
                current = None
        if current is not None:
            confirmed[number] = current
        elif any(e.get("act") == "removed" for e in entries):
            removed.add(number)
    for number in sorted(set(confirmed) | removed):
        if number not in card.versions:
            card.versions[number] = VersionView(number=number, path=card.directory / f"v{number}.toml",
                                                file_state=RecordState.ABSENT.value)
    for number, view in card.versions.items():
        entries = by_version.get(number, [])
        view.confirmed = next((e for e in entries if e.get("act") == "confirmed"), None)
        if number in removed:
            view.status = "removed"
            continue
        if number not in confirmed:
            view.status = "draft"
            continue
        entry = confirmed[number]
        view.entry = entry
        view.status = "confirmed"
        frozen = view.frozen_path
        view.copy_missing = frozen is None or not frozen.exists()
        code = code_copy_of(entry)
        view.code_missing = code is not None and not (card.directory.parent / code).is_file()
        # 9.11: any change to the content of a confirmed v<n>.toml is asked
        # about -- an edit, a file no longer valid or no longer readable
        # (re-saved in another encoding), a file gone. Only a file still in
        # the cloud asks nothing: it is on its way, not changed.
        if view.file_state == RecordState.PRESENT.value:
            if view.content_hash != entry.get("content"):
                view.question_kind = "edited"
        elif view.file_state in (RecordState.INVALID.value, RecordState.UNREADABLE.value, RecordState.ABSENT.value):
            view.question_kind = view.file_state
        view.question = view.question_kind is not None
        if view.question:
            card.questions.append(number)
    ordered = sorted(confirmed)
    for i, number in enumerate(ordered):
        if i + 1 < len(ordered):
            successor = ordered[i + 1]
            succ = card.versions[successor].confirmed or confirmed[successor]
            card.versions[number].status = "superseded"
            card.versions[number].superseded_by = (successor, succ.at)
    drafts = sorted(n for n, v in card.versions.items() if v.status == "draft")
    if drafts:
        top = max(card.numbers_seen)
        if drafts[-1] == top:
            card.draft = top
        for n in drafts:
            if n != card.draft:
                card.versions[n].status = "orphan_draft"
                card.problems.append(f"v{n}.toml is not confirmed and is not the highest version (a card has one draft, the highest)")
    if ordered and card.abandoned is None:
        card.in_use = ordered[-1]
        card.versions[ordered[-1]].status = "in_use"


def code_copy_of(entry: LedgerEntry | Mapping[str, Any] | None) -> str | None:
    """The code copy (`_code/<sha>.<ext>`) an entry names, or None."""
    if entry is None:
        return None
    script = (entry.get("checked") or {}).get("script")
    copy = script.get("copy") if isinstance(script, Mapping) else None
    return copy if isinstance(copy, str) and copy else None


def read_cards(project_root: str | Path) -> list[Card]:
    dirs = card_dirs(project_root) or []
    return [read_card(project_root, d, siblings=dirs) for d in dirs]


def open_card(project_root: str | Path, card_id: str) -> Card:
    """The card named `card_id` (case-folded), read; raises `VariableError`
    (`no_such_card`) when there is none."""
    found = find_card_dirs(project_root, card_id)
    if not found:
        raise VariableError("no_such_card", f"there is no variable card {card_id!r} in {variables_dir(project_root)}")
    return read_card(project_root, found[0])


# -- references (9.11) -------------------------------------------------------------------


@dataclass(frozen=True)
class Reference:
    """What a result stores: the variable's id, the version label, the id of
    the log entry it relies on and that entry's content hash."""

    variable: str
    version: int
    entry: str
    content: str

    @property
    def label(self) -> str:
        return f"{self.variable}@v{self.version}·{short_hash(self.content)}"

    def payload(self) -> dict[str, Any]:
        return {"variable": self.variable, "version": self.version, "entry": self.entry, "content": self.content,
                "label": self.label}


@dataclass(frozen=True)
class Resolved:
    reference: Reference
    entry: LedgerEntry
    frozen: Path
    text: str | None  # the wording the reference was made against (None: copy missing)

    @property
    def copy_missing(self) -> bool:
        return self.text is None


def reference_to(project_root: str | Path, card_id: str, version: int | None = None) -> Reference:
    """A reference to a confirmed version as it stands now (its current
    entry: the confirmation, or the correction in force). Only a confirmed
    version can be referred to; `version` None means the one in use."""
    card = open_card(project_root, card_id)
    if not card.readable:
        raise VariableError("untrusted", f"{card.id}: {card.detail}", message_zh=card.message)
    number = card.in_use if version is None else version
    view = card.versions.get(number) if number is not None else None
    if view is None or view.entry is None:
        raise VariableError("not_confirmed", f"{card.id} has no confirmed version {number}; only a confirmed version can be referred to")
    return Reference(card.id, number, view.entry.id, str(view.entry.get("content")))


def resolve(project_root: str | Path, ref: Reference) -> Resolved | None:
    """The entry `ref` relies on, when the card's log holds it with that hash
    -- else None, shown as 「引用暂不可解析」. Never the current holder of the
    same number."""
    found = find_card_dirs(project_root, ref.variable)
    if len(found) != 1:
        return None
    card = read_card(project_root, found[0])
    if card.log is None or card.log.ledger is None or card.state == "unreadable":
        return None
    entry = card.log.ledger.by_id(ref.entry)
    if entry is None or entry.get("act") not in ("confirmed", "corrected"):
        return None
    if entry.get("content") != ref.content or entry.get("version") != ref.version:
        return None
    frozen = card.directory / str(entry.get("frozen"))
    text: str | None = None
    got = files.read_record(frozen)
    if got.state is RecordState.PRESENT:
        try:
            if content_hash(parse_version(got.text or "")) == ref.content:
                text = got.text
        except VersionInvalid:
            text = None
    return Resolved(ref, entry, frozen, text)


def resolve_text(project_root: str | Path, text: str, *, require_hash: bool = False) -> Reference | None:
    """A written reference (`returns@v1`, or `returns@v1·9c1f2a3b`) as a
    `Reference`: with a hash, the entry for that version whose content
    starts with it (exactly one); without, the version's current entry.
    None when it cannot be made."""
    m = REFERENCE_TEXT_RE.match(text.strip())
    if m is None:
        return None
    card_id, number, prefix = m.group("id"), int(m.group("n")), m.group("hash")
    if prefix is None:
        if require_hash:
            return None
        try:
            return reference_to(project_root, card_id, number)
        except VariableError:
            return None
    found = find_card_dirs(project_root, card_id)
    if len(found) != 1:
        return None
    card = read_card(project_root, found[0])
    matches = [
        e for e in card.entries
        if e.get("act") in ("confirmed", "corrected") and e.get("version") == number
        and str(e.get("content")).removeprefix(CONTENT_PREFIX).startswith(prefix)
    ]
    if len({str(e.get("content")) for e in matches}) != 1:
        return None
    entry = matches[-1]
    return Reference(card.id, number, entry.id, str(entry.get("content")))


def upstream_notes(project_root: str | Path, entry: LedgerEntry | None) -> list[str]:
    """「上游 <id> 已有 v<n>」 for each variable input pinned in `entry` whose
    card now has a newer version in use -- and nothing else happens: this
    version was built on the one it pinned (9.11)."""
    notes: list[str] = []
    if entry is None:
        return notes
    for item in entry.get("upstream") or []:
        if not isinstance(item, Mapping):
            continue
        found = find_card_dirs(project_root, str(item.get("variable", "")))
        if len(found) != 1:
            continue
        upstream = read_card(project_root, found[0])
        pinned = item.get("version")
        if upstream.in_use is not None and isinstance(pinned, int) and upstream.in_use > pinned:
            notes.append(UPSTREAM_NEWER.format(id=upstream.id, n=upstream.in_use))
    return notes


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value
