"""Human mappings: `.rce/mappings.toml` and its ingest (DESIGN.md sections
8.1 and 8.5, task V4 phase 1a).

Why a file, not a table in the graph
------------------------------------

Section 4's doctrine -- the researcher's own file is the truth, the graph
resyncs from it -- applies to hand-drawn links exactly as it applies to
attempt verdicts. A link the user draws on the canvas is therefore appended
to `.rce/mappings.toml`, a file they can read, diff and commit; the graph
edge is *derived* from it by `ingest_mappings`, never written directly by
the UI. Delete `graph.db` and nothing a human asserted is lost.

The file
--------

::

    # 手工标注的映射。RCE 只记录它能读到的；它读不到的，由你在这里补上。
    # 此文件是唯一真相：图谱里的人工连线从它派生，删掉 graph.db 也不会丢。
    [[mapping]]
    from = "复现包_分步/17-叙事更替与汇率波动.Rmd"
    to   = "复现包_分步/17-叙事更替与汇率波动.pdf"
    type = "generates"                 # reads | writes | generates
    note = "knitr 渲染产出"             # 可选
    date = "2026-09-06"                # 标注日期（本地日期，写入时填）

File-entry direction vs stored edge direction
---------------------------------------------

An entry's `from` -> `to` is the direction the researcher *drew*: from an
output socket to an input socket on the canvas (section 8.1), i.e. the
direction data flows. The socket grammar admits exactly three shapes:

=================  ==========  =========  ===================================
entry `type`       `from` is   `to` is    stored graph edge
=================  ==========  =========  ===================================
`reads`            dataset     script     `script --reads--> dataset`
`writes`           script      dataset    `script --writes--> dataset`
`generates`        script      figure     `script --generates--> figure`
=================  ==========  =========  ===================================

`writes` and `generates` are stored as written. `reads` is stored
*reversed*, because that is how `rce.ingest.dataflow` already stores every
read it extracts (`script --reads--> dataset`: the script is the actor in
both of a script's verbs); a human `reads` and a machine `reads` of the
same pair are then the same (src, dst, type) under two extractors, and
every view that already understands machine reads understands the human
one. The 8.5 example (from = the Rmd, to = the pdf, type = generates) is
therefore stored exactly as written. The reverse spelling of a read (from
= the script, to = the dataset) is refused, not silently flipped: one
canonical spelling per edge is what lets duplicate detection be a plain
(from, to, type) comparison, and a file the writer and a human both edit
must not hold the same assertion in two shapes.

What is refused, and how
------------------------

Each `[[mapping]]` is validated on its own (section 8.5: "refuses that
entry with a line number and keeps the others"):

  - `from`/`to` must be non-empty project-relative strings. An absolute
    path is refused outright (the file is meant to travel with the
    project); otherwise the path is normalized lexically, a `..` that
    climbs above the root is refused, and the normalized path is then
    put through the same resolve-then-`relative_to` check every other
    path in this codebase takes (`rce.webapp.server._resolve_within_root`),
    so a symlink inside the project pointing outside it is refused too.
    The write path (`add_mapping`) runs the very same check -- it is
    never less confined than the read path.
  - each endpoint is typed by extension, with the canvas's deterministic
    classification (`rce.ingest.dataflow.node_type_for_path`: the
    dataflow extractor's own dataset/figure rule plus the script
    suffixes) -- never guessed from content;
  - `type` must be one of reads | writes | generates, and the (from type,
    to type, type) triple must be one of the three rows above.

`note` and `date` are commentary, not identity: a malformed one is
reported and dropped while the entry itself is kept (an unquoted TOML
date, `date = 2026-09-06`, is accepted as the date it obviously is). A
second entry with the same (from, to, type) is reported as a duplicate and
skipped; the first one asserts the edge. tomllib reports no line numbers
for parsed values, so a problem carries the line of its entry's
`[[mapping]]` header when those headers can be matched one-to-one with the
parsed entries, and always carries the entry's 1-based index.

The ingest is the human's hand
------------------------------

`ingest_mappings` upserts each valid entry's edge with `extractor =
"mapping"` and then moves it to `confirmed` through `db.set_edge_status`,
the human-only path -- the same way `rce attempts` writes
`human_fields.verdict` from the human's own table: the human wrote the
file; the ingest is only carrying their assertion into the mirror. That is
also why the file wins over a later `rce confirm --reject` of a mapping
edge: the next ingest sets it back to confirmed, because the way to
retract a hand-drawn link is to delete its entry. `db.HUMAN_EXTRACTORS`
keeps every machine extractor from writing (or bulk-deleting) a `mapping`
edge; `upsert_edge`'s own status rule keeps any machine re-ingest from
downgrading one.

Missing endpoint nodes are created -- the ghost -> real transition of
section 8.1 -- typed by extension, titled with the relative path, and
marked `attrs.ghost_origin = "mapping"`. That marker is how resync knows
which nodes it may take back: when an entry leaves the file its `mapping`
edge is deleted (only that edge, never another extractor's), and an
endpoint node is then deleted too only if it still carries the marker
(a machine extractor that has since upserted the same node rewrites its
attrs and so takes ownership), has no `human_fields`, and no edge of any
extractor references it any more.

Failing to read the source is not evidence of deletion (Section 4,
"Attempt orphans"). A missing `.rce/mappings.toml` means zero mappings to
assert, but resync is skipped -- edges already in the graph are left
untouched; a file that cannot be read, is not UTF-8, is not valid TOML, or
whose `mapping` key is not an array of tables raises `MappingsFileError`
before anything is touched. A file that *was* read and holds zero valid
entries is a real observation, and resyncs to zero. Per-entry refusals
are not file-level failures: the file was read, and an entry that no
longer validly asserts an edge no longer asserts it (the problem list says
why, so the researcher sees exactly which line to fix).

The writer
----------

`add_mapping` / `delete_mapping` edit the file with a small fixed-schema
TOML emitter (stdlib has `tomllib` but no writer; the schema above is all
it ever needs). They preserve every byte they do not own -- the header
comment, other entries, their order and comments -- append new entries at
the end, escape `"` and `\\` (and other control characters) so `tomllib`
round-trips every value, refuse the invisible line-separator set the map
editor already refuses (`rce.webapp.mapedit._reject_line_boundaries`), and
refuse a duplicate (same from, to, type). Every planned write is checked
before it lands: the new text is re-parsed with `tomllib` and must equal
the old entries plus (or minus) exactly the one asked for, so a file shape
the line-level editor misreads is refused rather than corrupted. The write
itself is the map editor's discipline, shared rather than copied:
`mapedit.write_backup_bytes` into `.rce/backups/`, then
`mapedit.atomic_replace_bytes` (durable tmp write, atomic replace,
directory fsync). Re-ingest and the generation bump belong to the caller
(the canvas endpoint, under the watcher's ingest lock), exactly as
`mapedit.apply_edit`'s caller owns the bump.
"""

from __future__ import annotations

import datetime as _dt
import logging
import posixpath
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from rce import db
from rce.ingest import dataflow as dataflow_ingest

logger = logging.getLogger(__name__)

MAPPINGS_RELATIVE_PATH = ".rce/mappings.toml"
EXTRACTOR = "mapping"

# entry `type` -> (from node type, to node type), the socket grammar of
# DESIGN.md section 8.1 in file-entry direction (module docstring table).
GRAMMAR: dict[str, tuple[str, str]] = {
    "reads": ("dataset", "script"),
    "writes": ("script", "dataset"),
    "generates": ("script", "figure"),
}
MAPPING_TYPES = tuple(GRAMMAR)

# The header a freshly created file starts with -- verbatim from 8.5.
FILE_HEADER = (
    "# 手工标注的映射。RCE 只记录它能读到的；它读不到的，由你在这里补上。\n"
    "# 此文件是唯一真相：图谱里的人工连线从它派生，删掉 graph.db 也不会丢。\n"
)

# A `[[mapping]]` header line (optionally followed by a comment), and any
# table/array-of-tables header at all (the writer's block boundaries).
_MAPPING_HEADER_RE = re.compile(r"^[ \t]*\[\[[ \t]*mapping[ \t]*\]\][ \t]*(#.*)?$")
_ANY_HEADER_RE = re.compile(r"^[ \t]*\[")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class MappingsFileError(Exception):
    """The mappings file exists but could not be observed as a whole
    (unreadable, not UTF-8, not valid TOML, wrong top-level shape). Raised
    before the graph is touched -- see module docstring."""


class MappingsWriteError(Exception):
    """A requested add/delete that cannot be satisfied against the file as
    it is. `code` is machine-readable (`duplicate`, `not_found`, a
    validation code, `file_unreadable`, `unsafe_edit`) so the canvas can
    show its own product-language sentence (section 8.8; e.g. `duplicate`
    -> 「这条映射已存在」); the message is the English explanation the CLI
    and logs use."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MappingProblem:
    """One refused (or partly dropped) entry: its 1-based `index` among
    the file's `[[mapping]]` entries, the line of its header when known,
    a machine-readable `code`, and an English `message`."""

    index: int
    line: int | None
    code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "line": self.line, "code": self.code, "message": self.message}

    def location(self) -> str:
        return f"line {self.line}" if self.line is not None else f"entry #{self.index}"


@dataclass(frozen=True)
class Mapping:
    """One valid entry, normalized. `from_path`/`to_path` are the
    lexically-normalized project-relative POSIX paths (the node ids'
    path halves); `src_id`/`dst_id` are the STORED edge's endpoints, i.e.
    already reversed for `reads` (module docstring)."""

    from_path: str
    to_path: str
    type: str
    from_type: str
    to_type: str
    note: str | None = None
    date: str | None = None
    index: int = 0
    line: int | None = None

    @property
    def from_id(self) -> str:
        return f"{self.from_type}:{self.from_path}"

    @property
    def to_id(self) -> str:
        return f"{self.to_type}:{self.to_path}"

    @property
    def src_id(self) -> str:
        return self.to_id if self.type == "reads" else self.from_id

    @property
    def dst_id(self) -> str:
        return self.from_id if self.type == "reads" else self.to_id

    @property
    def key(self) -> tuple[str, str, str]:
        """The entry's identity in the file: (from, to, type)."""
        return (self.from_path, self.to_path, self.type)


@dataclass(frozen=True)
class LoadResult:
    """What `load_mappings` observed: whether the file exists at all, the
    valid entries in file order, and every problem found."""

    file_present: bool
    mappings: list[Mapping] = field(default_factory=list)
    problems: list[MappingProblem] = field(default_factory=list)


@dataclass(frozen=True)
class IngestReport:
    """`ingest_mappings`' result: counts for the CLI summary line and the
    problem list callers show."""

    file_present: bool
    counts: dict[str, int]
    problems: list[MappingProblem]


class _EntryRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def mappings_path(project_root: str | Path) -> Path:
    return Path(project_root) / MAPPINGS_RELATIVE_PATH


def _confined_mappings_path(project_root: Path, *, for_write: bool) -> Path:
    """`mappings_path`, refused (`MappingsFileError`) unless it -- and, for a
    write, the `.rce/backups/` directory the writer puts its backup in --
    resolves inside the project root: the same resolve-then-`relative_to`
    check every entry's `from`/`to` takes (`_confine`).

    Why (adversarial review of the V4 work): the entries were confined but
    the file itself was not, so a project whose `.rce` is a symlink out of
    the project (git carries symlinks, and section 8.10 now invites
    committing `.rce/`) had the canvas's 确认标注 create -- or atomically
    replace -- a `mappings.toml` in some other directory, with backups
    beside it: the write path less confined than the read path, which 8.5
    says must never happen. A read through such a link is refused too, so
    the graph never ingests a file from outside the project.

    For a write, `mappings.toml` being a symlink at all (even to a file
    inside the project) is refused as well: the atomic replace would
    silently turn the link into a regular file, and the backup would hold
    the link target's bytes under this file's name. Hand-edit such a setup;
    the app will not guess which of the two files was meant."""
    path = mappings_path(project_root)
    root = project_root.resolve()
    candidates = [path]
    if for_write:
        from rce.webapp import mapedit  # local import, exactly as `_commit` does

        candidates.append(path.parent / mapedit.BACKUPS_DIRNAME)
    for candidate in candidates:
        try:
            candidate.resolve().relative_to(root)
        except (ValueError, OSError):
            raise MappingsFileError(
                f"{candidate.relative_to(project_root).as_posix()} resolves outside the project root "
                f"(to {candidate.resolve()}) -- a symlinked .rce? refusing to "
                f"{'write' if for_write else 'read'} it"
            ) from None
    if for_write and path.is_symlink():
        raise MappingsFileError(
            f"{MAPPINGS_RELATIVE_PATH} is a symlink -- the app will not replace a link with a "
            "regular file; edit it by hand"
        )
    return path


# -- validation ------------------------------------------------------------------


def _confine(project_root: Path, raw: object, what: str) -> str:
    """`raw` as a confined, lexically-normalized project-relative POSIX
    path, or `_EntryRefused` (module docstring: absolute refused; `..`
    above the root refused; then resolve-then-`relative_to`, which also
    catches a symlink escape)."""
    if not isinstance(raw, str):
        raise _EntryRefused("not_string", f"'{what}' must be a string, got {type(raw).__name__}")
    if not raw.strip():
        raise _EntryRefused("empty_path", f"'{what}' is empty")
    if "\x00" in raw:
        raise _EntryRefused("escapes_root", f"'{what}' {raw!r} contains a NUL character")
    if posixpath.isabs(raw) or raw.startswith("\\") or _WINDOWS_DRIVE_RE.match(raw):
        raise _EntryRefused(
            "absolute_path", f"'{what}' {raw!r} is absolute; mappings use project-relative paths"
        )
    normalized = posixpath.normpath(raw)
    if normalized == "." or normalized == ".." or normalized.startswith("../"):
        raise _EntryRefused("escapes_root", f"'{what}' {raw!r} points outside the project root")
    root = project_root.resolve()
    try:
        (root / normalized).resolve().relative_to(root)
    except (ValueError, OSError):
        raise _EntryRefused(
            "escapes_root", f"'{what}' {raw!r} resolves outside the project root (symlink?)"
        ) from None
    return normalized


def _classify(path: str, what: str) -> str:
    node_type = dataflow_ingest.node_type_for_path(path)
    if node_type is None:
        raise _EntryRefused(
            "unknown_extension",
            f"'{what}' {path!r}: extension is not a script, dataset or figure type",
        )
    return node_type


def _validate_identity(project_root: Path, entry: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """(from_path, to_path, type, from_type, to_type) for a valid entry, or
    `_EntryRefused`. Shared verbatim by ingest and writer."""
    for key in ("from", "to", "type"):
        if key not in entry:
            raise _EntryRefused("missing_field", f"missing required key '{key}'")
    edge_type = entry["type"]
    if not isinstance(edge_type, str):
        raise _EntryRefused("not_string", "'type' must be a string")
    if edge_type not in GRAMMAR:
        raise _EntryRefused(
            "bad_type", f"type {edge_type!r} is not one of {' | '.join(MAPPING_TYPES)}"
        )
    from_path = _confine(project_root, entry["from"], "from")
    to_path = _confine(project_root, entry["to"], "to")
    from_type = _classify(from_path, "from")
    to_type = _classify(to_path, "to")
    want_from, want_to = GRAMMAR[edge_type]
    if (from_type, to_type) != (want_from, want_to):
        hint = ""
        if (to_type, from_type) == (want_from, want_to):
            hint = " -- swap 'from' and 'to'"
        raise _EntryRefused(
            "grammar",
            f"'{edge_type}' goes from a {want_from} to a {want_to}; "
            f"this entry goes from a {from_type} to a {to_type}{hint}",
        )
    return from_path, to_path, edge_type, from_type, to_type


def _header_lines(text: str, entry_count: int) -> list[int | None]:
    """1-based header line for each entry, when the `[[mapping]]` headers
    in the text match the parsed entries one-to-one; otherwise all None."""
    lines = [i + 1 for i, line in enumerate(text.splitlines()) if _MAPPING_HEADER_RE.match(line)]
    if len(lines) == entry_count:
        return list(lines)
    return [None] * entry_count


def _parse_text(text: str) -> list[Any]:
    """The raw `mapping` array from `text`; `MappingsFileError` for
    anything that is not an observable file."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MappingsFileError(f"{MAPPINGS_RELATIVE_PATH} is not valid TOML: {exc}") from exc
    raw = data.get("mapping", [])
    if not isinstance(raw, list):
        raise MappingsFileError(
            f"{MAPPINGS_RELATIVE_PATH}: 'mapping' must be an array of tables ([[mapping]])"
        )
    return raw


def _read_text(path: Path) -> str | None:
    """The file's text, None when it does not exist."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MappingsFileError(f"cannot read {MAPPINGS_RELATIVE_PATH}: {exc}") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MappingsFileError(f"{MAPPINGS_RELATIVE_PATH} is not valid UTF-8: {exc}") from exc


def _validate_entries(project_root: Path, raw_entries: list[Any], text: str) -> LoadResult:
    lines = _header_lines(text, len(raw_entries))
    mappings: list[Mapping] = []
    problems: list[MappingProblem] = []
    seen: set[tuple[str, str, str]] = set()
    for i, entry in enumerate(raw_entries):
        index, line = i + 1, lines[i]
        if not isinstance(entry, dict):
            problems.append(MappingProblem(index, line, "not_a_table", "entry is not a [[mapping]] table"))
            continue
        try:
            from_path, to_path, edge_type, from_type, to_type = _validate_identity(project_root, entry)
        except _EntryRefused as exc:
            problems.append(MappingProblem(index, line, exc.code, str(exc)))
            continue
        if (from_path, to_path, edge_type) in seen:
            problems.append(MappingProblem(
                index, line, "duplicate",
                f"same from/to/type as an earlier entry ({from_path} -> {to_path}, {edge_type}); skipped",
            ))
            continue
        seen.add((from_path, to_path, edge_type))
        note = entry.get("note")
        if note is not None and not isinstance(note, str):
            problems.append(MappingProblem(index, line, "bad_note", "'note' must be a string; ignored"))
            note = None
        date = entry.get("date")
        if isinstance(date, _dt.date) and not isinstance(date, _dt.datetime):
            date = date.isoformat()
        elif date is not None and not isinstance(date, str):
            problems.append(MappingProblem(index, line, "bad_date", "'date' must be a date string; ignored"))
            date = None
        mappings.append(Mapping(
            from_path=from_path, to_path=to_path, type=edge_type,
            from_type=from_type, to_type=to_type, note=note, date=date, index=index, line=line,
        ))
    return LoadResult(file_present=True, mappings=mappings, problems=problems)


def load_mappings(project_root: str | Path) -> LoadResult:
    """Parse and validate `.rce/mappings.toml`. A missing file is zero
    mappings with `file_present=False` (not an error); a file that cannot
    be observed raises `MappingsFileError`; bad entries are refused
    individually into `problems` while the good ones are kept."""
    project_root = Path(project_root)
    text = _read_text(_confined_mappings_path(project_root, for_write=False))
    if text is None:
        return LoadResult(file_present=False)
    return _validate_entries(project_root, _parse_text(text), text)


# -- ingest ----------------------------------------------------------------------


def _mapping_edges(conn: Connection) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for edge_type in MAPPING_TYPES:
        edges.extend(e for e in db.query_edges(conn, type=edge_type) if e["extractor"] == EXTRACTOR)
    return edges


def _maybe_remove_ghost_node(conn: Connection, node_id: str) -> bool:
    """Delete `node_id` only if this ingest created it and nothing else
    has claimed it since (module docstring)."""
    node = db.get_node(conn, node_id)
    if node is None or node["attrs"].get("ghost_origin") != EXTRACTOR or node["human_fields"]:
        return False
    if db.query_edges(conn, src=node_id) or db.query_edges(conn, dst=node_id):
        return False
    db.delete_node(conn, node_id)
    return True


def ingest_mappings(conn: Connection, project_root: str | Path) -> IngestReport:
    """Mirror `.rce/mappings.toml` into the graph (module docstring): create
    missing endpoint nodes, upsert each valid entry's `mapping` edge and
    confirm it through the human-only path, then -- only if the file was
    actually read -- remove `mapping` edges whose entry is gone, and any
    endpoint node this ingest created that nothing references any more.
    Raises `MappingsFileError` (graph untouched) when the file exists but
    cannot be observed. Idempotent."""
    project_root = Path(project_root)
    result = load_mappings(project_root)
    counts = {
        "mappings": len(result.mappings), "refused": 0, "nodes_created": 0,
        "edges_confirmed": 0, "edges_removed": 0, "nodes_removed": 0,
    }
    counts["refused"] = sum(1 for p in result.problems if p.code not in ("bad_note", "bad_date"))
    # info, not warning: the problems are returned, and every caller shows
    # them in its own channel (the CLI prints them; the watcher logs them).
    for problem in result.problems:
        logger.info("%s %s: %s", MAPPINGS_RELATIVE_PATH, problem.location(), problem.message)

    asserted: set[tuple[str, str, str]] = set()
    for m in result.mappings:
        for node_id, node_type, rel in ((m.from_id, m.from_type, m.from_path), (m.to_id, m.to_type, m.to_path)):
            if db.get_node(conn, node_id) is None:
                db.upsert_node(conn, node_id, node_type, title=rel, attrs={"ghost_origin": EXTRACTOR})
                counts["nodes_created"] += 1
        details: dict[str, Any] = {"line": m.line, "entry": m.index}
        if m.note is not None:
            details["note"] = m.note
        if m.date is not None:
            details["date"] = m.date
        db.upsert_edge(
            conn, m.src_id, m.dst_id, m.type, EXTRACTOR,
            evidence={"file": MAPPINGS_RELATIVE_PATH, "source": "human"},
            confidence=1.0, status="auto",
            edge_attrs={"mapping": details}, human_source=True,
        )
        current = [
            e for e in db.query_edges(conn, src=m.src_id, dst=m.dst_id, type=m.type)
            if e["extractor"] == EXTRACTOR
        ]
        if current and current[0]["status"] != "confirmed":
            db.set_edge_status(conn, m.src_id, m.dst_id, m.type, EXTRACTOR, "confirmed")
            counts["edges_confirmed"] += 1
        asserted.add((m.src_id, m.dst_id, m.type))

    if not result.file_present:
        # Not having observed the file is not evidence its entries are gone.
        return IngestReport(file_present=False, counts=counts, problems=list(result.problems))

    for edge in _mapping_edges(conn):
        if (edge["src"], edge["dst"], edge["type"]) in asserted:
            continue
        counts["edges_removed"] += db.delete_edge(conn, edge["src"], edge["dst"], edge["type"], EXTRACTOR)
        logger.info(
            "mapping %s --%s--> %s no longer in %s -- removed",
            edge["src"], edge["type"], edge["dst"], MAPPINGS_RELATIVE_PATH,
        )
        for node_id in (edge["src"], edge["dst"]):
            if _maybe_remove_ghost_node(conn, node_id):
                counts["nodes_removed"] += 1
    return IngestReport(file_present=True, counts=counts, problems=list(result.problems))


# -- writer ----------------------------------------------------------------------


def _toml_string(value: str) -> str:
    """A TOML basic string `tomllib` reads back as exactly `value`."""
    out = ['"']
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _emit_entry(entry: dict[str, str], newline: str) -> str:
    lines = ["[[mapping]]"]
    lines.append(f"from = {_toml_string(entry['from'])}")
    lines.append(f"to   = {_toml_string(entry['to'])}")
    lines.append(f"type = {_toml_string(entry['type'])}")
    if entry.get("note") is not None:
        lines.append(f"note = {_toml_string(entry['note'])}")
    if entry.get("date") is not None:
        lines.append(f"date = {_toml_string(entry['date'])}")
    return newline.join(lines) + newline


def _reject_line_breaks(value: str, what: str) -> None:
    # Imported here: rce.webapp.mapedit imports rce.ingest.attempts, and an
    # ingest module importing the webapp package at module load would make
    # every `rce.ingest` import drag the web layer in.
    from rce.webapp import mapedit

    try:
        mapedit._reject_line_boundaries(value, what)
    except mapedit.MapEditError as exc:
        raise MappingsWriteError("line_break", str(exc)) from exc


def _validate_for_write(project_root: Path, entry: dict[str, Any]) -> tuple[str, str, str]:
    for key in ("from", "to", "type", "note", "date"):
        value = entry.get(key)
        if isinstance(value, str):
            _reject_line_breaks(value, f"mapping '{key}'")
    for key in ("note", "date"):
        if entry.get(key) is not None and not isinstance(entry[key], str):
            raise MappingsWriteError(f"bad_{key}", f"'{key}' must be a string")
    try:
        from_path, to_path, edge_type, _, _ = _validate_identity(project_root, entry)
    except _EntryRefused as exc:
        raise MappingsWriteError(exc.code, str(exc)) from None
    return from_path, to_path, edge_type


def _load_for_write(project_root: Path) -> tuple[str | None, list[Any], LoadResult]:
    try:
        path = _confined_mappings_path(project_root, for_write=True)
    except MappingsFileError as exc:
        raise MappingsWriteError("escapes_root", str(exc)) from exc
    try:
        text = _read_text(path)
        if text is None:
            return None, [], LoadResult(file_present=False)
        raw = _parse_text(text)
    except MappingsFileError as exc:
        raise MappingsWriteError(
            "file_unreadable", f"{exc} -- fix the file by hand before editing it from the app"
        ) from exc
    return text, raw, _validate_entries(project_root, raw, text)


def _newline_of(text: str | None) -> str:
    return "\r\n" if text and "\r\n" in text else "\n"


def _commit(project_root: Path, old_text: str | None, new_text: str) -> str | None:
    """Back up (if there was a file) and atomically replace; returns the
    backup's project-relative path or None for a newly created file."""
    from rce.webapp import mapedit

    from rce.records import files as records_files

    path = mappings_path(project_root)
    try:
        records_files.ensure_dir_within(project_root, path.parent)  # never re-creates a moved project
    except records_files.RecordFileError as exc:
        raise MappingsWriteError("file_unreadable", str(exc)) from exc
    backup = None
    if old_text is not None:
        backup = mapedit.write_backup_bytes(project_root, path, old_text.encode("utf-8"), ".toml")
    mapedit.atomic_replace_bytes(path, new_text.encode("utf-8"))
    return backup


def _check_round_trip(new_text: str, expected: list[Any]) -> None:
    try:
        parsed = tomllib.loads(new_text).get("mapping", [])
    except tomllib.TOMLDecodeError as exc:
        raise MappingsWriteError("unsafe_edit", f"planned edit would not parse as TOML ({exc}); refusing") from exc
    if parsed != expected:
        raise MappingsWriteError(
            "unsafe_edit",
            f"{MAPPINGS_RELATIVE_PATH} has a shape this writer cannot edit faithfully; "
            "refusing rather than risk changing other entries -- edit it by hand",
        )


def add_mapping(
    project_root: str | Path,
    from_path: str,
    to_path: str,
    type: str,
    note: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """`_add_mapping` under the project lock with the identity re-checked
    (DESIGN.md 9.4, 9.7): a human record, so a pre-V5 project refuses it
    (`NeedsMigrationError`) and two processes take turns."""
    from rce.records import situation  # noqa: PLC0415 -- records imports nothing from ingest

    with situation.write_guard(project_root, human=True):
        return _add_mapping(project_root, from_path, to_path, type, note=note, date=date)


def _add_mapping(
    project_root: str | Path,
    from_path: str,
    to_path: str,
    type: str,
    note: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Append one entry to `.rce/mappings.toml` (creating the file with the
    8.5 header if needed). `date` defaults to today's LOCAL date (8.5:
    "本地日期，写入时填"). Paths are stored in their normalized form.
    Raises `MappingsWriteError` (file untouched) on any refusal. Returns
    `{file, backup, entry}`. Does not re-ingest -- the caller does, under
    the watcher's ingest lock."""
    project_root = Path(project_root)
    if date is None:
        date = _dt.date.today().isoformat()
    candidate = {"from": from_path, "to": to_path, "type": type, "note": note, "date": date}
    norm_from, norm_to, edge_type = _validate_for_write(project_root, candidate)
    old_text, raw, loaded = _load_for_write(project_root)
    if any(m.key == (norm_from, norm_to, edge_type) for m in loaded.mappings):
        raise MappingsWriteError(
            "duplicate", f"mapping {norm_from} -> {norm_to} ({edge_type}) already exists"
        )
    entry = {"from": norm_from, "to": norm_to, "type": edge_type}
    if note is not None:
        entry["note"] = note
    entry["date"] = date
    newline = _newline_of(old_text)
    if old_text is None:
        new_text = FILE_HEADER + _emit_entry(entry, newline)
    else:
        base = old_text
        if base and not base.endswith(("\n", "\r")):
            base += newline
        new_text = base + (newline if base.strip() else "") + _emit_entry(entry, newline)
    _check_round_trip(new_text, list(raw) + [entry])
    backup = _commit(project_root, old_text, new_text)
    return {"file": MAPPINGS_RELATIVE_PATH, "backup": backup, "entry": entry}


def _blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(start, end) line spans of every `[[mapping]]` block: header through
    its last line that is neither blank nor a comment, before the next
    table header. Trailing comments stay outside the span (they may
    annotate what follows) -- a delete never removes human text it cannot
    attribute."""
    headers = [i for i, line in enumerate(lines) if _ANY_HEADER_RE.match(line)]
    spans = []
    for n, start in enumerate(headers):
        if not _MAPPING_HEADER_RE.match(lines[start].rstrip("\r\n")):
            continue
        stop = headers[n + 1] if n + 1 < len(headers) else len(lines)
        end = start + 1
        for i in range(start + 1, stop):
            stripped = lines[i].strip()
            if stripped and not stripped.startswith("#"):
                end = i + 1
        spans.append((start, end))
    return spans


def delete_mapping(project_root: str | Path, from_path: str, to_path: str, type: str) -> dict[str, Any]:
    """`_delete_mapping` under the project write guard (see `add_mapping`)."""
    from rce.records import situation  # noqa: PLC0415

    with situation.write_guard(project_root, human=True):
        return _delete_mapping(project_root, from_path, to_path, type)


def _delete_mapping(project_root: str | Path, from_path: str, to_path: str, type: str) -> dict[str, Any]:
    """Remove every entry asserting (from, to, type) -- compared in
    normalized form -- together with the blank lines just above it.
    Everything else is kept byte-for-byte. Raises `MappingsWriteError`
    (`not_found` when no such entry exists). Returns `{file, backup,
    removed}`. Does not re-ingest (see `add_mapping`)."""
    project_root = Path(project_root)
    old_text, raw, _ = _load_for_write(project_root)
    if old_text is None:
        raise MappingsWriteError("not_found", f"no {MAPPINGS_RELATIVE_PATH} -- nothing to delete")
    try:
        target = _validate_identity(project_root, {"from": from_path, "to": to_path, "type": type})[:3]
    except _EntryRefused as exc:
        raise MappingsWriteError(exc.code, str(exc)) from None

    def matches(entry: Any) -> bool:
        if not isinstance(entry, dict):
            return False
        try:
            return _validate_identity(project_root, entry)[:3] == target
        except _EntryRefused:
            return False

    lines = old_text.splitlines(keepends=True)
    drop: set[int] = set()
    for start, end in _blocks(lines):
        try:
            block_entries = tomllib.loads("".join(lines[start:end])).get("mapping", [])
        except tomllib.TOMLDecodeError:
            continue
        if len(block_entries) == 1 and matches(block_entries[0]):
            first = start
            while first > 0 and not lines[first - 1].strip():
                first -= 1
            drop.update(range(first, end))
    if not drop:
        raise MappingsWriteError(
            "not_found", f"no mapping {target[0]} -> {target[1]} ({target[2]}) in {MAPPINGS_RELATIVE_PATH}"
        )
    new_text = "".join(line for i, line in enumerate(lines) if i not in drop)
    remaining = [e for e in raw if not matches(e)]
    _check_round_trip(new_text, remaining)
    backup = _commit(project_root, old_text, new_text)
    return {"file": MAPPINGS_RELATIVE_PATH, "backup": backup, "removed": len(raw) - len(remaining)}
