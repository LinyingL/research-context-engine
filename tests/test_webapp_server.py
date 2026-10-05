"""Tests for rce.webapp.server (task V1): the local read-only web view over
the graph.

Two layers, per the module's own separation of concerns (mirroring
rce.mcp_server's test style): the plain payload functions
(summary_payload/attempts_payload/tree_payload/lineage_payload/file_payload/
open_payload) are tested directly against the `conn` fixture and tmp_path,
no HTTP involved; a smaller set of tests spins up a real `RceHTTPServer` on
an OS-assigned port (127.0.0.1, port=0) in a background thread to cover
routing, status codes, and query/body parsing end-to-end. Path-traversal
defense (`_resolve_within_root`, exercised via both /api/file and
/api/open) gets its own dedicated tests at every layer, per the task's own
requirement.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from rce import cli, db, lineage, paths
from rce import project as project_identity
from rce.webapp import registry, server


# -- fixtures / helpers -------------------------------------------------------


def _mk_attempt(
    conn,
    source_file: str,
    number: str,
    *,
    date: str = "2026-01-01",
    description: str = "d",
    variables: str = "v",
    verdict: str = "",
    result: str = "",
    step_files: list[str] | None = None,
    step_files_broken: list[int] | None = None,
) -> str:
    node_id = f"attempt:{source_file}#{number}"
    attrs = {
        "number": number, "date": date, "description": description, "variables": variables,
        "source_file": source_file, "source_line": 1,
        "step_refs": [], "step_files": step_files or [], "step_files_broken": step_files_broken or [],
    }
    db.upsert_node(conn, node_id, "attempt", title=description, attrs=attrs)
    db.set_human_fields(conn, node_id, {"verdict": verdict, "result": result})
    return node_id


def _mk_edge(conn, script: str, path: str, node_type: str, edge_type: str, missing: bool = False) -> None:
    """Mirrors exactly what rce.ingest.dataflow.ingest_dataflow_repo itself
    writes for one recognized read/write call site (see tests/test_lineage.py's
    own `_add` helper, which this copies rather than imports -- test modules
    don't share fixtures across files in this codebase)."""
    script_id, target_id = f"script:{script}", f"{node_type}:{path}"
    db.upsert_node(conn, script_id, "script", title=script)
    db.upsert_node(conn, target_id, node_type, title=path)
    evidence = {"file": script, "line": 1, "callee": "call"}
    if missing:
        evidence["missing"] = True
    db.upsert_edge(conn, script_id, target_id, edge_type, extractor="dataflow", evidence=evidence, confidence=1.0)


def _write_attempts_config(project_root: Path, *, steps_dir: str | None = None) -> None:
    """Just enough of `.rce/attempts.toml` for `attempts_ingest.load_config`
    to succeed -- `server._load_steps_dir` (hence `tree_payload`) reads only
    the config, never the source Markdown table itself."""
    rce_dir = project_root / ".rce"
    rce_dir.mkdir(parents=True, exist_ok=True)
    lines = ['file = "map.md"', 'heading = "H"']
    if steps_dir:
        lines.append(f'steps_dir = "{steps_dir}"')
    lines += [
        "", "[columns]", 'id = "#"', 'date = "date"', 'description = "desc"',
        'variables = "vars"', 'result = "result"', 'verdict = "verdict"',
    ]
    (rce_dir / "attempts.toml").write_text("\n".join(lines))


def _init_project(project_root: Path) -> None:
    """What `rce init` leaves behind since DESIGN.md section 8.10 rule 1:
    a `.rce/` in the project for the researcher's own files, and the graph
    OUTSIDE it under `rce.paths.graph_db_path` (the conftest-wide
    `RCE_HOME` keeps that inside tmp_path)."""
    (project_root / ".rce").mkdir(parents=True, exist_ok=True)
    # V5 (DESIGN.md 9.4): an identity file, and the index under its id.
    project_identity.init_project(project_root)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway HOME for registry-touching tests (the /api/projects
    endpoints, cmd_serve's registration). The registry itself now lives
    under `rce.paths.rce_home()`, which conftest's autouse
    `isolated_rce_home` already points at a throwaway directory -- so isolation from
    the user's real `~/.rce/projects.json` holds either way; this fixture
    additionally pins HOME for anything still reading it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def live_server(tmp_path: Path):
    """A real RceHTTPServer bound to 127.0.0.1 on an OS-assigned port,
    running in a background thread for the duration of one test."""
    project = tmp_path / "proj"
    _init_project(project)
    httpd = server.build_server(project, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base_url, project
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(base_url: str, path: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(base_url + path) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get_raw(base_url: str, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(base_url + path) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(base_url: str, path: str, body: dict) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base_url + path, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _request_with_headers(
    base_url: str, method: str, path: str, headers: dict[str, str], body: bytes | None = None
) -> tuple[int, Any]:
    """Like `_get`/`_post`, but via `http.client` directly so a caller can
    set an arbitrary `Host`/`Origin` -- exactly what the cross-origin-defense
    tests below need to simulate, and something `urllib.request` won't let a
    caller override for `Host` without this lower-level escape hatch.
    `http.client` honors an explicit `Host` in `headers` (it skips generating
    its own only when one is already present -- stdlib's own
    `HTTPConnection._send_request`), so this reaches the server with exactly
    the header value under test, not whatever the real socket peer implies.
    """
    parsed = urllib.parse.urlsplit(base_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)
    finally:
        conn.close()


# -- summary_payload -----------------------------------------------------------


def test_summary_payload_counts_nodes_edges_and_pending(conn, tmp_path):
    db.upsert_node(conn, "project:x", "project")
    db.upsert_node(conn, "figure:a.png", "figure")
    db.upsert_edge(
        conn, "project:x", "figure:a.png", "includes", extractor="test",
        evidence={"note": "x"}, confidence=1.0, status="pending",
    )
    payload = server.summary_payload(conn, tmp_path)
    assert payload["project_root"] == str(tmp_path)
    # Section 8.10 rule 1: the graph is no longer inside the project, so
    # the summary has to say where it actually is.
    assert payload["graph_path"] == str(paths.graph_db_path(tmp_path))
    assert payload["graph_path"] != str(tmp_path / ".rce" / "graph.db")
    assert payload["nodes"]["project"] == 1 and payload["nodes"]["figure"] == 1
    assert payload["edges"]["includes"] == 1
    assert payload["pending"] == 1


# -- attempts_payload ----------------------------------------------------------


def test_attempts_payload_empty_returns_hint(conn):
    assert server.attempts_payload(conn) == {"attempts": [], "hint": server._NO_ATTEMPTS_HINT}


def test_attempts_payload_includes_human_fields_and_attrs(conn):
    _mk_attempt(conn, "map.md", "1", verdict="✅ alive", result="worked", step_files=["1-a.py"])
    entry = server.attempts_payload(conn)["attempts"][0]
    assert entry["id"] == "attempt:map.md#1"
    assert entry["verdict"] == "✅ alive" and entry["result"] == "worked"
    assert entry["attrs"]["step_files"] == ["1-a.py"]


# -- tree_payload ---------------------------------------------------------------


def test_tree_payload_empty_graph_returns_hint(conn, tmp_path):
    assert server.tree_payload(conn, tmp_path) == {"attempts": [], "hint": server._NO_ATTEMPTS_HINT}


def test_tree_payload_nests_lettered_children_under_numeric_parent(conn, tmp_path):
    _write_attempts_config(tmp_path, steps_dir="steps")
    (tmp_path / "steps").mkdir()
    _mk_attempt(conn, "map.md", "14", step_files=["14-split.py"])
    _mk_attempt(conn, "map.md", "14a", step_files=["14-split.py"])
    _mk_attempt(conn, "map.md", "14b", step_files=["14-split.py"])
    _mk_attempt(conn, "map.md", "15")

    payload = server.tree_payload(conn, tmp_path)
    assert [a["number"] for a in payload["attempts"]] == ["14", "15"]
    parent = payload["attempts"][0]
    assert [c["number"] for c in parent["children"]] == ["14a", "14b"]
    assert parent["scripts"][0]["path"] == "steps/14-split.py"
    assert payload["attempts"][1]["children"] == []


def test_tree_payload_lettered_attempts_are_siblings_when_parent_missing(conn, tmp_path):
    _write_attempts_config(tmp_path, steps_dir="steps")
    _mk_attempt(conn, "map.md", "14a")
    _mk_attempt(conn, "map.md", "14b")

    payload = server.tree_payload(conn, tmp_path)
    assert [a["number"] for a in payload["attempts"]] == ["14a", "14b"]
    assert all(a["children"] == [] for a in payload["attempts"])


def test_tree_payload_scoped_per_source_file_never_cross_nests(conn, tmp_path):
    """Two different attempt timelines sharing the number "14"/"14a" must
    never nest one file's "14a" under the other file's "14" just because
    the bare numbers coincide (module docstring's own scoping rule)."""
    _write_attempts_config(tmp_path)
    _mk_attempt(conn, "fileA.md", "14")
    _mk_attempt(conn, "fileB.md", "14a")

    payload = server.tree_payload(conn, tmp_path)
    numbers_and_children = {(a["id"], tuple(c["id"] for c in a["children"])) for a in payload["attempts"]}
    assert numbers_and_children == {
        ("attempt:fileA.md#14", ()),
        ("attempt:fileB.md#14a", ()),
    }


def test_tree_payload_tags_has_generator_when_a_writer_exists_anywhere(conn, tmp_path):
    _write_attempts_config(tmp_path, steps_dir="steps")
    _mk_attempt(conn, "map.md", "1", step_files=["1-run.py"])
    _mk_edge(conn, "steps/1-run.py", "data/in.csv", "dataset", "reads")
    _mk_edge(conn, "steps/0-prep.py", "data/in.csv", "dataset", "writes")

    reads = server.tree_payload(conn, tmp_path)["attempts"][0]["scripts"][0]["reads"]
    assert reads == [{
        "path": "data/in.csv", "role": "has_generator", "missing": False,
        "status": "auto", "review": False, "conflict": False, "judgement": None,
    }]


def test_tree_payload_tags_orphan_input_when_no_writer_anywhere(conn, tmp_path):
    _write_attempts_config(tmp_path, steps_dir="steps")
    _mk_attempt(conn, "map.md", "1", step_files=["1-run.py"])
    _mk_edge(conn, "steps/1-run.py", "data/in.csv", "dataset", "reads", missing=True)

    reads = server.tree_payload(conn, tmp_path)["attempts"][0]["scripts"][0]["reads"]
    assert reads == [{
        "path": "data/in.csv", "role": "orphan_input", "missing": True,
        "status": "auto", "review": False, "conflict": False, "judgement": None,
    }]


def test_tree_payload_scripts_empty_when_steps_dir_not_configured(conn, tmp_path):
    _write_attempts_config(tmp_path, steps_dir=None)
    _mk_attempt(conn, "map.md", "1", step_files=["1-run.py"])
    assert server.tree_payload(conn, tmp_path)["attempts"][0]["scripts"] == []


def test_tree_payload_scripts_empty_when_config_file_missing(conn, tmp_path):
    # No .rce/attempts.toml at all -- degrade rather than guess a prefix.
    _mk_attempt(conn, "map.md", "1", step_files=["1-run.py"])
    assert server.tree_payload(conn, tmp_path)["attempts"][0]["scripts"] == []


# -- lineage_payload -------------------------------------------------------------


def test_lineage_payload_delegates_to_build_lineage_report(conn, tmp_path):
    _mk_edge(conn, "s.py", "data/x.csv", "dataset", "reads")
    assert server.lineage_payload(conn, tmp_path) == lineage.build_lineage_report(conn, tmp_path)


# -- file_payload: normal + error paths ------------------------------------------


def test_file_payload_returns_utf8_content(tmp_path):
    (tmp_path / "a.txt").write_text("héllo", encoding="utf-8")
    payload = server.file_payload(tmp_path, "a.txt")
    assert payload == {
        "path": "a.txt", "content": "héllo", "truncated": False, "size": len("héllo".encode("utf-8")),
    }


def test_file_payload_truncates_oversized_file_and_states_so(tmp_path):
    big = "x" * (server._FILE_SIZE_LIMIT + 100)
    (tmp_path / "big.txt").write_text(big)
    payload = server.file_payload(tmp_path, "big.txt")
    assert payload["truncated"] is True
    assert len(payload["content"].encode("utf-8")) <= server._FILE_SIZE_LIMIT
    assert payload["size"] == len(big)


def test_file_payload_rejects_binary_with_null_byte(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(server.BinaryFileError):
        server.file_payload(tmp_path, "b.bin")


def test_file_payload_rejects_non_utf8_text_when_not_truncated(tmp_path):
    (tmp_path / "latin1.txt").write_bytes("café".encode("latin-1"))
    with pytest.raises(server.BinaryFileError):
        server.file_payload(tmp_path, "latin1.txt")


def test_file_payload_missing_file_raises_not_found(tmp_path):
    with pytest.raises(server.NotFoundError):
        server.file_payload(tmp_path, "nope.txt")


def test_file_payload_directory_raises_not_a_file(tmp_path):
    (tmp_path / "adir").mkdir()
    with pytest.raises(server.NotAFileError):
        server.file_payload(tmp_path, "adir")


# -- file_payload: dedicated path-traversal tests --------------------------------


def test_file_payload_rejects_dotdot_traversal(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (tmp_path / "secret.txt").write_text("top secret")
    with pytest.raises(server.PathTraversalError):
        server.file_payload(project, "../secret.txt")


def test_file_payload_rejects_absolute_path(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    with pytest.raises(server.PathTraversalError):
        server.file_payload(project, "/etc/passwd")


def test_file_payload_rejects_symlink_escaping_root(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside content")
    (project / "link").symlink_to(outside)
    with pytest.raises(server.PathTraversalError):
        server.file_payload(project, "link")


def test_resolve_within_root_allows_a_normal_relative_path(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert server._resolve_within_root(tmp_path, "a.txt") == (tmp_path.resolve() / "a.txt")


# -- open_payload: normal + error paths ------------------------------------------


def test_open_payload_rejects_non_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_is_macos", lambda: False)
    with pytest.raises(server.UnsupportedPlatformError):
        server.open_payload(tmp_path, "x", False)


def test_open_payload_calls_subprocess_with_validated_list_args(monkeypatch, tmp_path):
    (tmp_path / "f.txt").write_text("x")
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append((args, kw)))

    result = server.open_payload(tmp_path, "f.txt", False)

    expected_path = str(tmp_path.resolve() / "f.txt")
    assert calls == [(["open", expected_path], {"check": False})]
    assert isinstance(calls[0][0], list)  # list-arg form -- never shell=True
    assert result == {"opened": expected_path, "reveal": False}


def test_open_payload_reveal_uses_dash_r_flag(monkeypatch, tmp_path):
    (tmp_path / "f.txt").write_text("x")
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append((args, kw)))

    server.open_payload(tmp_path, "f.txt", True)

    assert calls[0][0][:2] == ["open", "-R"]


def test_open_payload_missing_path_raises_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    with pytest.raises(server.NotFoundError):
        server.open_payload(tmp_path, "nope.txt", False)


# -- open_payload: dedicated path-traversal tests (subprocess must never run) ----


def test_open_payload_rejects_dotdot_traversal_before_subprocess(monkeypatch, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append((args, kw)))

    with pytest.raises(server.PathTraversalError):
        server.open_payload(project, "../escape", False)
    assert calls == []


def test_open_payload_rejects_symlink_traversal_before_subprocess(monkeypatch, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    (project / "link").symlink_to(outside)
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append((args, kw)))

    with pytest.raises(server.PathTraversalError):
        server.open_payload(project, "link", False)
    assert calls == []


# -- serve() / build_server(): startup behavior ----------------------------------


def test_build_server_binds_loopback_only(tmp_path):
    httpd = server.build_server(tmp_path, 0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


def test_serve_raises_before_binding_when_project_not_initialized(tmp_path):
    with pytest.raises(server.ProjectNotInitializedError):
        server.serve(tmp_path, port=0)


def test_serve_prints_url_and_opens_browser(tmp_path, monkeypatch, capsys):
    _init_project(tmp_path)
    monkeypatch.setattr(server.RceHTTPServer, "serve_forever", lambda self: None)
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", lambda url: opened.append(url))

    server.serve(tmp_path, port=0, open_browser=True)

    out = capsys.readouterr().out
    assert out.startswith("RCE app: http://127.0.0.1:")
    assert out.rstrip("\n").endswith("(Ctrl+C to stop)")
    url = out.split("RCE app: ")[1].split("  (Ctrl+C")[0]
    assert opened == [url]


def test_serve_does_not_open_browser_when_disabled(tmp_path, monkeypatch, capsys):
    _init_project(tmp_path)
    monkeypatch.setattr(server.RceHTTPServer, "serve_forever", lambda self: None)
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", lambda url: opened.append(url))

    server.serve(tmp_path, port=0, open_browser=False)

    assert opened == []


# -- cli wiring -------------------------------------------------------------------


def _capture_serve(monkeypatch) -> list[tuple]:
    """Stub out the blocking webapp_server.serve loop, recording its args --
    cmd_serve's own logic (path resolution, registry interplay) is what
    these tests exercise, never a real socket."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        server, "serve",
        lambda root, port, open_browser=True, served=None: calls.append((root, port, open_browser)),
    )
    return calls


def test_cli_serve_reports_clean_error_for_uninitialized_project(fake_home, tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    assert cli.main(["serve", str(project)]) == 1
    err = capsys.readouterr().err
    assert "Error" in err and "rce init" in err
    # ...and the failed serve never polluted the registry with an
    # uninitialized path (cmd_serve registers only real projects).
    assert registry.load() == []


def test_cli_serve_with_path_registers_project_then_serves_it(fake_home, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    _init_project(project)
    calls = _capture_serve(monkeypatch)

    assert cli.main(["serve", str(project), "--no-browser"]) == 0

    assert [e["path"] for e in registry.load()] == [str(project.resolve())]
    assert calls == [(project.resolve(), 8317, False)]


def test_cli_serve_without_path_serves_most_recent_registry_entry(fake_home, tmp_path, monkeypatch):
    older, newer = tmp_path / "older", tmp_path / "newer"
    _init_project(older)
    _init_project(newer)
    registry.register(older)
    registry.register(newer)  # most-recently-served first
    calls = _capture_serve(monkeypatch)

    assert cli.main(["serve", "--no-browser"]) == 0

    assert len(calls) == 1
    assert str(calls[0][0]) == registry.load()[0]["path"]
    assert calls[0][0].name == "newer"


def test_cli_serve_without_path_and_empty_registry_gives_actionable_error(fake_home, monkeypatch, capsys):
    calls = _capture_serve(monkeypatch)
    assert cli.main(["serve"]) == 1
    err = capsys.readouterr().err
    assert "Error" in err and "rce serve" in err  # tells the user the fix, not just the state
    assert calls == []


# -- HTTP-level routing / status codes -------------------------------------------


def test_http_root_returns_spa_shell_with_key_mount_points(live_server):
    """task V2: `/` serves the real single-page app (src/rce/webapp/app.html),
    not the V1 placeholder -- assert the DOM hooks the app's own JS looks up
    by id/data-attribute are actually present in the served markup."""
    base_url, _ = live_server
    status, body = _get_raw(base_url, "/")
    assert status == 200
    html = body.decode("utf-8")
    assert html.lstrip().lower().startswith("<!doctype html>")
    for mount_point in (
        'id="app"', 'id="view-tree"', 'id="view-lineage"', 'id="panel"',
        'id="panel-backdrop"', 'id="panel-body"', 'id="project-switcher"',
        # Section 8.10's two additions: the degraded-project header state
        # and the dead-registry-entry cleanup button.
        'id="project-state"', 'id="remove-missing-btn"',
        'data-view="tree"', 'data-view="lineage"',
        # Task V4 phase 2a: the canvas tab and its view.
        'data-view="canvas"', 'id="view-canvas"',
    ):
        assert mount_point in html, f"missing mount point in served app.html: {mount_point}"


def test_served_app_carries_the_degraded_state_copy_in_product_language(live_server):
    """DESIGN.md sections 8.8 and 8.10: what the researcher reads for a
    degraded project is Chinese product language, and the wording is the
    design's own -- the English engine string lives on a hover title. Pinned
    here because this copy is binding, not a placeholder someone may
    casually reword.

    Also pins the keying: the page selects its wording by the server's
    machine-readable `state` name, never by matching English error prose."""
    _, body = _get_raw(live_server[0], "/")
    html = body.decode("utf-8")
    for copy in (
        "项目不可用 — 图谱文件已不存在",   # rule 2's header state
        "图谱文件正在从云端下载…",           # rule 1's dataless state
        "移除失效项目",                      # rule 3's cleanup button
        "（目录已不存在）",                  # rule 3's dead entry marker
    ):
        assert copy in html, f"missing product-language copy in app.html: {copy}"
    assert "graph_missing" in html and "graph_downloading" in html


def test_http_root_has_zero_external_resources(live_server):
    """task V2 requirement: the app must be fully self-contained (no CDN, no
    external stylesheet/script/image/font) so it works entirely offline --
    assert no http(s):// URL appears anywhere in the served page at all."""
    base_url, _ = live_server
    _, body = _get_raw(base_url, "/")
    html = body.decode("utf-8")
    assert re.search(r"https?://", html) is None


# -- The canvas's served script (DESIGN.md section 8, task V4 phase 2a) --------

_CANVAS_JS_FILE = Path(server.__file__).parent / "canvas.js"


def _get_with_type(base_url: str, path: str) -> tuple[int, str, bytes]:
    with urllib.request.urlopen(base_url + path) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def test_http_canvas_js_is_served_verbatim_as_javascript(live_server):
    """GET /canvas.js serves `src/rce/webapp/canvas.js` byte-for-byte with a
    JavaScript content type (a browser refuses to execute a script served
    as anything else under nosniff-style checks, and WebKit warns), and the
    file defines the one global the page calls -- `window.RCECanvas`."""
    status, content_type, body = _get_with_type(live_server[0], "/canvas.js")
    assert status == 200
    assert content_type.startswith("text/javascript")
    assert body == _CANVAS_JS_FILE.read_bytes()
    assert "window.RCECanvas" in body.decode("utf-8")


def test_http_canvas_js_is_read_fresh_on_every_request(live_server, monkeypatch, tmp_path):
    """Same read-fresh discipline as app.html: an edit to the file shows on
    the next request with no server restart (nothing cached in memory)."""
    fake = tmp_path / "canvas.js"
    fake.write_text("// one\n", encoding="utf-8")
    monkeypatch.setattr(server, "_CANVAS_JS_PATH", fake)
    assert _get_with_type(live_server[0], "/canvas.js")[2] == b"// one\n"
    fake.write_text("// two\n", encoding="utf-8")
    assert _get_with_type(live_server[0], "/canvas.js")[2] == b"// two\n"


@pytest.mark.parametrize("headers, word", [
    ({"Origin": "http://evil.example"}, "Origin"),
    ({"Host": "attacker.example:1234"}, "Host"),
])
def test_http_canvas_js_runs_the_origin_check_first(live_server, headers, word):
    """The script route sits behind the same `_check_local_origin` as every
    other route: a foreign page (or a rebound hostname) gets a 403, never
    the script."""
    status, payload = _request_with_headers(live_server[0], "GET", "/canvas.js", headers)
    assert status == 403
    assert word in payload["error"]


def test_served_app_loads_canvas_js_same_origin_before_its_inline_script(live_server):
    """The page references the canvas script by a same-origin path, and
    BEFORE its own inline script: canvas.js only defines `window.RCECanvas`,
    and the inline init (which may restore the 画布 view from localStorage
    at once) must find it already defined."""
    html = _get_raw(live_server[0], "/")[1].decode("utf-8")
    tag = '<script src="/canvas.js"></script>'
    assert html.count(tag) == 1
    assert html.index(tag) < html.index("<script>\n")


def test_served_app_tabs_read_in_product_language(live_server):
    """DESIGN.md section 8.8: the tabs read 「决策树」「血缘」「画布」, the brand
    mark stays RCE, and the old English tab labels are gone."""
    html = _get_raw(live_server[0], "/")[1].decode("utf-8")
    tabs = re.findall(r'<button class="tab[^"]*" data-view="(\w+)"[^>]*>([^<]+)</button>', html)
    # V5 phase 9 (9.11 "In the app"): a fourth tab, 「变量」.
    assert tabs == [("tree", "决策树"), ("lineage", "血缘"), ("canvas", "画布"), ("variables", "变量")]
    assert '<span class="brand-mark">RCE</span>' in html
    assert ">Decision Tree<" not in html and ">Lineage<" not in html


# DESIGN.md 8.8's binding glossary, English side: every spelling the old UI
# used for each term (the source spelling and, where CSS upper-cased it, the
# displayed one). Matched case-sensitively and as whole words, so code such
# as `"/api/open"` or the payload's `kind === "reads"` never trips it.
_GLOSSARY_ENGLISH = (
    "Reads", "READS", "Writes", "WRITES",
    "No recorded reads or writes",
    "Orphan inputs", "Orphan input",
    "Read by", "READ BY", "Written by", "WRITTEN BY",
    "Lineage chains", "Chains",
    "Broken links", "not found)",
    "Duplicate copies", "Other copies", "OTHER COPIES",
    "Open", "Reveal in Finder", "Close", "Loading",
)
_GLOSSARY_CHINESE = (
    "读取", "写出", "没有读到读写记录。", "无来源输入", "被这些脚本读取",
    "由这些脚本写出", "血缘链", "断链", "（读取，文件不存在）", "（写出，文件不存在）",
    "同名拷贝", "其它拷贝", "打开", "在 Finder 中显示", "关闭", "载入中…",
)


def _js_string_literals(source: str) -> list[str]:
    """Every '…', "…" and `…` literal in `source`, comments skipped -- the
    places a script's user-visible copy can live. Regex literals are
    skipped by the usual "a slash after an operator starts a regex" rule,
    which is all this codebase's two scripts need."""
    out, i, n = [], 0, len(source)
    while i < n:
        c = source[i]
        if source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
        elif source.startswith("/*", i):
            i = source.index("*/", i) + 2
        elif c in "'\"`":
            j = i + 1
            while source[j] != c:
                j += 2 if source[j] == "\\" else 1
            out.append(source[i + 1:j])
            i = j + 1
        elif c == "/" and re.search(r"(^|[(,=:\[!&|?{};+]|return)\s*$", source[max(0, i - 8):i]):
            j, in_class = i + 1, False
            while j < n and source[j] != "\n" and (source[j] != "/" or in_class):
                if source[j] == "\\":
                    j += 1
                elif source[j] == "[":
                    in_class = True
                elif source[j] == "]":
                    in_class = False
                j += 1
            i = j + 1
        else:
            i += 1
    return out


def _html_markup_copy(html: str) -> list[str]:
    """Text nodes and copy-carrying attributes of the markup itself (the
    inline script and style removed, comments removed)."""
    markup = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    markup = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", markup, flags=re.S)
    texts = [t for t in re.findall(r">([^<]+)<", markup) if t.strip()]
    attrs = re.findall(r'\b(?:title|aria-label|placeholder|alt)="([^"]*)"', markup)
    return texts + attrs


def _english_glossary_hits(pieces: list[str]) -> list[tuple[str, str]]:
    hits = []
    for piece in pieces:
        for term in _GLOSSARY_ENGLISH:
            if re.search(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", piece):
                hits.append((term, piece))
    return hits


def test_served_ui_copy_uses_the_8_8_glossary_not_its_english(live_server):
    """DESIGN.md 8.8 (amended): ALL UI copy is Chinese, and each glossary
    term has exactly one rendering. Fails if any glossary term's English
    comes back as copy -- a quoted string literal in either served script,
    or a text node / title / aria-label / placeholder in the markup.
    Comments may say what they like."""
    html = _get_raw(live_server[0], "/")[1].decode("utf-8")
    js = _get_with_type(live_server[0], "/canvas.js")[2].decode("utf-8")
    inline = html[html.index('<script>\n"use strict"') + len("<script>"):html.rindex("</script>")]

    page_literals = _js_string_literals(inline)
    canvas_literals = _js_string_literals(js)
    pieces = page_literals + canvas_literals + _html_markup_copy(html)

    assert _english_glossary_hits(pieces) == []
    # The scanner really saw the copy (a broken tokenizer must not pass by
    # finding nothing): the Chinese side of the glossary is there instead.
    seen = "\n".join(pieces)
    for term in _GLOSSARY_CHINESE:
        assert term in seen, f"glossary rendering missing from the served UI: {term}"


def test_glossary_scanner_catches_english_copy_but_not_comments_or_code():
    """The guard above is only worth something if it would fail: pin that
    it flags a reintroduced English label and ignores comments and code."""
    caught = _js_string_literals('x.textContent = "Reads"; // Reads\nfetch("/api/open")')
    assert _english_glossary_hits(caught) == [("Reads", "Reads")]
    assert _english_glossary_hits(_html_markup_copy('<button aria-label="Close">x</button>')) == [("Close", "Close")]
    assert _english_glossary_hits(_html_markup_copy("<!-- Loading… -->\n<p>载入中…</p>")) == []
    assert _english_glossary_hits(_js_string_literals("/* Open */ const r = /Open/; kind === 'reads'")) == []


def test_served_ui_has_no_hover_only_errors_and_one_detail_helper(live_server):
    """DESIGN.md 8.8 "Errors" (amended): an error that blocked an action is
    never hover-only. The 「（悬停查看原因）」 wording is gone from every action
    path in both served scripts, one shared 「详情」 helper exists, and each
    action's failure path goes through it."""
    html = _get_raw(live_server[0], "/")[1].decode("utf-8")
    js = _get_with_type(live_server[0], "/canvas.js")[2].decode("utf-8")
    assert "悬停查看原因" not in html and "悬停查看原因" not in js
    assert html.count("function renderBlockingError(") == 1
    assert "function renderBlockingError(" not in js  # shared, not copied
    assert '"详情"' in html and 'toggle.type = "button"' in html
    assert ".err-detail {" in html and "var(--ink-soft)" in html[html.index(".err-detail {"):][:300]
    for call in (
        'showHeaderError("切换项目失败", err)',                        # switch project
        'showHeaderError("移除失效项目失败", err)',                    # remove project
        'showHeaderError("停止服务失败", err)',                        # stop service
        'renderBlockingError(statusEl, reveal ? "无法在 Finder 中显示" : "无法打开", err)',
        "renderBlockingError(statusEl, cnText, err)",                 # attempt form
        "showHeaderError(message, err)",                              # shell Finder commands
    ):
        assert call in html, call
    assert "if (err) renderBlockingError(el, text, err);" in js       # every canvas write
    # every coded refusal has its own sentence; the mapping ones keep 8.5's
    for code in ("attempt_duplicate", "attempt_not_found", "attempt_line_break",
                 "attempt_table_missing", "attempt_unknown_field"):
        assert f"  {code}: \"" in html, code
    assert 'mapping_exists: "这条映射已存在"' in html
    assert 'human_link: "这是你的标注：请用「删除标注」移除"' in html


def test_served_brand_subtitle_is_yanjiu_mailuo(live_server):
    """8.8 (amended): the brand mark stays RCE; its subtitle becomes
    「研究脉络」 (the old English one named two of three views)."""
    html = _get_raw(live_server[0], "/")[1].decode("utf-8")
    assert '<span class="brand-sub">研究脉络</span>' in html
    assert "decision tree &amp; lineage" not in html


def test_canvas_js_carries_the_design_copy_in_product_language():
    """The canvas's binding copy (8.1-8.4, 8.7, 8.8), pinned so it is not
    casually reworded: type labels, the 8.1 socket names, the toolbar, the
    relayout confirm, and the state tags."""
    js = _CANVAS_JS_FILE.read_text(encoding="utf-8")
    for copy in (
        "数据集", "脚本", "图表",                          # 8.1 node types
        "来源", "数据", "读取", "写出", "生成", "生成自",   # 8.1 sockets
        "查找节点…", "适应全部", "100%", "＋", "－",        # 8.2 toolbar
        "重新排列", "将丢弃你在这个视图里摆放的位置",      # 8.4 relayout (as amended)
        "尚未入图", "文件不存在", "检测到循环", "全部",     # 8.1/8.2/8.4/8.7
    ):
        assert copy in js, f"missing product-language copy in canvas.js: {copy}"


def test_canvas_js_carries_the_editing_copy_in_product_language():
    """Phase 2b's binding copy (8.1 refusal, 8.3 popover and link actions,
    8.5 duplicate), pinned the same way -- and nothing on the canvas says
    node/edge/socket to the user (8.8): those words appear only in code."""
    js = _CANVAS_JS_FILE.read_text(encoding="utf-8")
    for copy in (
        "只能把数据集接到脚本的「读取」插口",             # 8.1 refusal
        "确认标注", "取消", "备注",                      # 8.3 popover
        "删除标注", "标记为错误提取", "撤销",              # 8.3 link actions
        "这条映射已存在", "无法标注",                     # 8.5 / 8.8 error framing
    ):
        assert copy in js, f"missing product-language copy in canvas.js: {copy}"


def test_canvas_js_writes_only_through_the_canvas_endpoints():
    """Every POST the canvas makes is one of the origin-checked endpoints
    the server routes -- no write path exists only in the page."""
    js = _CANVAS_JS_FILE.read_text(encoding="utf-8")
    posted = set(re.findall(r'apiPost\("(/api/[\w/]+)"', js))
    assert posted == {
        "/api/canvas/layout", "/api/open", "/api/mappings/add", "/api/mappings/delete",
        "/api/edges/reject", "/api/edges/restore",
        # V5 phase 7: the link card's judgments (9.3) and setting aside an
        # arrangement record that cannot be read (9.2).
        "/api/judgements", "/api/records/answer",
    }
    source = Path(server.__file__).read_text(encoding="utf-8")
    for path in posted:
        assert f'"{path}"' in source, f"canvas.js posts to {path}, which the server does not route"


def test_canvas_js_loads_nothing_external():
    """Zero external resources holds for the second served file too. The
    only URL-shaped string allowed is the SVG namespace, an identifier that
    `createElementNS` needs and that no browser ever fetches."""
    js = _CANVAS_JS_FILE.read_text(encoding="utf-8")
    without_ns = js.replace('"http://www.w3.org/2000/svg"', "")
    assert re.search(r"https?://", without_ns) is None
    assert "import(" not in js and "importScripts" not in js


def test_canvas_js_ships_as_package_data():
    """Like app.html, canvas.js must be in the wheel, not only in this
    editable checkout -- otherwise an installed `rce serve` 500s on it."""
    import tomllib
    pyproject = Path(server.__file__).resolve().parents[3] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert "webapp/canvas.js" in data["tool"]["setuptools"]["package-data"]["rce"]


def test_http_summary_endpoint(live_server):
    base_url, project = live_server
    status, payload = _get(base_url, "/api/summary")
    assert status == 200
    assert payload["project_root"] == str(project) and payload["pending"] == 0


def test_http_attempts_endpoint_empty_hint(live_server):
    status, payload = _get(live_server[0], "/api/attempts")
    assert status == 200
    assert payload == {"attempts": [], "hint": server._NO_ATTEMPTS_HINT}


def test_http_tree_endpoint_empty_hint(live_server):
    status, payload = _get(live_server[0], "/api/tree")
    assert status == 200 and payload["attempts"] == []


def test_http_lineage_endpoint_empty_report(live_server):
    status, payload = _get(live_server[0], "/api/lineage")
    assert status == 200 and payload["orphans"] == [] and payload["chains"] == []


def test_http_unknown_api_endpoint_returns_404(live_server):
    status, _ = _get(live_server[0], "/api/nope")
    assert status == 404


def test_http_unknown_path_returns_404(live_server):
    status, _ = _get(live_server[0], "/nope")
    assert status == 404


def test_http_file_missing_query_param_returns_400(live_server):
    status, _ = _get(live_server[0], "/api/file")
    assert status == 400


def test_http_file_normal_read(live_server):
    base_url, project = live_server
    (project / "hello.txt").write_text("hi there")
    status, payload = _get(base_url, "/api/file?path=hello.txt")
    assert status == 200 and payload["content"] == "hi there" and payload["truncated"] is False


def test_http_file_traversal_dotdot_returns_403(live_server):
    status, _ = _get(live_server[0], "/api/file?path=" + urllib.parse.quote("../../etc/passwd"))
    assert status == 403


def test_http_file_traversal_absolute_returns_403(live_server):
    status, _ = _get(live_server[0], "/api/file?path=" + urllib.parse.quote("/etc/passwd"))
    assert status == 403


def test_http_open_returns_501_on_non_macos(live_server, monkeypatch):
    monkeypatch.setattr(server, "_is_macos", lambda: False)
    status, payload = _post(live_server[0], "/api/open", {"path": "README.md"})
    assert status == 501 and "macOS" in payload["error"]


def test_http_open_calls_subprocess_with_validated_list_args(live_server, monkeypatch):
    base_url, project = live_server
    (project / "README.md").write_text("x")
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append((args, kw)))

    status, payload = _post(base_url, "/api/open", {"path": "README.md", "reveal": True})

    expected = str((project.resolve() / "README.md"))
    assert status == 200
    assert calls == [(["open", "-R", expected], {"check": False})]
    assert payload == {"opened": expected, "reveal": True}


def test_http_open_traversal_blocked_before_subprocess(live_server, monkeypatch):
    base_url, _ = live_server
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append((args, kw)))

    status, payload = _post(base_url, "/api/open", {"path": "../escape"})

    assert status == 403 and calls == []


def test_http_open_missing_path_key_returns_400(live_server):
    status, _ = _post(live_server[0], "/api/open", {})
    assert status == 400


# -- Cross-origin defense (security-review fix): Host/Origin checks -------------


def test_http_open_rejects_mismatched_host_header(live_server, monkeypatch):
    """DNS-rebinding shape: the request's `Host` names a domain other than
    this server's own `127.0.0.1:<port>` -- whatever that domain currently
    resolves to, a legitimate request to *this* server never carries it."""
    base_url, project = live_server
    (project / "f.txt").write_text("x")
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append((args, kw)))

    status, payload = _request_with_headers(
        base_url, "POST", "/api/open",
        {"Content-Type": "application/json", "Host": "attacker.example:1234"},
        body=json.dumps({"path": "f.txt"}).encode("utf-8"),
    )

    assert status == 403 and calls == []
    assert "Host" in payload["error"]


def test_http_open_rejects_foreign_origin_text_plain_simple_request(live_server, monkeypatch):
    """The exact drive-by shape the security review flagged: a 'simple'
    cross-origin POST (`Content-Type: text/plain`, so the browser sends it
    with no CORS preflight at all) whose `Host` is correctly this server's
    own address -- the browser really is talking to 127.0.0.1:<port> -- but
    whose `Origin` names the unrelated page that issued the `fetch()`. Must
    be rejected before `subprocess.run` ever runs."""
    base_url, project = live_server
    (project / "f.txt").write_text("x")
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append((args, kw)))

    status, payload = _request_with_headers(
        base_url, "POST", "/api/open",
        {"Content-Type": "text/plain", "Origin": "http://evil.example"},
        body=json.dumps({"path": "f.txt"}).encode("utf-8"),
    )

    assert status == 403 and calls == []
    assert "Origin" in payload["error"]


def test_http_open_allows_matching_origin_header(live_server, monkeypatch):
    """The defense must not be so strict it blocks the app's own same-origin
    fetches -- an `Origin` that actually matches this server is accepted."""
    base_url, project = live_server
    (project / "f.txt").write_text("x")
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append((args, kw)))

    status, _ = _request_with_headers(
        base_url, "POST", "/api/open",
        {"Content-Type": "application/json", "Origin": base_url},
        body=json.dumps({"path": "f.txt"}).encode("utf-8"),
    )

    assert status == 200 and len(calls) == 1


def test_http_allows_portless_loopback_origin(live_server, monkeypatch):
    """Safari serializes a same-origin POST's Origin to a non-default port
    WITHOUT the port -- literally `http://127.0.0.1` -- which the exact-match
    check used to reject, breaking every POST-backed button in the app for
    anyone whose default browser is Safari (observed live 2026-08-30 via the
    RCE.app launch flow). The portless loopback form proves the same thing
    the exact form does (a foreign/rebound page's Origin always names its own
    host), so it must be accepted."""
    base_url, project = live_server
    (project / "f.txt").write_text("x")
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append((args, kw)))

    status, _ = _request_with_headers(
        base_url, "POST", "/api/open",
        {"Content-Type": "application/json", "Origin": "http://127.0.0.1"},
        body=json.dumps({"path": "f.txt"}).encode("utf-8"),
    )

    assert status == 200 and len(calls) == 1


def test_http_portless_acceptance_does_not_widen_the_check(live_server):
    """The Safari accommodation admits exactly one extra literal value --
    every neighboring shape (wrong port, localhost spelling, https scheme,
    trailing slash) stays rejected."""
    base_url, _ = live_server
    port = int(base_url.rsplit(":", 1)[1])
    for origin in (
        f"http://127.0.0.1:{port + 1}",
        "http://localhost",
        f"http://localhost:{port}",
        "https://127.0.0.1",
        "http://127.0.0.1/",
    ):
        status, payload = _request_with_headers(
            base_url, "POST", "/api/open",
            {"Content-Type": "application/json", "Origin": origin},
            body=json.dumps({"path": "f.txt"}).encode("utf-8"),
        )
        assert status == 403 and "Origin" in payload["error"], origin


def test_http_summary_rejects_mismatched_host_header(live_server):
    """Defense in depth on GET too (module docstring): without this, DNS
    rebinding could make a foreign-looking `Origin`/`Host` pair pass the
    browser's own same-origin check for reading a GET response back into
    attacker JS, not just for POST's side effect."""
    base_url, _ = live_server
    status, _ = _request_with_headers(
        base_url, "GET", "/api/summary", {"Host": "attacker.example:1234"}
    )
    assert status == 403


# -- /api/projects + POST /api/projects/switch (task V3 phase 1) -----------------


def _registered_path(label: str) -> str:
    """The path string exactly as the registry stores it (resolved at
    registration time) -- switch requests must be string-equal to it, so
    tests read it back rather than re-deriving it from a tmp_path that may
    or may not already be fully resolved on this platform."""
    return next(e["path"] for e in registry.load() if e["label"] == label)


def test_http_projects_lists_registry_with_initialized_flags(live_server, fake_home, tmp_path):
    base_url, project = live_server
    registry.register(project)
    uninitialized = tmp_path / "empty-proj"
    uninitialized.mkdir()
    registry.register(uninitialized)

    status, payload = _get(base_url, "/api/projects")

    assert status == 200
    assert payload["current"] == str(project)
    by_label = {p["label"]: p for p in payload["projects"]}
    assert by_label["proj"]["initialized"] is True
    assert by_label["empty-proj"]["initialized"] is False
    # Most-recently-registered first -- the same order load() promises.
    assert [p["label"] for p in payload["projects"]] == ["empty-proj", "proj"]


def test_http_projects_empty_registry_still_reports_current(live_server, fake_home):
    base_url, project = live_server
    status, payload = _get(base_url, "/api/projects")
    assert status == 200
    assert payload["projects"] == [] and payload["current"] == str(project)
    assert payload["blocked"] is None and payload["current_id"].startswith("p-")


def test_http_switch_success_repoints_summary_at_new_root(live_server, fake_home, tmp_path):
    """The core switch contract end to end: after a valid switch, every
    subsequent request -- /api/summary here -- serves the new root."""
    base_url, project = live_server
    registry.register(project)
    other = tmp_path / "other"
    _init_project(other)
    registry.register(other)
    target = _registered_path("other")

    status, payload = _post(base_url, "/api/projects/switch", {"path": target})

    assert status == 200
    assert payload["current"] == target and payload["label"] == "other"
    assert payload["blocked"] is None and payload["project_id"].startswith("p-")
    status, summary = _get(base_url, "/api/summary")
    assert status == 200 and summary["project_root"] == target
    # A successful switch is a "serve" for recency purposes: the registry's
    # most-recent entry is now the switched-to project (cmd_serve without a
    # path would resume from it).
    assert registry.load()[0]["path"] == target


def test_http_switch_rejects_path_not_in_registry(live_server, fake_home, tmp_path):
    """Even a real, initialized project is refused if it was never
    registered -- the registry is the allow-list, and a request body can
    never introduce a new filesystem path to serve."""
    base_url, project = live_server
    registry.register(project)
    outside = tmp_path / "outside"
    _init_project(outside)  # initialized, but deliberately NOT registered

    status, payload = _post(base_url, "/api/projects/switch", {"path": str(outside.resolve())})

    assert status == 403 and "not a registered project" in payload["error"]
    _, summary = _get(base_url, "/api/summary")
    assert summary["project_root"] == str(project)  # still serving the old root


def test_http_switch_rejects_registered_but_uninitialized(live_server, fake_home, tmp_path):
    base_url, project = live_server
    registry.register(project)
    uninitialized = tmp_path / "empty-proj"
    uninitialized.mkdir()
    registry.register(uninitialized)

    status, payload = _post(
        base_url, "/api/projects/switch", {"path": _registered_path("empty-proj")}
    )

    assert status == 400 and "not initialized" in payload["error"]
    _, summary = _get(base_url, "/api/summary")
    assert summary["project_root"] == str(project)


def test_http_switch_missing_path_key_returns_400(live_server, fake_home):
    status, _ = _post(live_server[0], "/api/projects/switch", {})
    assert status == 400


def test_http_switch_rejects_foreign_origin_before_any_registry_check(live_server, fake_home, tmp_path):
    """The drive-by shape, aimed at the new mutating endpoint: a 'simple'
    cross-origin POST (text/plain, no CORS preflight) targeting a path that
    IS a valid registry member -- the origin check must reject it before
    the switch logic ever runs, or a hostile page could repoint the server
    among the user's own registered projects."""
    base_url, project = live_server
    registry.register(project)
    other = tmp_path / "other"
    _init_project(other)
    registry.register(other)

    status, payload = _request_with_headers(
        base_url, "POST", "/api/projects/switch",
        {"Content-Type": "text/plain", "Origin": "http://evil.example"},
        body=json.dumps({"path": _registered_path("other")}).encode("utf-8"),
    )

    assert status == 403 and "Origin" in payload["error"]
    _, summary = _get(base_url, "/api/summary")
    assert summary["project_root"] == str(project)  # switch never happened


def test_http_switch_rejects_mismatched_host_header(live_server, fake_home):
    base_url, _ = live_server
    status, _ = _request_with_headers(
        base_url, "POST", "/api/projects/switch",
        {"Content-Type": "application/json", "Host": "attacker.example:1234"},
        body=json.dumps({"path": "/whatever"}).encode("utf-8"),
    )
    assert status == 403


def test_http_projects_rejects_mismatched_host_header(live_server, fake_home):
    """The origin check runs before EVERY endpoint, the new read-only one
    included (module docstring's cross-origin defense)."""
    base_url, _ = live_server
    status, _ = _request_with_headers(
        base_url, "GET", "/api/projects", {"Host": "attacker.example:1234"}
    )
    assert status == 403


# -- GET /api/generation + auto-refresh watcher wiring (task V3 phase 2) --------


def _write_attempts_map(project: Path, rows: list[str]) -> None:
    """A real map.md matching `_write_attempts_config`'s file/heading, with
    `rows` as pre-formatted `| # | date | desc | vars | result | verdict |`
    lines -- just enough table for `rce.ingest.attempts` to parse."""
    header = (
        "## H\n\n"
        "| # | date | desc | vars | result | verdict |\n"
        "|---|------|------|------|--------|---------|\n"
    )
    (project / "map.md").write_text(header + "\n".join(rows) + "\n")


def test_http_generation_reports_watcher_status(live_server):
    """The endpoint's initial contract: generation starts at 1, nothing is
    refreshing, no error -- and it answers with no watcher thread running
    at all (build_server creates the watcher; only serve() starts it)."""
    status, payload = _get(live_server[0], "/api/generation")
    assert status == 200
    assert payload == {"generation": 1, "refreshing": False, "last_error": None}


def test_http_generation_rejects_mismatched_host_header(live_server):
    """_check_local_origin runs before EVERY endpoint, this one included."""
    base_url, _ = live_server
    status, _ = _request_with_headers(
        base_url, "GET", "/api/generation", {"Host": "attacker.example:1234"}
    )
    assert status == 403


def test_http_switch_bumps_generation(live_server, fake_home, tmp_path):
    """A project switch retargets the watcher and bumps the generation, so
    every open page's next /api/generation poll triggers a re-fetch of the
    new project's data."""
    base_url, project = live_server
    registry.register(project)
    other = tmp_path / "other"
    _init_project(other)
    registry.register(other)

    status, _ = _post(base_url, "/api/projects/switch", {"path": _registered_path("other")})
    assert status == 200

    status, payload = _get(base_url, "/api/generation")
    assert status == 200
    assert payload == {"generation": 2, "refreshing": False, "last_error": None}


def test_http_failed_switch_does_not_bump_generation(live_server, fake_home):
    """A rejected switch (unknown path here) must leave the watcher alone --
    retarget only runs after validation, so a drive-by rejection can never
    even make open pages re-fetch."""
    base_url, _ = live_server
    status, _ = _post(base_url, "/api/projects/switch", {"path": "/not/registered"})
    assert status == 403
    _, payload = _get(base_url, "/api/generation")
    assert payload["generation"] == 1


def test_http_tree_reflects_map_edit_after_watcher_poll(tmp_path):
    """The full auto-refresh loop at the HTTP surface, on the server's OWN
    watcher (the one RceHTTPServer constructed), driven deterministically
    via poll_once() instead of a sleeping thread: edit the map, poll, and
    /api/generation and /api/tree both serve the new state."""
    project = tmp_path / "proj"
    _init_project(project)
    _write_attempts_config(project)
    _write_attempts_map(project, ["| 1 | 2026-01-01 | first | v | r | ✅ |"])
    httpd = server.build_server(project, 0, watch_interval=0.01)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        httpd.watcher.poll_once()  # baseline

        _write_attempts_map(
            project,
            ["| 1 | 2026-01-01 | first | v | r | ✅ |", "| 2 | 2026-01-02 | second | v | r | 🕒 |"],
        )
        assert httpd.watcher.poll_once() is True

        status, payload = _get(base_url, "/api/generation")
        assert status == 200
        assert payload == {"generation": 2, "refreshing": False, "last_error": None}
        status, payload = _get(base_url, "/api/tree")
        assert status == 200
        assert [a["number"] for a in payload["attempts"]] == ["1", "2"]
        assert payload["attempts"][1]["description"] == "second"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# -- POST /api/attempts/preview + /api/attempts/write (task V3 phase 3) ---------


_ROW_1 = "| 1 | 2026-01-01 | first | v | r | ✅ |"
_APPEND_2 = {
    "op": "append",
    "number": "2",
    "fields": {"date": "2026-01-02", "description": "second", "verdict": "🕒"},
}


def _map_project(project: Path) -> None:
    """live_server's project plus just enough attempts config + map for the
    edit endpoints to have a real table to write into."""
    _write_attempts_config(project)
    _write_attempts_map(project, [_ROW_1])


def test_http_attempts_preview_returns_diff_without_writing(live_server):
    base_url, project = live_server
    _map_project(project)
    before = (project / "map.md").read_text()

    status, payload = _post(base_url, "/api/attempts/preview", _APPEND_2)

    assert status == 200
    assert payload["file"] == "map.md" and payload["old_row"] is None
    assert "| 2 |" in payload["new_row"] and "+| 2 |" in payload["diff"]
    assert (project / "map.md").read_text() == before  # a preview writes nothing
    assert not [b for b in (project / ".rce" / "backups").glob("*") if not b.name.startswith("project.toml.")]  # the identity keeps its own snapshots (9.12)


def test_http_attempts_write_appends_row_tree_reflects_it_and_backup_exists(live_server):
    """The whole write contract at the HTTP surface: the map file gains the
    row, /api/tree serves it immediately (the write path re-ingested on its
    own -- no watcher poll ran here), the original is backed up, and the
    generation moved so open pages re-fetch."""
    base_url, project = live_server
    _map_project(project)
    original = (project / "map.md").read_text()

    status, payload = _post(base_url, "/api/attempts/write", _APPEND_2)

    assert status == 200
    assert payload["ok"] is True and payload["ingest_error"] is None
    assert "| 2 | 2026-01-02 | second |" in (project / "map.md").read_text()
    assert (project / payload["backup"]).read_text() == original  # the pre-edit content

    status, tree = _get(base_url, "/api/tree")
    assert status == 200
    assert [a["number"] for a in tree["attempts"]] == ["1", "2"]

    status, generation = _get(base_url, "/api/generation")
    assert status == 200
    assert generation["generation"] == payload["generation"] == 2
    assert generation["last_error"] is None


def test_http_attempts_write_update_changes_the_row(live_server):
    base_url, project = live_server
    _map_project(project)

    status, payload = _post(
        base_url, "/api/attempts/write",
        {"op": "update", "number": "1", "fields": {"verdict": "☠️ 放弃"}},
    )

    assert status == 200 and payload["ok"] is True
    _, attempts_data = _get(base_url, "/api/attempts")
    assert attempts_data["attempts"][0]["verdict"] == "☠️ 放弃"
    assert attempts_data["attempts"][0]["attrs"]["description"] == "first"  # untouched cell


def test_http_attempts_write_duplicate_number_returns_400_and_writes_nothing(live_server):
    base_url, project = live_server
    _map_project(project)
    before = (project / "map.md").read_text()

    status, payload = _post(
        base_url, "/api/attempts/write", {"op": "append", "number": "1", "fields": {}}
    )

    assert status == 400 and "already exists" in payload["error"]
    assert (project / "map.md").read_text() == before
    assert not [b for b in (project / ".rce" / "backups").glob("*") if not b.name.startswith("project.toml.")]  # the identity keeps its own snapshots (9.12)  # refused before backing up


def test_http_attempts_write_unknown_number_update_returns_400(live_server):
    base_url, project = live_server
    _map_project(project)
    status, payload = _post(
        base_url, "/api/attempts/write",
        {"op": "update", "number": "99", "fields": {"verdict": "x"}},
    )
    assert status == 400 and "no row" in payload["error"]


@pytest.mark.parametrize("endpoint", ["/api/attempts/preview", "/api/attempts/write"])
def test_http_attempt_refusals_carry_their_code_as_state(live_server, endpoint):
    """DESIGN.md 8.8 "Errors": each refusal the attempt form can cause names
    its cause in `state` (the same channel `mapping_exists` uses) beside the
    unchanged English `error`, so the page picks a Chinese sentence without
    matching English."""
    base_url, project = live_server
    _map_project(project)
    cases = [
        ({"op": "append", "number": "1", "fields": {}}, "attempt_duplicate", "already exists"),
        ({"op": "update", "number": "99", "fields": {"verdict": "x"}}, "attempt_not_found", "no row"),
        ({"op": "update", "number": "1", "fields": {"result": "a\u2029b"}}, "attempt_line_break", "U+2029"),
        ({"op": "append", "number": "2", "fields": {"id": "3"}}, "attempt_unknown_field", "unknown field"),
    ]
    for body, state, english in cases:
        status, payload = _post(base_url, endpoint, body)
        assert status == 400 and payload["state"] == state and english in payload["error"]
    # a refusal outside the form's own cases stays uncoded
    status, payload = _post(base_url, endpoint, {"op": "update", "number": "1", "fields": {}})
    assert status == 400 and "state" not in payload
    # the table can no longer be found: heading gone, then the config gone
    (project / "map.md").write_text("# nothing here\n", encoding="utf-8")
    status, payload = _post(base_url, endpoint, _APPEND_2)
    assert status == 400 and payload["state"] == "attempt_table_missing"
    (project / ".rce" / "attempts.toml").unlink()
    status, payload = _post(base_url, endpoint, _APPEND_2)
    assert status == 400 and payload["state"] == "attempt_table_missing"


def test_http_attempts_write_malformed_op_returns_400(live_server):
    status, _ = _post(live_server[0], "/api/attempts/write", {"op": "delete", "number": "1"})
    assert status == 400


def test_http_attempts_preview_rejects_foreign_origin(live_server):
    """Origin-checked exactly like its mutating twin -- a drive-by page
    must not even get a diff of the user's own research log back."""
    base_url, project = live_server
    _map_project(project)
    status, payload = _request_with_headers(
        base_url, "POST", "/api/attempts/preview",
        {"Content-Type": "text/plain", "Origin": "http://evil.example"},
        body=json.dumps(_APPEND_2).encode("utf-8"),
    )
    assert status == 403 and "Origin" in payload["error"]


def test_http_attempts_write_rejects_foreign_origin_before_touching_the_file(live_server):
    """THE drive-by shape at the highest-stakes endpoint: a no-preflight
    cross-origin POST aimed at writing into the user's own map file. The
    origin check must reject it before mapedit ever runs -- no write, no
    backup, no generation bump."""
    base_url, project = live_server
    _map_project(project)
    before = (project / "map.md").read_text()

    status, payload = _request_with_headers(
        base_url, "POST", "/api/attempts/write",
        {"Content-Type": "text/plain", "Origin": "http://evil.example"},
        body=json.dumps(_APPEND_2).encode("utf-8"),
    )

    assert status == 403 and "Origin" in payload["error"]
    assert (project / "map.md").read_text() == before
    assert not [b for b in (project / ".rce" / "backups").glob("*") if not b.name.startswith("project.toml.")]  # the identity keeps its own snapshots (9.12)
    _, generation = _get(base_url, "/api/generation")
    assert generation["generation"] == 1


def test_http_attempts_write_rejects_mismatched_host_header(live_server):
    base_url, project = live_server
    _map_project(project)
    status, _ = _request_with_headers(
        base_url, "POST", "/api/attempts/write",
        {"Content-Type": "application/json", "Host": "attacker.example:1234"},
        body=json.dumps(_APPEND_2).encode("utf-8"),
    )
    assert status == 403
    assert "| 2 |" not in (project / "map.md").read_text()


def test_http_attempts_write_runs_under_the_watchers_ingest_lock(tmp_path):
    """Concurrency contract: the write path takes the SAME lock the
    watcher's poll ingests under. Hold that lock and a write request must
    block -- file untouched -- until it is released, then complete
    normally. (Deterministic in the failing direction: if the write used
    any other lock, it would finish while this one is still held.)"""
    project = tmp_path / "proj"
    _init_project(project)
    _map_project(project)
    httpd = server.build_server(project, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        results: list[tuple[int, Any]] = []
        writer = threading.Thread(
            target=lambda: results.append(_post(base_url, "/api/attempts/write", _APPEND_2)),
            daemon=True,
        )
        httpd.watcher.ingest_lock.acquire()
        try:
            writer.start()
            writer.join(timeout=0.5)
            assert writer.is_alive()  # blocked on the shared lock
            assert results == []
            assert "| 2 |" not in (project / "map.md").read_text()  # not even the write ran
        finally:
            httpd.watcher.ingest_lock.release()
        writer.join(timeout=10)
        assert not writer.is_alive()
        status, payload = results[0]
        assert status == 200 and payload["ok"] is True
        assert "| 2 |" in (project / "map.md").read_text()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_http_summary_echoes_attempts_config_columns(live_server):
    """The edit form is built from the project's own [columns] names via
    this echo -- never hardcoded column labels in the frontend."""
    base_url, project = live_server
    _map_project(project)
    status, payload = _get(base_url, "/api/summary")
    assert status == 200
    assert payload["attempts_config"]["file"] == "map.md"
    assert payload["attempts_config"]["columns"]["verdict"] == "verdict"


def test_http_summary_attempts_config_null_without_config(live_server):
    status, payload = _get(live_server[0], "/api/summary")
    assert status == 200 and payload["attempts_config"] is None


# -- POST /api/shutdown (task V3 phase 4) ----------------------------------------


def test_http_shutdown_actually_stops_serve_forever(tmp_path):
    """The endpoint's whole contract: respond {ok: true}, then the
    serve_forever loop exits -- observed as the real background thread
    finishing. Deliberately not the shared live_server fixture: this test's
    subject IS the teardown, so it owns the full lifecycle itself (the
    fixture's own later shutdown() would mask whether the endpoint did
    anything)."""
    project = tmp_path / "proj"
    _init_project(project)
    httpd = server.build_server(project, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, payload = _post(base_url, "/api/shutdown", {})
        assert status == 200 and payload == {"ok": True}
        thread.join(timeout=5)
        assert not thread.is_alive()  # serve_forever returned
    finally:
        httpd.shutdown()  # harmless if the endpoint already stopped the loop
        httpd.server_close()
        thread.join(timeout=5)


def test_http_shutdown_with_another_pid_keeps_serving(live_server):
    """RCE.app's quit names its own child's pid (adversarial review of the
    V4 work): an engine the researcher started from a terminal, which is
    what may actually hold the port, must answer 409 and keep serving
    (DESIGN.md 8.9: "left alone")."""
    base_url, _ = live_server
    status, payload = _post(base_url, "/api/shutdown", {"pid": os.getpid() + 1})
    assert status == 409 and "not shutting down" in payload["error"]
    assert _get(base_url, "/api/projects")[0] == 200
    assert _post(base_url, "/api/shutdown", {"pid": "1"})[0] == 400
    assert _post(base_url, "/api/shutdown", {"pid": True})[0] == 400
    assert _get(base_url, "/api/projects")[0] == 200


def test_http_shutdown_with_its_own_pid_stops(tmp_path):
    project = tmp_path / "proj"
    _init_project(project)
    httpd = server.build_server(project, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, payload = _post(base_url, "/api/shutdown", {"pid": os.getpid()})
        assert status == 200 and payload == {"ok": True}
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_http_shutdown_get_is_rejected_and_server_keeps_serving(live_server):
    """POST-only: a plain GET of the path (e.g. a link, a prefetch) must
    never stop the server -- it falls through to the ordinary
    unknown-endpoint 404, and the server demonstrably still answers."""
    base_url, _ = live_server
    status, payload = _get(base_url, "/api/shutdown")
    assert status == 404 and "error" in payload
    status, _ = _get(base_url, "/api/summary")
    assert status == 200  # still alive


def test_http_shutdown_rejects_foreign_origin_and_keeps_serving(live_server):
    """The drive-by shape again (module docstring's "Shutdown defense"): a
    no-preflight cross-origin POST must be 403'd by _check_local_origin
    before the shutdown thread is ever spawned -- killing the user's
    running app is a side effect like any other."""
    base_url, _ = live_server
    status, payload = _request_with_headers(
        base_url, "POST", "/api/shutdown",
        {"Content-Type": "text/plain", "Origin": "http://evil.example"},
        body=b"",
    )
    assert status == 403 and "Origin" in payload["error"]
    status, _ = _get(base_url, "/api/summary")
    assert status == 200  # the loop was never told to stop


def test_http_shutdown_rejects_mismatched_host_and_keeps_serving(live_server):
    base_url, _ = live_server
    status, payload = _request_with_headers(
        base_url, "POST", "/api/shutdown",
        {"Content-Type": "application/json", "Host": "attacker.example:1234"},
        body=b"",
    )
    assert status == 403 and "Host" in payload["error"]
    status, _ = _get(base_url, "/api/summary")
    assert status == 200


def test_serve_starts_watcher_and_server_close_stops_it(tmp_path, monkeypatch):
    """serve() is the one place the polling thread starts (build_server
    never does), and its finally-block server_close stops it -- observed
    from inside the (stubbed) serve_forever, where the thread must be
    alive, and after serve() returns, where it must be gone."""
    _init_project(tmp_path)
    observed: dict[str, Any] = {}

    def fake_serve_forever(self):
        observed["alive_during_serve"] = (
            self.watcher._thread is not None and self.watcher._thread.is_alive()
        )
        observed["httpd"] = self

    monkeypatch.setattr(server.RceHTTPServer, "serve_forever", fake_serve_forever)

    server.serve(tmp_path, port=0, open_browser=False)

    assert observed["alive_during_serve"] is True
    assert observed["httpd"].watcher._thread is None  # server_close stopped it


def test_build_server_does_not_start_watcher_thread(tmp_path):
    """Routing-only consumers (this file's own live_server fixture) must
    never pay for background polling -- only serve() starts the thread."""
    httpd = server.build_server(tmp_path, 0, watch_interval=0.01)
    try:
        assert httpd.watcher._thread is None
    finally:
        httpd.server_close()


# -- DESIGN.md section 8.10 rule 1: the graph lives outside the project -------


def test_require_db_resolves_outside_the_project(tmp_path):
    project = tmp_path / "proj"
    _init_project(project)
    assert server._require_db(project) == paths.graph_db_path(project)


def test_require_db_migrates_a_legacy_in_project_graph_on_first_touch(tmp_path):
    """"Migrated on first touch by any subcommand or the server" -- the
    server's touch is `_require_db`, and the rows must survive it."""
    project = tmp_path / "proj"
    project.mkdir()
    legacy = paths.legacy_graph_db_path(project)
    legacy.parent.mkdir(parents=True)
    conn = db.connect(legacy)
    try:
        db.migrate(conn)
        db.upsert_node(conn, "figure:old.png", "figure", title="old.png")
    finally:
        conn.close()

    resolved = server._require_db(project)

    assert resolved == paths.graph_db_path(project) and resolved.exists()
    assert not legacy.exists()
    conn = db.connect(resolved)
    try:
        assert db.get_node(conn, "figure:old.png") is not None
    finally:
        conn.close()


def test_require_db_reports_a_failed_migration_as_a_500_not_a_missing_project(tmp_path, monkeypatch):
    """A graph that could not be verified is a different fact from no graph
    at all, and must not be reported as "run rce init" -- that advice would
    invite the user to build an empty graph beside their real one."""
    project = tmp_path / "proj"
    project.mkdir()
    legacy = paths.legacy_graph_db_path(project)
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"not a database" * 100)

    with pytest.raises(server.GraphMigrationError) as excinfo:
        server._require_db(project)

    assert excinfo.value.status == 500
    assert legacy.exists()


def test_http_summary_reports_the_graphs_actual_location(live_server, tmp_path):
    base_url, project = live_server
    status, payload = _get(base_url, "/api/summary")
    assert status == 200
    assert payload["graph_path"] == str(paths.graph_db_path(project))


# -- rule 1: a cloud-evicted graph answers instead of blocking ----------------


def test_require_db_refuses_to_open_a_dataless_graph(tmp_path, monkeypatch):
    """`sqlite3.connect` on an iCloud-evicted file blocks for as long as the
    download takes and cannot be interrupted -- so the check happens before
    anyone opens it, and it raises rather than waits."""
    project = tmp_path / "proj"
    _init_project(project)
    monkeypatch.setattr(server.paths, "is_dataless", lambda path: Path(path).name == "graph.db")  # the graph, not the identity file, is evicted

    with pytest.raises(server.GraphDownloadingError) as excinfo:
        server._require_db(project)

    assert excinfo.value.status == 503
    assert excinfo.value.state == "graph_downloading"


def test_http_dataless_graph_returns_503_with_its_own_state(live_server, monkeypatch):
    base_url, _ = live_server
    monkeypatch.setattr(server.paths, "is_dataless", lambda path: Path(path).name == "graph.db")  # the graph, not the identity file, is evicted

    status, payload = _get(base_url, "/api/summary")

    assert status == 503
    assert payload["state"] == "graph_downloading"
    assert "iCloud" in payload["error"]


def test_dataless_check_is_not_asked_of_a_graph_that_is_not_there(tmp_path, monkeypatch):
    """Order matters: "missing" outranks "downloading", or a project that
    was never initialized would be reported as one that is still syncing."""
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(server.paths, "is_dataless", lambda path: True)

    with pytest.raises(server.ProjectNotInitializedError):
        server._require_db(project)


def _mk_legacy(project: Path) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    legacy = paths.legacy_graph_db_path(project)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(legacy)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    return legacy


def test_require_db_answers_downloading_for_a_dataless_legacy_graph(tmp_path, monkeypatch):
    """Rule 1 covers the migration too (adversarial review of the V4 work):
    the legacy graph is the file inside the iCloud-synced project, and the
    migration's own `sqlite3.connect` on it was the blocking open the rule
    forbids. It must become the same 503 header state, untouched."""
    project = tmp_path / "proj"
    legacy = _mk_legacy(project)
    monkeypatch.setattr(server.paths, "is_dataless", lambda path: Path(path) == legacy)
    monkeypatch.setattr(server.paths, "_request_download", lambda path: None)
    monkeypatch.setattr(
        server.paths, "_backup_database",
        lambda src, dst: (_ for _ in ()).throw(AssertionError("opened a dataless file")),
    )

    with pytest.raises(server.GraphDownloadingError) as excinfo:
        server._require_db(project)

    assert excinfo.value.state == "graph_downloading"
    assert legacy.exists() and not paths.graph_db_path(project).exists()


def test_switch_to_a_project_with_a_dataless_legacy_graph_does_not_block(live_server, fake_home, tmp_path, monkeypatch):
    """The switch succeeds at once (the project is real and registered);
    its first read then reports 「图谱文件正在从云端下载…」 instead of the
    handler thread hanging on the download."""
    base_url, project = live_server
    registry.register(project)
    legacy = _mk_legacy(tmp_path / "legacy")
    registry.register(tmp_path / "legacy")
    monkeypatch.setattr(server.paths, "is_dataless", lambda path: Path(path) == legacy)
    monkeypatch.setattr(server.paths, "_request_download", lambda path: None)

    status, _ = _post(base_url, "/api/projects/switch", {"path": _registered_path("legacy")})
    assert status == 200
    status, payload = _get(base_url, "/api/summary")
    assert status == 503 and payload["state"] == "graph_downloading"
    assert legacy.exists()

    monkeypatch.setattr(server.paths, "is_dataless", lambda path: False)  # the download landed
    status, _ = _get(base_url, "/api/summary")
    assert status == 200 and not legacy.exists()


def test_serve_starts_even_while_the_graph_is_downloading(tmp_path, monkeypatch):
    """A transient iCloud state must not become RCE.app's 「引擎没有在 10 秒内
    启动」: `serve` reports it on stderr and serves anyway."""
    project = tmp_path / "proj"
    _init_project(project)
    monkeypatch.setattr(server.paths, "is_dataless", lambda path: Path(path).name == "graph.db")  # the graph, not the identity file, is evicted
    started = []

    class _Stub:
        server_address = ("127.0.0.1", 1)

        class watcher:  # noqa: N801 -- mimics the attribute
            @staticmethod
            def start():
                started.append("watcher")

        def serve_forever(self):
            started.append("serving")

        def server_close(self):
            started.append("closed")

    monkeypatch.setattr(server, "build_server", lambda root, port, served=None: _Stub())
    server.serve(project, 0, open_browser=False)
    assert started == ["watcher", "serving", "closed"]


# -- rule 2: a vanished graph degrades, it does not deadlock ------------------


def test_http_vanished_graph_reports_a_state_the_page_can_render(live_server):
    base_url, project = live_server
    paths.graph_db_path(project).unlink()

    status, payload = _get(base_url, "/api/tree")

    assert status == 400
    assert payload["state"] == "graph_missing"
    assert "graph.db" in payload["error"]


def test_http_vanished_graph_leaves_the_project_switcher_usable(live_server, fake_home, tmp_path):
    """The whole point of rule 2: the reads fail, but the endpoints the
    switcher needs keep answering, so the researcher can move to another
    project instead of staring at a dead page."""
    base_url, project = live_server
    registry.register(project)
    other = tmp_path / "other"
    _init_project(other)
    registry.register(other)
    paths.graph_db_path(project).unlink()

    assert _get(base_url, "/api/summary")[0] == 400
    status, payload = _get(base_url, "/api/projects")
    assert status == 200 and len(payload["projects"]) == 2
    assert _get(base_url, "/api/generation")[0] == 200

    status, _ = _post(base_url, "/api/projects/switch", {"path": _registered_path("other")})
    assert status == 200
    assert _get(base_url, "/api/summary")[0] == 200  # recovered by switching


def test_ordinary_errors_carry_no_state_so_the_page_shows_its_error_box(live_server):
    """Only the two degraded-project conditions get a state; everything
    else stays a plain message, or the page would start rendering header
    states for unrelated failures."""
    status, payload = _get(live_server[0], "/api/file?path=nope.txt")
    assert status == 404 and "state" not in payload


# -- rule 3: a registry entry whose directory is gone -------------------------


def test_http_projects_marks_an_entry_whose_directory_is_gone(live_server, fake_home, tmp_path):
    base_url, project = live_server
    registry.register(project)
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    registry.register(doomed)
    doomed.rmdir()

    status, payload = _get(base_url, "/api/projects")

    assert status == 200
    by_label = {p["label"]: p for p in payload["projects"]}
    assert by_label["doomed"]["available"] is False
    assert by_label["proj"]["available"] is True


def test_http_projects_available_and_initialized_are_separate_facts(live_server, fake_home, tmp_path):
    """A directory that exists but was never `rce init`ed is available and
    uninitialized -- greyed out in the switcher, but NOT offered for
    removal: nothing about it is dead."""
    base_url, project = live_server
    uninitialized = tmp_path / "empty-proj"
    uninitialized.mkdir()
    registry.register(uninitialized)

    _, payload = _get(base_url, "/api/projects")

    entry = next(p for p in payload["projects"] if p["label"] == "empty-proj")
    assert entry["available"] is True and entry["initialized"] is False


def test_http_projects_remove_drops_the_entry(live_server, fake_home, tmp_path):
    base_url, project = live_server
    registry.register(project)
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    registry.register(doomed)
    target = _registered_path("doomed")
    doomed.rmdir()

    status, payload = _post(base_url, "/api/projects/remove", {"path": target})

    assert status == 200 and payload == {"removed": target}
    _, listing = _get(base_url, "/api/projects")
    assert [p["label"] for p in listing["projects"]] == ["proj"]


def test_http_projects_remove_deletes_nothing_on_disk(live_server, fake_home, tmp_path):
    """It removes a bookmark. The project and its graph must survive, or a
    mis-click would cost the researcher their graph."""
    base_url, project = live_server
    registry.register(project)

    status, _ = _post(base_url, "/api/projects/remove", {"path": _registered_path("proj")})

    assert status == 200
    assert project.is_dir() and paths.graph_db_path(project).exists()


def test_http_projects_remove_rejects_a_path_not_in_the_registry(live_server, fake_home, tmp_path):
    """Same allow-list rule as /switch: a request body can never introduce
    a filesystem path the user did not register."""
    base_url, project = live_server
    registry.register(project)
    outside = tmp_path / "outside"
    outside.mkdir()

    status, payload = _post(base_url, "/api/projects/remove", {"path": str(outside.resolve())})

    assert status == 403 and "not a registered project" in payload["error"]
    assert len(registry.load()) == 1


def test_http_projects_remove_matches_by_string_equality_only(live_server, fake_home, tmp_path):
    """No resolution, no normalization of the client-supplied value -- the
    same defense /switch relies on."""
    base_url, project = live_server
    registry.register(project)
    stored = _registered_path("proj")

    status, _ = _post(base_url, "/api/projects/remove", {"path": stored + "/"})

    assert status == 403
    assert len(registry.load()) == 1


def test_http_projects_remove_missing_path_key_returns_400(live_server, fake_home):
    status, _ = _post(live_server[0], "/api/projects/remove", {})
    assert status == 400


def test_http_projects_remove_rejects_a_foreign_origin_before_touching_the_registry(
    live_server, fake_home
):
    """The drive-by shape aimed at the newest mutating endpoint: this one
    writes ~/.rce/projects.json, which IS the allow-list /switch validates
    against, so a hostile page must not be able to edit it."""
    base_url, project = live_server
    registry.register(project)
    stored = _registered_path("proj")

    status, payload = _request_with_headers(
        base_url, "POST", "/api/projects/remove",
        {
            "Host": urllib.parse.urlsplit(base_url).netloc,
            "Origin": "http://evil.example",
            "Content-Type": "text/plain",
        },
        body=json.dumps({"path": stored}).encode("utf-8"),
    )

    assert status == 403 and "Origin" in payload["error"]
    assert len(registry.load()) == 1  # nothing was removed


def test_get_of_the_remove_endpoint_is_a_plain_404(live_server, fake_home):
    """POST-only, like every other side effect here: a GET falls through to
    the ordinary unknown-endpoint 404 rather than acting."""
    status, _ = _get(live_server[0], "/api/projects/remove")
    assert status == 404


def test_switch_migrates_a_legacy_graph_in_the_project_switched_to(live_server, fake_home, tmp_path):
    """A switch is the server's first touch of the new project, and the
    write path never goes through `_require_db` -- so the migration has to
    happen on the switch itself, not on the first read after it."""
    base_url, project = live_server
    registry.register(project)
    legacy_project = tmp_path / "legacy"
    legacy_project.mkdir()
    legacy = paths.legacy_graph_db_path(legacy_project)
    legacy.parent.mkdir(parents=True)
    conn = db.connect(legacy)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    registry.register(legacy_project)

    status, _ = _post(base_url, "/api/projects/switch", {"path": _registered_path("legacy")})

    assert status == 200
    assert not legacy.exists()
    assert paths.graph_db_path(legacy_project).exists()


# -- Native shell bridge (DESIGN.md section 8.9, task V4 phase 3) ------------

_SHELL_SWIFT_FILE = Path(server.__file__).parent / "shell" / "RCEShell.swift"


def _page_shell_commands(html: str) -> set[str]:
    block = html[html.index("const SHELL_COMMANDS = {"):]
    block = block[: block.index("\n};")]
    return set(re.findall(r'^\s*"([a-z-]+)":', block, re.M))


def test_served_page_exposes_the_shell_dispatcher_and_title_message(live_server):
    """The page's half of the bridge is in what the server actually serves:
    one frozen window.RCE with a `command` entry point, and the one message
    shape the shell accepts, posted from loadProjects (load and switch)."""
    _, body = _get_raw(live_server[0], "/")
    html = body.decode("utf-8")
    assert "window.RCE = Object.freeze({" in html and "command(name)" in html
    assert 'window.webkit.messageHandlers.rce' in html
    assert 'postMessage({ type: "title", text:' in html
    load_projects = html[html.index("async function loadProjects()"):html.index("function updateSwitcherVisibility()")]
    assert load_projects.count("postShellTitle();") == 2  # success and failure paths alike


def test_page_dispatcher_and_shell_whitelist_name_the_same_commands():
    """The shell's compiled-in whitelist (Swift) and the page's command
    table must agree exactly: a name only one side knows is a menu item
    that silently does nothing, or a page command no menu can reach."""
    html = server._APP_HTML_PATH.read_text(encoding="utf-8")
    swift = _SHELL_SWIFT_FILE.read_text(encoding="utf-8")
    whitelist_src = swift[swift.index("let shellCommands: Set<String> = ["):]
    whitelist_src = whitelist_src[: whitelist_src.index("]")]
    swift_names = set(re.findall(r'"([a-z-]+)"', whitelist_src))
    expected = {
        "tree", "lineage", "canvas", "variables", "new-attempt", "reload", "zoom-in", "zoom-out",
        "zoom-reset", "fit", "reveal-project", "open-map",
    }
    assert _page_shell_commands(html) == expected
    assert swift_names == expected


def test_page_finder_commands_use_only_the_existing_open_endpoint():
    """reveal-project / open-map add no endpoint: the project root is "."
    and the map file is the server's own summary echo, both through the
    origin-checked, root-confined POST /api/open."""
    html = server._APP_HTML_PATH.read_text(encoding="utf-8")
    bridge = html[html.index("// -- Native shell bridge"):html.index("// -- Init ---")]
    assert set(re.findall(r'apiPost\("(/api/[\w/]+)"', bridge)) == {"/api/open"}
    assert 'shellOpen(".", true,' in bridge
    assert 'apiGet("/api/summary")' in bridge


def test_http_open_dot_reveals_the_project_root_itself(live_server, monkeypatch):
    """The 在 Finder 中显示项目 command's request: "." resolves to the served
    root, which the confinement check accepts (it is not outside itself)."""
    base_url, project = live_server
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append((args, kw)))

    status, payload = _post(base_url, "/api/open", {"path": ".", "reveal": True})

    assert status == 200
    assert calls == [(["open", "-R", str(project.resolve())], {"check": False})]


def test_http_open_from_the_shell_with_portless_origin_is_accepted(live_server, monkeypatch):
    """WebKit's same-origin POST carries the portless Origin (8.9 "Origin"):
    the shell adds no new origin shape, and the existing check accepts it."""
    base_url, _ = live_server
    port = urllib.parse.urlsplit(base_url).port
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: None)
    status, _ = _request_with_headers(
        base_url, "POST", "/api/open",
        {"Origin": "http://127.0.0.1", "Host": f"127.0.0.1:{port}", "Content-Type": "application/json"},
        json.dumps({"path": ".", "reveal": True}).encode("utf-8"),
    )
    assert status == 200
