"""Local read-only web view over the graph (task V1, DESIGN.md section 7,
"Later"). stdlib `http.server` only -- zero third-party dependency, same
constraint as every other subcommand (pyproject.toml `dependencies = []`).

Bound to `127.0.0.1` only, hardcoded in `build_server` -- this is never a
multi-user or network-facing service, and nothing in this module accepts a
`--host` argument to change that.

Every endpoint below is a read over the graph (`rce.db`/`rce.lineage`) or the
project filesystem, with one deliberate exception: `POST /api/open`, which
shells out to macOS's `open` to reveal a path in Finder or open it with its
default application -- never to execute or modify project content, and
always with the path list-argument form (`subprocess.run([...])`, never
`shell=True`) so there is no shell-injection surface regardless of what the
path string contains.

Endpoints (all GET unless noted):

    GET  /api/summary   -- node/edge counts by type, project root, pending
                            confirmation queue size, plus an echo of the
                            attempts config's columns/file (or null when no
                            usable `.rce/attempts.toml` exists) so the app
                            can build the edit form from the project's OWN
                            column names, never hardcoded ones (see
                            `summary_payload`).
    GET  /api/attempts  -- attempt nodes (human_fields verdict/result plus
                            every attrs field); `{"attempts": [], "hint":
                            ...}` when the graph has none (see
                            `attempts_payload`).
    GET  /api/tree      -- the decision-tree view, this app's own reason to
                            exist: attempts (natural "#" order, a "14a"/"14b"
                            split nested under "14" when it exists, else
                            siblings) -> each attempt's `attrs.step_files`
                            scripts -> each script's `reads`/`writes` data
                            files, tagged `has_generator`/`orphan_input`.
                            Derived entirely from existing graph edges --
                            zero inference (see `tree_payload`).
    GET  /api/lineage   -- `rce.lineage.build_lineage_report`'s four blocks,
                            unchanged (see `lineage_payload`) -- this endpoint
                            does not re-implement that report.
    GET  /api/file      -- one project text file's content, UTF-8, capped at
                            `_FILE_SIZE_LIMIT` bytes (truncation flagged, never
                            silent); binary content is refused, not garbled
                            (see `file_payload`).
    POST /api/open      -- body `{"path": REL, "reveal": bool}`: reveal a
                            project path in Finder (`open -R`) or open it with
                            its default application (`open`). macOS only --
                            every other platform gets 501 with an explanation,
                            never a confusing subprocess failure (see
                            `open_payload`/`_is_macos`).
    GET  /api/projects  -- the machine-managed project registry
                            (`rce.webapp.registry`, `~/.rce/projects.json`)
                            plus which project this server is currently
                            serving: `{"projects": [{path, label,
                            initialized}], "current": path}` (see
                            `projects_payload`).
    POST /api/projects/switch -- body `{"path"}`: repoint this running
                            server at another *registered, initialized*
                            project and return the new current. The path
                            must be string-equal to a registry entry --
                            see "Switch-target defense" below (see
                            `switch_project_payload`).
    POST /api/attempts/preview -- body `{"op": "append"|"update", "number",
                            "fields"}`: a pure dry run of an attempt-row
                            edit against the researcher's own map file --
                            unified diff plus old/new row, NOTHING written
                            (see `attempts_preview_payload` and
                            `rce.webapp.mapedit`).
    POST /api/attempts/write -- same body: actually performs the edit --
                            backup, atomic write into the source Markdown,
                            re-ingest under the watcher's own ingest lock,
                            generation bump -- and returns `{ok, backup,
                            generation, ingest_error}`. See "Write-path
                            defense" below (see `attempts_write_payload`).
    GET  /api/generation -- the auto-refresh watcher's status
                            (`rce.webapp.watcher`, task V3 phase 2):
                            `{"generation": int, "refreshing": bool,
                            "last_error": str|null}`. The frontend polls
                            this and re-fetches its views whenever the
                            generation moved -- see "Auto-refresh" below.
    GET  /             -- the single-page app (task V2), served verbatim from
                            `src/rce/webapp/app.html`: inline CSS/JS, zero
                            external resources, zero build step -- it reads
                            this same JSON API entirely client-side (see that
                            file's own top comment for the two-view contract).

Path-traversal defense (`/api/file` and `/api/open` alike, both required by
task V1): `_resolve_within_root` resolves the requested path -- symlinks
included -- and rejects it unless the *resolved* path is still under the
project root. This is what actually stops `../../etc/passwd`, an absolute
path, and a symlink planted inside the project that points outside it: all
three end up outside `root` after `Path.resolve()`, so the same one check
catches every case rather than pattern-matching on `..` textually (which a
symlink would trivially evade).

Cross-origin defense (every endpoint, `RceRequestHandler._check_local_origin`,
security-review fix): a page open in the user's browser on any *other*
origin can still make this loopback server do something just by having the
browser send it a request -- a "simple" cross-origin POST (e.g.
`Content-Type: text/plain`) needs no CORS preflight at all, and even a plain
cross-origin GET always reaches the server; the browser only blocks the
*page's own script* from reading a cross-origin GET's response body (no
`Access-Control-Allow-Origin` header is ever sent here), which protects
nothing against `POST /api/open`'s side effect of shelling out to `open`.
DNS rebinding defeats even that read-block: a domain the attacker controls
can resolve to something else while the browser loads the page, then to
`127.0.0.1` on a later request, and the browser's same-origin check compares
against the *hostname it believes it dialed*, never the IP it actually
reached. Both `do_GET` and `do_POST` call `_check_local_origin` before doing
anything else: it rejects unless `Host` equals `127.0.0.1:<the port this
process actually bound>` (a rebound or attacker-controlled hostname never
produces that exact Host value, no matter what it resolves to) and, only
when a browser actually sends one, `Origin` equals that same
`http://127.0.0.1:<port>` -- missing entirely is accepted, since a
non-browser CLI caller (`curl`, this module's own test suite) never sends
one.

Switch-target defense (`POST /api/projects/switch`, task V3 phase 1): this
is the one endpoint that changes what the whole server serves, so its input
is never treated as a filesystem path at all. The requested string must be
*string-equal* to a `rce.webapp.registry.load()` entry's `"path"` -- no
resolution, no normalization, no prefix logic is ever applied to the
client-supplied value -- and that entry must be an initialized project
(`.rce/graph.db` exists). The registry lives at `~/.rce/projects.json`,
outside every project root, and only `rce serve <path>` on this user's own
command line (plus a successful switch's recency bump) ever writes it; so
even a request that somehow got past `_check_local_origin` could only ever
choose among projects the user has already deliberately served, never point
the server at `/etc` or another arbitrary directory. `_check_local_origin`
still runs first, exactly as for every other endpoint -- this validation is
depth behind that check, not a replacement for it.

Write-path defense (`POST /api/attempts/preview`/`.../write`, task V3
phase 3): these are the endpoints that can change the researcher's own map
file, so the stakes are the drive-by page again -- `_check_local_origin`
runs first, exactly as for every other endpoint, and is what stands between
a hostile page's no-preflight cross-origin POST and a write into the user's
research log. Behind that check, depth: the file written is never named by
the request at all -- it is always the one `.rce/attempts.toml` configures
(`rce.webapp.mapedit` loads it server-side), so no request body can steer
the write to another path; the edit itself is validated against the file's
current content (duplicate/unknown row numbers, newlines, undecodable
content all refuse cleanly); the original is backed up to `.rce/backups/`
before every write and the write is atomic; and the post-write re-ingest
runs under the watcher's own ingest lock so a UI write and a watcher poll
never ingest concurrently (`ProjectWatcher.ingest_lock`,
`record_external_change`). This is the one place the app writes project
content, and it writes only what DESIGN.md declares the single source of
truth -- the map file -- letting re-ingest mirror it into the graph, never
the graph directly ("resync from source", DESIGN.md section 4).

The current project root itself is mutable state on `RceHTTPServer`, read
and written only through accessors holding a `threading.Lock`
(`get_project_root`/`set_project_root`) -- `ThreadingHTTPServer` handles
each request on its own thread, so a switch must never interleave with
another handler reading the root mid-request. Each handler reads the root
once per request (via `_project_root()`) and works with that snapshot; a
switch landing mid-request affects the *next* request, never tears this one.

Auto-refresh (task V3 phase 2): every `RceHTTPServer` owns one
`rce.webapp.watcher.ProjectWatcher` -- a daemon polling thread that stats
the current project's attempts config/source file/steps_dir every ~2s,
re-runs the relevant ingest in-process on a change, and bumps the
generation counter `GET /api/generation` reports (see that module's own
docstring for the watch-set bounds and failure containment). The thread is
started by `serve()` -- never by `build_server`, so tests that only need
routing get no background polling -- and stopped by `server_close`. A
successful `POST /api/projects/switch` calls `watcher.retarget()` so
polling follows the new root and the frontend's next generation poll
triggers a re-fetch. The watcher endpoint is read-only status; it goes
through `_check_local_origin` exactly like every other endpoint.

Every handler-facing failure is one of the small `ApiError` subclasses below,
each carrying its own HTTP status; `RceRequestHandler` catches `ApiError`
once per request and renders `{"error": str(exc)}` at that status, mirroring
`rce.cli`'s single `CliError` catch in `main()`.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Callable

from rce import db, lineage
from rce.ingest import attempts as attempts_ingest
from rce.webapp import mapedit
from rce.webapp import registry as project_registry
from rce.webapp import watcher as project_watcher

logger = logging.getLogger(__name__)

RCE_DIRNAME = ".rce"
DB_FILENAME = "graph.db"

_FILE_SIZE_LIMIT = 200 * 1024  # 200KB (task V1 spec)


# -- Errors -------------------------------------------------------------------


class ApiError(Exception):
    """Base for every error an endpoint can raise; `status` is the HTTP code
    `RceRequestHandler` sends back alongside `{"error": str(self)}`."""

    status = 400


class NotFoundError(ApiError):
    status = 404


class MissingParamError(ApiError):
    status = 400


class PathTraversalError(ApiError):
    status = 403


class ForbiddenOriginError(ApiError):
    status = 403


class NotAFileError(ApiError):
    status = 400


class BinaryFileError(ApiError):
    status = 415


class UnsupportedPlatformError(ApiError):
    status = 501


class ProjectNotInitializedError(ApiError):
    status = 400


class AttemptEditError(ApiError):
    """`POST /api/attempts/preview`/`.../write` asked for an edit the map
    file's current state refuses (duplicate/unknown number, invalid field
    content, unusable config/table) -- 400: the request, not the server,
    is what cannot be satisfied. Wraps `rce.webapp.mapedit.MapEditError`
    and `rce.ingest.attempts.AttemptsConfigError` with their own messages
    intact, since those already say precisely what was wrong."""

    status = 400


class UnknownProjectError(ApiError):
    """`POST /api/projects/switch` asked for a path that is not a registry
    member -- 403, same as the traversal/origin rejections, because whatever
    sent it is trying to steer the server somewhere the user never
    registered (see module docstring's "Switch-target defense")."""

    status = 403


def _require_db(project_root: Path) -> Path:
    """Same message shape as `rce.cli`/`rce.mcp_server`'s own `_require_db`
    -- each subsystem owns its copy (existing convention in this codebase),
    since each raises its own module's error type."""
    path = project_root / RCE_DIRNAME / DB_FILENAME
    if not path.exists():
        raise ProjectNotInitializedError(
            f"no RCE project at {project_root} (missing {RCE_DIRNAME}/{DB_FILENAME}); "
            f"run 'rce init {project_root}' first"
        )
    return path


# -- Path safety (shared by /api/file and /api/open) -------------------------


def _resolve_within_root(project_root: Path, rel_path: str) -> Path:
    """Resolve `rel_path` against `project_root` and reject it unless the
    *resolved* path (symlinks followed, `..` collapsed) is still under the
    resolved root -- see module docstring's "Path-traversal defense".

    Deliberately does not special-case an absolute `rel_path` or a literal
    `..` segment before resolving: `Path.resolve()` normalizes both the same
    way (an absolute `rel_path` simply replaces the join outright -- documented
    `pathlib` behaviour -- and a `..` walks up), so the one `relative_to`
    check below catches every shape of escape identically, including one a
    textual `..`-substring check would miss (a symlink whose target is
    outside the root but whose own written path contains no `..` at all).
    """
    root = project_root.resolve()
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise PathTraversalError(
            f"{rel_path!r} resolves outside the project root -- refusing"
        ) from None
    return candidate


# -- /api/summary -------------------------------------------------------------


def _attempts_config_echo(project_root: Path) -> dict[str, Any] | None:
    """The attempts config's shape, for the frontend's edit form (task V3
    phase 3): the form's fields come from the project's OWN `[columns]`
    header names, never hardcoded ones, so the app reads them here rather
    than inventing any. None when no usable config exists -- the form then
    explains that instead of guessing at columns (DESIGN.md section 0)."""
    try:
        config = attempts_ingest.load_config(project_root)
    except attempts_ingest.AttemptsConfigError:
        return None
    return {
        "file": config.file,
        "heading": config.heading,
        "columns": config.columns,
        "steps_dir": config.steps_dir,
    }


def summary_payload(conn: Connection, project_root: Path) -> dict[str, Any]:
    node_counts = {t: len(db.get_nodes_by_type(conn, t)) for t in sorted(db.NODE_TYPES)}
    edge_counts = {t: 0 for t in sorted(db.EDGE_TYPES)}
    for edge in db.query_edges(conn):
        edge_counts[edge["type"]] += 1
    return {
        "project_root": str(project_root),
        "nodes": node_counts,
        "edges": edge_counts,
        "pending": len(db.pending_edges(conn)),
        "attempts_config": _attempts_config_echo(project_root),
    }


# -- /api/attempts + /api/tree: shared attempt-node helpers -------------------

_NO_ATTEMPTS_HINT = (
    "No attempt nodes in the graph yet. Configure .rce/attempts.toml and run "
    "'rce attempts' to ingest your attempt timeline first."
)

_NUMBER_SPLIT_RE = re.compile(r"^(\d+)(.*)$")


def _split_number(number: str) -> tuple[str, str]:
    """`("14", "a")` for `"14a"`, `("14", "")` for `"14"`; a label with no
    leading digits (should not happen in practice -- same guard
    `rce.ingest.attempts.attempt_sort_key` already applies) is returned as
    `(number, "")`, which always sorts to the top level below since an empty
    suffix never triggers the parent-nesting check."""
    m = _NUMBER_SPLIT_RE.match(number)
    if not m:
        return number, ""
    return m.group(1), m.group(2)


def _sorted_attempt_nodes(conn: Connection) -> list[dict[str, Any]]:
    nodes = db.get_nodes_by_type(conn, "attempt")
    return sorted(
        nodes,
        key=lambda n: (
            n["attrs"].get("source_file", ""),
            attempts_ingest.attempt_sort_key(n["attrs"].get("number", "")),
        ),
    )


def attempts_payload(conn: Connection) -> dict[str, Any]:
    nodes = _sorted_attempt_nodes(conn)
    if not nodes:
        return {"attempts": [], "hint": _NO_ATTEMPTS_HINT}
    attempts = [
        {
            "id": node["id"],
            "attrs": node["attrs"],
            "verdict": node["human_fields"].get("verdict", ""),
            "result": node["human_fields"].get("result", ""),
        }
        for node in nodes
    ]
    return {"attempts": attempts}


# -- /api/tree: the decision-tree view (task V1's core endpoint) -------------


def _occurrences(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Same defensive unwrap `rce.lineage`/`rce.cli` already use for a
    `reads`/`writes` edge's evidence -- see their own copies' docstrings for
    why a bare-dict fallback is kept (pre-T10 legacy row shape)."""
    occurrences = evidence.get("occurrences")
    if not isinstance(occurrences, list):
        occurrences = [evidence]
    return occurrences


def _target_path(conn: Connection, target_id: str) -> str:
    node = db.get_node(conn, target_id)
    if node is not None and node["title"]:
        return node["title"]
    return target_id.partition(":")[2] or target_id


def _lineage_role(conn: Connection, target_id: str) -> str:
    """Whether some script anywhere in the graph writes this target --
    `has_generator` if so, else `orphan_input` (the same "who wrote this
    input" question `rce.lineage`'s own orphan block answers, reused here as
    a per-file tag rather than a separate report block)."""
    return "has_generator" if db.query_edges(conn, dst=target_id, type="writes") else "orphan_input"


def _connected_files(conn: Connection, script_id: str, edge_type: str) -> list[dict[str, Any]]:
    entries = []
    for edge in db.query_edges(conn, src=script_id, type=edge_type):
        missing = any(occ.get("missing") for occ in _occurrences(edge["evidence"]))
        entries.append({
            "path": _target_path(conn, edge["dst"]),
            "role": _lineage_role(conn, edge["dst"]),
            "missing": missing,
        })
    return sorted(entries, key=lambda e: e["path"])


def _script_layer(conn: Connection, script_rel_path: str) -> dict[str, Any]:
    script_id = f"script:{script_rel_path}"
    return {
        "path": script_rel_path,
        "reads": _connected_files(conn, script_id, "reads"),
        "writes": _connected_files(conn, script_id, "writes"),
    }


def _load_steps_dir(project_root: Path) -> str | None:
    """`.rce/attempts.toml`'s `steps_dir`, or None if the config can't be
    loaded at all (e.g. it was removed after attempts were ingested).
    Attempt nodes only ever exist because `rce attempts` ingested them
    through this same config, so this ordinarily succeeds whenever
    `/api/tree` has any attempts to show at all; the fallback degrades to
    "no scripts layer" rather than guessing at a steps_dir prefix (DESIGN.md
    section 0)."""
    try:
        return attempts_ingest.load_config(project_root).steps_dir
    except attempts_ingest.AttemptsConfigError:
        return None


def _attempt_entry(conn: Connection, node: dict[str, Any], steps_dir: str | None) -> dict[str, Any]:
    attrs = node["attrs"]
    scripts: list[dict[str, Any]] = []
    if steps_dir:
        for filename in attrs.get("step_files") or []:
            scripts.append(_script_layer(conn, f"{steps_dir}/{filename}"))
    return {
        "id": node["id"],
        "number": attrs.get("number", ""),
        "date": attrs.get("date", ""),
        "description": attrs.get("description", ""),
        "verdict": node["human_fields"].get("verdict", ""),
        "scripts": scripts,
        "children": [],
    }


def tree_payload(conn: Connection, project_root: Path) -> dict[str, Any]:
    """The decision-tree JSON (task V1's own core endpoint): attempts (layer
    1) -> their step scripts (layer 2) -> each script's reads/writes data
    files (layer 3). Every layer comes from graph nodes/edges already
    written by `rce attempts`/`rce ingest` -- no re-parsing, no inference.

    Layer 1 nesting: a "14a"/"14b" split nests under a "14" node when one
    exists in the *same source file*; otherwise "14a"/"14b" are top-level
    siblings (task V1 spec: "父项不存在则作兄弟"). Scoped per source_file
    (an attempt id's own `attrs.source_file`), not globally by number alone,
    so two different attempt timelines' numbering can never cross-nest into
    each other's tree by coincidence.
    """
    nodes = _sorted_attempt_nodes(conn)
    if not nodes:
        return {"attempts": [], "hint": _NO_ATTEMPTS_HINT}
    steps_dir = _load_steps_dir(project_root)

    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for node in nodes:
        key = (node["attrs"].get("source_file", ""), node["attrs"].get("number", ""))
        entries[key] = _attempt_entry(conn, node, steps_dir)

    top_level: list[dict[str, Any]] = []
    for node in nodes:
        source_file = node["attrs"].get("source_file", "")
        number = node["attrs"].get("number", "")
        leading, suffix = _split_number(number)
        parent_key = (source_file, leading)
        if suffix and leading != number and parent_key in entries:
            entries[parent_key]["children"].append(entries[(source_file, number)])
        else:
            top_level.append(entries[(source_file, number)])
    return {"attempts": top_level}


# -- /api/lineage -------------------------------------------------------------


def lineage_payload(conn: Connection, project_root: Path) -> dict[str, Any]:
    """Exactly `rce.lineage.build_lineage_report`'s own structured result --
    the same function `rce lineage --json` already calls (rce.cli.cmd_lineage),
    reused rather than re-implemented here."""
    return lineage.build_lineage_report(conn, project_root)


# -- /api/file ----------------------------------------------------------------


def file_payload(project_root: Path, rel_path: str) -> dict[str, Any]:
    target = _resolve_within_root(project_root, rel_path)
    if not target.is_file():
        if not target.exists():
            raise NotFoundError(f"no such file: {rel_path}")
        raise NotAFileError(f"not a regular file: {rel_path}")
    raw = target.read_bytes()
    if b"\x00" in raw[:8192]:
        raise BinaryFileError(f"{rel_path} looks like a binary file; refusing to return its content")
    truncated = len(raw) > _FILE_SIZE_LIMIT
    content_bytes = raw[:_FILE_SIZE_LIMIT] if truncated else raw
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        if not truncated:
            raise BinaryFileError(f"{rel_path} is not valid UTF-8 text; refusing to return its content") from None
        # The 200KB cut can land mid multi-byte character; that is a
        # truncation artifact, not evidence the file is binary.
        content = content_bytes.decode("utf-8", errors="ignore")
    return {"path": rel_path, "content": content, "truncated": truncated, "size": len(raw)}


# -- POST /api/open -----------------------------------------------------------


def _is_macos() -> bool:
    return sys.platform == "darwin"


def open_payload(project_root: Path, rel_path: str, reveal: bool) -> dict[str, Any]:
    """Reveal `rel_path` in Finder (`open -R`) or open it with its default
    application (`open`), macOS only. `subprocess.run` is always given a
    plain list of arguments -- never `shell=True` -- so there is no shell
    metacharacter to worry about regardless of what `rel_path` contains; the
    path itself is validated by `_resolve_within_root` before it ever reaches
    `subprocess.run`."""
    if not _is_macos():
        raise UnsupportedPlatformError(
            "'open' is only available on macOS; this server is running on a different platform"
        )
    target = _resolve_within_root(project_root, rel_path)
    if not target.exists():
        raise NotFoundError(f"no such path: {rel_path}")
    args = ["open", "-R", str(target)] if reveal else ["open", str(target)]
    subprocess.run(args, check=False)
    return {"opened": str(target), "reveal": reveal}


# -- /api/projects + POST /api/projects/switch (task V3 phase 1) --------------


def projects_payload(current_root: Path) -> dict[str, Any]:
    """The registry (`rce.webapp.registry.load()`, most-recently-served
    first) with each entry's `initialized` state checked fresh per request
    -- a project can be `rce init`ed, or its disk unmounted, between two
    calls -- plus which project this server is currently serving. The
    current root is reported even when it is not (or no longer) a registry
    member: it is a fact about this server, not about the registry."""
    projects = [
        {
            "path": entry["path"],
            "label": entry["label"],
            "initialized": project_registry.is_initialized(Path(entry["path"])),
        }
        for entry in project_registry.load()
    ]
    return {"projects": projects, "current": str(current_root)}


def switch_project_payload(requested: str) -> tuple[Path, dict[str, Any]]:
    """Validate a switch request and return `(new_root, response_payload)`;
    the caller (the handler) is the one that actually repoints the server,
    via `RceHTTPServer.set_project_root` -- this function owns the
    validation and the registry recency bump, never the server state.

    The requested string is compared *string-equal* against registry
    entries' stored `"path"` values -- it is never resolved, joined, or
    otherwise interpreted as a filesystem path, so there is nothing here
    for a crafted value to traverse or normalize its way past (module
    docstring, "Switch-target defense"). Not a member: 403
    (`UnknownProjectError`). A member that is not an initialized project:
    400 (`ProjectNotInitializedError`) -- registered but unusable, e.g.
    never `rce init`ed or its disk currently absent. Only a valid switch
    bumps the entry to most-recently-served in the registry, so a later
    bare `rce serve` resumes from it (rce.cli.cmd_serve)."""
    entry = next((e for e in project_registry.load() if e["path"] == requested), None)
    if entry is None:
        raise UnknownProjectError(
            f"{requested!r} is not a registered project -- only paths already in the "
            f"registry (~/.rce/{project_registry.REGISTRY_FILENAME}, written by "
            f"'rce serve <path>') can be switched to"
        )
    new_root = Path(entry["path"])
    if not project_registry.is_initialized(new_root):
        raise ProjectNotInitializedError(
            f"registered project {entry['path']!r} is not initialized (missing "
            f"{RCE_DIRNAME}/{DB_FILENAME}); run 'rce init {entry['path']}' first"
        )
    project_registry.register(new_root)  # most-recently-served bump
    return new_root, {"current": entry["path"], "label": entry["label"]}


# -- POST /api/attempts/preview + /api/attempts/write (task V3 phase 3) -------


def _parse_attempt_edit_body(body: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Both attempt-edit endpoints share one body contract -- `{"op":
    "append"|"update", "number": str, "fields": {key: str}}` -- so both go
    through this single shape check (semantic validation -- duplicate
    numbers, newline content, unknown field keys -- belongs to
    `rce.webapp.mapedit`, which owns those rules)."""
    op = body.get("op")
    if op not in ("append", "update"):
        raise MissingParamError("request body 'op' must be \"append\" or \"update\"")
    number = body.get("number")
    if not isinstance(number, str):
        raise MissingParamError("request body must carry a string 'number' key")
    fields = body.get("fields", {})
    if not isinstance(fields, dict):
        raise MissingParamError("request body 'fields' must be a JSON object")
    return op, number, fields


def attempts_preview_payload(project_root: Path, body: dict[str, Any]) -> dict[str, Any]:
    """A pure dry run: `rce.webapp.mapedit.preview_edit`'s
    `{file, diff, old_row, new_row}`, verbatim -- nothing on disk moves."""
    op, number, fields = _parse_attempt_edit_body(body)
    try:
        return mapedit.preview_edit(project_root, op, number, fields)
    except (mapedit.MapEditError, attempts_ingest.AttemptsConfigError) as exc:
        raise AttemptEditError(str(exc)) from exc


def attempts_write_payload(
    project_root: Path, body: dict[str, Any], watcher: project_watcher.ProjectWatcher
) -> dict[str, Any]:
    """The actual write: `rce.webapp.mapedit.apply_edit` under the
    watcher's own ingest lock (so a UI write and a watcher poll never
    ingest concurrently -- module docstring's "Write-path defense"), then
    `record_external_change` re-baselines the watcher and bumps the
    generation, recording (or clearing) the write's own contained
    post-write ingest failure. `ingest_error` is passed through to the
    response so the UI can say "file written and backed up, but the rescan
    failed" rather than hiding a half-landed state behind a bare ok."""
    op, number, fields = _parse_attempt_edit_body(body)
    try:
        result = mapedit.apply_edit(
            project_root, op, number, fields, ingest_lock=watcher.ingest_lock,
        )
    except (mapedit.MapEditError, attempts_ingest.AttemptsConfigError) as exc:
        raise AttemptEditError(str(exc)) from exc
    generation = watcher.record_external_change(result["ingest_error"])
    return {
        "ok": True,
        "file": result["file"],
        "backup": result["backup"],
        "generation": generation,
        "ingest_error": result["ingest_error"],
    }


# -- The single-page app (task V2) -------------------------------------------

_APP_HTML_PATH = Path(__file__).parent / "app.html"


def _app_html() -> str:
    """`src/rce/webapp/app.html` verbatim -- read fresh on every request
    rather than cached in memory, since this is a local single-user tool
    (no request volume to speak of) and a fresh read means a developer
    editing the file sees the change on the next reload with no server
    restart. Packaged as `package-data` (pyproject.toml) so it ships
    alongside `server.py` in an installed wheel, not just this editable
    checkout."""
    return _APP_HTML_PATH.read_text(encoding="utf-8")


# -- HTTP plumbing -------------------------------------------------------------


class RceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type,
        project_root: Path,
        watch_interval: float = project_watcher.DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        # Mutable since task V3 phase 1 (POST /api/projects/switch), and
        # only ever touched through the two accessors below: each request
        # runs on its own thread (ThreadingHTTPServer), so a bare attribute
        # would let a switch interleave with another handler's read. Kept
        # name-mangled + locked rather than public so no future handler can
        # accidentally bypass the lock.
        self.__project_root = project_root
        self.__project_root_lock = threading.Lock()
        # Task V3 phase 2: the auto-refresh watcher. Created here (so
        # /api/generation always has status to report, and a switch always
        # has something to retarget) but its polling thread is only started
        # by serve() -- build_server alone spawns no background work, which
        # keeps every routing-only test thread-free. Reads the root through
        # the same locked accessor handlers use.
        self.watcher = project_watcher.ProjectWatcher(
            self.get_project_root, interval=watch_interval,
        )
        super().__init__(server_address, handler_cls)

    def get_project_root(self) -> Path:
        with self.__project_root_lock:
            return self.__project_root

    def set_project_root(self, project_root: Path) -> None:
        with self.__project_root_lock:
            self.__project_root = project_root

    def server_close(self) -> None:
        # The watcher thread must never outlive its server (it would keep
        # statting -- and on a change, re-ingesting -- a project nothing is
        # serving anymore). stop() is safe when the thread was never
        # started, and idempotent, so double server_close stays harmless.
        self.watcher.stop()
        super().server_close()


class RceRequestHandler(BaseHTTPRequestHandler):
    server_version = "RCE/1"
    server: RceHTTPServer  # set by socketserver at construction time

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (stdlib's own name)
        logger.debug("%s - %s", self.address_string(), format % args)

    def _project_root(self) -> Path:
        """One locked read per call (RceHTTPServer.get_project_root) -- a
        handler takes its snapshot of the root and works with that; a
        concurrent switch affects the next request, never tears this one."""
        return self.server.get_project_root()

    def _open_conn(self) -> Connection:
        return db.connect(_require_db(self._project_root()))

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_from_conn(self, fn: Callable[[Connection], Any]) -> None:
        conn = self._open_conn()
        try:
            payload = fn(conn)
        finally:
            conn.close()
        self._send_json(200, payload)

    def _check_local_origin(self) -> None:
        """Reject a request whose `Host` does not name this exact loopback
        server, and one whose `Origin` (when a browser sends one at all)
        names anything else -- see module docstring's "Cross-origin
        defense". Called first thing in both `do_GET` and `do_POST`, before
        any routing or body parsing, so a rejected request never reaches
        `open_payload` or any other handler."""
        port = self.server.server_address[1]
        expected_host = f"127.0.0.1:{port}"
        host = self.headers.get("Host")
        if host != expected_host:
            raise ForbiddenOriginError(
                f"request Host {host!r} does not match this server ({expected_host!r}); refusing"
            )
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{expected_host}":
            raise ForbiddenOriginError(
                f"request Origin {origin!r} does not match this server; refusing"
            )

    def do_GET(self) -> None:  # noqa: N802 (stdlib's own method name)
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            self._check_local_origin()
            if path == "/":
                self._send_html(200, _app_html())
            elif path == "/api/summary":
                self._json_from_conn(lambda conn: summary_payload(conn, self._project_root()))
            elif path == "/api/attempts":
                self._json_from_conn(attempts_payload)
            elif path == "/api/tree":
                self._json_from_conn(lambda conn: tree_payload(conn, self._project_root()))
            elif path == "/api/lineage":
                self._json_from_conn(lambda conn: lineage_payload(conn, self._project_root()))
            elif path == "/api/projects":
                # Registry + current root only -- deliberately not routed
                # through _open_conn/_json_from_conn, since listing which
                # projects exist must keep working even when the *current*
                # project's own graph.db has gone missing mid-serve.
                self._send_json(200, projects_payload(self._project_root()))
            elif path == "/api/generation":
                # Watcher status only (task V3 phase 2) -- like
                # /api/projects, deliberately not routed through
                # _open_conn: the frontend's refresh poll must keep
                # answering even when the current project's own graph.db
                # has gone missing mid-serve (that failure surfaces as the
                # watcher's last_error, not as this endpoint erroring).
                self._send_json(200, self.server.watcher.status_payload())
            elif path == "/api/file":
                values = query.get("path")
                if not values:
                    raise MissingParamError("missing required query parameter 'path'")
                self._send_json(200, file_payload(self._project_root(), values[0]))
            elif path.startswith("/api/"):
                raise NotFoundError(f"no such endpoint: {path}")
            else:
                raise NotFoundError(f"not found: {path}")
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except Exception:
            logger.exception("unhandled error handling GET %s", self.path)
            self._send_json(500, {"error": "internal server error"})

    def _read_json_object(self) -> dict[str, Any]:
        """Every POST endpoint's body is one JSON object -- one shared
        parse+shape check, identical 400s for the same malformed input.
        Per-endpoint key requirements layer on top (`_read_json_body_with_
        path` for the path-shaped endpoints, `_parse_attempt_edit_body`
        for the attempt-edit ones)."""
        length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError as exc:
            raise MissingParamError(f"invalid JSON request body: {exc}") from exc
        if not isinstance(body, dict):
            raise MissingParamError("request body must be a JSON object")
        return body

    def _read_json_body_with_path(self) -> dict[str, Any]:
        """The two path-shaped POST endpoints (`/api/open`,
        `/api/projects/switch`) additionally require a string `"path"` key."""
        body = self._read_json_object()
        if not isinstance(body.get("path"), str):
            raise MissingParamError("request body must be a JSON object with a string 'path' key")
        return body

    def do_POST(self) -> None:  # noqa: N802 (stdlib's own method name)
        parsed = urllib.parse.urlsplit(self.path)
        try:
            # Same first-thing origin check as do_GET, before any routing or
            # body parsing -- POST endpoints have side effects (open shells
            # out; switch repoints the whole server), so this line is what
            # stands between them and a drive-by page's cross-origin fetch.
            self._check_local_origin()
            if parsed.path == "/api/open":
                body = self._read_json_body_with_path()
                payload = open_payload(self._project_root(), body["path"], bool(body.get("reveal", False)))
                self._send_json(200, payload)
            elif parsed.path == "/api/projects/switch":
                body = self._read_json_body_with_path()
                new_root, payload = switch_project_payload(body["path"])
                self.server.set_project_root(new_root)
                # Re-target the auto-refresh watcher (task V3 phase 2):
                # drops the old root's baseline/error and bumps the
                # generation, so every open page's next poll re-fetches.
                self.server.watcher.retarget()
                self._send_json(200, payload)
            elif parsed.path == "/api/attempts/preview":
                # Pure dry run (task V3 phase 3) -- but origin-checked like
                # a write anyway (above), since its twin below mutates and
                # the two must never drift apart in what reaches them.
                body = self._read_json_object()
                self._send_json(200, attempts_preview_payload(self._project_root(), body))
            elif parsed.path == "/api/attempts/write":
                # The one endpoint that writes project content: the user's
                # own map file, via rce.webapp.mapedit (backup + atomic
                # write + re-ingest under the watcher's ingest lock) --
                # see module docstring's "Write-path defense".
                body = self._read_json_object()
                self._send_json(
                    200, attempts_write_payload(self._project_root(), body, self.server.watcher)
                )
            else:
                raise NotFoundError(f"no such endpoint: {parsed.path}")
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except Exception:
            logger.exception("unhandled error handling POST %s", self.path)
            self._send_json(500, {"error": "internal server error"})


def build_server(
    project_root: Path,
    port: int,
    watch_interval: float = project_watcher.DEFAULT_INTERVAL_SECONDS,
) -> RceHTTPServer:
    """Bound to 127.0.0.1 only -- see module docstring. `port=0` (used by
    the test suite) asks the OS for a free ephemeral port; the caller reads
    the actual bound port back from `server_address[1]`. `watch_interval`
    is the auto-refresh watcher's polling period, injectable so tests can
    run a fast real-thread loop -- the watcher itself is created either
    way but only serve() starts its thread."""
    return RceHTTPServer(
        ("127.0.0.1", port), RceRequestHandler, project_root, watch_interval=watch_interval,
    )


def serve(project_root: Path, port: int, open_browser: bool = True) -> None:
    """`rce serve`'s entry point: validate the project, print the one
    startup line the task spec requires verbatim, optionally open a browser
    tab, then block serving requests until Ctrl+C. `_require_db` runs before
    `build_server` so a project that was never `rce init`ed fails with the
    same clear message every other subcommand gives, before a socket is even
    opened.

    This is also the one place the auto-refresh watcher's polling thread is
    started (task V3 phase 2) -- a served app is the only consumer of live
    re-ingestion, so build_server callers that never serve (the test
    suite's routing fixtures) never pay for a background thread. The
    `finally` block's `server_close` stops it again."""
    _require_db(project_root)
    httpd = build_server(project_root, port)
    bound_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{bound_port}"
    print(f"RCE app: {url}  (Ctrl+C to stop)")
    httpd.watcher.start()
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
