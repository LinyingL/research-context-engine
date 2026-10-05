"""What the researcher's records are, and whether the index mirrors them
(DESIGN.md 9.2, 9.8; task V5 phase 5): `rce records`, `rce records
--verify`, `rce records --clean`.

The inventory
-------------

`inventory(conn, root)` lists every kind of human labor 9.2 names, where it
lives, how many there are, its newest snapshot, and anything that stands
between RCE and trusting it -- unreadable, in the cloud, a sync conflict
copy beside it, a ledger that has shrunk, a pre-V5 index still holding
judgments that wait for `rce migrate`. It reads only. The rows are data
(`Row`); the CLI words them in English, the app (a later phase) in its
own language.

Mirrors
-------

Two kinds of human labor are not in the ledger: attempt verdicts (the
Markdown table) and hand-drawn links (`.rce/mappings.toml`). The index
mirrors them; `verify_mirrors` says, per attempt and per link, where the
index differs from the file. A file that cannot be read, or a table that
cannot be located, is reported as such and compared with nothing --
failing to read the source is not evidence of a difference (Section 4).
The same function is what the migration (9.5 step 4) runs on the new index
(it must equal the files) and on the OLD index (its differences are a
stale mirror: listed, never a failure).

Clean
-----

`clean(root, apply=...)` removes copies nothing refers to. The kinds of
copy are a registry (`COPY_STORES`), each naming a directory under `.rce/`
and a function that returns every copy some record refers to -- or None
when that cannot be told (a log that cannot be read). The rule that makes
it safe: **a store whose references cannot all be read is not cleaned at
all**, and only regular files directly inside the store's directory are
candidates. Dry-run is the default; `apply=True` deletes, under the
project lock with the identity re-checked. The stores registered now are
the variable cards' frozen and code copies of 9.11 (the cards themselves
arrive in a later phase; their log format is fixed by the design, and the
crash order "copies first, the entry that names them last" is what leaves
unreferenced copies behind).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Callable, Iterable

from rce import db, paths
from rce.ingest import attempts as attempts_ingest
from rce.ingest import mappings as mappings_ingest
from rce.records import files as record_files
from rce.records import judgements
from rce.records import ledger as ledger_mod
from rce.records.files import RecordState
from rce.records.identity import IdentityState, read_identity
from rce.records.situation import READ_NOW, write_guard

VARIABLES_DIRNAME = "variables"
CODE_DIRNAME = "_code"
FROZEN_DIRNAME = "frozen"
LOG_FILENAME = "log.toml"


# -- mirrors ---------------------------------------------------------------------


def mapping_differences(conn: Connection, root: Path) -> list[str] | None:
    """Where the index's hand-drawn links differ from `.rce/mappings.toml`
    (English, one line each); None when the file cannot be compared (it is
    absent -- not a deletion, 8.12 -- or cannot be read)."""
    try:
        loaded = mappings_ingest.load_mappings(root)
    except mappings_ingest.MappingsFileError:
        return None
    if not loaded.file_present:
        return None
    want = {(m.src_id, m.dst_id, m.type) for m in loaded.mappings}
    have = {
        (e["src"], e["dst"], e["type"]): e["status"]
        for e in db.query_edges(conn) if e["extractor"] == mappings_ingest.EXTRACTOR
    }
    problems = []
    for key in sorted(want - set(have)):
        problems.append(f"hand-drawn link {key[0]} --{key[2]}--> {key[1]}: in mappings.toml, not in the index")
    for key in sorted(set(have) - want):
        problems.append(f"hand-drawn link {key[0]} --{key[2]}--> {key[1]}: in the index, not in mappings.toml")
    for key in sorted(want & set(have)):
        if have[key] != "confirmed":
            problems.append(f"hand-drawn link {key[0]} --{key[2]}--> {key[1]}: index status {have[key]!r}, not 'confirmed'")
    return problems


def attempt_differences(conn: Connection, root: Path) -> list[str] | None:
    """Where the index's attempt verdicts and results differ from the
    attempt table; None when there is no config, the file cannot be read,
    or the table cannot be located."""
    try:
        config = attempts_ingest.load_config(root)
    except attempts_ingest.AttemptsConfigError:
        return None
    got = record_files.read_record(root / config.file)
    if got.state is not RecordState.PRESENT:
        return None
    try:
        rows = attempts_ingest.parse_attempts_table(got.text or "", config.heading, config.columns)
    except attempts_ingest.AttemptsConfigError:
        return None
    want: dict[str, dict[str, str]] = {}
    for row in rows:
        node_id = f"attempt:{config.file}#{row.number}"
        want.setdefault(node_id, {"verdict": row.verdict, "result": row.result})
    have = {
        n["id"]: n.get("human_fields") or {}
        for n in db.get_nodes_by_type(conn, "attempt")
        if (n.get("attrs") or {}).get("source_file") == config.file
    }
    problems = []
    for node_id in sorted(set(want) - set(have)):
        problems.append(f"attempt {node_id}: in the table, not in the index")
    for node_id in sorted(set(have) - set(want)):
        problems.append(f"attempt {node_id}: in the index, not in the table")
    for node_id in sorted(set(want) & set(have)):
        mine = {k: have[node_id].get(k) for k in ("verdict", "result")}
        if mine != want[node_id]:
            problems.append(f"attempt {node_id}: index says {mine}, the table says {want[node_id]}")
    return problems


def verify_mirrors(conn: Connection, root: Path) -> list[str]:
    """Every difference between the index and the two mirrored files
    (empty when it mirrors them, or when a file cannot be compared)."""
    return [*(mapping_differences(conn, root) or []), *(attempt_differences(conn, root) or [])]


def verify(conn: Connection, root: Path) -> list[str]:
    """`rce records --verify`: per link, the index's human state is exactly
    what the record implies -- the judgment ledger (`judgements.verify`),
    the hand-drawn links and the attempt verdicts."""
    return [*judgements.verify(conn, root), *verify_mirrors(conn, root)]


# -- the inventory -----------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One kind of human labor (9.2). `path` is where it lives (relative to
    the project when inside it); `count` how many, in words; `problems`
    what stands between RCE and trusting it."""

    kind: str
    path: str
    count: str
    snapshot: str | None = None
    problems: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": self.path, "count": self.count, "snapshot": self.snapshot, "problems": list(self.problems)}


def _shown(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _snapshot(root: Path, path: Path) -> str | None:
    newest = record_files.newest_snapshot(root, path)
    return newest.name if newest is not None else None


def _file_problems(path: Path) -> list[str]:
    problems = []
    got = record_files.read_record(path)
    if got.state is RecordState.DATALESS:
        problems.append("in the cloud (download requested)")
    elif got.state is RecordState.UNREADABLE:
        problems.append(f"unreadable ({got.error})")
    copies = record_files.conflict_copies(path)
    if copies:
        problems.append(f"sync conflict copies beside it: {', '.join(p.name for p in copies)}")
    return problems


def _judgement_row(conn: Connection | None, root: Path) -> Row:
    loaded = ledger_mod.load_judgements(root)
    ident = read_identity(root)
    identity = ident.identity if ident.state is IdentityState.PRESENT else None
    problems: list[str] = []
    if loaded.ledger is not None:
        n = len(loaded.ledger.entries)
        stands = sum(
            1 for key in loaded.ledger.keys()
            if ledger_mod.judgement_status(loaded.ledger.state(key)) in ("confirmed", "rejected")
        )
        conflicts = sum(1 for key in loaded.ledger.keys() if ledger_mod.judgement_status(loaded.ledger.state(key)) == "conflict")
        count = f"{n} entr(y/ies), {stands} judgment(s) standing" + (f", {conflicts} in conflict" if conflicts else "")
    elif loaded.state is RecordState.ABSENT:
        count = "none yet"
    else:
        count = loaded.state.value
    if conn is not None:
        _loaded, decision = judgements.assess(conn, root, identity, for_migration=True)
        if decision.reason == "shrunk":
            problems.append(
                f"shrunk: the file lacks {len(decision.missing)} entr(y/ies) the index applied "
                f"-- answer with 'rce records --answer file|restore'"
            )
        elif decision.reason is not None:
            problems.append(f"{decision.reason}: {decision.detail or ''}".rstrip(": "))
    else:
        problems.extend(_file_problems(loaded.path))
    if loaded.state is RecordState.INVALID:
        where = f" (line {loaded.line})" if loaded.line else ""
        problems.append(f"invalid{where}: {loaded.error}")
    if identity is not None and identity.migrating_from is not None:
        problems.append(f"migration from {identity.migrating_from} not finished -- run 'rce migrate'")
    return Row("Confirm/reject of machine links", _shown(loaded.path, root), count, _snapshot(root, loaded.path), tuple(dict.fromkeys(problems)))


def _mapping_row(root: Path) -> Row:
    path = mappings_ingest.mappings_path(root)
    problems = _file_problems(path)
    try:
        loaded = mappings_ingest.load_mappings(root)
    except mappings_ingest.MappingsFileError as exc:
        return Row("Hand-drawn links", _shown(path, root), "cannot be read", _snapshot(root, path), (*problems, str(exc)))
    if not loaded.file_present:
        return Row("Hand-drawn links", _shown(path, root), "none yet", None, tuple(problems))
    notes = sum(1 for m in loaded.mappings if m.note)
    count = f"{len(loaded.mappings)} link(s), {notes} with a note"
    if loaded.problems:
        problems.append(f"{len(loaded.problems)} entr(y/ies) refused (see 'rce mappings')")
    return Row("Hand-drawn links", _shown(path, root), count, _snapshot(root, path), tuple(problems))


def _attempt_rows(root: Path) -> list[Row]:
    config_path = root / attempts_ingest.CONFIG_RELATIVE_PATH
    try:
        config = attempts_ingest.load_config(root)
    except attempts_ingest.AttemptsConfigError as exc:
        configured = config_path.exists()
        return [
            Row("Attempt verdicts", "-", "no attempt table configured" if not configured else "configuration not usable",
                None, () if not configured else (str(exc),)),
            Row("Attempt-table configuration", _shown(config_path, root), "present" if configured else "none",
                _snapshot(root, config_path) if configured else None, tuple(_file_problems(config_path))),
        ]
    table = root / config.file
    problems = _file_problems(table)
    got = record_files.read_record(table)
    if got.state is RecordState.PRESENT:
        try:
            rows = attempts_ingest.parse_attempts_table(got.text or "", config.heading, config.columns)
            judged = sum(1 for r in rows if r.verdict.strip())
            count = f"{len(rows)} attempt(s), {judged} with a verdict"
        except attempts_ingest.AttemptsConfigError as exc:
            count = "table not found"
            problems.append(str(exc))
    else:
        count = got.state.value
    return [
        Row("Attempt verdicts", _shown(table, root), count, _snapshot(root, table), tuple(problems)),
        Row("Attempt-table configuration", _shown(config_path, root), "present", _snapshot(root, config_path), tuple(_file_problems(config_path))),
    ]


def _canvas_row(root: Path) -> Row:
    from rce.webapp import canvas as canvas_mod  # noqa: PLC0415 -- the webapp imports this module's callers

    layout = canvas_mod.layout_record(root)
    problems = [] if layout.writable else [f"{layout.state}: {layout.error}"]
    problems += [p for p in _file_problems(layout.path) if "conflict" in p]
    return Row("Canvas arrangement", _shown(layout.path, root), layout.describe(), _snapshot(root, layout.path), tuple(problems))


def _variables_row(root: Path) -> Row:
    directory = paths.project_rce_dir(root) / VARIABLES_DIRNAME
    if not directory.is_dir():
        return Row("Variable definition cards", _shown(directory, root), "none yet")
    try:
        cards = sorted(p.name for p in directory.iterdir() if p.is_dir() and p.name != CODE_DIRNAME)
    except OSError as exc:
        return Row("Variable definition cards", _shown(directory, root), "cannot be listed", None, (str(exc),))
    return Row("Variable definition cards", _shown(directory, root), f"{len(cards)} card(s)")


def inventory(conn: Connection | None, root: Path) -> list[Row]:
    """The 9.2 inventory of `root` (module docstring). `conn` is the index,
    when there is one, for the shrink check; reads only."""
    root = Path(root)
    rows = [_judgement_row(conn, root), _mapping_row(root), *_attempt_rows(root), _canvas_row(root), _variables_row(root)]
    waiting = paths.legacy_sources(root)
    if waiting:
        rows.append(Row(
            "Pre-V5 index holding judgments", ", ".join(str(p) for _k, p in waiting), f"{len(waiting)} waiting",
            None, ("needs migration: 'rce migrate --list', then 'rce migrate --yes'",),
        ))
    return rows


# -- clean -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CopyStore:
    """A kind of kept copy (module docstring, "Clean"). `directories(root)`
    yields `(directory, referenced)` pairs: `referenced` is the set of file
    names in that directory some record refers to, or None when that cannot
    be told -- and then nothing in it is removed."""

    name: str
    directories: Callable[[Path], Iterable[tuple[Path, set[str] | None]]]


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _log_references(card: Path) -> set[str] | None:
    """Every string a card's `log.toml` holds (its entries name their copies
    as `frozen/<hash>.toml` and `_code/<hash>.<ext>`), or None when the log
    exists and cannot be read: then no copy is known to be unreferenced."""
    got = record_files.read_record(card / LOG_FILENAME)
    if got.state is RecordState.ABSENT:
        return set()
    if got.state is not RecordState.PRESENT or record_files.conflict_copies(card / LOG_FILENAME):
        return None
    try:
        data = tomllib.loads(got.text or "")
    except tomllib.TOMLDecodeError:
        return None
    return set(_strings(data))


def _cards(root: Path) -> list[Path] | None:
    directory = paths.project_rce_dir(root) / VARIABLES_DIRNAME
    if not directory.is_dir():
        return []
    try:
        return sorted(p for p in directory.iterdir() if p.is_dir() and not p.is_symlink() and p.name != CODE_DIRNAME)
    except OSError:
        return None


def _frozen_dirs(root: Path) -> Iterable[tuple[Path, set[str] | None]]:
    for card in _cards(root) or []:
        refs = _log_references(card)
        names = None if refs is None else {r.split("/", 1)[1] for r in refs if r.startswith(FROZEN_DIRNAME + "/")}
        yield card / FROZEN_DIRNAME, names


def _code_dirs(root: Path) -> Iterable[tuple[Path, set[str] | None]]:
    directory = paths.project_rce_dir(root) / VARIABLES_DIRNAME / CODE_DIRNAME
    cards = _cards(root)
    if cards is None:
        yield directory, None
        return
    names: set[str] | None = set()
    for card in cards:
        refs = _log_references(card)
        if refs is None:
            names = None
            break
        names |= {r.split("/", 1)[1] for r in refs if r.startswith(CODE_DIRNAME + "/")}
    yield directory, names


COPY_STORES: list[CopyStore] = [
    CopyStore("variable frozen copies", _frozen_dirs),
    CopyStore("variable code copies", _code_dirs),
]


@dataclass
class CleanReport:
    """`removable` (or `removed`, with `apply=True`) are project-relative
    paths; `undecidable` names the directories left alone because their
    references could not all be read."""

    removable: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    undecidable: list[str] = field(default_factory=list)


def _within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _scan_clean(root: Path, stores: list[CopyStore]) -> tuple[list[Path], list[str]]:
    removable: list[Path] = []
    undecidable: list[str] = []
    for store in stores:
        for directory, referenced in store.directories(root):
            if not directory.is_dir():
                continue
            shown = _shown(directory, root)
            if referenced is None or not _within(root, directory) or directory.is_symlink():
                undecidable.append(shown)
                continue
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                undecidable.append(shown)
                continue
            for entry in entries:
                if entry.name in referenced or entry.is_symlink() or not entry.is_file():
                    continue
                removable.append(entry)
    return removable, undecidable


def clean(root: Path, *, apply: bool = False, expected_id: Any = READ_NOW, stores: list[CopyStore] | None = None) -> CleanReport:
    """`rce records --clean` (module docstring). With `apply=True` the
    candidates are listed again under the project lock and only those are
    deleted."""
    root = Path(root)
    stores = COPY_STORES if stores is None else stores
    report = CleanReport()
    if not apply:
        found, report.undecidable = _scan_clean(root, stores)
        report.removable = [_shown(p, root) for p in found]
        return report
    with write_guard(root, expected_id, human=True):
        found, report.undecidable = _scan_clean(root, stores)
        report.removable = [_shown(p, root) for p in found]
        for path in found:
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            report.removed.append(_shown(path, root))
    return report
