"""The node canvas's data (DESIGN.md section 8.1, 8.2, 8.6, 8.7): what
`GET /api/canvas` returns and what `POST /api/canvas/layout` persists.

Kept out of `rce.webapp.server` on purpose: the server owns routing, the
origin check and the `ApiError` -> HTTP translation; this module owns the
one question the canvas asks of the graph -- *which datasets, scripts and
figures are on screen, and which links join them* -- plus the UI-state file
beside the graph. Nothing here writes the graph or a researcher-owned file.

What is on the canvas
---------------------

Nodes are exactly the three types section 8.1 names (`dataset`, `script`,
`figure`); links are exactly the three edge types the socket grammar can
express (`reads`, `writes`, `generates`) between two such nodes. A commit's
`generates` edge to a figure (`rce.ingest.pyfig`) is therefore not a link
here -- commits are not canvas objects.

- `rejected` edges are omitted (8.2: "not drawn"); `pending` ones are kept
  and carry their status so the page can dash them.
- Links are returned in their STORED direction (`src`/`dst`, the edge's
  identity -- what `POST /api/edges/reject` takes back), plus `from`/`to`
  in data-flow direction. The two differ only for `reads`, which every
  extractor (and the mapping ingest) stores as `script --reads--> dataset`
  while the canvas draws it `数据集.数据 -> 脚本.读取`. Handing the page
  both means it never has to re-derive the reversal rule.
- `human` is true exactly for extractor `mapping` (section 8.5).
- `orphan_input` is `rce.lineage.is_orphan_input` -- the lineage report's
  own definition (a dataset some script reads and no script writes),
  evaluated over the links the canvas actually shows: an edge the human
  has rejected is not a writer, so a dataset whose only writer was marked
  as a wrong extraction gets its clay dot.
- `missing` is a fresh filesystem fact: the graph knows the node, the file
  is not on disk now. `ghost` nodes (section 8.1) are the reverse: a file
  an attempt's `step_files` names, on disk, with no graph node yet. A ghost
  carries the id the mapping ingest WILL give it (`<type>:<path>`, typed
  by `rce.ingest.dataflow.node_type_for_path`), so a position saved while
  it is a ghost survives the ghost -> real transition untouched.

Every stat below goes through `_confined`, the same resolve-then-
`relative_to` check `rce.webapp.server._resolve_within_root` applies to
client paths: node titles and step-file names are config/graph-influenced,
and a path that resolves outside the project is never stat'ed at all
(reported `missing` for a graph node, never shown as a ghost).

Scope (section 8.7)
-------------------

`scope=all` shows every canvas node plus every attempt's ghosts. Scoping
to an attempt shows its step files, every dataset/figure its step scripts
read/write/generate, and one hop further UPSTREAM along `writes -> reads`
chains: the scripts that write a dataset a step script reads (8.7: "so
upstream generators stay visible"). Downstream readers of a step's outputs
are deliberately not pulled in -- 8.7 names only the upstream direction,
and following both would re-grow the hairball the scope exists to cut.
The default scope is the current attempt: the one row whose verdict
carries ✅ when exactly one does, else the most recent row by natural `#`
order (`attempt_sort_key`); `all` only when the graph has no attempts.

Layout state (section 8.6)
--------------------------

`canvas.json` lives at `rce.paths.canvas_state_path` -- beside the graph,
outside the project, path never influenced by a request. It is derived UI
state: a missing or corrupt file (or a corrupt entry inside it) degrades
to "no saved position", never to an error; writes are atomic but never
backed up. A merge changes only the ids a request names, and `null`
deletes one, so two pages (or the debounced drag and a later one) can
never wipe each other's unrelated positions.
"""

from __future__ import annotations

import json
import logging
import math
import posixpath
import threading
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from rce import db, lineage, paths
from rce.ingest import attempts as attempts_ingest
from rce.ingest import dataflow as dataflow_ingest
from rce.ingest import mappings as mappings_ingest
from rce.webapp import mapedit

logger = logging.getLogger(__name__)

CANVAS_NODE_TYPES = ("dataset", "script", "figure")
CANVAS_EDGE_TYPES = ("reads", "writes", "generates")
SCOPE_ALL = "all"
# Section 8.7: "the ✅ row when there is exactly one". The literal marker,
# not the attempts config's `active_verdicts` (which also counts 🕒 rows as
# alive for the dead-variable check -- a different question).
CURRENT_VERDICT_MARKER = "✅"

# Restoring a rejected link puts it back at the machine status every
# extractor of a canvas edge type writes (`rce.ingest.dataflow`: always
# "auto"). Only canvas edges can be rejected/restored from the app, which
# is what makes this constant the edge's true prior status.
RESTORED_STATUS = "auto"

# One lock for every read-merge-write of a canvas.json (any project): two
# handler threads merging at once must not lose either one's ids.
_LAYOUT_LOCK = threading.Lock()


class UnknownScopeError(LookupError):
    """`scope=` names neither `all` nor an attempt node in the graph."""


class LayoutShapeError(ValueError):
    """A `POST /api/canvas/layout` body that is not the 8.6 shape."""


# -- small helpers ---------------------------------------------------------------


def _confined(root: Path, rel_path: str) -> Path | None:
    """`rel_path` under the resolved `root`, or None if it resolves outside
    it (or cannot be resolved at all) -- the caller then never stats it."""
    try:
        candidate = (root / rel_path).resolve()
        candidate.relative_to(root)
    except (ValueError, OSError):
        return None
    return candidate


def _on_disk(root: Path, rel_path: str) -> bool:
    candidate = _confined(root, rel_path)
    return candidate is not None and candidate.exists()


def _node_path(node: dict[str, Any]) -> str:
    """A node's project-relative path: its title (every extractor and the
    mapping ingest set it to exactly that), else its id's path half."""
    return node.get("title") or node["id"].partition(":")[2]


def _node_entry(node_id: str, node_type: str, path: str, *, ghost: bool, missing: bool, orphan: bool) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "path": path,
        "label": posixpath.basename(path) or path,
        "dir": posixpath.dirname(path),
        "missing": missing,
        "orphan_input": orphan,
        "ghost": ghost,
    }


def _occurrence_lines(evidence: dict[str, Any]) -> list[int]:
    occurrences = evidence.get("occurrences")
    if not isinstance(occurrences, list):
        occurrences = [evidence]  # pre-T10 legacy shape, as every evidence reader allows
    lines = {occ.get("line") for occ in occurrences if isinstance(occ, dict)}
    return sorted(n for n in lines if isinstance(n, int) and not isinstance(n, bool))


def _mapping_details(edge: dict[str, Any]) -> dict[str, Any]:
    details = edge["evidence"].get("mapping")
    return details if isinstance(details, dict) else {}


def evidence_hint(edge: dict[str, Any]) -> str:
    """The link hover card's one line (section 8.2), product language: a
    machine link names its extractor and the evidence line when there is
    one (「dataflow 提取 · 第 15 行」, 「… 第 15 行等 3 处」 for several call
    sites); a human link names who and when (「你于 2026-09-06 标注 · 备注」)."""
    if edge["extractor"] == mappings_ingest.EXTRACTOR:
        details = _mapping_details(edge)
        date, note = details.get("date"), details.get("note")
        text = f"你于 {date} 标注" if isinstance(date, str) and date else "你的标注"
        if isinstance(note, str) and note:
            text += f" · {note}"
        return text
    text = f"{edge['extractor']} 提取"
    lines = _occurrence_lines(edge["evidence"])
    if lines:
        text += f" · 第 {lines[0]} 行"
        if len(lines) > 1:
            text += f"等 {len(lines)} 处"
    return text


def link_id(edge: dict[str, Any]) -> str:
    """Unambiguous whatever the paths contain: the edge identity as JSON."""
    return json.dumps(
        [edge["src"], edge["dst"], edge["type"], edge["extractor"]], ensure_ascii=False, separators=(",", ":"),
    )


def link_entry(edge: dict[str, Any]) -> dict[str, Any]:
    human = edge["extractor"] == mappings_ingest.EXTRACTOR
    details = _mapping_details(edge) if human else {}
    flow_from, flow_to = (
        (edge["dst"], edge["src"]) if edge["type"] == "reads" else (edge["src"], edge["dst"])
    )
    return {
        "id": link_id(edge),
        "src": edge["src"],
        "dst": edge["dst"],
        "from": flow_from,
        "to": flow_to,
        "type": edge["type"],
        "extractor": edge["extractor"],
        "status": edge["status"],
        "human": human,
        "evidence_hint": evidence_hint(edge),
        "date": details.get("date") if human else None,
        "note": details.get("note") if human else None,
    }


def is_canvas_edge(conn: Connection, edge: dict[str, Any]) -> bool:
    """Whether `edge` is one the canvas draws (ignoring status): a grammar
    edge type between two canvas-typed graph nodes. Used by the reject/
    restore endpoints so the app can only change statuses it shows."""
    if edge["type"] not in CANVAS_EDGE_TYPES:
        return False
    for node_id in (edge["src"], edge["dst"]):
        node = db.get_node(conn, node_id)
        if node is None or node["type"] not in CANVAS_NODE_TYPES:
            return False
    return True


# -- attempts and scope ----------------------------------------------------------


def _steps_dir(project_root: Path) -> str | None:
    """`.rce/attempts.toml`'s `steps_dir`, or None (same degrade as
    `server._load_steps_dir`: no config, no step layer -- never a guess)."""
    try:
        return attempts_ingest.load_config(project_root).steps_dir
    except attempts_ingest.AttemptsConfigError:
        return None


def _step_paths(attempt: dict[str, Any], steps_dir: str | None) -> list[str]:
    if not steps_dir:
        return []
    out = []
    for name in attempt["attrs"].get("step_files") or []:
        if isinstance(name, str) and name:
            out.append(posixpath.normpath(f"{steps_dir}/{name}"))
    return out


def _attempt_order_key(node: dict[str, Any]) -> tuple[Any, ...]:
    attrs = node["attrs"]
    return (attempts_ingest.attempt_sort_key(attrs.get("number", "")), attrs.get("source_file", ""))


def default_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Section 8.7's "current attempt": the single ✅ row, else the most
    recent by natural `#` order (a label with no leading digits -- which
    `attempt_sort_key` sorts last -- never wins "most recent" over a real
    number). None when there are no attempts at all."""
    if not attempts:
        return None
    current = [a for a in attempts if CURRENT_VERDICT_MARKER in a["human_fields"].get("verdict", "")]
    if len(current) == 1:
        return current[0]
    numbered = [a for a in attempts if math.isfinite(_attempt_order_key(a)[0][0])]
    return max(numbered or attempts, key=_attempt_order_key)


def _scope_summary(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": attempt["id"],
        "number": attempt["attrs"].get("number", ""),
        "title": attempt.get("title") or attempt["attrs"].get("description", ""),
        "verdict": attempt["human_fields"].get("verdict", ""),
    }


# -- GET /api/canvas -------------------------------------------------------------


def build_canvas(conn: Connection, project_root: Path, scope: str | None = None) -> dict[str, Any]:
    """The whole `GET /api/canvas` body (module docstring). `scope` is
    `"all"`, an attempt node id, or None for the default scope; anything
    else raises `UnknownScopeError`."""
    root = Path(project_root).resolve()
    nodes = {n["id"]: n for t in CANVAS_NODE_TYPES for n in db.get_nodes_by_type(conn, t)}
    edges = [
        e for t in CANVAS_EDGE_TYPES for e in db.query_edges(conn, type=t)
        if e["status"] != "rejected" and e["src"] in nodes and e["dst"] in nodes
    ]
    attempts = sorted(db.get_nodes_by_type(conn, "attempt"), key=_attempt_order_key)
    by_id = {a["id"]: a for a in attempts}
    default = default_attempt(attempts)
    if scope is None:
        scope = default["id"] if default is not None else SCOPE_ALL
    if scope != SCOPE_ALL and scope not in by_id:
        raise UnknownScopeError(f"no attempt {scope!r} in the graph (scope must be 'all' or an attempt id)")

    steps_dir = _steps_dir(root)
    # Step files of each attempt, typed; ghosts are the on-disk ones with no node.
    step_ids: dict[str, list[str]] = {}
    ghosts: dict[str, tuple[str, str]] = {}
    for attempt in attempts:
        ids = []
        for path in _step_paths(attempt, steps_dir):
            node_type = dataflow_ingest.node_type_for_path(path)
            if node_type is None:
                continue
            node_id = f"{node_type}:{path}"
            if node_id not in nodes:
                if not _on_disk(root, path):
                    continue
                ghosts[node_id] = (node_type, path)
            ids.append(node_id)
        step_ids[attempt["id"]] = ids

    if scope == SCOPE_ALL:
        visible = set(nodes) | set(ghosts)
        framed = attempts
    else:
        own = set(step_ids[scope])
        scripts = {i for i in own if i in nodes and nodes[i]["type"] == "script"}
        touched = {e["dst"] for e in edges if e["src"] in scripts}
        read = {e["dst"] for e in edges if e["src"] in scripts and e["type"] == "reads"}
        upstream = {e["src"] for e in edges if e["type"] == "writes" and e["dst"] in read}
        visible = own | touched | upstream
        framed = [by_id[scope]]

    links = [e for e in edges if e["src"] in visible and e["dst"] in visible]
    readers = {e["dst"] for e in edges if e["type"] == "reads"}
    writers = {e["dst"] for e in edges if e["type"] == "writes"}

    node_entries = []
    for node_id in sorted(visible):
        if node_id in nodes:
            node = nodes[node_id]
            path = _node_path(node)
            node_entries.append(_node_entry(
                node_id, node["type"], path, ghost=False,
                missing=not _on_disk(root, path),
                orphan=lineage.is_orphan_input(node["type"], node_id in readers, node_id in writers),
            ))
        else:
            node_type, path = ghosts[node_id]
            node_entries.append(_node_entry(node_id, node_type, path, ghost=True, missing=False, orphan=False))

    frames = []
    for attempt in framed:
        members = [i for i in step_ids[attempt["id"]] if i in visible]
        if members or scope != SCOPE_ALL:
            summary = _scope_summary(attempt)
            frames.append({
                "attempt_id": summary["id"], "number": summary["number"],
                "title": summary["title"], "verdict": summary["verdict"], "node_ids": members,
            })

    layout = load_layout(project_root)
    default_id = default["id"] if default is not None else None
    return {
        "nodes": node_entries,
        "links": sorted((link_entry(e) for e in links), key=lambda link: link["id"]),
        "frames": frames,
        "positions": {k: v for k, v in layout["positions"].items() if k in visible},
        "viewport": layout["viewport"],
        "scope": {"id": SCOPE_ALL} if scope == SCOPE_ALL else _scope_summary(by_id[scope]),
        "scopes": [dict(_scope_summary(a), current=a["id"] == default_id) for a in attempts],
    }


# -- canvas.json (section 8.6) ---------------------------------------------------


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_position(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(_finite_number(v) for v in value)


def _valid_viewport(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and all(_finite_number(value.get(k)) for k in ("x", "y", "zoom"))
        and value["zoom"] > 0
    )


def load_layout(project_root: Path) -> dict[str, Any]:
    """`{"positions": {id: [x, y]}, "viewport": {x, y, zoom} | None}` from
    canvas.json. Missing, unreadable, non-JSON or wrongly shaped content
    degrades to empty (section 8.6), entry by entry: one bad position
    drops only itself."""
    empty: dict[str, Any] = {"positions": {}, "viewport": None}
    path = paths.canvas_state_path(project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.info("ignoring unreadable canvas state %s (%s) -- no saved positions", path, exc)
        return empty
    if not isinstance(data, dict):
        return empty
    raw_positions = data.get("positions")
    positions = {
        k: [float(v[0]), float(v[1])]
        for k, v in (raw_positions.items() if isinstance(raw_positions, dict) else ())
        if isinstance(k, str) and k and _valid_position(v)
    }
    viewport = data.get("viewport")
    if _valid_viewport(viewport):
        viewport = {k: float(viewport[k]) for k in ("x", "y", "zoom")}
    else:
        viewport = None
    return {"positions": positions, "viewport": viewport}


def parse_layout_body(body: dict[str, Any]) -> tuple[dict[str, list[float] | None] | None, Any]:
    """Validate a layout POST body; returns `(positions or None, viewport)`
    where `viewport` is the sentinel `...` when absent. Every number must be
    finite (NaN/Infinity would poison the JSON file for every later read),
    a position is exactly `[x, y]` or `null` (delete), and a viewport is
    `{x, y, zoom}` with `zoom > 0`, or `null` (forget it)."""
    unknown = set(body) - {"positions", "viewport"}
    if unknown:
        raise LayoutShapeError(f"unknown key(s) in layout body: {', '.join(sorted(unknown))}")
    positions = body.get("positions")
    if positions is not None:
        if not isinstance(positions, dict):
            raise LayoutShapeError("'positions' must be an object of node id -> [x, y] or null")
        for key, value in positions.items():
            if not key:
                raise LayoutShapeError("a position's node id must be a non-empty string")
            if value is not None and not _valid_position(value):
                raise LayoutShapeError(f"position for {key!r} must be [x, y] (finite numbers) or null")
    viewport: Any = ...
    if "viewport" in body:
        viewport = body["viewport"]
        if viewport is not None:
            if not _valid_viewport(viewport) or set(viewport) - {"x", "y", "zoom"}:
                raise LayoutShapeError("'viewport' must be {x, y, zoom} with finite numbers and zoom > 0, or null")
    return positions, viewport


def save_layout(project_root: Path, body: dict[str, Any]) -> dict[str, Any]:
    """Merge a validated layout body into canvas.json and write it
    atomically (no backup -- section 8.6). Only ids named in `positions`
    change; `null` deletes that id. Returns the merged layout."""
    positions, viewport = parse_layout_body(body)
    with _LAYOUT_LOCK:
        layout = load_layout(project_root)
        for key, value in (positions or {}).items():
            if value is None:
                layout["positions"].pop(key, None)
            else:
                layout["positions"][key] = [float(value[0]), float(value[1])]
        if viewport is not ...:
            layout["viewport"] = (
                None if viewport is None else {k: float(viewport[k]) for k in ("x", "y", "zoom")}
            )
        path = paths.canvas_state_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(layout, ensure_ascii=False, sort_keys=True).encode("utf-8")
        mapedit.atomic_replace_bytes(path, data)
    return layout
