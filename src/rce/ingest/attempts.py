"""Deterministic ingest for a researcher's hand-maintained attempt timeline
(DESIGN.md section 4, migration 0002, task A2).

Config-driven, never guessed (DESIGN.md section 0): which file, heading,
and column maps to which field come only from `.rce/attempts.toml`
(SAMPLE_CONFIG is both the doc and the missing-config error's template).

`verdict`/`result` are the human's judgement in the row's own prose --
written to `human_fields` via `set_human_fields` once, on first sight of an
id, and never again: a later re-parse refreshes `attrs` (machine-parsed
facts) but leaves `human_fields` exactly as it is, so a correction made any
other way is never silently reset by a routine re-ingest.

An id reused by a row whose description no longer matches what's already
stored under it is a collision (DESIGN.md section 4): skipped and logged,
never merged onto the existing node.
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
file = "00-项目地图_唯一真相.md"      # markdown file, relative to the project root
heading = "二、尝试途径总年表"          # heading right above the table (prefix match is enough)
steps_dir = "复现包_分步"              # optional: numbered step scripts dir, for step-ref linking

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


@dataclass(frozen=True)
class AttemptsConfig:
    file: str
    heading: str
    columns: dict[str, str]
    steps_dir: str | None = None


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
    return AttemptsConfig(
        file=data["file"], heading=data["heading"], columns=dict(columns), steps_dir=data.get("steps_dir"),
    )


# -- Markdown table location + cell cleanup ---------------------------------

_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_CODE_RE = re.compile(r"`([^`]*)`")
_BOLD_RE = re.compile(r"\*\*([^*]*)\*\*")
_STEP_REF_RE = re.compile(r"\((\d+)(?:-(\d+))?\)")


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
    position. Raises AttemptsConfigError if a configured column name isn't
    among the table's actual headers; returns [] with a logged warning if
    the heading or a table under it isn't present at all."""
    lines = md_text.splitlines()
    located = _find_table(lines, heading)
    if located is None:
        logger.warning("heading %r or a table under it not found", heading)
        return []
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
    matched text, no fuzzy matching."""
    return [f"{a}-{b}" if b else a for a, b in _STEP_REF_RE.findall(description)]


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


# -- Graph ingest -------------------------------------------------------------


def _node_id(file: str, number: str) -> str:
    return f"attempt:{file}#{number}"


def _cleanup_orphans(conn: Connection, file: str, seen_ids: set[str]) -> dict[str, int]:
    """Attempt nodes from `file` produced on an earlier run but not this one
    (row deleted/renumbered) -- mirrors rce.ingest.claims's orphan cleanup.
    A node still carrying human_fields (true of every attempt node this
    extractor writes) is preserved and logged, never deleted -- it records a
    real judgement call, not something a re-ingest gets to erase."""
    removed = preserved = 0
    prefix = f"attempt:{file}#"
    for node in db.get_nodes_by_type(conn, "attempt"):
        if node["id"] in seen_ids or not node["id"].startswith(prefix):
            continue
        if node["human_fields"]:
            preserved += 1
            logger.info("%s no longer present in %s -- preserved (has human_fields)", node["id"], file)
            continue
        db.delete_edges_for_node(conn, node["id"], extractor="attempts")
        db.delete_node(conn, node["id"])
        removed += 1
    return {"orphans_removed": removed, "orphans_preserved": preserved}


def ingest_attempts_repo(conn: Connection, project_root: str | Path, config: AttemptsConfig) -> dict[str, int]:
    """Parse the configured attempt timeline and mirror it into `attempt`
    nodes. Idempotent on (file, id): re-running never duplicates a node and
    never touches an existing node's `human_fields` (see module docstring).
    A read failure on the source file is logged and returns all-zero counts
    rather than raising -- not evidence the attempts are gone."""
    project_root = Path(project_root)
    counts = {
        "attempts": 0, "created": 0, "updated": 0, "collisions_skipped": 0,
        "orphans_removed": 0, "orphans_preserved": 0,
    }
    source_path = project_root / config.file
    try:
        text = source_path.read_text(errors="replace")
    except OSError as exc:
        logger.error("cannot read %s: %s", source_path, exc)
        return counts

    steps_dir = (project_root / config.steps_dir) if config.steps_dir else None
    seen_ids: set[str] = set()

    for row in parse_attempts_table(text, config.heading, config.columns):
        node_id = _node_id(config.file, row.number)
        existing = db.get_node(conn, node_id)
        if existing is not None and existing["attrs"].get("description") != row.description:
            counts["collisions_skipped"] += 1
            logger.warning(
                "%s: id %r already holds a different attempt (%r) -- new row %r skipped, never "
                "merged onto an existing id (DESIGN.md section 4)",
                config.file, row.number, existing["attrs"].get("description"), row.description,
            )
            continue
        seen_ids.add(node_id)
        counts["attempts"] += 1

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
        if is_new or not existing["human_fields"]:
            db.set_human_fields(conn, node_id, {"verdict": row.verdict, "result": row.result})

    counts.update(_cleanup_orphans(conn, config.file, seen_ids))
    return counts
