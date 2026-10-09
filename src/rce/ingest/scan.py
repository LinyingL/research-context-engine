"""What a scan saw (DESIGN.md 9.6, "Basis" and "What a scan must report";
task V5 phase 3). No judgment logic lives here: this module records, per
scan, which sources each extractor read and what it produced from them,
and answers the questions the judgment ledger will ask of a link.

**Why a scan must report.** A judgment is a statement about particular
evidence, applied only while that evidence stands. The index's
`evidence.occurrences` cannot say whether it still stands: it is an
accumulation of everything ever seen. So each scan stamps, on every link it
produces, what IT saw (`edges.scan_basis` / `scan_seen` / `scan_source`,
migration 0004), and reports, per source, one of:

- `READ_AND_PARSED` -- the source was read and understood;
- `UNREADABLE` -- it could not be read (permissions, evicted, gone mid-scan);
- `UNPARSEABLE` -- it was read but not fully understood (a Python syntax
  error, an R call with unbalanced parentheses, a corrupt tracking run, a
  savefig line git cannot attribute);
- `ABSENT` -- it is not in an inventory that was read successfully (a
  renamed or removed file). An observation, like `READ_AND_PARSED`: its
  links were not produced.

`NOT_SCANNED` is what a query answers for a source no scan reported. Before
this module, "unparseable" and "no calls" looked the same (`[]`), and a link
could not be said to be "no longer produced" by anyone.

**A source** is what 9.6 names: the file named in the link's evidence
(dataflow, pyfig, latex, mdpaper, claims), the tracking store read in that
run (mlflow `mlflow:<dir>`, wandb `wandb:<entity/project>`), the commit list
(git: `commits`), the consistency check (attempts_consistency: `check`), the
attempt table's Markdown file (attempts), `.rce/mappings.toml` (mapping).
Two refinements, both so that a scan never speaks for what it did not read:

- a claims `backed_by` link rests on the claim's file AND the experiment's
  tracking store, and 9.6 computes its basis "only against experiments read
  in the same ingest run". Its source is therefore the pair
  `<file>\\x1f<store>`, reported only for stores this scan read; a link to
  an experiment from a store not read this run keeps its previous state;
- the file inventory is a pseudo-extractor (`INVENTORY`) whose sources are
  the files it listed: it is what says a script, dataset or figure is
  still "in the scan" when no call names it any more (9.6 reason 「机器不再
  得出这条关联」 needs both ends present after the call is deleted).

**A scan speaks only for the extractors it ran and the sources it read.**
`finish()` writes only the rows of sources this scan reported; a failed
scan writes only its failure statuses (always safe: they assert nothing);
an extractor that did not run, a store not read, a file a partial scan
skipped -- all keep their previous state. Every extractor accepts `scan=`;
called without one it opens and finishes its own (`own_scan`), so every
partial re-ingest (the watcher's attempts / dataflow / mappings) leaves a
correct, partial scan record.

**Basis** (`basis`) is the per-extractor canonical record of the 9.6 table,
nothing positional. Comparison is on `db.canonical_basis` (JSON, sorted
keys). A link produced twice in one scan merges its basis
(`db.merge_basis`); a new scan replaces it.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Iterable, Iterator, Mapping

from rce import db

logger = logging.getLogger(__name__)

READ_AND_PARSED = "read_and_parsed"
UNREADABLE = "unreadable"
UNPARSEABLE = "unparseable"
ABSENT = "absent"
NOT_SCANNED = "not_scanned"

#: Statuses that are observations: the scan can say what the source produces.
OBSERVED = frozenset({READ_AND_PARSED, ABSENT})
#: Statuses that keep a source's previous state: the scan could not look.
FAILED = frozenset({UNREADABLE, UNPARSEABLE})

INVENTORY = "inventory"

#: Extractors whose sources are project files, so a file missing from a
#: successfully read inventory is `ABSENT` for them.
FILE_EXTRACTORS = frozenset({"dataflow", "pyfig", "latex", "mdpaper", "claims", "citations"})

#: Separator of a claims link's compound source `<file>\x1f<store>`
#: (module docstring); a control character no file name carries.
SOURCE_SEPARATOR = "\x1f"

#: The Zotero half of a `citations` candidate's source (11.4):
#: `<draft>\x1fzotero`, reported unreadable when the library could not be
#: read, so the candidates it would have offered keep their state.
ZOTERO_STORE = "zotero"

GIT_SOURCE = "commits"
CHECK_SOURCE = "check"


# -- basis ---------------------------------------------------------------------


def bare_call_name(label: str, *, language: str = "python") -> str:
    """The bare name of a call as the dataflow/pyfig evidence labels it:
    Python drops the receiver (`pd.read_csv`, `df.to_csv`, `plt.savefig`
    -> `read_csv`, `to_csv`, `savefig`), R drops the package prefix
    (`haven::read_dta` -> `read_dta`) and keeps dots that are part of the
    name (`read.csv`)."""
    if language == "r":
        return label.rsplit("::", 1)[-1]
    return label.rsplit(".", 1)[-1]


def rounded(value: float, places: int) -> str:
    """A metric value rounded half-up to `places` decimals, as text (exact,
    and stable in canonical JSON) -- the claims basis's metric values."""
    from rce.ingest.claims import _round_half_up  # one rounding rule, claims owns it

    return str(_round_half_up(Decimal(str(value)), places))


def basis(extractor: str, edge_type: str, **facts: Any) -> dict[str, Any]:
    """The 9.6 basis of one production of a link.

    | extractor | basis |
    | dataflow reads/writes | {"calls": [bare call name]} (`call=`) |
    | claims backed_by | {"sentence", "number", "metrics": {name: rounded}} |
    | pyfig generates | {"calls": [bare call name]} (`call=`) |
    | mlflow/wandb produces | {"artifacts": [artifact path]} (`artifact=`) |
    | citations cites | {"cited": [normalised "surname|year" or "doi:<doi>"],
    |                 |  "entry_dois": [the matched reference-list entries' DOIs],
    |                 |  "entries": [the entry's text], for a `ref:entry:` link only} (11.4) |
    | everything else | {} -- the link's identity alone |

    Lists, not scalars, wherever one scan can produce a link more than once
    (two calls reading the same file): `db.merge_basis` unions them."""
    if extractor in ("dataflow", "pyfig"):
        return {"calls": [facts["call"]]}
    if extractor == "claims" and edge_type == "backed_by":
        return {
            "sentence": facts["sentence"],
            "number": facts["number"],
            "metrics": dict(facts["metrics"]),
        }
    if extractor in ("mlflow", "wandb") and edge_type == "produces":
        return {"artifacts": [facts["artifact"]]}
    if extractor == "citations" and edge_type == "cites":
        out = {"cited": sorted(facts["cited"]), "entry_dois": sorted(facts.get("entry_dois", ()))}
        if facts.get("entries"):
            out["entries"] = sorted(facts["entries"])
        return out
    return {}


def file_of(source: str) -> str:
    """The project file a source names (the file half of a claims pair)."""
    return source.split(SOURCE_SEPARATOR, 1)[0]


def claims_source(file: str, store: str) -> str:
    return f"{file}{SOURCE_SEPARATOR}{store}"


def citations_zotero_source(file: str) -> str:
    """The source of a `citations` link offered from the Zotero library
    for a citation in `file` (`ZOTERO_STORE`)."""
    return f"{file}{SOURCE_SEPARATOR}{ZOTERO_STORE}"


# -- recording -------------------------------------------------------------------


class Scan:
    """One scan in progress. Extractors call `ran`, `source`, `node` and
    `mark`; `finish` (via `scan`/`own_scan`) writes it all at once."""

    def __init__(self, conn: Connection, scan_id: int, label: str) -> None:
        self.conn = conn
        self.id = scan_id
        self.label = label
        self._ran: list[str] = []
        self._sources: dict[tuple[str, str], str] = {}
        self._nodes: set[tuple[str, str, str]] = set()
        self._prior: dict[tuple[str, str], int | None] = {}
        self._inventory: set[str] | None = None

    # extractors report ----------------------------------------------------------

    def ran(self, extractor: str) -> None:
        if extractor not in self._ran:
            self._ran.append(extractor)

    @property
    def extractors(self) -> list[str]:
        return list(self._ran)

    def source(self, extractor: str, source: str, status: str) -> None:
        """Report one source's status in this scan. A failure reported for
        a source overrides an earlier success in the same scan (a scan
        that hit a problem in a source does not speak for it)."""
        if status not in OBSERVED | FAILED:
            raise ValueError(f"unknown source status: {status!r}")
        key = (extractor, source)
        if self._sources.get(key) in FAILED and status not in FAILED:
            return
        self._sources[key] = status

    def status_of(self, extractor: str, source: str) -> str | None:
        return self._sources.get((extractor, source))

    def sources_of(self, extractor: str) -> dict[str, str]:
        return {s: st for (e, s), st in self._sources.items() if e == extractor}

    def node(self, node_id: str, extractor: str, source: str) -> None:
        """`source`, read by `extractor` in this scan, produced `node_id`."""
        self._nodes.add((node_id, extractor, source))

    def nodes_of(self, extractor: str) -> dict[str, str]:
        """{node_id: source} this scan's `extractor` produced so far."""
        return {n: s for n, e, s in sorted(self._nodes) if e == extractor}

    def prior_scan(self, extractor: str, source: str) -> int | None:
        """The previous scan that observed this source (before this one)."""
        key = (extractor, source)
        if key not in self._prior:
            row = db.scan_source_row(self.conn, extractor, source)
            self._prior[key] = None if row is None else row["observed_scan"]
        return self._prior[key]

    def mark(self, extractor: str, source: str, basis_: dict[str, Any]) -> dict[str, Any]:
        """Keyword arguments for `db.upsert_edge` stamping a link this scan
        produced from `source` on `basis_`. Only for a source the scan
        read: a caller with a failed source upserts without a mark."""
        return {
            "scan_id": self.id,
            "scan_source": source,
            "basis": basis_,
            "prior_scan": self.prior_scan(extractor, source),
        }

    def inventory(self, inventory: Mapping[str, Iterable[str]]) -> None:
        """Record a successfully read file inventory (`git ls-files` or the
        filesystem walk): every listed file is `READ_AND_PARSED` for
        `INVENTORY` and produces the script / dataset / figure node its
        extension names; at `finish`, a file an earlier inventory listed
        and this one does not is `ABSENT` -- for the inventory and for every
        file-based extractor that ran in this scan."""
        from rce.ingest.dataflow import node_type_for_path

        files: set[str] = set()
        for paths in inventory.values():
            files.update(paths)
        self._inventory = files
        for path in files:
            self.source(INVENTORY, path, READ_AND_PARSED)
            node_type = node_type_for_path(path)
            if node_type is not None:
                self.node(f"{node_type}:{path}", INVENTORY, path)

    # finishing ---------------------------------------------------------------------

    def _absent_sources(self) -> list[tuple[str, str]]:
        if self._inventory is None:
            return []
        absent: list[tuple[str, str]] = []
        extractors = [INVENTORY, *(e for e in self._ran if e in FILE_EXTRACTORS)]
        for extractor in extractors:
            for row in db.scan_sources_of(self.conn, extractor):
                source = row["source"]
                if (extractor, source) in self._sources:
                    continue
                if file_of(source) not in self._inventory:
                    absent.append((extractor, source))
        return absent

    def finish(self, *, failed: bool = False) -> None:
        if failed:
            sources = [(e, s, st) for (e, s), st in self._sources.items() if st in FAILED]
            db.finish_scan(
                self.conn, self.id, outcome="failed", extractors=self._ran,
                sources=sorted(sources), nodes=[],
            )
            return
        for extractor, source in self._absent_sources():
            self._sources[(extractor, source)] = ABSENT
        observed = {key for key, st in self._sources.items() if st in OBSERVED}
        nodes = [(n, e, s) for n, e, s in self._nodes if (e, s) in observed]
        db.finish_scan(
            self.conn, self.id, outcome="finished", extractors=self._ran,
            sources=sorted((e, s, st) for (e, s), st in self._sources.items()),
            nodes=sorted(nodes),
        )


class PreScanReportsIndex(Exception):
    """A scan was asked of an index from before scan reports (a pre-V5
    index, schema 0001-0003). Such a project is frozen until it is migrated,
    scans included (DESIGN.md 9.12, acceptance 2026-10-05): nothing new may
    land in the old index before the migration reads its own count. The
    entry points refuse first ("migrate first"); this is the backstop."""


@contextmanager
def scan(conn: Connection, label: str) -> Iterator[Scan]:
    """Open a scan, yield it, and finish it -- as `failed` if the body
    raises (then only its failure statuses are written). An index that
    predates scan reports is never scanned (`PreScanReportsIndex`)."""
    if not db._has_scan_stamps(conn):
        raise PreScanReportsIndex(
            "this index predates V5 (it has no scan reports); the project is frozen until it is migrated, "
            "scans included -- migrate first: rce migrate"
        )
    current = Scan(conn, db.begin_scan(conn, label), label)
    try:
        yield current
    except BaseException:
        try:
            current.finish(failed=True)
        except Exception:  # noqa: BLE001 -- never mask the original failure
            logger.exception("could not record failed scan %d", current.id)
        raise
    current.finish()


@contextmanager
def own_scan(conn: Connection, given: Scan | None, label: str) -> Iterator[Scan]:
    """The caller's scan when it passed one, else a scan of this call alone."""
    if given is not None:
        yield given
        return
    with scan(conn, label) as current:
        yield current


# -- queries the judgment ledger asks (phase 4) ------------------------------------


def _key(edge: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return edge["src"], edge["dst"], edge["type"], edge["extractor"]


def edge_source(conn: Connection, edge: Mapping[str, Any]) -> str | None:
    """The source that last produced this link in a recording scan (from
    `edges`, or from the stamps an orphan cleanup kept), else None."""
    row = db.edge_scan_row(conn, *_key(edge))
    return None if row is None else row["scan_source"]


def source_status(conn: Connection, edge: Mapping[str, Any]) -> str:
    """READ_AND_PARSED | ABSENT | UNREADABLE | UNPARSEABLE as the latest scan
    of this link's source reported it; NOT_SCANNED when no recording scan
    ever produced the link or reported its source. ABSENT is an
    observation (the file is not in the inventory), like READ_AND_PARSED."""
    row = db.edge_scan_row(conn, *_key(edge))
    if row is None or row["scan_source"] is None:
        return NOT_SCANNED
    status_row = db.scan_source_row(conn, row["extractor"], row["scan_source"])
    return NOT_SCANNED if status_row is None else status_row["status"]


def produced_in_latest_scan(conn: Connection, edge: Mapping[str, Any]) -> bool | None:
    """True when the latest scan that observed this link's source produced
    it; False when that scan observed the source and did not; None when
    no scan can say (source never reported, or its latest status is a
    failure -- check `source_status` first)."""
    row = db.edge_scan_row(conn, *_key(edge))
    if row is None or row["scan_source"] is None:
        return None
    status_row = db.scan_source_row(conn, row["extractor"], row["scan_source"])
    if status_row is None or status_row["status"] not in OBSERVED:
        return None
    if row["removed"]:
        return False
    return row["scan_seen"] is not None and row["scan_seen"] >= status_row["observed_scan"]


def current_basis(conn: Connection, edge: Mapping[str, Any]) -> dict[str, Any] | None:
    """The basis the latest observing scan produced this link on, or None
    when it was not produced there (see `produced_in_latest_scan`)."""
    if not produced_in_latest_scan(conn, edge):
        return None
    row = db.edge_scan_row(conn, *_key(edge))
    assert row is not None and row["scan_basis"] is not None
    return json.loads(row["scan_basis"])


def last_basis(conn: Connection, edge: Mapping[str, Any]) -> dict[str, Any] | None:
    """The basis of the link's last production, produced now or not."""
    row = db.edge_scan_row(conn, *_key(edge))
    if row is None or row["scan_basis"] is None:
        return None
    return json.loads(row["scan_basis"])


def node_present(conn: Connection, node_id: str) -> bool | None:
    """Is `node_id` in the scan results: did the latest observing scan of
    some source that produced it produce it again? None for a node that
    exists in the index but no recording scan ever produced (an index from
    before scans were recorded); False for a node no scan produces now.
    A node an orphan cleanup deleted from the index counts as present only
    if the file inventory still lists its file -- its own extractor's
    earlier productions say nothing once that extractor removed it."""
    rows = db.node_source_rows(conn, node_id)
    in_index = db.get_node(conn, node_id) is not None
    if not rows:
        # Never produced by any recorded scan. A file's node is then known
        # absent (a file inventory lists every project file); a node with no
        # file -- an experiment, a commit -- may simply come from a store or
        # history this index never read (a rebuild without `--mlruns`), and
        # that cannot be told.
        if in_index:
            return None
        return False if node_file(node_id) is not None else None
    if not in_index:
        rows = [row for row in rows if row["extractor"] == INVENTORY]
    for row in rows:
        if row["observed_scan"] is not None and row["scan_seen"] >= row["observed_scan"]:
            return True
    return False


#: Node types whose id names a project file (`<type>:<path>[#...]`).
FILE_NODE_TYPES = frozenset({"script", "dataset", "figure", "claim", "section", "attempt"})


def node_file(node_id: str) -> str | None:
    """The project file a node's id names (`claim:paper.md#ab12` ->
    `paper.md`), or None for a node that is not a file's (experiment,
    commit, project, reference, contributor)."""
    type_, _, rest = node_id.partition(":")
    if type_ not in FILE_NODE_TYPES or not rest:
        return None
    return rest.split("#", 1)[0]


def failed_sources(conn: Connection, edge: Mapping[str, Any]) -> list[str]:
    """The sources of the link's extractor that concern one of its ends'
    files and that the latest scan could NOT read (unreadable /
    unparseable). For a link the index holds no row for -- a fresh index,
    a migrated judgment -- this is how 「来源文件暂不可读」 is still told: a
    source that could not be read cannot say the link is gone (9.6)."""
    files = {f for f in (node_file(edge["src"]), node_file(edge["dst"])) if f}
    if not files:
        return []
    return sorted(
        row["source"] for row in db.scan_sources_of(conn, edge["extractor"])
        if row["status"] in FAILED and file_of(row["source"]) in files
    )


#: Node types that ARE their file (a claim or a section is a part of one).
WHOLE_FILE_NODE_TYPES = frozenset({"script", "dataset", "figure"})


def _on_disk(node_id: str, project_root: Path | None) -> bool:
    """A node that is a whole file, and that file is on disk. The scan may
    not list it -- a git project's ignored `data/` is in no inventory, and
    such a dataset is "in the scan" only through the calls naming it -- but
    a file that is there was not renamed or removed (9.6's wording of
    「关联的一端不在本次扫描结果里」), so a removed call is 「机器不再得出这条
    关联」."""
    type_ = node_id.partition(":")[0]
    rel = node_file(node_id)
    if project_root is None or rel is None or type_ not in WHOLE_FILE_NODE_TYPES or os.path.isabs(rel):
        return False
    return os.path.lexists(Path(project_root) / rel)


def endpoints_present(
    conn: Connection, edge: Mapping[str, Any], project_root: str | Path | None = None,
) -> bool | None:
    """Are both ends of the link produced by the latest scans of THEIR
    sources (9.6: 「机器不再得出这条关联」 needs both; 「关联的一端不在本次
    扫描结果里」 is the other case)? False if either end is known absent,
    True if both are present, None if either cannot be told. With
    `project_root`, a whole-file end whose file is on disk counts as
    present (`_on_disk`)."""
    root = Path(project_root) if project_root is not None else None
    ends = [
        True if present is False and _on_disk(node, root) else present
        for node, present in ((n, node_present(conn, n)) for n in (edge["src"], edge["dst"]))
    ]
    if False in ends:
        return False
    if None in ends:
        return None
    return True


def new_links_like(conn: Connection, edge: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The candidates of 9.6 "No transfer, but a prompt", as 9.12 rules
    them: links that first appeared in the scan in which this link stopped
    being produced, with the same type and extractor, still produced now,
    and

    - for every extractor but claims: the same basis, from the same source
      or sharing an end with this link (how a renamed script's read of the
      same file -- a different source by definition -- is found);
    - for claims `backed_by`: ANY basis -- a reworded claim has, by
      construction, a different basis (its sentence) -- from a claim in the
      same file to the same experiment.

    Empty while the link is produced, or when it was never stamped. A
    candidate is a prompt and nothing else."""
    row = db.edge_scan_row(conn, *_key(edge))
    if row is None or row["scan_lost"] is None:
        return []
    claims = row["extractor"] == "claims" and row["type"] == "backed_by"
    if not claims and row["scan_basis"] is None:
        return []
    candidates = []
    for other in db.edges_appeared_in_scan(
        conn, row["scan_lost"], row["type"], row["extractor"], None if claims else row["scan_basis"],
    ):
        if _key(other) == _key(row):
            continue
        if claims:
            claim_file = node_file(row["src"])
            related = (
                claim_file is not None
                and node_file(other["src"]) == claim_file
                and other["dst"] == row["dst"]
            )
        else:
            related = (
                other["scan_source"] == row["scan_source"]
                or other["src"] == row["src"]
                or other["dst"] == row["dst"]
            )
        if related and produced_in_latest_scan(conn, other):
            candidates.append(other)
    return candidates
