"""Write path for the attempt timeline: edit the researcher's OWN Markdown
map file, never a second store (task V3 phase 3).

DESIGN.md's "resync from source" doctrine (section 4) makes the source
Markdown table the single authority for an attempt's existence and its
verdict/result -- the graph is a mirror refreshed by re-ingest. A UI that
"adds an attempt" therefore has exactly one legitimate move: append or edit
a row *in that Markdown file*, then re-run the same ingest `rce attempts`
runs, and let the mirror follow. Writing the graph directly and skipping the
file would create the two-authorities divergence the ingest module's own
docstring explicitly warns future write paths about ("whoever adds one must
resolve that conflict explicitly") -- this module resolves it the only way
that keeps one authority: the in-UI edit *is* a source-file edit.

Everything here is driven by `.rce/attempts.toml` exactly as ingest is
(`rce.ingest.attempts.load_config`): which file, which heading, which header
text maps to which field. Nothing is guessed (DESIGN.md section 0) -- no
config, no table, or an ambiguous row is a clean refusal, never a
best-effort write into somebody's hand-maintained research log.

Parsing is reused, not re-implemented: locating the table, splitting a row
into cells, and cleaning a cell all go through `rce.ingest.attempts`' own
`_find_table` / `_split_row` / `_clean_cell` / `parse_attempts_table` --
private names imported deliberately, because duplicating those rules here
would let the writer and the parser drift apart, which for a *writer* is
worse than the usual duplication cost: a row this module writes that the
parser then reads differently silently corrupts the graph's mirror of the
user's own file. The one thing built here that ingest has no need for is
the inverse of `_split_row`'s unescape: a literal `|` inside a cell is
written as `\\|`, exactly the sequence `_split_row` turns back into `|`.

Two-step contract (the API/UI mirror it): `preview_edit` is pure -- it
plans the edit against the file's current bytes and returns a unified diff
plus the old/new row, writing nothing; `apply_edit` re-plans from a fresh
read (the file may have moved since the preview -- re-validating against
what is actually on disk is the point, not an inefficiency), then:

  1. backs the file up verbatim to `.rce/backups/<name>.<UTC stamp>Z.md`,
     pruning to the newest `BACKUP_KEEP` backups of that same file;
  2. writes atomically -- full new content to a tmp file in the same
     directory, then `os.replace` -- so a crash mid-write can never leave
     the map half-written (the original survives any failure before the
     rename, and the rename itself is atomic on POSIX);
  3. re-runs the attempts ingest exactly as `rce.cli.cmd_attempts` does
     (`load_config` + `ingest_attempts_repo`), under whatever lock the
     caller passes -- the server passes the watcher's own ingest lock
     (task V3 phase 2), so a UI write and a watcher poll never ingest
     concurrently. The lock is held across plan+write+ingest, which also
     serializes two concurrent UI writes: the second re-plans after the
     first landed and re-validates (e.g. a now-duplicate append fails
     cleanly instead of writing a colliding row).

An ingest failure after a successful write is contained, not fatal
(mirroring the watcher's own containment): the file edit the user asked
for *happened* and is backed up, so the error is returned in the result
(`ingest_error`) for the UI to surface -- rolling the file back because
the graph hiccuped would put the mirror above the source, exactly
backwards. The generation bump lives with the caller (the server owns the
watcher); this module knows only files and the graph.

Fidelity rules for a file this module does not own: an update rewrites
ONLY the cells it was asked to change -- every other cell of that row, and
every other line of the file, is carried over byte-for-byte (raw text,
decoration and escapes included), which is why prefilled-but-unedited form
fields must not be round-tripped through the parser's *cleaned* values by
callers (the web UI only submits fields the user actually changed). The
file's own newline convention is detected and kept; a file whose line
endings cannot be reproduced exactly (mixed conventions, exotic Unicode
line separators) is refused rather than silently normalized, and a file
that is not valid UTF-8 is refused rather than written back with
replacement characters -- both are "never guess" refusals, not features
deferred.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rce import db
from rce.ingest import attempts as attempts_ingest

logger = logging.getLogger(__name__)

# Same constants as rce.cli / rce.webapp.server / rce.webapp.watcher --
# each subsystem owns its copy (existing convention in this codebase).
RCE_DIRNAME = ".rce"
DB_FILENAME = "graph.db"

BACKUPS_DIRNAME = "backups"
BACKUP_KEEP = 20

# Every logical column except `id`: the fields an edit may set. The id is
# the row's identity (the node id is derived from it, DESIGN.md section 4)
# and is passed separately as `number`, never as an editable cell.
EDITABLE_FIELDS = tuple(k for k in attempts_ingest._REQUIRED_COLUMNS if k != "id")

_RAW_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")  # same split rule as _split_row


class MapEditError(Exception):
    """An edit request that cannot be satisfied against the file as it
    currently is (duplicate/unknown number, invalid field content, a file
    this module refuses to rewrite faithfully). The message is shown to the
    user by the web UI, wrapped in its own Chinese product-language framing
    -- same division as the refresh chip's error handling."""


@dataclass(frozen=True)
class EditPlan:
    """One planned edit, fully computed and not yet written: the file's
    current text, the text it would become, and the single row that
    differs -- everything both `preview_edit` (which stops here) and
    `apply_edit` (which writes it) need."""

    config: attempts_ingest.AttemptsConfig
    source_path: Path
    original_raw: bytes
    original_text: str
    new_text: str
    old_row: str | None  # raw line being replaced; None for an append
    new_row: str
    newline: str


# -- Field validation ---------------------------------------------------------


def _validate_number(number: object) -> str:
    if not isinstance(number, str) or not number.strip():
        raise MapEditError("attempt number must be a non-empty string")
    number = number.strip()
    if "\n" in number or "\r" in number:
        raise MapEditError("attempt number must not contain newlines")
    # A number whose parsed (cleaned) form differs from what was typed
    # (e.g. "**5**") would collide with "5" only after ingest cleaned it --
    # too late for the duplicate check below to have seen it. Plain text
    # only, so what is written is exactly what the parser reads back.
    if attempts_ingest._clean_cell(number) != number:
        raise MapEditError(
            f"attempt number {number!r} must be plain text (no markdown decoration)"
        )
    return number


def _validate_fields(fields: object) -> dict[str, str]:
    """Trimmed plain-string cells only. Newlines are rejected because a
    Markdown table cell cannot hold a raw newline -- the parser's own
    convention for an intentional line break inside a cell is a literal
    `<br>` (`rce.ingest.attempts._clean_cell` turns it back into `\\n`),
    and writing that marker is the caller's explicit choice, never an
    automatic translation done here."""
    if not isinstance(fields, dict):
        raise MapEditError("fields must be an object of column-key -> string")
    cleaned: dict[str, str] = {}
    for key, value in fields.items():
        if key not in EDITABLE_FIELDS:
            raise MapEditError(
                f"unknown field {key!r} -- editable fields are {list(EDITABLE_FIELDS)} "
                f"(the id column is addressed via 'number', never as a field)"
            )
        if not isinstance(value, str):
            raise MapEditError(f"field {key!r} must be a string, got {type(value).__name__}")
        value = value.strip()
        if "\n" in value or "\r" in value:
            raise MapEditError(
                f"field {key!r} must not contain raw newlines -- a markdown table cell "
                f"cannot hold them (use a literal <br> if you mean a line break)"
            )
        cleaned[key] = value
    return cleaned


def _escape_cell(value: str) -> str:
    """The exact inverse of `_split_row`'s unescape: a literal `|` becomes
    `\\|`, so the cell survives the round trip through the real parser."""
    return value.replace("|", "\\|")


# -- Faithful file text handling ---------------------------------------------


def _decode_source(raw: bytes, source_path: Path) -> tuple[str, list[str], str, str]:
    """`(text, lines, newline, trailing)` for the source file, refusing any
    content this module cannot reproduce byte-for-byte (module docstring):
    `lines` are `str.splitlines()` -- the SAME split `parse_attempts_table`
    uses, so a parsed row's 1-based `line` indexes `lines` directly -- and
    `newline.join(lines) + trailing` is verified to equal `text` exactly
    before any edit is planned."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MapEditError(
            f"{source_path} is not valid UTF-8 -- refusing to rewrite it "
            f"(a write would substitute replacement characters)"
        ) from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    trailing = newline if text.endswith(("\r\n", "\n", "\r")) else ""
    if newline.join(lines) + trailing != text:
        raise MapEditError(
            f"{source_path} mixes line-ending conventions (or uses unusual line "
            f"separators) -- refusing to rewrite it, since an edit could not keep "
            f"the untouched lines byte-for-byte identical"
        )
    return text, lines, newline, trailing


def _split_raw_row(line: str) -> tuple[str, list[str], str]:
    """`(prefix, raw_cells, suffix)` for one physical table row, preserving
    every byte: `prefix`/`suffix` are the leading/trailing whitespace plus
    the outer pipes exactly as written, and `raw_cells` keep their inner
    padding, decoration and `\\|` escapes untouched. Splitting uses the
    same `(?<!\\\\)\\|` rule as `_split_row`; the reassembly is verified to
    reproduce the original line exactly, and a line that does not (an
    escape shape the split rule and this reassembly disagree on) is
    refused rather than rewritten lossily."""
    stripped_lead = line.lstrip()
    prefix_ws = line[: len(line) - len(stripped_lead)]
    core = line.strip()
    suffix_ws = stripped_lead[len(core):]
    has_lead = core.startswith("|")
    has_trail = core.endswith("|") and len(core) > 1
    inner = core[1:] if has_lead else core
    inner = inner[:-1] if has_trail else inner
    cells = _RAW_CELL_SPLIT_RE.split(inner)
    prefix = prefix_ws + ("|" if has_lead else "")
    suffix = ("|" if has_trail else "") + suffix_ws
    if prefix + "|".join(cells) + suffix != line:
        raise MapEditError(
            f"cannot faithfully rewrite table row {line!r} -- refusing rather than "
            f"altering cells that were not part of the edit"
        )
    return prefix, cells, suffix


# -- Edit planning -------------------------------------------------------------


def _column_index(header_line: str, columns: dict[str, str]) -> tuple[list[str], dict[str, int]]:
    """The configured logical key -> header cell position mapping, built
    with the same `_split_row` + `_clean_cell` the parser applies to the
    header -- `parse_attempts_table` validated the names already (it runs
    first in `_plan_edit`), so a missing one here is impossible rather than
    silently re-checked differently."""
    header_cells = [attempts_ingest._clean_cell(c) for c in attempts_ingest._split_row(header_line)]
    col_index = {key: header_cells.index(columns[key]) for key in attempts_ingest._REQUIRED_COLUMNS}
    return header_cells, col_index


def _build_row(
    header_line: str, header_cells: list[str], col_index: dict[str, int],
    number: str, fields: dict[str, str],
) -> str:
    """A brand-new row in the EXISTING header's column order: configured
    columns get their field values (missing ones stay empty), columns the
    config does not map stay empty -- they belong to the researcher, not to
    this tool. Outer-pipe style mirrors the header row's own."""
    index_to_value = {col_index["id"]: number}
    for key in EDITABLE_FIELDS:
        index_to_value[col_index[key]] = fields.get(key, "")
    cells = [f" {_escape_cell(index_to_value.get(i, ''))} " for i in range(len(header_cells))]
    core = header_line.strip()
    lead = "|" if core.startswith("|") else ""
    trail = "|" if core.endswith("|") else ""
    return lead + "|".join(cells) + trail


def _table_end(lines: list[str], data_idx: int) -> int:
    """First line index after the table's last physical row -- the same
    "a row is a line starting with |" rule `parse_attempts_table`'s row
    loop stops on, applied physically so an append lands after even a
    ragged row the parser skipped."""
    end = data_idx
    while end < len(lines) and lines[end].strip().startswith("|"):
        end += 1
    return end


def _plan_edit(project_root: Path, op: str, number: str, fields: dict[str, str]) -> EditPlan:
    """Validate + compute one edit against the file's current bytes; pure.
    Raises `MapEditError` for anything about the request, and lets
    `AttemptsConfigError` (missing/broken config, unlocatable table --
    `parse_attempts_table` runs first and owns those rules) propagate
    untouched, exactly as ingest raises them."""
    if op not in ("append", "update"):
        raise MapEditError(f"unknown op {op!r} -- expected 'append' or 'update'")
    number = _validate_number(number)
    fields = _validate_fields(fields)

    config = attempts_ingest.load_config(project_root)
    source_path = project_root / config.file
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        raise MapEditError(f"cannot read {source_path}: {exc}") from exc
    text, lines, newline, trailing = _decode_source(raw, source_path)

    # The real parser first: it owns heading/table location and column-name
    # validation, and its parsed rows carry the exact 1-based source line
    # each row lives on -- the anchor every edit below is planned against.
    rows = attempts_ingest.parse_attempts_table(text, config.heading, config.columns)
    located = attempts_ingest._find_table(lines, config.heading)
    if located is None:  # unreachable: parse_attempts_table above already located it
        raise attempts_ingest.AttemptsTableNotFoundError(
            f"heading {config.heading!r} or a table under it not found in {config.file}"
        )
    header_idx, data_idx = located
    header_cells, col_index = _column_index(lines[header_idx], config.columns)

    matching = [r for r in rows if r.number == number]
    if op == "append":
        if matching:
            raise MapEditError(
                f"attempt number {number!r} already exists in the table (line "
                f"{matching[0].line}) -- appending it again would create the duplicate-id "
                f"collision ingest refuses to merge (DESIGN.md section 4)"
            )
        new_row = _build_row(lines[header_idx], header_cells, col_index, number, fields)
        new_lines = list(lines)
        new_lines.insert(_table_end(lines, data_idx), new_row)
        old_row = None
    else:
        if not matching:
            raise MapEditError(f"no row with attempt number {number!r} in the table -- nothing to update")
        if len(matching) > 1:
            raise MapEditError(
                f"attempt number {number!r} appears on {len(matching)} rows -- refusing to "
                f"pick one (fix the duplicate in the source file first)"
            )
        if not fields:
            raise MapEditError("update carries no fields -- nothing to change")
        line_idx = matching[0].line - 1
        prefix, raw_cells, suffix = _split_raw_row(lines[line_idx])
        if len(raw_cells) != len(header_cells):  # parse guarantees this; keep the invariant loud
            raise MapEditError(
                f"row {number!r} has {len(raw_cells)} cell(s) but the header has "
                f"{len(header_cells)} -- refusing to edit a row the parser and this "
                f"writer disagree about"
            )
        for key, value in fields.items():
            raw_cells[col_index[key]] = f" {_escape_cell(value)} "
        new_row = prefix + "|".join(raw_cells) + suffix
        new_lines = list(lines)
        new_lines[line_idx] = new_row
        old_row = lines[line_idx]

    new_text = newline.join(new_lines) + trailing
    return EditPlan(
        config=config, source_path=source_path, original_raw=raw, original_text=text,
        new_text=new_text, old_row=old_row, new_row=new_row, newline=newline,
    )


# -- Public API: preview (pure) + apply (backup, atomic write, re-ingest) -----


def preview_edit(project_root: str | Path, op: str, number: str, fields: dict[str, str]) -> dict[str, object]:
    """Plan the edit and return `{file, diff, old_row, new_row}` -- a
    unified diff of the whole file plus the one row that changes -- with
    NO write of any kind. The UI shows this and asks for an explicit
    confirm; only the confirm calls `apply_edit`."""
    plan = _plan_edit(Path(project_root), op, number, fields)
    diff = "\n".join(difflib.unified_diff(
        plan.original_text.splitlines(), plan.new_text.splitlines(),
        fromfile=plan.config.file, tofile=plan.config.file, lineterm="",
    ))
    return {"file": plan.config.file, "diff": diff, "old_row": plan.old_row, "new_row": plan.new_row}


def _write_backup(project_root: Path, plan: EditPlan) -> str:
    """The original bytes, verbatim, to `.rce/backups/<name>.<stamp>Z.md`;
    then prune that file's own backups to the newest `BACKUP_KEEP`. The
    stamp is zero-padded UTC down to microseconds, so lexicographic name
    order IS chronological order and pruning needs no mtime reads."""
    backups_dir = project_root / RCE_DIRNAME / BACKUPS_DIRNAME
    backups_dir.mkdir(parents=True, exist_ok=True)
    source_name = plan.source_path.name
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    backup_path = backups_dir / f"{source_name}.{stamp}Z.md"
    counter = 0
    while backup_path.exists():  # same microsecond twice: disambiguate, never overwrite
        counter += 1
        backup_path = backups_dir / f"{source_name}.{stamp}Z.{counter}.md"
    backup_path.write_bytes(plan.original_raw)

    siblings = sorted(
        p for p in backups_dir.iterdir()
        if p.is_file() and p.name.startswith(source_name + ".") and p.name.endswith(".md")
    )
    for old in siblings[:-BACKUP_KEEP]:
        old.unlink()
        logger.info("pruned old map backup %s (keeping newest %d)", old, BACKUP_KEEP)
    return str(backup_path.relative_to(project_root))


def _atomic_write(plan: EditPlan) -> None:
    """Full new content to a tmp file in the same directory (same
    filesystem, so the rename is atomic), then `os.replace` over the
    original -- a failure at any point before the replace leaves the
    original untouched, and the tmp file is cleaned up on the way out."""
    tmp_path = plan.source_path.parent / f".{plan.source_path.name}.rce-edit-tmp"
    try:
        tmp_path.write_bytes(plan.new_text.encode("utf-8"))
        os.replace(tmp_path, plan.source_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _reingest_attempts(project_root: Path) -> None:
    """Exactly `rce.cli.cmd_attempts`'s own calls (reuse, never
    re-implement -- same rule as the watcher's `_reingest`), with the same
    missing-db refusal: never let `db.connect` conjure a fresh graph.db
    inside a project whose database vanished mid-serve."""
    db_path = project_root / RCE_DIRNAME / DB_FILENAME
    if not db_path.exists():
        raise RuntimeError(
            f"no RCE project at {project_root} (missing {RCE_DIRNAME}/{DB_FILENAME}); "
            "the file was written and backed up, but the graph could not be re-ingested"
        )
    conn = db.connect(db_path)
    try:
        config = attempts_ingest.load_config(project_root)
        counts = attempts_ingest.ingest_attempts_repo(conn, project_root, config)
        logger.info("mapedit re-ingested attempts for %s: %s", project_root, counts)
    finally:
        conn.close()


def apply_edit(
    project_root: str | Path,
    op: str,
    number: str,
    fields: dict[str, str],
    ingest_lock: threading.Lock | None = None,
) -> dict[str, object]:
    """Perform the edit: re-plan from a fresh read, back up, write
    atomically, re-ingest -- all under `ingest_lock` when the caller passes
    one (the server passes the watcher's, so a UI write never interleaves
    with a watcher poll's ingest; see module docstring for why the lock
    spans planning too). Returns `{file, backup, new_row, ingest_error}`;
    `ingest_error` is a contained post-write ingest failure (str) or None
    -- the file edit itself either fully happened or an exception was
    raised with the original intact."""
    project_root = Path(project_root)
    lock = ingest_lock if ingest_lock is not None else threading.Lock()
    with lock:
        plan = _plan_edit(project_root, op, number, fields)
        backup = _write_backup(project_root, plan)
        _atomic_write(plan)
        ingest_error: str | None = None
        try:
            _reingest_attempts(project_root)
        except Exception as exc:  # noqa: BLE001 -- containment, same as the watcher's
            logger.exception("post-write re-ingest of %s failed -- file written and backed up", project_root)
            ingest_error = str(exc)
    return {"file": plan.config.file, "backup": backup, "new_row": plan.new_row, "ingest_error": ingest_error}
