"""Deterministic LaTeX/.bib ingester -- zero-model extractor layer (T2).

Conservative line-by-line regex/brace-scanning, no third-party LaTeX/bib
library (Occam rule 1/5: \\section, \\includegraphics, \\cite, and .bib
@entry are simple enough that a hand-rolled scanner is less code and less
risk than a new dependency). Writes via rce.db's upsert_node/upsert_edge,
inheriting idempotency from there. Anything not safely resolvable is
skipped and logged, never guessed (HANDOFF-SPEC.md section 5).
"""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from rce import db

logger = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"\\(section|subsection)\*?\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")
_INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]*)\}")
_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\{((?:\{[^{}]*\})+)\}")
_GRAPHICSPATH_DIR_RE = re.compile(r"\{([^{}]*)\}")
_CITE_RE = re.compile(r"\\cite\*?(?:\[[^\]]*\])?(?:\[[^\]]*\])?\{([^{}]*)\}")
_LABEL_RE = re.compile(r"\\label\{([^{}]*)\}")
_REF_RE = re.compile(r"\\ref\{([^{}]*)\}")
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")

_BIB_ENTRY_START_RE = re.compile(r"@(\w+)\s*\{\s*([^,\s}]+)\s*,")
_BIB_FIELD_START_RE = re.compile(r"(\w+)\s*=\s*")
_BIB_FIELDS_WANTED = frozenset({"title", "author", "year"})
_BIB_NON_ENTRY_TYPES = frozenset({"comment", "string", "preamble"})

def _strip_comment(line: str) -> str:
    """Drop everything from the first unescaped '%' onward ('\\%' is a
    literal percent, not a comment start, so it survives intact)."""
    out, i = [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            out.append(line[i : i + 2])
            i += 2
            continue
        if ch == "%":
            break
        out.append(ch)
        i += 1
    return "".join(out)

def _slugify(title: str) -> str:
    return _SLUG_INVALID_RE.sub("-", title.strip().lower()).strip("-") or "section"

def _resolve_figure_path(tex_rel_path: str, graphics_dir: str | None, raw_path: str) -> str | None:
    """Resolve relative to the .tex file, with basic \\graphicspath support
    (first declared directory only -- v0 simplification, see deviations).
    None (caller skips + logs) for: empty path, an unexpandable macro (a
    literal '\\'), or a path that normalizes outside the repo root."""
    raw_path = raw_path.strip()
    if not raw_path or "\\" in raw_path:
        return None
    tex_dir = posixpath.dirname(tex_rel_path) or "."
    prefix = graphics_dir.strip("/") if graphics_dir else "."
    normalized = posixpath.normpath(posixpath.join(tex_dir, prefix, raw_path))
    if normalized == ".." or normalized.startswith("../") or posixpath.isabs(normalized):
        return None
    return normalized

@dataclass(frozen=True)
class ParsedSection:
    id: str
    title: str
    level: str
    line: int

@dataclass(frozen=True)
class Link:
    """A section's outgoing link -- `target` is a figure_id for
    figure_links or a bare .bib key for cite_links."""
    section_id: str
    target: str
    line: int

@dataclass(frozen=True)
class TexParseResult:
    tex_path: str
    sections: list[ParsedSection]
    section_attrs: dict[str, dict[str, list]]
    figure_links: list[Link]
    cite_links: list[Link]

@dataclass(frozen=True)
class BibEntry:
    key: str
    entry_type: str
    fields: dict[str, str]

def parse_tex_file(repo_root: str | Path, tex_rel_path: str) -> TexParseResult:
    """Parse one .tex file, line by line. \\label/\\ref are recorded only
    onto the current section's attrs -- v0 adds no new edge type for them
    (architecture decision, see deviations). A figure/cite/label/ref before
    the first \\section has no Section to attach to and is skipped+logged:
    the ontology has no document-level fallback node.
    """
    text = (Path(repo_root) / tex_rel_path).read_text(errors="replace")
    sections: list[ParsedSection] = []
    section_attrs: dict[str, dict[str, list]] = {}
    figure_links: list[Link] = []
    cite_links: list[Link] = []
    slug_counts: dict[str, int] = {}
    current_id: str | None = None
    graphics_dir: str | None = None

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw_line)

        if graphics_dir is None:
            gp_match = _GRAPHICSPATH_RE.search(line)
            if gp_match:
                dirs = _GRAPHICSPATH_DIR_RE.findall(gp_match.group(1))
                if dirs:
                    graphics_dir = dirs[0]

        sec_match = _SECTION_RE.search(line)
        if sec_match:
            level, title = sec_match.group(1), sec_match.group(2).strip()
            base_slug = _slugify(title)
            seen = slug_counts.get(base_slug, 0)
            slug = base_slug if seen == 0 else f"{base_slug}-{seen + 1}"
            slug_counts[base_slug] = seen + 1
            current_id = f"section:{tex_rel_path}#{slug}"
            sections.append(ParsedSection(current_id, title, level, lineno))
            section_attrs[current_id] = {"labels": [], "refs": []}

        for fig_match in _INCLUDEGRAPHICS_RE.finditer(line):
            if current_id is None:
                logger.warning("%s:%d: \\includegraphics before any \\section; skipping", tex_rel_path, lineno)
                continue
            resolved = _resolve_figure_path(tex_rel_path, graphics_dir, fig_match.group(1))
            if resolved is None:
                logger.warning(
                    "%s:%d: cannot resolve \\includegraphics path %r; skipping",
                    tex_rel_path, lineno, fig_match.group(1),
                )
                continue
            figure_links.append(Link(current_id, f"figure:{resolved}", lineno))

        for cite_match in _CITE_RE.finditer(line):
            if current_id is None:
                logger.warning("%s:%d: \\cite before any \\section; skipping", tex_rel_path, lineno)
                continue
            for key in (k.strip() for k in cite_match.group(1).split(",")):
                if key:
                    cite_links.append(Link(current_id, key, lineno))

        if current_id is not None:
            for attr_name, pattern in (("labels", _LABEL_RE), ("refs", _REF_RE)):
                for m in pattern.finditer(line):
                    section_attrs[current_id][attr_name].append({"name": m.group(1).strip(), "line": lineno})

    return TexParseResult(tex_rel_path, sections, section_attrs, figure_links, cite_links)

def _parse_bib_fields(body: str) -> dict[str, str]:
    """Extract title/author/year from one @entry's body, handling BibTeX's
    three value forms: {braced} (brace-counted, nested braces survive),
    "quoted", and bare (e.g. `year = 2020`, ends at the next comma)."""
    fields: dict[str, str] = {}
    for m in _BIB_FIELD_START_RE.finditer(body):
        name = m.group(1).lower()
        if name not in _BIB_FIELDS_WANTED or name in fields:
            continue
        start = m.end()
        if start >= len(body):
            continue
        delim = body[start]
        if delim == "{":
            depth, j = 1, start + 1
            while j < len(body) and depth > 0:
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                j += 1
            value = body[start + 1 : j - 1]
        elif delim == '"':
            end = body.find('"', start + 1)
            if end == -1:
                continue
            value = body[start + 1 : end]
        else:
            end = body.find(",", start)
            value = body[start : end if end != -1 else len(body)]
        fields[name] = value.strip()
    return fields

def parse_bib_entries(text: str) -> list[BibEntry]:
    """Parse @entry blocks via brace-depth counting -- not a full BibTeX
    grammar (Occam rule 1/5: title/author/year is all v0 needs)."""
    text = "\n".join(_strip_comment(line) for line in text.splitlines())
    entries: list[BibEntry] = []
    for m in _BIB_ENTRY_START_RE.finditer(text):
        entry_type = m.group(1).lower()
        if entry_type in _BIB_NON_ENTRY_TYPES:
            continue
        open_brace = text.index("{", m.start())
        depth, i = 1, open_brace + 1
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[m.end() : i - 1]
        entries.append(BibEntry(m.group(2).strip(), entry_type, _parse_bib_fields(body)))
    return entries

def parse_bib_file(bib_path: str | Path) -> list[BibEntry]:
    return parse_bib_entries(Path(bib_path).read_text(errors="replace"))

def ingest_latex_repo(
    conn: Connection, repo_root: str | Path, tex_paths: list[str], bib_paths: list[str]
) -> dict[str, int]:
    """Ingest LaTeX sources + .bib files into the provenance graph.

    Order is deliberate: bib files first (real Reference nodes), then all
    tex files are parsed to collect every cited key, then placeholder
    Reference nodes are written for keys with no bib entry -- all before
    any `cites` edge, since edges.dst has a foreign key to nodes.id and
    must already exist at insert time. Idempotent via db.upsert_*.
    """
    repo_root = Path(repo_root)
    resolved_keys: set[str] = set()

    for bib_rel_path in bib_paths:
        try:
            entries = parse_bib_file(repo_root / bib_rel_path)
        except OSError as exc:
            logger.warning("cannot read bib file %s: %s", bib_rel_path, exc)
            continue
        for entry in entries:
            db.upsert_node(
                conn, f"ref:{entry.key}", "reference", title=entry.fields.get("title"),
                attrs={
                    "entry_type": entry.entry_type,
                    "author": entry.fields.get("author"),
                    "year": entry.fields.get("year"),
                    "bib_file": bib_rel_path,
                },
            )
            resolved_keys.add(entry.key)

    parsed: list[TexParseResult] = []
    all_cited_keys: set[str] = set()
    for tex_rel_path in tex_paths:
        try:
            result = parse_tex_file(repo_root, tex_rel_path)
        except OSError as exc:
            logger.warning("cannot read tex file %s: %s", tex_rel_path, exc)
            continue
        parsed.append(result)
        all_cited_keys.update(link.target for link in result.cite_links)

    for key in all_cited_keys - resolved_keys:
        db.upsert_node(conn, f"ref:{key}", "reference", title=None, attrs={"unresolved": True})

    counts = {"sections": 0, "figures": 0, "cites": 0}
    for result in parsed:
        for section in result.sections:
            attrs: dict[str, Any] = dict(result.section_attrs[section.id])
            attrs["tex_path"] = result.tex_path
            attrs["level"] = section.level
            db.upsert_node(conn, section.id, "section", title=section.title, attrs=attrs)
            counts["sections"] += 1

        for fig_link in result.figure_links:
            db.upsert_node(conn, fig_link.target, "figure", title=fig_link.target.split(":", 1)[1])
            db.upsert_edge(
                conn, fig_link.section_id, fig_link.target, "includes",
                extractor="latex", evidence={"file": result.tex_path, "line": fig_link.line},
                confidence=1.0, status="auto",
            )
            counts["figures"] += 1

        for cite_link in result.cite_links:
            db.upsert_edge(
                conn, cite_link.section_id, f"ref:{cite_link.target}", "cites",
                extractor="latex", evidence={"file": result.tex_path, "line": cite_link.line},
                confidence=1.0, status="auto",
            )
            counts["cites"] += 1

    return counts
