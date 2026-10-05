"""Migrating what exists (DESIGN.md 9.5; task V5 phase 5): the judgments of
pre-V5 indexes moved into the record, as an explicit act.

Before V5 a confirm or reject lived only in an index keyed by a path hash,
and a path hash does not prove whose judgments they are. So nothing here
runs on open. What runs on open is discovery (`rce.paths.legacy_sources`:
the index at this path's hash, a pre-8.10 `.rce/graph.db`), reported as
"legacy records waiting"; `list_old_indexes` lists every un-retired pre-V5
index on the machine for `rce migrate --list`; `preview` shows what an
index holds and how many of its judged links' endpoints a scan of THIS
folder produces (a folder that merely reuses an old project's path shows
a low match); `decline` remembers 「这不是这个项目的」 for this folder and
leaves the index untouched; `migrate` does it, after an explicit yes.

The steps, under the project lock from first to last
----------------------------------------------------

The order 9.5 numbers is export, basis, identity, rebuild-and-verify,
retire. On disk this module writes the identity FIRST: `.rce/project.toml`
is created exclusively with `migrating_from` before any record file is
written. Writing the ledger (or `.rce/canvas.json`) into a folder that has
no identity file would make it, should the process die in between, a
folder with records and no identity -- 9.4's 「项目身份文件不见了」, which
stops and asks -- instead of a migration that resumes. So, in order:

1. identity -- created exclusively (or, on a V5 folder merging another
   machine's index, `migrating_from` raised on its own); from here every
   open treats the folder as migrating: human records are refused, the
   old index keeps serving reads, no index is built on open;
2. the fresh scan of 9.5 step 4, into `graph.db.rebuild` beside where the
   new index will live -- it is what step 2's "what a fresh scan yields"
   compares against;
3. export and basis (9.5 steps 1-2) -- every confirmed or rejected machine
   link of the old index becomes ledger entries, `via = "migrated"`,
   `migrated_from = <source>`; a reject that remembered a prior
   confirmation is two entries in order; `mapping` links are skipped. The
   basis is recorded `at-migration` only when the old index's accumulated
   occurrences, taken one by one, yield exactly one basis and it equals
   the fresh scan's; otherwise the entry records what the old index held
   (`basis_recorded = "old-index"`) and the link comes up under review --
   never re-certified. Several bases are recorded as their canonical
   texts under `basis.old_index`, which no scan can ever produce, so such
   a link waits for 「仍然成立」 whatever the script does next. Exporting
   twice adds nothing: per link, this source's own migrated entries are
   compared with what it should have written, and only a missing tail is
   appended. A link the ledger already judges the same way gets nothing;
   one it judges otherwise gets the migrated entry with `contradicts =
   <the entry it contradicts>`, and is 「记录冲突，待处理」 until the
   researcher writes a new entry -- a migration date never decides. The
   arrangement is copied to `.rce/canvas.json` only if none is there;
4. rebuild and verify -- the record files are mirrored, the ledger applied,
   and the new index reconciled against the OLD INDEX'S OWN COUNT M: each
   judged link matched by key to ledger entries and found as exactly one
   of applied / under review / under review because no scan produces it.
   Unmatched must be 0; 「来源文件暂不可读」 (judged links resting on a
   source this scan could not read, and any unreadable source at all)
   must be 0; the new index's hand-drawn links and attempt verdicts must
   equal their files (where the old index differed, that is listed as a
   stale mirror, not a failure). Otherwise nothing is retired, the
   half-built index is removed, the old index keeps serving, and what did
   not match is returned;
5. install the new index (the swap of `rce rebuild` when the folder had
   one), then retire the old index -- only if no engine answers on the
   app's port and no other process holds its `graph.db` open (「请先退出 RCE
   与 MCP 服务」) -- by renaming it into `~/.rce/graphs/.retired/<hash>-
   <date>/`; then clear `migrating_from`.

A crash at any point leaves a folder whose next `rce migrate` resumes:
step 1 is kept (the same source), leftovers of step 2 are removed, step 3
appends nothing it already appended, and a source already retired with
the new index installed only needs the flag cleared. `fault` names each
boundary so tests can stop the process there.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable

from rce import db, paths
from rce import project as project_mod
from rce import rebuild as rebuild_mod
from rce.ingest import claims as claims_ingest
from rce.ingest import pipeline
from rce.ingest import scan as scan_mod
from rce.inventory import attempt_differences, mapping_differences, verify_mirrors
from rce.records import files as record_files
from rce.records import identity as identity_mod
from rce.records import judgements
from rce.records import ledger as ledger_mod
from rce.records.identity import IdentityState, ProjectIdentity, read_identity
from rce.records.lock import HeldLock, project_lock
from rce.records.situation import classify, index_db_path, write_home
from rce.records.trust import assess_ledger

logger = logging.getLogger(__name__)

Echo = Callable[[str], None]
Fault = Callable[[str], None]

MIGRATED = "migrated"
AT_MIGRATION = "at-migration"
OLD_INDEX = "old-index"
ENGINE_PORT_ENV = "RCE_ENGINE_PORT"
PLEASE_QUIT = "请先退出 RCE 与 MCP 服务"
HUMAN_STATUSES = ("confirmed", "rejected")


class MigrationRefused(Exception):
    """Nothing to migrate, or the folder is not in a state a migration can
    start or resume from. Nothing was written."""


# -- reading an old index ------------------------------------------------------------


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """The old index, read-only: nothing this module does may change it."""
    if paths.is_dataless(db_path):
        paths._request_download(db_path)
        raise MigrationRefused(f"{db_path} is in the cloud right now; its download was requested -- try again shortly")
    uri = "file:" + urllib.parse.quote(str(db_path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class JudgedLink:
    key: tuple[str, str, str, str]
    status: str
    remembered: str | None
    evidence: dict[str, Any]


def judged_links(conn: sqlite3.Connection) -> tuple[list[JudgedLink], int]:
    """Every confirmed or rejected machine link of an old index (the M of
    9.5 step 4), and how many judged hand-drawn links were skipped."""
    rows = conn.execute(
        "SELECT src, dst, type, extractor, status, evidence FROM edges WHERE status IN ('confirmed', 'rejected') "
        "ORDER BY src, dst, type, extractor"
    ).fetchall()
    links, mappings = [], 0
    for row in rows:
        if row["extractor"] == ledger_mod.MAPPING_EXTRACTOR:
            mappings += 1
            continue
        try:
            evidence = json.loads(row["evidence"]) if row["evidence"] else {}
        except json.JSONDecodeError:
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        remembered = evidence.get("status_before_reject") if row["status"] == "rejected" else None
        links.append(JudgedLink(
            (row["src"], row["dst"], row["type"], row["extractor"]), row["status"],
            remembered if isinstance(remembered, str) else None, evidence,
        ))
    return links, mappings


@dataclass(frozen=True)
class OldIndex:
    """What an old index holds (9.5 "What the researcher is shown")."""

    key: str
    db_path: Path
    recorded_path: str | None = None
    path_exists: bool | None = None
    confirmed: int = 0
    rejected: int = 0
    remembered: int = 0
    arranged_views: int = 0
    mapping_links: int = 0
    error: str | None = None

    @property
    def judged(self) -> int:
        return self.confirmed + self.rejected

    def payload(self) -> dict[str, Any]:
        return {
            "key": self.key, "db_path": str(self.db_path), "recorded_path": self.recorded_path,
            "path_exists": self.path_exists, "confirmed": self.confirmed, "rejected": self.rejected,
            "remembered": self.remembered, "arranged_views": self.arranged_views,
            "mapping_links": self.mapping_links, "judged": self.judged, "error": self.error,
        }


def _arranged_views(directory: Path) -> int:
    try:
        data = json.loads((directory / paths.CANVAS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    views = data.get("views") if isinstance(data, dict) else None
    if not isinstance(views, dict):
        return 0
    return sum(1 for v in views.values() if isinstance(v, dict) and isinstance(v.get("positions"), dict) and v["positions"])


def read_old_index(key: str, db_path: Path) -> OldIndex:
    try:
        conn = _open_readonly(db_path)
    except (MigrationRefused, sqlite3.Error) as exc:
        return OldIndex(key, db_path, error=str(exc))
    try:
        links, mappings = judged_links(conn)
        recorded = None
        for row in conn.execute("SELECT attrs FROM nodes WHERE type = 'project'").fetchall():
            try:
                attrs = json.loads(row["attrs"] or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(attrs, dict) and isinstance(attrs.get("path"), str):
                recorded = attrs["path"]
                break
    except sqlite3.Error as exc:
        return OldIndex(key, db_path, error=str(exc))
    finally:
        conn.close()
    arranged = 0 if key == paths.IN_PROJECT_SOURCE else _arranged_views(db_path.parent)
    return OldIndex(
        key, db_path, recorded_path=recorded,
        path_exists=None if recorded is None else Path(recorded).is_dir(),
        confirmed=sum(1 for link in links if link.status == "confirmed"),
        rejected=sum(1 for link in links if link.status == "rejected"),
        remembered=sum(1 for link in links if link.remembered is not None),
        arranged_views=arranged, mapping_links=mappings,
    )


def list_old_indexes() -> list[OldIndex]:
    """`rce migrate --list`: every un-retired pre-V5 index under
    `~/.rce/graphs/` -- every directory holding a `graph.db` that is not
    a V5 index (named by a project id) and not retired, replaced or
    declined bookkeeping."""
    graphs = paths.rce_home() / paths.GRAPHS_DIRNAME
    try:
        entries = sorted(graphs.iterdir())
    except OSError:
        return []
    from rce.records.lock import PROJECT_ID_RE  # noqa: PLC0415

    found = []
    for entry in entries:
        if entry.name.startswith(".") or PROJECT_ID_RE.match(entry.name) or not (entry / paths.DB_FILENAME).exists():
            continue
        found.append(read_old_index(paths.source_key_for_dir(entry), entry / paths.DB_FILENAME))
    return found


def _source_db(root: Path, key: str) -> Path:
    db_path = paths.migration_source_db(root, key)
    if db_path is None:
        raise MigrationRefused(f"{key!r} does not name an index")
    return db_path


def sources_for(root: Path, from_dir: str | Path | None = None) -> list[tuple[str, Path]]:
    """What a migration of `root` would read: `--from <dir>`, or the
    discovered sources (path hash, in-project), never a declined one."""
    if from_dir is not None:
        directory = Path(from_dir).expanduser().resolve()
        if not (directory / paths.DB_FILENAME).exists():
            raise MigrationRefused(f"{directory} holds no {paths.DB_FILENAME}")
        key = paths.source_key_for_dir(directory)
        if key.startswith(paths.LEGACY_HASH_SOURCE_PREFIX):
            from rce.records.lock import PROJECT_ID_RE  # noqa: PLC0415

            if PROJECT_ID_RE.match(key[len(paths.LEGACY_HASH_SOURCE_PREFIX):]):
                raise MigrationRefused(f"{directory} is a V5 index, not a pre-V5 one")
        return [(key, directory / paths.DB_FILENAME)]
    return paths.legacy_sources(root)


# -- the match ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Preview:
    root: Path
    index: OldIndex
    produced: int
    scan_error: str | None = None

    @property
    def match(self) -> float | None:
        return None if not self.index.judged else self.produced / self.index.judged

    def payload(self) -> dict[str, Any]:
        return {
            **self.index.payload(), "produced": self.produced, "match": self.match, "scan_error": self.scan_error,
            "this_folder": str(self.root),
        }


def _scratch_scan(root: Path, echo: Echo) -> tuple[Path, str | None]:
    """A throwaway index of this folder's scan, for the match only."""
    directory = paths.rce_home() / paths.GRAPHS_DIRNAME / f".scratch-{secrets.token_hex(6)}"
    directory.mkdir(parents=True)
    db_path = directory / paths.DB_FILENAME
    conn = db.connect(db_path)
    error = None
    try:
        db.migrate(conn)
        try:
            pipeline.ingest_sources(conn, root, echo=echo, apply=False)
        except pipeline.IngestFailed as exc:
            error = str(exc)
        pipeline.ingest_records(conn, root, echo=echo, apply=False)
    finally:
        conn.close()
    return db_path, error


def preview(root: str | Path, *, from_dir: str | Path | None = None, echo: Echo = lambda _l: None) -> list[Preview]:
    """What waits for this folder and how well it matches (reads the
    project and the old indexes; writes only a scratch index it removes)."""
    root = Path(root)
    sources = sources_for(root, from_dir)
    if not sources:
        return []
    indexes = [read_old_index(k, p) for k, p in sources]
    scratch, error = _scratch_scan(root, echo)
    try:
        conn = db.connect(scratch)
        try:
            out = []
            for index in indexes:
                produced = 0
                if index.error is None:
                    old = _open_readonly(index.db_path)
                    try:
                        links, _m = judged_links(old)
                    finally:
                        old.close()
                    produced = sum(
                        1 for link in links
                        if db.get_node(conn, link.key[0]) is not None and db.get_node(conn, link.key[1]) is not None
                    )
                out.append(Preview(root, index, produced, error))
            return out
        finally:
            conn.close()
    finally:
        shutil.rmtree(scratch.parent, ignore_errors=True)


def decline(root: str | Path, *, from_dir: str | Path | None = None, keys: Iterable[str] | None = None) -> list[str]:
    """「这不是这个项目的」: remember, for this folder, that these indexes are
    not its own; the indexes are untouched. Returns the keys declined."""
    root = Path(root)
    chosen = list(keys) if keys is not None else [k for k, _p in sources_for(root, from_dir)]
    got = read_identity(root)
    pid = got.identity.id if got.state is IdentityState.PRESENT and got.identity else None
    with project_lock(root, pid):
        for key in chosen:
            paths.decline_source(root, key)
    logger.warning("RCE: %s declined pre-V5 index(es) %s (left untouched)", root, ", ".join(chosen))
    return chosen


# -- basis (9.5 step 2) ------------------------------------------------------------------------


def _occurrences(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    occ = evidence.get("occurrences")
    if isinstance(occ, list):
        return [o for o in occ if isinstance(o, dict)]
    return []


def occurrence_bases(old: sqlite3.Connection, link: JudgedLink) -> list[dict[str, Any]] | None:
    """The basis each accumulated occurrence of the old index's evidence
    yields, one per occurrence (None when an occurrence does not say
    enough to compute one: then no single basis can be claimed)."""
    src, _dst, type_, extractor = link.key
    occurrences = _occurrences(link.evidence)
    if extractor in ("dataflow", "pyfig"):
        out = []
        for o in occurrences:
            callee, file = o.get("callee"), o.get("file")
            if not isinstance(callee, str) or not callee:
                return None
            language = "python" if not isinstance(file, str) or file.lower().endswith(".py") else "r"
            out.append(scan_mod.basis(extractor, type_, call=scan_mod.bare_call_name(callee, language=language)))
        return out or None
    if extractor in ("mlflow", "wandb") and type_ == "produces":
        name = "artifact_path" if extractor == "mlflow" else "file_name"
        out = []
        for o in occurrences:
            if not isinstance(o.get(name), str):
                return None
            out.append(scan_mod.basis(extractor, type_, artifact=o[name]))
        return out or None
    if extractor == "claims" and type_ == "backed_by":
        row = old.execute("SELECT attrs FROM nodes WHERE id = ?", (src,)).fetchone()
        try:
            attrs = json.loads(row["attrs"]) if row is not None else None
        except json.JSONDecodeError:
            attrs = None
        if not isinstance(attrs, dict) or not isinstance(attrs.get("sentence"), str) or not isinstance(attrs.get("precision_decimals"), int):
            return None
        out = []
        for o in occurrences:
            if not isinstance(o.get("metric"), str) or not isinstance(o.get("metric_value"), (int, float)) or not isinstance(o.get("claim_raw"), str):
                return None
            out.append(scan_mod.basis(
                "claims", "backed_by", sentence=claims_ingest._normalize_for_id(attrs["sentence"]), number=o["claim_raw"],
                metrics={o["metric"]: scan_mod.rounded(o["metric_value"], attrs["precision_decimals"])},
            ))
        return out or None
    return [{}]


def migration_basis(old: sqlite3.Connection, fresh: sqlite3.Connection, link: JudgedLink) -> tuple[dict[str, Any], str]:
    """(basis, basis_recorded) for a migrated entry (module docstring)."""
    bases = occurrence_bases(old, link)
    distinct = sorted({db.canonical_basis(b) or "{}" for b in bases}) if bases is not None else None
    if distinct is not None and len(distinct) == 1:
        only = json.loads(distinct[0])
        now = scan_mod.current_basis(fresh, dict(zip(("src", "dst", "type", "extractor"), link.key)))
        if now is not None and db.canonical_basis(now) == distinct[0]:
            return only, AT_MIGRATION
        return only, OLD_INDEX
    if distinct is None:
        distinct = sorted(json.dumps(o, sort_keys=True, ensure_ascii=False) for o in _occurrences(link.evidence))
    return {"old_index": distinct}, OLD_INDEX


# -- export (9.5 step 1) ------------------------------------------------------------------------


def _desired(link: JudgedLink) -> list[str]:
    if link.status == "rejected" and link.remembered == "confirmed":
        return ["confirmed", "rejected"]
    return [link.status]


@dataclass
class Exported:
    appended: int = 0
    contradicted: int = 0
    already: int = 0
    copied_arrangement: bool = False
    problems: list[str] = field(default_factory=list)


def _ledger_now(root: Path) -> ledger_mod.Ledger:
    loaded = ledger_mod.load_judgements(root)
    if loaded.state is record_files.RecordState.ABSENT:
        return ledger_mod.parse_ledger("", ledger_mod.JUDGEMENT_SCHEMA)
    if loaded.ledger is None:
        raise MigrationRefused(f"the judgment ledger cannot be read ({loaded.state.value}: {loaded.error}); repair it first")
    return loaded.ledger


def export(
    root: Path,
    key: str,
    old: sqlite3.Connection,
    fresh: sqlite3.Connection,
    links: list[JudgedLink],
    *,
    lock: HeldLock,
    identity: ProjectIdentity,
    applied: dict[str, Any],
    fault: Fault | None = None,
) -> Exported:
    """Steps 1-2 (module docstring). The caller holds the id's lock."""
    loaded = ledger_mod.load_judgements(root)
    decision = assess_ledger(identity, loaded, applied, for_migration=True)
    if not decision.may_write:
        raise MigrationRefused(
            f"the judgment ledger may not be written now ({decision.reason}: {decision.detail or decision.message}); "
            f"nothing was exported"
        )
    out = Exported()
    create = decision.may_create
    for link in links:
        ledger = _ledger_now(root)
        state = ledger.state(link.key)
        mine = [e for e in state.history if e.get("via") == MIGRATED and e.get("migrated_from") == key]
        desired = _desired(link)
        done = [e.get("verdict") for e in mine]
        if done == desired:
            out.already += 1
            continue
        if done and done != desired[: len(done)]:
            out.problems.append(f"{link.key}: this source's earlier entries ({done}) do not match the old index ({desired})")
            continue
        if not done and state.history:
            standing = ledger_mod.judgement_status(state)
            if standing == link.status:
                out.already += 1
                continue
            target = state.history[-1].id
            to_write = [(link.status, target)]
            out.contradicted += 1
        else:
            to_write = [(v, None) for v in desired[len(done):]]
        basis, recorded = migration_basis(old, fresh, link)
        for verdict, contradicts in to_write:
            fields = {
                "verdict": verdict, "src": link.key[0], "dst": link.key[1], "type": link.key[2],
                "extractor": link.key[3], "via": MIGRATED, "migrated_from": key, "contradicts": contradicts,
                "basis_recorded": recorded, "basis": basis,
            }
            try:
                ledger_mod.append(
                    ledger_mod.judgements_path(root), ledger_mod.JUDGEMENT_SCHEMA, fields,
                    lock=lock, project_root=root, create=create,
                )
            except ledger_mod.LedgerWriteRefused as exc:
                raise MigrationRefused(f"could not export {link.key}: {exc}") from exc
            create = False
            out.appended += 1
            if fault is not None:
                fault(f"export:{out.appended}")
    return out


def copy_arrangement(root: Path, old_dir: Path) -> bool:
    """The old index's arrangement becomes `.rce/canvas.json`, only if none
    is there (created exclusively: an arrangement is never overwritten)."""
    from rce.webapp import canvas as canvas_mod  # noqa: PLC0415

    source = old_dir / paths.CANVAS_FILENAME
    target = canvas_mod.canvas_record_path(root)
    if os.path.lexists(target) or not source.is_file():
        return False
    data = source.read_bytes()
    try:
        json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    record_files.ensure_dir_within(root, target.parent)
    tmp = record_files.temp_path_for(target)
    try:
        record_files.write_new_file(tmp, data)
        try:
            os.link(tmp, target)
        except FileExistsError:
            return False
        record_files._fsync_dir(target.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return True


# -- verify (9.5 step 4) -----------------------------------------------------------------------


@dataclass
class Tally:
    """The reconciliation, from the old index's own count M."""

    m: int = 0
    applied: int = 0
    review_not_produced: int = 0
    review_other: int = 0
    unreadable: int = 0
    unmatched: list[str] = field(default_factory=list)
    unreadable_sources: list[str] = field(default_factory=list)
    skipped_mappings: int = 0
    contradicted: int = 0
    mirror_failures: list[str] = field(default_factory=list)
    stale_mirror: list[str] = field(default_factory=list)

    @property
    def balanced(self) -> bool:
        return (
            not self.unmatched and self.unreadable == 0 and not self.unreadable_sources and not self.mirror_failures
            and self.applied + self.review_not_produced + self.review_other == self.m
        )

    def payload(self) -> dict[str, Any]:
        return {
            "m": self.m, "applied": self.applied, "review_not_produced": self.review_not_produced,
            "review_other": self.review_other, "unreadable": self.unreadable, "unmatched": self.unmatched,
            "unreadable_sources": self.unreadable_sources, "skipped_mappings": self.skipped_mappings,
            "contradicted": self.contradicted, "mirror_failures": self.mirror_failures,
            "stale_mirror": self.stale_mirror, "balanced": self.balanced,
        }

    def lines(self) -> list[str]:
        out = [
            f"Old index: {self.m} judged link(s) (hand-drawn links skipped: {self.skipped_mappings})",
            f"  applied:                                  {self.applied}",
            f"  under review:                             {self.review_other}"
            + (f" (incl. {self.contradicted} record conflict(s))" if self.contradicted else ""),
            f"  under review, no scan produces it now:    {self.review_not_produced}",
            f"  来源文件暂不可读 (source not readable):    {self.unreadable}",
            f"  unmatched:                                {len(self.unmatched)}",
        ]
        out += [f"    unmatched: {u}" for u in self.unmatched]
        out += [f"    source not readable: {s}" for s in self.unreadable_sources]
        out += [f"    mirror differs from its file: {m}" for m in self.mirror_failures]
        if self.stale_mirror:
            out.append(f"  stale mirror in the old index (not a failure): {len(self.stale_mirror)}")
            out += [f"    {s}" for s in self.stale_mirror]
        return out


def reconcile(
    root: Path, old: sqlite3.Connection, fresh: sqlite3.Connection, links: list[JudgedLink],
    skipped: int, unreadable_sources: list[str], contradicted: int,
) -> Tally:
    tally = Tally(m=len(links), skipped_mappings=skipped, unreadable_sources=list(unreadable_sources), contradicted=contradicted)
    ledger = _ledger_now(root)
    states = db.judgement_states(fresh)
    for link in links:
        label = f"{link.key[0]} --{link.key[2]}--> {link.key[1]} ({link.key[3]})"
        state = ledger.state(link.key)
        if not state.history:
            tally.unmatched.append(f"{label}: no ledger entry")
            continue
        item = states.get(link.key)
        if item is None or item["outcome"] == "not_in_index":
            tally.unmatched.append(f"{label}: not found in the new index")
            continue
        outcome, reason = item["outcome"], item.get("reason")
        if outcome == "held" or (item.get("detail") or {}).get("source_unreadable"):
            tally.unreadable += 1
        elif outcome == "applied":
            if ledger_mod.judgement_status(state) != link.status:
                tally.unmatched.append(f"{label}: the ledger says {ledger_mod.judgement_status(state)}, the old index {link.status}")
            else:
                tally.applied += 1
        elif outcome == "review" and reason in (judgements.NOT_PRODUCED, judgements.ENDPOINT_GONE):
            tally.review_not_produced += 1
        elif outcome in ("review", "conflict"):
            tally.review_other += 1
        else:
            tally.unmatched.append(f"{label}: {outcome}")
    tally.mirror_failures = verify_mirrors(fresh, root)
    tally.stale_mirror = [*(mapping_differences(old, root) or []), *(attempt_differences(old, root) or [])]
    return tally


# -- retire (9.5 step 5) ------------------------------------------------------------------------


def engine_running() -> bool:
    """Whether an engine answers on the app's port (8.9: every real
    install uses one fixed port). `RCE_ENGINE_PORT` overrides it; `0`
    turns the probe off (the test suite never touches a real engine)."""
    from rce.webapp import macapp  # noqa: PLC0415

    raw = os.environ.get(ENGINE_PORT_ENV)
    try:
        port = int(raw) if raw is not None else macapp.DEFAULT_PORT
    except ValueError:
        port = macapp.DEFAULT_PORT
    if port <= 0:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def holders(files: Iterable[Path]) -> set[int] | None:
    """PIDs of OTHER processes holding any of `files` open; None when that
    cannot be told (then nothing is retired)."""
    existing = [str(f) for f in files if f.exists()]
    if not existing:
        return set()
    me = os.getpid()
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None)
    if lsof:
        try:
            done = subprocess.run([lsof, "-t", "--", *existing], capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            done = None
        if done is not None and done.returncode in (0, 1):
            # lsof exits 1 when nothing holds the files (and 0 with the pids).
            pids = {int(x) for x in done.stdout.split() if x.strip().isdigit()}
            return {p for p in pids if p != me}
    proc = Path("/proc")
    if proc.is_dir():
        wanted = {os.path.realpath(f) for f in existing}
        found = set()
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == me:
                continue
            try:
                for fd in (entry / "fd").iterdir():
                    try:
                        if os.path.realpath(fd) in wanted:
                            found.add(int(entry.name))
                    except OSError:
                        continue
            except OSError:
                continue
        return found
    return None


def retired_dir_for(key: str, root: Path) -> Path:
    base = paths.rce_home() / paths.GRAPHS_DIRNAME / paths.RETIRED_DIRNAME
    if key.startswith(paths.LEGACY_HASH_SOURCE_PREFIX):
        name = key[len(paths.LEGACY_HASH_SOURCE_PREFIX):]
    elif key == paths.IN_PROJECT_SOURCE:
        name = f"{paths.canonical_path_hash(root)}-in-project"
    else:
        name = Path(key).name
    stem = f"{name}-{date.today().isoformat()}"
    target, n = base / stem, 1
    while os.path.lexists(target):
        n += 1
        target = base / f"{stem}-{n}"
    return target


def _move(source: Path, target: Path) -> None:
    try:
        os.rename(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(source), str(target))


def retire(root: Path, key: str, db_path: Path, *, engine_probe: Callable[[], bool] = engine_running,
           holder_probe: Callable[[Iterable[Path]], set[int] | None] = holders) -> Path:
    """Rename the old index into `.retired/` (never delete it), only when no
    engine answers and no other process holds it open."""
    sidecars = [db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")]
    if engine_probe():
        raise MigrationRefused(f"{PLEASE_QUIT}: an RCE engine is running and may still be using {db_path}")
    held = holder_probe(sidecars)
    if held is None:
        raise MigrationRefused(f"{PLEASE_QUIT}: whether another process holds {db_path} open cannot be told here")
    if held:
        raise MigrationRefused(f"{PLEASE_QUIT}: process(es) {', '.join(map(str, sorted(held)))} hold {db_path} open")
    target = retired_dir_for(key, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if key == paths.IN_PROJECT_SOURCE:
        target.mkdir()
        for f in sidecars:
            if f.exists():
                _move(f, target / f.name)
    else:
        _move(db_path.parent, target)
    record_files._fsync_dir(target.parent)
    return target


# -- migrate -----------------------------------------------------------------------------------


@dataclass
class Migrated:
    root: Path
    key: str | None
    ok: bool
    identity: ProjectIdentity | None = None
    tally: Tally | None = None
    exported: Exported | None = None
    retired_to: Path | None = None
    stopped: str | None = None
    previews: list[Preview] = field(default_factory=list)
    resumed: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "key": self.key, "project_id": self.identity.id if self.identity else None,
            "tally": self.tally.payload() if self.tally else None,
            "appended": self.exported.appended if self.exported else 0,
            "retired_to": str(self.retired_to) if self.retired_to else None,
            "stopped": self.stopped, "resumed": self.resumed,
            "previews": [p.payload() for p in self.previews],
        }


def _start_identity(root: Path, key: str) -> tuple[ProjectIdentity, bool]:
    """Step 1 on disk: the identity with `migrating_from` (module docstring).
    Returns (identity, resumed). Hold the path lock."""
    got = read_identity(root)
    if got.state is IdentityState.ABSENT:
        return identity_mod.create_identity(root, migrating_from=key), False
    if got.state is not IdentityState.PRESENT or got.identity is None:
        raise MigrationRefused(f"the project identity file of {root} cannot be read ({got.state.value})")
    ident = got.identity
    if ident.migrating_from is None:
        return identity_mod.set_flag(root, ident, "migrating_from", key), False
    if ident.migrating_from != key:
        raise MigrationRefused(
            f"{root} is still migrating from {ident.migrating_from}; finish that first ('rce migrate' resumes it)"
        )
    return ident, True


def _finish(root: Path, ident: ProjectIdentity) -> ProjectIdentity:
    current = read_identity(root).identity
    if current is not None and current.migrating_from is not None:
        return identity_mod.set_flag(root, current, "migrating_from", None)
    return ident


def migrate_one(
    root: Path,
    key: str,
    *,
    echo: Echo = lambda _l: None,
    fault: Fault | None = None,
    engine_probe: Callable[[], bool] = engine_running,
    holder_probe: Callable[[Iterable[Path]], set[int] | None] = holders,
) -> Migrated:
    """Migrate one source into `root` (module docstring, steps 1-5)."""
    fault = fault or (lambda _p: None)
    source_db = _source_db(root, key)
    c = classify(root)
    if c.blocked:
        raise project_mod.ProjectBlocked(c)
    with project_lock(root, None), (project_lock(root, c.project_id) if c.project_id else _null()):
        again = classify(root)
        if again.blocked:
            raise project_mod.ProjectBlocked(again)
        ident, resumed = _start_identity(root, key)
        fault("after_identity")
        with project_lock(root, ident.id) as held:
            target = index_db_path(ident.id)
            if not source_db.exists():
                if resumed and target.exists():
                    ident = _finish(root, ident)
                    return Migrated(root, key, ok=True, identity=ident, resumed=True,
                                    stopped=None, retired_to=None)
                raise MigrationRefused(f"the old index {source_db} is not on this machine; nothing to resume from")
            if not target.exists():
                write_home(ident.id, root)  # before graph.db exists, as every index does
            staging = rebuild_mod.staging_path(target)
            rebuild_mod.remove_database(staging)
            old = _open_readonly(source_db)
            fresh = db.connect(staging)
            try:
                db.migrate(fresh)
                project_mod._upsert_project_node(fresh, root, ident)
                links, skipped = judged_links(old)
                producers = rebuild_mod.producers_of(old, root)
                report = rebuild_mod.build_index(fresh, root, ident, producers=producers, echo=echo, apply=False)
                fault("after_scan")
                exported = export(
                    root, key, old, fresh, links, lock=held, identity=ident,
                    applied=_applied_copy(target, root), fault=fault,
                )
                if source_db.parent.name != paths.RCE_DIRNAME:
                    exported.copied_arrangement = copy_arrangement(root, source_db.parent)
                current = read_identity(root).identity
                if exported.appended and current is not None and not current.ledger:
                    ident = identity_mod.set_flag(root, current, "ledger", True)
                else:
                    ident = current or ident
                fault("after_export")
                judgements.apply_ledger(fresh, root, identity=ident, for_migration=True)
                unreadable = list(report.unreadable)
                if report.ingest_error:
                    unreadable.append(f"the scan failed: {report.ingest_error}")
                unreadable += rebuild_mod.evicted_sources(fresh, root)
                tally = reconcile(root, old, fresh, links, skipped, unreadable, exported.contradicted)
                tally.unmatched += exported.problems
            finally:
                old.close()
                fresh.close()
            if not tally.balanced:
                rebuild_mod.remove_database(staging)
                return Migrated(root, key, ok=False, identity=ident, tally=tally, exported=exported, resumed=resumed,
                                stopped="the tally did not balance; nothing was retired and the old index keeps serving")
            fault("after_verify")
            rebuild_mod.install(staging, target)
            fault("after_install")
            try:
                retired = retire(root, key, source_db, engine_probe=engine_probe, holder_probe=holder_probe)
            except MigrationRefused as exc:
                return Migrated(root, key, ok=False, identity=ident, tally=tally, exported=exported, resumed=resumed,
                                stopped=str(exc))
            fault("after_retire")
            ident = _finish(root, ident)
    _follow_registry(root, ident)
    logger.warning("RCE: migrated %s into %s; the old index was retired to %s", key, root, retired)
    return Migrated(root, key, ok=True, identity=ident, tally=tally, exported=exported, retired_to=retired, resumed=resumed)


def _follow_registry(root: Path, ident: ProjectIdentity) -> None:
    """A pre-V5 project registered by path now has an id: its entry is
    replaced by one keyed by the id (no recency bump for an unregistered
    folder -- migrating is not serving)."""
    from rce.webapp import registry  # noqa: PLC0415 -- the webapp imports this module

    canonical = paths._canonical_path(root)
    for entry in registry.load():
        if entry.get("id") is None and paths._canonical_path(entry["path"]) == canonical:
            registry.register(Path(canonical), ident.id)
            return


class _null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def _applied_copy(target: Path, root: Path) -> dict[str, Any]:
    """The current V5 index's copy of the entries it applied (empty for a
    folder that had no V5 index): the shrink check before exporting."""
    if not target.exists():
        return {}
    conn = db.connect(target)
    try:
        db.migrate(conn)
        return judgements.applied_copy(conn, ledger_mod.load_judgements(root))
    finally:
        conn.close()


def pending_key(root: Path) -> str | None:
    """The source a folder is still migrating from, if any."""
    got = read_identity(root)
    if got.state is IdentityState.PRESENT and got.identity is not None:
        return got.identity.migrating_from
    return None


def migrate(
    root: str | Path,
    *,
    yes: bool = False,
    from_dir: str | Path | None = None,
    echo: Echo = lambda _l: None,
    fault: Fault | None = None,
    engine_probe: Callable[[], bool] = engine_running,
    holder_probe: Callable[[Iterable[Path]], set[int] | None] = holders,
) -> list[Migrated]:
    """`rce migrate` / `POST /api/migration/run`: resume an unfinished
    migration, or -- after the explicit `yes` -- migrate every waiting
    source in turn. Without `yes` (and nothing to resume) returns one
    result carrying the previews and writes nothing."""
    root = Path(root)
    if not root.is_dir():
        raise MigrationRefused(f"{root} is not an existing folder")
    resume = pending_key(root)
    if resume is not None:
        keys = [resume]
    else:
        sources = sources_for(root, from_dir)
        if not sources:
            raise MigrationRefused(f"no pre-V5 index waits for {root} ('rce migrate --list' shows every old index)")
        if not yes:
            return [Migrated(root, None, ok=False, previews=preview(root, from_dir=from_dir, echo=echo),
                             stopped="not migrated: shown for confirmation (pass --yes)")]
        keys = [k for k, _p in sources]
    results = []
    for key in keys:
        result = migrate_one(root, key, echo=echo, fault=fault, engine_probe=engine_probe, holder_probe=holder_probe)
        results.append(result)
        if not result.ok:
            break
    return results


def waiting_payload(root: Path) -> dict[str, Any]:
    """For status and summary: the sources waiting, and an unfinished
    migration. Cheap (no scan)."""
    try:
        sources = paths.legacy_sources(root)
    except OSError:
        sources = []
    return {
        "waiting": [{"key": k, "db_path": str(p)} for k, p in sources],
        "migrating_from": pending_key(root),
    }
