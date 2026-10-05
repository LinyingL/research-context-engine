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
    view is derived from;
  - `.rce/mappings.toml` (DESIGN.md section 8.5: "the file joins the
    watch set"), watched whether or not an attempts config exists -- the
    hand-drawn links do not depend on the attempt timeline.

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
A change to `.rce/mappings.toml` re-runs exactly what `rce mappings` runs
(`mappings_ingest.ingest_mappings`), and a change touching ONLY that file
runs nothing else -- in particular not the attempts ingest, which would
fail outright on a project that has mappings but no attempts config. Its
failures (an unparseable file mid-save) are contained exactly like an
attempts failure; refused individual entries are not failures -- they are
logged by the ingest and left for the canvas to show, not raised into the
refresh chip.

Since V5 (DESIGN.md 9.1, 9.2) the record files join the watch set too:
`.rce/judgements.toml` and `.rce/canvas.json`. A change to the judgment
ledger -- a hand edit, a `git pull`, another engine's or the CLI's write --
re-applies the ledger to the index (`rce.records.judgements.apply_ledger`)
and nothing else; a change to the arrangement only bumps the generation, so
open pages re-read it. Every re-ingest ends by applying the ledger too (the
end of a scan), and the first poll that sees a root applies it once, beside
the mappings sync, so a judgment made while the app was closed reaches the
index without the file having to be touched again. A ledger RCE cannot
trust (refused, unreadable, shrunk) is never applied; its state is reported
under `records` in the status payload, and polling goes on.

A vanished graph is not a transient failure (DESIGN.md section 8.10
rule 2). Observed in real use: the graph disappeared mid-serve and this
watcher raised the same "graph database disappeared" error -- with a full
traceback -- once per 2-second poll, forever, into the log. So the missing
graph is now checked FIRST, before the snapshot is even compared: the
watcher logs one line, records it as `last_error`, bumps the generation
once (so open pages re-fetch and land on the header state 「项目不可用 —
图谱文件已不存在」), and then stops re-ingesting this root entirely until the
file reappears. The baseline is deliberately left untouched while the
graph is away, so an edit made during the outage is still a visible
difference when the graph comes back and the ordinary cycle resumes.

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

Thread-safety: `status_payload`/`retarget`/`record_external_change` are
called from HTTP handler threads while `poll_once` runs on the watcher
thread, so all mutable state sits behind `_state_lock`. `_ingest_lock`
separately serializes the re-ingest itself -- and is public (task V3
phase 3) so the UI write path (`rce.webapp.mapedit` via
`/api/attempts/write`) ingests under the same lock; after such a write,
`record_external_change` re-baselines the map/config half of the watch
set (never the steps_dir half -- a UI write runs no dataflow ingest, so
a step-file change pending since the last poll must stay visible to the
next one; see `_absorb_non_steps_only`) and bumps the generation so the
watcher neither re-ingests the change a second time nor leaves open
pages unaware of it. A project switch (`retarget`, called by the
`/api/projects/switch` handler) bumps an internal epoch; a `poll_once`
that was already mid-ingest against the *old* root notices the epoch
moved and discards its baseline/error writes instead of clobbering the
fresh state -- the switch's own generation bump already told the frontend
to re-fetch, and the next poll re-baselines against the new root without
ingesting (switching projects is not evidence anything in the new project
changed).

One exception to "serving is not evidence anything changed": the mappings
file. `.rce/mappings.toml` is the ONLY truth for hand-drawn links, `rce
ingest` does not read it, and nothing else ingests it while the app is
closed -- so an edit made then (by hand, by `git pull`, by another
checkout) would otherwise stay invisible until the file happened to be
touched again: removed entries still drawn as human links (whose
「删除标注」 then 404s, because the file no longer has them), new entries
never drawn (adversarial review of the V4 work). So the first poll that
sees a root also runs the mappings ingest once (`_sync_mappings_on_first_
sight`) -- cheap, idempotent, and a no-op for the generation when the
graph already matched. A failure there is contained like any other, shown
as `last_error`, and retried on the next poll until it lands.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, ContextManager

from rce import db, paths
from rce.ingest import attempts as attempts_ingest
from rce.ingest import dataflow as dataflow_ingest
from rce.ingest import files as files_ingest
from rce.ingest import git as git_ingest
from rce.ingest import mappings as mappings_ingest
from rce.ingest import scan as scan_mod
from rce.records import judgements
from rce.records import ledger as ledger_mod

logger = logging.getLogger(__name__)

# The graph's location is rce.paths' business alone since DESIGN.md
# section 8.10 rule 1; these stay as module attributes only for callers
# that quote them.
RCE_DIRNAME = paths.RCE_DIRNAME
DB_FILENAME = paths.DB_FILENAME

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
    the mappings file, the attempts config, the source Markdown it names,
    and the files one level inside `steps_dir`. Rebuilt from the config on every call, so a
    config edit re-shapes what the next poll watches with no extra
    bookkeeping."""
    files: dict[str, tuple[int, int]] = {}
    steps: set[str] = set()

    for record in record_paths(project_root):
        record_entry = _stat_entry(record)
        if record_entry is not None:
            files[str(record)] = record_entry

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


def record_paths(project_root: Path) -> tuple[Path, Path, Path]:
    """The record files watched whatever the attempts config says:
    mappings (8.5), the judgment ledger and the arrangement (9.2)."""
    return (
        mappings_ingest.mappings_path(project_root),
        ledger_mod.judgements_path(project_root),
        canvas_record_path(project_root),
    )


def canvas_record_path(project_root: Path) -> Path:
    return project_root / paths.RCE_DIRNAME / "canvas.json"


def _steps_changed(old: WatchSnapshot, new: WatchSnapshot) -> bool:
    """Whether any changed/appeared/vanished path in old->new was a
    steps_dir member on either side -- the signal that the dataflow half
    of the re-ingest is worth running at all."""
    differing = set(old.files.items()) ^ set(new.files.items())
    differing_paths = {path for path, _ in differing}
    return bool(differing_paths & (old.steps_paths | new.steps_paths))


def _changed_paths(old: WatchSnapshot, new: WatchSnapshot) -> set[str]:
    """Every path that changed, appeared or vanished between old and new."""
    return {path for path, _ in set(old.files.items()) ^ set(new.files.items())}


def _absorb_non_steps_only(old: WatchSnapshot, fresh: WatchSnapshot) -> WatchSnapshot:
    """The baseline `record_external_change` may commit: the fresh
    snapshot's view of the map/config files (the UI write's own edit,
    absorbed so the next poll does not re-ingest it a second time), but
    the OLD baseline's view of every steps_dir member. A UI write runs
    only the attempts ingest -- never dataflow -- so a step-file change
    that landed after the last poll but before the write has NOT been
    dataflow-ingested yet; re-baselining it to its on-disk state here
    would absorb it unseen, and no later map-only save would ever repair
    that (a real missed-ingest, adversarial-review finding -- the
    docstring's "benign race" only ever covered the redundant-re-ingest
    direction). Carrying the old entries forward keeps that change a
    visible difference for the next poll, whose ordinary cycle then runs
    the dataflow half. `steps_paths` is the union of both sides so a
    member that appeared or vanished in the gap stays classified as a
    steps change either way."""
    steps = old.steps_paths | fresh.steps_paths
    files = {path: entry for path, entry in fresh.files.items() if path not in steps}
    for path in steps:
        if path in old.files:
            files[path] = old.files[path]
    return WatchSnapshot(files=files, steps_paths=frozenset(steps))


def _absorb_only(old: WatchSnapshot, fresh: WatchSnapshot, absorb: frozenset[str]) -> WatchSnapshot:
    """The narrower baseline a write that re-ingested only SOME of the
    watch set commits (the canvas's mapping writes, DESIGN.md section 8.5,
    which run the mappings ingest and nothing else): the fresh state of the
    `absorb` paths, the OLD baseline's state of every other path. The same
    missed-ingest reasoning as `_absorb_non_steps_only`, one step further:
    an attempts-map save that landed just before a mapping write has not
    been attempts-ingested by that write, so it must stay a visible
    difference for the next poll."""
    files: dict[str, tuple[int, int]] = {}
    for path in set(old.files) | set(fresh.files):
        source = fresh if path in absorb else old
        if path in source.files:
            files[path] = source.files[path]
    return WatchSnapshot(files=files, steps_paths=old.steps_paths | fresh.steps_paths)


def _graph_location(root: Path) -> str:
    try:
        return str(paths.graph_db_path(root))
    except paths.GraphMigrationError:  # an identity file that cannot be read right now
        return "(unknown: the project identity file cannot be read)"


def _graph_present(root: Path) -> bool:
    """Whether the served root's graph is there. A folder that moved away
    or whose identity file cannot be read has, for the watcher, no graph:
    one log line and silence (section 8.10 rule 2), never a traceback per
    poll."""
    try:
        return paths.graph_db_path(root).exists()
    except paths.GraphMigrationError:
        return False


class _GuardedLock:
    """`ProjectWatcher.ingest_lock` as callers see it: the write guard
    (outer) and the in-process ingest lock (inner), entered and left as
    one context manager -- so `mapedit.apply_edit(ingest_lock=...)` and
    `with watcher.ingest_lock:` need no change to take turns across
    processes too."""

    def __init__(self, lock: threading.Lock, guard: Callable[[], ContextManager[object]]) -> None:
        self._lock = lock
        self._guard = guard
        self._local = threading.local()

    def __enter__(self) -> "_GuardedLock":
        stack = contextlib.ExitStack()
        stack.enter_context(self._guard())
        try:
            stack.enter_context(self._lock)
        except BaseException:
            stack.close()
            raise
        frames = getattr(self._local, "frames", None)
        if frames is None:
            frames = self._local.frames = []
        frames.append(stack)
        return self

    def __exit__(self, *exc_info: object) -> bool:
        stack = self._local.frames.pop()
        return bool(stack.__exit__(*exc_info))

    def locked(self) -> bool:
        """Whether the in-process ingest lock is held (as `threading.Lock`)."""
        return self._lock.locked()

    def acquire(self) -> bool:
        """`threading.Lock`'s spelling of entering (blocking)."""
        self.__enter__()
        return True

    def release(self) -> None:
        self.__exit__(None, None, None)


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
        write_guard: Callable[[], ContextManager[object]] | None = None,
        active: Callable[[], bool] | None = None,
    ) -> None:
        self._get_project_root = get_project_root
        self._interval = interval
        # Guards every mutable field below; never held across an ingest.
        self._state_lock = threading.Lock()
        # Serializes the re-ingest itself, held only while ingesting.
        self._ingest_lock = threading.Lock()
        # V5 (DESIGN.md 9.4, 9.7): every ingest is an index write, so it
        # runs inside the server's write guard (the cross-process project
        # lock plus the identity re-check), ALWAYS taken before the
        # in-process ingest lock -- one order for every thread, so a
        # request holding the guard and the watcher can never deadlock.
        self._write_guard = write_guard or contextlib.nullcontext
        # Whether the served project may be polled at all (not while it is
        # blocked on a question or opened read-only).
        self._active = active or (lambda: True)
        self._guarded_ingest_lock = _GuardedLock(self._ingest_lock, self._write_guard)
        self._generation = 1
        self._refreshing = False
        self._last_error: str | None = None
        self._baseline: WatchSnapshot | None = None
        self._baseline_root: Path | None = None
        # Section 8.10 rule 2: the root whose graph is currently missing,
        # or None. Remembering WHICH root is what makes the log line fire
        # once instead of once per poll, while still firing again for a
        # different project whose graph is also gone.
        self._graph_missing_root: Path | None = None
        # The root whose mappings file has been ingested since this watcher
        # first saw it (module docstring, the mappings exception), and
        # whether the last such attempt failed (so its error is logged once
        # and cleared by the attempt that finally lands).
        self._mappings_synced_root: Path | None = None
        self._mappings_sync_failed = False
        # The judgment ledger's trust state as the last application found
        # it (9.3), reported by `status_payload` under `records`.
        self._records: dict[str, object] | None = None
        self._epoch = 0  # bumped by retarget(); lets a mid-ingest poll notice a switch
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- status / retarget / external writes (called from HTTP handler threads) --

    @property
    def ingest_lock(self) -> "_GuardedLock":
        """The lock serializing every re-ingest of the served project.
        Exposed (task V3 phase 3) so the UI write path
        (`rce.webapp.mapedit.apply_edit`, via the `/api/attempts/write`
        handler) runs its own write+re-ingest under the SAME lock this
        watcher's `poll_once` ingests under -- the two must never
        interleave, and two locks could only ever drift apart. Since V5 it
        is the guarded form (`_GuardedLock`): entering it also takes the
        project write guard, first."""
        return self._guarded_ingest_lock

    def bump_generation(self) -> int:
        """The graph changed without any watched FILE changing (a human
        marked a link as a wrong extraction, or restored one -- a status
        written straight through `db.set_edge_status`): bump the generation
        so every open page re-fetches, and touch nothing else -- no
        baseline is re-taken (nothing on disk was ingested) and
        `last_error` is left as it is (nothing was re-ingested to clear
        it). Returns the new generation."""
        with self._state_lock:
            self._generation += 1
            return self._generation

    def record_external_change(
        self, error: str | None = None, absorb: Collection[str] | None = None,
    ) -> int:
        """A UI write (task V3 phase 3) just edited a watched file and ran
        its own re-ingest in-process: re-baseline the map/config half of
        the watch set to what is on disk NOW, so the next poll does not
        re-detect and re-ingest the same change, and bump the generation
        so every open page's next poll re-fetches -- the exact effect a
        poll-detected change would have had. The steps_dir half of the
        baseline is NOT re-taken here: a UI write never runs the dataflow
        ingest, so absorbing a step file's fresh mtime would swallow a
        change the watcher still owes an ingest for -- see
        `_absorb_non_steps_only` for the full failure this used to cause.
        `error` is the write path's own contained post-write ingest
        failure (or None on success, which also clears a stale earlier
        error -- same "next good ingest clears it" rule as `poll_once`).
        Returns the new generation so the write endpoint can put it in
        its response.

        Benign race, on purpose: a poll that was already mid-cycle can
        commit its own (pre-write) baseline right after this -- the next
        poll then re-detects the write and re-ingests once more. Ingest is
        idempotent and serialized by `ingest_lock`, so the cost is one
        redundant re-ingest and generation bump, never corruption -- the
        same shape as `poll_once`'s own epoch note, without needing the
        epoch machinery (nothing here must *discard* anything).

        `absorb` (DESIGN.md section 8.5's mapping writes): when given, ONLY
        these watched paths are re-baselined -- for a write whose own
        re-ingest covered just those files -- via `_absorb_only`. None
        keeps the attempts write's behaviour above."""
        root = self._get_project_root()
        snapshot = take_snapshot(root)
        with self._state_lock:
            if self._baseline is not None and self._baseline_root == root:
                if absorb is not None:
                    snapshot = _absorb_only(self._baseline, snapshot, frozenset(absorb))
                else:
                    snapshot = _absorb_non_steps_only(self._baseline, snapshot)
            self._baseline, self._baseline_root = snapshot, root
            self._last_error = error
            self._generation += 1
            return self._generation

    def status_payload(self) -> dict[str, object]:
        """`GET /api/generation`'s body, verbatim: `{generation,
        refreshing, last_error}` plus `records` while the judgment ledger
        cannot be trusted -- one locked read, JSON-ready."""
        with self._state_lock:
            payload: dict[str, object] = {
                "generation": self._generation,
                "refreshing": self._refreshing,
                "last_error": self._last_error,
            }
            # 9.3: a judgment ledger RCE cannot trust right now (refused,
            # unreadable, shrunk, a conflict copy) is reported -- present
            # only then, so a healthy project's payload keeps its V3 shape.
            if self._records is not None and self._records.get("state") != "ok":
                payload["records"] = self._records
            return payload

    def record_write(self, absorb: Collection[str], *, bump: bool = True) -> int:
        """A request just wrote these record files AND applied them itself
        (a judgment, an answer, an arrangement): re-baseline exactly those
        paths so the next poll does not act on them a second time, refresh
        the ledger's reported state, and (unless `bump=False`, for a drag
        that changed nothing any other view shows) bump the generation.
        Unlike `record_external_change`, `last_error` is left alone: the
        write re-ran no ingest, so it cannot have cleared one."""
        root = self._get_project_root()
        snapshot = take_snapshot(root)
        records = _read_records_state(root)
        with self._state_lock:
            if self._baseline is not None and self._baseline_root == root:
                self._baseline = _absorb_only(self._baseline, snapshot, frozenset(absorb))
            if records is not None:
                self._records = records
            if bump:
                self._generation += 1
            return self._generation

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
            # The old root's missing graph is not the new root's problem:
            # clear it, so a new project whose graph is also gone gets its
            # own (single) log line rather than being silenced by the old.
            self._graph_missing_root = None
            # The new root's mappings file has not been synced by us yet.
            self._mappings_synced_root = None
            self._records = None
            self._mappings_sync_failed = False
            self._generation += 1

    # -- the poll cycle --------------------------------------------------------

    def poll_once(self) -> bool:
        """One full cycle: snapshot, compare against the baseline, and on a
        difference re-ingest + bump the generation. Returns whether a change
        was acted on (a test convenience; the thread ignores it).

        The first poll after construction or `retarget` only establishes
        the baseline -- serving a project is not evidence it changed, so
        nothing is ingested and the generation stays put.

        A root whose graph has vanished is short-circuited before any of
        that (section 8.10 rule 2): nothing is snapshotted, compared or
        ingested, and the baseline is left exactly as it was so a change
        made during the outage is still pending when the file returns."""
        if not self._active():
            return False
        root = self._get_project_root()
        if not _graph_present(root):
            self._note_graph_missing(root)
            return False
        self._note_graph_present()
        # Snapshot BEFORE the first-sight sync reads the mappings file: an
        # edit landing between the two is then a visible difference for the
        # next poll (one redundant idempotent ingest), never absorbed unseen.
        snapshot = take_snapshot(root)
        synced = self._sync_mappings_on_first_sight(root)
        with self._state_lock:
            epoch = self._epoch
            baseline, baseline_root = self._baseline, self._baseline_root
            first_sight = baseline is None or baseline_root != root
            if first_sight:
                self._baseline, self._baseline_root = snapshot, root
            elif snapshot.files == baseline.files:
                return False
            else:
                self._refreshing = True
        if first_sight:
            # Edits made while the app was closed are seen now (9.2).
            self._snapshot_records(root, None)
            return synced

        steps_changed = _steps_changed(baseline, snapshot)
        changed = _changed_paths(baseline, snapshot)
        self._snapshot_records(root, changed)
        mappings_file, ledger_file, canvas_file = (str(p) for p in record_paths(root))
        mappings_changed = mappings_file in changed
        ledger_changed = ledger_file in changed
        attempts_changed = bool(changed - {mappings_file, ledger_file, canvas_file})
        error: str | None = None
        try:
            if attempts_changed or mappings_changed or ledger_changed:
                with self._guarded_ingest_lock:
                    self._reingest(
                        root, steps_changed, attempts=attempts_changed, mappings=mappings_changed,
                    )
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

    def _snapshot_records(self, root: Path, changed: set[str] | None) -> None:
        """9.2: a snapshot the first time RCE sees a record file changed
        each day -- the watcher is what sees hand edits (`inventory.
        snapshot_records`; `changed` None: every record file, on first
        sight). Under the write guard; never raises."""
        from rce import inventory  # noqa: PLC0415 -- leaf use

        try:
            with self._guarded_ingest_lock:
                inventory.snapshot_records(root, changed)
        except Exception:  # noqa: BLE001 -- a snapshot never stops the watcher
            logger.exception("snapshotting the record files of %s failed", root)

    def _apply_ledger(self, conn, root: Path) -> None:
        """Apply the judgment ledger (the last step of every ingest here)
        and remember its trust state for `status_payload`. An untrusted
        ledger is reported there, never raised: the watcher keeps going."""
        result = judgements.apply_ledger(conn, root)
        state = db.get_record_status(conn, judgements.RECORD_STATUS_NAME)
        with self._state_lock:
            self._records = state
        if result.decision is not None and not result.applied:
            logger.warning("watcher: judgment ledger of %s not applied (%s)", root, result.decision.reason)

    def _sync_mappings_on_first_sight(self, root: Path) -> bool:
        """Ingest `.rce/mappings.toml` once per root this watcher serves
        (module docstring, the mappings exception), so edits made while
        the app was closed reach the graph without the file having to be
        touched again. Under the ingest lock like every other ingest;
        bumps the generation only when the graph actually changed (an
        entry added, confirmed or removed), so serving an up-to-date
        project still costs open pages nothing. Returns whether it changed
        the graph.

        Failure is contained exactly like a poll's: logged once, reported
        as `last_error`, and retried on every later poll -- the root is
        only marked synced once an ingest has landed, which then also
        clears the error it had reported. A switch mid-sync (epoch moved)
        discards the result for the old root."""
        with self._state_lock:
            if self._mappings_synced_root == root:
                return False
            epoch = self._epoch
            already_failed = self._mappings_sync_failed
        try:
            with self._guarded_ingest_lock:
                conn = db.connect(paths.graph_db_path(root))
                try:
                    report = mappings_ingest.ingest_mappings(conn, root)
                    # 9.1: the record files are applied on first sight of a
                    # project, as mappings.toml is -- the judgment ledger too.
                    before = _human_state(conn)
                    self._apply_ledger(conn, root)
                    ledger_changed = _human_state(conn) != before
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001 -- containment, same as poll_once's
            if not already_failed:
                logger.exception("startup sync of %s for %s failed -- retrying each poll",
                                 mappings_ingest.MAPPINGS_RELATIVE_PATH, root)
            with self._state_lock:
                if self._epoch == epoch:
                    if not self._mappings_sync_failed:
                        self._generation += 1  # once: let open pages show the chip
                    self._mappings_sync_failed = True
                    self._last_error = str(exc)
            return False
        counts = report.counts
        changed = ledger_changed or any(
            counts.get(key, 0) for key in ("nodes_created", "edges_confirmed", "edges_removed", "nodes_removed")
        )
        if changed:
            logger.info("startup sync of mappings for %s: %s", root, counts)
        with self._state_lock:
            if self._epoch != epoch:
                return False
            self._mappings_synced_root = root
            if self._mappings_sync_failed:
                self._mappings_sync_failed = False
                self._last_error = None
                changed = True  # the error chip must clear on open pages
            if changed:
                self._generation += 1
        return changed

    def _note_graph_missing(self, root: Path) -> None:
        """First poll to find `root`'s graph gone: one log line (a warning,
        not an exception -- there is no traceback worth printing for "the
        file is not there"), `last_error` set so `GET /api/generation`
        reports it, and one generation bump so every open page re-fetches
        once and lands on the degraded header state. Every subsequent poll
        while it is still gone does nothing at all -- that silence IS the
        rule."""
        with self._state_lock:
            if self._graph_missing_root == root:
                return
            self._graph_missing_root = root
            self._last_error = (
                f"no RCE project at {root} (missing its graph at {_graph_location(root)}); "
                "the graph database disappeared while being served"
            )
            self._generation += 1
        logger.warning(
            "graph for %s is gone (%s) -- auto re-ingest paused for this project until it "
            "reappears", root, _graph_location(root),
        )

    def _note_graph_present(self) -> None:
        """The graph is readable. If it had been missing, say so once,
        clear the error and bump the generation so the degraded pages
        recover on their next poll; otherwise this is a no-op on the hot
        path (one lock acquisition, one comparison)."""
        with self._state_lock:
            if self._graph_missing_root is None:
                return
            recovered = self._graph_missing_root
            self._graph_missing_root = None
            self._last_error = None
            self._generation += 1
        logger.info("graph for %s is back -- auto re-ingest resumed", recovered)

    def _reingest(
        self, root: Path, steps_changed: bool, *, attempts: bool = True, mappings: bool = False,
    ) -> None:
        """Re-run the ingests the changed files feed (module docstring):
        the attempts ingest -- exactly `rce.cli.cmd_attempts`'s own calls --
        when anything but the mappings file changed, plus, only when the
        change touched `steps_dir`, the same dataflow step `rce.cli.
        cmd_ingest` runs; and the mappings ingest -- exactly `rce.cli.
        cmd_mappings`' call -- when `.rce/mappings.toml` changed. The two
        halves are independent: a failure in one does not skip the other
        (a half-saved attempt table must not keep a just-drawn link out of
        the graph, nor the reverse). Raises on failure -- the first
        failure itself when only one half failed, so its message reaches
        `last_error` unchanged; the caller (`poll_once`) is the one place
        that catches and records."""
        db_path = paths.graph_db_path(root)
        if not db_path.exists():
            # Never let db.connect() conjure a fresh graph.db where the
            # real one used to be -- the served project is validated as
            # initialized at serve/switch time, and `poll_once` now
            # short-circuits a missing graph before ever reaching here, so
            # this only trips on a vanish inside the poll cycle itself. It
            # stays as the last line of defense rather than being deleted:
            # `_reingest` must be safe to call on its own terms.
            raise RuntimeError(
                f"no RCE project at {root} (missing its graph at {db_path}); "
                "the graph database disappeared while being served"
            )
        conn = db.connect(db_path)
        errors: list[Exception] = []
        try:
            if attempts:
                try:
                    config = attempts_ingest.load_config(root)
                    counts = attempts_ingest.ingest_attempts_repo(conn, root, config)
                    logger.info("watcher re-ingested attempts for %s: %s", root, counts)
                    if steps_changed:
                        self._reingest_dataflow(conn, root)
                except Exception as exc:  # noqa: BLE001 -- re-raised below, after the other half
                    errors.append(exc)
            if mappings:
                try:
                    report = mappings_ingest.ingest_mappings(conn, root)
                    logger.info("watcher re-ingested mappings for %s: %s", root, report.counts)
                    for problem in report.problems:
                        logger.warning(
                            "%s %s refused: %s", mappings_ingest.MAPPINGS_RELATIVE_PATH,
                            problem.location(), problem.message,
                        )
                except Exception as exc:  # noqa: BLE001 -- re-raised below
                    errors.append(exc)
            # The end of every scan, and the reaction to the ledger itself
            # changing (9.1, 9.6): recompute the human state.
            try:
                self._apply_ledger(conn, root)
            except Exception as exc:  # noqa: BLE001 -- re-raised below
                errors.append(exc)
        finally:
            conn.close()
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise RuntimeError("; ".join(str(e) for e in errors))

    def _reingest_dataflow(self, conn, root: Path) -> None:
        """The dataflow slice of `rce.cli.cmd_ingest`, reused not
        re-implemented: the git-tracked inventory with the same
        `NotAGitRepositoryError` -> filesystem-walk degradation `cmd_ingest`
        applies (W1), then `ingest_dataflow_repo` over the .py/.R/.Rmd
        lists. Any other `GitIngestError` propagates to `poll_once`'s
        containment, mirroring `cmd_ingest` treating it as fatal for the
        run rather than guessing at an inventory.

        One partial scan (DESIGN.md 9.6): the inventory it read and the
        dataflow extractor, nothing else -- a scan speaks only for the
        extractors it ran and the sources it read."""
        try:
            inventory = git_ingest.list_source_files(root)
        except git_ingest.NotAGitRepositoryError:
            inventory = files_ingest.list_source_files(root)
        with scan_mod.scan(conn, "watcher: dataflow") as sc:
            sc.inventory(inventory)
            counts = dataflow_ingest.ingest_dataflow_repo(
                conn, root, inventory["py"], inventory["r"], inventory["rmd"], scan=sc,
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


def _human_state(conn) -> tuple[object, object]:
    """A cheap fingerprint of the index's human state, to tell whether an
    application changed anything an open page shows."""
    states = {k: (v["outcome"], v["reason"], v["entry_id"]) for k, v in db.judgement_states(conn).items()}
    return db.edge_statuses(conn), states


def _read_records_state(root: Path) -> dict[str, object] | None:
    """The judgment ledger's last trust state as the index stored it, or
    None when the index cannot be opened right now."""
    try:
        db_path = paths.graph_db_path(root)
        if not db_path.exists():
            return None
        conn = db.connect(db_path)
    except Exception:  # noqa: BLE001 -- a status read never fails a write
        return None
    try:
        return db.get_record_status(conn, judgements.RECORD_STATUS_NAME)
    finally:
        conn.close()
