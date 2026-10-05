"""Where RCE's derived state lives (DESIGN.md section 8.10, resilience
rule 1) -- the single module that answers "where is this project's
graph.db?", so no other module ever computes that path itself.

Why the graph left the project
------------------------------

Until this module existed, the graph was `<project>/.rce/graph.db`
(DESIGN.md section 2). One week of real use killed that: the researcher's
project sits in `~/Documents`, which that Mac syncs to iCloud Drive
("Desktop & Documents Folders"). A file the sync provider has evicted or
is mid-transfer is *materialized on `open()`* -- there is no non-blocking
way to open it -- so `sqlite3.connect` blocked for over a minute and every
DB-backed endpoint hung while `/api/generation` and `/api/projects` kept
answering (a hang, not an error: nothing to report, nothing to retry).
SQLite under a file-provider sync is a documented corruption path besides.

So the derived half of `.rce/` moves out of the project entirely, to
`~/.rce/graphs/<id>/`, and `.rce/` inside the project keeps only what the
researcher owns and may want under git: `attempts.toml`, `mappings.toml`
(section 8.5), `backups/`, and the one-line `README` (`write_project_readme`)
that says where the graph went. Since V5 (section 9.2) the human records
live there too: `project.toml`, `judgements.toml` and the canvas
arrangement `canvas.json` (`rce.webapp.canvas.canvas_record_path`);
`canvas_state_path` names only the pre-V5 place beside the graph, read as
a fallback until a project's arrangement is first written to its record.

Where the index lives: keyed by the project's identity (V5)
------------------------------------------------------------

Since DESIGN.md section 9.4 a project is identified by a file it carries,
`.rce/project.toml`, not by where it sits. The index of a project with an
id lives at `~/.rce/graphs/<id>/` (`index_dir`), beside a `home.json`
that remembers which folder is its home (`rce.records.situation`). Moving
or renaming the folder no longer strands the index, and a new, unrelated
project created at an old project's path has a different id (or none)
and inherits nothing.

Before V5 the `<id>` was a truncated hash of the project's *canonical*
path (`canonical_path_hash`). Those indexes still exist on the
researcher's disk and still hold judgments, so the functions that find
them are kept under `legacy_*` names: a folder with no `project.toml`
whose path-hash index exists is a pre-V5 project, served read-only for
human records until `rce migrate` (a later phase) moves its judgments
into the record. `graph_dir(project_root)` -- what every caller asks --
answers with the id directory when the folder has an id and with the
legacy directory when it has none, so read paths need not know which
kind of project they serve. An identity file that exists but cannot be
read is never answered with the legacy directory
(`IdentityUnavailableError`): "cannot read who this is" must not become
"serve whatever index sits at this path's hash".

The canonical path is still what the project *lock* is keyed by before
an id exists, and the legacy hash is what `rce migrate --list` will look
for; its spelling rules (letter case, Unicode normalization, symlinks
folded by asking the filesystem) are unchanged.

`RCE_HOME`
----------

`rce_home()` honours the `RCE_HOME` environment variable, which names the
`.rce` home directory itself (not the user's home). It exists so the test
suite -- and anyone running two isolated RCE states side by side -- never
touches the real `~/.rce`; `tests/conftest.py` sets it for every test.
Resolved per call, never at import time, exactly as
`rce.webapp.registry.registry_path()` resolves `Path.home()` per call and
for the same reason.

Legacy migration
----------------

`migrate_legacy_graph` is the one-time move (for a pre-V5 folder with no
id only), run on first touch by any
subcommand or the server (every `_require_db` copy calls `resolve_graph_db`,
which is `graph_db_path` plus this migration). It copies through SQLite's
own online-backup API rather than `shutil.copyfile` -- a WAL-mode database
keeps committed data in a `-wal` sidecar until it is checkpointed, so
copying the main file alone can silently drop the most recent writes --
verifies the copy with `PRAGMA integrity_check`, and only then removes the
legacy file and its `-wal`/`-shm` companions. A failed check leaves
everything exactly as it was and raises `GraphMigrationError`: a graph that
cannot be verified is never deleted, and never quietly replaced by an empty
one.

The dataless flag
-----------------

`is_dataless` is the other half of rule 1: macOS marks a file whose content
has been evicted to iCloud with `SF_DATALESS`, and `os.stat` reports that
flag *without* materializing the file (only `open()` blocks). The server
checks it before opening the graph so a cloud-evicted file becomes an
immediate, honest answer -- 「图谱文件正在从云端下载…」 -- instead of a hung
handler thread. Guarded for non-macOS, where `st_flags` does not exist.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

# The project-side directory: what the researcher owns (attempts.toml,
# mappings.toml, backups/, README). Same literal every subsystem used
# before this module existed -- they now import it from here.
RCE_DIRNAME = ".rce"
DB_FILENAME = "graph.db"
CANVAS_FILENAME = "canvas.json"
README_FILENAME = "README"

# Under rce_home(): ~/.rce/graphs/<id>/{graph.db,canvas.json}
GRAPHS_DIRNAME = "graphs"

HOME_ENV_VAR = "RCE_HOME"

# Length of the hex digest used as <id>. 16 hex chars = 64 bits: a
# collision needs ~4 billion distinct project paths on one machine before
# it is even worth thinking about, and a short directory name keeps
# `rce status`'s graph line readable.
_ID_LENGTH = 16

# macOS: "file is dataless" (sys/stat.h SF_DATALESS). Not exposed by the
# `stat` module, so it is spelled out here rather than guessed at from a
# platform check.
SF_DATALESS = 0x40000000


class GraphMigrationError(Exception):
    """A legacy in-project `graph.db` could not be moved out of the project
    safely -- the copy failed, or `PRAGMA integrity_check` did not say
    `ok`. Nothing was deleted; the legacy file is still exactly where it
    was. Callers (`rce.cli._require_db`, `rce.webapp.server._require_db`)
    re-raise this as their own user-facing error type."""


class IdentityUnavailableError(GraphMigrationError):
    """The folder has a `.rce/project.toml` that cannot be read right now
    (in the cloud, unparseable, or with a sync conflict copy beside it),
    so which index belongs to it cannot be said. Raised by `graph_dir`
    instead of falling back to the legacy path-hash directory. A subclass
    of `GraphMigrationError` only so every existing `_require_db` copy
    turns it into its own user-facing error without a new except clause;
    entry points classify the folder first (`rce.records.situation`) and
    stop with the situation's own message before ever getting here."""


class LegacyGraphDatalessError(GraphMigrationError):
    """The legacy in-project graph (or one of its `-wal`/`-shm` companions)
    is dataless -- macOS has evicted its content to iCloud -- so the
    migration refused to open it rather than block on the download
    (section 8.10 rule 1: the check comes *before* opening). Nothing was
    copied, moved or deleted. A subclass of `GraphMigrationError` so every
    existing caller still handles it; the web server catches it first and
    answers with the transient 「图谱文件正在从云端下载…」 header state
    instead of the 500 a failed verification deserves."""


# -- Home + per-project locations ---------------------------------------------


def rce_home() -> Path:
    """`~/.rce`, or `$RCE_HOME` when set (the `.rce` directory itself, not
    the user's home). Resolved on every call, never cached at import time,
    so a test's `monkeypatch.setenv` takes effect for code that was
    imported long before it."""
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / RCE_DIRNAME


def _canonical_path(project_root: str | Path) -> str:
    """The one spelling of `project_root` the id is hashed from.

    `Path.resolve()` collapses `..`, a relative path, a trailing slash and
    symlinks -- but on macOS it does NOT canonicalize letter case or
    Unicode normalization, and the default APFS volume is insensitive to
    both: `~/Documents/RMB` and `~/documents/rmb`, or an NFC and an NFD
    spelling of `默认安全锚_论文流水线`/`é`, name ONE directory. Hashing
    `resolve()` alone gave them two ids, so whichever spelling touched a
    legacy graph first migrated it under its own id, and every other
    spelling then found "no RCE project" and was told to `rce init` a
    second, empty graph (adversarial review of the V4 work). Before the
    graph left the project the spelling never mattered, so this is a
    regression the move itself introduced.

    The fix asks the filesystem rather than guessing a folding rule:
    macOS's `fcntl(F_GETPATH)` on an open descriptor returns the path *as
    stored on disk* -- stored case, stored normalization, symlinks already
    resolved -- whichever spelling was used to open it. Opened with
    `O_EVTONLY` (the descriptor Finder uses for watching: no read access
    needed and it does not keep the volume busy), on the directory itself,
    never a file in it, so nothing is materialized. Anywhere this cannot
    run (not macOS, a path that does not exist yet, a permission error)
    the result is `resolve()`, exactly what the id was before -- and on
    macOS that fallback is NFC-normalized so it is at least stable across
    normalization forms. Linux is left byte-exact: its filesystems are
    case- and normalization-sensitive, so two spellings there really are
    two directories."""
    resolved = Path(project_root).resolve()
    if sys.platform != "darwin":
        return str(resolved)
    try:
        import fcntl  # noqa: PLC0415 -- POSIX-only; this branch is macOS-only
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_EVTONLY", 0))
        try:
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
        finally:
            os.close(fd)
        on_disk = raw.split(b"\0", 1)[0].decode("utf-8")
        if on_disk:
            return on_disk
    except (OSError, AttributeError, UnicodeDecodeError):
        pass
    return unicodedata.normalize("NFC", str(resolved))


def canonical_path_hash(project_root: str | Path) -> str:
    """A truncated SHA-256 of `project_root`'s canonical absolute path
    (`_canonical_path`): the same value for every spelling of one folder
    (a relative path, a symlinked `/tmp` on macOS, a trailing slash, a
    different letter case or Unicode normalization on a case-insensitive
    volume). Before V5 this WAS the project's id; now it keys only the
    project lock of a folder that has no id yet (`rce.records.lock`) and
    the pre-V5 indexes (`legacy_graph_dir`)."""
    canonical = _canonical_path(project_root)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_ID_LENGTH]


# The pre-V5 name of the path hash, kept for what reads pre-V5 state.
legacy_graph_id = canonical_path_hash


def index_dir(project_id: str) -> Path:
    """`~/.rce/graphs/<project id>` -- the index of a project that has an
    id (DESIGN.md 9.4): `graph.db`, `home.json`, and (until the record
    takes it over) `canvas.json`. The id is validated because it becomes
    a directory name and may have been read from a file the researcher, or
    a copied folder, supplied."""
    from rce.records.lock import PROJECT_ID_RE  # noqa: PLC0415 -- records imports this module

    if not isinstance(project_id, str) or not PROJECT_ID_RE.match(project_id):
        raise ValueError(f"not a project id: {project_id!r}")
    return rce_home() / GRAPHS_DIRNAME / project_id


def _project_id_of(project_root: str | Path) -> str | None:
    """The id `.rce/project.toml` names; None when the folder has no
    identity file; `IdentityUnavailableError` when it has one that cannot
    be read."""
    from rce.records import identity  # noqa: PLC0415 -- records imports this module

    got = identity.read_identity(project_root)
    if got.state is identity.IdentityState.ABSENT:
        return None
    if got.state is identity.IdentityState.PRESENT and got.identity is not None:
        return got.identity.id
    raise IdentityUnavailableError(
        f"the project identity file {got.path} cannot be read right now ({got.state.value}"
        f"{': ' + got.error if got.error else ''}); not guessing which index belongs to {project_root}"
    )


def legacy_graph_dir(project_root: str | Path) -> Path:
    """`~/.rce/graphs/<path hash>` -- where a pre-V5 project's index lives
    (and where every project's did before V5)."""
    return rce_home() / GRAPHS_DIRNAME / legacy_graph_id(project_root)


def legacy_index_db_path(project_root: str | Path) -> Path:
    """The pre-V5 index database of the folder at `project_root`'s path."""
    return legacy_graph_dir(project_root) / DB_FILENAME


def graph_dir(project_root: str | Path) -> Path:
    """The directory holding everything RCE derives about this project:
    `index_dir(<id>)` when the folder carries an id, else the legacy
    path-hash directory (module docstring). Computing it never creates
    it; see `ensure_graph_dir`. Raises `IdentityUnavailableError` for an
    identity file that exists but cannot be read."""
    project_id = _project_id_of(project_root)
    if project_id is not None:
        return index_dir(project_id)
    return legacy_graph_dir(project_root)


def graph_db_path(project_root: str | Path) -> Path:
    """THE path to a project's graph database. Every `_require_db` copy in
    this codebase resolves through here (via `resolve_graph_db`) rather
    than joining `.rce/graph.db` itself -- one definition, so the location
    can never drift between the CLI, the MCP server, the web server, the
    watcher and the map-file writer."""
    return graph_dir(project_root) / DB_FILENAME


def canvas_state_path(project_root: str | Path) -> Path:
    """The PRE-V5 place of `canvas.json` (DESIGN.md section 8.6), beside
    the graph. Since V5 phase 4 the arrangement is a record in the
    project's own `.rce/canvas.json` (9.2, `rce.webapp.canvas`); this path
    is only read, as a fallback while that record does not exist yet, and
    phase 5's migration copies it."""
    return graph_dir(project_root) / CANVAS_FILENAME


def project_rce_dir(project_root: str | Path) -> Path:
    """`<project>/.rce` -- the researcher-owned half: `project.toml`,
    `judgements.toml`, `canvas.json`, `attempts.toml`, `mappings.toml`,
    `backups/`, `README`. No database lives here."""
    return Path(project_root) / RCE_DIRNAME


def legacy_graph_db_path(project_root: str | Path) -> Path:
    """Where the graph lived before section 8.10 rule 1, inside the
    project. Only `migrate_legacy_graph`, `graph_exists` and the identity
    situation check (a pre-V5 database, 9.5) have any business reading
    this -- nothing opens a database here anymore."""
    return project_rce_dir(project_root) / DB_FILENAME


def has_legacy_index(project_root: str | Path) -> bool:
    """Whether a pre-V5 database that may hold this folder's judgments
    exists: the index at this path's hash, or a pre-8.10 in-project
    `.rce/graph.db` (DESIGN.md 9.5, "What is looked for")."""
    return legacy_index_db_path(project_root).exists() or legacy_graph_db_path(project_root).exists()


def graph_exists(project_root: str | Path) -> bool:
    """Whether `project_root` is an initialized RCE project -- its index
    exists, OR (a folder with no id) a legacy in-project one does and has
    simply not been moved out yet. False for a folder whose identity file
    cannot be read: whether it is initialized cannot be said, and the
    callers (the registry's listing) treat it as not servable."""
    try:
        if graph_db_path(project_root).exists():
            return True
        return _project_id_of(project_root) is None and legacy_graph_db_path(project_root).exists()
    except IdentityUnavailableError:
        return False


def ensure_graph_dir(project_root: str | Path) -> Path:
    """Create `graph_dir(project_root)` if needed and return it. Called by
    `rce init` (and by the migration) -- deliberately NOT by
    `graph_db_path`, so merely asking where a graph would live never
    litters `~/.rce/graphs` with directories for projects that were never
    initialized. Under `rce_home()`, never in the project, so `parents=True`
    here cannot re-create a project folder."""
    directory = graph_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# -- The project's own one-line signpost ---------------------------------------

_README_TEMPLATE = (
    "此目录只保留你自己维护的文件（attempts.toml / mappings.toml / backups/）；"
    "RCE 派生的图谱数据库在 {graph_dir}，不放在项目里，以免被云同步损坏。\n"
)


def write_project_readme(project_root: str | Path) -> Path:
    """Write `<project>/.rce/README`: one line, in the app's own product
    language, saying where the graph went (DESIGN.md section 8.10 rule 1 --
    "so nothing is hidden"). Rewritten on every `rce init` so a project
    that was initialized before the move gets the signpost too, and so a
    stale path from a moved project is corrected rather than left lying."""
    rce_dir = ensure_project_rce_dir(project_root)
    readme = rce_dir / README_FILENAME
    readme.write_text(_README_TEMPLATE.format(graph_dir=graph_dir(project_root)), encoding="utf-8")
    return readme


def ensure_project_rce_dir(project_root: str | Path) -> Path:
    """`<project>/.rce`, created if needed -- but only inside a folder that
    exists (DESIGN.md 9.4: record writers never re-create a folder that
    has gone). `parents=True` here would quietly rebuild a moved project's
    old path as an empty shell holding one file, which the next open would
    then mistake for a project."""
    root = Path(project_root)
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not an existing folder; not creating anything in it")
    rce_dir = root / RCE_DIRNAME
    rce_dir.mkdir(exist_ok=True)
    return rce_dir


# -- Legacy migration ----------------------------------------------------------


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:  # a leftover we could not clean up is not fatal
        logger.warning("could not remove %s (%s)", path, exc)


def _sidecars(db_path: Path) -> tuple[Path, Path]:
    """SQLite's WAL companions for `db_path`. They are part of the database
    -- leaving them behind next to a deleted main file would let a later
    `sqlite3.connect` on a recreated `graph.db` inherit a stranger's WAL."""
    return (
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    )


def _backup_database(source: Path, destination: Path) -> str:
    """Copy `source` to `destination` through SQLite's online-backup API
    and return `PRAGMA integrity_check`'s verdict on the *copy*.

    The backup API rather than a byte copy because a WAL-mode database's
    most recent commits can live entirely in its `-wal` sidecar until a
    checkpoint; `shutil.copyfile` of the main file alone would drop them
    silently, which is the one failure mode a migration must not have.
    The connections here are deliberately raw `sqlite3.connect` calls, not
    `rce.db.connect`: this is a bit-for-bit move, and setting pragmas on a
    file we are about to delete would be a pointless write to it."""
    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            row = dst.execute("PRAGMA integrity_check").fetchone()
        finally:
            dst.close()
    finally:
        src.close()
    return row[0] if row else "no result"


# Name of the per-graph-directory lock file every migrator takes (below).
_MIGRATION_LOCK_FILENAME = ".migrate.lock"
# Prefix of the per-attempt staging file; the random suffix comes from
# `tempfile.mkstemp`, so no two migrators can ever share one.
_STAGING_PREFIX = DB_FILENAME + ".migrating"

# One in-process lock beside the cross-process `flock`: on every platform
# `flock` excludes other open file descriptions, but a platform without
# `fcntl` (Windows) would otherwise get no exclusion at all between the
# server's own handler threads.
_THREAD_MIGRATION_LOCK = threading.Lock()


@contextlib.contextmanager
def _migration_lock(directory: Path) -> Iterator[None]:
    """Exclusive, blocking lock around one project's migration -- across
    threads of this process AND across processes (the RCE.app-spawned `rce
    serve`, an MCP client's `rce mcp`, a terminal `rce status` can all
    first-touch the same project at once).

    Why it must exist (adversarial review of the V4 work): without it,
    concurrent migrators raced on one fixed staging name -- each deleted
    "a previous attempt's leftover" that was really another migrator's
    in-flight copy, and whichever reached `os.replace` first installed
    *whatever* file had the staging name at that instant, possibly a
    half-written copy that never passed `integrity_check`, then deleted
    the legacy graph. A lock plus a unique staging name restores the one
    guarantee that matters: the file renamed into place is the very file
    that was verified, and the legacy graph is deleted only after that.

    `fcntl.flock` on a dedicated lock file in the graph directory (which
    lives in `~/.rce`, never in a synced folder). The lock is released
    when the descriptor closes, including when the process dies, so a
    crashed migrator can never wedge the next one."""
    lock_path = directory / _MIGRATION_LOCK_FILENAME
    with _THREAD_MIGRATION_LOCK:
        try:
            import fcntl  # noqa: PLC0415 -- POSIX-only
        except ImportError:  # pragma: no cover -- not a platform RCE ships on
            yield
            return
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _remove_staging_leftovers(directory: Path) -> None:
    """Delete every staging file (and its sidecars) in `directory`. Only
    ever called while holding `_migration_lock`, so anything matching is a
    crashed attempt's leftover, never another migrator's live copy."""
    for leftover in directory.glob(_STAGING_PREFIX + "*"):
        _unlink_quietly(leftover)


# Paths whose download a background thread has already been asked to start
# (see `_request_download`), so a page polling every 2s does not spawn a
# thread per poll for the same file.
_DOWNLOADS_REQUESTED: set[str] = set()
_DOWNLOADS_LOCK = threading.Lock()


def _request_download(path: Path) -> None:
    """Ask macOS to bring a dataless file back from iCloud WITHOUT blocking
    the caller: a daemon thread does the one `open()` + 1-byte read that
    materializes it (the same thing that hung the handler thread in V3,
    moved off it). Without this nothing would ever open the evicted legacy
    graph, the download would never start, and 「图谱文件正在从云端下载…」
    would stay true forever. At most one such thread per path at a time;
    a failure is logged, never raised -- the next first-touch simply asks
    again."""
    key = str(path)
    with _DOWNLOADS_LOCK:
        if key in _DOWNLOADS_REQUESTED:
            return
        _DOWNLOADS_REQUESTED.add(key)

    def _materialize() -> None:
        try:
            with open(path, "rb") as handle:
                handle.read(1)
        except OSError as exc:
            logger.warning("could not download %s from iCloud (%s)", path, exc)
        finally:
            with _DOWNLOADS_LOCK:
                _DOWNLOADS_REQUESTED.discard(key)

    threading.Thread(target=_materialize, name=f"rce-download:{path.name}", daemon=True).start()


def _refuse_if_dataless(legacy: Path) -> None:
    """Section 8.10 rule 1 applied to the migration itself: the legacy graph
    is the one file that DOES live in the cloud-synced project, so it is
    the one most likely to be evicted -- and the backup API's
    `sqlite3.connect` on it is exactly the blocking `open()` the rule
    forbids. Checked on the main file and both sidecars (a WAL-mode
    graph's recent commits may live only in `-wal`)."""
    evicted = [p for p in (legacy, *_sidecars(legacy)) if is_dataless(p)]
    if not evicted:
        return
    for path in evicted:
        _request_download(path)
    raise LegacyGraphDatalessError(
        f"the graph at {legacy} is not on this disk right now (macOS has evicted it to iCloud); "
        f"its download has been requested and the move out of the project will happen on the "
        f"next touch. Nothing was moved or deleted."
    )


def migrate_legacy_graph(project_root: str | Path) -> Path | None:
    """Move `<project>/.rce/graph.db` to `~/.rce/graphs/<id>/graph.db`, once.

    Returns the new path when a migration actually happened, None when
    there was nothing to do (no legacy file, or an external graph already
    exists -- in which case the legacy file is left alone rather than
    guessed about: two graphs is a situation for a human, not for a
    silent overwrite in either direction).

    Order is copy -> verify -> delete, never delete-before-verify: the copy
    lands on a staging file unique to this attempt, `PRAGMA
    integrity_check` must answer `ok`, and only then is that same staging
    file renamed into place and the legacy file (with its `-wal`/`-shm`)
    removed. Any failure raises `GraphMigrationError` with the legacy
    graph still where it was.

    Concurrency: the whole check-copy-verify-rename-delete runs under
    `_migration_lock`, and the "is there still anything to do?" test is
    repeated once the lock is held -- so of N concurrent first touches
    exactly one migrates and the rest return None and use its result.

    A dataless legacy graph is refused before anything opens it
    (`LegacyGraphDatalessError`, with its download requested in the
    background) -- see `_refuse_if_dataless`.
    """
    legacy = legacy_graph_db_path(project_root)
    if not legacy.exists():
        return None
    if _project_id_of(project_root) is not None:
        # A folder with an id never gets a path-hash index: an in-project
        # graph inside it is a pre-V5 database for `rce migrate` to list
        # (DESIGN.md 9.5), never something to move by opening the folder.
        return None
    target = legacy_index_db_path(project_root)
    if target.exists():
        return None
    _refuse_if_dataless(legacy)

    target.parent.mkdir(parents=True, exist_ok=True)
    with _migration_lock(target.parent):
        if target.exists() or not legacy.exists():
            return None  # another migrator finished while we waited
        _remove_staging_leftovers(target.parent)  # a previous crashed attempt's
        fd, staging_name = tempfile.mkstemp(prefix=_STAGING_PREFIX + "-", dir=target.parent)
        os.close(fd)
        staging = Path(staging_name)

        def _discard_staging() -> None:
            _unlink_quietly(staging)
            for sidecar in _sidecars(staging):
                _unlink_quietly(sidecar)

        try:
            verdict = _backup_database(legacy, staging)
        except (sqlite3.Error, OSError) as exc:
            _discard_staging()
            raise GraphMigrationError(
                f"could not copy the graph out of {legacy} to {target}: {exc}. "
                f"Nothing was deleted -- the original graph is still at {legacy}."
            ) from exc
        if verdict != "ok":
            _discard_staging()
            raise GraphMigrationError(
                f"the copy of {legacy} failed PRAGMA integrity_check ({verdict!r}), so it was "
                f"discarded. Nothing was deleted -- the original graph is still at {legacy}."
            )

        try:
            os.replace(staging, target)
        except OSError as exc:
            _discard_staging()
            raise GraphMigrationError(
                f"could not move the verified copy of {legacy} into place at {target}: {exc}. "
                f"Nothing was deleted -- the original graph is still at {legacy}."
            ) from exc
        for sidecar in _sidecars(staging):
            _unlink_quietly(sidecar)
        _unlink_quietly(legacy)
        for sidecar in _sidecars(legacy):
            _unlink_quietly(sidecar)
    # One line, not one per file moved: this happens once in a project's
    # life and the user needs exactly one fact from it. WARNING, not INFO,
    # because WARNING is the lowest level an unconfigured `rce` (no `-v`)
    # prints at all -- through `logging`'s last-resort stderr handler, which
    # is also what lands in RCE.app's `serve.log`. At INFO the move of the
    # researcher's graph out of their project was completely silent
    # (adversarial review of the V4 work), which section 8.10's "one log
    # line ... so nothing is hidden" rules out.
    logger.warning("RCE moved this project's graph out of the project: %s -> %s", legacy, target)
    # Section 8.10 rule 1 (amended): the researcher who opens `.rce/` and
    # finds no `graph.db` must find the answer in the same folder -- the
    # same one-line signpost `rce init` writes. Only here, after the move
    # succeeded; every failure above raised before reaching this line. A
    # signpost that cannot be written does not undo a completed move, so
    # that failure is logged, not raised.
    try:
        write_project_readme(project_root)
    except OSError as exc:
        logger.warning("could not write %s (%s)", project_rce_dir(project_root) / README_FILENAME, exc)
    return target


def resolve_graph_db(project_root: str | Path) -> Path:
    """`graph_db_path`, preceded by the one-time legacy migration -- what
    every `_require_db` copy in this codebase calls, so "first touch by any
    subcommand or the server" is literally any of them.

    Split from `graph_db_path` on purpose: asking where a graph lives (for
    a status line, an error message, a canvas path) must stay a pure
    computation with no filesystem side effect and no exception to catch.
    Raises `GraphMigrationError` if a legacy graph exists but could not be
    verified after copying. The move applies only to a folder with no id
    (a pre-V5 project); see `migrate_legacy_graph`."""
    migrate_legacy_graph(project_root)
    return graph_db_path(project_root)


# -- macOS dataless (iCloud-evicted) files -------------------------------------


def is_dataless(path: str | Path) -> bool:
    """Whether macOS has evicted `path`'s content to iCloud (`SF_DATALESS`).

    `os.stat` reports the flag without materializing the file -- it is
    `open()` that blocks, for as long as the download takes -- so this is
    the one check that can be made *before* committing a handler thread to
    a file that may not be local. False everywhere but macOS (no such
    flag, and `st_flags` does not exist), and False for a path that cannot
    be statted at all: "is it evicted" is not the question a missing file
    answers, and its own error belongs to whoever tries to open it."""
    if sys.platform != "darwin":
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)
