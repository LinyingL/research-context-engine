"""The `citations` extractor (DESIGN.md 11.4): the drafts tied to the
literature they cite. Deterministic: what the text says, resolved only by
identifiers or offered as candidates -- never guessed (Section 0).

**What it reads.** Every draft of the inventory: the `.md` files the
Markdown extractor reads (README/CHANGELOG/LICENSE are not drafts) and the
`.tex` files. Fenced code blocks (Markdown) and comments (LaTeX) are not
prose and are not read; tables are (a review's table cites as much as its
prose does).

- **DOIs** (`rce.literature.find_dois`, normalised per 11.4). A DOI inside
  a URL is one DOI, found once.
- **Author-year citations in Latin script.** Narrative -- `Simon (1955)`,
  `Simon（1955）`, `Chen & Peng (2010)`, `Chen and Peng (2010)`,
  `Hassan et al. (2019)`, `Berardi, M. (2022)`, `Simon (1955, 1976)` --
  and parenthetical -- `(Hansen, 2000)`, `（Samuelson & Zeckhauser,
  1988）`, `(Fatum et al. 2017)`, `(Fraiberger 2021)`, several separated
  by `;` or `；`. Years
  1900-2099 with an optional letter; a page (`p. 12`, `: 12`) may follow.
  The drafts write in Chinese around Latin names, so the connectives
  `、` `与` `和` `及` and `等` (et al.) are read beside `,` `&` `and`
  `et al.`; the NAMES are Latin, always (11.4: `张川川（2020）` is not
  read). In a parenthetical citation without a comma before the year no
  Latin word may stand right before the names (`(Gelpern et al. Economic
  Policy 2022)` gives nothing, not "Policy 2022"); a capitalised word alone
  with a year in brackets (`(Beijing 2008)`) is read as a citation -- on
  the researcher's drafts the comma-less form was 89 real citations and no
  such word (measured 2026-10-09); it resolves to nothing and shows as
  unresolved, it never makes a link.
  Not a name, never read as the first author: a word without a lower-case
  letter (`AES(2023)`, `GDP (2019)`), month names (`July (2019)`), and the
  capitalised words a sentence or a caption starts with (`Figure`,
  `Table`, `Section`, `However`, `In` ... -- `_NOT_NAMES`); when such a word
  leads a list of names (`Moreover, Smith (2020)`) it is dropped and the
  next name is the first author.
  Each occurrence keeps its file, line, column, exact text, section and --
  when a claim (Section 4) covers the sentence -- the claim(s).
- **The draft's own reference list**: the entries under a heading
  `参考文献` / `References` / `Bibliography` (any heading level, an
  optional numbering prefix; `\\section{...}` or `thebibliography` in
  LaTeX) up to the next heading of the same or a higher level, or a
  thematic break (`---`). An entry is a list item or a line (a following
  indented line continues it); blockquotes and tables in the list are not
  entries. Each entry: first author's surname, year (+ letter), DOI if it
  has one, the text. Citations and DOIs inside the reference list are its
  entries, not citations.

**Resolution** (11.4, each step its own function): 1. `resolve_in_draft`:
exactly one entry of the same draft with the same first surname and year
(and letter) is that entry -- `auto`; several are candidates -- `pending`.
2. `zotero_item_for_doi`: a DOI (in the text, or the matched entry's) to
the Zotero item with that DOI. 3. `rce.literature.lookup_dois`: DOI
online, only when the setting is on (it only adds what the reference
is; the link already rests on the DOI). 4. `zotero_candidates`: an
author-year citation with no DOI path -- Zotero items whose first
creator's surname (case/diacritics-insensitive) and year match are
candidates, `pending`. A DOI written in the text is an identifier: `auto`.

**The graph.** Nodes `ref:doi:<doi>`, `ref:zotero:<item key>` (no DOI),
`ref:entry:<draft>#<n>` (an entry with neither; n counts the entries of
the list from 1). Edges `cites` from the citation's section and from each
claim covering it, extractor `citations`; one edge per (from, reference),
its evidence the occurrences (`file`, `line`, `text`). A citation with
neither a section nor a claim (before the first heading) is counted
`unanchored` and links nothing. Basis (9.6): `{"cited": [normalised
"surname|year" or "doi:<doi>"], "entry_dois": [the matched entries'
DOIs]}`, plus `"entries": [the entry's text]` for a link to a
`ref:entry:` node, whose id is only a position in the list. Source (9.6): the draft; for a Zotero candidate the pair
`<draft>\\x1fzotero` (`scan.citations_zotero_source`), reported unreadable
when the library could not be read, so those links then keep their state.
A link a scan that read its source no longer produces is removed (its
stamps are kept for review), and so is a reference no link points to: the
graph says what the drafts cite now.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Callable, Iterable, Mapping

from rce import cloud
from rce import db
from rce import literature
from rce.ingest import claims as claims_ingest
from rce.ingest import mdpaper as mdpaper_ingest
from rce.ingest import scan as scan_mod
from rce.ingest.latex import ParsedSection, _strip_comment, parse_tex_file

logger = logging.getLogger(__name__)

EXTRACTOR = "citations"

# -- the grammar -------------------------------------------------------------------

#: Latin letters: ASCII, Latin-1, Latin Extended-A/B, Latin Extended Additional.
_L = "A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f\u1e00-\u1eff"
_PARTICLE = r"(?:[Vv]an|[Vv]on|[Dd]e|[Dd]er|[Dd]en|[Dd]el|[Dd]ella|[Dd]i|[Dd]u|[Dd]a|[Dd]os|[Dd]as|[Ll]e|[Ll]a|[Tt]en|[Tt]er)"
_NAME = rf"(?:{_PARTICLE}\s+){{0,2}}[{_L}](?:[{_L}]|['’][{_L}]|-[{_L}])*"
_INITIALS = r"[A-Z]\.(?:\s*-?\s*[A-Z]\.)*"
_AUTHOR = rf"{_NAME}(?:\s*,\s*{_INITIALS})?"
_CONJ = r"(?:&|\band\b|与|和|及)"
_SEP = rf"(?:\s*[,，]\s*(?:{_CONJ}\s*)?|\s*[、/–—]\s*|\s*{_CONJ}\s*)"
_ETAL = r"(?:\s*,?\s*et\s*al\.?|\s*等人?)"
_START = rf"(?<![{_L}\d'’\-.])"
_AUTHORS = rf"{_START}{_AUTHOR}(?:{_SEP}{_AUTHOR}){{0,9}}"
_YEAR = r"(?:19|20)\d{2}[a-z]?(?![0-9A-Za-z])"
_PAGE = r"(?:\s*(?:[,，]\s*pp?\.|[:：])\s*\d+(?:\s*[-–]\s*\d+)?)?"
_YEARS = rf"{_YEAR}{_PAGE}(?:\s*[,，;；]\s*{_YEAR}{_PAGE})*"

_YEAR_TOKEN_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})([a-z]?)(?![0-9A-Za-z])")
#: The bracket after a narrative citation's names: the years, then -- after a
#: comma or semicolon -- anything, one level of brackets included (a
#: volume's issue: `(2019, JFE 132(2):384-403)`).
_NARRATIVE_BRACKET_RE = re.compile(
    rf"[（(]\s*(?P<years>{_YEARS})\s*(?P<rest>[,，;；](?:[^()（）]|[（(][^()（）]*[)）])*)?[)）]"
)
#: A further year in that rest, after a semicolon: `Manski (1990, ...; 1994)`.
_REST_YEAR_RE = re.compile(rf"[;；]\s*(?P<years>{_YEAR}{_PAGE})\s*(?=[;；)）]|$)")
#: Latin names joined by `与` `和` `及` `、` to Chinese text before them
#: (`张川川与 Simon（2020）`): the Chinese text may be the first author,
#: whose name V7 does not read (11.4) -- or prose (`增加与 Fatum et al.
#: (2017) 的对话`, `AES三部曲、Ferranti(2025)`): the two cannot be told
#: apart. The citation is read, its first Latin name kept as written, and
#: it is never resolved by that name alone: even one matching entry of
#: the draft is only a candidate (`cjk_joined`).
_CJK_JOINED_RE = re.compile(r"[\u3400-\u9fff]\s*(?:与|和|及|、)\s*$")
_NARRATIVE_AUTHORS_RE = re.compile(rf"{_AUTHORS}(?:{_ETAL})?\s*$")
_GROUP_RE = re.compile(r"[（(]([^()（）]{1,400})[)）]")
#: A part of a parenthetical group: names, then the years -- after a comma,
#: after `et al.`/`等`, or after a space alone when no Latin word stands
#: right before the names (`(Fraiberger 2021)`, but not the `Policy 2022`
#: of `(Gelpern et al. Economic Policy 2022)`).
_PART_RES = (
    re.compile(rf"{_AUTHORS}(?:{_ETAL})?\s*[,，]\s*(?P<years>{_YEARS})\s*$"),
    re.compile(rf"{_AUTHORS}{_ETAL}\s*(?P<years>{_YEARS})\s*$"),
    re.compile(rf"(?<![{_L}\d]\s){_AUTHORS}\s+(?P<years>{_YEARS})\s*$"),
)
_NAME_TOKEN_RE = re.compile(rf"{_START}{_NAME}")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s\u3000-\u303f\u3400-\u9fff\uff00-\uffef<>\"|]+")


def _url_spans(text: str) -> list[tuple[int, int]]:
    """Where the URLs of `text` are: each up to the first closing bracket
    it did not open (a Markdown link's `](https://x)` and the citation's
    own `)` after it are not the URL's)."""
    return [(m.start(), m.start() + len(literature._balanced(m.group(0)))) for m in _URL_RE.finditer(text)]
#: How far before a narrative bracket the names may start.
_NARRATIVE_WINDOW = 200

_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}
#: Capitalised words that start a sentence, a caption or a reference to a
#: part of a text -- never a first author.
_NOT_NAMES = _MONTHS | {
    "a", "an", "the", "this", "that", "these", "those", "it", "its", "our", "we", "their", "they",
    "in", "on", "at", "as", "by", "for", "from", "with", "of", "to", "into", "since", "after", "before", "during",
    "however", "moreover", "furthermore", "thus", "hence", "also", "and", "but", "or", "so", "then", "here",
    "there", "see", "cf", "following", "recently", "notably", "similarly", "specifically", "unlike", "like",
    "while", "whereas", "although", "both", "using", "according", "per", "via", "eg", "ie", "vs",
    "section", "sections", "table", "tables", "figure", "figures", "fig", "figs", "appendix", "chapter",
    "panel", "column", "columns", "equation", "eq", "model", "models", "note", "notes", "source", "sources",
    "data", "sample", "wave", "round", "version", "step", "stage", "phase", "part", "volume", "vol", "no",
    "page", "pages", "year", "years", "spring", "summer", "autumn", "fall", "winter", "q", "fy",
}
_REFERENCE_TITLES = {"参考文献", "references", "bibliography"}

_MD_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_THEMATIC_BREAK_RE = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
_LIST_ITEM_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)]|\[\d{1,3}\])\s+(.*)$")
_TITLE_PREFIX_RE = re.compile(r"^(?:[\dIVXivx一二三四五六七八九十]+[.、．)）]?\s*|[（(][\d一二三四五六七八九十]+[)）]\s*)")
_TEX_REF_HEADING_RE = re.compile(r"\\(?:part|chapter|section|subsection|subsubsection)\*?\{([^{}]*)\}")
_TEX_ANY_HEADING_RE = re.compile(r"\\(part|chapter|section|subsection|subsubsection)\*?\{")
_TEX_LEVELS = {"part": 0, "chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4}
_TEX_ITEM_RE = re.compile(r"\\(?:bibitem|item)(?:\[[^\]]*\])?(?:\{[^{}]*\})?\s*")


#: One part of a surname: capitalised, and capitalised inside only after a
#: prefix names carry (`McDowell`, `MacKinnon`, `DeGroot`, `O'Brien`) --
#: `TopicShift` is a variable, not a name.
_NAME_PART_RE = re.compile(
    rf"^(?:Mc|Mac|O['’]|D['’]|De|Di|Da|Du|La|Le|Van|Von|Fitz)?[A-Z\u00c0-\u00de\u0100-\u024f\u1e00-\u1eff]"
    rf"[^A-Z\u00c0-\u00de]*$"
)


def is_surname(word: str, *, month_ok: bool = False) -> bool:
    """A Latin surname as a citation writes it: starts upper-case, has a
    lower-case letter, is not a word a sentence or caption starts with.
    A month name is a surname only where a date cannot be meant
    (`month_ok`: `March & Simon (1958)`, `(March, 1963)`)."""
    core = re.sub(rf"^(?:{_PARTICLE}\s+)+", "", word) or word
    excluded = _NOT_NAMES - _MONTHS if month_ok else _NOT_NAMES
    return (
        all(_NAME_PART_RE.match(part) for part in core.split("-"))
        and any(c.islower() for c in core)
        and core.casefold().strip(".") not in excluded
        and word.casefold().strip(".") not in excluded
    )


def _first_surname(authors_text: str, *, narrative: bool) -> tuple[str | None, int]:
    """The first author of a matched name list and its offset in it,
    leading non-names dropped; (None, -1) when none is a name. In the
    narrative form a month name followed by no other name is a date
    (`July (2019)`), never an author; with co-authors, or inside a
    parenthetical citation's comma form, it is a name."""
    tokens = [m for m in _NAME_TOKEN_RE.finditer(authors_text)]
    for i, m in enumerate(tokens):
        co_authors = any(is_surname(t.group(0)) for t in tokens[i + 1:])
        if is_surname(m.group(0), month_ok=co_authors or not narrative):
            return m.group(0), m.start()
    return None, -1


# -- data ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """One occurrence of a citation in a draft."""

    file: str
    line: int
    col: int
    text: str
    kind: str  # "doi" | "author_year"
    doi: str | None = None
    surname: str | None = None
    year: int | None = None
    letter: str = ""
    section_id: str | None = None
    claim_ids: tuple[str, ...] = ()
    #: Joined to Chinese text before it (`张川川与 Simon（2020）`): the first
    #: author may be a Chinese-script name V7 does not read -- resolved only
    #: as candidates (`_CJK_JOINED_RE`).
    cjk_joined: bool = False

    @property
    def hyphen_list(self) -> tuple[str, ...]:
        """The surname read as authors joined by hyphens (`Pesaran-Shin-Smith`,
        `Matthes-Kohring`) -- or a hyphenated surname (`Campbell-Verduyn`):
        the text cannot tell. Its parts when each is a surname, else ()."""
        if self.kind != "author_year" or not self.surname or "-" not in self.surname:
            return ()
        parts = tuple(self.surname.split("-"))
        return parts if all(len(p) > 1 and is_surname(p, month_ok=True) for p in parts) else ()

    @property
    def key(self) -> str:
        """The normalised citation (its 9.6 basis): `doi:<doi>` or
        `<folded surname>|<year><letter>`."""
        if self.kind == "doi":
            return f"doi:{self.doi}"
        return f"{literature.fold_name(self.surname or '')}|{self.year}{self.letter}"

    def payload(self) -> dict[str, Any]:
        return {
            "file": self.file, "line": self.line, "col": self.col, "text": self.text, "kind": self.kind,
            "doi": self.doi, "surname": self.surname, "year": self.year, "letter": self.letter,
            "section": self.section_id, "claims": list(self.claim_ids), "key": self.key,
            "cjk_joined": self.cjk_joined,
        }


@dataclass(frozen=True)
class RefEntry:
    """One entry of a draft's reference list."""

    file: str
    n: int
    line: int
    text: str
    surname: str | None
    year: int | None
    letter: str
    doi: str | None

    @property
    def node_id(self) -> str:
        return f"ref:doi:{self.doi}" if self.doi else f"ref:entry:{self.file}#{self.n}"

    def matches(self, surname: str, year: int | None, letter: str) -> bool:
        return (
            self.surname is not None
            and literature.fold_name(self.surname) == literature.fold_name(surname)
            and self.year == year
            and self.letter == letter
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "source": "entry", "title": None, "authors": [self.surname] if self.surname else [],
            "year": self.year, "letter": self.letter or None, "venue": None, "doi": self.doi,
            "draft": self.file, "n": self.n, "line": self.line, "entry_text": self.text,
        }

    def payload(self) -> dict[str, Any]:
        return {
            "file": self.file, "n": self.n, "line": self.line, "text": self.text, "surname": self.surname,
            "year": self.year, "letter": self.letter, "doi": self.doi, "node": self.node_id,
        }


@dataclass(frozen=True)
class DraftCitations:
    file: str
    citations: tuple[Citation, ...]
    entries: tuple[RefEntry, ...]
    reference_list: bool


# -- finding citations in one line ----------------------------------------------------


def _blank(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(max(0, start), min(len(chars), end)):
            chars[i] = " "
    return "".join(chars)


def _years(years_text: str, offset: int) -> list[tuple[int, str, int]]:
    """(year, letter, column) of each year in a year list."""
    return [(int(m.group(1)), m.group(2), offset + m.start()) for m in _YEAR_TOKEN_RE.finditer(years_text)]


def find_in_line(line: str) -> list[tuple[int, str, str, dict[str, Any]]]:
    """Every citation in one line of prose: (column, kind, exact text,
    fields), DOIs first found on the raw line, author-year citations on
    the line with URLs and DOIs blanked."""
    found: list[tuple[int, str, str, dict[str, Any]]] = []
    dois = literature.find_dois(line)
    for start, end, doi in dois:
        found.append((start, "doi", line[start:end], {"doi": doi}))
    text = _blank(line, _url_spans(line) + [(s, e) for s, e, _d in dois])
    taken: list[tuple[int, int]] = []
    for m in _NARRATIVE_BRACKET_RE.finditer(text):
        window_start = max(0, m.start() - _NARRATIVE_WINDOW)
        before = text[window_start:m.start()]
        a = _NARRATIVE_AUTHORS_RE.search(before)
        if a is None:
            continue
        surname, offset = _first_surname(a.group(0), narrative=True)
        if surname is None:
            continue
        start = window_start + a.start() + offset
        exact = line[start:m.end()]
        taken.append((start, m.end()))
        joined = offset == 0 and bool(_CJK_JOINED_RE.search(text[:start]))
        years = _years(m.group("years"), m.start("years"))
        if m.group("rest"):
            for y in _REST_YEAR_RE.finditer(m.group("rest")):
                years += _years(y.group("years"), m.start("rest") + y.start("years"))
        for year, letter, col in years:
            fields = {"surname": surname, "year": year, "letter": letter}
            if joined:
                fields["cjk_joined"] = True
            found.append((col, "author_year", exact, fields))
    for g in _GROUP_RE.finditer(text):
        if any(s <= g.start() < e for s, e in taken):
            continue
        content_start = g.start(1)
        pos = content_start
        for part in re.split(r"([;；])", g.group(1)):
            if part in (";", "；"):
                pos += len(part)
                continue
            for pattern in _PART_RES:
                m = pattern.search(part)
                if m is None:
                    continue
                surname, offset = _first_surname(m.group(0)[: m.start("years") - m.start()], narrative=False)
                if surname is None:
                    break
                start = pos + m.start() + offset
                exact = line[start:pos + m.end("years")]
                joined = offset == 0 and bool(_CJK_JOINED_RE.search(part[:m.start()]))
                for year, letter, col in _years(m.group("years"), pos + m.start("years")):
                    fields = {"surname": surname, "year": year, "letter": letter}
                    if joined:
                        fields["cjk_joined"] = True
                    found.append((col, "author_year", exact, fields))
                break
            pos += len(part)
    # The same citation written twice on one line (`doi:X — https://.../X`)
    # is one occurrence, as the graph keeps it (file, line, text): the first.
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for f in sorted(found, key=lambda f: (f[0], f[1])):
        fields = f[3]
        ident = (f[1], f[2] if f[1] == "author_year" else "", fields.get("doi") or f"{fields.get('surname')}|{fields.get('year')}{fields.get('letter', '')}")
        if ident in seen:
            continue
        seen.add(ident)
        unique.append(f)
    return unique


# -- reference lists ------------------------------------------------------------------


def _is_reference_title(title: str) -> bool:
    t = re.sub(r"[*_`]", "", title).strip()
    t = _TITLE_PREFIX_RE.sub("", t).strip().rstrip(":：").strip().casefold()
    return any(t.endswith(name) for name in _REFERENCE_TITLES)


def parse_entry(file: str, n: int, line: int, text: str) -> RefEntry:
    """One reference-list entry: first author's surname (Latin script
    only), year and letter (the first year after the names; DOIs and URLs
    are not read for it), DOI."""
    clean = re.sub(r"^\s*(?:\[\d{1,3}\]\s*)?[*_]*\s*", "", text)
    dois = literature.find_dois(clean)
    surname = None
    m = re.match(rf"({_NAME})\s*(?:[,，(（]|&|\band\b|et\s*al|等)", clean)
    if m and is_surname(m.group(1), month_ok=True):  # an entry's head is never a date
        surname = m.group(1)
    scan_from = m.end(1) if m else 0
    blanked = _blank(clean, _url_spans(clean) + [(s, e) for s, e, _d in dois])
    year_m = _YEAR_TOKEN_RE.search(blanked, scan_from)
    return RefEntry(
        file=file, n=n, line=line, text=text.strip(), surname=surname,
        year=int(year_m.group(1)) if year_m else None, letter=year_m.group(2) if year_m else "",
        doi=dois[0][2] if dois else None,
    )


def _entries_from_lines(file: str, region: list[tuple[int, str]], *, tex: bool) -> tuple[list[RefEntry], list[int]]:
    """Entries of a reference-list region [(line number, text)], and the
    line numbers of the blocks that are not entries -- neither a Latin
    first author nor a DOI -- which are read as prose."""
    blocks: list[tuple[int, list[str], list[int]]] = []
    for lineno, raw in region:
        if not raw.strip():
            continue
        stripped = raw.strip()
        if stripped.startswith(">") or stripped.startswith("|") or stripped.startswith("<!--"):
            continue
        if tex:
            if re.match(r"\\(?:begin|end)\{thebibliography\}", stripped):
                continue
            item = _TEX_ITEM_RE.match(stripped)
            if item is not None:
                blocks.append((lineno, [stripped[item.end():]], [lineno]))
            elif blocks and raw[:1].isspace():
                blocks[-1][1].append(stripped)
                blocks[-1][2].append(lineno)
            else:
                blocks.append((lineno, [stripped], [lineno]))
            continue
        item = _LIST_ITEM_RE.match(raw)
        if item is not None:
            blocks.append((lineno, [item.group(1)], [lineno]))
        elif blocks and raw[:1] in (" ", "\t"):
            blocks[-1][1].append(stripped)
            blocks[-1][2].append(lineno)
        else:
            blocks.append((lineno, [stripped], [lineno]))
    entries: list[RefEntry] = []
    prose: list[int] = []
    for lineno, parts, numbers in blocks:
        entry = parse_entry(file, len(entries) + 1, lineno, " ".join(parts))
        if entry.surname is None and entry.doi is None:
            prose.extend(numbers)  # not an entry: read as prose (a review's remark under the heading)
            continue
        entries.append(entry)
    return entries, prose


def _md_reference_region(lines: list[str]) -> tuple[bool, set[int], list[tuple[int, str]]]:
    """(found, the 0-based line indexes of the list, [(line number, text)])."""
    in_region, level = False, 0
    indexes: set[int] = set()
    region: list[tuple[int, str]] = []
    found = False
    for i, line in enumerate(lines):
        heading = _MD_HEADING_RE.match(line)
        if in_region:
            if (heading and len(heading.group(1)) <= level) or _THEMATIC_BREAK_RE.match(line):
                in_region = False
            else:
                if heading is None:
                    indexes.add(i)
                    region.append((i + 1, line))
                continue
        if heading and not found and _is_reference_title(heading.group(2)):
            in_region, level, found = True, len(heading.group(1)), True
    return found, indexes, region


def _tex_reference_region(lines: list[str]) -> tuple[bool, set[int], list[tuple[int, str]]]:
    in_region, level, env = False, 99, False
    indexes: set[int] = set()
    region: list[tuple[int, str]] = []
    found = False
    for i, line in enumerate(lines):
        heading = _TEX_ANY_HEADING_RE.search(line)
        if in_region:
            ended = (env and "\\end{thebibliography}" in line) or (
                not env and heading is not None and _TEX_LEVELS[heading.group(1)] <= level
            )
            if ended:
                in_region = False
                if env:
                    indexes.add(i)
                continue
            indexes.add(i)
            region.append((i + 1, line))
            continue
        if found:
            continue
        if "\\begin{thebibliography}" in line:
            in_region, env, found = True, True, True
            indexes.add(i)
            continue
        ref = _TEX_REF_HEADING_RE.search(line)
        if ref and _is_reference_title(ref.group(1)):
            in_region, level, found = True, _TEX_LEVELS[_TEX_ANY_HEADING_RE.search(line).group(1)], True
            indexes.add(i)
    return found, indexes, region


# -- one draft --------------------------------------------------------------------------


def _claims_by_line(claims: list[claims_ingest.ParsedClaim]) -> dict[int, list[claims_ingest.ParsedClaim]]:
    out: dict[int, list[claims_ingest.ParsedClaim]] = {}
    for claim in claims:
        out.setdefault(claim.line, []).append(claim)
    return out


def _inside_identifier(line: str, raw: str) -> bool:
    """Whether every occurrence of a claim's printed number on the line is
    part of a DOI or a URL (`10.1016` of a DOI is not a claim's number):
    such a "claim" covers no sentence of the citation's."""
    spans = _url_spans(line) + [(s, e) for s, e, _d in literature.find_dois(line)]
    starts = [m.start() for m in re.finditer(re.escape(raw), line)]
    return bool(starts) and all(any(s <= p < e for s, e in spans) for p in starts)


def parse_lines(
    file: str,
    lines: list[str],
    sections: list[ParsedSection],
    *,
    tex: bool = False,
    claims: list[claims_ingest.ParsedClaim] | None = None,
    claim_lines: list[str] | None = None,
) -> DraftCitations:
    """The citations and reference list of one draft, given its lines
    (comments already stripped for LaTeX), its sections (ascending by line)
    and, optionally, its claims and the lines the claims were read from."""
    if tex:
        found, ref_indexes, region = _tex_reference_region(lines)
        prose = list(lines)
    else:
        prose = mdpaper_ingest._blank_code_fences(lines)
        found, ref_indexes, region = _md_reference_region(prose)
    entries, prose_lines = _entries_from_lines(file, region, tex=tex)
    ref_indexes = ref_indexes - {n - 1 for n in prose_lines}
    by_line = _claims_by_line(claims or [])
    citations: list[Citation] = []
    sec_idx, section_id = 0, None
    for i, line in enumerate(prose):
        lineno = i + 1
        while sec_idx < len(sections) and sections[sec_idx].line <= lineno:
            section_id = sections[sec_idx].id
            sec_idx += 1
        if i in ref_indexes or not line.strip():
            continue
        if not tex and _MD_HEADING_RE.match(line) is None and line.lstrip().startswith("<!--"):
            continue
        for col, kind, exact, fields in find_in_line(line):
            claim_ids: tuple[str, ...] = ()
            here = by_line.get(lineno)
            if here and claim_lines is not None and i < len(claim_lines):
                sentence = claims_ingest._extract_sentence(claim_lines[i], col, col + 1)
                claim_ids = tuple(sorted({
                    c.id for c in here if c.sentence == sentence and not _inside_identifier(line, c.raw)
                }))
            citations.append(Citation(
                file=file, line=lineno, col=col, text=exact, kind=kind, section_id=section_id,
                claim_ids=claim_ids, **fields,
            ))
    return DraftCitations(file, tuple(citations), tuple(entries), found)


def parse_draft(repo_root: str | Path, rel_path: str) -> DraftCitations:
    """Read and parse one draft (`.md` or `.tex`); raises OSError (a
    `cloud.CloudOnlyError` too) when it cannot be read."""
    root = Path(repo_root)
    text = cloud.read_text(root / rel_path)
    raw = text.splitlines()
    if rel_path.lower().endswith(".tex"):
        stripped = [_strip_comment(line) for line in raw]
        sections = parse_tex_file(root, rel_path).sections
        claims = claims_ingest.parse_tex_claims(root, rel_path)
        claim_lines = claims_ingest._blank_skip_regions([claims_ingest._blank_command_args(l) for l in stripped])
        return parse_lines(rel_path, stripped, sections, tex=True, claims=claims, claim_lines=claim_lines)
    sections = mdpaper_ingest.parse_md_file(root, rel_path).sections
    claims = mdpaper_ingest.parse_md_claims(root, rel_path)
    claim_lines = mdpaper_ingest._blank_md_table_rows(mdpaper_ingest._blank_code_fences(raw))
    return parse_lines(rel_path, raw, sections, claims=claims, claim_lines=claim_lines)


def drafts_of(inventory: Mapping[str, Iterable[str]]) -> list[str]:
    """The drafts of an inventory: paper Markdown and LaTeX files."""
    md = [p for p in inventory.get("md", []) if mdpaper_ingest._is_paper_markdown(p)]
    return sorted(set(md) | set(inventory.get("tex", [])))


# -- resolution (11.4 steps 1-4) -----------------------------------------------------------


def _names_in(text: str) -> set[str]:
    return {literature.fold_name(m.group(0)) for m in _NAME_TOKEN_RE.finditer(text)}


def resolve_in_draft(citation: Citation, entries: Iterable[RefEntry]) -> list[RefEntry]:
    """Step 1: the entries of the same draft with the citation's first
    surname, year and letter. One is the entry; several are candidates.
    A hyphenated surname that matches no entry as written is read as a
    hyphen-joined author list (`Matthes-Kohring (2008)`): the entries whose
    first author is its first part and which name every other part too."""
    if citation.kind != "author_year" or citation.surname is None:
        return []
    entries = list(entries)
    exact = [e for e in entries if e.matches(citation.surname, citation.year, citation.letter)]
    parts = citation.hyphen_list
    if exact or not parts:
        return exact
    others = {literature.fold_name(p) for p in parts[1:]}
    return [
        e for e in entries
        if e.matches(parts[0], citation.year, citation.letter) and others <= _names_in(e.text)
    ]


def zotero_item_for_doi(doi: str, library: literature.ZoteroLibrary) -> literature.ZoteroItem | None:
    """Step 2: the Zotero item carrying this DOI."""
    return library.by_doi(doi)


def zotero_candidates(citation: Citation, library: literature.ZoteroLibrary) -> list[literature.ZoteroItem]:
    """Step 4: Zotero items whose first creator's surname and year match an
    author-year citation (the year's letter is not in Zotero)."""
    if citation.kind != "author_year" or citation.surname is None or citation.year is None:
        return []
    exact = library.by_surname_year(citation.surname, citation.year)
    parts = citation.hyphen_list
    if exact or not parts:
        return exact
    others = {literature.fold_name(p) for p in parts[1:]}
    return [
        i for i in library.by_surname_year(parts[0], citation.year)
        if others <= {literature.fold_name(c) for c in i.creators}
    ]


def zotero_node_id(item: literature.ZoteroItem) -> str:
    return f"ref:doi:{item.doi}" if item.doi else f"ref:zotero:{item.key}"


AUTO = "auto"
PENDING = "pending"

HOW_DOI = "doi"
HOW_ENTRY = "entry"
HOW_ENTRY_CANDIDATES = "entry_candidates"
HOW_ZOTERO_CANDIDATES = "zotero_candidates"
HOW_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Target:
    node_id: str
    via: str  # doi | entry | entry_candidate | zotero_candidate
    entry: RefEntry | None = None
    zotero: literature.ZoteroItem | None = None


@dataclass(frozen=True)
class Resolution:
    citation: Citation
    status: str | None
    how: str
    targets: tuple[Target, ...] = ()


def resolve(draft: DraftCitations, library: literature.ZoteroLibrary) -> list[Resolution]:
    """Steps 1, 2 and 4 for every citation of one draft (step 3 only
    describes a DOI's reference; it changes no link)."""
    out: list[Resolution] = []
    for c in draft.citations:
        if c.kind == "doi":
            assert c.doi is not None
            out.append(Resolution(c, AUTO, HOW_DOI, (Target(f"ref:doi:{c.doi}", "doi", zotero=zotero_item_for_doi(c.doi, library)),)))
            continue
        matched = resolve_in_draft(c, draft.entries)
        if len(matched) == 1 and not c.cjk_joined:
            e = matched[0]
            item = zotero_item_for_doi(e.doi, library) if e.doi else None
            out.append(Resolution(c, AUTO, HOW_ENTRY, (Target(e.node_id, "entry", entry=e, zotero=item),)))
            continue
        if matched:
            out.append(Resolution(c, PENDING, HOW_ENTRY_CANDIDATES, tuple(
                Target(e.node_id, "entry_candidate", entry=e, zotero=zotero_item_for_doi(e.doi, library) if e.doi else None)
                for e in matched
            )))
            continue
        items = zotero_candidates(c, library)
        if items:
            out.append(Resolution(c, PENDING, HOW_ZOTERO_CANDIDATES, tuple(
                Target(zotero_node_id(i), "zotero_candidate", zotero=i) for i in items
            )))
            continue
        out.append(Resolution(c, None, HOW_UNRESOLVED))
    return out


def dois_to_describe(resolutions: Iterable[Resolution]) -> set[str]:
    """The DOIs of resolved references Zotero does not describe: what the
    online lookup (step 3) may be asked about."""
    out: set[str] = set()
    for r in resolutions:
        for t in r.targets:
            if t.node_id.startswith("ref:doi:") and t.zotero is None:
                out.add(t.node_id[len("ref:doi:"):])
    return out


def reference_nodes(
    resolutions: Iterable[Resolution], online: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """{node id: attrs} of every reference a resolution points to. A DOI's
    reference is described by Zotero, else the online answer, else the
    first entry (in draft and entry order) carrying it, else by its DOI
    alone (`source` None)."""
    entries_by_doi: dict[str, RefEntry] = {}
    nodes: dict[str, dict[str, Any]] = {}
    targets = [t for r in resolutions for t in r.targets]
    for t in targets:
        if t.entry is not None and t.entry.doi:
            prior = entries_by_doi.get(t.entry.doi)
            if prior is None or (t.entry.file, t.entry.n) < (prior.file, prior.n):
                entries_by_doi[t.entry.doi] = t.entry
    for t in targets:
        if t.node_id in nodes:
            continue
        if t.zotero is not None:
            attrs = t.zotero.metadata()
        elif t.node_id.startswith("ref:doi:"):
            doi = t.node_id[len("ref:doi:"):]
            meta = online.get(doi)
            if meta is not None:
                attrs = {
                    "source": "crossref", "title": meta.get("title"), "authors": list(meta.get("authors") or []),
                    "year": meta.get("year"), "venue": meta.get("venue"), "fetched": meta.get("fetched"),
                }
            elif doi in entries_by_doi:
                attrs = entries_by_doi[doi].metadata()
            else:
                attrs = {"source": None, "title": None, "authors": [], "year": None, "venue": None}
            attrs["doi"] = doi
        else:
            assert t.entry is not None
            attrs = t.entry.metadata()
        nodes[t.node_id] = attrs
    return nodes


def node_title(node_id: str, attrs: Mapping[str, Any]) -> str:
    if attrs.get("title"):
        return str(attrs["title"])
    if attrs.get("entry_text"):
        text = str(attrs["entry_text"])
        return text if len(text) <= 120 else text[:119] + "…"
    return node_id.split(":", 2)[-1]


# -- the graph ------------------------------------------------------------------------------


def entry_basis_text(entry: RefEntry) -> str:
    """A reference-list entry as its link's basis holds it: the entry's
    text, whitespace collapsed (9.6: nothing positional -- an entry
    inserted above it changes which entry `#n` names, and the basis says so)."""
    return " ".join(entry.text.split())


@dataclass
class _Edge:
    status: str = PENDING
    cited: set[str] = field(default_factory=set)
    entry_dois: set[str] = field(default_factory=set)
    entries: set[str] = field(default_factory=set)
    occurrences: list[dict[str, Any]] = field(default_factory=list)
    hows: set[str] = field(default_factory=set)
    candidate_count: int = 0
    sources: set[str] = field(default_factory=set)

    @property
    def source(self) -> str:
        plain = sorted(s for s in self.sources if scan_mod.SOURCE_SEPARATOR not in s)
        return plain[0] if plain else sorted(self.sources)[0]


def _edges_of(resolutions: Iterable[Resolution], existing: Callable[[str], bool]) -> tuple[dict[tuple[str, str], _Edge], int]:
    edges: dict[tuple[str, str], _Edge] = {}
    unanchored = 0
    for r in resolutions:
        if r.status is None:
            continue
        c = r.citation
        froms = [s for s in ([c.section_id] if c.section_id else []) + list(c.claim_ids) if existing(s)]
        if not froms:
            unanchored += 1
            logger.info("%s:%d: citation %r has no section or claim to link from", c.file, c.line, c.text)
            continue
        for src in froms:
            for t in r.targets:
                e = edges.setdefault((src, t.node_id), _Edge())
                if r.status == AUTO:
                    e.status = AUTO
                e.cited.add(c.key)
                if t.entry is not None and t.entry.doi:
                    e.entry_dois.add(t.entry.doi)
                if t.entry is not None and t.node_id.startswith("ref:entry:"):
                    # The id `ref:entry:<draft>#<n>` is a position: the
                    # entry's text makes the basis say WHICH entry it was.
                    e.entries.add(entry_basis_text(t.entry))
                occurrence = {"file": c.file, "line": c.line, "text": c.text}
                if occurrence not in e.occurrences:
                    e.occurrences.append(occurrence)
                e.hows.add(r.how)
                e.candidate_count = max(e.candidate_count, len(r.targets) if r.status == PENDING else 1)
                e.sources.add(
                    scan_mod.citations_zotero_source(c.file) if t.via == "zotero_candidate" else c.file
                )
    return edges, unanchored


def ingest_citations_repo(
    conn: Connection,
    repo_root: str | Path,
    draft_paths: Iterable[str],
    *,
    scan: scan_mod.Scan | None = None,
    library: literature.ZoteroLibrary | None = None,
    lookup: Callable[[Iterable[str]], dict[str, dict[str, Any]]] | None = None,
    complete: bool = False,
) -> dict[str, int]:
    """Write the citations of `draft_paths` into the graph (module
    docstring). Must run after the Markdown, LaTeX and claims extractors of
    the same scan (its links start at their sections and claims).
    `complete=True` says `draft_paths` are all the project's drafts, so the
    links of a draft no longer among them are removed too. `library`
    defaults to the researcher's Zotero library, `lookup` to
    `rce.literature.lookup_dois` (online only when the setting is on)."""
    with scan_mod.own_scan(conn, scan, EXTRACTOR) as sc:
        return _ingest(conn, Path(repo_root), sorted(set(draft_paths)), sc, library, lookup, complete)


def _ingest(
    conn: Connection, root: Path, drafts: list[str], sc: scan_mod.Scan,
    library: literature.ZoteroLibrary | None, lookup: Callable[[Iterable[str]], dict[str, dict[str, Any]]] | None,
    complete: bool,
) -> dict[str, int]:
    sc.ran(EXTRACTOR)
    library = literature.load_library() if library is None else library
    if not library.readable:
        logger.warning("Zotero library not read: %s", library.reason)
    zotero_status = scan_mod.READ_AND_PARSED if library.readable else scan_mod.UNREADABLE
    parsed: list[DraftCitations] = []
    read_ok: set[str] = set()
    counts = {
        "drafts": 0, "citations": 0, "dois": 0, "author_year": 0, "entries": 0, "resolved": 0, "pending": 0,
        "unresolved": 0, "unanchored": 0, "edges": 0, "references": 0, "edges_removed": 0, "references_removed": 0,
    }
    for rel in drafts:
        try:
            draft = parse_draft(root, rel)
        except OSError as exc:
            logger.warning("cannot read draft %s: %s", rel, exc)
            sc.source(EXTRACTOR, rel, scan_mod.UNREADABLE)
            sc.source(EXTRACTOR, scan_mod.citations_zotero_source(rel), scan_mod.UNREADABLE)
            continue
        parsed.append(draft)
        read_ok.add(rel)
    resolutions = [r for d in parsed for r in resolve(d, library)]
    online = (lookup or literature.lookup_dois)(sorted(dois_to_describe(resolutions)))
    nodes = reference_nodes(resolutions, online)
    edges, unanchored = _edges_of(resolutions, lambda node_id: db.get_node(conn, node_id) is not None)

    for d in parsed:
        counts["drafts"] += 1
        counts["entries"] += len(d.entries)
    for r in resolutions:
        counts["citations"] += 1
        counts["dois" if r.citation.kind == "doi" else "author_year"] += 1
        counts["resolved" if r.status == AUTO else "pending" if r.status == PENDING else "unresolved"] += 1
    counts["unanchored"] = unanchored

    linked = {dst for (_src, dst) in edges}
    for node_id in sorted(linked):
        attrs = nodes[node_id]
        db.upsert_node(conn, node_id, "reference", title=node_title(node_id, attrs), attrs=attrs)
        counts["references"] += 1
    for (src, dst), e in sorted(edges.items()):
        for source in sorted(e.sources):
            sc.node(dst, EXTRACTOR, source)
        basis = scan_mod.basis(EXTRACTOR, "cites", cited=e.cited, entry_dois=e.entry_dois, entries=e.entries)
        source = e.source
        source_ok = zotero_status if scan_mod.SOURCE_SEPARATOR in source else scan_mod.READ_AND_PARSED
        mark = sc.mark(EXTRACTOR, source, basis) if source_ok == scan_mod.READ_AND_PARSED else {}
        for occurrence in e.occurrences:
            db.upsert_edge(
                conn, src, dst, "cites", extractor=EXTRACTOR, evidence=occurrence, confidence=1.0,
                status=e.status,
                edge_attrs={"resolved_by": sorted(e.hows), "candidate_count": e.candidate_count},
                **mark,
            )
        counts["edges"] += 1

    for rel in read_ok:
        sc.source(EXTRACTOR, rel, scan_mod.READ_AND_PARSED)
        sc.source(EXTRACTOR, scan_mod.citations_zotero_source(rel), zotero_status)
    removed_edges, removed_nodes = _remove_stale(
        conn, set(edges), read_ok, set(drafts) if complete else None, library.readable,
    )
    counts["edges_removed"], counts["references_removed"] = removed_edges, removed_nodes
    return counts


_REF_PREFIXES = ("ref:doi:", "ref:zotero:", "ref:entry:")


def _remove_stale(
    conn: Connection, produced: set[tuple[str, str]], read_ok: set[str], all_drafts: set[str] | None,
    zotero_read: bool,
) -> tuple[int, int]:
    """Remove the citations links a scan that read their source did not
    produce (and those of a draft no longer in the project, when the
    caller listed them all), then the references nothing links to."""
    removed = 0
    rows = conn.execute(
        "SELECT src, dst, scan_source FROM edges WHERE extractor = ? AND type = 'cites'", (EXTRACTOR,),
    ).fetchall()
    for src, dst, source in rows:
        if (src, dst) in produced:
            continue
        file = scan_mod.file_of(source) if source else scan_mod.node_file(src)
        if file is None:
            continue
        gone = all_drafts is not None and file not in all_drafts
        observed = file in read_ok and (source is None or scan_mod.SOURCE_SEPARATOR not in source or zotero_read)
        if gone or observed:
            removed += db.delete_edge(conn, src, dst, "cites", EXTRACTOR)
    nodes_removed = 0
    for prefix in _REF_PREFIXES:
        for (node_id,) in conn.execute(
            "SELECT id FROM nodes WHERE type = 'reference' AND id LIKE ? AND NOT EXISTS "
            "(SELECT 1 FROM edges WHERE edges.src = nodes.id OR edges.dst = nodes.id)",
            (prefix + "%",),
        ).fetchall():
            try:
                db.delete_node(conn, node_id)
            except sqlite3.IntegrityError:
                continue
            nodes_removed += 1
    return removed, nodes_removed


# -- the report (rce citations; the 文献 view reads the same) ---------------------------------


def report(
    repo_root: str | Path,
    draft_paths: Iterable[str],
    *,
    library: literature.ZoteroLibrary | None = None,
    online: Mapping[str, Mapping[str, Any]] | None = None,
    statuses: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    """What the drafts cite and how each citation resolves -- read only:
    nothing is written and the network is not asked (`online` defaults to
    the cached answers). `statuses` ({(src, dst): status} of the graph's
    `citations` links) lets a confirmed or rejected candidate show as
    such."""
    root = Path(repo_root)
    library = literature.load_library() if library is None else library
    drafts_out: list[dict[str, Any]] = []
    totals = {"citations": 0, "resolved": 0, "pending": 0, "unresolved": 0, "unanchored": 0, "unreadable": 0}
    all_res: list[Resolution] = []
    per_draft: list[tuple[str, DraftCitations | None, list[Resolution]]] = []
    unreadable: dict[str, OSError] = {}
    for rel in sorted(set(draft_paths)):
        try:
            d = parse_draft(root, rel)
        except OSError as exc:
            per_draft.append((rel, None, []))
            unreadable[rel] = exc
            logger.info("cannot read draft %s: %s", rel, exc)
            continue
        res = resolve(d, library)
        all_res.extend(res)
        per_draft.append((rel, d, res))
    if online is None:
        online = {doi: meta for doi in dois_to_describe(all_res) if (meta := literature.cached(doi)) is not None}
    nodes = reference_nodes(all_res, online)
    for rel, d, res in per_draft:
        if d is None:
            totals["unreadable"] += 1
            exc = unreadable[rel]
            drafts_out.append({
                "file": rel, "readable": False, "citations": [], "entries": 0, "counts": {},
                # 11.1: a draft only in a synced folder's cloud, its client not
                # running, says so in the provider's sentence.
                "message_zh": exc.message if isinstance(exc, cloud.CloudOnlyError) else None,
                "detail": str(exc),
            })
            continue
        items = []
        c_counts = {"citations": 0, "resolved": 0, "pending": 0, "unresolved": 0, "unanchored": 0}
        for r in res:
            state = _state_of(r, statuses)
            c_counts["citations"] += 1
            c_counts[state] += 1
            items.append({
                **r.citation.payload(), "how": r.how, "status": r.status, "state": state,
                "targets": [{"node": t.node_id, "via": t.via, **nodes.get(t.node_id, {})} for t in r.targets],
            })
        for k, v in c_counts.items():
            totals[k] += v
        drafts_out.append({
            "file": rel, "readable": True, "reference_list": d.reference_list, "entries": len(d.entries),
            "citations": items, "counts": c_counts,
        })
    drafts_out.sort(key=lambda x: (-(x["counts"].get("unresolved", 0) + x["counts"].get("pending", 0)), x["file"]))
    cloud_messages = sorted({e.message for e in unreadable.values() if isinstance(e, cloud.CloudOnlyError)})
    return {
        "drafts": drafts_out, "totals": totals, "cloud": cloud_messages,
        "zotero": {"status": library.status, "data_dir": str(library.data_dir) if library.data_dir else None,
                   "items": len(library.items), "reason": library.reason},
        "lookup": {"enabled": literature.doi_lookup_enabled()},
    }


def _state_of(r: Resolution, statuses: Mapping[tuple[str, str], str] | None) -> str:
    """resolved | pending | unresolved | unanchored for one citation, a
    researcher's confirmation of one candidate making it resolved (and every
    candidate rejected, unresolved). `unanchored`: before the draft's first
    heading, in no section and no claim -- the graph links nothing from it
    (`_edges_of`), so it is neither resolved nor waiting for a decision."""
    if not r.citation.section_id and not r.citation.claim_ids:
        return "unanchored"
    if r.status == AUTO:
        return "resolved"
    if r.status is None:
        return "unresolved"
    if statuses:
        c = r.citation
        froms = ([c.section_id] if c.section_id else []) + list(c.claim_ids)
        found = [statuses.get((f, t.node_id)) for f in froms for t in r.targets]
        if "confirmed" in found:
            return "resolved"
        if found and all(s == "rejected" for s in found):
            return "unresolved"
    return "pending"
