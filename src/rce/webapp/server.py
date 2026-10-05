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
                            initialized, available}], "current": path}`.
                            `available` is DESIGN.md section 8.10 rule 3's
                            question -- is the directory still there at all
                            -- so the switcher can mark a dead entry and
                            offer to remove it (see `projects_payload`).
    POST /api/projects/remove -- body `{"path"}`: drop one entry from the
                            registry, matched by the same string equality
                            `/switch` uses. Removes a bookmark, never a
                            project: nothing on disk is touched (see
                            `remove_project_payload`).
    POST /api/projects/switch -- body `{"path"}`: repoint this running
                            server at another *registered, initialized*
                            project and return the new current. The path
                            must be string-equal to a registry entry --
                            see "Switch-target defense" below (see
                            `switch_project_payload`). Optional `"id"`
                            narrows the match to that entry (V5).
    POST /api/projects/locate -- body `{"id", "path"}`: 「选择新位置…」 for
                            an entry whose folder moved; adopts the chosen
                            folder only if it carries that id (V5, see
                            `locate_payload`).
    POST /api/project/resolve -- body `{"answer": "fork"|"claim"|"other"|
                            "readonly"|"restore"|"adopt"}`: the answer to a
                            blocked project's question -- only one its
                            `situation.answers` lists (V5, DESIGN.md 9.4,
                            9.12; see `resolve_payload`).
    GET  /api/engine    -- which project this engine serves: `{"engine":
                            "rce", "version": 5, "pid", "project_root",
                            "project_id", "graph_path", "rce_home",
                            "blocked", "needs_migration"}` -- what `rce
                            migrate` asks before retiring an old index (9.12;
                            see `engine_payload`). Opens no index.
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
    GET  /api/canvas    -- `?scope=all|<attempt id>` (default: the current
                            attempt): the node canvas's datasets/scripts/
                            figures, links, attempt frames, saved positions
                            and scope list (DESIGN.md 8.1/8.7; see
                            `rce.webapp.canvas.build_canvas`).
    POST /api/canvas/layout -- body `{"project": <as GET /api/canvas
                            returned it>, "scope": "all"|<attempt id>,
                            "positions"?: {id: [x, y] | null},
                            "viewport"?: {x, y, zoom} | null,
                            "reset"?: bool}`: merge into THAT view's entry
                            of the arrangement record `.rce/canvas.json`
                            (8.4, 9.2; see `canvas_layout_payload`); 409
                            `layout_unreadable` while that file cannot be
                            read -- it is never written over.
    POST /api/mappings/add -- body `{"from", "to", "type", "note"?}`: append
                            one human mapping to `.rce/mappings.toml` and
                            re-ingest it; returns the resulting link (8.5;
                            see "Canvas write defense" below).
    POST /api/mappings/delete -- body `{"from", "to", "type"}`: remove that
                            entry from the file and re-ingest.
    POST /api/edges/reject -- body `{"src", "dst", "type", "extractor"}`:
                            标记为错误提取 -- a `rejected` entry in
                            `.rce/judgements.toml`, then applied (V5, 9.3;
                            `rce.records.judgements.judge`, the one human
                            write path). A human mapping is refused:
                            deleting the entry is how it goes.
    POST /api/edges/restore -- same body: undo the above -- an `undone`
                            entry naming the reject (see
                            `edge_status_payload`).
    POST /api/judgements -- body `{"src", "dst", "type", "extractor",
                            "verdict": confirmed|rejected|withdrawn|undone,
                            "note"?}`: any human act on a machine link from
                            the app, through the same write path.
    GET  /api/review    -- 9.6's list: judgments under review or in
                            conflict (待复核), held because their source could
                            not be read, or whose link the index lacks, plus
                            the judgment ledger's trust state.
    GET  /api/history   -- `?src&dst&type&extractor`: every ledger entry
                            for one link, in order (9.9 #12).
    POST /api/records/answer -- body `{"file": "judgements", "answer":
                            "file"|"restore"}` answers 「记录文件比图谱少了 N
                            条判断」 (9.3); `{"file": "canvas", "answer":
                            "set_aside"}` moves an arrangement record RCE
                            cannot read into `.rce/backups/`.
    GET  /api/migration -- 9.5: the pre-V5 indexes waiting for this folder,
                            each with what it holds and its match (how many
                            of its judged links' endpoints a scan of this
                            folder produces -- a scan into a scratch index
                            that is removed), and an unfinished migration.
    POST /api/migration/run -- body `{"answer": "migrate"|"not_mine"}`: the
                            explicit act of 9.5 (`rce.migration.migrate`,
                            under the project lock from first step to last),
                            or 「这不是这个项目的」 (the old index untouched,
                            the refusal remembered for this folder). The
                            folder is reopened afterwards (it may now have
                            an id). `summary.migration` says what waits.
    POST /api/shutdown  -- respond `{"ok": true}`, then stop this server's
                            `serve_forever` loop from a separate thread
                            (task V3 phase 4) -- the app's 停止服务 button;
                            see "Shutdown defense" below. Optional body
                            `{"pid": int}`: 409 and keep serving unless it
                            is this process (RCE.app's quit; see
                            `_check_shutdown_target`).
    GET  /             -- the single-page app (task V2), served verbatim from
                            `src/rce/webapp/app.html`: inline CSS/JS, zero
                            external resources, zero build step -- it reads
                            this same JSON API entirely client-side (see that
                            file's own top comment for the two-view contract).
    GET  /canvas.js    -- the node canvas's script (DESIGN.md section 8, task
                            V4 phase 2a), served verbatim from
                            `src/rce/webapp/canvas.js` with the same
                            read-fresh discipline as `/`. The page's one
                            same-origin `<script src>`: kept out of app.html
                            so the canvas (and the link editing that builds
                            on it) stays a file of its own rather than
                            doubling the page. Same origin check as every
                            other route -- a foreign page cannot even fetch
                            the script through a rebound hostname.

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
`http://127.0.0.1:<port>` or the portless `http://127.0.0.1` (Safari drops
the non-default port when serializing a same-origin POST's Origin -- a
foreign page's Origin still always names its own host, so the portless
loopback form proves the same thing the exact one does) -- missing entirely
is accepted, since a non-browser CLI caller (`curl`, this module's own test
suite) never sends one.

Switch-target defense (`POST /api/projects/switch`, task V3 phase 1): this
is the one endpoint that changes what the whole server serves, so its input
is never treated as a filesystem path at all. The requested string must be
*string-equal* to a `rce.webapp.registry.load()` entry's `"path"` -- no
resolution, no normalization, no prefix logic is ever applied to the
client-supplied value -- and that entry must be an initialized project
(its graph exists, `rce.paths.graph_exists`). The registry lives at
`~/.rce/projects.json`, outside every project root, and only
`rce serve <path>` on this user's own
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

Canvas write defense (DESIGN.md section 8.5/8.6, task V4 phase 1b): the
canvas adds the second researcher-owned file the app writes,
`.rce/mappings.toml`, and the same layers apply in the same order.
`_check_local_origin` first. The file written is never named by the
request -- it is always `rce.ingest.mappings.mappings_path(root)`; the
request names only the entry's `from`/`to`, which the phase-1a validator
(`rce.ingest.mappings`, never a second copy of its grammar) confines to the
project root with the same resolve-then-`relative_to` check every read path
takes, before anything is written. The write itself is that module's
fixed-schema writer (backup to `.rce/backups/`, durable atomic replace),
run under the watcher's ingest lock together with the re-ingest of the
mappings file alone, after which the watcher re-baselines ONLY that file
(`record_external_change(absorb=...)`) so an unrelated save it has not
ingested yet is never swallowed. `_require_db` runs before the write, so a
project whose graph is missing or still in iCloud refuses cleanly instead
of writing a file it then cannot ingest (or blocking on a dataless open).
`.rce/canvas.json` is written to a path computed from the served root
alone (`rce.webapp.canvas.canvas_record_path`), never one a request names.
Since V5 the edge-status endpoints and `POST /api/judgements` write NO
status into the graph: they append to `.rce/judgements.toml` through
`rce.records.judgements.judge` -- record first, index second (9.1) -- and
only for links the canvas actually draws (`canvas.is_canvas_edge`) or the
review list names, never for a mapping edge (whose truth is its file).

Shutdown defense (`POST /api/shutdown`, task V3 phase 4): the one endpoint
whose side effect is the server itself, so `_check_local_origin` runs first
exactly as everywhere else -- a drive-by page must not be able to kill the
user's running app with a no-preflight cross-origin POST (denial of service
is a side effect too, and this endpoint needs no body at all, so nothing
else would stand in the way). POST-only like every other side effect; a GET
of the path falls through to the ordinary unknown-endpoint 404. The `{"ok":
true}` response is written before anything stops, and `httpd.shutdown()` is
then called from a NEW daemon thread: `shutdown()` blocks until
`serve_forever` has actually exited, so calling it synchronously from the
handler would, on a single-threaded HTTP server, deadlock the very loop it
waits on -- and even under `ThreadingHTTPServer` (where the handler thread
is not the serve loop's thread) detaching it keeps this handler's own
response/connection teardown independent of the loop's. Only the serve loop
is stopped here; closing the listening socket and stopping the watcher stay
where they already live -- `serve()`'s finally-block `server_close`, which
`serve_forever`'s return now reaches, so a UI-initiated stop and a Ctrl+C
tear down through the identical path.

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

Where the graph is, and what happens when it is not there (DESIGN.md
section 8.10, resilience rules 1 and 2): the graph is NOT in the project --
it is at `~/.rce/graphs/<id>/graph.db`, resolved by `rce.paths`, because a
project inside an iCloud-synced folder made `sqlite3.connect` block for over
a minute on an evicted file while `/api/generation` and `/api/projects` kept
answering. `_require_db` therefore does three things before any handler gets
a connection: it runs the one-time legacy migration (`resolve_graph_db`), it
refuses a missing graph with `ProjectNotInitializedError`, and -- the
non-obvious one -- it checks macOS's `SF_DATALESS` flag on the file and
raises `GraphDownloadingError` rather than calling `open()` on a file whose
content is still in the cloud, since that call is what blocks and there is
no non-blocking form of it. Both errors carry a machine-readable `state`
alongside their message so the page can render a header state in product
language (「项目不可用 — 图谱文件已不存在」 / 「图谱文件正在从云端下载…」) instead
of an English error box, with the project switcher left usable so the
researcher can move to another project. `/api/projects` and
`/api/generation` still deliberately bypass the database entirely, so a
degraded project can always be navigated away from.

Project identity (DESIGN.md 9.4, task V5). What the server serves is a
`ServedProject`: the folder, the id it was opened with (None for a pre-V5
folder), and -- when the identity check at the entry point (`rce serve`,
a switch, 「选择新位置…」) found a situation the researcher must answer
first, or a registry entry whose folder is gone -- the machine-readable
`blocked` payload. A blocked project is served anyway: every endpoint that
reads or writes the project answers 409 with `state: "project_blocked"`
and the situation's data (`situation`), so the app can ask the question;
`/api/projects` and `/api/generation` keep answering; nothing is written.
`POST /api/project/resolve {answer: fork|claim|other|readonly|restore|adopt}` takes the
answer (origin-checked like every POST); `POST /api/projects/locate {id,
path}` re-attaches a registry entry whose folder moved, adopting the
chosen folder only if it carries that id -- the one endpoint that takes a
filesystem path from the page, and it reads nothing there but the
identity file and opens nothing that does not carry an id the researcher
already registered.

Every write a page can cause -- a human record (attempt rows, mappings,
link statuses, the canvas arrangement) or an index write (the watcher's
re-ingest) -- runs inside `RceHTTPServer.write_guard`: the cross-process
project lock (9.7), then the 9.4 re-check that the served folder still
exists, still carries the id it was opened with and is still that id's
home. A folder moved in Finder under a running engine therefore gets
nothing written at its old path, and the page is told 「项目已移动或已在
别处认领，请重新打开」 (`state: "project_moved"`). A pre-V5 project refuses
human records (`needs_migration`), and so does one whose migration has
not finished -- whose old index keeps serving reads until the new one is
installed (`_served_db`); a project opened read-only refuses
every write (`read_only`); a lock held by another process for too long is
`project_busy` (503), never a write without it.

Every handler-facing failure is one of the small `ApiError` subclasses below,
each carrying its own HTTP status; `RceRequestHandler` catches `ApiError`
once per request and renders `{"error": str(exc)}` -- plus `"state"` when the
error names one -- at that status, mirroring `rce.cli`'s single `CliError`
catch in `main()`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Callable, Iterator

from rce import db, lineage, migration, paths
from rce import project as project_identity
from rce.records import judgements
from rce.records import ledger as ledger_mod
from rce.records import lock as records_lock
from rce.records import situation as records_situation
from rce.ingest import attempts as attempts_ingest
from rce.ingest import dataflow as dataflow_ingest
from rce.ingest import mappings as mappings_ingest
from rce.webapp import canvas, mapedit
from rce.webapp import registry as project_registry
from rce.webapp import watcher as project_watcher

logger = logging.getLogger(__name__)

# Kept as module attributes for the messages that quote them; rce.paths
# owns the definitions (DESIGN.md section 8.10 rule 1).
RCE_DIRNAME = paths.RCE_DIRNAME
DB_FILENAME = paths.DB_FILENAME

_FILE_SIZE_LIMIT = 200 * 1024  # 200KB (task V1 spec)


# -- Errors -------------------------------------------------------------------


class ApiError(Exception):
    """Base for every error an endpoint can raise; `status` is the HTTP code
    `RceRequestHandler` sends back alongside `{"error": str(self)}`.

    `state` is the optional machine-readable name of a *degraded project*
    condition the frontend has its own product-language wording for
    (DESIGN.md section 8.10 rule 2). It exists because the alternative is
    the page pattern-matching English engine prose to decide what to
    render, which would break the first time a message is reworded. Only
    the two degraded-project states the design names, the canvas's two
    refusals that have their own product-language sentence
    (`mapping_exists` -> 「这条映射已存在」, `human_link` -> delete the
    标注 instead), the canvas layout's `project_changed` (the page drops
    the write, `ProjectChangedError`), and the attempt form's coded refusals
    (`AttemptEditError`, `attempt_*`) carry one; every other error stays a
    plain message the page frames generically."""

    status = 400
    state: str | None = None
    # Extra machine-readable keys merged into the error body (the blocked
    # project's situation, the registry entry a locate was for).
    extra: dict[str, Any] | None = None


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
    """No graph for this project -- never initialized, or (section 8.10
    rule 2) it vanished mid-serve. The `state` is what turns this into the
    header state 「项目不可用 — 图谱文件已不存在」 with the project switcher
    still usable, rather than an English error box over an empty view."""

    status = 400
    state = "graph_missing"


class GraphDownloadingError(ApiError):
    """The graph file exists but macOS has evicted its content to iCloud
    (`SF_DATALESS`). 503 because it is transient by nature -- the download
    is presumably in flight -- and answering it immediately is the entire
    point: `open()`ing a dataless file blocks the handler thread until the
    transfer completes, which is the hang section 8.10 rule 1 was written
    to end. The page says 「图谱文件正在从云端下载…」 and keeps polling."""

    status = 503
    state = "graph_downloading"


class GraphMigrationError(ApiError):
    """A legacy in-project `graph.db` could not be moved out of the project
    safely (`rce.paths.GraphMigrationError`). 500, not 400: nothing about
    the request is wrong, and the graph is still exactly where it was --
    this needs a human looking at a log line, not a retry."""

    status = 500


class AttemptEditError(ApiError):
    """`POST /api/attempts/preview`/`.../write` asked for an edit the map
    file's current state refuses (duplicate/unknown number, invalid field
    content, unusable config/table) -- 400: the request, not the server,
    is what cannot be satisfied. Wraps `rce.webapp.mapedit.MapEditError`
    and `rce.ingest.attempts.AttemptsConfigError` with their own messages
    intact, since those already say precisely what was wrong.

    The refusals a researcher can cause from the form carry a `state` --
    `attempt_` + `rce.webapp.mapedit.error_code` (`attempt_duplicate`,
    `attempt_not_found`, `attempt_line_break`, `attempt_table_missing`,
    `attempt_unknown_field`) -- through the same `state` channel the
    mapping refusals use, so the page shows one Chinese sentence per cause
    (DESIGN.md 8.8 "Errors") without matching the English."""

    status = 400

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.state = "attempt_" + code


def _attempt_edit_error(exc: Exception, project_root: Path) -> AttemptEditError:
    return AttemptEditError(str(exc), mapedit.error_code(exc, project_root))


class MappingEditError(ApiError):
    """`POST /api/mappings/add`/`.../delete` asked for an entry the
    phase-1a writer refuses (grammar, confinement, unknown extension, line
    breaks, an unreadable file) -- 400, its English message intact for the
    hover title; the page frames it as 「无法标注：…」 (section 8.8)."""

    status = 400


class MappingExistsError(ApiError):
    """Section 8.5: a duplicate assertion (same from/to/type) is refused at
    write time. 409 -- the request is well-formed, the file's current state
    is what conflicts -- with a `state` so the page says 「这条映射已存在」
    without pattern-matching the English message."""

    status = 409
    state = "mapping_exists"


class HumanLinkError(ApiError):
    """`POST /api/edges/reject`/`.../restore` named a human mapping. Its
    truth is `.rce/mappings.toml`, so the way to remove it is deleting the
    entry (「删除标注」), never a status write the next ingest would undo.
    The `state` lets the page offer exactly that instead of an error box."""

    status = 400
    state = "human_link"


class EdgeStatusError(ApiError):
    """A restore of a link that is not currently rejected -- 409, the
    graph's current state conflicts with the request (a second click, or
    another window already restored it)."""

    status = 409


class NotThisEngineError(ApiError):
    """`POST /api/shutdown` named a process id that is not this server's
    (see `_check_shutdown_target`). 409: the request is well-formed, but
    the engine listening on the port is not the one it meant to stop."""

    status = 409


class UnknownProjectError(ApiError):
    """`POST /api/projects/switch` asked for a path that is not a registry
    member -- 403, same as the traversal/origin rejections, because whatever
    sent it is trying to steer the server somewhere the user never
    registered (see module docstring's "Switch-target defense")."""

    status = 403


class ProjectBlockedError(ApiError):
    """The served project is in a situation the researcher must answer
    first (DESIGN.md 9.4: a copy, a home that cannot be checked, a lost or
    unreadable identity) or its registry entry's folder is gone
    (`situation: "missing"`). 409, with the situation under `situation`;
    nothing was read from or written to the index."""

    status = 409
    state = "project_blocked"

    def __init__(self, blocked: dict[str, Any]) -> None:
        message = blocked.get("detail") or blocked.get("message") or blocked.get("situation", "blocked")
        super().__init__(f"this project cannot be opened until a question is answered: {message}")
        self.extra = {"situation": blocked}


class ProjectMovedApiError(ApiError):
    """9.4's write-time re-check failed: the served folder moved, was
    removed, or was claimed elsewhere. Nothing was written."""

    status = 409
    state = "project_moved"


class NeedsMigrationApiError(ApiError):
    """A human record on a pre-V5 project (9.10). Nothing was written."""

    status = 409
    state = "needs_migration"


class ReadOnlyError(ApiError):
    """The project was opened read-only (「原位置暂时不可用，先只读打开」)."""

    status = 409
    state = "read_only"


class ProjectBusyError(ApiError):
    """Another process held the project lock for longer than a request
    waits (9.7). 503: transient; nothing was written."""

    status = 503
    state = "project_busy"


class NotThisProjectError(ApiError):
    """`POST /api/projects/locate` chose a folder that does not carry the
    registry entry's id. Nothing was adopted."""

    status = 409
    state = "not_this_project"


class AnswerRefusedError(ApiError):
    """`POST /api/project/resolve` gave an answer the folder's situation,
    as it is now, does not take. Nothing was written."""

    status = 409
    state = "answer_refused"


class RecordRefusedError(ApiError):
    """A human record write refused because the record cannot be trusted
    right now (DESIGN.md 9.3): missing though expected, in the cloud,
    unreadable, a sync conflict copy beside it, or SHRUNK (state
    `record_shrunk`, with the missing entries). Nothing was written; the
    record's state is under `records`, its Chinese sentence under
    `message`."""

    status = 409
    state = "record_untrusted"

    def __init__(self, message: str, *, state: str | None = None, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        if state is not None:
            self.state = state
        self.extra = extra or {}


class LayoutUnreadableError(ApiError):
    """`.rce/canvas.json` cannot be read: no arrangement is written over it
    until it is repaired or set aside (9.2). 409."""

    status = 409
    state = "layout_unreadable"


class NoQuestionError(ApiError):
    """`POST /api/records/answer` when there is no question to answer."""

    status = 409
    state = "no_question"


class MigrationRefusedError(ApiError):
    """`/api/migration[/run]`: nothing waits, or the folder cannot be
    migrated as it is now (9.5). Nothing was written. 409."""

    status = 409
    state = "migration_refused"


@dataclass(frozen=True)
class ServedProject:
    """What this server serves (module docstring, "Project identity")."""

    root: Path
    project_id: str | None = None
    needs_migration: bool = False
    read_only: bool = False
    blocked: dict[str, Any] | None = None
    label: str | None = None


def _missing(root: Path, project_id: str | None, label: str | None, reason: str) -> dict[str, Any]:
    return {
        "situation": "missing",
        "path": str(root),
        "project_id": project_id,
        "label": label or root.name,
        "reason": reason,
        "message": "找不到项目文件夹（可能已移动）",
        "answers": ["locate"],
        "blocked": True,
    }


def served_for(
    project_root: Path,
    *,
    expected_id: str | None = None,
    label: str | None = None,
    register: bool = False,
    probes: records_situation.Probes | None = None,
) -> ServedProject:
    """The identity check of a serving entry point, turned into what the
    server serves. `expected_id` is the registry entry's id when the
    folder is reached through the registry: a folder that is gone, or
    whose path now holds another project (or none), is the entry's
    "missing" state, and nothing about the folder found there is
    acted on. `register` records a successfully opened project as most
    recently served -- never a blocked one."""
    root = Path(project_root)
    if not root.is_dir():
        return ServedProject(root, expected_id, blocked=_missing(root, expected_id, label, "folder_gone"), label=label)
    if expected_id is not None:
        try:
            current = records_situation.classify(root, probes=probes)
        except NotADirectoryError:
            return ServedProject(root, expected_id, blocked=_missing(root, expected_id, label, "folder_gone"), label=label)
        if current.project_id != expected_id and current.situation is not records_situation.Situation.UNREADABLE_ID:
            return ServedProject(
                root, expected_id, blocked=_missing(root, expected_id, label, "path_holds_another_project"), label=label,
            )
    try:
        opened = project_identity.open_project(root, register=register, probes=probes)
    except project_identity.ProjectBlocked as exc:
        c = exc.classification
        return ServedProject(root, c.project_id, needs_migration=c.needs_migration, blocked=c.payload(), label=label)
    except project_identity.AnswerRefused as exc:
        blocked = {"situation": "changed", "path": str(root), "detail": str(exc), "answers": [], "blocked": True}
        return ServedProject(root, expected_id, blocked=blocked, label=label)
    return ServedProject(root, opened.project_id, needs_migration=opened.needs_migration, label=label)


def _served_graph_path(served: ServedProject) -> Path | None:
    """The index database this server reads for `served` -- `_served_db`'s
    choice without its checks (None when blocked: nothing is read)."""
    if served.blocked is not None:
        return None
    if served.project_id is None:
        return paths.graph_db_path(served.root)
    path = records_situation.index_db_path(served.project_id)
    if not path.exists() and served.needs_migration:
        return paths.graph_db_path(served.root)
    return path


def engine_payload(served: ServedProject) -> dict[str, Any]:
    """`GET /api/engine`: which project this engine serves, in the shape
    `rce migrate` asks for before it retires an old index (DESIGN.md 9.12:
    only an engine serving THIS project, or the old index itself, blocks
    the retirement). Reads nothing but the served state: no index is
    opened, so it answers even for a blocked or missing project."""
    graph = _served_graph_path(served)
    return {
        "engine": "rce",
        "version": 5,
        "pid": os.getpid(),
        "project_root": str(served.root),
        "project_id": served.project_id,
        "graph_path": str(graph) if graph is not None else None,
        "rce_home": str(paths.rce_home()),
        "blocked": served.blocked is not None,
        "needs_migration": served.needs_migration,
    }


def _check_shutdown_target(body: dict[str, Any]) -> None:
    """`POST /api/shutdown`'s optional `{"pid": int}`: when present, stop
    only if it is THIS process.

    Why (adversarial review of the V4 work): RCE.app sends the shutdown on
    quit whenever the engine it spawned is still running -- but that child
    may never have bound the port (e.g. blocked in a slow first-touch
    migration) while an engine the researcher started from a terminal
    did, and DESIGN.md 8.9 says such an engine is left alone. The shell
    now sends its child's pid, so the check happens here, atomically, in
    the only process that knows the answer; a mismatch is a 409 and this
    server keeps serving (the shell then stops its own child by signal).
    No `pid` (the page's own 停止服务 button) keeps the original contract."""
    if "pid" not in body:
        return
    pid = body["pid"]
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise MissingParamError("request body 'pid' must be an integer when present")
    if pid != os.getpid():
        raise NotThisEngineError(
            f"this engine is process {os.getpid()}, not {pid} -- not shutting down"
        )


def _require_db(project_root: Path) -> Path:
    """Same message shape as `rce.cli`/`rce.mcp_server`'s own `_require_db`
    -- each subsystem owns its copy (existing convention in this codebase),
    since each raises its own module's error type -- and, in this copy
    only, the two checks that stand between a handler thread and a file it
    should not touch (module docstring, "Where the graph is"):

    1. the graph resolves through `rce.paths.resolve_graph_db`, so a legacy
       in-project database is migrated out on the server's first touch;
    2. `is_dataless` is asked BEFORE the caller opens the file, because
       `sqlite3.connect` on a cloud-evicted file blocks for as long as the
       download takes and cannot be interrupted -- an honest 503 now beats
       a hung request thread later.
    """
    try:
        path = paths.resolve_graph_db(project_root)
    except paths.LegacyGraphDatalessError as exc:
        # The legacy in-project graph is the one most likely to be evicted
        # (it lives in the synced folder); the migration refused to open it
        # and asked for its download -- the same transient header state as
        # an evicted external graph, never a hung thread or a 500.
        raise GraphDownloadingError(str(exc)) from exc
    except paths.GraphMigrationError as exc:
        raise GraphMigrationError(str(exc)) from exc
    if not path.exists():
        raise ProjectNotInitializedError(
            f"no RCE project at {project_root} (missing its graph at {path}); "
            f"run 'rce init {project_root}' first"
        )
    if paths.is_dataless(path):
        raise GraphDownloadingError(
            f"the graph at {path} is not on this disk right now (macOS has evicted it to "
            f"iCloud); waiting for the download instead of opening it"
        )
    return path


def _served_db(served: ServedProject) -> Path:
    """The served project's index database, or the error that says why
    there is none to open: a blocked project (409, its situation), then
    `_require_db`'s own checks. A project with an id is opened at
    `~/.rce/graphs/<id>/graph.db` by the id it was opened with -- never by
    re-reading the folder, which may have moved under the engine."""
    if served.blocked is not None:
        raise ProjectBlockedError(served.blocked)
    if served.project_id is None:
        return _require_db(served.root)
    path = records_situation.index_db_path(served.project_id)
    if not path.exists() and served.needs_migration:
        # 9.5: until the migration's tally balances, the old index keeps
        # serving (read-only for human records) -- `graph_db_path` names it.
        return _require_db(served.root)
    if not path.exists():
        raise ProjectNotInitializedError(
            f"the index of project {served.project_id} is missing ({path}); reopen the project to rebuild it"
        )
    if paths.is_dataless(path):
        raise GraphDownloadingError(
            f"the graph at {path} is not on this disk right now (macOS has evicted it to "
            f"iCloud); waiting for the download instead of opening it"
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


def summary_payload(conn: Connection, project_root: Path, served: ServedProject | None = None) -> dict[str, Any]:
    node_counts = {t: len(db.get_nodes_by_type(conn, t)) for t in sorted(db.NODE_TYPES)}
    edge_counts = {t: 0 for t in sorted(db.EDGE_TYPES)}
    for edge in db.query_edges(conn):
        edge_counts[edge["type"]] += 1
    return {
        "project_root": str(project_root),
        # Section 8.10 rule 1: "`rce status` and `/api/summary` report the
        # graph's actual location so nothing is hidden" -- it is no longer
        # inside the project, so a user who is not told cannot find it.
        "graph_path": str(paths.graph_db_path(project_root)),
        "nodes": node_counts,
        "edges": edge_counts,
        "pending": len(db.pending_edges(conn)),
        # 9.6: links whose old judgment waits for the researcher (待复核);
        # never counted in `pending` (待确认).
        "review": judgements.review_count(conn),
        # 9.3: whether the judgment ledger can be trusted right now.
        "records": db.get_record_status(conn, judgements.RECORD_STATUS_NAME),
        "attempts_config": _attempts_config_echo(project_root),
        # 9.4 / 9.10: who this is, and whether human records can be saved.
        "project_id": served.project_id if served else None,
        "needs_migration": bool(served and served.needs_migration),
        "read_only": bool(served and served.read_only),
        # 9.5: pre-V5 indexes that may hold this folder's judgments, and an
        # unfinished migration ("legacy records waiting").
        "migration": migration.waiting_payload(project_root),
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
    flags = judgements.link_flags(conn)
    for edge in db.query_edges(conn, src=script_id, type=edge_type):
        missing = any(occ.get("missing") for occ in _occurrences(edge["evidence"]))
        marks = flags.for_key(judgements.key_of(edge))
        entries.append({
            "path": _target_path(conn, edge["dst"]),
            "role": _lineage_role(conn, edge["dst"]),
            "missing": missing,
            # 9.6 "Where it shows" (决策树): the link's status and whether
            # its judgment waits for review -- never shown as applied.
            "status": edge["status"],
            "review": marks["review"],
            "conflict": marks["conflict"],
            "judgement": marks["judgement"],
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


def entry_state(entry: dict[str, Any]) -> dict[str, Any]:
    """One registry entry, checked fresh: `available` (the folder is
    there), `initialized` (its index exists), and `missing` -- DESIGN.md
    9.4's 「找不到项目文件夹（可能已移动）」: the folder is gone, or (for an
    entry with an id) the folder at that path carries another id or none.
    An identity file that cannot be read is not "missing": opening it
    shows its own situation."""
    root = Path(entry["path"])
    available = project_registry.is_available(root)
    project_id = entry.get("id")
    if project_id is None:
        return {"available": available, "initialized": project_registry.is_initialized(root), "missing": not available}
    missing = not available
    if available:
        got = records_situation.read_identity(root)
        if got.state is records_situation.IdentityState.ABSENT or (
            got.state is records_situation.IdentityState.PRESENT and got.identity and got.identity.id != project_id
        ):
            missing = True
    initialized = records_situation.index_db_path(project_id).exists()
    return {"available": available and not missing, "initialized": initialized, "missing": missing}


def projects_payload(current: Path | ServedProject) -> dict[str, Any]:
    """The registry (`rce.webapp.registry.load()`, most-recently-served
    first), each entry's state checked fresh per request (`entry_state`)
    -- a project can be `rce init`ed, moved, or its disk unmounted between
    two calls -- plus which project this server is currently serving and,
    when it is blocked, why. The current root is reported even when it is
    not (or no longer) a registry member: it is a fact about this server,
    not about the registry.

    `initialized` false means "registered but has no index yet"; `missing`
    (with `available` false) means the folder is gone or no longer this
    project, which the switcher shows as 「找不到项目文件夹（可能已移动）」
    with 「选择新位置…」 (`POST /api/projects/locate`) or 「移除失效项目」."""
    served = current if isinstance(current, ServedProject) else ServedProject(Path(current))
    projects = [
        {"id": entry.get("id"), "path": entry["path"], "label": entry["label"], **entry_state(entry)}
        for entry in project_registry.load()
    ]
    return {
        "projects": projects,
        "current": str(served.root),
        "current_id": served.project_id,
        "blocked": served.blocked,
        "read_only": served.read_only,
        "needs_migration": served.needs_migration,
    }


def switch_project_payload(requested: str, requested_id: str | None = None) -> tuple[ServedProject, dict[str, Any]]:
    """Validate a switch request and return `(served, response_payload)`;
    the caller (the handler) is the one that actually repoints the server,
    via `RceHTTPServer.set_served` -- this function owns the validation,
    the identity check and the registry recency bump, never the server
    state.

    The requested string is compared *string-equal* against registry
    entries' stored `"path"` values (and, when the page sends one, the
    entry's `"id"`) -- it is never resolved, joined, or otherwise
    interpreted as a filesystem path, so there is nothing here for a
    crafted value to traverse or normalize its way past (module docstring,
    "Switch-target defense"). Not a member: 403 (`UnknownProjectError`).

    Then the identity check (9.4) runs on the entry's folder before
    anything is written: a folder that is gone or now holds another
    project switches to that entry's "missing" state; a folder in a
    situation to be answered switches to its blocked state -- in both
    cases the server serves the question, and nothing (registry included)
    is written. A pre-V5 or never-initialized entry keeps its V3 rules: a
    member that is not an initialized project is a 400
    (`ProjectNotInitializedError`), and a legacy in-project graph is moved
    out here, on the server's first touch. Only a switch that opened the
    project bumps it to most-recently-served."""
    entry = next(
        (e for e in project_registry.load()
         if e["path"] == requested and (requested_id is None or e.get("id") == requested_id)),
        None,
    )
    if entry is None:
        raise UnknownProjectError(
            f"{requested!r} is not a registered project -- only paths already in the "
            f"registry (~/.rce/{project_registry.REGISTRY_FILENAME}, written by "
            f"'rce serve <path>') can be switched to"
        )
    new_root = Path(entry["path"])
    if entry.get("id") is None and new_root.is_dir():
        c = records_situation.classify(new_root)
        if c.situation is records_situation.Situation.NOT_A_PROJECT:
            raise ProjectNotInitializedError(
                f"registered project {entry['path']!r} is not initialized (missing its graph at "
                f"{paths.legacy_index_db_path(new_root)}); run 'rce init {entry['path']}' first"
            )
        if c.situation is records_situation.Situation.LEGACY:
            try:
                # The server's first touch of a pre-V5 project (section 8.10
                # rule 1): its in-project graph moves out here, not on the
                # first read, so a switch followed by a read finds it.
                paths.migrate_legacy_graph(new_root)
            except paths.LegacyGraphDatalessError:
                pass  # its first read answers 「图谱文件正在从云端下载…」 and retries
            except paths.GraphMigrationError as exc:
                raise GraphMigrationError(str(exc)) from exc
    served = served_for(new_root, expected_id=entry.get("id"), label=entry["label"], register=True)
    return served, {
        "current": entry["path"],
        "label": entry["label"],
        "project_id": served.project_id,
        "blocked": served.blocked,
    }


def locate_payload(body: dict[str, Any]) -> tuple[ServedProject, dict[str, Any]]:
    """「选择新位置…」 (DESIGN.md 9.4): re-attach the registry entry `id` to
    the folder the researcher chose. The folder goes through the same
    identity check as any open, and is adopted ONLY if it carries that id
    -- RCE never searches for a folder by itself, and never attaches one
    that is not this project. The chosen path must be absolute and an
    existing folder; only its identity file is read before that check."""
    project_id = body.get("id")
    chosen = body.get("path")
    if not isinstance(project_id, str) or not isinstance(chosen, str) or not chosen:
        raise MissingParamError("request body must carry string 'id' and 'path' keys")
    entry = project_registry.find(project_id) if records_lock.PROJECT_ID_RE.match(project_id) else None
    if entry is None:
        raise UnknownProjectError(f"{project_id!r} is not a registered project")
    candidate = Path(chosen)
    if not candidate.is_absolute():
        raise MissingParamError("'path' must be an absolute folder path")
    root = candidate.resolve()
    if not root.is_dir():
        raise NotFoundError(f"{chosen!r} is not an existing folder")
    found = records_situation.classify(root)
    if found.project_id != project_id:
        err = NotThisProjectError(
            f"{root} does not carry the project {project_id} (found: {found.situation.value}"
            f"{' ' + found.project_id if found.project_id else ''}); nothing was attached"
        )
        err.extra = {"found": found.payload()}
        raise err
    served = served_for(root, expected_id=project_id, label=entry["label"], register=True)
    return served, {"current": str(root), "label": root.name, "project_id": project_id, "blocked": served.blocked}


_ANSWERS = {
    "fork": project_identity.fork,
    "claim": project_identity.claim,
    "other": project_identity.other,
    "adopt": project_identity.adopt,
    "restore": project_identity.restore,
}
RESOLVE_ANSWERS = ("fork", "claim", "other", "readonly", "adopt", "restore")


def resolve_payload(served: ServedProject, body: dict[str, Any]) -> tuple[ServedProject, dict[str, Any]]:
    """The researcher's answer to the served project's question (9.4,
    9.12): `fork` (「作为独立分支继续」), `claim` (「这里才是原项目」),
    `other` (「这是另一个项目」), `readonly` (「原位置暂时不可用，先只读打开」,
    for a home that cannot be checked: the index is read, nothing is
    adopted and nothing is written); for a lost identity file also
    `restore` (「从备份恢复项目身份文件」, offered only when a snapshot
    exists) and `adopt` (「沿用这些记录，建立新身份」). Only an answer the
    situation's `answers` lists is taken. Each answer re-checks the folder
    under the project lock and writes nothing if the question no longer
    stands. After `restore` the folder may ask a question of its own (a
    copy): `blocked` in the reply."""
    answer = body.get("answer")
    if answer not in RESOLVE_ANSWERS:
        raise MissingParamError(f"request body 'answer' must be one of {', '.join(RESOLVE_ANSWERS)}")
    blocked = served.blocked
    if blocked is None or answer not in blocked.get("answers", ()):
        raise AnswerRefusedError(
            f"this project has no open question that '{answer}' answers"
        )
    if answer == "readonly":
        if not served.project_id:
            raise AnswerRefusedError("there is no index to open read-only")
        return (
            ServedProject(served.root, served.project_id, read_only=True, label=served.label),
            {"ok": True, "answer": answer, "project_id": served.project_id, "read_only": True},
        )
    try:
        result = _ANSWERS[answer](served.root)
    except (project_identity.AnswerRefused, project_identity.ProjectBlocked) as exc:
        raise AnswerRefusedError(str(exc)) from exc
    except records_situation.WriteRefused as exc:
        raise AnswerRefusedError(str(exc)) from exc
    new_served = served_for(served.root, register=True, label=served.label)
    return new_served, {
        "ok": True,
        "answer": answer,
        "project_id": result.identity.id,
        "previous_id": result.previous_id,
        "moved_aside": list(result.moved_aside),
        "git_tracked_identity": result.git_tracked_identity,
        "build_error": result.build_error,
        "restored_from": result.restored_from,
        "blocked": new_served.blocked,
    }


def remove_project_payload(requested: str) -> dict[str, Any]:
    """Drop one entry from the registry (DESIGN.md section 8.10 rule 3), so
    the researcher never has to hand-edit `~/.rce/projects.json` when a
    project directory is gone.

    Matched by string equality against a `registry.load()` entry, exactly
    as `switch_project_payload` matches and for exactly the same reason
    (module docstring, "Switch-target defense"): the client-supplied string
    is never resolved, joined, or treated as a filesystem path, so a
    crafted value has nothing to traverse -- it either names an entry the
    user already registered, or it names nothing (403).

    Deliberately NOT gated on the entry being dead: `available` is checked
    per request and can flip between the page rendering a button and the
    user clicking it (a disk remounts, a directory is restored), and
    refusing a removal because the project came back is a worse failure
    than removing a bookmark the user asked to remove. This deletes
    nothing on disk -- no graph, no project, no file -- so the blast
    radius of being wrong is one re-registration by `rce serve <path>`.

    Removing the currently-served project is allowed too: this server keeps
    serving what it serves, and `projects_payload` already reports a
    current root that is not a registry member."""
    if not project_registry.remove(requested):
        raise UnknownProjectError(
            f"{requested!r} is not a registered project -- only paths already in the "
            f"registry (~/{project_registry.RCE_DIRNAME}/{project_registry.REGISTRY_FILENAME}) "
            f"can be removed from it"
        )
    return {"removed": requested}


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
        raise _attempt_edit_error(exc, project_root) from exc


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
        raise _attempt_edit_error(exc, project_root) from exc
    generation = watcher.record_external_change(result["ingest_error"])
    return {
        "ok": True,
        "file": result["file"],
        "backup": result["backup"],
        "generation": generation,
        "ingest_error": result["ingest_error"],
    }


# -- The node canvas (DESIGN.md section 8, task V4 phase 1b) -----------------


def _canvas_project(project_root: Path) -> str:
    """The identity a canvas payload carries and a layout POST must echo:
    the served root, compared as a string and never used as a path."""
    return str(Path(project_root).resolve())


def canvas_payload(conn: Connection, project_root: Path, scope: str | None) -> dict[str, Any]:
    """`rce.webapp.canvas.build_canvas`, with an unknown scope as a 404,
    plus `project` -- which project this picture is of (see
    `canvas_layout_payload`)."""
    try:
        payload = canvas.build_canvas(conn, project_root, scope)
    except canvas.UnknownScopeError as exc:
        raise NotFoundError(str(exc)) from exc
    payload["project"] = _canvas_project(project_root)
    return payload


class ProjectChangedError(ApiError):
    """A layout POST made in a page still showing another project: the
    server was switched (from another window, or RCE.app) after that page
    fetched its canvas. 409 -- well-formed, but it describes a picture of a
    project this server no longer serves, and two projects can share card
    ids (a copied replication package), so writing it would overwrite the
    other project's arrangement. The page drops such a write."""

    status = 409
    state = "project_changed"


def canvas_layout_payload(conn: Connection, project_root: Path, body: dict[str, Any]) -> dict[str, Any]:
    """Merge a layout body into its view of `canvas.json` (sections 8.4,
    8.6). Opened through `_open_conn`, so `_require_db` runs first: the
    file lives in the graph's own directory, which exists only for an
    initialized project, and the graph is what bounds the write -- a scope
    the project does not have is a 404 (as `GET /api/canvas`); a position
    for a card that view does not show is skipped. The body must carry the
    `project` its page's canvas payload named; any other value is a 409
    (`ProjectChangedError`) and nothing is written."""
    body = dict(body)
    project = body.pop("project", None)
    if not isinstance(project, str) or not project:
        raise MissingParamError("layout body must carry 'project', as GET /api/canvas returned it")
    if project != _canvas_project(project_root):
        raise ProjectChangedError("layout made for another project; this server now serves a different one")
    try:
        view = canvas.save_layout(conn, project_root, body)
    except canvas.UnknownScopeError as exc:
        raise NotFoundError(str(exc)) from exc
    except canvas.LayoutShapeError as exc:
        raise MissingParamError(str(exc)) from exc
    except canvas.LayoutRecordError as exc:
        error = LayoutUnreadableError(str(exc))
        error.extra = {"layout": exc.record.payload(), "message": exc.record.message}
        raise error from exc
    return {"ok": True, "scope": body["scope"], "positions": len(view["positions"]), "viewport": view["viewport"]}


def _string_fields(body: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    values = [body.get(k) for k in keys]
    if not all(isinstance(v, str) and v for v in values):
        quoted = ", ".join(f"'{k}'" for k in keys)
        raise MissingParamError(f"request body must carry non-empty string keys {quoted}")
    return values  # type: ignore[return-value]


def _mapping_write_error(exc: mappings_ingest.MappingsWriteError) -> ApiError:
    if exc.code == "duplicate":
        return MappingExistsError(str(exc))
    if exc.code == "not_found":
        return NotFoundError(str(exc))
    return MappingEditError(str(exc))


def _reingest_mappings(project_root: Path) -> None:
    """Exactly `rce mappings`' call, on its own connection, with the same
    never-conjure-a-graph refusal as `mapedit._reingest_attempts` (the
    graph could vanish between `_require_db` and here)."""
    db_path = paths.graph_db_path(project_root)
    if not db_path.exists():
        raise RuntimeError(
            f"no RCE project at {project_root} (missing its graph at {db_path}); "
            "the mappings file was written and backed up, but the graph could not be re-ingested"
        )
    conn = db.connect(db_path)
    try:
        report = mappings_ingest.ingest_mappings(conn, project_root)
        logger.info("canvas write re-ingested mappings for %s: %s", project_root, report.counts)
        judgements.apply_after_scan(conn, project_root)  # 9.1: the end of a scan
    finally:
        conn.close()


def _mapping_link(project_root: Path, entry: dict[str, Any]) -> dict[str, Any] | None:
    """The link the just-written entry became, read back from the graph --
    so the response is what the canvas will draw, not an echo of the
    request. None if the re-ingest did not land it."""
    from_type = dataflow_ingest.node_type_for_path(entry["from"])
    to_type = dataflow_ingest.node_type_for_path(entry["to"])
    if from_type is None or to_type is None:
        return None
    mapping = mappings_ingest.Mapping(
        from_path=entry["from"], to_path=entry["to"], type=entry["type"],
        from_type=from_type, to_type=to_type,
    )
    db_path = paths.graph_db_path(project_root)
    if not db_path.exists():
        return None
    conn = db.connect(db_path)
    try:
        for edge in db.query_edges(conn, src=mapping.src_id, dst=mapping.dst_id, type=mapping.type):
            if edge["extractor"] == mappings_ingest.EXTRACTOR:
                return canvas.link_entry(edge)
    finally:
        conn.close()
    return None


def _write_mapping(
    project_root: Path,
    watcher: project_watcher.ProjectWatcher,
    write: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], str | None, int]:
    """The shared write discipline of both mapping endpoints (module
    docstring, "Canvas write defense"): graph checked first; the file write
    and the mappings re-ingest under the watcher's ingest lock; the
    re-ingest's failure contained and reported, never hiding that the file
    was written; only the mappings file re-baselined; generation bumped.

    When the re-ingest FAILED, the mappings file is deliberately not
    absorbed into the watcher's baseline and the failure is not written
    into the watcher's `last_error` (adversarial review of the V4 work):

    - absorbing it made the watcher believe the change had been ingested,
      so it never retried -- the file held the entry, the graph did not,
      and redrawing the link was refused with 「这条映射已存在」. Left
      un-absorbed, the change stays a visible difference and the next poll
      re-runs the mappings ingest by itself;
    - one cause deserves one message (section 8.10 rule 2's principle):
      the response's `ingest_error` already becomes the canvas's chip, so
      also setting `last_error` put the same failure on screen twice (the
      header's 「重扫失败…」 as well). If the watcher's own retry fails too,
      THAT is reported through `last_error`, as any poll failure is.

    Nothing in the graph changed on that path, so no generation bump
    either; the response carries the current one."""
    _require_db(project_root)
    with watcher.ingest_lock:
        try:
            result = write()
        except mappings_ingest.MappingsWriteError as exc:
            raise _mapping_write_error(exc) from exc
        ingest_error: str | None = None
        try:
            _reingest_mappings(project_root)
        except Exception as exc:  # noqa: BLE001 -- containment, same as apply_edit's
            logger.exception("post-write mappings re-ingest of %s failed -- file written", project_root)
            ingest_error = str(exc)
    if ingest_error is not None:
        return result, ingest_error, int(watcher.status_payload()["generation"])  # type: ignore[call-overload]
    generation = watcher.record_external_change(
        None, absorb={str(mappings_ingest.mappings_path(project_root))},
    )
    return result, ingest_error, generation


def mappings_add_payload(
    project_root: Path, body: dict[str, Any], watcher: project_watcher.ProjectWatcher
) -> dict[str, Any]:
    """Append one human mapping (section 8.5) and return the link it became.
    `note` is optional; an empty or whitespace-only note is no note (the
    popover's 备注 field left blank)."""
    from_path, to_path, edge_type = _string_fields(body, ("from", "to", "type"))
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise MissingParamError("request body 'note' must be a string when present")
    if note is not None and not note.strip():
        note = None
    result, ingest_error, generation = _write_mapping(
        project_root, watcher,
        lambda: mappings_ingest.add_mapping(project_root, from_path, to_path, edge_type, note=note),
    )
    return {
        "ok": True,
        "file": result["file"],
        "backup": result["backup"],
        "entry": result["entry"],
        "link": _mapping_link(project_root, result["entry"]),
        "generation": generation,
        "ingest_error": ingest_error,
    }


def mappings_delete_payload(
    project_root: Path, body: dict[str, Any], watcher: project_watcher.ProjectWatcher
) -> dict[str, Any]:
    """Remove one human mapping (「删除标注」); the re-ingest then drops its
    edge -- the only way a mapping edge ever leaves the graph (8.5)."""
    from_path, to_path, edge_type = _string_fields(body, ("from", "to", "type"))
    result, ingest_error, generation = _write_mapping(
        project_root, watcher,
        lambda: mappings_ingest.delete_mapping(project_root, from_path, to_path, edge_type),
    )
    return {
        "ok": True,
        "file": result["file"],
        "backup": result["backup"],
        "removed": result["removed"],
        "generation": generation,
        "ingest_error": ingest_error,
    }


def _judgement_error(exc: judgements.JudgementRefused) -> ApiError:
    """A refused human record write as the page's error (nothing written)."""
    if exc.code == "untrusted":
        decision = exc.decision
        records = None
        if decision is not None:
            records = {
                "state": decision.verdict.value, "reason": decision.reason, "message": decision.message,
                "detail": decision.detail, "line": decision.line,
                "missing": [judgements._summary(m) for m in decision.missing],
            }
        shrunk = decision is not None and decision.verdict.value == "shrunk"
        return RecordRefusedError(
            str(exc), state="record_shrunk" if shrunk else None,
            extra={"records": records, "message": exc.message_zh},
        )
    if exc.code == "mapping":
        return HumanLinkError(str(exc))
    if exc.code == "no_such_link":
        return NotFoundError(str(exc))
    if exc.code in ("not_rejected", "nothing_to_undo"):
        return EdgeStatusError(str(exc))
    if exc.code == "no_index":
        return ProjectNotInitializedError(str(exc))
    if exc.code == "no_question":
        return NoQuestionError(str(exc))
    if exc.code in ("question_changed", "would_lose"):
        decision = exc.decision
        return RecordRefusedError(
            str(exc), state="record_" + exc.code,
            extra={
                "records": None if decision is None else {
                    "state": decision.verdict.value, "reason": decision.reason, "message": decision.message,
                    "missing": [judgements._summary(m) for m in decision.missing],
                },
                "message": RECORD_ANSWER_MESSAGES[exc.code],
            },
        )
    return MissingParamError(str(exc))


# 8.8: what the page says when an answer to 9.3's question is refused.
RECORD_ANSWER_MESSAGES = {
    "question_changed": "记录文件在提问之后又变了，请重新查看问题再回答",
    "would_lose": "记录文件当前为空或无法使用，以文件为准会丢掉仅存的判断副本；请先恢复文件，或选择「把缺少的补回文件」",
}


def _judge(served: ServedProject, key: tuple[str, str, str, str], verdict: str, **kwargs: Any) -> judgements.Judged:
    try:
        return judgements.judge(
            served.root, key, verdict, via="canvas", expected_id=served.project_id,
            timeout=WRITE_LOCK_TIMEOUT_S, **kwargs,
        )
    except judgements.JudgementRefused as exc:
        raise _judgement_error(exc) from exc


def _link_after(conn: Connection, key: tuple[str, str, str, str]) -> dict[str, Any] | None:
    src, dst, edge_type, extractor = key
    for edge in db.query_edges(conn, src=src, dst=dst, type=edge_type):
        if edge["extractor"] == extractor:
            return judgements.link_flags(conn).annotate(canvas.link_entry(edge))
    return None


def _judged_payload(conn: Connection, key: tuple[str, str, str, str], judged: judgements.Judged) -> dict[str, Any]:
    state = judged.state
    return {
        "ok": True,
        "link": _link_after(conn, key),
        "entry": judgements._summary(judged.entry.data),
        "status": judged.status,
        "judgement": None if state is None else {
            "outcome": state["outcome"], "reason": state.get("reason"),
            "label": judgements.REASON_LABELS.get(state.get("reason") or ""),
        },
    }


def edge_status_payload(conn: Connection, served: ServedProject, body: dict[str, Any], action: str) -> dict[str, Any]:
    """`action` "reject" (标记为错误提取) or "restore" (its undo) on one
    canvas link -- since V5 a ledger entry, `rejected` or an `undone`
    naming the reject, through the one human write path
    (`rce.records.judgements.judge`); the index follows from the record.

    Order of refusals: a `mapping` extractor first (whether or not such an
    edge exists, the answer is the same -- delete the entry), then a link
    that does not exist or is not one the canvas draws (404: the app can
    only change statuses it shows), then for restore a link whose last act
    is not a reject (409). The undo of a reject puts back whatever stood
    before it (8.12, kept by the ledger's undo): a link the researcher had
    *confirmed* is confirmed again. Reject is idempotent: a second click on
    a reject that stands and is applied writes nothing."""
    src, dst, edge_type, extractor = _string_fields(body, ("src", "dst", "type", "extractor"))
    key = (src, dst, edge_type, extractor)
    if extractor == mappings_ingest.EXTRACTOR:
        raise HumanLinkError(
            "this link is a human mapping from .rce/mappings.toml -- delete the mapping "
            "instead of marking it as a wrong extraction"
        )
    matches = [e for e in db.query_edges(conn, src=src, dst=dst, type=edge_type) if e["extractor"] == extractor]
    if not matches or not canvas.is_canvas_edge(conn, matches[0]):
        raise NotFoundError(f"no canvas link {src} --{edge_type}--> {dst} (extractor {extractor!r})")
    if action == "reject":
        judged = _judge(served, key, "rejected", unless_standing=True)
    else:
        judged = _judge(served, key, "undone", undo_only="rejected")
    return _judged_payload(conn, key, judged)


def judgement_payload(conn: Connection, served: ServedProject, body: dict[str, Any]) -> dict[str, Any]:
    """`POST /api/judgements`: confirm / reject / withdraw / undo one machine
    link from the app (9.3, 9.6's three review actions included), through
    the one write path. A link the index does not hold is accepted only
    when the ledger already has a history for it (settling a review of a
    link no scan produces any more)."""
    src, dst, edge_type, extractor, verdict = _string_fields(body, ("src", "dst", "type", "extractor", "verdict"))
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise MissingParamError("request body 'note' must be a string when present")
    key = (src, dst, edge_type, extractor)
    judged = _judge(served, key, verdict, note=note)
    return _judged_payload(conn, key, judged)


def history_payload(served: ServedProject, query_args: dict[str, list[str]]) -> dict[str, Any]:
    values = [(query_args.get(k) or [None])[0] for k in ("src", "dst", "type", "extractor")]
    if not all(isinstance(v, str) and v for v in values):
        raise MissingParamError("history needs query parameters src, dst, type and extractor")
    entries = judgements.history(served.root, values)
    return {"entries": entries, "readable": entries is not None}


def records_answer_payload(served: ServedProject, body: dict[str, Any]) -> dict[str, Any]:
    """`POST /api/records/answer` (module docstring)."""
    record, answer = _string_fields(body, ("file", "answer"))
    if record == "judgements":
        # The ids of the missing entries the page showed (`records.missing[].id`):
        # an answer is tied to the question the researcher saw (9.3), never
        # applied to whatever is missing at click time.
        shown = body.get("missing")
        if not isinstance(shown, list) or not all(isinstance(i, str) and i for i in shown):
            raise MissingParamError(
                "request body must carry 'missing': the ids of the missing entries the question showed"
            )
        try:
            answered = judgements.answer_shrunk(
                served.root, answer, expected_missing=shown, expected_id=served.project_id,
                timeout=WRITE_LOCK_TIMEOUT_S,
            )
        except judgements.JudgementRefused as exc:
            raise _judgement_error(exc) from exc
        return {
            "ok": True, "file": "judgements", "answer": answer,
            "missing": len(answered.missing), "appended": len(answered.appended),
        }
    if record == "canvas" and answer == "set_aside":
        moved = canvas.set_aside_layout(served.root)
        if moved is None:
            raise NoQuestionError("the arrangement record is readable or absent; nothing to set aside")
        return {"ok": True, "file": "canvas", "answer": answer, "moved_to": moved.relative_to(served.root).as_posix()}
    raise MissingParamError("'file' must be 'judgements' (answer 'file' or 'restore') or 'canvas' (answer 'set_aside')")


# -- The single-page app (task V2) -------------------------------------------

_APP_HTML_PATH = Path(__file__).parent / "app.html"


def migration_payload(served: ServedProject) -> dict[str, Any]:
    """`GET /api/migration` (9.5, for the app's question): what waits for
    this folder -- each old index with what it holds and how many of its
    judged links' endpoints a scan of THIS folder produces -- and an
    unfinished migration. Reads only (the match is a scan into a scratch
    index that is removed)."""
    if served.blocked is not None:
        raise ProjectBlockedError(served.blocked)
    waiting = migration.waiting_payload(served.root)
    previews: list[dict[str, Any]] = []
    if waiting["waiting"] and waiting["migrating_from"] is None:
        try:
            previews = [p.payload() for p in migration.preview(served.root)]
        except migration.MigrationRefused as exc:
            raise MigrationRefusedError(str(exc)) from exc
    return {**waiting, "previews": previews}


MIGRATION_ANSWERS = ("migrate", "not_mine")


def migration_run_payload(served: ServedProject, body: dict[str, Any]) -> dict[str, Any]:
    """`POST /api/migration/run {answer: migrate|not_mine}` -- the explicit
    act of 9.5. `migrate` is the researcher's yes (resuming an unfinished
    migration needs none); `not_mine` is 「这不是这个项目的」, which leaves
    the old indexes untouched and remembers the refusal for this folder.
    The retire step's checks (9.12) are the CLI's: another engine serving
    this project or this old index, or another process with it open,
    stops it; this engine itself never does (`migration.engine_holding`
    recognises its own pid)."""
    if served.blocked is not None:
        raise ProjectBlockedError(served.blocked)
    answer = body.get("answer")
    if answer not in MIGRATION_ANSWERS:
        raise MissingParamError(f"request body 'answer' must be one of {', '.join(MIGRATION_ANSWERS)}")
    try:
        if answer == "not_mine":
            return {"declined": migration.decline(served.root)}
        results = migration.migrate(served.root, yes=True)
    except migration.MigrationRefused as exc:
        raise MigrationRefusedError(str(exc)) from exc
    except project_identity.ProjectBlocked as exc:
        raise ProjectBlockedError(exc.classification.payload()) from exc
    except records_lock.ProjectLockTimeout as exc:
        raise ProjectBusyError(str(exc)) from exc
    return {"results": [r.payload() for r in results], "ok": all(r.ok for r in results)}


def _app_html() -> str:
    """`src/rce/webapp/app.html` verbatim -- read fresh on every request
    rather than cached in memory, since this is a local single-user tool
    (no request volume to speak of) and a fresh read means a developer
    editing the file sees the change on the next reload with no server
    restart. Packaged as `package-data` (pyproject.toml) so it ships
    alongside `server.py` in an installed wheel, not just this editable
    checkout."""
    return _APP_HTML_PATH.read_text(encoding="utf-8")


_CANVAS_JS_PATH = Path(__file__).parent / "canvas.js"


def _canvas_js() -> str:
    """`src/rce/webapp/canvas.js` verbatim, read fresh per request exactly
    like `_app_html` (and packaged alongside it as `package-data`). The
    page loads it with a plain same-origin `<script src="/canvas.js">`, so
    the app stays zero-build and zero-external-resource: two files served
    as written instead of one."""
    return _CANVAS_JS_PATH.read_text(encoding="utf-8")


# -- HTTP plumbing -------------------------------------------------------------


# How long a request waits for another process's hold on the project lock
# before answering `project_busy` (9.7: writers take turns; a handler thread
# never waits forever).
WRITE_LOCK_TIMEOUT_S = 10.0


class RceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type,
        project_root: Path,
        watch_interval: float = project_watcher.DEFAULT_INTERVAL_SECONDS,
        served: ServedProject | None = None,
    ) -> None:
        # Mutable since task V3 phase 1 (POST /api/projects/switch), and
        # only ever touched through the accessors below: each request runs
        # on its own thread (ThreadingHTTPServer), so a bare attribute would
        # let a switch interleave with another handler's read. Kept
        # name-mangled + locked rather than public so no future handler can
        # accidentally bypass the lock. Since V5 it is the whole
        # `ServedProject` (root, id, blocked state), computed by the
        # identity check when the caller did not pass one.
        self.__served = served if served is not None else served_for(Path(project_root))
        self.__served_lock = threading.Lock()
        # Task V3 phase 2: the auto-refresh watcher. Created here (so
        # /api/generation always has status to report, and a switch always
        # has something to retarget) but its polling thread is only started
        # by serve() -- build_server alone spawns no background work, which
        # keeps every routing-only test thread-free. Its re-ingests run
        # under this server's write guard (V5: project lock + identity
        # re-check), and it does not poll a blocked or read-only project.
        self.watcher = project_watcher.ProjectWatcher(
            self.get_project_root, interval=watch_interval,
            write_guard=lambda: self.write_guard(human=False, timeout=None),
            active=self._watcher_active,
        )
        super().__init__(server_address, handler_cls)

    def get_served(self) -> ServedProject:
        with self.__served_lock:
            return self.__served

    def set_served(self, served: ServedProject) -> None:
        with self.__served_lock:
            self.__served = served

    def get_project_root(self) -> Path:
        return self.get_served().root

    def set_project_root(self, project_root: Path) -> None:
        """Serve `project_root` after running the identity check on it."""
        self.set_served(served_for(Path(project_root)))

    def _watcher_active(self) -> bool:
        served = self.get_served()
        return served.blocked is None and not served.read_only

    @contextlib.contextmanager
    def write_guard(self, *, human: bool, timeout: float | None = WRITE_LOCK_TIMEOUT_S) -> Iterator[None]:
        """Every write a request (or the watcher) makes: refused for a
        blocked or read-only project, then the project lock and the 9.4
        re-check (`rce.records.situation.write_guard`), with each refusal
        turned into its `state` for the page. Covers refusals raised by
        the writers inside the block too (they take the same, re-entrant
        lock themselves)."""
        served = self.get_served()
        if served.blocked is not None:
            raise ProjectBlockedError(served.blocked)
        if served.read_only:
            raise ReadOnlyError("this project was opened read-only; nothing is written")
        try:
            with records_situation.write_guard(served.root, served.project_id, human=human, timeout=timeout):
                yield
        except records_situation.NeedsMigrationError as exc:
            raise NeedsMigrationApiError(str(exc)) from exc
        except records_situation.WriteRefused as exc:
            raise ProjectMovedApiError(str(exc)) from exc
        except records_lock.ProjectLockTimeout as exc:
            raise ProjectBusyError(f"another RCE process is writing this project; try again ({exc})") from exc

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

    def _served(self) -> ServedProject:
        return self.server.get_served()

    def _open_conn(self) -> Connection:
        return db.connect(_served_db(self._served()))

    def _require_unblocked(self) -> ServedProject:
        served = self._served()
        if served.blocked is not None:
            raise ProjectBlockedError(served.blocked)
        return served

    def _switch_to(self, served: ServedProject) -> None:
        self.server.set_served(served)
        # Re-target the auto-refresh watcher (task V3 phase 2): drops the
        # old root's baseline/error and bumps the generation, so every open
        # page's next poll re-fetches.
        self.server.watcher.retarget()

    def _send_api_error(self, exc: ApiError) -> None:
        """`{"error": msg}` plus `"state"` when the error names one -- the
        two degraded-project conditions the page has its own product
        language for (see `ApiError.state`). Absent for every other error,
        so the page's generic error box stays the default."""
        body: dict[str, Any] = {"error": str(exc)}
        if exc.state is not None:
            body["state"] = exc.state
        if exc.extra:
            body.update(exc.extra)
        self._send_json(exc.status, body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        self._send_text(status, html, "text/html; charset=utf-8")

    def _send_text(self, status: int, text: str, content_type: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
        # Safari serializes the Origin of a same-origin POST to a
        # non-default port WITHOUT the port ("http://127.0.0.1", observed
        # live 2026-08-30 from the RCE.app -> default-browser flow), so the
        # portless loopback form must be accepted alongside the exact one.
        # This does not widen the drive-by/rebinding surface: a foreign
        # page's Origin always carries its own hostname, and the only way a
        # browser produces a bare "http://127.0.0.1" is a page actually
        # served from loopback port 80 on this machine -- a local process,
        # outside this threat model (see module docstring).
        if origin is not None and origin not in (
            f"http://{expected_host}",
            "http://127.0.0.1",
        ):
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
            elif path == "/canvas.js":
                self._send_text(200, _canvas_js(), "text/javascript; charset=utf-8")
            elif path == "/api/summary":
                served = self._served()
                self._json_from_conn(lambda conn: summary_payload(conn, served.root, served))
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
                self._send_json(200, projects_payload(self._served()))
            elif path == "/api/engine":
                # Which project this engine serves (9.12, `rce migrate`'s
                # retire guard). Like /api/projects, not routed through
                # _open_conn: it must answer whatever state the project is in.
                self._send_json(200, engine_payload(self._served()))
            elif path == "/api/generation":
                # Watcher status only (task V3 phase 2) -- like
                # /api/projects, deliberately not routed through
                # _open_conn: the frontend's refresh poll must keep
                # answering even when the current project's own graph.db
                # has gone missing mid-serve (that failure surfaces as the
                # watcher's last_error, not as this endpoint erroring).
                self._send_json(200, self.server.watcher.status_payload())
            elif path == "/api/canvas":
                scope = (query.get("scope") or [None])[0]
                self._json_from_conn(lambda conn: canvas_payload(conn, self._project_root(), scope))
            elif path == "/api/review":
                self._json_from_conn(judgements.review_items)
            elif path == "/api/migration":
                self._send_json(200, migration_payload(self._served()))
            elif path == "/api/history":
                self._send_json(200, history_payload(self._require_unblocked(), query))
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
            self._send_api_error(exc)
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
                requested_id = body.get("id") if isinstance(body.get("id"), str) else None
                served, payload = switch_project_payload(body["path"], requested_id)
                self._switch_to(served)
                self._send_json(200, payload)
            elif parsed.path == "/api/projects/locate":
                # 「选择新位置…」 (9.4): adopts the chosen folder only if it
                # carries the registry entry's id (see locate_payload).
                served, payload = locate_payload(self._read_json_object())
                self._switch_to(served)
                self._send_json(200, payload)
            elif parsed.path == "/api/project/resolve":
                # The answer to a blocked project's question (9.4).
                served, payload = resolve_payload(self._served(), self._read_json_object())
                self._switch_to(served)
                self._send_json(200, payload)
            elif parsed.path == "/api/projects/remove":
                # Section 8.10 rule 3. POST, and origin-checked above like
                # every other side effect: this writes ~/.rce/projects.json,
                # which is the allow-list /api/projects/switch validates
                # against, so a drive-by page must never be able to edit it.
                body = self._read_json_body_with_path()
                self._send_json(200, remove_project_payload(body["path"]))
            elif parsed.path == "/api/attempts/preview":
                # Pure dry run (task V3 phase 3) -- but origin-checked like
                # a write anyway (above), since its twin below mutates and
                # the two must never drift apart in what reaches them.
                body = self._read_json_object()
                self._require_unblocked()
                self._send_json(200, attempts_preview_payload(self._project_root(), body))
            elif parsed.path == "/api/attempts/write":
                # The one endpoint that writes project content: the user's
                # own map file, via rce.webapp.mapedit (backup + atomic
                # write + re-ingest under the watcher's ingest lock) --
                # see module docstring's "Write-path defense".
                body = self._read_json_object()
                with self.server.write_guard(human=True):
                    payload = attempts_write_payload(self._project_root(), body, self.server.watcher)
                self._send_json(200, payload)
            elif parsed.path in ("/api/mappings/add", "/api/mappings/delete"):
                # The canvas's one write dialog (section 8.3/8.5): writes
                # .rce/mappings.toml, never a request-named path -- see
                # module docstring's "Canvas write defense".
                body = self._read_json_object()
                fn = mappings_add_payload if parsed.path.endswith("/add") else mappings_delete_payload
                with self.server.write_guard(human=True):
                    payload = fn(self._project_root(), body, self.server.watcher)
                self._send_json(200, payload)
            elif parsed.path in ("/api/edges/reject", "/api/edges/restore", "/api/judgements"):
                # A human act on a machine link (9.1, 9.3): the ledger first,
                # through `rce.records.judgements.judge`, then the index.
                body = self._read_json_object()
                served = self._served()
                with self.server.write_guard(human=True):
                    conn = self._open_conn()
                    try:
                        if parsed.path == "/api/judgements":
                            payload = judgement_payload(conn, served, body)
                        else:
                            action = "reject" if parsed.path.endswith("/reject") else "restore"
                            payload = edge_status_payload(conn, served, body, action)
                    finally:
                        conn.close()
                # The ledger was written and applied here: absorb it so the
                # watcher does not apply it a second time; pages re-fetch.
                payload["generation"] = self.server.watcher.record_write(
                    {str(ledger_mod.judgements_path(served.root))},
                )
                self._send_json(200, payload)
            elif parsed.path == "/api/records/answer":
                body = self._read_json_object()
                served = self._served()
                with self.server.write_guard(human=True):
                    payload = records_answer_payload(served, body)
                payload["generation"] = self.server.watcher.record_write(
                    {str(ledger_mod.judgements_path(served.root)), str(canvas.canvas_record_path(served.root))},
                )
                self._send_json(200, payload)
            elif parsed.path == "/api/migration/run":
                # 9.5's explicit act. Not inside `write_guard`: the migration
                # takes the project lock itself, from first step to last,
                # and is the one writer allowed while `migrating_from` is set.
                body = self._read_json_object()
                served = self._served()
                payload = migration_run_payload(served, body)
                # Who this folder is may have changed (a pre-V5 folder now has
                # an id): reopen it, and let every page re-fetch.
                self._switch_to(served_for(served.root, label=served.label))
                self._send_json(200, payload)
            elif parsed.path == "/api/canvas/layout":
                # UI state beside the graph (8.6) -- origin-checked like
                # every POST: a drive-by page must not scramble the canvas.
                body = self._read_json_object()
                with self.server.write_guard(human=True):
                    conn = self._open_conn()
                    try:
                        payload = canvas_layout_payload(conn, self._project_root(), body)
                    finally:
                        conn.close()
                # A record now (9.2, `.rce/canvas.json`): absorbed so the
                # watcher does not take this page's own drag for a change.
                self.server.watcher.record_write({str(canvas.canvas_record_path(self._project_root()))}, bump=False)
                self._send_json(200, payload)
            elif parsed.path == "/api/shutdown":
                # Stops this whole server (task V3 phase 4). Respond first,
                # then stop the serve loop from a separate thread -- see
                # module docstring's "Shutdown defense" for why shutdown()
                # must never be awaited from a handler, and why socket/
                # watcher cleanup deliberately stays in serve()'s own
                # finally-block server_close.
                _check_shutdown_target(self._read_json_object())
                self._send_json(200, {"ok": True})
                threading.Thread(
                    target=self.server.shutdown, name="rce-shutdown", daemon=True
                ).start()
            else:
                raise NotFoundError(f"no such endpoint: {parsed.path}")
        except ApiError as exc:
            self._send_api_error(exc)
        except Exception:
            logger.exception("unhandled error handling POST %s", self.path)
            self._send_json(500, {"error": "internal server error"})


def build_server(
    project_root: Path,
    port: int,
    watch_interval: float = project_watcher.DEFAULT_INTERVAL_SECONDS,
    served: ServedProject | None = None,
) -> RceHTTPServer:
    """Bound to 127.0.0.1 only -- see module docstring. `port=0` (used by
    the test suite) asks the OS for a free ephemeral port; the caller reads
    the actual bound port back from `server_address[1]`. `watch_interval`
    is the auto-refresh watcher's polling period, injectable so tests can
    run a fast real-thread loop -- the watcher itself is created either
    way but only serve() starts its thread."""
    return RceHTTPServer(
        ("127.0.0.1", port), RceRequestHandler, project_root, watch_interval=watch_interval, served=served,
    )


def serve(project_root: Path, port: int, open_browser: bool = True, served: ServedProject | None = None) -> None:
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
    `finally` block's `server_close` stops it again.

    V5: `served` is the identity check's result (`served_for`; computed
    here when not given). A blocked project -- or a registry entry whose
    folder is gone -- does not stop the server: it serves the question
    (module docstring, "Project identity") and writes nothing."""
    if served is None:
        served = served_for(Path(project_root))
    try:
        if served.blocked is None:
            _served_db(served)
        else:
            print(
                f"RCE: {project_root}: {served.blocked.get('situation')} -- the app shows what to do; "
                f"nothing is written until it is answered",
                file=sys.stderr,
            )
    except GraphDownloadingError as exc:
        # Not fatal at startup (section 8.10 rule 1): the graph exists, it is
        # just in iCloud right now and its download has been requested.
        # Refusing to start would turn a transient state into RCE.app's
        # 「引擎没有在 10 秒内启动」; serving lets the page show the honest
        # header state and pick the graph up by itself once it is local.
        print(f"RCE: {exc}", file=sys.stderr)
    httpd = build_server(served.root, port, served=served)
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
