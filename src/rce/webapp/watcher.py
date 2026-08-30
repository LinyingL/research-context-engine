"""Auto-refresh file watcher for the local web view (task V3 phase 2).

A daemon polling thread owned by `rce.webapp.server.RceHTTPServer`: every
`interval` seconds (~2s in production, injectable for tests) it stats a
*bounded* watch set for the currently served project and, when something
changed, re-runs the same in-process ingest the CLI would -- then bumps a
generation counter the frontend polls (`GET /api/generation`) to know it
should re-fetch.

Polling `os.stat`, not FSEvents/inotify/watchdog, on purpose: this project
ships with `dependencies = []` (pyproject.toml, DESIGN.md section 0's
"simplest thing that works"), the stdlib has no portable filesystem-event
API, and a 2-second stat over a handful of files is invisible on a local
single-user tool. The watch set is deliberately small and enumerable --
never a recursive walk of the whole project:

  - `.rce/attempts.toml` itself (so creating or fixing the config is
    noticed, and so a `steps_dir`/`file` change re-shapes the watch set on
    the very next poll -- the snapshot is rebuilt from the config each
    time);
  - the attempts source Markdown file the config's `file` key names;
  - every file directly inside `steps_dir` -- one level only, no
    recursion: `rce.ingest.attempts._resolve_step_files` itself only ever
    links files at that level, so watching deeper would watch things no
    view is derived from.

A file that does not (yet) exist is simply absent from the snapshot, so a
change is any difference in the (path -> (mtime_ns, size)) mapping: an
edit, a deletion, or a new file appearing all compare unequal. When the
config itself is missing or unloadable the watch set degrades to the
config path alone -- nothing to poll, nothing to spuriously re-ingest,
and the moment a working config appears the snapshot changes and ingest
runs.

What "re-ingest" means here (reuse, never re-implement -- the same rule
`rce.webapp.server`'s payloads follow): the attempts half is exactly what
`rce.cli.cmd_attempts` runs (`attempts_ingest.load_config` +
`ingest_attempts_repo`); when the change touched `steps_dir`, the
dataflow half is exactly what `rce.cli.cmd_ingest` runs for its dataflow
step (`git`-tracked inventory with the same `NotAGitRepositoryError`
filesystem-walk fallback, then `dataflow_ingest.ingest_dataflow_repo`) --
the piece of the full ingest the tree/lineage views are actually derived
from, cheap enough to re-run on a local project. A map-file-only edit
never re-runs dataflow; nothing about commits/latex/mlflow is re-ingested
here at all (an edited step script or attempt row changes none of those).

Failure containment: a half-saved table, a heading mid-rename, a script
with a syntax hiccup -- the user editing their own files *will* produce
transient ingest failures, and none of them may kill the watcher or the
server. Every exception from a re-ingest is caught, logged, and remembered
as `last_error` for `GET /api/generation`'s status payload; polling
continues, and the next successful re-ingest clears it. The generation
counter is bumped even for a failed re-ingest: the files on disk really
did change (a `/api/file` preview is already stale), and the frontend
pairs the re-fetch with the error chip rather than silently showing old
data as if nothing happened.

Thread-safety: `status_payload`/`retarget` are called from HTTP handler
threads while `poll_once` runs on the watcher thread, so all mutable
state sits behind `_state_lock`. `_ingest_lock` separately serializes the
re-ingest itself. A project switch (`retarget`, called by the
`/api/projects/switch` handler) bumps an internal epoch; a `poll_once`
that was already mid-ingest against the *old* root notices the epoch
moved and discards its baseline/error writes instead of clobbering the
fresh state -- the switch's own generation bump already told the frontend
to re-fetch, and the next poll re-baselines against the new root without
ingesting (switching projects is not evidence anything in the new project
changed).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rce import db
from rce.ingest import attempts as attempts_ingest
from rce.ingest import dataflow as dataflow_ingest
from rce.ingest import files as files_ingest
from rce.ingest import git as git_ingest

logger = logging.getLogger(__name__)

# Same constants as rce.cli / rce.webapp.server / rce.webapp.registry --
# each subsystem owns its copy (existing convention in this codebase).
RCE_DIRNAME = ".rce"
DB_FILENAME = "graph.db"

DEFAULT_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True)
class WatchSnapshot:
    """One poll's view of the watch set: every watched file that currently
    exists, mapped to `(st_mtime_ns, st_size)` -- nanosecond mtime so two
    saves inside the same second still differ on filesystems that record
    it, with size as the second signal for those that do not. `steps_paths`
    remembers which of those files live directly in `steps_dir`, so a
    change can be classified as dataflow-relevant without re-deriving the
    config at diff time."""

    files: dict[str, tuple[int, int]]
    steps_paths: frozenset[str]


def _stat_entry(path: Path) -> tuple[int, int] | None:
    """`(mtime_ns, size)` for `path`, or None if it cannot be statted --
    a missing file is an ordinary member-absent state, never an error."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def take_snapshot(project_root: Path) -> WatchSnapshot:
    """Stat the bounded watch set for `project_root` (module docstring):
    the attempts config, the source Markdown it names, and the files one
    level inside `steps_dir`. Rebuilt from the config on every call, so a
    config edit re-shapes what the next poll watches with no extra
    bookkeeping."""
    files: dict[str, tuple[int, int]] = {}
    steps: set[str] = set()

    config_path = project_root / attempts_ingest.CONFIG_RELATIVE_PATH
    entry = _stat_entry(config_path)
    if entry is not None:
        files[str(config_path)] = entry

    try:
        config = attempts_ingest.load_config(project_root)
    except attempts_ingest.AttemptsConfigError:
        # Missing or currently-unusable config: watch only the config file
        # itself. The moment a working one is saved, the snapshot changes
        # and the poll re-ingests -- no guessing at which file to watch.
        return WatchSnapshot(files=files, steps_paths=frozenset())

    source_entry = _stat_entry(project_root / config.file)
    if source_entry is not None:
        files[str(project_root / config.file)] = source_entry

    if config.steps_dir:
        steps_dir = project_root / config.steps_dir
        try:
            children = sorted(steps_dir.iterdir())
        except OSError:
            children = []  # missing/unreadable steps_dir: nothing there to watch
        for child in children:
            if not child.is_file():
                continue  # one level only -- a subdirectory is never descended into
            child_entry = _stat_entry(child)
            if child_entry is not None:
                files[str(child)] = child_entry
                steps.add(str(child))
    return WatchSnapshot(files=files, steps_paths=frozenset(steps))


def _steps_changed(old: WatchSnapshot, new: WatchSnapshot) -> bool:
    """Whether any changed/appeared/vanished path in old->new was a
    steps_dir member on either side -- the signal that the dataflow half
    of the re-ingest is worth running at all."""
    differing = set(old.files.items()) ^ set(new.files.items())
    differing_paths = {path for path, _ in differing}
    return bool(differing_paths & (old.steps_paths | new.steps_paths))


class ProjectWatcher:
    """The polling watcher itself. Owned by `RceHTTPServer` (one per server
    process); `get_project_root` is the server's own locked accessor, read
    fresh at the top of every poll so a project switch re-targets polling
    with no extra wiring beyond `retarget()`.

    `poll_once` is public and thread-free on purpose: tests drive the whole
    change-detect -> re-ingest -> generation-bump cycle deterministically
    by calling it directly, and the background thread (`start`) is nothing
    but `poll_once` on a stop-event timer."""

    def __init__(
        self,
        get_project_root: Callable[[], Path],
        interval: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._get_project_root = get_project_root
        self._interval = interval
        # Guards every mutable field below; never held across an ingest.
        self._state_lock = threading.Lock()
        # Serializes the re-ingest itself, held only while ingesting.
        self._ingest_lock = threading.Lock()
        self._generation = 1
        self._refreshing = False
        self._last_error: str | None = None
        self._baseline: WatchSnapshot | None = None
        self._baseline_root: Path | None = None
        self._epoch = 0  # bumped by retarget(); lets a mid-ingest poll notice a switch
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- status / retarget (called from HTTP handler threads) -----------------

    def status_payload(self) -> dict[str, object]:
        """`GET /api/generation`'s body, verbatim: `{generation,
        refreshing, last_error}` -- one locked read, JSON-ready."""
        with self._state_lock:
            return {
                "generation": self._generation,
                "refreshing": self._refreshing,
                "last_error": self._last_error,
            }

    def retarget(self) -> None:
        """A project switch happened: drop the old root's baseline (the next
        poll re-baselines against the new root without ingesting -- a switch
        is not evidence anything in the new project changed), forget the old
        root's error (it described a project no longer served), and bump the
        generation so the frontend re-fetches. The epoch bump makes a poll
        already mid-ingest against the old root discard its own final
        baseline/error writes (see `poll_once`)."""
        with self._state_lock:
            self._epoch += 1
            self._baseline = None
            self._baseline_root = None
            self._last_error = None
            self._generation += 1

    # -- the poll cycle --------------------------------------------------------

    def poll_once(self) -> bool:
        """One full cycle: snapshot, compare against the baseline, and on a
        difference re-ingest + bump the generation. Returns whether a change
        was acted on (a test convenience; the thread ignores it).

        The first poll after construction or `retarget` only establishes
        the baseline -- serving a project is not evidence it changed, so
        nothing is ingested and the generation stays put."""
        root = self._get_project_root()
        snapshot = take_snapshot(root)
        with self._state_lock:
            epoch = self._epoch
            baseline, baseline_root = self._baseline, self._baseline_root
            if baseline is None or baseline_root != root:
                self._baseline, self._baseline_root = snapshot, root
                return False
            if snapshot.files == baseline.files:
                return False
            self._refreshing = True

        steps_changed = _steps_changed(baseline, snapshot)
        error: str | None = None
        try:
            with self._ingest_lock:
                self._reingest(root, steps_changed)
        except Exception as exc:  # noqa: BLE001 -- containment is the whole point
            # A half-saved table or a mid-edit script must never kill the
            # watcher (module docstring): remember the failure for the
            # status endpoint and keep polling -- the next good save both
            # re-ingests and clears this.
            logger.exception("auto re-ingest of %s failed -- watcher keeps polling", root)
            error = str(exc)

        with self._state_lock:
            self._refreshing = False
            if self._epoch != epoch:
                # A switch landed while this poll was ingesting: retarget()
                # already reset the baseline and bumped the generation for
                # the *new* root -- committing this poll's old-root results
                # on top would resurrect exactly the state it cleared.
                return True
            self._baseline, self._baseline_root = snapshot, root
            self._last_error = error
            self._generation += 1
        return True

    def _reingest(self, root: Path, steps_changed: bool) -> None:
        """Re-run the ingests the changed files feed (module docstring):
        always the attempts ingest -- exactly `rce.cli.cmd_attempts`'s own
        calls -- plus, only when the change touched `steps_dir`, the same
        dataflow step `rce.cli.cmd_ingest` runs. Raises on failure; the
        caller (`poll_once`) is the one place that catches and records."""
        db_path = root / RCE_DIRNAME / DB_FILENAME
        if not db_path.exists():
            # Never let db.connect() conjure a fresh graph.db inside a
            # project that was never `rce init`ed -- the served project is
            # validated as initialized at serve/switch time, so this only
            # trips if the database vanished mid-serve, which the user
            # should hear about via last_error rather than have papered
            # over with an empty new file.
            raise RuntimeError(
                f"no RCE project at {root} (missing {RCE_DIRNAME}/{DB_FILENAME}); "
                "the graph database disappeared while being served"
            )
        conn = db.connect(db_path)
        try:
            config = attempts_ingest.load_config(root)
            counts = attempts_ingest.ingest_attempts_repo(conn, root, config)
            logger.info("watcher re-ingested attempts for %s: %s", root, counts)
            if steps_changed:
                self._reingest_dataflow(conn, root)
        finally:
            conn.close()

    def _reingest_dataflow(self, conn, root: Path) -> None:
        """The dataflow slice of `rce.cli.cmd_ingest`, reused not
        re-implemented: the git-tracked inventory with the same
        `NotAGitRepositoryError` -> filesystem-walk degradation `cmd_ingest`
        applies (W1), then `ingest_dataflow_repo` over the .py/.R/.Rmd
        lists. Any other `GitIngestError` propagates to `poll_once`'s
        containment, mirroring `cmd_ingest` treating it as fatal for the
        run rather than guessing at an inventory."""
        try:
            inventory = git_ingest.list_source_files(root)
        except git_ingest.NotAGitRepositoryError:
            inventory = files_ingest.list_source_files(root)
        counts = dataflow_ingest.ingest_dataflow_repo(
            conn, root, inventory["py"], inventory["r"], inventory["rmd"],
        )
        logger.info("watcher re-ingested dataflow for %s: %s", root, counts)

    # -- background thread -----------------------------------------------------

    def start(self) -> None:
        """Spawn the daemon polling thread; idempotent (a second call while
        the thread is alive is a no-op, so `serve()` restarting after a
        test's manual `start()` never doubles the polling)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="rce-project-watcher", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the polling thread if one is running; safe to call when it
        never was (`RceHTTPServer.server_close` calls this unconditionally)."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        # wait() first, poll after: the server's first page load should not
        # race an ingest, and an immediate first poll would only establish
        # the baseline anyway.
        while not self._stop_event.wait(self._interval):
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 -- the thread must outlive any bug here
                # poll_once already contains ingest failures; this guards the
                # snapshot/compare machinery itself (e.g. an OSError shape no
                # one anticipated) -- log and keep the thread alive.
                logger.exception("watcher poll cycle failed -- watcher keeps polling")
