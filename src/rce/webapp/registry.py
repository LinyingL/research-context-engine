"""Machine-managed registry of known RCE projects (task V3 phase 1:
multi-project switching), stored at `~/.rce/projects.json`.

JSON, not TOML, on purpose: unlike `.rce/attempts.toml` -- which a
researcher writes and edits by hand, so it uses the config format with
comments and a copy-pasteable template -- this file is written and
reordered exclusively by RCE itself (`rce serve <path>` registers,
`POST /api/projects/switch` bumps recency). Nothing here is
hand-maintained, so the stdlib `json` round-trip is the right tool and
comment support would buy nothing.

Contract:

  - `load()` returns the registered projects, most-recently-served first,
    each as `{"path": <absolute path str>, "label": <display name>}`. The
    label defaults to the directory's basename at registration time. A
    missing, corrupt, or wrong-shaped registry file degrades to `[]` --
    the registry is a convenience cache, never something whose corruption
    should take `rce serve` down; individual malformed entries are dropped
    (and logged) rather than poisoning the rest.
  - `register(path)` is idempotent on the resolved path and moves that
    entry to the front (most-recently-served first), preserving an
    existing entry's stored label. Writes are atomic (tmp file +
    `os.replace` in the same directory), so a crash mid-write can never
    leave a half-written `projects.json` for the next `load()` to choke
    on -- it either sees the old file or the new one.
  - `is_initialized(path)` says whether a registry entry is a real,
    initialized RCE project (`.rce/graph.db` exists) -- the same
    definition of "initialized" `rce.cli`/`rce.webapp.server`'s own
    `_require_db` copies use. The registry deliberately keeps
    uninitialized entries on `load()` (they are facts about what was
    registered, and the project may simply live on an unmounted disk);
    it is the *consumers* -- `POST /api/projects/switch` refusing to
    switch, the web UI greying the option out -- that gate on this check.

Security note (why `load()` membership matters): `rce.webapp.server`'s
`POST /api/projects/switch` accepts a path only if it is string-equal to a
`load()` entry's `"path"`. This file is therefore the allow-list that
keeps a browser page from pointing the server at an arbitrary filesystem
path -- only paths the user has themselves served via the CLI ever appear
here. It lives under `~/.rce/`, outside any project root, so nothing
reachable through the server's own file endpoints can read or write it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Same constants as rce.cli / rce.webapp.server -- each subsystem owns its
# copy (existing convention in this codebase); importing server here would
# invert the dependency direction (server imports this module).
RCE_DIRNAME = ".rce"
DB_FILENAME = "graph.db"

REGISTRY_FILENAME = "projects.json"


def registry_path() -> Path:
    """`~/.rce/projects.json` -- resolved per call, not at import time, so a
    test monkeypatching `HOME` (which `Path.home()` honors on POSIX) gets a
    throwaway registry without touching the user's real one."""
    return Path.home() / RCE_DIRNAME / REGISTRY_FILENAME


def is_initialized(path: Path) -> bool:
    """Whether `path` is an initialized RCE project: `.rce/graph.db` exists
    -- the same definition every `_require_db` copy in this codebase uses."""
    return (Path(path) / RCE_DIRNAME / DB_FILENAME).exists()


def _valid_entry(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and isinstance(entry.get("label"), str)
        and bool(entry["path"])
    )


def load() -> list[dict[str, str]]:
    """Registered projects, most-recently-served first. Degrades to `[]` on
    a missing/corrupt/wrong-shaped file, and drops (with a log line, never
    silently) any individual entry that isn't `{"path": str, "label": str}`
    -- a half-broken registry keeps its good entries rather than crashing
    `rce serve` or the `/api/projects` endpoint."""
    path = registry_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []  # missing file is the ordinary first-run state, not an error
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
        entries.append({"path": entry["path"], "label": entry["label"]})
    return entries


def _write_atomic(entries: list[dict[str, str]]) -> None:
    """tmp file + `os.replace` in the same directory: a reader (another
    `rce serve` process, a concurrent request thread) only ever sees the
    old complete file or the new complete file, never a partial write."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"projects": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def register(path: Path) -> None:
    """Record `path` (resolved to absolute) as the most recently served
    project. Idempotent: an already-registered path is moved to the front,
    keeping its stored label; a new one is inserted at the front with the
    directory basename as its default label."""
    resolved = str(Path(path).resolve())
    entries = load()
    existing = next((e for e in entries if e["path"] == resolved), None)
    if existing is not None:
        entries.remove(existing)
        entries.insert(0, existing)
    else:
        entries.insert(0, {"path": resolved, "label": Path(resolved).name})
    _write_atomic(entries)
