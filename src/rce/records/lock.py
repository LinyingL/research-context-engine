"""One writer at a time (DESIGN.md section 9.7): the cross-process project
lock every write to a record file, and every scan that writes the index,
takes first.

Why a new lock, when the codebase already has several
-----------------------------------------------------

Every lock before V5 was in-process: a `threading.Lock` in the watcher, in
the map editor, in the canvas writer. Two processes -- the app's engine
and a terminal `rce confirm`, or two engines on one project -- each held
their own and wrote the same file anyway; 600 concurrent position writes
left 201 survivors and 279 exceptions (9.0). The record files of V5 are
the one thing RCE may not lose, so their writers take turns across
processes.

`fcntl.flock` on `~/.rce/locks/<key>.lock`:

- **under `rce_home()`, never in the project.** The project may sit in a
  cloud-synced folder; a lock file there could be evicted, synced to the
  other Mac (where it means nothing), or materialized on `open()` -- the
  blocking 8.10 forbids. `~/.rce` is local and never evicted.
  `project_lock` refuses outright if `RCE_HOME` resolves inside the
  project it is locking.
- **one file for every spelling of the folder.** The key is the project id
  from `.rce/project.toml`; before a project has one, the canonical-path
  hash `rce.paths.project_graph_id` computes (letter case, Unicode
  normalization and symlinks folded by the filesystem itself). The two
  key shapes cannot collide: an id is `p-` + 32 hex, a path key is
  `path-` + 16 hex.
- **released by the kernel when the process dies**, so a crashed writer
  never wedges the next one; nothing is ever "stale".

Re-entrant within one thread, exclusive across threads. `flock` belongs
to an open file description, so a second `open()` + `flock` from the same
process would block on itself; and a thread of the engine must not slip
in while another thread of the same engine holds the project. So each key
has one in-process `threading.RLock` and one descriptor: the first
acquisition in this process (depth 0 -> 1) opens and flocks, nested
acquisitions by the same thread only count, and the last release unlocks
and closes. Other threads wait on the RLock before ever reaching `flock`.

A lock that cannot be taken is an error, never a reason to proceed
unlocked (the pre-V5 `_migration_lock` silently yields without `fcntl`;
this one raises `ProjectLockError`): the whole point of the record is
that it is not written by two hands at once.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from rce import paths

LOCKS_DIRNAME = "locks"
LOCK_SUFFIX = ".lock"

# The two key shapes. A project id is minted by `rce.records.identity`
# ("p-" + 32 lowercase hex); validated here too because the key becomes a
# file name, and an id read from a file the researcher (or a copied
# folder) supplied must never be able to name a path outside `locks/`.
PROJECT_ID_RE = re.compile(r"^p-[0-9a-f]{32}$")
PATH_KEY_PREFIX = "path-"

_POLL_INTERVAL_S = 0.02


class ProjectLockError(Exception):
    """The lock could not be taken (cannot create `~/.rce/locks`, `flock`
    itself failed, the platform has no `flock`, the lock would sit inside
    the project). Writers must treat this as "write nothing"."""


class ProjectLockTimeout(ProjectLockError):
    """Another process (or thread) held the lock for longer than the
    caller was willing to wait. Nothing was written."""


def lock_key(project_root: str | Path, project_id: str | None = None) -> str:
    """The lock key: the project id when the project has one, else
    `path-<canonical-path hash>`. A malformed id is refused rather than
    hashed or sanitized -- a project whose identity file says something
    that is not an id has an identity problem the caller must surface."""
    if project_id is not None:
        if not isinstance(project_id, str) or not PROJECT_ID_RE.match(project_id):
            raise ProjectLockError(f"not a project id: {project_id!r}")
        return project_id
    return PATH_KEY_PREFIX + paths.project_graph_id(project_root)


def lock_path(key: str) -> Path:
    """`<rce_home>/locks/<key>.lock`. `key` must be a value `lock_key`
    returns."""
    if not (PROJECT_ID_RE.match(key) or re.match(r"^path-[0-9a-f]{16}$", key)):
        raise ProjectLockError(f"not a lock key: {key!r}")
    return paths.rce_home() / LOCKS_DIRNAME / f"{key}{LOCK_SUFFIX}"


@dataclass
class _KeyState:
    rlock: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0
    fd: int | None = None
    owner: int | None = None  # threading.get_ident() of the holder


_STATES: dict[str, _KeyState] = {}
_STATES_GUARD = threading.Lock()


def _state_for(path: Path) -> _KeyState:
    with _STATES_GUARD:
        state = _STATES.get(str(path))
        if state is None:
            state = _KeyState()
            _STATES[str(path)] = state
        return state


@dataclass(frozen=True)
class HeldLock:
    """What `project_lock` yields: proof, checkable by a writer, that the
    calling thread holds the lock for `key`. `rce.records.ledger.append`
    takes one and calls `require_held()` so "under the caller's lock" is
    enforced, not merely documented."""

    key: str
    path: Path

    def is_held(self) -> bool:
        state = _state_for(self.path)
        return state.depth > 0 and state.owner == threading.get_ident()

    def require_held(self) -> None:
        if not self.is_held():
            raise ProjectLockError(
                f"the project lock {self.key} is not held by this thread; "
                f"record writes must happen inside `project_lock`"
            )


def _refuse_lock_inside_project(lock_file: Path, project_root: str | Path | None) -> None:
    if project_root is None:
        return
    try:
        root = Path(project_root).resolve()
        lock_file.parent.resolve().relative_to(root)
    except ValueError:
        return  # not inside: the normal case
    except OSError as exc:
        raise ProjectLockError(f"cannot check where the project lock lives ({exc})") from exc
    raise ProjectLockError(
        f"the lock directory {lock_file.parent} is inside the project {project_root}; "
        f"RCE_HOME must not point into a project folder"
    )


def _flock_acquire(fd: int, deadline: float | None) -> None:
    import fcntl  # noqa: PLC0415 -- POSIX-only, checked by the caller

    if deadline is None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise ProjectLockError(f"flock failed ({exc})") from exc
        return
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ProjectLockTimeout("the project is locked by another writer") from None
            time.sleep(_POLL_INTERVAL_S)
        except OSError as exc:
            raise ProjectLockError(f"flock failed ({exc})") from exc


@contextlib.contextmanager
def project_lock(
    project_root: str | Path,
    project_id: str | None = None,
    *,
    timeout: float | None = None,
) -> Iterator[HeldLock]:
    """Hold the cross-process lock for this project for the duration of the
    `with` block. Blocks (or, with `timeout` seconds, raises
    `ProjectLockTimeout`). Re-entrant for the calling thread.

    Key transition: a project with no id is locked by path. Whoever creates
    `.rce/project.toml` does so under the path lock and, from then on, the
    id lock is the one that counts; a caller that needs both takes the path
    lock first and the id lock inside it, never the reverse, so two writers
    can never deadlock on the pair."""
    key = lock_key(project_root, project_id)
    lock_file = lock_path(key)
    _refuse_lock_inside_project(lock_file, project_root)
    try:
        import fcntl  # noqa: F401,PLC0415 -- availability check only
    except ImportError as exc:  # pragma: no cover -- not a platform RCE ships on
        raise ProjectLockError("this platform has no flock; refusing to write unlocked") from exc

    deadline = None if timeout is None else time.monotonic() + timeout
    state = _state_for(lock_file)
    if deadline is None:
        state.rlock.acquire()
    elif not state.rlock.acquire(timeout=max(0.0, timeout)):
        raise ProjectLockTimeout("the project is locked by another thread of this process")
    try:
        if state.depth == 0:
            try:
                lock_file.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
            except OSError as exc:
                raise ProjectLockError(f"cannot open the lock file {lock_file} ({exc})") from exc
            try:
                _flock_acquire(fd, deadline)
            except BaseException:
                os.close(fd)
                raise
            state.fd = fd
            state.owner = threading.get_ident()
        state.depth += 1
        try:
            yield HeldLock(key=key, path=lock_file)
        finally:
            state.depth -= 1
            if state.depth == 0:
                fd, state.fd, state.owner = state.fd, None, None
                if fd is not None:
                    try:
                        import fcntl  # noqa: PLC0415

                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
    finally:
        state.rlock.release()
