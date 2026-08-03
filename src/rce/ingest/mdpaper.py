r"""Deterministic Markdown paper ingester (task W3) -- zero-model extractor
layer, same constitution as `rce.ingest.latex`/`rce.ingest.claims`
(DESIGN.md section 0: "code beats models wherever code suffices", "never
guess"). A researcher's paper is often 88 .md files, not LaTeX; before this
module, none of that prose was reachable by the graph at all.

Architecture (Occam rule 5, "reuse existing code"): this is not a
from-scratch parser. It reuses, unchanged, three pieces of `rce.ingest.latex`
that are already format-agnostic despite living in the LaTeX module --

  * `ParsedSection` / `Link` -- both dataclasses hold only (id, title, level,
    line) / (section_id, target, line), nothing LaTeX-specific.
  * `_slugify` (via `_dedupe_slug`) -- the exact same slug + collision-
    numbering rule LaTeX \section/\subsection titles get, so a project
    mixing both formats never sees two different conventions. A title that
    slugifies to nothing printable (e.g. an all-Chinese heading -- see
    `_slugify`'s own fallback) collapses to "section"/"section-2"/... the
    same way it already does for LaTeX; this is not a new limitation
    introduced here, it is the existing rule applied identically.
  * `_match_known_image` -- extension-aware match against the repo's
    tracked image inventory, for the ghost-figure check below.

-- and, from `rce.ingest.claims`, the entire deterministic number-matching
core: `_scan_claims_in_lines` (recognised forms, the compound-modifier skip,
sentence extraction, content-addressed ids) and `ingest_parsed_claims` (the
node/edge-writing, candidate-matching, and orphan-cleanup write path). Only
the *blanking* upstream of those is Markdown-specific -- fenced code blocks
and GFM table rows here, LaTeX commands/environments there -- see
`_blank_code_fences`/`_blank_md_table_rows` below.

Not a CommonMark implementation: headings are ATX only (`#`/`##`/`###` --
Setext `Title\n===` headings are out of scope, never guessed at), a fenced
code block is recognised by a run of 3+ backticks or tildes with the same
character closing it (mismatched-character "closes" inside an open fence
are treated as still-open, matching the overwhelmingly common single-fence-
style convention), and a table is recognised by its own structural marker
(a line containing `|` immediately followed by a GFM delimiter row) rather
than a full table grammar. Every one of these is a deliberate, documented
simplification in the same spirit as `rce.ingest.latex`'s own
`\graphicspath`-first-directory-only simplification -- a hand-rolled
scanner for this much structure is less code and less risk than a new
Markdown-parsing dependency (DESIGN.md section 0, Occam rule 1; the project
ships zero required runtime dependencies, see pyproject.toml).

Which .md files count as paper prose (task W3 point 2): every .md file
`rce ingest`'s file inventory finds, except the well-known non-paper
convention names (README/CHANGELOG/LICENSE, in their customary ALL-CAPS
form) -- see `_is_paper_markdown`. Everything else is ingested: a
researcher's own map/draft/notes file is real, traceable prose too, and
"never guess" cuts against trying to be clever about which .md files are
"real" papers. An extra section node from a non-paper .md file is harmless;
a silently-dropped real one is not.
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
from rce.ingest import claims as claims_ingest
from rce.ingest.latex import Link, ParsedSection, _dedupe_slug, _match_known_image

logger = logging.getLogger(__name__)

# ATX headings only (CommonMark's other heading form, Setext, is out of
# scope -- never guessed at). Up to 3 leading spaces tolerated (CommonMark
# allows it); trailing "#*" (a closing ATX sequence, e.g. "## Title ##") is
# stripped. Restricted to 1-3 hashes on purpose (task W3): a 4th leading '#'
# makes `\s+` fail to match at that position, so h4+ headings are not
# recognised as sections at all, mirroring rce.ingest.latex's own
# \section/\subsection-only (no \subsubsection) scope.
_MD_HEADING_RE = re.compile(r"^ {0,3}(#{1,3})\s+(.*?)\s*#*\s*$")

# ![alt](path "optional title") -- the path group stops at the first
# whitespace or ')', so a path containing a literal space or parenthesis
# (the CommonMark <path> / escaped-paren forms) is not handled; a real
# research repo's image filenames are not expected to need those forms, and
# under-matching here only means a figure link is skipped + logged, never a
# wrong one fabricated.
_MD_IMAGE_RE = re.compile(r'!\[[^\]]*\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)')

# A fenced code block opens with a line of 3+ backticks or tildes (optional
# leading indent up to 3 spaces, optional language tag after -- both
# irrelevant to the toggle, the whole marker line is blanked regardless).
_CODE_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

# A GFM table delimiter row: only '-', ':', '|', and whitespace, with at
# least one '|' somewhere on the line (the lookahead) -- the '|' requirement
# is deliberately stricter than bare GFM so a plain Markdown horizontal rule
# ("---" on its own line, a common section divider in a hand-written paper)
# is never mistaken for a one-column table's delimiter row (see
# _blank_md_table_rows for how this is used: it only ever fires when the
# line *before* this one already contains a '|' too, but requiring one here
# as well closes the remaining bare-"---"-after-a-'|'-line edge case).
_TABLE_DELIM_RE = re.compile(r"^\s*(?=.*\|)\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")

# Non-paper convention filenames (task W3 point 2), matched against the
# basename only and case-sensitively against the literal ALL-CAPS form real
# repos actually use -- a lowercase "readme_notes.md" documenting an
# experiment is a real research note, not a convention file, and is never
# guessed to be one.
_NON_PAPER_MD_PREFIXES = ("README", "CHANGELOG", "LICENSE")


def _is_paper_markdown(md_rel_path: str) -> bool:
    """False for a well-known non-paper convention filename; True (ingest
    it) for everything else -- see module docstring, "which .md files count"."""
    basename = posixpath.basename(md_rel_path)
    return not basename.startswith(_NON_PAPER_MD_PREFIXES)


def _blank_code_fences(lines: list[str]) -> list[str]:
    """Replace every line of a fenced code block -- including both fence
    marker lines -- with spaces, so a `#` comment or a decimal literal shown
    as example code is never mistaken for a real heading or claim. `depth`
    is not needed (unlike LaTeX's environment nesting): a fence cannot
    nest inside another still-open fence of the same kind (the first
    matching close always ends it), so a simple open/closed toggle
    suffices.

    A fence line with a *different* character than the one currently open
    (e.g. a literal ```` ``` ```` shown inside an open `~~~` block) is
    treated as ordinary fenced content, not a close -- matching the
    overwhelmingly common single-fence-style convention in real documents;
    mismatched nested fencing is out of scope (v0 simplification, mirrors
    rce.ingest.latex's own documented simplifications elsewhere).
    """
    blanked = list(lines)
    in_fence = False
    fence_marker: str | None = None
    for i, line in enumerate(lines):
        m = _CODE_FENCE_RE.match(line)
        if m:
            marker = m.group(1)[0]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, None
            blanked[i] = " " * len(line)
            continue
        if in_fence:
            blanked[i] = " " * len(line)
    return blanked


def _blank_md_table_rows(lines: list[str]) -> list[str]:
    """Replace every line of a GFM-style table (header + delimiter + data
    rows) with spaces -- mirrors rce.ingest.claims's LaTeX tabular-
    environment skip, so a number in a table cell is never scanned as a
    claim.

    A table is recognised by its own structural marker: a line containing
    '|' immediately followed by a delimiter row (`_TABLE_DELIM_RE`) -- the
    one thing a real GFM table always has and ordinary prose never does.
    Once found this way, every following line containing '|' is a data row
    of the same table; the table ends at the first blank line or first
    line with no '|' at all. Intended to run on lines already passed through
    `_blank_code_fences`, so a literal `---`/`|` shown inside example code is
    already all-spaces and can never falsely trigger this.
    """
    blanked = list(lines)
    n = len(lines)
    i = 0
    while i < n - 1:
        if "|" in lines[i] and _TABLE_DELIM_RE.match(lines[i + 1]):
            start = i
            j = i + 1
            while j < n and lines[j].strip() and "|" in lines[j]:
                j += 1
            for k in range(start, j):
                blanked[k] = " " * len(lines[k])
            i = j
        else:
            i += 1
    return blanked


def _resolve_image_path(md_rel_path: str, raw_path: str) -> str | None:
    """Resolve an image reference's path relative to the .md file's own
    directory (Markdown has no `\\graphicspath`-equivalent, so there is no
    second directory to try). None (caller skips + logs) for: an empty
    path, a remote URL (http(s):// or protocol-relative "//" -- not a
    repo-tracked file, nothing to validate against the image inventory), or
    a path that normalizes outside the repo root."""
    raw_path = raw_path.strip()
    if not raw_path:
        return None
    if raw_path.startswith("<") and raw_path.endswith(">") and len(raw_path) >= 2:
        raw_path = raw_path[1:-1].strip()  # CommonMark's <path with spaces> form
    if raw_path.startswith(("http://", "https://", "//")):
        return None
    md_dir = posixpath.dirname(md_rel_path) or "."
    normalized = posixpath.normpath(posixpath.join(md_dir, raw_path))
    if normalized == ".." or normalized.startswith("../") or posixpath.isabs(normalized):
        return None
    return normalized


@dataclass(frozen=True)
class MdParseResult:
    md_path: str
    sections: list[ParsedSection]
    section_attrs: dict[str, dict[str, Any]]
    figure_links: list[Link]


def parse_md_file(repo_root: str | Path, md_rel_path: str) -> MdParseResult:
    """Parse one Markdown file's headings and image references.

    A figure before the first heading has no section to attach to and is
    skipped + logged -- mirroring `rce.ingest.latex.parse_tex_file`'s
    identical treatment of `\\includegraphics` before any `\\section`; there
    is no document-level fallback node for either format.
    """
    text = (Path(repo_root) / md_rel_path).read_text(errors="replace")
    raw_lines = text.splitlines()
    fenced = _blank_code_fences(raw_lines)  # headings/images must not see fenced-code content

    sections: list[ParsedSection] = []
    section_attrs: dict[str, dict[str, Any]] = {}
    figure_links: list[Link] = []
    slug_counts: dict[str, int] = {}
    current_id: str | None = None

    for lineno, line in enumerate(fenced, start=1):
        heading_match = _MD_HEADING_RE.match(line)
        if heading_match:
            hashes, title = heading_match.group(1), heading_match.group(2).strip()
            slug = _dedupe_slug(title, slug_counts)
            current_id = f"section:{md_rel_path}#{slug}"
            sections.append(ParsedSection(current_id, title, f"h{len(hashes)}", lineno))
            section_attrs[current_id] = {}

        for img_match in _MD_IMAGE_RE.finditer(line):
            if current_id is None:
                logger.warning("%s:%d: image reference before any heading; skipping", md_rel_path, lineno)
                continue
            resolved = _resolve_image_path(md_rel_path, img_match.group(1))
            if resolved is None:
                logger.warning(
                    "%s:%d: cannot resolve image path %r (empty, a remote URL, or outside "
                    "the repo root); skipping",
                    md_rel_path, lineno, img_match.group(1),
                )
                continue
            figure_links.append(Link(current_id, f"figure:{resolved}", lineno))

    return MdParseResult(md_rel_path, sections, section_attrs, figure_links)


def parse_md_claims(repo_root: str | Path, md_rel_path: str) -> list[claims_ingest.ParsedClaim]:
    """Scan one Markdown file for quantitative prose claims -- the Markdown
    counterpart of `rce.ingest.claims.parse_tex_claims`, reusing that
    module's `_scan_claims_in_lines` number-matching core unchanged (see
    this module's docstring): recognised forms, the compound-modifier skip,
    and the content-addressed id scheme are identical for LaTeX and
    Markdown prose. Only the blanking upstream differs -- fenced code
    blocks and GFM table rows here, LaTeX commands/environments there.
    """
    repo_root = Path(repo_root)
    text = (repo_root / md_rel_path).read_text(errors="replace")
    raw_lines = text.splitlines()
    fenced = _blank_code_fences(raw_lines)
    tabled = _blank_md_table_rows(fenced)
    sections = parse_md_file(repo_root, md_rel_path).sections  # ascending by line
    return claims_ingest._scan_claims_in_lines(md_rel_path, tabled, sections)


def ingest_md_repo(
    conn: Connection, repo_root: str | Path, md_paths: list[str], image_paths: list[str] | None = None,
) -> dict[str, int]:
    """Ingest Markdown paper sources into the graph (task W3): ATX headings
    as section nodes, image references as `section --includes--> figure`
    edges (ghost-figure validated exactly like `rce.ingest.latex`), and
    quantitative prose claims via the exact deterministic core
    `rce.ingest.claims` uses for LaTeX (`ingest_parsed_claims`) -- `claim`
    nodes and pending `backed_by` candidates only ever appear when an
    `experiment` node with a matching metric already exists, same as LaTeX.

    Must run after mlflow/wandb ingestion for the same reason
    `rce.ingest.claims.ingest_claims_repo` must (see there): with no
    `experiment` nodes yet, every claim would trivially get zero
    candidates.

    `_is_paper_markdown` filters out the well-known non-paper convention
    filenames (README/CHANGELOG/LICENSE) before anything else runs; the
    count of how many were skipped this way is returned as
    `md_skipped_non_paper` so a caller's summary line can state it plainly
    rather than a bare "0 sections" looking like a broken extractor (never
    silent about *why* a result is empty).

    `image_paths`, if given, is the exact set of repo-tracked image paths
    (mirrors `rce.ingest.latex.ingest_latex_repo`'s own parameter) to
    validate image references against; a resolved path with no match is a
    ghost figure -- skipped + logged, never a dangling node. `None`
    disables the check entirely (library-call default, matches latex.py).

    Idempotent via db.upsert_node/upsert_edge and
    `ingest_parsed_claims`'s own orphan cleanup, scoped to exactly the .md
    paths this call successfully read -- a file that fails to read (or is
    filtered out as a non-paper convention name) is excluded from that
    scope, never treated as evidence its content is gone (DESIGN.md section
    0, "never guess"; see `rce.ingest.claims._cleanup_orphaned_claims`).
    """
    repo_root = Path(repo_root)
    known_images: set[str] | None = None if image_paths is None else set(image_paths)

    parsed: list[MdParseResult] = []
    parsed_claims_by_path: dict[str, list[claims_ingest.ParsedClaim]] = {}
    skipped_non_paper = 0
    for md_rel_path in md_paths:
        if not _is_paper_markdown(md_rel_path):
            skipped_non_paper += 1
            logger.info(
                "%s: convention filename (README/CHANGELOG/LICENSE), not paper prose -- skipping",
                md_rel_path,
            )
            continue
        try:
            result = parse_md_file(repo_root, md_rel_path)
            claims_here = parse_md_claims(repo_root, md_rel_path)
        except OSError as exc:
            logger.warning("cannot read md file %s: %s", md_rel_path, exc)
            continue
        parsed.append(result)
        parsed_claims_by_path[md_rel_path] = claims_here

    counts = {"sections": 0, "figures": 0, "md_skipped_non_paper": skipped_non_paper}
    for result in parsed:
        for section in result.sections:
            attrs: dict[str, Any] = dict(result.section_attrs[section.id])
            # "tex_path" (not "md_path") is deliberate -- see
            # ingest_parsed_claims's docstring for why the same literal key
            # is reused across formats for claim nodes; section attrs carry
            # it too purely for informational consistency (rce query's
            # attrs dump), nothing structural depends on the name here.
            attrs["tex_path"] = result.md_path
            attrs["level"] = section.level
            db.upsert_node(conn, section.id, "section", title=section.title, attrs=attrs)
            counts["sections"] += 1

        for fig_link in result.figure_links:
            fig_path = fig_link.target.split(":", 1)[1]
            if known_images is not None:
                matched_path = _match_known_image(fig_path, known_images)
                if matched_path is None:
                    logger.warning(
                        "%s:%d: image reference resolves to %r, which is not a tracked "
                        "repo image under any of git.IMAGE_EXTENSIONS; skipping "
                        "(ghost figure, no node or edge created)",
                        result.md_path, fig_link.line, fig_path,
                    )
                    continue
                fig_path = matched_path
            fig_id = f"figure:{fig_path}"
            db.upsert_node(conn, fig_id, "figure", title=fig_path)
            db.upsert_edge(
                conn, fig_link.section_id, fig_id, "includes",
                extractor="mdpaper", evidence={"file": result.md_path, "line": fig_link.line},
                confidence=1.0, status="auto",
            )
            counts["figures"] += 1

    claim_counts = claims_ingest.ingest_parsed_claims(conn, parsed_claims_by_path)
    counts.update(claim_counts)
    return counts
