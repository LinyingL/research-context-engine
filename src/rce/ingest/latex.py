"""Deterministic LaTeX/.bib ingester -- zero-model extractor layer (T2).

Conservative line-by-line regex/brace-scanning, no third-party LaTeX/bib
library (Occam rule 1/5: \\section, \\includegraphics, \\cite, and .bib
@entry are simple enough that a hand-rolled scanner is less code and less
risk than a new dependency). Writes via rce.db's upsert_node/upsert_edge,
inheriting idempotency from there. Anything not safely resolvable is
skipped and logged, never guessed (DESIGN.md section 5).
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from rce import db
from rce.ingest import git as git_ingest

logger = logging.getLogger(__name__)

# Deterministic extension search order for a \includegraphics path written
# without a suffix (e.g. \includegraphics{overview}) -- ordinary, common
# LaTeX usage: pdflatex/latex itself tries a fixed list of extensions at
# compile time until one is found on disk. .pdf/.png/.jpg/.jpeg first
# (pdflatex's own priority order), the rest of git.IMAGE_EXTENSIONS
# alphabetically -- deterministic, not a guess (DESIGN.md section 5).
_EXT_SEARCH_ORDER = (".pdf", ".png", ".jpg", ".jpeg") + tuple(
    sorted(git_ingest.IMAGE_EXTENSIONS - {".pdf", ".png", ".jpg", ".jpeg"})
)

_SECTION_RE = re.compile(r"\\(section|subsection)\*?\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")
_INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]*)\}")
_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\{((?:\{[^{}]*\})+)\}")
_GRAPHICSPATH_DIR_RE = re.compile(r"\{([^{}]*)\}")
_CITE_RE = re.compile(
    # natbib (\cite, \citep, \citet, \citealp -- plus capitalized \Citep/\Citet
    # sentence-start variants) and biblatex (\parencite, \textcite, \autocite).
    # DESIGN.md section 5, 2026-07-22 addendum: real papers overwhelmingly
    # use \citep/\citet, so \cite alone leaves `cites` edges almost never firing.
    r"\\(?:cite(?:p|t|alp)?|Citep|Citet|parencite|textcite|autocite)\*?"
    r"(?:\[[^\]]*\])?(?:\[[^\]]*\])?\{([^{}]*)\}"
)
_LABEL_RE = re.compile(r"\\label\{([^{}]*)\}")
_REF_RE = re.compile(r"\\ref\{([^{}]*)\}")
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")

# Hex digits of a sha256 digest used only for _slugify's no-ASCII-survives
# fallback below -- an id-length convention, not a match/scoring threshold,
# same rationale as claims._ID_HASH_HEX_LEN (DESIGN.md section 0's "never
# guess" governs match *decisions*, not how many hash characters make a
# fallback slug practically collision-free within one file).
_SLUG_FALLBACK_HASH_HEX_LEN = 8

# T10 (candidate-2 testbed regression): a \cite in the abstract/introduction,
# before the paper's first \section, used to be dropped outright -- real
# papers routinely cite prior work before any \section command. Such
# citations now attach to this synthetic per-file section instead of being
# lost; see parse_tex_file's cite-link handling below.
_PREAMBLE_SLUG = "preamble"
_PREAMBLE_TITLE = "Preamble/Abstract"

def _normalize_bib_key(key: str) -> str:
    """Canonical form used for ref: node IDs and cite/bib matching.

    DESIGN.md section 5, 2026-07-22 addendum: bib key matching is
    case-insensitive (\\cite{smith2020} must resolve against
    @article{Smith2020}), so node IDs use this lowercase form while the
    original as-written key is kept in the node's attrs.
    """
    return key.strip().lower()


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
    """Lowercase `title`, collapse every run of non a-z0-9 characters to a
    single '-', and strip leading/trailing '-'.

    A title with no ASCII letters/digits at all -- routine for a
    Chinese-language heading, which is the overwhelming majority of real
    headings in the audience task W3's Markdown support directly targets --
    leaves nothing printable this way. That case used to fall back to the
    bare literal `"section"`, disambiguated only by `_dedupe_slug`'s
    per-file *encounter-order* counter below ("section", "section-2", ...).
    Because `claims._content_id` folds the owning section's slug into every
    claim's content-addressed id (DESIGN.md section 4), that made the slug
    -- and therefore every claim id beneath it -- silently *position*-
    derived for most non-ASCII headings: inserting, deleting, or reordering
    one unrelated same-shaped heading anywhere earlier in the file
    renumbered every later fallback slug, changing claim ids whose own
    sentence/number/section-title never changed and silently orphaning any
    human confirm/reject verdict recorded against them (the exact failure
    mode section 4 says content-addressed ids exist to prevent).
    The fallback is instead derived from the title's own text: a short hash
    of the raw (pre-lowercasing) title, stable no matter what else in the
    file changes. Two sections whose titles are genuinely identical still
    hash identically and get disambiguated by `_dedupe_slug`'s counter, same
    as any other repeated slug -- only a *different* non-ASCII title now
    also gets a different slug, instead of colliding with every other one.
    """
    slug = _SLUG_INVALID_RE.sub("-", title.strip().lower()).strip("-")
    if slug:
        return slug
    digest = hashlib.sha256(title.strip().encode("utf-8")).hexdigest()[:_SLUG_FALLBACK_HASH_HEX_LEN]
    return f"section-{digest}"

def _compute_ordered_slugs(entries: list[tuple[str, str]]) -> list[str]:
    """Two-pass slug assignment shared by `parse_tex_file` below (LaTeX
    \\section/\\subsection) and `rce.ingest.mdpaper.parse_md_file` (Markdown
    ATX headings) -- extracted (task W3) so both formats agree on
    collision-numbering, rewritten here (blocker, two review rounds) so
    numbering no longer depends on encounter order.

    Bug this replaces: when a title has few or no ASCII characters
    (`_slugify` strips everything else), two genuinely *different* titles
    routinely land on the same base slug -- e.g. "H3 检验" and another
    differently-worded heading both slugify to "h3", or "A. 引言"/"A. 方法"
    both slugify to "a". The old single-pass `_dedupe_slug` disambiguated
    any repeated base slug purely by an encounter-order counter (`-2`,
    `-3`, ...), with no regard for whether the colliding titles were
    actually the same text. Because `claims._content_id` folds the owning
    section's slug into every claim's content-addressed id (DESIGN.md
    section 4), inserting/reordering an unrelated heading earlier in the
    file could renumber every later colliding slug -- and every claim id
    beneath it -- with no change to those headings' own text: the exact
    position-dependence content-addressed ids exist to prevent, reintroduced
    one level up.

    `entries` is `(base_slug_text, identity_text)` pairs in document order
    -- the caller must gather every heading before calling this (hence each
    caller below is itself two passes). One entry per heading, plus (LaTeX
    only) a leading entry for the synthetic preamble node when needed, with
    `base_slug_text=_PREAMBLE_SLUG` (occupies that reserved slug exactly
    like a real heading slugifying to "preamble" would) and
    `identity_text=_PREAMBLE_TITLE` (hash-disambiguates it as a distinct
    "title" from a real `\\section{Preamble}` sharing that slug).

    Fix, in two passes over the full `entries` list:

    1. Compute `_slugify(base_slug_text)` per entry, group by that base.
    2. A base touched by >= 2 *distinct* `identity_text` values is a real
       collision: every entry sharing it gets
       `<base>-<sha256(identity_text)[:_SLUG_FALLBACK_HASH_HEX_LEN]>` --
       a function of the heading's own text, so inserting/reordering an
       unrelated heading never moves it. A base touched by exactly one
       distinct text is not a collision and keeps the bare base slug,
       unchanged from before this fix -- a non-colliding project's ids do
       not move at all.
    3. The one remaining position-dependent case: `identity_text` repeated
       verbatim (no textual difference left to key on). Repeats of
       whichever slug step 2 produced are numbered `-2`, `-3`, ... in
       encounter order -- `_dedupe_slug`'s old convention, now applied on
       top of a collision-aware base.
    """
    base_slugs = [_slugify(base_text) for base_text, _ in entries]

    distinct_identities: dict[str, set[str]] = {}
    for (_, identity), base in zip(entries, base_slugs):
        distinct_identities.setdefault(base, set()).add(identity)

    disambiguated: list[str] = []
    for (_, identity), base in zip(entries, base_slugs):
        if len(distinct_identities[base]) >= 2:
            digest = hashlib.sha256(identity.strip().encode("utf-8")).hexdigest()[:_SLUG_FALLBACK_HASH_HEX_LEN]
            disambiguated.append(f"{base}-{digest}")
        else:
            disambiguated.append(base)

    seen_counts: dict[str, int] = {}
    ordered: list[str] = []
    for slug in disambiguated:
        seen = seen_counts.get(slug, 0)
        seen_counts[slug] = seen + 1
        ordered.append(slug if seen == 0 else f"{slug}-{seen + 1}")
    return ordered

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

def _match_known_image(fig_path: str, known_images: set[str]) -> str | None:
    """Resolve a parsed \\includegraphics path against the repo's tracked
    image inventory, extension-aware.

    \\includegraphics{overview} (no suffix) is normal, mainstream LaTeX
    usage -- pdflatex/latex itself searches a fixed list of extensions at
    compile time. Regression fix (commit 3085680 introduced an exact-match-
    only ghost-figure guard that misclassified this common form as a ghost):
    a path that already ends in one of git.IMAGE_EXTENSIONS is still matched
    exactly against `known_images`, unchanged. A path with no image suffix
    is instead tried against each candidate in _EXT_SEARCH_ORDER
    (deterministic, mirrors pdflatex's own search order) and the first
    extension present in `known_images` wins. If more than one candidate is
    tracked (e.g. both overview.png and overview.pdf exist), that ambiguity
    is logged at INFO so the choice stays auditable. No match at all -- a
    real ghost figure -- returns None; the caller skips + logs a warning.
    """
    if posixpath.splitext(fig_path)[1].lower() in git_ingest.IMAGE_EXTENSIONS:
        return fig_path if fig_path in known_images else None
    hits = [f"{fig_path}{ext}" for ext in _EXT_SEARCH_ORDER if f"{fig_path}{ext}" in known_images]
    if not hits:
        return None
    if len(hits) > 1:
        logger.info(
            "%r matches multiple tracked images %r; using %r (deterministic "
            "extension search order, mirrors pdflatex)", fig_path, hits, hits[0],
        )
    return hits[0]

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
    section_attrs: dict[str, dict[str, Any]]
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
    (architecture decision, see deviations). A figure/label/ref before the
    first \\section still has no Section to attach to and is skipped+logged:
    the ontology has no document-level fallback node for those.

    A \\cite (or \\citep/\\citet/etc.) before the first \\section is the one
    exception (T10, candidate-2 testbed regression): real papers routinely
    cite prior work in the abstract or introduction before any \\section
    command, and dropping those citations silently loses real evidence. Such
    citations attach instead to a synthetic `section:<tex_rel_path>#preamble`
    node (title "Preamble/Abstract", `attrs["synthetic"] = True`), created
    lazily -- only emitted into `sections`/`section_attrs` (and so only ever
    upserted as a node) when at least one such citation is actually present.

    T-blocker fix (two review rounds): every heading's slug -- including the
    synthetic preamble node below, when needed -- is now computed in one
    shot, up front, by `_compute_ordered_slugs` (full rationale there),
    instead of a running per-line counter. A real `\\section{Preamble}` is
    just one more entry in that same collision-detection pass as the
    synthetic node, so a genuine collision now gets `preamble-<hash>` on
    *both* sides rather than the old `preamble`/`preamble-2` numbered by
    whichever the parser reached first -- more stable, not merely
    different (see this module's test suite for the updated expectations).
    The `setdefault`/dedup guards below remain a second, defensive layer
    against the residual (astronomically unlikely) case of a hash collision.
    """
    text = (Path(repo_root) / tex_rel_path).read_text(errors="replace")
    stripped_lines = [_strip_comment(raw_line) for raw_line in text.splitlines()]

    # Pass 1 (lightweight): collect every heading's title in document order,
    # and whether a synthetic preamble node will be needed (a \cite-family
    # command appears before the first \section/\subsection) -- both must be
    # known before any slug is assigned (_compute_ordered_slugs above).
    heading_titles: list[str] = []
    preamble_needed = False
    seen_first_section = False
    for line in stripped_lines:
        sec_match = _SECTION_RE.search(line)
        if sec_match:
            heading_titles.append(sec_match.group(2).strip())
            seen_first_section = True
            continue
        if not seen_first_section and _CITE_RE.search(line):
            preamble_needed = True

    slug_entries: list[tuple[str, str]] = []
    if preamble_needed:
        slug_entries.append((_PREAMBLE_SLUG, _PREAMBLE_TITLE))
    slug_entries.extend((title, title) for title in heading_titles)
    slug_iter = iter(_compute_ordered_slugs(slug_entries))

    sections: list[ParsedSection] = []
    section_attrs: dict[str, dict[str, Any]] = {}
    figure_links: list[Link] = []
    cite_links: list[Link] = []
    current_id: str | None = None
    graphics_dir: str | None = None
    preamble_id = f"section:{tex_rel_path}#{next(slug_iter) if preamble_needed else _PREAMBLE_SLUG}"
    preamble_first_line: int | None = None

    for lineno, line in enumerate(stripped_lines, start=1):
        if graphics_dir is None:
            gp_match = _GRAPHICSPATH_RE.search(line)
            if gp_match:
                dirs = _GRAPHICSPATH_DIR_RE.findall(gp_match.group(1))
                if dirs:
                    graphics_dir = dirs[0]

        sec_match = _SECTION_RE.search(line)
        if sec_match:
            level, title = sec_match.group(1), sec_match.group(2).strip()
            slug = next(slug_iter)
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
            cite_section_id = current_id
            if cite_section_id is None:
                # T10: attach to the synthetic preamble section instead of
                # dropping -- see this function's docstring.
                cite_section_id = preamble_id
                if preamble_first_line is None:
                    preamble_first_line = lineno
            for key in (k.strip() for k in cite_match.group(1).split(",")):
                if key:
                    cite_links.append(Link(cite_section_id, key, lineno))

        if current_id is not None:
            for attr_name, pattern in (("labels", _LABEL_RE), ("refs", _REF_RE)):
                for m in pattern.finditer(line):
                    section_attrs[current_id][attr_name].append({"name": m.group(1).strip(), "line": lineno})

    if preamble_first_line is not None:
        # Inserted first: textually, any preamble citation precedes every
        # real \section in the file. Both guards below are defensive
        # (slug_counts above should already prevent id collision with a
        # real section) -- never silently clobber or duplicate an existing
        # entry for this id.
        if not any(s.id == preamble_id for s in sections):
            sections.insert(0, ParsedSection(preamble_id, _PREAMBLE_TITLE, "preamble", preamble_first_line))
        section_attrs.setdefault(preamble_id, {"labels": [], "refs": [], "synthetic": True})

    return TexParseResult(tex_rel_path, sections, section_attrs, figure_links, cite_links)

def _parse_bib_fields(body: str) -> dict[str, str]:
    """Extract title/author/year from one @entry's body, handling BibTeX's
    three value forms: {braced} (brace-counted, nested braces survive),
    "quoted", and bare (e.g. `year = 2020`, ends at the next comma).

    Scans with a cursor that always advances past a field's *entire* value
    -- wanted or not -- before looking for the next field name. This is
    required even for fields we discard (note/url/booktitle/...): a
    finditer-style scan of the whole body would happily match a `name =`
    substring sitting inside another field's value (e.g.
    `note = {see author = Smith}`) and silently overwrite title/author/year
    with garbage. Constitution: parsing must skip+log on doubt, never guess."""
    fields: dict[str, str] = {}
    pos, n = 0, len(body)
    while pos < n:
        m = _BIB_FIELD_START_RE.match(body, pos)
        if not m:
            pos += 1
            continue
        name = m.group(1).lower()
        start = m.end()
        if start >= n:
            break
        delim = body[start]
        if delim == "{":
            depth, j = 1, start + 1
            while j < n and depth > 0:
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                j += 1
            value = body[start + 1 : j - 1]
            pos = j
        elif delim == '"':
            end = body.find('"', start + 1)
            if end == -1:
                break  # unterminated quote -- stop rather than guess further
            value = body[start + 1 : end]
            pos = end + 1
        else:
            end = body.find(",", start)
            pos = end if end != -1 else n
            value = body[start:pos]
        if name in _BIB_FIELDS_WANTED and name not in fields:
            fields[name] = value.strip()
    return fields

def parse_bib_entries(text: str) -> list[BibEntry]:
    """Parse @entry blocks via brace-depth counting -- not a full BibTeX
    grammar (Occam rule 1/5: title/author/year is all v0 needs).

    Unlike .tex, '%' has no comment meaning in BibTeX -- it is an ordinary
    character that shows up routinely in field values (e.g.
    `title = {Achieving 50% accuracy}`). No comment stripping happens here:
    _strip_comment is .tex-only (see parse_tex_file). The @-anchored entry
    scan below already ignores anything outside an @entry, so nothing
    outside entries needs stripping either."""
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
    conn: Connection, repo_root: str | Path, tex_paths: list[str], bib_paths: list[str],
    image_paths: list[str] | None = None,
) -> dict[str, int]:
    """Ingest LaTeX sources + .bib files into the provenance graph.

    Order is deliberate: bib files first (real Reference nodes), then all
    tex files are parsed to collect every cited key, then placeholder
    Reference nodes are written for keys with no bib entry -- all before
    any `cites` edge, since edges.dst has a foreign key to nodes.id and
    must already exist at insert time. Idempotent via db.upsert_*.

    Bib key matching is case-insensitive (see _normalize_bib_key): both the
    resolved-keys set below and every ref: node ID use the lowercase form,
    so \\cite{smith2020} resolves against @article{Smith2020} instead of
    producing a spurious unresolved placeholder alongside the real node.
    Two distinctly-cased bib keys that collide after normalization (e.g.
    Smith2020 and smith2020 in the same or different .bib files) still
    resolve to one node, last-write-wins -- but that overwrite is now
    logged (T5.5 review item 1), never silent.

    `image_paths`, if given, is the exact set of repo-tracked image paths
    (e.g. from rce.ingest.git.list_source_files()["image"]) to validate
    \\includegraphics targets against: a resolved path with no match in this
    set -- including via the extension-aware fallback in
    _match_known_image() for a suffix-less \\includegraphics{name} -- is a
    "ghost figure" (references an image the repo does not actually have)
    and is skipped + logged rather than becoming a node with no backing
    file (T5.5 review item 2). `None` (the default) disables this check
    entirely, keeping this function usable as a standalone library call
    (e.g. in tests) without requiring a repo file inventory.
    """
    repo_root = Path(repo_root)
    resolved_keys: set[str] = set()  # normalized (lowercase) keys with a real bib entry
    known_images: set[str] | None = None if image_paths is None else set(image_paths)
    # normalized key -> as-written key of the bib entry currently stored at
    # that key, so a later differently-cased collision can be detected.
    seen_bib_keys: dict[str, str] = {}

    for bib_rel_path in bib_paths:
        try:
            entries = parse_bib_file(repo_root / bib_rel_path)
        except OSError as exc:
            logger.warning("cannot read bib file %s: %s", bib_rel_path, exc)
            continue
        for entry in entries:
            norm_key = _normalize_bib_key(entry.key)
            prior_key = seen_bib_keys.get(norm_key)
            if prior_key is not None and prior_key != entry.key:
                logger.warning(
                    "%s: bib key %r collides with already-seen key %r after "
                    "case normalization (both -> %r); %r overwrites %r (last write wins)",
                    bib_rel_path, entry.key, prior_key, norm_key, entry.key, prior_key,
                )
            seen_bib_keys[norm_key] = entry.key
            db.upsert_node(
                conn, f"ref:{norm_key}", "reference", title=entry.fields.get("title"),
                attrs={
                    "entry_type": entry.entry_type,
                    "author": entry.fields.get("author"),
                    "year": entry.fields.get("year"),
                    "bib_file": bib_rel_path,
                    "key": entry.key,  # original as-written casing, preserved
                },
            )
            resolved_keys.add(norm_key)

    parsed: list[TexParseResult] = []
    all_cited_keys: set[str] = set()  # normalized (lowercase)
    for tex_rel_path in tex_paths:
        try:
            result = parse_tex_file(repo_root, tex_rel_path)
        except OSError as exc:
            logger.warning("cannot read tex file %s: %s", tex_rel_path, exc)
            continue
        parsed.append(result)
        all_cited_keys.update(_normalize_bib_key(link.target) for link in result.cite_links)

    for key in all_cited_keys - resolved_keys:
        db.upsert_node(conn, f"ref:{key}", "reference", title=None, attrs={"unresolved": True, "key": key})

    counts = {"sections": 0, "figures": 0, "cites": 0}
    for result in parsed:
        for section in result.sections:
            attrs: dict[str, Any] = dict(result.section_attrs[section.id])
            attrs["tex_path"] = result.tex_path
            attrs["level"] = section.level
            db.upsert_node(conn, section.id, "section", title=section.title, attrs=attrs)
            counts["sections"] += 1

        for fig_link in result.figure_links:
            fig_path = fig_link.target.split(":", 1)[1]
            if known_images is not None:
                matched_path = _match_known_image(fig_path, known_images)
                if matched_path is None:
                    logger.warning(
                        "%s:%d: \\includegraphics resolves to %r, which is not a tracked "
                        "repo image under any of git.IMAGE_EXTENSIONS; skipping "
                        "(ghost figure, no node or edge created)",
                        result.tex_path, fig_link.line, fig_path,
                    )
                    continue
                fig_path = matched_path
            fig_id = f"figure:{fig_path}"
            db.upsert_node(conn, fig_id, "figure", title=fig_path)
            db.upsert_edge(
                conn, fig_link.section_id, fig_id, "includes",
                extractor="latex", evidence={"file": result.tex_path, "line": fig_link.line},
                confidence=1.0, status="auto",
            )
            counts["figures"] += 1

        for cite_link in result.cite_links:
            norm_key = _normalize_bib_key(cite_link.target)
            db.upsert_edge(
                conn, cite_link.section_id, f"ref:{norm_key}", "cites",
                extractor="latex", evidence={"file": result.tex_path, "line": cite_link.line},
                confidence=1.0, status="auto",
            )
            counts["cites"] += 1

    return counts
