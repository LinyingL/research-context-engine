"""Deterministic ingest for a researcher's hand-maintained attempt timeline
(DESIGN.md section 4, migration 0002, task A2).

Config-driven, never guessed (DESIGN.md section 0): which file, heading,
and column maps to which field come only from `.rce/attempts.toml`
(SAMPLE_CONFIG is both the doc and the missing-config error's template).

`verdict`/`result` are the human's judgement, but the researcher writes
that judgement in the row's own prose in the *source* Markdown file, not
in this graph. Every parse therefore writes them to `human_fields` via
`set_human_fields` again -- resynced to whatever the source row currently
says -- not just on first sight of an id. A write is skipped when the
freshly parsed verdict/result already match what is stored, so an
unchanged row produces no gratuitous write and no log noise; a changed
row always propagates on the next `rce attempts` run, however it changed.

This is the mirror image of `edges.status` (`confirm`/`reject`, DESIGN.md
section 0/2), not an exception to "humans own judgement": for an edge,
the candidate is machine-generated and the human's decision is made *in
the graph itself* via `rce confirm` -- the graph is the only place that
decision is ever recorded, so re-ingestion touching `status` would erase
it outright. For an attempt, the decision is made in the researcher's
*own source file* next to the row -- the graph only mirrors it. Freezing
`human_fields` on first sight (this extractor's original design) did not
protect that decision; it froze a copy against a source that kept moving,
which let the mirror silently go stale. A real audit against a 23-row
project map caught this concretely: a row's verdict changed from a dead
marker to an active one in the source file, and the freshly re-ingested
graph -- and `revived_dead_variables` reading it -- still reported no
findings, purely because the old write-once rule had locked that node's
`human_fields` on an earlier parse. The rule "never overwrite the one
place a human decision lives" did not change; resyncing on every parse is
what actually applies it here, because the source file, not the graph, is
that place.

This assumes the source Markdown file is currently the *only* place an
attempt's verdict is ever recorded. If a future write path lets someone
edit an attempt's verdict directly in the graph (mirroring `rce confirm`
for edges), that edit and the source file become two authorities that can
disagree, and this resync would silently discard the in-graph edit on the
next `rce attempts` run. No such path exists today -- `set_human_fields`
has exactly one caller in the whole `src` tree, this extractor -- but
whoever adds one must resolve that conflict explicitly (e.g. detect
divergence and refuse to overwrite, or have the in-graph edit rewrite the
source file) rather than discover it by losing an edit.

The same "source file is the sole authority" reasoning applies to a row's
*existence*, not just its verdict/result: a row deleted (or renumbered) out
of the table has its `attempt` node, and every edge touching it, deleted on
the next parse -- unless a human has confirmed or rejected one of those
edges in the graph itself (e.g. via `rce confirm` on a `uses` edge
`rce.consistency` wrote), in which case the node and its edges are left
alone and logged instead, the same `edges.status` signal
`rce.ingest.claims`'s own orphan cleanup already checks for. This still
differs from claims in scope (which edges count, not whether the check
happens at all) -- see `_cleanup_orphans` below for the full rule and why
an earlier version of this function got that scope question wrong.

Separately, and upstream of all of the above: a row's existence must never
be inferred from a *failed attempt* to even locate the table -- if the
configured heading or the table beneath it cannot be found in the source
file at all, that is an observation failure, not evidence every row is
gone, and `_cleanup_orphans` must not run at all in that case. See
`AttemptsTableNotFoundError` and `parse_attempts_table` below.

A description differing from what's already stored under an id is *not* a
collision -- it is the ordinary case of a human editing that cell (the
`途径`/description column is everyday table maintenance), and `attrs`
(description/date/variables/step_refs/step_files/source_line -- every
machine-parsed field) is refreshed on every re-parse just like any other
node's `attrs`; `human_fields` resyncs to the row's current verdict/result
same as always, which in this scenario is simply unchanged, since only the
description cell moved. The actual collision DESIGN.md section 4 warns
about is a `#` value used by *two different rows in the same parse* (a
duplicate id within one table): the second row is skipped and logged,
never merged onto the first.

`.rce/attempts.toml` also carries two optional top-level lists consumed by
`rce.consistency`'s revived-dead-variable check (task A3), not by ingest
itself: `dead_variables` (substrings from the project map's own "red line"
section that must never resurface in a live attempt) and `active_verdicts`
(which verdict markers, as the researcher writes them, count as "alive").
Both default to `None` (not `[]`) when absent from the TOML, so
`rce.consistency` can tell "not declared -- skip this check and say why"
apart from "declared empty -- run it, it just matches nothing".
`available_step_numbers` below is also consumed by that module, for its
broken-reference check's "nearest existing neighbor" hint.

A third optional top-level key, `date_year` (an int), is consumed by
`rce.consistency`'s stale-verdict check to parse a hand-written date column
that is rarely a clean `YYYY-MM-DD` -- see that module for the exact accepted
forms. It also defaults to `None` (not declared), in which case that check's
date parsing is unchanged: only a full `YYYY-MM-DD` is accepted.

All three of these top-level keys -- along with `steps_dir` -- must appear
*before* the `[columns]` table in the TOML file. TOML nests any bare key
written after a `[table]` header into that table, so a list written after
`[columns]` silently becomes `columns.dead_variables` and `load_config`
never sees it; this was a real bug in this very module's own `SAMPLE_CONFIG`
template (task A3.1) and is why the ordering is called out here and enforced
by `test_config_loads_real_shaped_toml` feeding `SAMPLE_CONFIG` itself
through `load_config`.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from rce import db

logger = logging.getLogger(__name__)

CONFIG_RELATIVE_PATH = ".rce/attempts.toml"
_REQUIRED_COLUMNS = ("id", "date", "description", "variables", "result", "verdict")

SAMPLE_CONFIG = """\
# .rce/attempts.toml -- tells `rce attempts` which file/heading/table holds
# your hand-maintained attempt timeline. RCE never guesses this.
#
# All top-level keys (this section) MUST come before the [columns] table
# below -- TOML nests any key written after a [table] header into that
# table, so a list like `dead_variables` placed after [columns] silently
# becomes `columns.dead_variables` and is never seen here again.
file = "00-项目地图_唯一真相.md"      # markdown file, relative to the project root
heading = "二、尝试途径总年表"          # heading right above the table (prefix match is enough)
steps_dir = "复现包_分步"              # optional: numbered step scripts dir, for step-ref linking

# Both optional (task A3, rce.consistency's revived-dead-variable check).
# Leave either unset to skip that one check -- rce never assumes an empty
# list means "declared, nothing dead" when you just never wrote the key.
dead_variables = ["信息熵", "lnRate 配置比例", "8 立场框架", "维基叙事度量"]
active_verdicts = ["✅", "🕒"]           # verdict markers that count as "alive"

# Optional (task A3, rce.consistency's stale-verdict check): the year your
# whole timeline's date column belongs to. You declare it -- rce never
# infers it. Once set, a bare "MM-DD", a "<=MM-DD"/"≤MM-DD" upper bound, or
# an "MM-DD~DD"/"MM-DD~MM-DD" range is also accepted (see rce.consistency
# for the exact rules); leave unset and only a full "YYYY-MM-DD" parses.
# date_year = 2026

[columns]                              # markdown header text for each field, as YOU wrote it
id = "#"
date = "时间"
description = "途径"
variables = "变量→因变量(频率)"
result = "结果"
verdict = "判决"
"""


class AttemptsConfigError(Exception):
    """Missing/unusable .rce/attempts.toml; message already includes
    SAMPLE_CONFIG so the caller can print it as-is."""


class AttemptsTableNotFoundError(AttemptsConfigError):
    """The configured `heading` or the table beneath it could not be located
    in the source file *this run* -- distinct from "the table was located
    and its data section is genuinely empty," which is a legitimate result
    and returns a plain `[]` from `parse_attempts_table`, not this error.

    A subclass of `AttemptsConfigError` (not a separate exception hierarchy)
    on purpose: whichever of the two is true -- the heading text drifted
    out of sync with `.rce/attempts.toml`, or the source file changed shape
    (an inserted sub-heading, a dropped separator row) -- from `rce
    attempts`'s point of view this is the same class of problem as a
    misconfigured column name: something `.rce/attempts.toml` points at no
    longer matches what is actually in the project, and `rce.cli.cmd_attempts`
    should give one clean message and exit 1, not guess and not crash with a
    raw traceback.

    Conflating this with "table found, zero rows" into a bare `[]` was
    Blocker 1 (DESIGN.md, "Attempt orphans"): a caller reading an empty
    return as "this file now has zero attempts" and feeding it to orphan
    cleanup deletes every node ever ingested for that file the moment the
    table merely becomes unlocatable -- an editing accident, not evidence
    the rows are gone. See `parse_attempts_table` and `ingest_attempts_repo`
    for how this is kept from reaching `_cleanup_orphans` at all.
    """


@dataclass(frozen=True)
class AttemptsConfig:
    file: str
    heading: str
    columns: dict[str, str]
    steps_dir: str | None = None
    # Task A3 (rce.consistency), not read by anything in this module: `None`
    # means "not declared in .rce/attempts.toml" (skip that check and say
    # why), distinct from an explicit `[]` (declared, matches nothing).
    dead_variables: list[str] | None = None
    active_verdicts: list[str] | None = None
    # Task A3 (rce.consistency's stale-verdict check), not read by anything
    # in this module: `None` means "not declared" -- only a strict
    # YYYY-MM-DD date parses. A declared value is the year the researcher
    # states the whole date column belongs to (never inferred by rce).
    date_year: int | None = None


def load_config(project_root: Path) -> AttemptsConfig:
    path = project_root / CONFIG_RELATIVE_PATH
    if not path.exists():
        raise AttemptsConfigError(
            f"no {CONFIG_RELATIVE_PATH} found -- rce never guesses which table is the "
            f"attempt timeline. Create one:\n\n{SAMPLE_CONFIG}"
        )
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise AttemptsConfigError(f"{path} is not valid TOML: {exc}\n\nTemplate:\n{SAMPLE_CONFIG}") from exc

    missing_top = [k for k in ("file", "heading", "columns") if k not in data]
    if missing_top:
        raise AttemptsConfigError(f"{path} missing required key(s) {missing_top}\n\nTemplate:\n{SAMPLE_CONFIG}")
    columns = data["columns"]
    missing_cols = [k for k in _REQUIRED_COLUMNS if k not in columns]
    if missing_cols:
        raise AttemptsConfigError(
            f"{path}'s [columns] missing required key(s) {missing_cols}\n\nTemplate:\n{SAMPLE_CONFIG}"
        )
    date_year = data.get("date_year")
    if date_year is not None and not isinstance(date_year, int):
        raise AttemptsConfigError(
            f"{path}'s date_year must be a plain integer year (e.g. 2026), got {date_year!r}"
        )
    return AttemptsConfig(
        file=data["file"], heading=data["heading"], columns=dict(columns), steps_dir=data.get("steps_dir"),
        # .get (not .get(..., [])): a missing key must stay None, never
        # silently become "[] -- declared, nothing dead" (see module
        # docstring and rce.consistency).
        dead_variables=data.get("dead_variables"), active_verdicts=data.get("active_verdicts"),
        date_year=date_year,
    )


# -- Markdown table location + cell cleanup ---------------------------------

_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_CODE_RE = re.compile(r"`([^`]*)`")
_BOLD_RE = re.compile(r"\*\*([^*]*)\*\*")
_STEP_REF_RE = re.compile(r"\((\d{1,3})(?:-(\d{1,3}))?\)")
# A conservative pair of guards, never a guess at which range is "real"
# (DESIGN.md section 0): the {1,3} digit cap means a 4+-digit number (a
# year, e.g. the "(2014-2026)" range that appears elsewhere in a real
# project map) never matches as a step reference at all -- a repro-package
# steps directory does not run into four digits. `_MAX_STEP_REF_WIDTH` is a
# second, independent guard against an implausibly wide *range* of
# otherwise-valid-looking 1-3-digit numbers (e.g. a typo'd "(1-500)").
_MAX_STEP_REF_WIDTH = 50


def _clean_cell(raw: str) -> str:
    """Strip Markdown decoration (bold/inline-code/links/`<br>`); arrows,
    emoji and other plain unicode text are the human's own content and are
    left untouched."""
    text = _BR_RE.sub("\n", raw)
    text = _LINK_RE.sub(r"\1", text)
    text = _CODE_RE.sub(r"\1", text)
    text = _BOLD_RE.sub(r"\1", text)
    return text.strip()


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line)]


def _is_separator_row(line: str) -> bool:
    body = line.strip().strip("|")
    return bool(body) and "-" in body and set(body) <= set(" -:|")


def _find_table(lines: list[str], heading: str) -> tuple[int, int] | None:
    """(header_line_idx, first_data_line_idx), 0-based, for the first table
    under `heading`; None if the heading or a table under it isn't found.
    Prose between heading and table is skipped over, not mistaken for it."""
    heading = heading.strip()
    start = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and m.group(1).startswith(heading):
            start = i + 1
            break
    if start is None:
        return None
    for i in range(start, len(lines) - 1):
        if _HEADING_RE.match(lines[i]):
            return None  # next heading reached first -- no table under ours
        if lines[i].strip().startswith("|") and _is_separator_row(lines[i + 1]):
            return i, i + 2
    return None


@dataclass(frozen=True)
class AttemptRow:
    number: str
    date: str
    description: str
    variables: str
    result: str
    verdict: str
    line: int


def parse_attempts_table(md_text: str, heading: str, columns: dict[str, str]) -> list[AttemptRow]:
    """Rows of the first table under `heading`, mapped by column *name*, not
    position.

    Raises `AttemptsConfigError` if a configured column name isn't among
    the table's actual headers. Raises `AttemptsTableNotFoundError` (a
    subclass of `AttemptsConfigError`) if the heading or a table under it
    can't be located at all -- callers must not treat this the same as a
    plain `[]` return. A plain `[]` is reserved for the *other* empty
    outcome: the heading and its table both were located, but the table's
    data section genuinely has no rows (every row really was deleted, or
    the timeline is a fresh, empty one) -- a real, legitimate state, not a
    failed observation. Collapsing these two outcomes into one bare `[]`
    was Blocker 1 (see `AttemptsTableNotFoundError`'s docstring and
    DESIGN.md's "Attempt orphans" section): a caller cannot tell "we looked
    and there is nothing" from "we could not look" from the return value
    alone, and treating the latter as the former is exactly the guess
    DESIGN.md section 0 rules out.
    """
    lines = md_text.splitlines()
    located = _find_table(lines, heading)
    if located is None:
        raise AttemptsTableNotFoundError(
            f"heading {heading!r} or a table under it not found in the source file -- "
            f".rce/attempts.toml currently configures heading={heading!r}; check that this "
            f"text still matches a heading in the file, and that a table (a header row "
            f"followed by a |---|---| separator row, prose in between is fine) still follows "
            f"it before the next heading"
        )
    header_idx, data_idx = located
    header_cells = [_clean_cell(c) for c in _split_row(lines[header_idx])]

    col_index: dict[str, int] = {}
    for key in _REQUIRED_COLUMNS:
        header_name = columns[key]
        if header_name not in header_cells:
            raise AttemptsConfigError(
                f"configured [columns].{key} = {header_name!r} not found in the table's actual "
                f"header {header_cells!r} -- check {CONFIG_RELATIVE_PATH}"
            )
        col_index[key] = header_cells.index(header_name)

    rows: list[AttemptRow] = []
    for i in range(data_idx, len(lines)):
        line = lines[i]
        if not line.strip().startswith("|"):
            break
        cells = _split_row(line)
        if len(cells) != len(header_cells):
            logger.warning("line %d: row has %d cell(s), expected %d -- skipped", i + 1, len(cells), len(header_cells))
            continue
        cleaned = [_clean_cell(c) for c in cells]
        number = cleaned[col_index["id"]]
        if not number:
            logger.warning("line %d: empty id cell -- skipped", i + 1)
            continue
        rows.append(AttemptRow(
            number=number, date=cleaned[col_index["date"]], description=cleaned[col_index["description"]],
            variables=cleaned[col_index["variables"]], result=cleaned[col_index["result"]],
            verdict=cleaned[col_index["verdict"]], line=i + 1,
        ))
    return rows


# -- Step-number reference resolution ----------------------------------------


def _extract_step_refs(description: str) -> list[str]:
    """Parenthesized step-number refs, e.g. "(16-18)" or "(5)" -- raw
    matched text, no fuzzy matching. A range wider than
    `_MAX_STEP_REF_WIDTH` is dropped and logged rather than treated as a
    step reference (see the constant's own comment; the 4+-digit case,
    e.g. a year range, is already excluded by `_STEP_REF_RE` itself and
    never reaches this function at all)."""
    refs = []
    for a, b in _STEP_REF_RE.findall(description):
        if not b:
            refs.append(a)
            continue
        width = int(b) - int(a)
        if width < 0 or width > _MAX_STEP_REF_WIDTH:
            logger.warning(
                "description %r: parenthesized range (%s-%s) is wider than %d -- not treated "
                "as a step-number reference (DESIGN.md section 0, conservative guard)",
                description, a, b, _MAX_STEP_REF_WIDTH,
            )
            continue
        refs.append(f"{a}-{b}")
    return refs


def _resolve_step_files(steps_dir: Path, refs: list[str]) -> tuple[list[str], list[int]]:
    """A file matches step N iff its name starts with "N-" (never guessed,
    DESIGN.md section 0). Several files can share one step number (e.g. a
    .Rmd and its rendered .pdf) -- all are kept. A number with zero matches
    comes back in `broken`, for a future consistency check (A3)."""
    wanted: set[int] = set()
    for ref in refs:
        a, _, b = ref.partition("-")
        wanted.update(range(int(a), int(b or a) + 1))
    if not wanted:
        return [], []
    by_number: dict[int, list[str]] = {n: [] for n in wanted}
    if steps_dir.is_dir():
        for entry in sorted(steps_dir.iterdir()):
            m = re.match(r"(\d+)-", entry.name)
            if entry.is_file() and m and int(m.group(1)) in by_number:
                by_number[int(m.group(1))].append(entry.name)
    files = sorted({name for names in by_number.values() for name in names})
    broken = sorted(n for n, names in by_number.items() if not names)
    return files, broken


def available_step_numbers(steps_dir: Path) -> list[int]:
    """Every step number actually present as a file prefix in `steps_dir`,
    sorted ascending -- the same "name starts with 'N-'" rule as
    `_resolve_step_files` (never guessed, DESIGN.md section 0). A missing
    directory returns [] rather than raising. Used by `rce.consistency`'s
    broken-reference check (task A3) to report the nearest step numbers
    that DO exist, so a human can spot a rename or deletion.
    """
    if not steps_dir.is_dir():
        return []
    numbers: set[int] = set()
    for entry in steps_dir.iterdir():
        m = re.match(r"(\d+)-", entry.name)
        if entry.is_file() and m:
            numbers.add(int(m.group(1)))
    return sorted(numbers)


# -- Graph ingest -------------------------------------------------------------

_ATTEMPT_NUMBER_RE = re.compile(r"^(\d+)(.*)$")


def attempt_sort_key(number: str) -> tuple[float, str]:
    """Natural order for attempt `#` labels: "14a" sorts right after "14",
    ahead of "15" -- a plain string sort would put "14a" after "2". A label
    with no leading digits (should not happen in practice) sorts last.

    Shared by `rce.cli`'s plain listing and every `rce.consistency` check
    (via `rce.consistency.attempts_for_file`), so a project's findings and
    its listing always walk attempts in the same order -- this used to be
    defined separately in `rce.cli` alone, which meant `--check`'s findings
    came out in raw database-read order instead of `#` order."""
    m = _ATTEMPT_NUMBER_RE.match(number)
    if not m:
        return (float("inf"), number)
    return (int(m.group(1)), m.group(2))


def _node_id(file: str, number: str) -> str:
    return f"attempt:{file}#{number}"


def _cleanup_orphans(conn: Connection, file: str, seen_ids: set[str]) -> dict[str, int]:
    """Attempt nodes from `file` produced on an earlier run but not this one
    (the row was deleted, or its `#` was renumbered out from under it):
    delete the node, and every edge touching it -- unless a human has
    confirmed or rejected one of those edges in the graph, in which case
    the node (and all its edges) are preserved instead.

    This is the deletion-direction half of "resync from source, not
    write-once" (module docstring above, DESIGN.md section 4): the source
    Markdown file is the sole authority for whether an attempt row exists
    at all, exactly as it is the sole authority for that row's
    verdict/result. A researcher deleting a row from their own map *is*
    the human decision, ordinarily; there is ordinarily nothing left in the
    graph worth protecting by keeping the node around once the row is
    gone. And if the source file itself lives in git -- the ordinary case
    -- its history still has the deleted row, so the graph does not have
    to be the archive of record.

    Do NOT read this as "the old preserve-if-judged behavior was a bug, so
    rce.ingest.claims's `_cleanup_orphaned_claims` must have the same bug":
    that one preserves an orphaned claim node whose `backed_by` edge
    carries a confirmed/rejected `edges.status`, and that is correct and
    stays correct. `human_fields` looking truthy is not evidence of a
    decision *in the graph* the way a confirmed/rejected edge is -- it is
    simply the last mirrored copy of whatever the row's verdict cell said,
    and the row no longer exists to mirror. Checking `human_fields` here,
    as an earlier version of this function did, was applying claims's
    guard to the wrong authority: every node this extractor ever writes
    gets human_fields on its very first parse (even an empty
    verdict/result becomes the truthy `{"verdict": "", "result": ""}`), so
    that guard was true for every attempt node in practice and the delete
    branch beneath it was dead code -- reachable only via a hand-built
    test node that never went through `set_human_fields` at all, never in
    real use.

    Removing that wrong check does not mean there is *no* right check,
    though, and an earlier version of this fix went one correction too
    far by concluding exactly that: it reasoned that an attempt node's
    only possible edge, `uses`, is "a deterministic, always-auto fact that
    no human ever confirms or rejects," and deleted unconditionally on
    that basis. That assumption was never actually enforced anywhere
    (Blocker 2, a real end-to-end run against the project map): `rce
    confirm` accepts *any* `(src, dst, type, extractor)` edge, with no
    allowlist restricting it to `backed_by`. `rce confirm --status
    confirmed <attempt-node> <commit> uses attempts_consistency` succeeds
    today and moves that edge's `status` to `"confirmed"` -- and the
    unconditional-delete version of this function then destroyed that
    edge and its node the next time the row was removed from the source
    and re-ingested, silently: no log line, no count, nothing to indicate
    a human judgement had just been erased.

    The right check, restored here, is the same signal claims already
    uses: `edges.status` in `('confirmed', 'rejected')`, not
    `human_fields`. Every edge touching an orphaned node -- `src` or `dst`,
    any extractor -- is inspected; if any of them carries that status, the
    node and every one of its edges are left exactly as they are, and this
    is logged (which node, which file it vanished from, how many edges
    carry a decision) and counted under `orphans_preserved_with_human_
    decision` so it surfaces in `rce attempts`'s own summary line, not
    only in the log. Only when none of a node's edges are confirmed or
    rejected does the delete proceed, exactly as before: every edge
    touching the node is removed (counted under `orphan_edges_removed`,
    mirroring claims's `backed_by_edges_removed`), then the node itself.

    This still differs from claims in one respect, unchanged from before:
    the edge check and the eventual delete are not scoped to edges this
    extractor (`attempts`) itself produced -- a `uses` edge written by
    `rce.consistency` (extractor `attempts_consistency`) is inspected and,
    if unconfirmed, deleted right alongside the node. Two reasons: first,
    a human-confirmed `uses` edge deserves the same protection as a
    human-confirmed `backed_by` edge regardless of which extractor wrote
    it -- there is no reason to scope the *protection* narrowly just
    because claims happens to scope its own extractor-local bookkeeping
    that way. Second, when no such protection applies, `nodes.id` is a
    foreign key `edges.src`/`dst` reference with no cascade
    (db.delete_node's own docstring) -- leaving an unconfirmed `uses` edge
    in place would make `delete_node` below raise `sqlite3.IntegrityError`
    on exactly the real-world case this fix is for (a project with
    steps_dir configured and `--check` already run at least once before
    the row was deleted).
    """
    removed = 0
    edges_removed = 0
    preserved = 0
    prefix = f"attempt:{file}#"
    for node in db.get_nodes_by_type(conn, "attempt"):
        node_id = node["id"]
        if node_id in seen_ids or not node_id.startswith(prefix):
            continue
        # Both directions, deduped by the edges table's own integer `id`:
        # an attempt is `src` of every edge this codebase currently writes
        # (`uses`), but nothing here assumes that stays true forever.
        touching = {e["id"]: e for e in db.query_edges(conn, src=node_id)}
        touching.update({e["id"]: e for e in db.query_edges(conn, dst=node_id)})
        human_judged = [e for e in touching.values() if e["status"] in ("confirmed", "rejected")]
        if human_judged:
            preserved += 1
            logger.warning(
                "%s no longer present in %s, but %d of its edge(s) carry a human confirm/reject "
                "decision (via rce confirm) -- preserving the node instead of deleting it, "
                "since deleting it would destroy that decision with no record anywhere else; "
                "resolve manually",
                node_id, file, len(human_judged),
            )
            continue
        edges_removed += db.delete_edges_for_node(conn, node_id)
        db.delete_node(conn, node_id)
        removed += 1
        logger.info("%s no longer present in %s -- deleted (source file is sole authority on whether it exists)", node_id, file)
    return {
        "orphans_removed": removed,
        "orphan_edges_removed": edges_removed,
        "orphans_preserved_with_human_decision": preserved,
    }


def ingest_attempts_repo(conn: Connection, project_root: str | Path, config: AttemptsConfig) -> dict[str, int]:
    """Parse the configured attempt timeline and mirror it into `attempt`
    nodes. Idempotent on (file, id): re-running never duplicates a node,
    always refreshes `attrs` (description/date/variables/step_refs/
    step_files/source_line) to the row's current text, and resyncs an
    existing node's `human_fields` (verdict/result) to the row's current
    values every time -- the `set_human_fields` *write* is skipped when the
    freshly parsed verdict/result already match what is stored, so an
    unchanged row causes no gratuitous write and no log noise (see module
    docstring for why this differs from `edges.status`). This no-op only
    covers that one write: `db.upsert_node` above it still runs, and still
    refreshes `attrs`/`updated_at`, for every row on every parse regardless
    of whether anything in the row actually changed -- `upsert_node` has no
    equality check of its own, unlike the `human_fields` write here, so an
    unchanged row is not a no-op end to end, only its human_fields half is.

    A row whose `#` repeats one already seen *in this same parse* is a
    collision -- skipped and logged, never merged onto the first (see
    module docstring). A row no longer present this parse (deleted, or
    renumbered away) is an orphan: its node, and every edge touching it, is
    deleted -- unless a human has confirmed or rejected one of those edges,
    in which case the node is preserved instead; see `_cleanup_orphans` for
    the full rule and its one remaining scope difference from
    `rce.ingest.claims`'s own orphan handling.

    Two distinct failures short-circuit before any of the above runs, and
    are handled differently on purpose (DESIGN.md section 0, "never
    guess" -- neither is evidence the attempts are gone, but only one of
    them is left silent): a read failure on the source file itself
    (`OSError`) is logged and this function returns all-zero counts rather
    than raising -- orphan cleanup does not run in that case either (every
    existing node for `config.file` would otherwise look orphaned). A
    *table location* failure -- the configured heading or the table under
    it cannot be found at all, `parse_attempts_table` raising
    `AttemptsTableNotFoundError` -- is logged with the same "cleanup
    skipped, existing nodes untouched" framing but is then re-raised rather
    than swallowed: the caller (`rce.cli.cmd_attempts`) treats it as a
    config-shaped error and exits 1, because unlike a transient read
    failure this means the run's own request could not be satisfied at all
    and the operator should notice, not see a quiet zero-count success
    line."""
    project_root = Path(project_root)
    counts = {
        "attempts": 0, "created": 0, "updated": 0, "collisions_skipped": 0,
        "orphans_removed": 0, "orphan_edges_removed": 0,
        "orphans_preserved_with_human_decision": 0,
    }
    source_path = project_root / config.file
    try:
        text = source_path.read_text(errors="replace")
    except OSError as exc:
        logger.error("cannot read %s: %s", source_path, exc)
        return counts

    steps_dir = (project_root / config.steps_dir) if config.steps_dir else None
    seen_ids: set[str] = set()

    try:
        rows = parse_attempts_table(text, config.heading, config.columns)
    except AttemptsTableNotFoundError:
        logger.error(
            "%s: could not locate this run's attempt table -- orphan cleanup skipped entirely "
            "(not treated as \"zero attempts, everything else orphaned\"); every existing "
            "attempt node for %r is left exactly as it was",
            source_path, config.file,
        )
        raise

    for row in rows:
        node_id = _node_id(config.file, row.number)
        if node_id in seen_ids:
            counts["collisions_skipped"] += 1
            logger.warning(
                "%s: id %r appears on more than one row in this parse (duplicate #) -- second "
                "and later occurrences skipped, never merged onto the first (DESIGN.md section 4)",
                config.file, row.number,
            )
            continue
        seen_ids.add(node_id)
        counts["attempts"] += 1
        existing = db.get_node(conn, node_id)

        attrs: dict[str, Any] = {
            "number": row.number, "date": row.date, "description": row.description,
            "variables": row.variables, "source_file": config.file, "source_line": row.line,
        }
        refs = _extract_step_refs(row.description)
        attrs["step_refs"] = refs
        if steps_dir is not None:
            files, broken = _resolve_step_files(steps_dir, refs)
            attrs["step_files"] = files
            attrs["step_files_broken"] = broken

        is_new = existing is None
        db.upsert_node(conn, node_id, "attempt", title=row.description, attrs=attrs)
        counts["created" if is_new else "updated"] += 1

        # Resync every parse (module docstring): the source file is the
        # sole authority for verdict/result, so a changed row must always
        # propagate. The equality check below is only to skip a
        # no-op write on an unchanged row -- it is not a write-once guard.
        new_human_fields = {"verdict": row.verdict, "result": row.result}
        current_human_fields = None if is_new else existing["human_fields"]
        if current_human_fields != new_human_fields:
            db.set_human_fields(conn, node_id, new_human_fields)

    counts.update(_cleanup_orphans(conn, config.file, seen_ids))
    return counts
