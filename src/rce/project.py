"""The identity check every entry point makes first (DESIGN.md 9.4), and
the answers to the questions it can ask.

`rce.records.situation.classify` says what a folder is; this module does
what each situation calls for, before anything else is written -- no
registry entry or recency bump, no README, no scan, no project node, no
legacy move:

| situation      | `open_project`                                              |
|----------------|-------------------------------------------------------------|
| NORMAL         | open (a respelled home -- a rename, a case change -- is     |
|                | adopted like a move: only the spelling changes)             |
| MOVED          | adopt: `home.json`, the registry entry's path and label,    |
|                | the project node's path; one WARNING log line               |
| NO_INDEX       | build the index for the id from the sources and the record |
|                | (a scan, then the judgment ledger applied, 9.4 / 9.9 #5)   |
| LEGACY         | open; human records are refused until it is migrated        |
| NOT_A_PROJECT  | open (the callers' own "run rce init" refusal follows)      |
| COPY, CANNOT_CHECK, LOST_ID, UNREADABLE_ID | `ProjectBlocked`, nothing written |

The answers (`fork`, `claim`, `other`; for a lost identity file also
`restore` and `adopt`, 9.12) are explicit acts, each under the
project lock and each re-classifying the folder under that lock before
it writes, so an answer given to a question that no longer stands writes
nothing (`AnswerRefused`). Every index this module creates records its
home *before* `graph.db` exists (`rce.records.situation`).

The registry is touched only by `open_project(register=True)` (what
serving a project means) and by adoption (a moved project's entry follows
it). A CLI subcommand on a blocked folder therefore leaves no trace.

This module is the layer above `rce.records` (which knows nothing of the
registry or the extractors) and below the three surfaces (CLI, server,
MCP) that call it.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from rce import db, paths
from rce.ingest import pipeline
from rce.records import files, identity
from rce.records import ledger as ledger_mod
from rce.records.identity import ProjectIdentity
from rce.records.lock import project_lock
from rce.records.situation import (
    Classification,
    Probes,
    Situation,
    V5_RECORD_NAMES,
    classify,
    index_db_path,
    write_guard,
    write_home,
)
from rce.webapp import registry

logger = logging.getLogger(__name__)

class ProjectBlocked(Exception):
    """The folder is in a situation where nothing may be written until the
    researcher answers (COPY, CANNOT_CHECK, LOST_ID, UNREADABLE_ID)."""

    def __init__(self, classification: Classification) -> None:
        super().__init__(describe_blocked(classification))
        self.classification = classification


class AnswerRefused(Exception):
    """An answer (fork / claim / other) does not fit the folder's situation
    as it is now. Nothing was written."""


@dataclass(frozen=True)
class Opened:
    """What an entry point opened: the folder, its id (None for a pre-V5
    or not-yet-initialized folder) and the situation it was found in."""

    root: Path
    classification: Classification
    adopted: bool = False
    created_index: bool = False

    @property
    def project_id(self) -> str | None:
        return self.classification.project_id

    @property
    def situation(self) -> Situation:
        return self.classification.situation

    @property
    def needs_migration(self) -> bool:
        return self.classification.needs_migration


# -- what the CLI says when it stops ------------------------------------------------


def describe_blocked(c: Classification) -> str:
    """English, for the CLI: the reason, then the exact commands."""
    root = str(c.root)
    if c.situation is Situation.COPY:
        head = (
            f"{root} carries the project id {c.project_id}, whose home is {c.home.canonical_path if c.home else '?'}, "
            f"and that folder still carries the same id: this folder is a copy. Nothing was written. Choose one:"
        )
    elif c.situation is Situation.CANNOT_CHECK:
        head = (
            f"{root} carries the project id {c.project_id}, but its home "
            f"{c.home.canonical_path if c.home else '(not recorded)'} cannot be checked ({c.reason}): "
            f"a move cannot be told from a copy. Nothing was written. Choose one, or reconnect the "
            f"original's volume and try again (the app can also open it read-only):"
        )
    elif c.situation is Situation.LOST_ID:
        records = ", ".join(c.extra.get("records", []))
        snap = c.extra.get("snapshot")
        snapshot_only = c.reason == "identity_snapshot_only"
        if snapshot_only:
            # 9.12: no record files, but a snapshot of project.toml -- this
            # folder had an identity; `rce init` does not mint a new one.
            lines = [
                f"{root} has no .rce/project.toml, but .rce/backups/ holds a snapshot of one: this folder had "
                f"an identity, and RCE never gives it a new one silently (not even 'rce init'). Nothing was "
                f"written. Choose one:"
            ]
        else:
            lines = [
                f"{root} has no .rce/project.toml, but .rce/ holds records ({records}); RCE never gives "
                f"existing records a new identity silently. Nothing was written. Choose one:"
            ]
        if snap:
            known = (
                f"; this machine has its index, home {snap['index_home'] or '(not recorded)'}"
                if snap.get("index_on_this_machine") else "; this machine has no index for it"
            )
            lines.append(
                f"  rce project restore {root}   -- put back .rce/project.toml from {snap['file']} "
                f"(project {snap['project_id']}, created {snap['created']}{known})"
            )
        if snapshot_only:
            lines.append(f"  rce project adopt {root}     -- give this folder a new identity; build a fresh index from its sources")
            return "\n".join(lines)
        lines.append(f"  rce project adopt {root}     -- keep every record here under a new identity; build a fresh index from them")
        lines.append(f"  rce project other {root}     -- this .rce/ was copied in from another project: new id, copied records moved into .rce/backups/")
        if not snap:
            lines.append("(or restore .rce/project.toml yourself, from git or the folder this .rce/ came from)")
        return "\n".join(lines)
    elif c.situation is Situation.UNREADABLE_ID:
        where = f" (line {c.extra['line']})" if "line" in c.extra else ""
        copies = c.extra.get("conflict_copies")
        fix = (
            f"remove or merge the sync conflict copies ({', '.join(copies)}) beside it"
            if copies else "repair it, or wait for it to finish downloading"
        )
        return (
            f"the project identity file {c.root / '.rce' / 'project.toml'} cannot be read ({c.reason}"
            f"{': ' + c.detail if c.detail else ''}){where}; who this project is cannot be told. "
            f"Nothing was written. {fix[0].upper() + fix[1:]}, then try again."
        )
    else:  # pragma: no cover -- only blocking situations are described
        return f"{root}: {c.situation.value}"
    return (
        f"{head}\n"
        f"  rce project fork {root}    -- continue here as an independent branch (new id)\n"
        f"  rce project claim {root}   -- this folder is the original; rebuild the index from it\n"
        f"  rce project other {root}   -- this is another project that received a copy of .rce/"
    )


# -- opening ------------------------------------------------------------------------


def open_project(
    project_root: str | Path,
    *,
    register: bool = False,
    probes: Probes | None = None,
) -> Opened:
    """THE identity check of an entry point (module docstring). Raises
    `ProjectBlocked` (nothing written) or `NotADirectoryError`.
    `register=True` -- serving the project -- records it in the registry
    as most recently served, by id (or by path for a pre-V5 folder)."""
    root = Path(project_root)
    c = classify(root, probes=probes)
    if c.blocked:
        raise ProjectBlocked(c)
    adopted = created = False
    if c.situation is Situation.NORMAL and c.home is not None and c.home.claim_pending:
        c = _finish_claim(root, c, probes)
    if c.situation is Situation.MOVED or (c.situation is Situation.NORMAL and c.respelled):
        c, adopted = _adopt(root, c, probes)
    elif c.situation is Situation.NO_INDEX and not c.needs_migration:
        # A folder whose migration has not finished (`migrating_from`) is
        # NOT given an index here: its old index keeps serving, read-only,
        # until `rce migrate` resumes and the tally balances (9.5 step 3).
        c, created = _build_missing_index(root, c, probes)
    if register:
        if c.project_id is not None:
            registry.register(Path(paths._canonical_path(root)), c.project_id)
        elif c.situation is Situation.LEGACY:
            registry.register(root)
    return Opened(root, c, adopted=adopted, created_index=created)


def _reclassify_same(root: Path, before: Classification, probes: Probes | None) -> Classification:
    now = classify(root, probes=probes)
    if now.situation is not before.situation or now.project_id != before.project_id:
        if now.blocked:
            raise ProjectBlocked(now)
        raise AnswerRefused(f"{root} changed while it was being opened ({before.situation.value} -> {now.situation.value}); nothing written")
    return now


def _adopt(root: Path, c: Classification, probes: Probes | None) -> tuple[Classification, bool]:
    """A moved, renamed or respelled project: the index's home becomes
    this folder (9.4). One log line."""
    assert c.identity is not None
    with project_lock(root, c.project_id):
        c = _reclassify_same(root, c, probes)
        old = c.home.canonical_path if c.home else "?"
        write_home(c.project_id, root)
        _touch_project_node(root, c.identity)
        _signpost(root)
    # The spelling as the file system stores it: after a case-only rename
    # the registry shows the folder's new name, not the one typed.
    registry.relocate(c.project_id, Path(paths._canonical_path(root)))
    # WARNING: the lowest level an unconfigured `rce` prints, and what lands
    # in RCE.app's serve.log -- a project's home changing is never silent.
    logger.warning("RCE: project %s now lives at %s (was %s)", c.project_id, root, old)
    return classify(root, probes=probes), True


def _build_missing_index(root: Path, c: Classification, probes: Probes | None) -> tuple[Classification, bool]:
    assert c.identity is not None
    with project_lock(root, c.project_id):
        c = _reclassify_same(root, c, probes)
        create_index(root, c.identity)
        error = _build_from_sources(root, c.identity, lambda _line: None)
    logger.warning("RCE: built a new index for project %s at %s from its sources and record%s",
                   c.project_id, paths.index_dir(c.project_id), f" (scan failed: {error})" if error else "")
    return classify(root, probes=probes), True


def create_index(project_root: Path, ident: ProjectIdentity) -> Path:
    """`~/.rce/graphs/<id>/`: `home.json` first, then a migrated `graph.db`
    holding the project node `project:<id>`. Hold the id's lock."""
    write_home(ident.id, project_root)
    db_path = index_db_path(ident.id)
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
        _upsert_project_node(conn, project_root, ident)
    finally:
        conn.close()
    _signpost(Path(project_root))
    return db_path


def _upsert_project_node(conn, project_root: Path, ident: ProjectIdentity) -> str:
    node_id = project_node_id(ident.id)
    db.upsert_node(conn, node_id, "project", title=Path(project_root).name, attrs={"path": str(project_root)})
    return node_id


def _touch_project_node(project_root: Path, ident: ProjectIdentity) -> None:
    db_path = index_db_path(ident.id)
    if not db_path.exists():
        return
    conn = db.connect(db_path)
    try:
        _upsert_project_node(conn, project_root, ident)
    finally:
        conn.close()


def _signpost(root: Path) -> None:
    """`.rce/README` rewritten (9.12): the index's current location and the
    record files this folder holds -- whenever the identity check adopts,
    migrates or builds an index. A signpost that cannot be written never
    undoes what it describes: logged, not raised."""
    try:
        paths.write_project_readme(root)
    except (OSError, files.RecordFileError, paths.IdentityUnavailableError) as exc:
        logger.warning("could not write %s (%s)", paths.project_rce_dir(root) / paths.README_FILENAME, exc)


def project_node_id(project_id: str) -> str:
    """The project node is `project:<id>` (9.4), not the folder's name: a
    rename no longer forks it into two nodes."""
    return f"project:{project_id}"


# -- rce init -----------------------------------------------------------------------


@dataclass(frozen=True)
class Initialized:
    identity: ProjectIdentity
    db_path: Path
    applied: list[int]
    created_identity: bool
    readme: Path


def init_project(project_root: str | Path, *, probes: Probes | None = None) -> Initialized:
    """`rce init`: create `.rce/project.toml` (exclusively) and the index
    under the id; idempotent on a project that already has one. A pre-V5
    folder is refused (`AnswerRefused`): giving it an id would strand its
    judgments in the old index, and migration is an explicit act (9.5)."""
    root = Path(project_root)
    c = classify(root, probes=probes)
    if c.blocked:
        raise ProjectBlocked(c)
    if c.situation is Situation.LEGACY:
        raise AnswerRefused(
            f"{root} was indexed before V5 (its index is at {paths.legacy_graph_dir(root)}); it stays "
            f"readable, and its judgments are moved into the record by `rce migrate`, which creates "
            f"its identity. Not creating one now."
        )
    created_identity = False
    if c.situation is Situation.NOT_A_PROJECT:
        with project_lock(root, None):
            c = _reclassify_same(root, c, probes)
            ident = identity.create_identity(root)
            created_identity = True
            with project_lock(root, ident.id):
                create_index(root, ident)
    else:
        opened = open_project(root, probes=probes)
        ident = opened.classification.identity
        assert ident is not None
    with write_guard(root, ident.id):
        db_path = index_db_path(ident.id)
        conn = db.connect(db_path)
        try:
            applied = db.migrate(conn)
            _upsert_project_node(conn, root, ident)
        finally:
            conn.close()
        readme = paths.write_project_readme(root)
    return Initialized(ident, db_path, applied, created_identity, readme)


# -- the answers ----------------------------------------------------------------------

Echo = Callable[[str], None]


def _git_tracks(project_root: Path, rel: str) -> bool:
    """Whether git tracks `rel` in `project_root` (False when there is no
    git, no repository, or the file is untracked)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel], cwd=project_root,
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@dataclass(frozen=True)
class Answered:
    answer: str
    root: Path
    identity: ProjectIdentity
    previous_id: str | None
    moved_aside: tuple[str, ...] = ()
    replaced_index: Path | None = None
    git_tracked_identity: bool = False
    build_error: str | None = None
    # `restore`: the snapshot put back, and the folder's situation once the
    # identity check has run on it (a copy, say, asks its own question).
    restored_from: str | None = None
    situation_after: str | None = None
    blocked_after: str | None = None


def _require(c: Classification, allowed: tuple[Situation, ...], answer: str) -> None:
    """Act on an answer only when the folder's question offers it: the
    situation must be one the answer is for, and the answer one of those
    the question offers (`Classification.answers` -- a lost identity known
    only by its snapshot offers restore / adopt, not 「这是另一个项目」)."""
    if c.situation not in allowed:
        raise AnswerRefused(
            f"'{answer}' answers {' / '.join(s.value for s in allowed)}; {c.root} is {c.situation.value} -- nothing written"
        )
    if answer not in c.answers:
        offered = " / ".join(c.answers) or "none"
        raise AnswerRefused(
            f"'{answer}' is not an answer to {c.root}'s question (offered: {offered}) -- nothing written"
        )


def _build_from_sources(root: Path, ident: ProjectIdentity, echo: Echo) -> str | None:
    """Scan this folder into its (fresh) index and apply the record --
    the record files and the judgment ledger even when the source scan
    failed, so a judgment is never missing from an index for want of a
    scan; returns the scan's failure, if any, as text -- the identity
    answer itself has already landed."""
    conn = db.connect(index_db_path(ident.id))
    error: str | None = None
    try:
        try:
            pipeline.ingest_sources(conn, root, echo=echo)
        except pipeline.IngestFailed as exc:
            error = str(exc)
        pipeline.ingest_records(conn, root, echo=echo)
    finally:
        conn.close()
    return error


def fork(project_root: str | Path, *, probes: Probes | None = None, build: bool = True, echo: Echo = lambda _l: None, today: date | None = None) -> Answered:
    """「作为独立分支继续」: this folder gets a new id with `forked_from`; its
    record files came with the copy, so its judgments start equal to the
    original's and diverge from here; it gets its own index."""
    root = Path(project_root)
    c = classify(root, probes=probes)
    _require(c, (Situation.COPY, Situation.CANNOT_CHECK), "fork")
    old = c.identity
    assert old is not None
    with project_lock(root, old.id):
        c = _reclassify_same(root, c, probes)
        new = ProjectIdentity(
            id=identity.new_project_id(), created=(today or date.today()).isoformat(),
            ledger=old.ledger, forked_from=old.id,
        )
        identity.replace_identity(root, old, new)
        with project_lock(root, new.id):
            create_index(root, new)
            build_error = _build_from_sources(root, new, echo) if build else None
    tracked = _git_tracks(root, f"{paths.RCE_DIRNAME}/{identity.PROJECT_FILENAME}")
    logger.warning("RCE: %s is now project %s, forked from %s", root, new.id, old.id)
    return Answered("fork", root, new, old.id, git_tracked_identity=tracked, build_error=build_error)


def claim(project_root: str | Path, *, probes: Probes | None = None, echo: Echo = lambda _l: None) -> Answered:
    """「这里才是原项目」: the index's home becomes this folder AND the index
    is rebuilt from this folder through `rce rebuild` (9.4, 9.8), so
    nothing from the other folder's scans or record survives in it; the
    previous index is kept one generation (`graph.db.prev`). The rebuild's
    per-link comparison is reported but does not stop the swap: the
    previous index is the OTHER folder's, and a difference from it is the
    point of claiming. The id is kept: the other folder, still carrying
    it, is asked the same question when it is next opened."""
    root = Path(project_root)
    c = classify(root, probes=probes)
    _require(c, (Situation.COPY, Situation.CANNOT_CHECK), "claim")
    ident = c.identity
    assert ident is not None
    build_error: str | None = None
    with project_lock(root, ident.id):
        c = _reclassify_same(root, c, probes)
        # Marked pending BEFORE the home moves here, cleared only after the
        # rebuild: a claim killed in between is finished by the next open
        # (`_finish_claim`), never left serving the other folder's index.
        write_home(ident.id, root, claim_pending=True)
        rebuilt = _rebuild_for_claim(root, ident, echo)
        _signpost(root)
        if rebuilt.report is not None and rebuilt.report.ingest_error:
            build_error = rebuilt.report.ingest_error
    registry.relocate(ident.id, root)
    logger.warning("RCE: %s claimed project %s; the previous index was kept at %s", root, ident.id, rebuilt.previous)
    return Answered("claim", root, ident, ident.id, replaced_index=rebuilt.previous, build_error=build_error)


def _rebuild_for_claim(root: Path, ident: ProjectIdentity, echo: Echo):
    """The claim's rebuild, then the pending mark cleared. Hold the lock."""
    from rce import rebuild as rebuild_mod  # noqa: PLC0415 -- rebuild imports this module

    if not index_db_path(ident.id).exists():
        create_index(root, ident)
        write_home(ident.id, root, claim_pending=True)
    rebuilt = rebuild_mod.rebuild(root, expected_id=ident.id, echo=echo, enforce=False)
    write_home(ident.id, root)
    return rebuilt


def _finish_claim(root: Path, c: Classification, probes: Probes | None) -> Classification:
    """A claim killed between moving the home here and rebuilding the index
    (`Home.claim_pending`): finish it before anything else reads or writes
    the index -- which until then is the OTHER folder's."""
    assert c.identity is not None
    with project_lock(root, c.project_id):
        c = _reclassify_same(root, c, probes)
        if c.home is None or not c.home.claim_pending:
            return c
        _rebuild_for_claim(root, c.identity, lambda _l: None)
    logger.warning("RCE: finished the interrupted claim of project %s by %s", c.project_id, root)
    return classify(root, probes=probes)


def _move_records_aside(root: Path) -> tuple[str, ...]:
    """The copied ledger, arrangement and variable cards speak about
    another project's files: moved (never deleted) into `.rce/backups/
    from-another-project-<UTC stamp>/`."""
    rce_dir = paths.project_rce_dir(root)
    names = [n for n in V5_RECORD_NAMES if os.path.lexists(rce_dir / n)]
    names += [p.name for p in files.conflict_copies(rce_dir / "judgements.toml")]
    if not names:
        return ()
    backups = rce_dir / files.BACKUPS_DIRNAME
    backups.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backups / f"from-another-project-{stamp}"
    target.mkdir()
    for name in names:
        os.rename(rce_dir / name, target / name)
    return tuple(names)


def other(project_root: str | Path, *, probes: Probes | None = None, build: bool = True, echo: Echo = lambda _l: None, today: date | None = None) -> Answered:
    """「这是另一个项目」: a folder that merely received a copy of someone
    else's `.rce/`. A new id with no `forked_from`; the copied record
    files are moved into `.rce/backups/` FIRST (a crash in between leaves
    the old id and the question, never a new id over foreign records)."""
    root = Path(project_root)
    c = classify(root, probes=probes)
    _require(c, (Situation.COPY, Situation.CANNOT_CHECK, Situation.LOST_ID), "other")
    old = c.identity
    with project_lock(root, old.id if old else None):
        c = _reclassify_same(root, c, probes)
        moved = _move_records_aside(root)
        if old is not None:
            new = ProjectIdentity(id=identity.new_project_id(), created=(today or date.today()).isoformat())
            identity.replace_identity(root, old, new)
        else:
            new = identity.create_identity(root, today=today)
        with project_lock(root, new.id):
            create_index(root, new)
            build_error = _build_from_sources(root, new, echo) if build else None
    logger.warning("RCE: %s is now an independent project %s (records moved aside: %s)", root, new.id, ", ".join(moved) or "none")
    return Answered("other", root, new, old.id if old else None, moved_aside=moved, build_error=build_error)


def _ledger_in_use(root: Path) -> bool:
    """Whether `.rce/judgements.toml` must be flagged in `project.toml`
    (`ledger = true`): it is there and holds entries, or cannot be read --
    a file that exists is never treated as absent (9.3)."""
    loaded = ledger_mod.load_judgements(root)
    if loaded.state is files.RecordState.ABSENT:
        return False
    return loaded.ledger is None or bool(loaded.ledger.entries)


def adopt(project_root: str | Path, *, probes: Probes | None = None, build: bool = True, echo: Echo = lambda _l: None, today: date | None = None) -> Answered:
    """「沿用这些记录，建立新身份」 (`rce project adopt`, DESIGN.md 9.12): a
    folder whose identity file was lost keeps every record it holds --
    ledger, arrangement, variable cards, all where they are -- under a NEW
    id (no `forked_from`: what the old id was is not known), and gets a
    fresh index built from its sources and those records. Only for
    LOST_ID; nothing is moved or rewritten."""
    root = Path(project_root)
    c = classify(root, probes=probes)
    _require(c, (Situation.LOST_ID,), "adopt")
    with project_lock(root, None):
        c = _reclassify_same(root, c, probes)
        new = identity.create_identity(root, ledger=_ledger_in_use(root), today=today)
        with project_lock(root, new.id):
            create_index(root, new)
            build_error = _build_from_sources(root, new, echo) if build else None
    logger.warning("RCE: %s keeps its records under a new identity %s (its identity file was lost)", root, new.id)
    return Answered("adopt", root, new, None, build_error=build_error)


def restore(project_root: str | Path, *, probes: Probes | None = None, echo: Echo = lambda _l: None) -> Answered:
    """Restore `.rce/project.toml` from its snapshot (`rce project restore`,
    DESIGN.md 9.12): the newest snapshot in `.rce/backups/` that reads as
    an identity is put back byte for byte, created exclusively. If the
    ledger holds entries and the snapshot predates the flag, `ledger =
    true` is raised again (9.3). Then the folder goes through the identity
    check like any open: a missing index is built, a move adopted -- and a
    copy, or a home that cannot be checked, asks its own question
    (`blocked_after`). Only for LOST_ID."""
    root = Path(project_root)
    c = classify(root, probes=probes)
    _require(c, (Situation.LOST_ID,), "restore")
    with project_lock(root, None):
        c = _reclassify_same(root, c, probes)
        found = identity.identity_snapshots(root)
        if not found:
            raise AnswerRefused(f"there is no readable snapshot of .rce/project.toml in .rce/backups/ of {root} -- nothing written")
        snap, _ident = found[0]
        try:
            ident = identity.restore_identity_file(root, snap)
        except identity.IdentityError as exc:
            raise AnswerRefused(f"{exc} -- nothing written") from exc
        if not ident.ledger and _ledger_in_use(root):
            with project_lock(root, ident.id):
                ident = identity.set_flag(root, ident, "ledger", True)
    rel = snap.relative_to(root).as_posix()
    logger.warning("RCE: %s: restored .rce/project.toml (project %s) from %s", root, ident.id, rel)
    blocked_after: str | None = None
    try:
        after = open_project(root, probes=probes).situation.value
    except ProjectBlocked as exc:
        after = exc.classification.situation.value
        blocked_after = str(exc)
    return Answered("restore", root, ident, None, restored_from=rel, situation_after=after, blocked_after=blocked_after)
