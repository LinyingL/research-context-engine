"""The literature a project's drafts cite (DESIGN.md 11.4): DOIs, the
researcher's Zotero library, and the opt-in DOI lookup online. Read-only
toward everything that is not RCE's own.

**DOIs.** `normalize_doi` is 11.4's rule: lower case; trailing
punctuation removed, half- and full-width (`.,;:)）。，；`, backticks,
markdown brackets and emphasis); a closing parenthesis only when it does
not close one the DOI opened (`10.1016/s0022-1996(00)00067-x` keeps its
own). `find_dois` finds them in text -- a DOI inside a URL
(`https://doi.org/10.x/y`) is one DOI, found once; it stops at whitespace,
at CJK and full-width characters, at `|` (a table cell), and at `?` / `#`
(a URL's query or fragment).

**Zotero.** `zotero_data_dir` reads the data directory from the Zotero
profile's `prefs.js` (`extensions.zotero.dataDir`, the default profile of
`profiles.ini`), else `~/Zotero`; `RCE_ZOTERO_DATA_DIR` overrides both
(the test suite points it at an empty folder: no test reads a real
library). `load_library` opens `zotero.sqlite` with `mode=ro&immutable=1`
-- SQLite then writes nothing, not even a lock or a journal -- and, if
that fails because the database is locked, reads a copy in a temporary
folder that is removed afterwards. It never writes the library. A data
directory that does not exist, or has no `zotero.sqlite`, is an empty
library (`absent`), an observation; a database that exists and cannot be
read (only in the cloud, corrupt, an unknown schema) is `unreadable`, and
a scan then speaks for nothing Zotero would have said.

An item is mapped to: its key, DOI field (normalised), creators (last
names, in order; the first author when there are authors, else the first
creator), year (from the `date` field), title, venue (publication, book,
proceedings, website, university or publisher), and its imported
attachments -- `storage:<file>` paths, which live in
`<data dir>/storage/<attachment key>/<file>`. Items in the trash, notes,
attachments and annotations, and feed items are not items of the library.

**DOI online.** Off unless the researcher turns it on (`settings.json`
under the RCE home, `doi_online_lookup`; 「用 DOI 联网查文献信息」 /
`rce citations lookup --on`). Only then does `lookup_dois` ask
`https://api.crossref.org/works/<doi>` -- the DOI in the URL and nothing
else: no email, no user agent naming anyone, no draft text. Answers are
cached under the RCE home with the date (`doi-cache.json`); a cached DOI
is never asked again. A failure (no network, a timeout, a 404, an answer
that does not parse) is not cached: it is asked again on a later scan,
never recorded as "not found". After a failure of the network itself the
remaining DOIs of that scan are not asked (each would wait out its own
timeout). `FETCH` is the one function that touches the network; tests
replace it with a recording stand-in.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from rce import paths

logger = logging.getLogger(__name__)

# -- DOIs ------------------------------------------------------------------------

# A DOI starts "10.<4-9 digits>/" (or, inside a URL's query, `/` written
# `%2F`) not glued to a longer number or a Latin word -- a CJK character
# right before it is prose (`DOI为10.x/y`); its suffix runs to whitespace,
# a CJK / full-width character, a table bar, a quote, an angle, square or
# curly bracket, a backslash (LaTeX: `\url{..}`, `\doi{..}`, `\\`), or a
# URL's query/fragment.
_DOI_STOP = "\\s\"'<>|?#\\[\\]{}\\\\\u2018-\u201f\u3000-\u303f\u3400-\u9fff\uff00-\uffef"
_DOI_BEFORE = "(?<![0-9A-Za-z_.\u00c0-\u024f\u1e00-\u1eff])"
DOI_RE = re.compile(rf"{_DOI_BEFORE}10\.\d{{4,9}}(?:/|%2[Ff])[^{_DOI_STOP}]+", re.IGNORECASE)

#: Trailing characters a DOI never ends in as written in prose (11.4).
_TRAILING = ".,;:)）。，；：`]*_!"
#: A trailing parenthetical of letters only -- a remark after the DOI
#: (`10.2307/2390654(JSTOR)`), never part of one (a DOI's own brackets
#: hold digits or sit inside it: `10.1016/s0022-1996(00)00067-x`).
_REMARK_RE = re.compile(r"\([A-Za-z][A-Za-z .&-]*\)$")
#: Oxford Academic article URLs carry the article's number after the DOI
#: (`academic.oup.com/<journal>/article/doi/10.1093/<journal>/<id>/<number>`).
_OUP_URL_RE = re.compile(r"academic\.oup\.com/\S*$", re.IGNORECASE)
_OUP_DOI_RE = re.compile(r"(10\.1093/[^/]+/[^/]+)/\d+$")


def _trim(raw: str) -> str:
    """`raw` with the trailing punctuation and remarks 11.4 removes (case
    kept, so its length is where the DOI as written ends)."""
    doi = raw.strip()
    while doi:
        if doi[-1] in _TRAILING:
            if doi[-1] == ")" and doi.count("(") >= doi.count(")"):
                remark = _REMARK_RE.search(doi)
                if remark is None or "/" not in doi[:remark.start()]:
                    break
                doi = doi[:remark.start()]
                continue
            doi = doi[:-1]
            continue
        break
    return doi


def normalize_doi(raw: str) -> str:
    """11.4's normal form: lower case, trailing punctuation removed; a
    closing parenthesis is kept only when the DOI opened it, and then not
    when it closes a remark of letters only (`(JSTOR)`); a `%2F` written
    for the `/` is decoded."""
    doi = _trim(raw).lower()
    if re.match(r"10\.\d{4,9}%2f", doi):
        doi = urllib.parse.unquote(doi)
    return doi


def _balanced(raw: str) -> str:
    """`raw` up to the first closing parenthesis it did not open (a DOI
    keeps its own `(00)`; `10.x/y)(SSRN` ends at `y`)."""
    depth = 0
    for i, ch in enumerate(raw):
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return raw[:i]
            depth -= 1
    return raw


_MD_LINK_RE = re.compile(r"\[([^\[\]]*)\]\(([^()\s]*(?:\([^()\s]*\)[^()\s]*)*)\)")


def find_dois(text: str) -> list[tuple[int, int, str]]:
    """(start, end, normalised DOI) of every DOI in `text`; `end` is where
    the DOI as written ends, trailing punctuation excluded. A Markdown link
    whose text and target carry the same DOI (`[10.x/y](https://doi.org/
    10.x/y)`) is one DOI, found once (the text's). A DOI written `%2F`
    (a URL's query) ends at the query's next `&`; one in an Oxford Academic
    article URL leaves out the article number the URL adds."""
    found = []
    for m in DOI_RE.finditer(text):
        raw = _balanced(m.group(0))
        if "%" in raw[:16] and re.match(r"10\.\d{4,9}%2[Ff]", raw):
            raw = raw.split("&", 1)[0]
        written = _trim(raw)
        doi = normalize_doi(written)
        if "/" not in doi or doi.endswith("/"):
            continue
        token = re.split(r"\s", text[max(0, m.start() - 300):m.start()])[-1]
        if _OUP_URL_RE.search(token):
            oup = _OUP_DOI_RE.match(doi)
            if oup:
                doi = oup.group(1)
                written = written[:len(doi)]  # ASCII: as long as the DOI
        found.append((m.start(), m.start() + len(written), doi))
    repeated: set[int] = set()
    for link in _MD_LINK_RE.finditer(text):
        in_text = {d for s, _e, d in found if link.start(1) <= s < link.end(1)}
        repeated |= {s for s, _e, d in found if link.start(2) <= s < link.end(2) and d in in_text}
    return [f for f in found if f[0] not in repeated]


# -- names -----------------------------------------------------------------------


def fold_name(name: str) -> str:
    """A surname for comparison: case- and diacritics-insensitive
    (`Söderlind` == `soderlind`, `Chiţu` == `chitu`)."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


# -- Zotero ----------------------------------------------------------------------

DATA_DIR_ENV_VAR = "RCE_ZOTERO_DATA_DIR"
SQLITE_NAME = "zotero.sqlite"

LIBRARY_READ = "read"
LIBRARY_ABSENT = "absent"
LIBRARY_UNREADABLE = "unreadable"

_PREF_RE = re.compile(r'user_pref\(\s*"([^"]+)"\s*,\s*(.+?)\s*\)\s*;')
_VENUE_FIELDS = ("publicationTitle", "bookTitle", "proceedingsTitle", "websiteTitle", "university", "publisher")
_NOT_ITEMS = ("attachment", "note", "annotation")


def _profile_roots(home: Path) -> list[Path]:
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Zotero"]
    return [home / ".zotero" / "zotero"]


def _default_profile(root: Path) -> Path | None:
    """The default profile of `profiles.ini` under `root`, or the only
    profile folder there is; None when it cannot be told."""
    ini = root / "profiles.ini"
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    sections: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            sections.append({})
        elif "=" in line and sections:
            k, _, v = line.partition("=")
            sections[-1][k.strip()] = v.strip()
    profiles = [s for s in sections if "Path" in s]
    chosen = [s for s in profiles if s.get("Default") == "1"] or (profiles if len(profiles) == 1 else [])
    if chosen:
        s = chosen[0]
        return (root / s["Path"]) if s.get("IsRelative", "1") == "1" else Path(s["Path"])
    try:
        candidates = sorted(p for p in (root / "Profiles").iterdir() if p.is_dir())
    except OSError:
        return None
    return candidates[0] if len(candidates) == 1 else None


def read_prefs(prefs_js: Path) -> dict[str, Any]:
    """The `user_pref(...)` values of a Firefox-style `prefs.js`."""
    try:
        text = prefs_js.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    prefs: dict[str, Any] = {}
    for name, raw in _PREF_RE.findall(text):
        try:
            prefs[name] = json.loads(raw)
        except ValueError:
            continue
    return prefs


def zotero_data_dir(*, home: Path | None = None) -> Path:
    """Where the researcher's Zotero library lives (module docstring)."""
    override = os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    home = Path.home() if home is None else Path(home)
    for root in _profile_roots(home):
        profile = _default_profile(root)
        if profile is None:
            continue
        prefs = read_prefs(profile / "prefs.js")
        data_dir = prefs.get("extensions.zotero.dataDir")
        if isinstance(data_dir, str) and data_dir and prefs.get("extensions.zotero.useDataDir", True) is not False:
            return Path(data_dir).expanduser()
    return home / "Zotero"


@dataclass(frozen=True)
class ZoteroAttachment:
    key: str
    filename: str
    content_type: str | None
    available: bool

    def payload(self) -> dict[str, Any]:
        return {"key": self.key, "file": self.filename, "content_type": self.content_type, "available": self.available}


@dataclass(frozen=True)
class ZoteroItem:
    key: str
    item_type: str
    title: str | None
    creators: tuple[str, ...]
    first_creator: str | None
    year: int | None
    venue: str | None
    doi: str | None
    attachments: tuple[ZoteroAttachment, ...] = ()

    def metadata(self) -> dict[str, Any]:
        """A reference node's attrs (11.4): what the library says."""
        return {
            "source": "zotero",
            "title": self.title,
            "authors": list(self.creators),
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "zotero_key": self.key,
            "attachments": [a.payload() for a in self.attachments],
        }


@dataclass
class ZoteroLibrary:
    data_dir: Path | None
    status: str
    items: list[ZoteroItem] = field(default_factory=list)
    reason: str | None = None
    from_copy: bool = False

    def __post_init__(self) -> None:
        self._by_doi: dict[str, ZoteroItem] = {}
        self._by_name_year: dict[tuple[str, int], list[ZoteroItem]] = {}
        for item in sorted(self.items, key=lambda i: i.key):
            if item.doi and item.doi not in self._by_doi:
                self._by_doi[item.doi] = item
            if item.first_creator and item.year is not None:
                self._by_name_year.setdefault((fold_name(item.first_creator), item.year), []).append(item)

    @property
    def readable(self) -> bool:
        return self.status != LIBRARY_UNREADABLE

    def by_doi(self, doi: str) -> ZoteroItem | None:
        return self._by_doi.get(doi)

    def by_surname_year(self, surname: str, year: int) -> list[ZoteroItem]:
        return list(self._by_name_year.get((fold_name(surname), year), []))

    def item(self, key: str) -> ZoteroItem | None:
        for item in self.items:
            if item.key == key:
                return item
        return None


def _year_of(date_field: str | None) -> int | None:
    if not date_field:
        return None
    head = date_field.strip()[:4]
    if head.isdigit() and head != "0000":
        return int(head)
    return None


def _read_items(conn: sqlite3.Connection, data_dir: Path) -> list[ZoteroItem]:
    deleted = "SELECT itemID FROM deletedItems"
    placeholders = ", ".join("?" for _ in _NOT_ITEMS)
    rows = conn.execute(
        f"""
        SELECT i.itemID, i.key, t.typeName FROM items i
        JOIN itemTypes t ON t.itemTypeID = i.itemTypeID
        LEFT JOIN libraries l ON l.libraryID = i.libraryID
        WHERE t.typeName NOT IN ({placeholders}) AND i.itemID NOT IN ({deleted})
          AND (l.type IS NULL OR l.type != 'feed')
        """,
        _NOT_ITEMS,
    ).fetchall()
    fields: dict[int, dict[str, str]] = {}
    for item_id, name, value in conn.execute(
        """
        SELECT d.itemID, f.fieldName, v.value FROM itemData d
        JOIN fields f ON f.fieldID = d.fieldID JOIN itemDataValues v ON v.valueID = d.valueID
        """
    ):
        fields.setdefault(item_id, {})[name] = value if isinstance(value, str) else str(value)
    creators: dict[int, list[tuple[int, str, str]]] = {}
    for item_id, last, first, ctype, order in conn.execute(
        """
        SELECT ic.itemID, c.lastName, c.firstName, ct.creatorType, ic.orderIndex FROM itemCreators ic
        JOIN creators c ON c.creatorID = ic.creatorID JOIN creatorTypes ct ON ct.creatorTypeID = ic.creatorTypeID
        ORDER BY ic.itemID, ic.orderIndex
        """
    ):
        name = (last or first or "").strip()
        if name:
            creators.setdefault(item_id, []).append((order, name, ctype))
    attachments: dict[int, list[ZoteroAttachment]] = {}
    for parent, key, link_mode, content_type, path in conn.execute(
        f"""
        SELECT ia.parentItemID, i.key, ia.linkMode, ia.contentType, ia.path FROM itemAttachments ia
        JOIN items i ON i.itemID = ia.itemID
        WHERE ia.parentItemID IS NOT NULL AND ia.itemID NOT IN ({deleted})
        ORDER BY i.key
        """
    ):
        filename = storage_filename(path, link_mode)
        if filename is None or not isinstance(key, str):
            continue
        target = data_dir / "storage" / key / filename
        available = target.is_file() and not paths.is_dataless(target)
        attachments.setdefault(parent, []).append(ZoteroAttachment(key, filename, content_type, available))
    items = []
    for item_id, key, type_name in rows:
        f = fields.get(item_id, {})
        people = creators.get(item_id, [])
        authors = [name for _o, name, ctype in people if ctype == "author"]
        first = authors[0] if authors else (people[0][1] if people else None)
        raw_doi = f.get("DOI")
        dois = find_dois(raw_doi) if raw_doi else []
        venue = next((f[k] for k in _VENUE_FIELDS if f.get(k)), None)
        items.append(ZoteroItem(
            key=key, item_type=type_name, title=f.get("title"),
            creators=tuple(name for _o, name, _t in people), first_creator=first,
            year=_year_of(f.get("date")), venue=venue, doi=dois[0][2] if dois else None,
            attachments=tuple(attachments.get(item_id, [])),
        ))
    return sorted(items, key=lambda i: i.key)


def storage_filename(path: str | None, link_mode: int | None) -> str | None:
    """The file name of an imported attachment (`storage:<name>`, link
    modes 0 and 1), or None: a linked file or URL is not in Zotero's
    storage, and a name that is not a plain file name is never used."""
    if link_mode not in (0, 1) or not isinstance(path, str) or not path.startswith("storage:"):
        return None
    name = path[len("storage:"):]
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        return None
    return name


def _uri(path: Path) -> str:
    return f"file:{urllib.parse.quote(str(path))}?mode=ro&immutable=1"


def _query_library(db_file: Path, data_dir: Path) -> list[ZoteroItem]:
    conn = sqlite3.connect(_uri(db_file), uri=True)
    try:
        return _read_items(conn, data_dir)
    finally:
        conn.close()


def load_library(data_dir: str | Path | None = None) -> ZoteroLibrary:
    """Read the Zotero library at `data_dir` (default: `zotero_data_dir()`),
    never writing it (module docstring)."""
    data_dir = zotero_data_dir() if data_dir is None else Path(data_dir)
    db_file = data_dir / SQLITE_NAME
    if not db_file.is_file():
        return ZoteroLibrary(data_dir, LIBRARY_ABSENT, reason=f"no {SQLITE_NAME} in {data_dir}")
    if paths.is_dataless(db_file):
        return ZoteroLibrary(data_dir, LIBRARY_UNREADABLE, reason=f"{db_file} is only in the cloud; not read")
    try:
        return ZoteroLibrary(data_dir, LIBRARY_READ, _query_library(db_file, data_dir))
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            return ZoteroLibrary(data_dir, LIBRARY_UNREADABLE, reason=f"{db_file}: {exc}")
        first_error = exc
    except sqlite3.DatabaseError as exc:
        return ZoteroLibrary(data_dir, LIBRARY_UNREADABLE, reason=f"{db_file}: {exc}")
    # Zotero is running and holds its lock: read a copy (11.4).
    tmp = Path(tempfile.mkdtemp(prefix="rce-zotero-"))
    try:
        copy = tmp / SQLITE_NAME
        shutil.copyfile(db_file, copy)
        return ZoteroLibrary(data_dir, LIBRARY_READ, _query_library(copy, data_dir), from_copy=True)
    except (OSError, sqlite3.DatabaseError) as exc:
        return ZoteroLibrary(data_dir, LIBRARY_UNREADABLE, reason=f"{db_file}: {first_error}; the copy: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- the Zotero program and its attachments (the 文献 view, 11.4) ------------------

#: A Zotero item or attachment key: 8 characters of Zotero's own alphabet
#: (upper-case letters and digits). Anything else is never used.
ZOTERO_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
ZOTERO_BUNDLE_ID = "org.zotero.zotero"
_INSTALLED_TTL_S = 60.0
_installed_cache: tuple[float, bool] | None = None


def valid_zotero_key(key: Any) -> bool:
    return isinstance(key, str) and ZOTERO_KEY_RE.fullmatch(key) is not None


def zotero_select_url(key: str) -> str:
    """「在 Zotero 中打开」: `zotero://select/library/items/<key>`, for a
    key that passed `valid_zotero_key` only."""
    if not valid_zotero_key(key):
        raise ValueError(f"not a Zotero item key: {key!r}")
    return f"zotero://select/library/items/{key}"


def _zotero_app_found() -> bool:
    """Whether macOS knows an application with Zotero's bundle id -- a
    Spotlight query (`mdfind`), which launches nothing and opens nothing."""
    if sys.platform != "darwin":
        return False
    import subprocess  # noqa: PLC0415 -- leaf use

    try:
        result = subprocess.run(
            ["mdfind", f"kMDItemCFBundleIdentifier == '{ZOTERO_BUNDLE_ID}'"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.strip().endswith(".app") for line in result.stdout.splitlines())


def zotero_installed() -> bool:
    """Whether the Zotero program is installed (cached for a minute)."""
    global _installed_cache
    import time  # noqa: PLC0415 -- leaf use

    now = time.monotonic()
    if _installed_cache is None or now - _installed_cache[0] > _INSTALLED_TTL_S:
        _installed_cache = (now, _zotero_app_found())
    return _installed_cache[1]


class AttachmentRefused(Exception):
    """「打开 PDF」 refused: nothing is opened. `code` names the cause,
    `message` is the page's sentence, `str(self)` the detail."""

    def __init__(self, code: str, message: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.message = message


def is_pdf(attachment: ZoteroAttachment) -> bool:
    return attachment.content_type == "application/pdf" and attachment.filename.lower().endswith(".pdf")


def attachment_file(item_key: Any, attachment_key: Any = None, *, library: ZoteroLibrary | None = None) -> Path:
    """The PDF to open for 「打开 PDF」 (11.4): ONLY a file the Zotero
    database names as an imported PDF attachment of `item_key` (and, when
    given, the attachment `attachment_key` of that item), at
    `<data dir>/storage/<attachment key>/<file name>`, which -- symlinks
    resolved -- must be a regular file inside the storage directory and
    on this machine (not only in the cloud). The library is read afresh;
    no path from the caller is ever used. Raises `AttachmentRefused`."""
    if not valid_zotero_key(item_key):
        raise AttachmentRefused("bad_key", "这不是一个 Zotero 条目", f"not a Zotero item key: {item_key!r}")
    if attachment_key is not None and not valid_zotero_key(attachment_key):
        raise AttachmentRefused("bad_key", "这不是一个 Zotero 附件", f"not a Zotero attachment key: {attachment_key!r}")
    library = load_library() if library is None else library
    if library.status != LIBRARY_READ or library.data_dir is None:
        raise AttachmentRefused("library", "没能读取 Zotero 文献库", library.reason or f"library {library.status}")
    item = library.item(item_key)
    if item is None:
        raise AttachmentRefused("no_item", "Zotero 文献库里没有这个条目", f"no item {item_key} in {library.data_dir}")
    own = [a for a in item.attachments if attachment_key is None or a.key == attachment_key]
    if not own and attachment_key is not None:
        raise AttachmentRefused("no_attachment", "这个附件不属于这个条目", f"item {item_key} has no attachment {attachment_key}")
    pdfs = [a for a in own if is_pdf(a)]
    if not pdfs:
        raise AttachmentRefused("not_pdf", "这个条目没有 PDF 附件", f"item {item_key}: no application/pdf attachment")
    att = next((a for a in pdfs if a.available), pdfs[0])
    storage = library.data_dir / "storage"
    try:
        storage_real = storage.resolve(strict=True)
        real = (storage / att.key / att.filename).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AttachmentRefused(
            "missing", "这个 PDF 不在本机的 Zotero 存储目录里", f"{storage / att.key / att.filename}: {exc}",
        ) from exc
    if not real.is_relative_to(storage_real) or real == storage_real:
        raise AttachmentRefused(
            "outside", "这个附件指向 Zotero 存储目录之外，RCE 不打开它", f"{real} is not inside {storage_real}",
        )
    if not real.is_file():
        raise AttachmentRefused("missing", "这个 PDF 不在本机的 Zotero 存储目录里", f"{real} is not a regular file")
    if paths.is_dataless(real):
        raise AttachmentRefused("cloud", "这个 PDF 只在云端，还没有下载到本机", f"{real} is only in the cloud")
    return real


# -- DOI online (opt-in) -----------------------------------------------------------

SETTINGS_FILENAME = "settings.json"
CACHE_FILENAME = "doi-cache.json"
DOI_LOOKUP_SETTING = "doi_online_lookup"
CROSSREF_WORKS = "https://api.crossref.org/works/"
TIMEOUT_S = 10.0


class LookupFailed(Exception):
    """One DOI could not be looked up now. `network` when the network
    itself failed (the rest of the scan's lookups are not tried)."""

    def __init__(self, message: str, *, network: bool = False) -> None:
        super().__init__(message)
        self.network = network


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    from rce.records import files  # noqa: PLC0415 -- records imports paths, which this module imports

    path.parent.mkdir(parents=True, exist_ok=True)
    files.durable_write(path, (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def settings_path() -> Path:
    return paths.rce_home() / SETTINGS_FILENAME


def cache_path() -> Path:
    return paths.rce_home() / CACHE_FILENAME


def doi_lookup_enabled() -> bool:
    """The machine setting 「用 DOI 联网查文献信息」; off unless turned on."""
    return _read_json(settings_path()).get(DOI_LOOKUP_SETTING) is True


def set_doi_lookup(enabled: bool) -> None:
    data = _read_json(settings_path())
    data[DOI_LOOKUP_SETTING] = bool(enabled)
    _write_json(settings_path(), data)


def cached(doi: str) -> dict[str, Any] | None:
    """A DOI's cached answer (with its date), or None. Never asks."""
    entry = _read_json(cache_path()).get(doi)
    return dict(entry) if isinstance(entry, dict) else None


def crossref_url(doi: str) -> str:
    return CROSSREF_WORKS + urllib.parse.quote(doi, safe="/:;()")


CROSSREF_HOST = "api.crossref.org"


class _CrossrefRedirectsOnly(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to https://api.crossref.org: the DOI is never
    sent in clear, nor to a host a redirect names (11.4: lookups go to
    doi.org / Crossref). Any other redirect ends the request (an HTTP
    error: the DOI is asked again on a later scan)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 -- urllib's signature
        target = urllib.parse.urlsplit(newurl)
        try:
            port = target.port
        except ValueError:
            return None
        if target.scheme != "https" or (target.hostname or "").lower() != CROSSREF_HOST or port not in (None, 443):
            logger.info("DOI lookup: a redirect to another place was not followed (%s %s)", code, target.scheme)
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_CrossrefRedirectsOnly())


def _fetch_crossref(url: str, timeout: float) -> bytes:
    """GET `url` with nothing but urllib's own headers -- no email, no
    identifying user agent, no body; a redirect is followed only to
    https://api.crossref.org. Every failure is a `LookupFailed` (a reply
    cut off mid-body too: `http.client.IncompleteRead`)."""
    request = urllib.request.Request(url, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise LookupFailed(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise LookupFailed(f"{type(exc).__name__}: {exc}", network=True) from exc
    except ValueError as exc:
        raise LookupFailed(f"{type(exc).__name__}: {exc}") from exc


#: The one function that touches the network (tests replace it).
FETCH: Callable[[str, float], bytes] = _fetch_crossref


def _first_text(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_crossref(body: bytes) -> dict[str, Any]:
    """A Crossref `works` answer as a reference node's attrs; raises
    `LookupFailed` when it does not parse."""
    try:
        message = json.loads(body.decode("utf-8"))["message"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise LookupFailed(f"unparseable answer: {exc}") from exc
    if not isinstance(message, dict):
        raise LookupFailed("unparseable answer")
    authors = []
    for person in message.get("author") or []:
        if isinstance(person, dict):
            name = person.get("family") or person.get("name")
            if isinstance(name, str) and name.strip():
                authors.append(name.strip())
    year = None
    for key in ("issued", "published-print", "published-online", "published"):
        parts = (message.get(key) or {}).get("date-parts") if isinstance(message.get(key), dict) else None
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0] and isinstance(parts[0][0], int):
            year = parts[0][0]
            break
    return {
        "title": _first_text(message.get("title")),
        "authors": authors,
        "year": year,
        "venue": _first_text(message.get("container-title")) or _first_text(message.get("publisher")),
    }


def lookup_dois(dois: Iterable[str], *, today: date | None = None) -> dict[str, dict[str, Any]]:
    """{doi: metadata} for the DOIs whose answer is cached or, when the
    setting is on, fetched now (module docstring). Nothing is fetched
    while the setting is off."""
    wanted = sorted(set(dois))
    cache = _read_json(cache_path())
    found = {d: dict(cache[d]) for d in wanted if isinstance(cache.get(d), dict)}
    missing = [d for d in wanted if d not in found]
    if not missing or not doi_lookup_enabled():
        return found
    stamp = (today or date.today()).isoformat()
    added = False
    for doi in missing:
        try:
            meta = parse_crossref(FETCH(crossref_url(doi), TIMEOUT_S))
        except LookupFailed as exc:
            logger.info("DOI lookup of %s failed (%s); asked again on a later scan", doi, exc)
            if exc.network:
                break
            continue
        except Exception:  # noqa: BLE001 -- an opt-in lookup never fails the scan (11.4: retried later)
            logger.warning("DOI lookup of %s failed unexpectedly; the rest are asked on a later scan", doi, exc_info=True)
            break
        meta["fetched"] = stamp
        cache[doi] = meta
        found[doi] = dict(meta)
        added = True
    if added:
        try:
            _write_json(cache_path(), cache)
        except OSError as exc:
            logger.warning("could not write the DOI cache %s: %s", cache_path(), exc)
    return found
