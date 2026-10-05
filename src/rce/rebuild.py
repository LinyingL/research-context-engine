"""`rce rebuild` (DESIGN.md 9.8; task V5 phase 5): build a fresh index
beside the current one, apply the record, compare human state per link,
and only then swap -- keeping the previous index one generation.

Why a rebuild must prove itself
-------------------------------

The index is derived; deleting it loses nothing (9.1). That is a claim,
and a rebuild is where it is tested: the new index is built from the
project's sources and its record only -- every producer the old index had
links from (the full scan; the MLflow store and W&B project it read; the
attempt table, its consistency check, the hand-drawn links) -- and then,
per link, the human state before and after is compared:

- a judgment APPLIED before and not applied after is a **failure** unless
  the source it rests on demonstrably changed (the new scan observed that
  source and no longer produces the link, or produces it on another
  basis). On unchanged sources the record alone must reproduce every
  judgment the index showed;
- the hand-drawn links and attempt verdicts of the new index must equal
  their files (`rce.inventory.verify_mirrors`);
- any source the rebuild scan could not read (`unreadable`, or a file
  evicted to the cloud) **blocks** the swap and is listed: an evicted file
  must not be mistaken for a vanished link.

A rebuild that fails or is blocked swaps nothing; the half-built index is
removed and the current one keeps serving.

The swap, and why a crash leaves one index or the other
-------------------------------------------------------

The new database is built at `graph.db.rebuild` beside `graph.db` (same
directory, same filesystem), checkpointed and switched to a rollback
journal so it carries no sidecar of its own. The current database is
checkpointed (its WAL emptied), then hard-linked as `graph.db.prev` --
one generation, replacing any older one -- and only then is the new file
renamed over `graph.db` (`os.replace`, atomic). A crash before the rename
leaves the old index under both names; after it, the new one is in place
and the old one is `graph.db.prev`. At no point is there neither. Stale
`-wal`/`-shm` names of the replaced database are removed after the
rename; a current WAL that still holds frames after the checkpoint (some
other process writing outside the lock) refuses the swap rather than let
those frames be replayed onto the new file.

Everything runs under the project lock with the identity re-checked
(`write_guard`, an index write), so no scan or record write interleaves.
A pre-V5 project, or one whose migration has not finished, is refused:
its record is not complete yet, and `rce migrate` is the rebuild that
applies to it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from rce import consistency, db, paths
from rce.ingest import attempts as attempts_ingest
from rce.ingest import pipeline
from rce.ingest import scan as scan_mod
from rce.inventory import verify_mirrors
from rce.records import judgements
from rce.records.identity import IdentityState, ProjectIdentity, read_identity
from rce.records.situation import READ_NOW, index_db_path, write_guard

logger = logging.getLogger(__name__)

STAGING_SUFFIX = ".rebuild"
PREVIOUS_SUFFIX = ".prev"
CONSISTENCY_EXTRACTOR = "attempts_consistency"

Echo = Callable[[str], None]
Fault = Callable[[str], None]


class RebuildRefused(Exception):
    """The project cannot be rebuilt now (pre-V5, migrating, no index, no
    readable identity). Nothing was written."""


# -- producers ---------------------------------------------------------------------


@dataclass(frozen=True)
class Producers:
    """What the old index read beyond the project's own files, so the fresh
    one reads it too. `problems` lists what cannot be reproduced."""

    mlruns: str | None = None
    wandb: str | None = None
    consistency: bool = False
    problems: tuple[str, ...] = ()


def producers_of(conn: sqlite3.Connection, project_root: Path) -> Producers:
    """Read from the index's scan reports (and its links, for an index whose
    scans were not recorded) which tracking stores and checks produced it."""
    sources = db.all_scan_sources(conn)
    extractors = {e["extractor"] for e in db.query_edges(conn)}
    mlflow = sorted({s["source"] for s in sources if s["extractor"] == "mlflow"})
    wandb = sorted({s["source"] for s in sources if s["extractor"] == "wandb"})
    problems: list[str] = []
    mlruns: str | None = None
    if len(mlflow) > 1:
        problems.append(f"the index read {len(mlflow)} MLflow stores ({', '.join(mlflow)}); a rebuild reads one")
    elif mlflow:
        location = mlflow[0].split(":", 1)[1]
        mlruns = str(Path(location) if os.path.isabs(location) else Path(project_root) / location)
    elif "mlflow" in extractors and not (Path(project_root) / "mlruns").is_dir():
        problems.append("the index holds MLflow links but does not say which store produced them")
    wandb_project: str | None = None
    if len(wandb) > 1:
        problems.append(f"the index read {len(wandb)} W&B projects; a rebuild reads one")
    elif wandb:
        wandb_project = wandb[0].split(":", 1)[1]
    elif "wandb" in extractors:
        problems.append("the index holds W&B links but does not say which project produced them")
    check = CONSISTENCY_EXTRACTOR in extractors or any(s["extractor"] == CONSISTENCY_EXTRACTOR for s in sources)
    return Producers(mlruns=mlruns, wandb=wandb_project, consistency=check, problems=tuple(problems))


# -- building ------------------------------------------------------------------------


@dataclass
class BuildReport:
    warnings: int = 0
    ingest_error: str | None = None
    unreadable: list[str] = field(default_factory=list)
    unparseable: list[str] = field(default_factory=list)
    applied: judgements.ApplyResult | None = None


def _source_label(row: dict[str, Any]) -> str:
    return f"{row['extractor']}: {scan_mod.file_of(row['source'])}"


def build_index(
    conn: sqlite3.Connection,
    project_root: Path,
    identity: ProjectIdentity | None,
    *,
    producers: Producers,
    echo: Echo = lambda _l: None,
    for_migration: bool = False,
    apply: bool = True,
) -> BuildReport:
    """Scan everything into the (fresh, migrated) index `conn`: the sources,
    the consistency check when the old index had one, the record files;
    then apply the judgment ledger once (`apply=False`: the caller does).
    The caller holds the project lock."""
    report = BuildReport()
    try:
        report.warnings = pipeline.ingest_sources(
            conn, project_root, mlruns=producers.mlruns, wandb=producers.wandb, echo=echo, apply=False,
        )
    except pipeline.IngestFailed as exc:
        report.ingest_error = str(exc)
    pipeline.ingest_records(conn, project_root, echo=echo, apply=False)
    if producers.consistency:
        try:
            config = attempts_ingest.load_config(project_root)
        except attempts_ingest.AttemptsConfigError as exc:
            echo(f"  attempts --check: not run ({exc})")
        else:
            consistency.run_checks(conn, project_root, config)
    for row in db.all_scan_sources(conn):
        if row["status"] == scan_mod.UNREADABLE:
            report.unreadable.append(_source_label(row))
        elif row["status"] == scan_mod.UNPARSEABLE:
            report.unparseable.append(_source_label(row))
    if apply and identity is not None:
        report.applied = judgements.apply_ledger(conn, project_root, identity=identity, for_migration=for_migration)
    return report


def evicted_sources(conn: sqlite3.Connection, project_root: Path) -> list[str]:
    """Project files the old index read that macOS has evicted to the cloud
    right now: a scan would block on them, or read them as gone."""
    found = []
    for row in db.all_scan_sources(conn):
        if row["extractor"] in scan_mod.FILE_EXTRACTORS | {"attempts", "mapping", scan_mod.INVENTORY}:
            rel = scan_mod.file_of(row["source"])
            if rel and not os.path.isabs(rel) and paths.is_dataless(Path(project_root) / rel):
                found.append(f"{row['extractor']}: {rel} (in the cloud)")
    return sorted(set(found))


# -- comparing -------------------------------------------------------------------------


def _source_changed(old: sqlite3.Connection, new: sqlite3.Connection, key: tuple[str, str, str, str]) -> bool:
    """Did the link's source demonstrably change: the new scan observed the
    source the OLD index says produced the link, and no longer produces it,
    or produces it on another basis?"""
    edge = dict(zip(("src", "dst", "type", "extractor"), key))
    old_row = db.edge_scan_row(old, *key)
    if old_row is None or old_row["scan_source"] is None:
        return False
    status = db.scan_source_row(new, key[3], old_row["scan_source"])
    if status is None or status["status"] not in scan_mod.OBSERVED:
        return False
    if not scan_mod.produced_in_latest_scan(new, edge):
        return True
    now = scan_mod.current_basis(new, edge)
    return db.canonical_basis(now) != old_row["scan_basis"]


def compare(old: sqlite3.Connection, new: sqlite3.Connection) -> tuple[list[str], dict[str, int]]:
    """Per link, the human state before and after (module docstring).
    Returns the failures (English) and a tally."""
    before = db.judgement_states(old)
    after = db.judgement_states(new)
    failures: list[str] = []
    tally = {"judged": len(before), "applied_before": 0, "applied_after": 0, "changed_source": 0}
    for key, item in sorted(before.items()):
        if item["outcome"] != "applied":
            continue
        tally["applied_before"] += 1
        now = after.get(key)
        if now is not None and now["outcome"] == "applied" and now.get("verdict") == item.get("verdict"):
            tally["applied_after"] += 1
            continue
        if _source_changed(old, new, key):
            tally["changed_source"] += 1
            continue
        shown = "absent" if now is None else f"{now['outcome']} ({now.get('reason')})"
        failures.append(
            f"{key[0]} --{key[2]}--> {key[1]} ({key[3]}): {item.get('verdict')} applied before, {shown} after, "
            f"on an unchanged source"
        )
    tally["applied_after_total"] = sum(1 for s in after.values() if s["outcome"] == "applied")
    return failures, tally


# -- swapping ---------------------------------------------------------------------------


def staging_path(target: Path) -> Path:
    return target.with_name(target.name + STAGING_SUFFIX)


def previous_path(target: Path) -> Path:
    return target.with_name(target.name + PREVIOUS_SUFFIX)


def _sidecars(path: Path) -> tuple[Path, Path]:
    return path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")


def remove_database(path: Path) -> None:
    """Remove a database file and its sidecars (a half-built index)."""
    for p in (path, *_sidecars(path)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


class SwapRefused(Exception):
    """The current index could not be emptied of its WAL; nothing swapped."""


def _seal(path: Path) -> None:
    """Checkpoint `path` and leave it in rollback-journal mode with no
    sidecar of its own."""
    raw = sqlite3.connect(path)
    try:
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw.execute("PRAGMA journal_mode = DELETE")
    finally:
        raw.close()


def install(staging: Path, target: Path, *, fault: Fault | None = None) -> Path | None:
    """Swap the fresh database `staging` into place at `target` (module
    docstring, "The swap"); returns `graph.db.prev` when there was a
    database to keep. The caller holds the project lock."""
    _seal(staging)
    previous: Path | None = None
    if target.exists():
        raw = sqlite3.connect(target)
        try:
            raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            raw.close()
        wal = _sidecars(target)[0]
        if wal.exists() and wal.stat().st_size > 0:
            raise SwapRefused(
                f"{target} is still being written by another process (its WAL is not empty); "
                f"quit RCE and MCP servers and try again"
            )
        previous = previous_path(target)
        remove_database(previous)
        os.link(target, previous)
        if fault is not None:
            fault("before_swap")
    os.replace(staging, target)
    for side in _sidecars(target):
        try:
            side.unlink()
        except FileNotFoundError:
            pass
    paths_fsync_dir(target.parent)
    return previous


def paths_fsync_dir(directory: Path) -> None:
    from rce.records import files  # noqa: PLC0415 -- leaf use

    files._fsync_dir(directory)


# -- rce rebuild ----------------------------------------------------------------------------


@dataclass
class Rebuilt:
    db_path: Path
    swapped: bool
    previous: Path | None = None
    blocked: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    tally: dict[str, int] = field(default_factory=dict)
    report: BuildReport | None = None

    @property
    def ok(self) -> bool:
        return self.swapped


def _ledger_question(old: sqlite3.Connection, root: Path, identity: ProjectIdentity) -> list[str]:
    """Why the record may not be rebuilt from now, judged against the
    CURRENT index: a ledger it cannot apply. Above all SHRUNK -- the file
    lacks entries this index applied and 9.3's question is open. A fresh
    index has no copy of what was applied, so it would simply obey the
    shrunk file: a rebuild would answer 「以文件为准」 for the researcher and
    throw the safety net away. The question is answered first."""
    _loaded, decision = judgements.assess(old, root, identity)
    if decision.may_apply:
        return []
    if decision.verdict.value == "shrunk":
        return [
            f"the judgment ledger has {len(decision.missing)} entr(y/ies) fewer than this index applied "
            f"({decision.message}); answer that first ('rce records --answer file|restore')"
        ]
    return [f"the judgment ledger cannot be applied now ({decision.reason}: {decision.detail or ''}); repair it first"]


def _identity_for_rebuild(root: Path) -> ProjectIdentity:
    got = read_identity(root)
    if got.state is IdentityState.ABSENT:
        if paths.has_legacy_index(root):
            raise RebuildRefused(f"{root} was indexed before V5; its record is moved out of the old index by 'rce migrate'")
        raise RebuildRefused(f"{root} is not an RCE project; run 'rce init' first")
    if got.state is not IdentityState.PRESENT or got.identity is None:
        raise RebuildRefused(f"the project identity file of {root} cannot be read ({got.state.value})")
    if got.identity.migrating_from is not None:
        raise RebuildRefused(f"the migration of {root} has not finished; 'rce migrate' resumes it")
    return got.identity


def rebuild(
    project_root: str | Path,
    *,
    expected_id: Any = READ_NOW,
    echo: Echo = lambda _l: None,
    timeout: float | None = None,
    enforce: bool = True,
    fault: Fault | None = None,
) -> Rebuilt:
    """`rce rebuild` (module docstring). `enforce=False` swaps even when the
    comparison found failures or a source was unreadable -- only `rce
    project claim`, whose whole point is that nothing of the other
    folder's index survives, passes it; the findings are still returned.
    Raises `RebuildRefused` (nothing written), the write guard's refusals,
    or `SwapRefused`."""
    root = Path(project_root)
    with write_guard(root, expected_id, human=False, timeout=timeout):
        identity = _identity_for_rebuild(root)
        target = index_db_path(identity.id)
        staging = staging_path(target)
        remove_database(staging)  # a crashed attempt's leftover, never a live index
        old: sqlite3.Connection | None = db.connect(target) if target.exists() else None
        try:
            if old is not None:
                db.migrate(old)
            producers = producers_of(old, root) if old is not None else Producers()
            blocked = list(producers.problems)
            if old is not None:
                blocked += [f"source not readable: {s}" for s in evicted_sources(old, root)]
                blocked += _ledger_question(old, root, identity)
                from rce.records import cards as cards_mod  # noqa: PLC0415 -- leaf use

                if db.has_variable_tables(old):
                    blocked += cards_mod.rebuild_questions(old, root, identity)
            if blocked and enforce:
                return Rebuilt(target, swapped=False, blocked=blocked)
            new = db.connect(staging)
            try:
                db.migrate(new)
                from rce import project as project_mod  # noqa: PLC0415 -- project imports this module

                project_mod._upsert_project_node(new, root, identity)
                report = build_index(new, root, identity, producers=producers, echo=echo)
                if report.ingest_error:
                    blocked.append(f"the scan failed: {report.ingest_error}")
                blocked += [f"source not readable: {s}" for s in report.unreadable]
                failures, tally = compare(old, new) if old is not None else ([], {})
                failures += [f"mirror: {m}" for m in verify_mirrors(new, root)]
                if report.applied is not None and not report.applied.applied and report.applied.decision is not None:
                    failures.append(f"the judgment ledger could not be applied ({report.applied.decision.reason})")
            finally:
                new.close()
            if fault is not None:
                fault("before_install")
            if (blocked or failures) and enforce:
                remove_database(staging)
                return Rebuilt(target, swapped=False, blocked=blocked, failures=failures, tally=tally, report=report)
        finally:
            if old is not None:
                old.close()
        try:
            previous = install(staging, target, fault=fault)
        except BaseException:
            if not target.exists() and staging.exists():
                os.replace(staging, target)  # never leave neither
            raise
        logger.warning("RCE: rebuilt the index of %s at %s (previous kept at %s)", root, target, previous)
        return Rebuilt(target, swapped=True, previous=previous, blocked=blocked, failures=failures, tally=tally, report=report)
