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
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from rce import cli, db, lineage
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
    rce_dir = project_root / ".rce"
    rce_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(rce_dir / "graph.db")
    try:
        db.migrate(conn)
    finally:
        conn.close()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway HOME so registry-touching tests (the /api/projects
    endpoints, cmd_serve's registration) never read or write the user's
    real ~/.rce/projects.json -- `registry.registry_path()` resolves
    `Path.home()` per call precisely to honor this monkeypatch."""
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
    assert reads == [{"path": "data/in.csv", "role": "has_generator", "missing": False}]


def test_tree_payload_tags_orphan_input_when_no_writer_anywhere(conn, tmp_path):
    _write_attempts_config(tmp_path, steps_dir="steps")
    _mk_attempt(conn, "map.md", "1", step_files=["1-run.py"])
    _mk_edge(conn, "steps/1-run.py", "data/in.csv", "dataset", "reads", missing=True)

    reads = server.tree_payload(conn, tmp_path)["attempts"][0]["scripts"][0]["reads"]
    assert reads == [{"path": "data/in.csv", "role": "orphan_input", "missing": True}]


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
        lambda root, port, open_browser=True: calls.append((root, port, open_browser)),
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
        'data-view="tree"', 'data-view="lineage"',
    ):
        assert mount_point in html, f"missing mount point in served app.html: {mount_point}"


def test_http_root_has_zero_external_resources(live_server):
    """task V2 requirement: the app must be fully self-contained (no CDN, no
    external stylesheet/script/image/font) so it works entirely offline --
    assert no http(s):// URL appears anywhere in the served page at all."""
    base_url, _ = live_server
    _, body = _get_raw(base_url, "/")
    html = body.decode("utf-8")
    assert re.search(r"https?://", html) is None


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
    assert payload == {"projects": [], "current": str(project)}


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
    assert payload == {"current": target, "label": "other"}
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
    assert not (project / ".rce" / "backups").exists()


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
    assert not (project / ".rce" / "backups").exists()  # refused before backing up


def test_http_attempts_write_unknown_number_update_returns_400(live_server):
    base_url, project = live_server
    _map_project(project)
    status, payload = _post(
        base_url, "/api/attempts/write",
        {"op": "update", "number": "99", "fields": {"verdict": "x"}},
    )
    assert status == 400 and "no row" in payload["error"]


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
    assert not (project / ".rce" / "backups").exists()
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
