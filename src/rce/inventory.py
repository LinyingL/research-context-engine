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
    the hand-drawn links and the attempt verdicts -- and the index holds
    every trusted variable card's log entries (`cards.verify`)."""
    from rce.records import cards as cards_mod  # noqa: PLC0415

    return [*judgements.verify(conn, root), *verify_mirrors(conn, root), *cards_mod.verify(conn, root)]


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
    #: For the app, which words a row in its own language (8.8): a stable
    #: name for the kind (`judgements`, `mappings`, `attempts`,
    #: `attempts_config`, `canvas`, `variables`, `legacy`), the numbers and
    #: states behind `count`, and `issues` -- machine-readable names of
    #: `problems` (`dataless`, `unreadable`, `conflict_copy`, `invalid`,
    #: `refused_entries`, `migration_unfinished`, `needs_migration`, or a
    #: ledger trust reason such as `shrunk`). `problems` stays the English.
    code: str = ""
    facts: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "path": self.path, "count": self.count, "snapshot": self.snapshot,
            "problems": list(self.problems), "code": self.code, "facts": dict(self.facts),
        }


def _shown(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _snapshot(root: Path, path: Path) -> str | None:
    newest = record_files.newest_snapshot(root, path)
    return newest.name if newest is not None else None


def _file_problems(path: Path, issues: list[str] | None = None) -> list[str]:
    """English problems of one record file; their names go into `issues`."""
    problems = []
    got = record_files.read_record(path)
    if got.state is RecordState.DATALESS:
        problems.append("in the cloud (download requested)")
        if issues is not None:
            issues.append("dataless")
    elif got.state is RecordState.UNREADABLE:
        problems.append(f"unreadable ({got.error})")
        if issues is not None:
            issues.append("unreadable")
    copies = record_files.conflict_copies(path)
    if copies:
        problems.append(f"sync conflict copies beside it: {', '.join(p.name for p in copies)}")
        if issues is not None:
            issues.append("conflict_copy")
    return problems


def _judgement_row(conn: Connection | None, root: Path) -> Row:
    loaded = ledger_mod.load_judgements(root)
    ident = read_identity(root)
    identity = ident.identity if ident.state is IdentityState.PRESENT else None
    problems: list[str] = []
    issues: list[str] = []
    facts: dict[str, Any] = {"file_state": loaded.state.value, "exists": loaded.path.exists()}
    if loaded.ledger is not None:
        n = len(loaded.ledger.entries)
        stands = sum(
            1 for key in loaded.ledger.keys()
            if ledger_mod.judgement_status(loaded.ledger.state(key)) in ("confirmed", "rejected")
        )
        conflicts = sum(1 for key in loaded.ledger.keys() if ledger_mod.judgement_status(loaded.ledger.state(key)) == "conflict")
        count = f"{n} entr(y/ies), {stands} judgment(s) standing" + (f", {conflicts} in conflict" if conflicts else "")
        facts.update(entries=n, standing=stands, conflicts=conflicts)
    elif loaded.state is RecordState.ABSENT:
        count = "none yet"
    else:
        count = loaded.state.value
    if conn is not None:
        _loaded, decision = judgements.assess(conn, root, identity, for_migration=True)
        if decision.reason is not None:
            issues.append(decision.reason)
            facts.update(trust_message=decision.message, missing=len(decision.missing), line=decision.line)
        if decision.reason == "shrunk":
            problems.append(
                f"shrunk: the file lacks {len(decision.missing)} entr(y/ies) the index applied "
                f"-- answer with 'rce records --answer file|restore'"
            )
        elif decision.reason is not None:
            problems.append(f"{decision.reason}: {decision.detail or ''}".rstrip(": "))
    else:
        problems.extend(_file_problems(loaded.path, issues))
    if loaded.state is RecordState.INVALID:
        where = f" (line {loaded.line})" if loaded.line else ""
        problems.append(f"invalid{where}: {loaded.error}")
        issues.append("invalid")
        facts["line"] = loaded.line
    if identity is not None and identity.migrating_from is not None:
        problems.append(f"migration from {identity.migrating_from} not finished -- run 'rce migrate'")
        issues.append("migration_unfinished")
    facts["issues"] = list(dict.fromkeys(issues))
    return Row("Confirm/reject of machine links", _shown(loaded.path, root), count, _snapshot(root, loaded.path),
               tuple(dict.fromkeys(problems)), code="judgements", facts=facts)


def _mapping_row(root: Path) -> Row:
    path = mappings_ingest.mappings_path(root)
    issues: list[str] = []
    problems = _file_problems(path, issues)
    facts: dict[str, Any] = {"exists": path.exists(), "issues": issues}
    try:
        loaded = mappings_ingest.load_mappings(root)
    except mappings_ingest.MappingsFileError as exc:
        issues.append("unreadable")
        facts.update(state="unreadable", issues=list(dict.fromkeys(issues)))
        return Row("Hand-drawn links", _shown(path, root), "cannot be read", _snapshot(root, path), (*problems, str(exc)),
                   code="mappings", facts=facts)
    if not loaded.file_present:
        facts["state"] = "absent"
        return Row("Hand-drawn links", _shown(path, root), "none yet", None, tuple(problems), code="mappings", facts=facts)
    notes = sum(1 for m in loaded.mappings if m.note)
    count = f"{len(loaded.mappings)} link(s), {notes} with a note"
    facts.update(state="present", links=len(loaded.mappings), with_note=notes, refused=len(loaded.problems))
    if loaded.problems:
        problems.append(f"{len(loaded.problems)} entr(y/ies) refused (see 'rce mappings')")
        issues.append("refused_entries")
    return Row("Hand-drawn links", _shown(path, root), count, _snapshot(root, path), tuple(problems), code="mappings", facts=facts)


def _attempt_rows(root: Path) -> list[Row]:
    config_path = root / attempts_ingest.CONFIG_RELATIVE_PATH
    try:
        config = attempts_ingest.load_config(root)
    except attempts_ingest.AttemptsConfigError as exc:
        configured = config_path.exists()
        config_issues: list[str] = []
        config_problems = tuple(_file_problems(config_path, config_issues))
        return [
            Row("Attempt verdicts", "-", "no attempt table configured" if not configured else "configuration not usable",
                None, () if not configured else (str(exc),), code="attempts",
                facts={"state": "unusable" if configured else "not_configured", "exists": False,
                       "issues": ["invalid"] if configured else []}),
            Row("Attempt-table configuration", _shown(config_path, root), "present" if configured else "none",
                _snapshot(root, config_path) if configured else None, config_problems, code="attempts_config",
                facts={"exists": configured, "issues": config_issues}),
        ]
    table = root / config.file
    issues: list[str] = []
    problems = _file_problems(table, issues)
    facts: dict[str, Any] = {"exists": table.exists(), "issues": issues}
    got = record_files.read_record(table)
    if got.state is RecordState.PRESENT:
        try:
            rows = attempts_ingest.parse_attempts_table(got.text or "", config.heading, config.columns)
            judged = sum(1 for r in rows if r.verdict.strip())
            count = f"{len(rows)} attempt(s), {judged} with a verdict"
            facts.update(state="present", attempts=len(rows), with_verdict=judged)
        except attempts_ingest.AttemptsConfigError as exc:
            count = "table not found"
            problems.append(str(exc))
            facts["state"] = "table_missing"
    else:
        count = got.state.value
        facts["state"] = got.state.value
    config_issues = []
    config_problems = tuple(_file_problems(config_path, config_issues))
    return [
        Row("Attempt verdicts", _shown(table, root), count, _snapshot(root, table), tuple(problems), code="attempts", facts=facts),
        Row("Attempt-table configuration", _shown(config_path, root), "present", _snapshot(root, config_path), config_problems,
            code="attempts_config", facts={"exists": True, "issues": config_issues}),
    ]


def _canvas_row(root: Path) -> Row:
    from rce.webapp import canvas as canvas_mod  # noqa: PLC0415 -- the webapp imports this module's callers

    layout = canvas_mod.layout_record(root)
    problems = [] if layout.writable else [f"{layout.state}: {layout.error}"]
    issues = [] if layout.writable else ["dataless" if layout.state == "dataless" else "invalid"]
    file_issues: list[str] = []
    problems += [p for p in _file_problems(layout.path, file_issues) if "conflict" in p]
    issues += [i for i in file_issues if i == "conflict_copy"]
    facts = {
        "state": layout.state, "exists": layout.path.exists(), "views": len(layout.views),
        "arranged": sum(1 for v in layout.views.values() if v["positions"]), "issues": issues,
    }
    return Row("Canvas arrangement", _shown(layout.path, root), layout.describe(), _snapshot(root, layout.path), tuple(problems),
               code="canvas", facts=facts)


def _variables_row(conn: Connection | None, root: Path) -> Row:
    """9.11's cards: how many, how many drafts, and per card what stands
    between RCE and trusting it -- unreadable (two ids equal up to case, a
    sync conflict copy), frozen (a log missing, in the cloud, invalid, or
    with fewer entries than the index applied), a confirmed version edited
    after confirmation, a frozen copy missing, the dead-variable list
    disagreeing."""
    from rce.records import cards as cards_mod  # noqa: PLC0415 -- cards imports judgements, as this module does
    from rce.records import variables as variables_mod  # noqa: PLC0415

    directory = variables_mod.variables_dir(root)
    shown = _shown(directory, root)
    if variables_mod.card_dirs(root) is None:
        return Row("Variable definition cards", shown, "cannot be listed", None, ("the folder cannot be listed",),
                   code="variables", facts={"exists": True, "issues": ["unreadable"]})
    found = cards_mod.overview(conn, root)
    if not found and not directory.is_dir():
        return Row("Variable definition cards", shown, "none yet", code="variables",
                   facts={"exists": False, "cards": 0, "issues": []})
    problems: list[str] = []
    issues: list[str] = []
    for card in found:
        trust = card.get("trust") or {}
        if card["state"] == "unreadable":
            problems.append(f"{card['id']}: unreadable -- {card['detail']}")
            issues.append(card["reason"] or "unreadable")
        elif trust.get("state") == "shrunk":
            problems.append(
                f"{card['id']}: log.toml lacks {len(trust['missing'])} entr(y/ies) the index applied "
                f"-- answer with 'rce variable answer {card['id']} file|restore'"
            )
            issues.append("shrunk")
        elif trust.get("state") not in (None, "ok"):
            problems.append(f"{card['id']}: frozen -- {trust.get('reason')}: {trust.get('detail') or ''}".rstrip(": "))
            issues.append(trust.get("reason") or "refuse_writes")
        for q in card["questions"]:
            problems.append(f"{card['id']}: v{q['version']} was changed after it was confirmed "
                            f"-- 'rce variable answer {card['id']} new|correct'")
            issues.append("edited_after_confirmation")
        for v in card["versions"]:
            if v["copy_missing"]:
                problems.append(f"{card['id']}: the frozen copy of v{v['version']} is missing")
                issues.append("copy_missing")
        problems += [f"{card['id']}: {p}" for p in card["problems"] if card["state"] != "unreadable"]
        for flag in card["dead_flags"]:
            problems.append(f"{card['id']}: the dead-variable list disagrees ({flag['direction']})")
            issues.append("dead_variable_disagreement")
    drafts = sum(1 for c in found if c["draft"] is not None)
    count = f"{len(found)} card(s), {drafts} draft(s)"
    newest = None
    for card in found:
        snap = record_files.newest_snapshot(root, directory / card["id"] / variables_mod.LOG_FILENAME,
                                            variables_mod.snapshot_subdir(card["id"]))
        if snap is not None and (newest is None or snap.name > newest):
            newest = snap.name
    return Row("Variable definition cards", shown, count, newest, tuple(dict.fromkeys(problems)), code="variables",
               facts={"exists": True, "cards": len(found), "drafts": drafts, "issues": list(dict.fromkeys(issues))})


def card_file_paths(root: Path) -> list[tuple[Path, str]]:
    """The files of every card that get a daily snapshot, each with its
    snapshot subdirectory (`.rce/backups/variables/<id>/`, 9.11): the log
    and the version files. Frozen and code copies are never rotated, and
    need none."""
    from rce.records import variables as variables_mod  # noqa: PLC0415

    found: list[tuple[Path, str]] = []
    for card in variables_mod.card_dirs(root) or []:
        sub = variables_mod.snapshot_subdir(card.name)
        found.append((card / variables_mod.LOG_FILENAME, sub))
        try:
            names = sorted(p.name for p in card.iterdir())
        except OSError:
            continue
        found += [(card / n, sub) for n in names if variables_mod.VERSION_FILE_RE.match(n)]
    return found


def record_file_paths(root: Path) -> list[Path]:
    """The single-file records of 9.2 that get a daily snapshot: the
    judgment ledger, `mappings.toml`, `attempts.toml`, the arrangement,
    and the attempt table `attempts.toml` names."""
    root = Path(root)
    rce_dir = paths.project_rce_dir(root)
    found = [
        ledger_mod.judgements_path(root),
        mappings_ingest.mappings_path(root),
        root / attempts_ingest.CONFIG_RELATIVE_PATH,
        rce_dir / "canvas.json",
    ]
    try:
        config = attempts_ingest.load_config(root)
    except attempts_ingest.AttemptsConfigError:
        return found
    found.append(root / config.file)
    return found


def snapshot_records(root: Path, only: Iterable[str | Path] | None = None) -> list[Path]:
    """9.2's "a snapshot the first time RCE sees the file changed each day
    -- which is what covers hand edits": called by whatever sees a record
    file (the watcher, on first sight of a project and on every change it
    observes). `only` limits it to those paths. Each file is snapshotted at
    most once a day and only when it differs from its newest snapshot
    (`files.snapshot_if_first_change_today`). Only for a project with a V5
    identity whose migration has finished: a pre-V5 folder is read-only.
    Failures are logged and contained -- a snapshot never stops a scan.
    The caller holds the project lock."""
    import logging  # noqa: PLC0415 -- leaf use

    root = Path(root)
    got = read_identity(root)
    if got.state is not IdentityState.PRESENT or got.identity is None or got.identity.migrating_from is not None:
        return []
    wanted = None if only is None else {str(p) for p in only}
    made: list[Path] = []
    targets: list[tuple[Path, str | None]] = [(p, None) for p in record_file_paths(root)] + list(card_file_paths(root))
    for path, subdir in targets:
        if wanted is not None and str(path) not in wanted:
            continue
        try:
            snap = record_files.snapshot_if_first_change_today(root, path, subdir)
        except Exception as exc:  # noqa: BLE001 -- contained (docstring)
            logging.getLogger(__name__).warning("could not snapshot %s: %s", path, exc)
            continue
        if snap is not None:
            made.append(snap)
    return made


def inventory(conn: Connection | None, root: Path) -> list[Row]:
    """The 9.2 inventory of `root` (module docstring). `conn` is the index,
    when there is one, for the shrink check; reads only."""
    root = Path(root)
    rows = [_judgement_row(conn, root), _mapping_row(root), *_attempt_rows(root), _canvas_row(root), _variables_row(conn, root)]
    waiting = paths.legacy_sources(root)
    if waiting:
        rows.append(Row(
            "Pre-V5 index holding judgments", ", ".join(str(p) for _k, p in waiting), f"{len(waiting)} waiting",
            None, ("needs migration: 'rce migrate --list', then 'rce migrate --yes'",),
            code="legacy", facts={"waiting": len(waiting), "exists": False, "issues": ["needs_migration"]},
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
    as `frozen/<hash>.toml` and `_code/<hash>.<ext>`), or None when that
    cannot be told: the log exists and cannot be read, or the card cannot
    be trusted -- unreadable, its log missing though expected, or (against
    the index) lacking entries the index applied, whose copies may be the
    only text left of them."""
    from rce.records import cards as cards_mod  # noqa: PLC0415
    from rce.records import variables as variables_mod  # noqa: PLC0415

    got = record_files.read_record(card / LOG_FILENAME)
    if got.state not in (RecordState.ABSENT, RecordState.PRESENT) or record_files.conflict_copies(card / LOG_FILENAME):
        return None
    root = card.parent.parent.parent
    read = variables_mod.read_card(root, card)
    ident = read_identity(root)
    identity = ident.identity if ident.state is IdentityState.PRESENT else None
    conn = None
    if identity is not None and paths.index_dir(identity.id).joinpath(paths.DB_FILENAME).exists():
        conn = db.connect(paths.index_dir(identity.id) / paths.DB_FILENAME)
    try:
        decision = cards_mod.assess_card(conn, root, read, identity) if identity is not None else None
    finally:
        if conn is not None:
            conn.close()
    if decision is None or not decision.may_apply or read.state != "ok":
        return None
    if got.state is RecordState.ABSENT:
        return set()
    try:
        data = tomllib.loads(got.text or "")
    except tomllib.TOMLDecodeError:
        return None
    return set(_strings(data))


def _cards(root: Path) -> list[Path] | None:
    from rce.records import variables as variables_mod  # noqa: PLC0415

    return variables_mod.card_dirs(root)


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
