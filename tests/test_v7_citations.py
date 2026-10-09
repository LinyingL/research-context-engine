"""DESIGN.md 11.4 / 11.5 #5: the `citations` extractor -- DOIs and Latin
author-year citations in the drafts, the drafts' reference lists, the four
resolution steps (in-draft, DOI -> Zotero, DOI online only when turned on,
author-year -> Zotero candidates), the graph it writes, and `rce
citations`. Every test names the 11.5 scenario it belongs to.

The Zotero library here is a Zotero-shaped SQLite file built per test (the
tables and columns RCE reads, as Zotero 6/7 has them); no test reads a real
library (`tests/conftest.py` points `RCE_ZOTERO_DATA_DIR` at an empty folder)
and no test touches the network (`literature.FETCH` is a recording stand-in).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.request
from pathlib import Path

import pytest

from rce import cli, db, literature
from rce import project as project_identity
from rce.ingest import citations as C
from rce.ingest import pipeline
from rce.ingest import scan as scan_mod
from rce.records import judgements
from rce.records import ledger as ledger_mod
from rce.records.identity import read_identity
from rce.records.situation import index_db_path, write_guard


def _cites(line: str) -> list[tuple[str, str, int | None, str]]:
    """(kind, surname or DOI, year, letter) of every citation in a line."""
    return [
        (kind, fields.get("surname") or fields.get("doi"), fields.get("year"), fields.get("letter", ""))
        for _col, kind, _text, fields in C.find_in_line(line)
    ]


# -- 11.5 #5: what is found ------------------------------------------------------------


@pytest.mark.parametrize("line, expected", [
    ("Simon (1955) 提出有限理性", [("author_year", "Simon", 1955, "")]),
    ("Simon（1955）提出", [("author_year", "Simon", 1955, "")]),
    ("Chen & Peng (2010) argue", [("author_year", "Chen", 2010, "")]),
    ("Chen and Peng (2010) argue", [("author_year", "Chen", 2010, "")]),
    ("Hassan et al. (2019) show", [("author_year", "Hassan", 2019, "")]),
    ("Samuelson 与 Zeckhauser（1988）", [("author_year", "Samuelson", 1988, "")]),
    ("Arslanalp、Eichengreen 与 Simpson-Bell（2022）测算", [("author_year", "Arslanalp", 2022, "")]),
    ("Clayton 等（2025）系统记录了", [("author_year", "Clayton", 2025, "")]),
    ("Berardi, M. (2022). Uncertainty", [("author_year", "Berardi", 2022, "")]),
    ("Simon (1955, 1976)", [("author_year", "Simon", 1955, ""), ("author_year", "Simon", 1976, "")]),
    ("Smith (2019a) and Smith (2019b)", [("author_year", "Smith", 2019, "a"), ("author_year", "Smith", 2019, "b")]),
    ("Simon (1955, p. 99)", [("author_year", "Simon", 1955, "")]),
    ("（Hansen, 2000）", [("author_year", "Hansen", 2000, "")]),
    ("（Samuelson & Zeckhauser, 1988）", [("author_year", "Samuelson", 1988, "")]),
    ("(Kahneman & Tversky, 1979; Simon, 1955)", [("author_year", "Kahneman", 1979, ""), ("author_year", "Simon", 1955, "")]),
    ("（Han, Xu & Yin, 2018；Li, Li & Si, 2020）", [("author_year", "Han", 2018, ""), ("author_year", "Li", 2020, "")]),
    ("（Fraiberger 等, 2021）", [("author_year", "Fraiberger", 2021, "")]),
    ("(Fatum et al. 2017)", [("author_year", "Fatum", 2017, "")]),
    ("情绪水平(Fraiberger 2021)没打过", [("author_year", "Fraiberger", 2021, "")]),
    ("Ranaldo 与 Söderlind（2010）", [("author_year", "Ranaldo", 2010, "")]),
    ("（March, 1963）以及", [("author_year", "March", 1963, "")]),
    ("March 与 Simon（1958）", [("author_year", "March", 1958, "")]),
    ("Moreover, Smith (2020) finds", [("author_year", "Smith", 2020, "")]),
    ("He, Nagel & Song (2022)", [("author_year", "He", 2022, "")]),
    ("Han/Xu/Yin (2018)", [("author_year", "Han", 2018, "")]),
    ("Morris–Shin (2002)", [("author_year", "Morris", 2002, "")]),
    ("McDowell(2023)", [("author_year", "McDowell", 2023, "")]),
])
def test_11_5_5_latin_author_year_citations_are_found(line, expected):
    assert _cites(line) == expected


@pytest.mark.parametrize("line", [
    "张川川（2020）认为",  # Chinese-script names are not read in V7
    "July (2019)",  # a month-year date
    "May (2019)",
    "Figure 2 (2019 data)",
    "Table 3 (2019)",
    "AES(2023)与",  # an acronym, not a name
    "GDP (2019)",
    "Python (3.11)",  # a version number
    "Stata 17 (2021)",
    "v2 (2024)",
    "(Gelpern et al. Economic Policy 2022)",  # a venue word before the year
    "TopicShift (2024)",  # a variable, not a name
    "在 2019 年（2020）",
    "## Section (2019)",
])
def test_11_5_5_false_positives_are_not_read(line):
    assert _cites(line) == []


def test_11_5_5_dois_normalised_trailing_punctuation_and_urls_once():
    line = (
        "见 doi:10.1016/J.JEBO.2022.08.023。另见 https://doi.org/10.1257/jep.27.1.173）"
        "与 `10.1093/qje/qjq004`，以及 [10.1016/j.econlet.2019.108827](https://doi.org/10.1016/j.econlet.2019.108827)；"
        "10.1016/S0022-1996(00)00067-X). 与 [10.1016/j.x.2026.1](https://doi.org/10.1016/j.x.2026.1)(SSRN)"
    )
    assert [d for _k, d, _y, _l in _cites(line)] == [
        "10.1016/j.jebo.2022.08.023",
        "10.1257/jep.27.1.173",
        "10.1093/qje/qjq004",
        "10.1016/j.econlet.2019.108827",
        "10.1016/s0022-1996(00)00067-x",
        "10.1016/j.x.2026.1",
    ]
    assert literature.normalize_doi("10.1/ABC.;:)）。，；`]*") == "10.1/abc"


def test_11_5_5_a_doi_url_query_and_table_bar_end_the_doi():
    assert _cites("| 10.1257/aer.20131314 | 相符 |") == [("doi", "10.1257/aer.20131314", None, "")]
    assert _cites("https://doi.org/10.1016/j.a.1?via=ihub") == [("doi", "10.1016/j.a.1", None, "")]


# -- 11.5 #5: the reference list -----------------------------------------------------------

DRAFT = """# Introduction

Simon（1955）提出有限理性；现状偏好（Samuelson & Zeckhauser, 1988）。
Smith (2019) shows the effect is 0.35 here. Jones (2001) again.
Chen & Peng (2010) argue it; see doi:10.9999/direct.1.

## 参考文献

> 本表条目均经核实。

- Samuelson, W., & Zeckhauser, R. (1988). Status quo bias. *JRU*, 1(1), 7–59. https://doi.org/10.1007/BF00055564
- Simon, H. A. (1955). A behavioral model of rational choice. *QJE*, 69(1), 99–118.
- Smith, A. (2019a). One paper.
- Smith, B. (2019). Another paper. doi:10.9999/smith.b
- Smith, C. (2019). A third paper.
  continued on the next line.
- 张川川（2020）. 中文文献.

---

*说明：Jones (2001) 不在表里。*
"""


def test_11_5_5_reference_list_entries_parsed():
    d = C.parse_lines("paper.md", DRAFT.splitlines(), [])
    assert d.reference_list
    assert [(e.n, e.surname, e.year, e.letter, e.doi) for e in d.entries] == [
        (1, "Samuelson", 1988, "", "10.1007/bf00055564"),
        (2, "Simon", 1955, "", None),
        (3, "Smith", 2019, "a", None),
        (4, "Smith", 2019, "", "10.9999/smith.b"),
        (5, "Smith", 2019, "", None),
    ]
    assert d.entries[4].text.endswith("continued on the next line.")
    # Citations inside the list are its entries; after the thematic break, prose again.
    lines = sorted({c.line for c in d.citations})
    assert lines == [3, 4, 5, 21]


@pytest.mark.parametrize("heading", ["# References", "#### Bibliography", "## 八、核实参考文献", "### 7. References"])
def test_11_5_5_reference_heading_any_level(heading):
    d = C.parse_lines("p.md", [heading, "", "Simon, H. A. (1955). Title."], [])
    assert d.reference_list and [(e.surname, e.year) for e in d.entries] == [("Simon", 1955)]


def test_11_5_5_reference_list_ends_at_a_heading_of_the_same_level():
    lines = ["## References", "- Simon, H. A. (1955). T.", "### Sub", "- Hansen, B. (2000). T.", "## Next", "Simon (1955) again."]
    d = C.parse_lines("p.md", lines, [])
    assert [e.surname for e in d.entries] == ["Simon", "Hansen"]
    assert [c.line for c in d.citations] == [6]


def test_11_5_5_latex_thebibliography():
    lines = [
        r"\section{Intro}", r"As Simon (1955) argued.",
        r"\begin{thebibliography}{9}", r"\bibitem{simon} Simon, H. A. (1955). A behavioral model. doi:10.2307/1884852",
        r"\end{thebibliography}",
    ]
    d = C.parse_lines("p.tex", lines, [], tex=True)
    assert [(e.surname, e.year, e.doi) for e in d.entries] == [("Simon", 1955, "10.2307/1884852")]
    assert [(c.surname, c.line) for c in d.citations] == [("Simon", 2)]


# -- 11.5 #5: checked against a hand count ------------------------------------------------------

# Two drafts in the shapes of the researcher's prose (2026-10-09), counted by
# hand: every Latin author-year citation and every DOI, nothing else.
HAND_A = """# 一、引言

其行为经济学的同构物是现状偏好与默认选项机制（Samuelson & Zeckhauser, 1988）；其组织基础是机构按既定规则行事（Cyert & March, 1963）。
媒体情绪的作用有跨国证据（Fraiberger等, 2021），亦有直接先例（Han, Xu & Yin, 2018；Li, Li & Si, 2020）。张川川（2020）的研究不计。
2019年4月至2021年10月（2019年4月），写入三大指数；QLR 统计量达 1504（常规临界值约 11.8）。
因果机制借助既有证据背书（Raddatz, Schmukler & Williams, 2017；Pandolfi & Williams, 2019），并把结论（Fatum, Yamamoto & Zhu, 2017）延伸。
"""
HAND_A_COUNT = {"author_year": 8, "doi": 0}

HAND_B = """## 查新报告

1. **VERIFIED** — Funke, M., Shu, C., Cheng, X., & Eraslan, S. (2015). Assessing the CNH–CNY pricing differential. *JIMF*, 59, 245–262. DOI: [10.1016/j.jimonfin.2015.07.008](https://doi.org/10.1016/j.jimonfin.2015.07.008) — 结论。
2. **VERIFIED** — Cheung, Y.-W., & Rime, D. (2014). The offshore renminbi exchange rate. DOI: [10.1016/j.jimonfin.2014.05.012](https://doi.org/10.1016/j.jimonfin.2014.05.012)
3. **VERIFIED(NBER WP)** — Agarwal, I., Chen, W., & Prasad, E. S. (2024). Beyond the fundamentals. NBER WP 33159. DOI: [10.3386/w33159](https://www.nber.org/papers/w33159)
已有文本度量:EPU(不确定性水平,Li-Li-Si 2020)和搜索量注意力(Han-Xu-Yin 2018;Li et al. 2023)。情绪水平(Fraiberger 2021)。

Sources: [Li et al. Wiley](https://onlinelibrary.wiley.com/doi/10.1111/acfi.13191) | [Funke et al. SSRN](https://papers.ssrn.com/abstract_id=2571449)
"""
HAND_B_COUNT = {"author_year": 7, "doi": 4}


@pytest.mark.parametrize("text, count", [(HAND_A, HAND_A_COUNT), (HAND_B, HAND_B_COUNT)])
def test_11_5_5_every_citation_found_against_a_hand_count(text, count):
    d = C.parse_lines("draft.md", text.splitlines(), [])
    found = {"author_year": 0, "doi": 0}
    for c in d.citations:
        found[c.kind] += 1
    assert found == count
    assert not any(c.surname and not c.surname.isascii() and "张" in c.surname for c in d.citations)


# -- Zotero ---------------------------------------------------------------------------------------

_ZOTERO_SCHEMA = """
CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT NOT NULL, editable INT, filesEditable INT, version INT, storageVersion INT, lastSync INT, archived INT);
CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT, templateItemTypeID INT, display INT);
CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INT NOT NULL, dateAdded TEXT, dateModified TEXT, clientDateModified TEXT, libraryID INT NOT NULL, key TEXT NOT NULL, version INT, synced INT);
CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT, fieldFormatID INT);
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value UNIQUE);
CREATE TABLE itemData (itemID INT, fieldID INT, valueID INT, PRIMARY KEY (itemID, fieldID));
CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INT);
CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
CREATE TABLE itemCreators (itemID INT, creatorID INT, creatorTypeID INT, orderIndex INT, PRIMARY KEY (itemID, creatorID, creatorTypeID, orderIndex));
CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INT, linkMode INT, contentType TEXT, charsetID INT, path TEXT, syncState INT, storageModTime INT, storageHash TEXT, lastProcessedModificationTime INT);
CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY, dateDeleted TEXT);
INSERT INTO libraries VALUES (1, 'user', 1, 1, 0, 0, 0, 0), (2, 'feed', 1, 1, 0, 0, 0, 0);
INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle'), (2, 'attachment'), (3, 'book'), (4, 'note');
INSERT INTO fields (fieldID, fieldName) VALUES (1, 'title'), (2, 'date'), (3, 'DOI'), (4, 'publicationTitle'), (5, 'publisher');
INSERT INTO creatorTypes VALUES (1, 'author'), (2, 'editor');
"""


class ZoteroFixture:
    """A Zotero-shaped data directory: `zotero.sqlite` and `storage/`."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "zotero.sqlite"
        conn = sqlite3.connect(self.path)
        conn.executescript(_ZOTERO_SCHEMA)
        conn.commit()
        conn.close()
        self._next = 1

    def add(self, key, *, title, date, creators, doi=None, venue=None, type_id=1, library=1, deleted=False,
            attachment=None):
        conn = sqlite3.connect(self.path)
        item_id = self._next
        self._next += 2
        conn.execute("INSERT INTO items (itemID, itemTypeID, libraryID, key) VALUES (?, ?, ?, ?)", (item_id, type_id, library, key))
        for field_id, value in ((1, title), (2, date), (3, doi), (4, venue)):
            if value is None:
                continue
            conn.execute("INSERT OR IGNORE INTO itemDataValues (value) VALUES (?)", (value,))
            vid = conn.execute("SELECT valueID FROM itemDataValues WHERE value = ?", (value,)).fetchone()[0]
            conn.execute("INSERT INTO itemData VALUES (?, ?, ?)", (item_id, field_id, vid))
        for order, (last, first, ctype) in enumerate(creators):
            cid = conn.execute("INSERT INTO creators (firstName, lastName, fieldMode) VALUES (?, ?, 0)", (first, last)).lastrowid
            conn.execute("INSERT INTO itemCreators VALUES (?, ?, ?, ?)", (item_id, cid, ctype, order))
        if deleted:
            conn.execute("INSERT INTO deletedItems VALUES (?, '2023-06-10')", (item_id,))
        if attachment is not None:
            att_key, filename, present = attachment
            conn.execute("INSERT INTO items (itemID, itemTypeID, libraryID, key) VALUES (?, 2, ?, ?)", (item_id + 1, library, att_key))
            conn.execute(
                "INSERT INTO itemAttachments (itemID, parentItemID, linkMode, contentType, path) VALUES (?, ?, 0, 'application/pdf', ?)",
                (item_id + 1, item_id, f"storage:{filename}"),
            )
            if present:
                (self.dir / "storage" / att_key).mkdir(parents=True, exist_ok=True)
                (self.dir / "storage" / att_key / filename).write_bytes(b"%PDF-1.4\n")
        conn.commit()
        conn.close()

    def digest(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


@pytest.fixture
def zotero(tmp_path, monkeypatch) -> ZoteroFixture:
    z = ZoteroFixture(tmp_path / "Zotero")
    z.add("SIMON55K", title="A Behavioral Model of Rational Choice", date="1955-02-00 1955",
          creators=[("Simon", "Herbert A.", 1)], doi="10.2307/1884852", venue="The Quarterly Journal of Economics",
          attachment=("ATTSIM01", "Simon - 1955.pdf", True))
    z.add("SODER10A", title="Safe haven currencies", date="2010-00-00 2010",
          creators=[("Söderlind", "Paul", 1), ("Ranaldo", "Angelo", 1)], venue="Review of Finance",
          attachment=("ATTSOD01", "missing.pdf", False))
    z.add("JONES01A", title="Jones one", date="2001-00-00 2001", creators=[("Jones", "A.", 1)])
    z.add("JONES01B", title="Jones two", date="2001-05-00 05/2001", creators=[("Editor", "E.", 2), ("JONES", "B.", 1)])
    z.add("TRASHED1", title="Trashed", date="2001-00-00 2001", creators=[("Jones", "C.", 1)], deleted=True)
    z.add("FEEDITEM", title="Feed", date="2001-00-00 2001", creators=[("Jones", "D.", 1)], library=2)
    z.add("ENTRYDOI", title="Status quo bias in decision making", date="1988-03-00 03/1988",
          creators=[("Samuelson", "William", 1), ("Zeckhauser", "Richard", 1)], doi="10.1007/BF00055564.")
    monkeypatch.setenv(literature.DATA_DIR_ENV_VAR, str(z.dir))
    return z


def test_11_5_5_zotero_library_read_on_a_copy_never_written(zotero):
    before = zotero.digest()
    lib = literature.load_library()
    assert lib.status == literature.LIBRARY_READ and not lib.from_copy
    keys = sorted(i.key for i in lib.items)
    assert keys == ["ENTRYDOI", "JONES01A", "JONES01B", "SIMON55K", "SODER10A"]  # no trash, feed, attachments
    simon = lib.by_doi("10.2307/1884852")
    assert simon.key == "SIMON55K" and simon.year == 1955 and simon.first_creator == "Simon"
    assert simon.venue == "The Quarterly Journal of Economics"
    assert [a.payload() for a in simon.attachments] == [
        {"key": "ATTSIM01", "file": "Simon - 1955.pdf", "content_type": "application/pdf", "available": True},
    ]
    assert lib.by_doi("10.1007/bf00055564").key == "ENTRYDOI"  # the DOI field normalised too
    assert lib.item("SODER10A").attachments[0].available is False
    # The first AUTHOR is the first creator (an editor listed first is not).
    assert lib.item("JONES01B").first_creator == "JONES"
    assert zotero.digest() == before
    assert sorted(p.name for p in zotero.dir.iterdir()) == ["storage", "zotero.sqlite"]  # no journal, no -wal


def test_11_5_5_a_locked_library_is_read_from_a_copy(zotero, monkeypatch):
    real = literature._query_library
    seen = []

    def locked_once(db_file, data_dir):
        seen.append(db_file)
        if len(seen) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(db_file, data_dir)

    monkeypatch.setattr(literature, "_query_library", locked_once)
    lib = literature.load_library()
    assert lib.status == literature.LIBRARY_READ and lib.from_copy
    assert seen[0] == zotero.path and seen[1] != zotero.path and not seen[1].exists()  # the copy is gone
    assert len(lib.items) == 5


def test_11_5_5_absent_and_unreadable_libraries(tmp_path):
    assert literature.load_library(tmp_path / "nowhere").status == literature.LIBRARY_ABSENT
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "zotero.sqlite").write_bytes(b"not a database at all" * 100)
    lib = literature.load_library(bad)
    assert lib.status == literature.LIBRARY_UNREADABLE and not lib.readable


def test_11_5_5_data_directory_from_the_zotero_profile(tmp_path, monkeypatch):
    monkeypatch.delenv(literature.DATA_DIR_ENV_VAR, raising=False)
    home = tmp_path / "home"
    import sys

    root = home / ("Library/Application Support/Zotero" if sys.platform == "darwin" else ".zotero/zotero")
    profile = root / "Profiles" / "abc.default"
    profile.mkdir(parents=True)
    (root / "profiles.ini").write_text("[General]\nStartWithLastProfile=1\n\n[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/abc.default\nDefault=1\n")
    assert literature.zotero_data_dir(home=home) == home / "Zotero"  # no dataDir pref: the default
    (profile / "prefs.js").write_text('user_pref("extensions.zotero.dataDir", "/Volumes/Data/My Zotero");\nuser_pref("extensions.zotero.useDataDir", true);\n')
    assert literature.zotero_data_dir(home=home) == Path("/Volumes/Data/My Zotero")
    (profile / "prefs.js").write_text('user_pref("extensions.zotero.dataDir", "/x");\nuser_pref("extensions.zotero.useDataDir", false);\n')
    assert literature.zotero_data_dir(home=home) == home / "Zotero"


def test_11_5_5_attachment_names_are_plain_file_names_only():
    assert literature.storage_filename("storage:a.pdf", 0) == "a.pdf"
    assert literature.storage_filename("storage:../x.pdf", 0) is None
    assert literature.storage_filename("storage:..", 1) is None
    assert literature.storage_filename("/abs/linked.pdf", 2) is None
    assert literature.storage_filename("storage:a.pdf", 3) is None


# -- resolution, step by step -------------------------------------------------------------------


def _draft(text=DRAFT):
    return C.parse_lines("paper.md", text.splitlines(), [])


def _cit(d, surname, year, letter=""):
    return next(c for c in d.citations if c.surname == surname and c.year == year and c.letter == letter)


def test_11_5_5_step1_in_draft_unique_or_candidates():
    d = _draft()
    assert [e.n for e in C.resolve_in_draft(_cit(d, "Simon", 1955), d.entries)] == [2]
    # Smith (2019): two entries (b, c) with no letter -- ambiguous; 2019a is not 2019.
    assert [e.n for e in C.resolve_in_draft(_cit(d, "Smith", 2019), d.entries)] == [4, 5]
    assert C.resolve_in_draft(_cit(d, "Jones", 2001), d.entries) == []


def test_11_5_5_step2_doi_to_zotero(zotero):
    lib = literature.load_library()
    assert C.zotero_item_for_doi("10.2307/1884852", lib).key == "SIMON55K"
    assert C.zotero_item_for_doi("10.9999/none", lib) is None


def test_11_5_5_step4_candidates_by_surname_and_year_diacritics_insensitive(zotero):
    lib = literature.load_library()
    d = C.parse_lines("p.md", ["Soderlind (2010) and Jones (2001) and jones? Simon (1955)"], [])
    assert [i.key for i in C.zotero_candidates(_cit(d, "Soderlind", 2010), lib)] == ["SODER10A"]
    assert [i.key for i in C.zotero_candidates(_cit(d, "Jones", 2001), lib)] == ["JONES01A", "JONES01B"]


def test_11_5_5_resolution_statuses(zotero):
    lib = literature.load_library()
    res = {(r.citation.kind, r.citation.surname or r.citation.doi, r.citation.year): r for r in C.resolve(_draft(), lib)}
    simon = res[("author_year", "Simon", 1955)]
    assert (simon.status, simon.how, [t.node_id for t in simon.targets]) == ("auto", "entry", ["ref:entry:paper.md#2"])
    sam = res[("author_year", "Samuelson", 1988)]
    # The entry carries a DOI: the reference is the DOI's, described by Zotero (step 2).
    assert sam.status == "auto" and sam.targets[0].node_id == "ref:doi:10.1007/bf00055564"
    assert sam.targets[0].zotero.key == "ENTRYDOI"
    smith = res[("author_year", "Smith", 2019)]
    assert (smith.status, smith.how) == ("pending", "entry_candidates")
    assert [t.node_id for t in smith.targets] == ["ref:doi:10.9999/smith.b", "ref:entry:paper.md#5"]
    jones = res[("author_year", "Jones", 2001)]
    assert (jones.status, jones.how) == ("pending", "zotero_candidates")
    assert [t.node_id for t in jones.targets] == ["ref:zotero:JONES01A", "ref:zotero:JONES01B"]
    direct = res[("doi", "10.9999/direct.1", None)]
    assert (direct.status, direct.how) == ("auto", "doi")
    chen = res[("author_year", "Chen", 2010)]
    assert (chen.status, chen.how, chen.targets) == (None, "unresolved", ())
    # Nothing is decided by surname alone: every non-identifier resolution is pending.
    assert all(r.status in ("pending", None) for r in res.values() if r.how not in ("entry", "doi"))


# -- step 3: online, only when turned on -------------------------------------------------------------


class Recorder:
    def __init__(self, answers=None, fail=None):
        self.urls: list[str] = []
        self.answers = answers or {}
        self.fail = fail or {}

    def __call__(self, url, timeout):
        self.urls.append(url)
        doi = url[len(literature.CROSSREF_WORKS):]
        if doi in self.fail:
            raise self.fail[doi]
        msg = self.answers.get(doi, {"title": [f"T {doi}"], "author": [{"family": "Fam"}], "issued": {"date-parts": [[2001]]},
                                     "container-title": ["J"]})
        return json.dumps({"status": "ok", "message": msg}).encode()


def test_11_5_5_online_lookup_does_nothing_until_turned_on(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(literature, "FETCH", rec)
    assert literature.lookup_dois(["10.2307/1884852"]) == {}
    assert rec.urls == [] and not literature.cache_path().exists()
    literature.set_doi_lookup(True)
    found = literature.lookup_dois(["10.2307/1884852", "10.1257/jep.27.1.173"])
    assert rec.urls == [
        "https://api.crossref.org/works/10.1257/jep.27.1.173",
        "https://api.crossref.org/works/10.2307/1884852",
    ]
    assert found["10.2307/1884852"]["title"] == "T 10.2307/1884852" and found["10.2307/1884852"]["fetched"]
    # Cached with the date; never asked again.
    cache = json.loads(literature.cache_path().read_text())
    assert set(cache) == {"10.2307/1884852", "10.1257/jep.27.1.173"} and all(v["fetched"] for v in cache.values())
    literature.lookup_dois(["10.2307/1884852"])
    assert len(rec.urls) == 2
    literature.set_doi_lookup(False)
    assert literature.lookup_dois(["10.2307/1884852"])["10.2307/1884852"]["title"]  # the cache still answers
    assert len(rec.urls) == 2


def test_11_5_5_a_failed_lookup_is_retried_later_never_recorded_as_not_found(monkeypatch):
    literature.set_doi_lookup(True)
    rec = Recorder(fail={"10.1/aaaa": literature.LookupFailed("HTTP 404"), "10.2/bbbb": literature.LookupFailed("down", network=True)})
    monkeypatch.setattr(literature, "FETCH", rec)
    assert literature.lookup_dois(["10.1/aaaa", "10.2/bbbb", "10.3/cccc"]) == {}
    # The 404 is tried, the network failure stops the rest of this scan.
    assert rec.urls == ["https://api.crossref.org/works/10.1/aaaa", "https://api.crossref.org/works/10.2/bbbb"]
    assert not literature.cache_path().exists()
    rec.fail = {}
    assert set(literature.lookup_dois(["10.1/aaaa", "10.2/bbbb", "10.3/cccc"])) == {"10.1/aaaa", "10.2/bbbb", "10.3/cccc"}


def test_11_5_5_the_request_carries_only_the_doi(monkeypatch):
    seen = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"message": {}}'

    def fake_urlopen(request, timeout):
        seen.append((request.full_url, request.get_method(), request.data, dict(request.header_items()), timeout))
        return Answer()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    literature._fetch_crossref(literature.crossref_url("10.2307/1884852"), 10.0)
    assert seen == [("https://api.crossref.org/works/10.2307/1884852", "GET", None, {}, 10.0)]


# -- the graph -----------------------------------------------------------------------------------------


def _project(tmp_path, text=DRAFT, name="paper.md"):
    root = tmp_path / "proj"
    root.mkdir()
    (root / name).write_text(text, encoding="utf-8")
    project_identity.init_project(root)
    _scan(root)
    return root


def _conn(root):
    return db.connect(index_db_path(read_identity(root).identity.id))


def _scan(root):
    with write_guard(root):
        conn = _conn(root)
        try:
            pipeline.ingest_sources(conn, root)
        finally:
            conn.close()


def _edges(root):
    conn = _conn(root)
    try:
        rows = conn.execute(
            "SELECT src, dst, status, evidence, scan_basis, scan_source FROM edges "
            "WHERE extractor = 'citations' ORDER BY src, dst"
        ).fetchall()
        return [tuple(r) for r in rows]
    finally:
        conn.close()


def _section(root, name="paper.md"):
    return C.parse_draft(root, name).citations[0].section_id


def test_11_5_5_graph_nodes_edges_statuses_and_basis(tmp_path, zotero):
    root = _project(tmp_path)
    sec = _section(root)
    edges = {(s, d): (st, json.loads(ev), json.loads(b), src) for s, d, st, ev, b, src in _edges(root)}
    assert (sec, "ref:entry:paper.md#2") in edges  # Simon 1955 via the entry, auto
    st, ev, basis, source = edges[(sec, "ref:entry:paper.md#2")]
    assert st == "auto" and source == "paper.md"
    assert basis == {"cited": ["simon|1955"], "entry_dois": []}
    assert ev["occurrences"] == [{"file": "paper.md", "line": 3, "text": "Simon（1955）"}]
    assert edges[(sec, "ref:doi:10.1007/bf00055564")][2] == {"cited": ["samuelson|1988"], "entry_dois": ["10.1007/bf00055564"]}
    assert edges[(sec, "ref:doi:10.9999/direct.1")][0] == "auto"
    assert edges[(sec, "ref:doi:10.9999/smith.b")][0] == "pending"
    assert edges[(sec, "ref:entry:paper.md#5")][0] == "pending"
    zot = edges[(sec, "ref:zotero:JONES01A")]
    assert zot[0] == "pending" and zot[3] == scan_mod.citations_zotero_source("paper.md")
    assert not any(d.startswith("ref:") and "Chen" in d for _s, d in edges)
    conn = _conn(root)
    try:
        node = db.get_node(conn, "ref:doi:10.1007/bf00055564")
        assert node["type"] == "reference" and node["attrs"]["source"] == "zotero"
        assert node["attrs"]["zotero_key"] == "ENTRYDOI" and node["title"] == "Status quo bias in decision making"
        entry = db.get_node(conn, "ref:entry:paper.md#2")
        assert entry["attrs"]["source"] == "entry" and entry["attrs"]["year"] == 1955
        assert db.get_node(conn, "ref:doi:10.9999/direct.1")["attrs"]["source"] is None
        assert scan_mod.source_status(conn, {"src": sec, "dst": "ref:zotero:JONES01A", "type": "cites", "extractor": "citations"}) == "read_and_parsed"
    finally:
        conn.close()


def test_11_5_5_a_claim_covering_the_sentence_cites_too(tmp_path):
    root = _project(tmp_path)
    claim_edges = [(s, d) for s, d, *_ in _edges(root) if s.startswith("claim:")]
    # "Smith (2019) shows the effect is 0.35 here." -- the claim 0.35 covers it.
    assert claim_edges and {d for _s, d in claim_edges} == {"ref:doi:10.9999/smith.b", "ref:entry:paper.md#5"}


def test_11_5_5_three_scans_identical_edges_and_ledger(tmp_path, zotero):
    root = _project(tmp_path)
    first, ledger0 = _edges(root), ledger_mod.judgements_path(root).exists()
    _scan(root)
    _scan(root)
    assert _edges(root) == first
    assert ledger_mod.judgements_path(root).exists() == ledger0 is False


def test_11_5_5_candidates_confirmed_and_rejected_through_the_ledger(tmp_path, zotero):
    root = _project(tmp_path)
    sec = _section(root)
    good = (sec, "ref:zotero:JONES01A", "cites", "citations")
    bad = (sec, "ref:zotero:JONES01B", "cites", "citations")
    judgements.judge(root, good, "confirmed", via="cli")
    judgements.judge(root, bad, "rejected", via="cli")
    entries = ledger_mod.load_judgements(root).ledger.entries
    assert [(e.get("verdict"), e.get("dst")) for e in entries] == [("confirmed", good[1]), ("rejected", bad[1])]
    assert entries[0].get("basis") == {"cited": ["jones|2001"], "entry_dois": []}
    ledger_before = ledger_mod.judgements_path(root).read_bytes()
    _scan(root)
    _scan(root)
    conn = _conn(root)
    try:
        statuses = db.edge_statuses(conn)
        assert statuses[good][0] == "confirmed" and statuses[bad][0] == "rejected"
    finally:
        conn.close()
    assert ledger_mod.judgements_path(root).read_bytes() == ledger_before
    rep = C.report(root, ["paper.md"], statuses={(s, d): st for (s, d, _t, _e), (st, _m) in statuses.items()})
    jones = next(c for c in rep["drafts"][0]["citations"] if c["surname"] == "Jones")
    assert jones["state"] == "resolved"


def test_11_5_5_a_changed_entry_doi_puts_the_judgment_under_review(tmp_path, zotero):
    root = _project(tmp_path)
    sec = _section(root)
    key = (sec, "ref:doi:10.1007/bf00055564", "cites", "citations")
    judgements.judge(root, key, "confirmed", via="cli")
    text = DRAFT.replace("https://doi.org/10.1007/BF00055564", "")
    (root / "paper.md").write_text(text, encoding="utf-8")
    _scan(root)
    conn = _conn(root)
    try:
        state = db.judgement_states(conn)[key]
        assert state["outcome"] == "review" and state["reason"] in (judgements.ENDPOINT_GONE, judgements.NOT_PRODUCED)
        # The edge to the entry that no longer carries a DOI is new; the old one is gone.
        assert (sec, "ref:entry:paper.md#1") in {(s, d) for s, d, *_ in _edges(root)}
        assert db.get_node(conn, "ref:doi:10.1007/bf00055564") is None
    finally:
        conn.close()


def test_11_5_5_basis_changes_when_the_matched_entry_doi_changes(tmp_path):
    text = DRAFT.replace("doi:10.9999/direct.1", "Zed (1999)").replace(
        "- 张川川（2020）. 中文文献.", "- Zed, Q. (1999). A paper. doi:10.1234/zed.one"
    )
    root = _project(tmp_path, text)
    sec = _section(root)
    key = (sec, "ref:doi:10.1234/zed.one", "cites", "citations")
    judgements.judge(root, key, "confirmed", via="cli")
    # Same DOI node reached by a second, different citation key: basis changes -> review.
    (root / "paper.md").write_text(text.replace("Zed (1999)", "Zed (1999) and doi:10.1234/zed.one"), encoding="utf-8")
    _scan(root)
    conn = _conn(root)
    try:
        state = db.judgement_states(conn)[key]
        assert state["outcome"] == "review" and state["reason"] == judgements.BASIS_CHANGED
    finally:
        conn.close()


def test_11_5_5_a_citation_removed_from_the_draft_loses_its_link(tmp_path):
    root = _project(tmp_path)
    assert any(d == "ref:doi:10.9999/direct.1" for _s, d, *_ in _edges(root))
    (root / "paper.md").write_text(DRAFT.replace("see doi:10.9999/direct.1.", ""), encoding="utf-8")
    _scan(root)
    assert not any(d == "ref:doi:10.9999/direct.1" for _s, d, *_ in _edges(root))
    conn = _conn(root)
    try:
        assert db.get_node(conn, "ref:doi:10.9999/direct.1") is None
    finally:
        conn.close()


def test_11_5_5_an_unreadable_library_keeps_the_candidates(tmp_path, zotero, monkeypatch):
    root = _project(tmp_path)
    sec = _section(root)
    assert (sec, "ref:zotero:JONES01A") in {(s, d) for s, d, *_ in _edges(root)}
    zotero.path.write_bytes(b"corrupt" * 200)
    _scan(root)
    assert (sec, "ref:zotero:JONES01A") in {(s, d) for s, d, *_ in _edges(root)}
    conn = _conn(root)
    try:
        assert scan_mod.source_status(conn, {"src": sec, "dst": "ref:zotero:JONES01A", "type": "cites", "extractor": "citations"}) == "unreadable"
    finally:
        conn.close()


def test_11_5_5_online_off_by_default_in_a_scan_and_only_dois_sent_when_on(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(literature, "FETCH", rec)
    root = _project(tmp_path)
    assert rec.urls == []
    literature.set_doi_lookup(True)
    _scan(root)
    assert rec.urls and all(u.startswith(literature.CROSSREF_WORKS + "10.") for u in rec.urls)
    assert sorted(u[len(literature.CROSSREF_WORKS):] for u in rec.urls) == ["10.1007/bf00055564", "10.9999/direct.1", "10.9999/smith.b"]
    conn = _conn(root)
    try:
        assert db.get_node(conn, "ref:doi:10.9999/direct.1")["attrs"]["source"] == "crossref"
    finally:
        conn.close()
    # Nothing of the drafts went anywhere: every request is a crossref URL of a DOI.
    assert not any("Simon" in u or "%" in u for u in rec.urls)


# -- rce citations ------------------------------------------------------------------------------------------


def test_11_5_5_cli_report_and_setting(tmp_path, zotero, capsys):
    root = _project(tmp_path)
    assert cli.main(["citations", str(root), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    draft = rep["drafts"][0]
    assert draft["file"] == "paper.md" and draft["reference_list"] and draft["entries"] == 5
    assert draft["counts"] == {"citations": 7, "resolved": 3, "pending": 3, "unresolved": 1}
    assert rep["zotero"]["status"] == "read" and rep["lookup"]["enabled"] is False
    assert cli.main(["citations", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Zotero library: read" in out and "not found" in out and "Chen & Peng (2010)" in out
    assert cli.main(["citations", "lookup", "--on"]) == 0
    assert literature.doi_lookup_enabled() and "on" in capsys.readouterr().out
    assert cli.main(["citations", "lookup", "--off"]) == 0
    assert not literature.doi_lookup_enabled()
    assert cli.main(["citations", str(root), "--on"]) == 1


def test_11_5_5_cli_report_writes_nothing_and_asks_no_network(tmp_path, zotero, monkeypatch):
    root = _project(tmp_path)
    rec = Recorder()
    monkeypatch.setattr(literature, "FETCH", rec)
    literature.set_doi_lookup(True)
    before = sorted(p.name for p in root.rglob("*"))
    assert cli.main(["citations", str(root)]) == 0
    assert rec.urls == [] and sorted(p.name for p in root.rglob("*")) == before


def test_11_5_5_a_rescan_names_the_unreadable_library_not_the_draft(tmp_path, zotero):
    from rce import addproject

    root = _project(tmp_path)
    zotero.path.write_bytes(b"corrupt" * 200)
    report = addproject.rescan(root)
    assert not any("paper.md" in s for s in report.unreadable_sources)
    assert any("Zotero library not read" in n for n in report.notes)
