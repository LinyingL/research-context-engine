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
that says where the graph went. `canvas.json` (section 8.6) is derived
too, so `canvas_state_path` puts it beside the graph, never in the project.

The `<id>` is a stable hash of the project's *resolved* path -- resolved,
so `/tmp/p` and its `/private/tmp/p` symlink target are one project rather
than two graphs that silently disagree; hashed rather than path-shaped, so
a project whose name carries `/`, non-ASCII, or a 300-character directory
name still gets a short, ASCII, filesystem-safe directory (this codebase
has already paid once for a non-ASCII path assumption). The id is one-way
on purpose: `rce status`, `/api/summary` and the project's own `.rce/README`
are how you go from a project to its graph; nothing needs the reverse.

A project that moves gets a new id and therefore an empty graph -- correct
rather than clever: the ingest is deterministic and re-running it is cheap,
whereas a graph that followed a path it can no longer verify would be a
graph about a directory that may now hold something else entirely.

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

`migrate_legacy_graph` is the one-time move, run on first touch by any
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

import hashlib
import logging
import os
import sqlite3
import sys
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


def project_graph_id(project_root: str | Path) -> str:
    """The stable `<id>` for `project_root`: a truncated SHA-256 of its
    *resolved* absolute path. Resolution is what makes two spellings of one
    project (a relative path, a symlinked `/tmp` on macOS, a trailing
    slash) share one graph instead of quietly forking into two."""
    resolved = str(Path(project_root).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:_ID_LENGTH]


def graph_dir(project_root: str | Path) -> Path:
    """`~/.rce/graphs/<id>` -- the directory holding everything RCE derives
    about this project (the graph now, `canvas.json` from section 8.6).
    Computing it never creates it; see `ensure_graph_dir`."""
    return rce_home() / GRAPHS_DIRNAME / project_graph_id(project_root)


def graph_db_path(project_root: str | Path) -> Path:
    """THE path to a project's graph database. Every `_require_db` copy in
    this codebase resolves through here (via `resolve_graph_db`) rather
    than joining `.rce/graph.db` itself -- one definition, so the location
    can never drift between the CLI, the MCP server, the web server, the
    watcher and the map-file writer."""
    return graph_dir(project_root) / DB_FILENAME


def canvas_state_path(project_root: str | Path) -> Path:
    """`canvas.json` (DESIGN.md section 8.6: node positions and last
    viewport) -- beside the graph, not in the project, because it is
    derived UI state, safe to delete, and not something the researcher is
    expected to read or commit. Exposed now so section 8.6's implementer
    inherits the location instead of re-deciding it."""
    return graph_dir(project_root) / CANVAS_FILENAME


def project_rce_dir(project_root: str | Path) -> Path:
    """`<project>/.rce` -- the researcher-owned half: `attempts.toml`,
    `mappings.toml`, `backups/`, `README`. No database lives here."""
    return Path(project_root) / RCE_DIRNAME


def legacy_graph_db_path(project_root: str | Path) -> Path:
    """Where the graph used to live, before section 8.10 rule 1. Only
    `migrate_legacy_graph` and `graph_exists` have any business reading
    this -- nothing opens a database here anymore."""
    return project_rce_dir(project_root) / DB_FILENAME


def graph_exists(project_root: str | Path) -> bool:
    """Whether `project_root` is an initialized RCE project -- the external
    graph exists, OR a legacy in-project one does and has simply not been
    migrated yet. Both count: a project whose graph is still in the old
    place is initialized, and refusing to serve it would leave the user
    unable to trigger the very migration that fixes it."""
    return graph_db_path(project_root).exists() or legacy_graph_db_path(project_root).exists()


def ensure_graph_dir(project_root: str | Path) -> Path:
    """Create `~/.rce/graphs/<id>` if needed and return it. Called by
    `rce init` (and by the migration) -- deliberately NOT by
    `graph_db_path`, so merely asking where a graph would live never
    litters `~/.rce/graphs` with directories for projects that were never
    initialized."""
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
    rce_dir = project_rce_dir(project_root)
    rce_dir.mkdir(parents=True, exist_ok=True)
    readme = rce_dir / README_FILENAME
    readme.write_text(_README_TEMPLATE.format(graph_dir=graph_dir(project_root)), encoding="utf-8")
    return readme


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


def migrate_legacy_graph(project_root: str | Path) -> Path | None:
    """Move `<project>/.rce/graph.db` to `~/.rce/graphs/<id>/graph.db`, once.

    Returns the new path when a migration actually happened, None when
    there was nothing to do (no legacy file, or an external graph already
    exists -- in which case the legacy file is left alone rather than
    guessed about: two graphs is a situation for a human, not for a
    silent overwrite in either direction).

    Order is copy -> verify -> delete, never delete-before-verify: the copy
    lands on a `.migrating` staging name, `PRAGMA integrity_check` must
    answer `ok`, and only then is the staging file renamed into place and
    the legacy file (with its `-wal`/`-shm`) removed. Any failure raises
    `GraphMigrationError` with everything still where it was.
    """
    legacy = legacy_graph_db_path(project_root)
    target = graph_db_path(project_root)
    if target.exists() or not legacy.exists():
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".migrating")
    _unlink_quietly(staging)  # a previous crashed attempt's leftover
    try:
        verdict = _backup_database(legacy, staging)
    except (sqlite3.Error, OSError) as exc:
        _unlink_quietly(staging)
        raise GraphMigrationError(
            f"could not copy the graph out of {legacy} to {target}: {exc}. "
            f"Nothing was deleted -- the original graph is still at {legacy}."
        ) from exc
    if verdict != "ok":
        _unlink_quietly(staging)
        raise GraphMigrationError(
            f"the copy of {legacy} failed PRAGMA integrity_check ({verdict!r}), so it was "
            f"discarded. Nothing was deleted -- the original graph is still at {legacy}."
        )

    os.replace(staging, target)
    _unlink_quietly(legacy)
    for sidecar in _sidecars(legacy):
        _unlink_quietly(sidecar)
    # One line, not one per file moved: this happens once in a project's
    # life and the user needs exactly one fact from it.
    logger.info("moved graph out of the project: %s -> %s", legacy, target)
    return target


def resolve_graph_db(project_root: str | Path) -> Path:
    """`graph_db_path`, preceded by the one-time legacy migration -- what
    every `_require_db` copy in this codebase calls, so "first touch by any
    subcommand or the server" is literally any of them.

    Split from `graph_db_path` on purpose: asking where a graph lives (for
    a status line, an error message, a canvas path) must stay a pure
    computation with no filesystem side effect and no exception to catch.
    Raises `GraphMigrationError` if a legacy graph exists but could not be
    verified after copying."""
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
