"""The append-only ledger engine (DESIGN.md section 9.3), and the judgment
ledger `.rce/judgements.toml` built on it.

Every append-only record of V5 is a ledger: the judgment ledger now, each
variable card's `log.toml` later (9.11, "an append-only ledger, exactly
9.3"). So the engine here is generic over the TOML array-of-tables name
and the entry schema (`LedgerSchema`); `JUDGEMENT_SCHEMA` is the first
schema, and `append_judgement` its writer.

The file is the history
-----------------------

An RCE write is the file's existing bytes plus the new entry's bytes
(`rce.records.files.append_bytes`). Existing text, comments, layout and
unknown keys are never re-emitted, so a hand edit survives every RCE
write and history is not a feature to build: it is the file. Before the
bytes are written, the whole new text is parsed back and must yield the
old entries unchanged plus exactly the new one -- the fixed-schema emitter
is trusted only as far as `tomllib` agrees with it, on every write.

Order is the file's order; the clock decides nothing
---------------------------------------------------

Each entry RCE writes carries `seq`, one more than the highest in the
file, assigned while the caller holds the project lock (`append` demands
the `HeldLock`). `at` is RCE's clock, written for a person to read and
never consulted: a corrected clock, or another Mac's running behind, must
not put a withdrawal before the confirmation it withdrew (the external
review caught an earlier draft ordering by time). The state of a key is
its last entry in file order among those not cancelled by an undo.

Two histories are a conflict, not a race
----------------------------------------

`seq` that repeats or runs backwards means two machines appended to
different copies and something merged them. Nothing picks a winner. An
anomaly is the first entry of a run that restarts the numbering; its
conflict region starts at the earliest entry before it whose `seq` is at
least as high -- i.e. where the two copies diverged -- so both sides of
the fork are in the region, not only the side that happens to come second
(a stricter reading of 9.3's "at or after the first anomaly"; it only
ever marks more, never decides more). Every key with an entry in a region
is in conflict, with both histories, until a new entry settles it.

How "new" is known is the one thing 9.3 does not say. Position cannot
tell: the second copy's own later entries also come after the anomaly
with ever higher numbers. So an entry RCE appends to a file that has
anomalies carries `settles`, the ids of the anomalous entries it was
written knowing about. Such an entry is outside those regions, and for
its own key it settles the conflict. Hand-written entries carry no
`settles` and are positioned by file order; one without `seq` is never an
anomaly itself.

Undo and withdraw (9.3, 8.12)
----------------------------

An `undo` entry (`verdict = "undone"` for judgments) names, in `undoes`,
the entry it cancels; the key is then whatever it was before. Only a
non-undo entry for the same key, earlier in the file, may be undone; an
undo is never undone (a redo is a new act). `append` further requires the
target to be the key's current state -- 「撤销」 takes back the *last*
act -- and refuses an undo on a key in conflict. A `withdrawn` entry is an
ordinary act whose meaning ("the machine's status") belongs to the
schema's reader (`judgement_status`).

Validation
----------

A hand-edited ledger is legal, but an entry that fails validation makes
the whole ledger unreadable, and the error names its line; so does one id
used twice with different content. The same id twice with *identical*
content is a merge that duplicated an entry, and is read once. Unknown
keys inside an entry are allowed and kept (a newer RCE's field must not
make an older one refuse the file).
"""

from __future__ import annotations

import math
import re
import secrets
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from rce import paths
from rce.records import files
from rce.records.files import RecordState
from rce.records.lock import HeldLock

Clock = Callable[[], datetime]
Key = tuple[str, ...]

ENGINE_FIELDS = ("id", "seq", "at", "settles", "undoes")


class LedgerError(Exception):
    """Base: a ledger could not be read or written as asked."""


class LedgerInvalidError(LedgerError):
    """The ledger's text is not a valid ledger. `line` names where (1-based),
    when it can be told."""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message if line is None else f"line {line}: {message}")
        self.line = line
        self.reason = message


class LedgerWriteRefused(LedgerError):
    """An append was refused (invalid value, unreadable file, nothing to
    undo, ...). Nothing was written."""


# -- the fixed-schema TOML emitter -------------------------------------------------

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
# Never emitted raw even where TOML would allow it: a person reading the
# file, or a line-based tool, would see a line break that is not there.
_ESCAPE_ALWAYS = "\x85  "


def toml_string(value: str) -> str:
    """A TOML basic string that `tomllib` reads back as exactly `value`:
    quotes and backslashes escaped, every control character (and NEL, LS,
    PS) as an escape, everything else -- CJK included -- as UTF-8."""
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif code < 0x20 or code == 0x7F or ch in _ESCAPE_ALWAYS:
            out.append(f"\\u{code:04X}")
        elif 0xD800 <= code <= 0xDFFF:
            raise LedgerWriteRefused(f"lone surrogate U+{code:04X} cannot be written")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.match(key) else toml_string(key)


def _toml_value(value: Any, where: str) -> str:
    if isinstance(value, bool):
        raise LedgerWriteRefused(f"{where}: true/false is not a value this ledger stores")
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LedgerWriteRefused(f"{where}: {value!r} is not a finite number")
        return repr(value)
    if isinstance(value, (list, tuple)):
        if not all(isinstance(v, str) for v in value):
            raise LedgerWriteRefused(f"{where}: a list may only hold strings")
        return "[" + ", ".join(toml_string(v) for v in value) + "]"
    raise LedgerWriteRefused(f"{where}: {type(value).__name__} is not a value this ledger stores")


def _emit_table(lines: list[str], header: str, table: Mapping[str, Any], depth: int) -> None:
    lines.append(f"[{header}]")
    subtables = []
    for key, value in table.items():
        if isinstance(value, Mapping):
            subtables.append((key, value))
        else:
            lines.append(f"{toml_key(key)} = {_toml_value(value, header + '.' + key)}")
    for key, value in subtables:
        if depth >= _MAX_TABLE_DEPTH:
            raise LedgerWriteRefused(f"{header}.{key}: tables nest at most {_MAX_TABLE_DEPTH} deep")
        _emit_table(lines, f"{header}.{toml_key(key)}", value, depth + 1)


_MAX_TABLE_DEPTH = 3


def emit_entry(schema: "LedgerSchema", entry: Mapping[str, Any]) -> bytes:
    """One `[[<table>]]` block, fields in the schema's order, tables (the
    judgment's `basis`) last. Starts with a blank line so entries read as
    paragraphs."""
    lines = ["", f"[[{toml_key(schema.table)}]]"]
    order = list(ENGINE_FIELDS[:3]) + [f for f in schema.field_order if f not in ENGINE_FIELDS[:3]]
    for extra in ("undoes", "settles"):
        if extra not in order:
            order.append(extra)
    tables = []
    for key in order + [k for k in entry if k not in order]:
        if key not in entry or entry[key] is None:
            continue
        value = entry[key]
        if isinstance(value, Mapping):
            tables.append((key, value))
            continue
        lines.append(f"{toml_key(key)} = {_toml_value(value, key)}")
    for key, value in tables:
        _emit_table(lines, f"{toml_key(schema.table)}.{toml_key(key)}", value, 1)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _reject_line_breaks(value: Any, where: str) -> None:
    """Every string RCE writes into a ledger is single-line: the
    invisible line separators are refused by the map editor's own check
    (`rce.webapp.mapedit._reject_line_boundaries`), so the two writers
    can never disagree about what a line break is."""
    from rce.webapp import mapedit  # noqa: PLC0415 -- leaf use; avoids an import cycle later

    if isinstance(value, str):
        try:
            mapedit._reject_line_boundaries(value, where)
        except mapedit.MapEditError as exc:
            raise LedgerWriteRefused(str(exc)) from exc
    elif isinstance(value, Mapping):
        for k, v in value.items():
            _reject_line_breaks(k, f"{where} key")
            _reject_line_breaks(v, f"{where}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _reject_line_breaks(v, f"{where}[{i}]")


# -- schema ------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerSchema:
    """What makes a ledger a judgment ledger or a variable log.

    `validate(entry)` checks the schema's own fields on read and raises
    `ValueError(message)`; `validate_new(entry)` adds what only RCE's own
    writes must satisfy (e.g. `via`). The engine checks `id`, `seq`, `at`,
    `settles`, `undoes`, the act and the key fields itself."""

    table: str
    id_prefix: str
    act_field: str
    acts: frozenset[str]
    undo_act: str | None
    key_fields: tuple[str, ...]
    field_order: tuple[str, ...]
    header: str
    validate: Callable[[Mapping[str, Any]], None]
    validate_new: Callable[[Mapping[str, Any]], None] = lambda entry: None

    def key_of(self, entry: Mapping[str, Any]) -> Key:
        return tuple(entry[f] for f in self.key_fields)


# -- parsed ledger ------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerEntry:
    data: Mapping[str, Any]
    index: int
    line: int | None
    key: Key

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def seq(self) -> int | None:
        return self.data.get("seq")

    @property
    def at(self) -> str | None:
        at = self.data.get("at")
        return at.isoformat() if isinstance(at, (datetime, date, time)) else at

    @property
    def settles(self) -> tuple[str, ...]:
        return tuple(self.data.get("settles", ()))

    def get(self, name: str, default: Any = None) -> Any:
        return self.data.get(name, default)


@dataclass(frozen=True)
class Anomaly:
    """`entry` is the first of a run whose `seq` repeats or decreases;
    `start` the index where the two histories diverged."""

    entry: LedgerEntry
    start: int


@dataclass(frozen=True)
class Conflict:
    """Key in conflict: its entries before the divergence (`common`) and its
    entries in each diverged run (`branches`, in file order)."""

    key: Key
    anomalies: tuple[str, ...]
    common: tuple[LedgerEntry, ...]
    branches: tuple[tuple[LedgerEntry, ...], ...]


@dataclass(frozen=True)
class KeyState:
    """`entry` is the key's current act (None: no act stands -- never
    judged, or every act undone); `conflict` is set instead when the key
    is in conflict, and then `entry` is None. `history` is every entry for
    the key in file order, undos included."""

    key: Key
    entry: LedgerEntry | None
    conflict: Conflict | None
    history: tuple[LedgerEntry, ...]


@dataclass
class Ledger:
    schema: LedgerSchema
    entries: tuple[LedgerEntry, ...]
    anomalies: tuple[Anomaly, ...]
    _by_key: dict[Key, list[LedgerEntry]] = field(default_factory=dict, repr=False)
    _cancelled: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        for e in self.entries:
            self._by_key.setdefault(e.key, []).append(e)
        undo = self.schema.undo_act
        self._cancelled = {e.data["undoes"] for e in self.entries if undo and e.data[self.schema.act_field] == undo}

    @property
    def max_seq(self) -> int:
        return max((e.seq for e in self.entries if e.seq is not None), default=0)

    @property
    def ids(self) -> set[str]:
        return {e.id for e in self.entries}

    def by_id(self, entry_id: str) -> LedgerEntry | None:
        for e in self.entries:
            if e.id == entry_id:
                return e
        return None

    def keys(self) -> list[Key]:
        return list(self._by_key)

    def history(self, key: Key) -> tuple[LedgerEntry, ...]:
        return tuple(self._by_key.get(tuple(key), ()))

    def is_cancelled(self, entry: LedgerEntry) -> bool:
        return entry.id in self._cancelled

    def _current(self, key: Key) -> LedgerEntry | None:
        undo = self.schema.undo_act
        for e in reversed(self._by_key.get(key, [])):
            if e.data[self.schema.act_field] == undo or e.id in self._cancelled:
                continue
            return e
        return None

    def _in_region(self, entry: LedgerEntry, anomaly: Anomaly) -> bool:
        return entry.index >= anomaly.start and anomaly.entry.id not in entry.settles

    def conflict(self, key: Key) -> Conflict | None:
        key = tuple(key)
        mine = self._by_key.get(key, [])
        open_anomalies = []
        for a in self.anomalies:
            in_region = [e for e in mine if self._in_region(e, a)]
            if not in_region:
                continue
            last = in_region[-1].index
            settled = any(
                e.index > last
                and a.entry.id in e.settles
                and e.id not in self._cancelled
                and e.data[self.schema.act_field] != self.schema.undo_act
                for e in mine
            )
            if not settled:
                open_anomalies.append(a)
        if not open_anomalies:
            return None
        start = min(a.start for a in open_anomalies)
        common = tuple(e for e in mine if e.index < start)
        branches: list[list[LedgerEntry]] = [[]]
        previous: int | None = None
        for e in self.entries[start:]:
            if e.settles and all(a.entry.id in e.settles for a in open_anomalies):
                continue
            if e.seq is not None:
                if previous is not None and e.seq <= previous:
                    branches.append([])
                previous = e.seq
            if e.key == key:
                branches[-1].append(e)
        return Conflict(
            key=key,
            anomalies=tuple(a.entry.id for a in open_anomalies),
            common=common,
            branches=tuple(tuple(b) for b in branches),
        )

    def conflicts(self) -> dict[Key, Conflict]:
        found = {}
        for key in self._by_key:
            c = self.conflict(key)
            if c is not None:
                found[key] = c
        return found

    def state(self, key: Key) -> KeyState:
        key = tuple(key)
        conflict = self.conflict(key)
        return KeyState(
            key=key,
            entry=None if conflict else self._current(key),
            conflict=conflict,
            history=self.history(key),
        )

    def entry_dicts(self) -> list[dict[str, Any]]:
        return [dict(e.data) for e in self.entries]


# -- parse ---------------------------------------------------------------------------


def _header_lines(text: str, table: str) -> list[int]:
    pattern = re.compile(rf'^\s*\[\[\s*(?:{re.escape(table)}|"{re.escape(table)}")\s*\]\]\s*(?:#.*)?$')
    return [n for n, line in enumerate(text.split("\n"), start=1) if pattern.match(line)]


def _toml_error_line(exc: tomllib.TOMLDecodeError) -> int | None:
    lineno = getattr(exc, "lineno", None)
    if lineno:
        return int(lineno)
    m = re.search(r"line (\d+)", str(exc))
    return int(m.group(1)) if m else None


def _nonempty_str(entry: Mapping[str, Any], name: str) -> str:
    value = entry.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name!r} must be a non-empty string, got {value!r}")
    return value


def _validate_engine_fields(schema: LedgerSchema, entry: Mapping[str, Any]) -> None:
    _nonempty_str(entry, "id")
    seq = entry.get("seq")
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 1):
        raise ValueError(f"'seq' must be a positive integer, got {seq!r}")
    at = entry.get("at")
    if at is not None and not isinstance(at, (str, datetime, date)):
        raise ValueError(f"'at' must be a time, got {at!r}")
    settles = entry.get("settles")
    if settles is not None and (not isinstance(settles, list) or not all(isinstance(s, str) for s in settles)):
        raise ValueError(f"'settles' must be a list of entry ids, got {settles!r}")
    act = entry.get(schema.act_field)
    if act not in schema.acts:
        raise ValueError(f"{schema.act_field!r} must be one of {', '.join(sorted(schema.acts))}, got {act!r}")
    for name in schema.key_fields:
        _nonempty_str(entry, name)
    if schema.undo_act is not None:
        if act == schema.undo_act:
            _nonempty_str(entry, "undoes")
        elif "undoes" in entry:
            raise ValueError(f"'undoes' belongs only on a {schema.undo_act!r} entry")


def parse_ledger(text: str, schema: LedgerSchema) -> Ledger:
    """Parse and validate a ledger's text. Raises `LedgerInvalidError`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LedgerInvalidError(f"not valid TOML ({exc})", _toml_error_line(exc)) from exc
    raw = data.get(schema.table, [])
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        raise LedgerInvalidError(f"{schema.table!r} must be written as [[{schema.table}]] entries")
    headers = _header_lines(text, schema.table)
    line_of = (lambda i: headers[i]) if len(headers) == len(raw) else (lambda i: None)

    entries: list[LedgerEntry] = []
    seen: dict[str, LedgerEntry] = {}
    for i, item in enumerate(raw):
        line = line_of(i)
        try:
            _validate_engine_fields(schema, item)
            schema.validate(item)
        except ValueError as exc:
            raise LedgerInvalidError(str(exc), line) from exc
        prior = seen.get(item["id"])
        if prior is not None:
            if dict(prior.data) == item:
                continue  # a merge duplicated this entry: read it once
            raise LedgerInvalidError(f"id {item['id']!r} is used twice with different content", line)
        entry = LedgerEntry(data=item, index=len(entries), line=line, key=schema.key_of(item))
        undo = schema.undo_act
        if undo is not None and item[schema.act_field] == undo:
            target = seen.get(item["undoes"])
            if target is None:
                raise LedgerInvalidError(f"'undoes' names {item['undoes']!r}, which is not an earlier entry", line)
            if target.data[schema.act_field] == undo:
                raise LedgerInvalidError("an undo cannot be undone; write the act again instead", line)
            if target.key != entry.key:
                raise LedgerInvalidError(f"'undoes' names an entry about a different {'/'.join(schema.key_fields)}", line)
        seen[item["id"]] = entry
        entries.append(entry)

    # An anomaly is the first entry of a run that restarts the numbering
    # (its seq is not above the previous numbered entry's); the rest of
    # that run continues it and is in the same region.
    anomalies: list[Anomaly] = []
    previous: int | None = None
    for e in entries:
        if e.seq is None:
            continue
        if previous is not None and e.seq <= previous:
            start = min(x.index for x in entries[: e.index] if x.seq is not None and x.seq >= e.seq)
            anomalies.append(Anomaly(entry=e, start=start))
        previous = e.seq
    return Ledger(schema=schema, entries=tuple(entries), anomalies=tuple(anomalies))


@dataclass(frozen=True)
class LedgerLoad:
    """`read_record` plus parsing: `state` is PRESENT with `ledger` set, or
    ABSENT / DATALESS / UNREADABLE / INVALID with `error` (and `line` for
    INVALID). `conflict_copies` is reported whatever the state."""

    state: RecordState
    path: Path
    ledger: Ledger | None = None
    data: bytes | None = None
    error: str | None = None
    line: int | None = None
    conflict_copies: tuple[Path, ...] = ()


def load_ledger(path: str | Path, schema: LedgerSchema) -> LedgerLoad:
    path = Path(path)
    copies = tuple(files.conflict_copies(path))
    got = files.read_record(path)
    if got.state is not RecordState.PRESENT:
        return LedgerLoad(got.state, path, data=got.data, error=got.error, conflict_copies=copies)
    try:
        ledger = parse_ledger(got.text or "", schema)
    except LedgerInvalidError as exc:
        return LedgerLoad(RecordState.INVALID, path, data=got.data, error=str(exc), line=exc.line, conflict_copies=copies)
    return LedgerLoad(RecordState.PRESENT, path, ledger=ledger, data=got.data, conflict_copies=copies)


# -- append -----------------------------------------------------------------------------


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _confine(project_root: Path, path: Path) -> None:
    try:
        path.resolve().relative_to(project_root.resolve())
    except ValueError as exc:
        raise LedgerWriteRefused(f"{path} is outside the project {project_root}") from exc


def append(
    path: str | Path,
    schema: LedgerSchema,
    fields: Mapping[str, Any],
    *,
    lock: HeldLock,
    project_root: str | Path,
    create: bool = False,
    now: Clock | None = None,
    snapshot_subdir: str | None = None,
) -> LedgerEntry:
    """Append one entry made of `fields` (the schema's fields; `id`, `seq`,
    `at` and `settles` are assigned here) and return it as read back.

    Must run under the project lock (`lock.require_held()`). `create` allows
    a missing file to be founded -- the caller decides that from 9.3's
    trust rules (`rce.records.trust`), never this function. For an undo,
    `undoes` may be omitted: it is filled with the key's current act; if
    given, it must be that act. Snapshots the file first, once a day."""
    lock.require_held()
    path, project_root = Path(path), Path(project_root)
    _confine(project_root, path)
    reserved = [f for f in ("id", "seq", "at", "settles") if f in fields]
    if reserved:
        raise LedgerWriteRefused(f"{', '.join(reserved)} are assigned by the ledger, not the caller")

    loaded = load_ledger(path, schema)
    if loaded.state is RecordState.ABSENT:
        if not create:
            raise LedgerWriteRefused(f"{path.name} does not exist and may not be created here")
        ledger = parse_ledger("", schema)
        old = b""
    elif loaded.state is RecordState.PRESENT:
        ledger, old = loaded.ledger, loaded.data or b""
    else:
        raise LedgerWriteRefused(f"{path.name} cannot be read ({loaded.state.value}: {loaded.error})")
    if loaded.conflict_copies:
        names = ", ".join(p.name for p in loaded.conflict_copies)
        raise LedgerWriteRefused(f"sync conflict copies sit beside {path.name}: {names}")

    entry: dict[str, Any] = {k: v for k, v in fields.items() if v is not None}
    try:
        probe = {**entry, "id": "x"}
        if schema.undo_act is not None and probe.get(schema.act_field) == schema.undo_act:
            probe.setdefault("undoes", "x")  # filled from the ledger below
        _validate_engine_fields(schema, probe)
        schema.validate(entry)
        schema.validate_new(entry)
    except ValueError as exc:
        raise LedgerWriteRefused(str(exc)) from exc
    _reject_line_breaks(entry, schema.table)
    key = schema.key_of(entry)
    state = ledger.state(key)

    if schema.undo_act is not None and entry[schema.act_field] == schema.undo_act:
        if state.conflict is not None:
            raise LedgerWriteRefused("this link's record is in conflict; settle it with a new judgment, not an undo")
        if state.entry is None:
            raise LedgerWriteRefused("there is no act on this link to undo")
        given = entry.get("undoes")
        if given is not None and given != state.entry.id:
            raise LedgerWriteRefused(f"only the last act ({state.entry.id}) can be undone, not {given}")
        entry["undoes"] = state.entry.id

    moment = (now or _local_now)()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    assigned: dict[str, Any] = {
        "id": schema.id_prefix + secrets.token_hex(16),
        "seq": ledger.max_seq + 1,
        "at": moment.isoformat(timespec="seconds"),
    }
    full = {**assigned, **entry}
    if ledger.anomalies:
        full["settles"] = [a.entry.id for a in ledger.anomalies]

    prefix = schema.header.encode("utf-8") if not old else b""
    new_bytes = prefix + emit_entry(schema, full)
    joint = b"\n" if old and not old.endswith(b"\n") else b""
    try:
        after = parse_ledger((old + joint + new_bytes).decode("utf-8").removeprefix("﻿"), schema)
    except LedgerInvalidError as exc:
        raise LedgerWriteRefused(f"the entry would not read back from {path.name} ({exc}); nothing written") from exc
    if after.entry_dicts() != ledger.entry_dicts() + [full]:
        raise LedgerWriteRefused(f"the entry would not read back exactly from {path.name}; nothing written")

    if old:
        files.snapshot_if_first_change_today(project_root, path, snapshot_subdir, now=now)
    try:
        files.append_bytes(path, new_bytes, expected_old=old, create=create)
    except files.RecordFileError as exc:
        raise LedgerWriteRefused(str(exc)) from exc
    return after.entries[-1]


# -- the judgment ledger (9.3) ----------------------------------------------------------

JUDGEMENTS_FILENAME = "judgements.toml"
VERDICTS = frozenset({"confirmed", "rejected", "withdrawn", "undone"})
VIAS = frozenset({"canvas", "cli", "mcp", "migrated", "recovered"})
MAPPING_EXTRACTOR = "mapping"

_JUDGEMENT_HEADER = "# 你对机器提取结果的判断。RCE 只追加、不改写；图谱里的确认/否决从这里派生。\n"


def _validate_basis(value: Any, where: str, depth: int) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{where!r} must be a table")
    for k, v in value.items():
        if isinstance(v, bool):
            raise ValueError(f"{where}.{k}: true/false is not a basis value")
        if isinstance(v, dict):
            if depth >= _MAX_TABLE_DEPTH - 1:
                raise ValueError(f"{where}.{k}: tables nest too deep")
            _validate_basis(v, f"{where}.{k}", depth + 1)
        elif isinstance(v, float) and not math.isfinite(v):
            raise ValueError(f"{where}.{k}: {v!r} is not a finite number")
        elif isinstance(v, list):
            if not all(isinstance(x, str) for x in v):
                raise ValueError(f"{where}.{k}: a list in the basis may only hold strings")
        elif not isinstance(v, (str, int, float)):
            raise ValueError(f"{where}.{k}: {type(v).__name__} is not a basis value")


def _validate_judgement(entry: Mapping[str, Any]) -> None:
    if entry.get("extractor") == MAPPING_EXTRACTOR:
        raise ValueError(
            "a hand-drawn link (extractor \"mapping\") is recorded in .rce/mappings.toml, never in the ledger"
        )
    via = entry.get("via")
    if via is not None and via not in VIAS:
        raise ValueError(f"'via' must be one of {', '.join(sorted(VIAS))}, got {via!r}")
    for name in ("note", "basis_recorded", "migrated_from", "contradicts"):
        if name in entry and not isinstance(entry[name], str):
            raise ValueError(f"{name!r} must be a string, got {entry[name]!r}")
    if "basis" in entry:
        _validate_basis(entry["basis"], "basis", 0)


def _validate_new_judgement(entry: Mapping[str, Any]) -> None:
    if entry.get("via") not in VIAS:
        raise ValueError(f"'via' must be one of {', '.join(sorted(VIAS))}, got {entry.get('via')!r}")


JUDGEMENT_SCHEMA = LedgerSchema(
    table="judgement",
    id_prefix="j-",
    act_field="verdict",
    acts=VERDICTS,
    undo_act="undone",
    key_fields=("src", "dst", "type", "extractor"),
    field_order=(
        "id", "seq", "at", "verdict", "src", "dst", "type", "extractor", "via", "undoes", "note",
        "migrated_from", "contradicts", "basis_recorded", "basis",
    ),
    header=_JUDGEMENT_HEADER,
    validate=_validate_judgement,
    validate_new=_validate_new_judgement,
)


def judgements_path(project_root: str | Path) -> Path:
    return Path(project_root) / paths.RCE_DIRNAME / JUDGEMENTS_FILENAME


def load_judgements(project_root: str | Path) -> LedgerLoad:
    return load_ledger(judgements_path(project_root), JUDGEMENT_SCHEMA)


def append_judgement(
    project_root: str | Path,
    *,
    lock: HeldLock,
    verdict: str,
    src: str,
    dst: str,
    type: str,  # noqa: A002 -- the ledger's own field name
    extractor: str,
    via: str,
    note: str | None = None,
    basis: Mapping[str, Any] | None = None,
    basis_recorded: str | None = None,
    undoes: str | None = None,
    create: bool = False,
    now: Clock | None = None,
) -> LedgerEntry:
    """Append one judgment to `.rce/judgements.toml` (see `append`). The
    caller -- the one record-write path of a later phase -- has already
    consulted `rce.records.trust` and passes `create=True` only when it
    says a first ledger may be founded."""
    fields = {
        "verdict": verdict,
        "src": src,
        "dst": dst,
        "type": type,
        "extractor": extractor,
        "via": via,
        "undoes": undoes,
        "note": note,
        "basis_recorded": basis_recorded,
        "basis": dict(basis) if basis is not None else None,
    }
    return append(
        judgements_path(project_root), JUDGEMENT_SCHEMA, fields,
        lock=lock, project_root=project_root, create=create, now=now,
    )


def contradicts(state: KeyState) -> str | None:
    """The id of the entry a link's current act contradicts, when that act
    is a migrated judgment that disagreed with the state the ledger already
    had (DESIGN.md 9.5 step 1: two machines disagreed, and a migration date
    must not decide between them). Such a link is in conflict until the
    researcher writes a new entry for it -- any later act replaces the
    contradicting one as the link's current act."""
    if state.entry is None:
        return None
    target = state.entry.get("contradicts")
    return target if isinstance(target, str) and target else None


def judgement_status(state: KeyState) -> str | None:
    """What the ledger says a link's human status is: "confirmed",
    "rejected", "conflict" (two merged histories, or a migrated judgment
    contradicting the one already recorded), or None (no judgment stands,
    or it was withdrawn -- the machine's status applies)."""
    if state.conflict is not None or contradicts(state) is not None:
        return "conflict"
    if state.entry is None:
        return None
    verdict = state.entry.data["verdict"]
    return verdict if verdict in ("confirmed", "rejected") else None


def entries_by_id(entries: Iterable[LedgerEntry]) -> dict[str, Mapping[str, Any]]:
    """`{id: entry data}` -- the shape the index keeps of what it applied,
    and what `rce.records.trust.assess_ledger` compares against."""
    return {e.id: e.data for e in entries}
