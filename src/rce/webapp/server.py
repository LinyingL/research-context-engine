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
                            confirmation queue size (see `summary_payload`).
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
    GET  /             -- a placeholder page (V2 replaces this with a real
                            single-page app at `src/rce/webapp/app.html`); all
                            markup is inline, no external request of any kind.

Path-traversal defense (`/api/file` and `/api/open` alike, both required by
task V1): `_resolve_within_root` resolves the requested path -- symlinks
included -- and rejects it unless the *resolved* path is still under the
project root. This is what actually stops `../../etc/passwd`, an absolute
path, and a symlink planted inside the project that points outside it: all
three end up outside `root` after `Path.resolve()`, so the same one check
catches every case rather than pattern-matching on `..` textually (which a
symlink would trivially evade).

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
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Callable

from rce import db, lineage
from rce.ingest import attempts as attempts_ingest

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


class NotAFileError(ApiError):
    status = 400


class BinaryFileError(ApiError):
    status = 415


class UnsupportedPlatformError(ApiError):
    status = 501


class ProjectNotInitializedError(ApiError):
    status = 400


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


# -- Placeholder page (V2 replaces this with src/rce/webapp/app.html) --------

_PLACEHOLDER_HTML = """\
<!doctype html>
<html>
<head><meta charset="utf-8"><title>RCE</title></head>
<body>
<h1>RCE</h1>
<p>The single-page decision-tree app lands in V2 (src/rce/webapp/app.html).
For now, the read-only JSON API is live:</p>
<ul>
<li><a href="/api/summary">/api/summary</a></li>
<li><a href="/api/attempts">/api/attempts</a></li>
<li><a href="/api/tree">/api/tree</a></li>
<li><a href="/api/lineage">/api/lineage</a></li>
</ul>
</body>
</html>
"""


# -- HTTP plumbing -------------------------------------------------------------


class RceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_cls: type, project_root: Path) -> None:
        self.project_root = project_root
        super().__init__(server_address, handler_cls)


class RceRequestHandler(BaseHTTPRequestHandler):
    server_version = "RCE/1"
    server: RceHTTPServer  # set by socketserver at construction time

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (stdlib's own name)
        logger.debug("%s - %s", self.address_string(), format % args)

    def _project_root(self) -> Path:
        return self.server.project_root

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

    def do_GET(self) -> None:  # noqa: N802 (stdlib's own method name)
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                self._send_html(200, _PLACEHOLDER_HTML)
            elif path == "/api/summary":
                self._json_from_conn(lambda conn: summary_payload(conn, self._project_root()))
            elif path == "/api/attempts":
                self._json_from_conn(attempts_payload)
            elif path == "/api/tree":
                self._json_from_conn(lambda conn: tree_payload(conn, self._project_root()))
            elif path == "/api/lineage":
                self._json_from_conn(lambda conn: lineage_payload(conn, self._project_root()))
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

    def do_POST(self) -> None:  # noqa: N802 (stdlib's own method name)
        parsed = urllib.parse.urlsplit(self.path)
        try:
            if parsed.path != "/api/open":
                raise NotFoundError(f"no such endpoint: {parsed.path}")
            length = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError as exc:
                raise MissingParamError(f"invalid JSON request body: {exc}") from exc
            if not isinstance(body, dict) or not isinstance(body.get("path"), str):
                raise MissingParamError("request body must be a JSON object with a string 'path' key")
            payload = open_payload(self._project_root(), body["path"], bool(body.get("reveal", False)))
            self._send_json(200, payload)
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except Exception:
            logger.exception("unhandled error handling POST %s", self.path)
            self._send_json(500, {"error": "internal server error"})


def build_server(project_root: Path, port: int) -> RceHTTPServer:
    """Bound to 127.0.0.1 only -- see module docstring. `port=0` (used by
    the test suite) asks the OS for a free ephemeral port; the caller reads
    the actual bound port back from `server_address[1]`."""
    return RceHTTPServer(("127.0.0.1", port), RceRequestHandler, project_root)


def serve(project_root: Path, port: int, open_browser: bool = True) -> None:
    """`rce serve`'s entry point: validate the project, print the one
    startup line the task spec requires verbatim, optionally open a browser
    tab, then block serving requests until Ctrl+C. `_require_db` runs before
    `build_server` so a project that was never `rce init`ed fails with the
    same clear message every other subcommand gives, before a socket is even
    opened."""
    _require_db(project_root)
    httpd = build_server(project_root, port)
    bound_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{bound_port}"
    print(f"RCE app: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
