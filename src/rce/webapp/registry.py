"""Machine-managed registry of known RCE projects (task V3 phase 1:
multi-project switching), stored at `~/.rce/projects.json`.

JSON, not TOML, on purpose: unlike `.rce/attempts.toml` -- which a
researcher writes and edits by hand, so it uses the config format with
comments and a copy-pasteable template -- this file is written and
reordered exclusively by RCE itself (`rce serve <path>` registers,
`POST /api/projects/switch` bumps recency). Nothing here is
hand-maintained, so the stdlib `json` round-trip is the right tool and
comment support would buy nothing.

Since V5 (DESIGN.md 9.4) an entry is `{"id", "path", "label"}`, keyed by
the project's id: a moved project updates its entry instead of adding
one, and an entry whose folder is gone -- or whose path now holds another
project -- is reported as such so the app can offer 「选择新位置…」. A
pre-V5 project (no id yet) keeps an id-less entry keyed by path, as every
entry was before; old-format files (no `"id"` key) are read as id-less
entries and written back in the new format. When a project with an id is
registered, an id-less entry at the same path is replaced by it.

Writes are serialized across processes by a `flock` on
`~/.rce/locks/registry.lock` and land through `rce.records.files.
durable_write`, whose temp names are unique per write: two engines
registering at once each keep their entry (9.0 measured 104 of 200
surviving with the old fixed `projects.json.tmp` and no lock).

Contract:

  - `load()` returns the registered projects, most-recently-served first,
    each as `{"id": <project id or None>, "path": <absolute path str>,
    "label": <display name>}`. The label is the directory's basename at
    registration time (and again after a move). A
    missing, unreadable, corrupt, or wrong-shaped registry file degrades
    to `[]` -- the registry is a convenience cache, never something whose
    corruption should take `rce serve` down; individual malformed entries
    are dropped (and logged) rather than poisoning the rest.
  - `register(path)` is idempotent on the resolved path and moves that
    entry to the front (most-recently-served first), preserving an
    existing entry's stored label. Writes are atomic (tmp file +
    `os.replace` in the same directory), so a crash mid-write can never
    leave a half-written `projects.json` for the next `load()` to choke
    on -- it either sees the old file or the new one. Unlike `load()`,
    it does NOT flatten a read failure into `[]`: a registry file that
    exists but cannot be read makes registration a logged no-op, because
    a read-modify-write on top of a misread empty list would replace the
    whole registry with a single entry (see `_read_entries`).
  - `is_initialized(path)` says whether a registry entry is a real,
    initialized RCE project -- delegated to `rce.paths.graph_exists`, the
    same definition of "initialized" `rce.cli`/`rce.webapp.server`'s own
    `_require_db` copies use (since DESIGN.md section 8.10 rule 1 that
    means the external `~/.rce/graphs/<id>/graph.db`, or a legacy
    in-project one not yet migrated). The registry deliberately keeps
    uninitialized entries on `load()` (they are facts about what was
    registered, and the project may simply live on an unmounted disk);
    it is the *consumers* -- `POST /api/projects/switch` refusing to
    switch, the web UI greying the option out -- that gate on this check.
  - `is_available(path)` is the weaker, blunter question section 8.10
    rule 3 asks: is the directory still there at all? A project the user
    deleted or moved is not merely uninitialized -- it is gone, and the
    switcher says so (「目录已不存在」) and offers to remove the entry.
  - `remove(path)` drops one entry, matched by STRING EQUALITY against the
    stored `"path"` -- never resolved or normalized, exactly as
    `POST /api/projects/switch` matches, so the two agree on what "this
    entry" means and a client can only ever name an entry it was shown.
    Returns whether anything was removed; the atomicity and
    unreadable-registry rules are `register()`'s, unchanged.

  - `register(path, id, label=...)` and `rename(id_or_path, label)` set a
    display name chosen in the app (DESIGN.md 10.2, 10.4) -- the label
    only, never the folder; `clean_label` says what a label may be.
    `remove_entry(path)` is `remove` returning the entry it dropped.

Security note (why `load()` membership matters): `rce.webapp.server`'s
`POST /api/projects/switch` accepts a path only if it is string-equal to a
`load()` entry's `"path"`. This file is therefore the allow-list that
keeps a browser page from pointing the server at an arbitrary filesystem
path -- only paths the user has themselves served via the CLI ever appear
here. It lives under `~/.rce/`, outside any project root, so nothing
reachable through the server's own file endpoints can read or write it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Iterator

from rce import paths
from rce.records import files
from rce.records.lock import PROJECT_ID_RE

logger = logging.getLogger(__name__)

# Kept as module attributes for the callers that quote them in their own
# error messages (rce.cli's empty-registry hint), but the definitions now
# live in rce.paths -- one module owns where RCE's state is (DESIGN.md
# section 8.10 rule 1), and this one no longer computes any of it itself.
RCE_DIRNAME = paths.RCE_DIRNAME
DB_FILENAME = paths.DB_FILENAME

REGISTRY_FILENAME = "projects.json"


def registry_path() -> Path:
    """`~/.rce/projects.json` (or under `$RCE_HOME`) -- resolved per call
    through `rce.paths.rce_home()`, not at import time, so a test pointing
    `RCE_HOME` at a throwaway directory never touches the user's real one."""
    return paths.rce_home() / REGISTRY_FILENAME


def is_initialized(path: Path) -> bool:
    """Whether `path` is an initialized RCE project -- `rce.paths.graph_exists`,
    the same definition every `_require_db` copy in this codebase uses."""
    return paths.graph_exists(path)


def is_available(path: Path) -> bool:
    """Whether the registered directory is still there at all (DESIGN.md
    section 8.10 rule 3). Distinct from `is_initialized` on purpose: a
    project on an unmounted disk or one the user deleted both fail that
    check, but only the second is a dead entry the switcher should offer
    to remove -- and only this one can answer "is it dead or just
    asleep?" the way a human would."""
    return Path(path).is_dir()


def _valid_entry(entry: object) -> bool:
    if not (
        isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and isinstance(entry.get("label"), str)
        and bool(entry["path"])
    ):
        return False
    project_id = entry.get("id")
    return project_id is None or (isinstance(project_id, str) and bool(PROJECT_ID_RE.match(project_id)))


_LOCK_FILENAME = "registry.lock"
_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Exclusive across threads and processes for one read-modify-write of
    the registry. The lock file sits under `rce_home()/locks/`, beside the
    project locks (DESIGN.md 9.7), never in a project."""
    with _THREAD_LOCK:
        import fcntl  # noqa: PLC0415 -- POSIX-only, like every RCE lock

        directory = paths.rce_home() / "locks"
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory / _LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the flock


def _read_entries() -> list[dict[str, str]]:
    """The registry's entries, or `[]` for a *missing* file (the ordinary
    first-run state). A file that exists but cannot be read raises the
    OSError instead: "no registry yet" and "registry temporarily
    unreadable" must stay distinguishable, because `register()`'s
    read-modify-write on top of a misread `[]` would atomically replace
    the whole registry with a single entry -- silently discarding every
    other registered project over a chmod slip or a sync tool's lock
    (adversarial-review finding). `load()` below is the one place that
    flattens the distinction, for read-only consumers."""
    path = registry_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("%s is not valid JSON (%s) -- treating the registry as empty", path, exc)
        return []
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        logger.warning("%s has an unexpected shape -- treating the registry as empty", path)
        return []
    entries: list[dict[str, str]] = []
    for entry in projects:
        if not _valid_entry(entry):
            logger.warning("%s: dropping malformed registry entry %r", path, entry)
            continue
        entries.append({"id": entry.get("id"), "path": entry["path"], "label": entry["label"]})
    return entries


def load() -> list[dict[str, str]]:
    """Registered projects, most-recently-served first. Degrades to `[]` on
    a missing/unreadable/corrupt/wrong-shaped file (an unreadable one is
    logged -- a missing one is just first-run), and drops (with a log
    line, never silently) any individual entry that isn't `{"path": str,
    "label": str}` -- a half-broken registry keeps its good entries rather
    than crashing `rce serve` or the `/api/projects` endpoint. Writers
    must NOT build on this flattened view: `register()` reads through
    `_read_entries()` so a transient read failure refuses the rewrite
    instead of clobbering the file with a one-entry registry."""
    try:
        return _read_entries()
    except OSError as exc:
        logger.warning(
            "%s exists but cannot be read (%s) -- treating the registry as empty "
            "for this read", registry_path(), exc,
        )
        return []


def _write_atomic(entries: list[dict[str, str | None]]) -> None:
    """Through `rce.records.files.durable_write` (unique temp name, fsync,
    `os.replace`): a reader only ever sees the old complete file or the
    new complete file. Call inside `_locked()`."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)  # rce_home(), never a project
    data = json.dumps({"projects": entries}, ensure_ascii=False, indent=2) + "\n"
    files.durable_write(path, data.encode("utf-8"))


def _update(change) -> bool:
    """Run `change(entries) -> bool` on the current entries under the lock
    and write them back if it returned True. A registry that exists but
    cannot be read is never rewritten (see `_read_entries`)."""
    with _locked():
        try:
            entries = _read_entries()
        except OSError as exc:
            logger.warning(
                "%s exists but cannot be read (%s) -- NOT rewriting it, since writing on top "
                "of a misread would discard every other registered project",
                registry_path(), exc,
            )
            return False
        if not change(entries):
            return False
        _write_atomic(entries)
        return True


def register(path: Path, project_id: str | None = None, *, label: str | None = None) -> None:
    """Record `path` (resolved to absolute) as the most recently served
    project. Keyed by `project_id` when the project has one (an entry with
    that id is moved to the front and follows the folder: a new path gets
    the new basename as its label unless the label was chosen
    (`_label_after_move`); an id-less entry at the same path is
    replaced), else by path as before V5. Idempotent.

    A registry file that exists but cannot be read makes this a logged
    no-op rather than a rewrite: the entries that may still be in that
    file outrank recording this one serve. Corrupt JSON is different --
    genuinely unrecoverable content -- and still gets rebuilt cleanly."""
    resolved = str(Path(path).resolve())

    def change(entries: list[dict]) -> bool:
        if project_id is not None:
            existing = next((e for e in entries if e.get("id") == project_id), None)
            stale = [e for e in entries if e.get("id") is None and e["path"] == resolved]
        else:
            existing = next((e for e in entries if e.get("id") is None and e["path"] == resolved), None)
            stale = []
        for entry in stale + ([existing] if existing is not None else []):
            entries.remove(entry)
        if existing is not None and existing["path"] == resolved:
            entry = existing
        elif existing is not None:
            entry = {"id": project_id, "path": resolved, "label": _label_after_move(existing, resolved)}
        else:
            entry = {"id": project_id, "path": resolved, "label": Path(resolved).name}
        if label is not None:
            entry["label"] = label
        entries.insert(0, entry)
        return True

    _update(change)


LABEL_MAX_LENGTH = 100


class LabelError(ValueError):
    """A display name that cannot be stored. `code` names why (`empty`,
    `multiline`, `too_long`); `message_zh` is the app's sentence."""

    _MESSAGES = {
        "empty": "显示名称不能为空",
        "multiline": "显示名称只能有一行",
        "too_long": f"显示名称不能超过 {LABEL_MAX_LENGTH} 个字符",
    }

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message_zh = self._MESSAGES[code]


def clean_label(label: object) -> str:
    """A display name as stored (DESIGN.md 10.4): a string, trimmed, one
    line (no line break or other control character), not empty, at most
    `LABEL_MAX_LENGTH` characters. Refused, never silently cut."""
    if not isinstance(label, str):
        raise LabelError("empty", "the label must be a string")
    cleaned = label.strip()
    if not cleaned:
        raise LabelError("empty", "the label is empty")
    if any(ord(ch) < 32 or ch in "\x7f\u2028\u2029\x85" for ch in cleaned):
        raise LabelError("multiline", "the label must be a single line without control characters")
    if len(cleaned) > LABEL_MAX_LENGTH:
        raise LabelError("too_long", f"the label is longer than {LABEL_MAX_LENGTH} characters")
    return cleaned


def rename(id_or_path: str, label: str) -> dict[str, str | None] | None:
    """「重命名显示名称…」 (DESIGN.md 10.4): change one entry's label -- never
    its folder, its path or its recency. The entry is named by its project
    id, or (a pre-V5 entry, which has none) by its stored path, matched by
    string equality exactly as `remove` matches. Returns the renamed entry,
    or None when nothing matched (or the registry could not be read).
    `label` is cleaned first (`clean_label`, which raises `LabelError`)."""
    cleaned = clean_label(label)
    by_id = bool(PROJECT_ID_RE.match(id_or_path))
    found: list[dict] = []

    def change(entries: list[dict]) -> bool:
        for entry in entries:
            if (entry.get("id") == id_or_path) if by_id else (entry["path"] == id_or_path):
                entry["label"] = cleaned
                found.append(dict(entry))
                return True
        return False

    _update(change)
    return found[0] if found else None


def relocate(project_id: str, path: Path) -> bool:
    """A moved project (9.4, adoption): its entry's path and (unless one was
    chosen, `_label_after_move`) label follow the folder, in place -- no recency bump, since adoption happens on any
    entry point, not only on a serve. Returns whether an entry changed.
    An id-less entry at the new path is replaced by it."""
    resolved = str(Path(path).resolve())

    def change(entries: list[dict]) -> bool:
        existing = next((e for e in entries if e.get("id") == project_id), None)
        if existing is None or existing["path"] == resolved:
            return False
        stale = [e for e in entries if e.get("id") is None and e["path"] == resolved]
        for entry in stale:
            entries.remove(entry)
        existing["label"] = _label_after_move(existing, resolved)
        existing["path"] = resolved
        return True

    return _update(change)


def _label_after_move(entry: dict, new_path: str) -> str:
    """The label of an entry whose folder moved to `new_path`: a name the
    researcher chose (「重命名显示名称…」 or the add dialog, 10.4 -- the
    label only, never undone by the folder moving) stays; a label that was
    only the old folder's name follows the folder (9.4)."""
    label = entry.get("label")
    if not label or label == Path(entry["path"]).name:
        return Path(new_path).name
    return label


def find(project_id: str) -> dict[str, str | None] | None:
    """The entry for `project_id`, or None."""
    return next((e for e in load() if e.get("id") == project_id), None)


def remove(path: str | Path) -> bool:
    """`remove_entry`, as whether anything matched."""
    return remove_entry(path) is not None


def remove_entry(path: str | Path) -> dict[str, str | None] | None:
    """Drop `path` from the registry; return the entry removed, or None.

    Matched by STRING EQUALITY against the stored `"path"` value -- never
    resolved, joined, or normalized -- the same rule
    `rce.webapp.server.switch_project_payload` applies, and for the same
    reason: the caller may be a browser page, so the only paths it can
    ever name are ones the registry already told it about. A dead entry's
    directory is gone anyway, which is precisely when resolution is least
    trustworthy.

    Removing an entry removes a *bookmark*: nothing on disk is touched,
    no graph is deleted, and re-serving the path registers it again. Like
    `register()`, a registry file that exists but cannot be read makes
    this a logged no-op rather than a rewrite -- the entries still in that
    file outrank this one removal."""
    requested = str(path)
    removed: list[dict] = []

    def change(entries: list[dict]) -> bool:
        kept = [entry for entry in entries if entry["path"] != requested]
        if len(kept) == len(entries):
            return False
        removed.extend(dict(e) for e in entries if e["path"] == requested)
        entries[:] = kept
        return True

    _update(change)
    return removed[0] if removed else None
