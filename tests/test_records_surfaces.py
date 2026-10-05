"""Every surface writes a human judgment through the one path, and every
reader shows what the ledger implies (DESIGN.md 9.1, 9.3, 9.6, 9.8; task V5
phase 4): the watcher, the HTTP endpoints, the CLI's `confirm` / `review` /
`records`."""

from __future__ import annotations

import http.client
import json
import threading
import urllib.parse

import pytest

from rce import cli, db
from rce.records import judgements
from rce.records import ledger as ledger_mod
from rce.webapp import canvas, server
from rce.webapp import watcher as project_watcher

from test_records_judgements import READ, WRITE, _conn, _entries, _ledger_bytes, _project, _state, _status

BODY = dict(zip(("src", "dst", "type", "extractor"), READ))


# -- the watcher -----------------------------------------------------------------------


def _watcher(root):
    return project_watcher.ProjectWatcher(lambda: root, interval=0.01)


def test_watcher_applies_a_ledger_edited_by_hand(tmp_path):
    """9.1: the ledger joins the watch set; a change to it (another engine,
    a `git pull`, a hand edit) is applied on the next poll."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="cli")
    w = _watcher(root)
    w.poll_once()  # baseline (and first-sight application)
    path = ledger_mod.judgements_path(root)
    first = path.read_text(encoding="utf-8")
    path.write_text(first.replace('verdict = "confirmed"', 'verdict = "rejected"'), encoding="utf-8")
    assert w.poll_once() is True
    assert _status(root, READ) == "rejected"
    assert "records" not in w.status_payload()


def test_watcher_applies_the_ledger_on_first_sight(tmp_path):
    """9.1 / 8.12: a judgment written while no engine was watching reaches
    the index on the first poll that sees the project."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="cli")
    conn = _conn(root)
    try:
        db.write_judgement_state(conn, statuses={READ: "auto"}, states={}, applied=None)  # an index that lags
    finally:
        conn.close()
    w = _watcher(root)
    w.poll_once()
    assert _status(root, READ) == "confirmed"


def test_watcher_reports_an_untrusted_ledger_and_keeps_polling(tmp_path):
    """9.3: a ledger RCE cannot trust is reported in the status payload; the
    index keeps what it had and the watcher goes on."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="cli")
    w = _watcher(root)
    w.poll_once()
    path = ledger_mod.judgements_path(root)
    path.write_bytes(path.read_bytes() + b"\n[[judgement]\n")
    assert w.poll_once() is True
    records = w.status_payload()["records"]
    assert records["state"] == "refuse_writes" and records["message"] == "判断记录文件当前无法读取，请先恢复它"
    assert _status(root, READ) == "confirmed"
    assert w.poll_once() is False  # still polling, nothing new


def test_watcher_reacts_to_the_arrangement_record_without_ingesting(tmp_path):
    root, _ = _project(tmp_path)
    w = _watcher(root)
    w.poll_once()
    before = w.status_payload()["generation"]
    path = canvas.canvas_record_path(root)
    path.write_text('{"views": {}}', encoding="utf-8")
    assert w.poll_once() is True
    assert w.status_payload()["generation"] == before + 1


# -- HTTP ---------------------------------------------------------------------------------


@pytest.fixture
def live(tmp_path):
    root, _ = _project(tmp_path)
    httpd = server.build_server(root, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", root, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call(base_url, method, path, body=None):
    parsed = urllib.parse.urlsplit(base_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
    data = None if body is None else json.dumps(body).encode("utf-8")
    try:
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)
    finally:
        conn.close()


def test_http_judgements_confirm_withdraw_and_history(live):
    base, root, _ = live
    status, payload = _call(base, "POST", "/api/judgements", {**BODY, "verdict": "confirmed", "note": "看过"})
    assert status == 200 and payload["status"] == "confirmed" and payload["link"]["status"] == "confirmed"
    assert payload["entry"]["note"] == "看过" and payload["entry"]["via"] == "canvas"
    status, payload = _call(base, "POST", "/api/judgements", {**BODY, "verdict": "withdrawn"})
    assert status == 200 and payload["status"] == "auto"
    query = urllib.parse.urlencode(BODY)
    status, payload = _call(base, "GET", f"/api/history?{query}")
    assert status == 200 and [e["verdict"] for e in payload["entries"]] == ["confirmed", "withdrawn"]
    status, payload = _call(base, "POST", "/api/judgements", {**BODY, "verdict": "pending"})
    assert status == 400


def test_http_judgements_refuse_a_mapping_and_an_unknown_link(live):
    base, root, _ = live
    status, payload = _call(base, "POST", "/api/judgements", {**BODY, "extractor": "mapping", "verdict": "rejected"})
    assert (status, payload["state"]) == (400, "human_link")
    status, _ = _call(base, "POST", "/api/judgements", {**BODY, "dst": "dataset:nope.csv", "verdict": "rejected"})
    assert status == 404
    assert _ledger_bytes(root) is None


def test_http_review_lists_the_item_and_the_summary_counts_it(live):
    """9.6 "Where it shows": the header counts 待复核, the list has the
    reason, the canvas marks the link, 待确认 does not count it."""
    base, root, _ = live
    _call(base, "POST", "/api/edges/reject", BODY)
    (root / "s.py").write_text("import pandas as pd\ndf = pd.read_json('data/in.csv')\ndf.to_csv('data/out.csv')\n")
    from test_records_judgements import _scan

    _scan(root)
    status, review = _call(base, "GET", "/api/review")
    assert status == 200 and review["count"] == 1
    assert review["review"][0]["label"] == "依据已变化" and review["review"][0]["verdict"] == "rejected"
    status, summary = _call(base, "GET", "/api/summary")
    assert summary["review"] == 1 and summary["pending"] == 0
    status, view = _call(base, "GET", "/api/canvas?scope=all")
    (link,) = [l for l in view["links"] if (l["src"], l["dst"]) == READ[:2]]
    assert link["review"] is True and link["status"] == "auto" and link["judgement"]["label"] == "依据已变化"
    status, tree = _call(base, "GET", "/api/lineage")
    assert status == 200


def test_http_scenario_11_a_click_writes_nothing_and_the_page_is_told(live):
    """9.9 #11 through the app: an unreadable ledger -- the click on
    reject is refused with the record's state, nothing is written."""
    base, root, _ = live
    _call(base, "POST", "/api/judgements", {**BODY, "verdict": "confirmed"})
    path = ledger_mod.judgements_path(root)
    path.write_bytes(path.read_bytes() + b"\nnot = [toml\n")
    before = path.read_bytes()
    status, payload = _call(base, "POST", "/api/edges/reject", BODY)
    assert (status, payload["state"]) == (409, "record_untrusted")
    assert payload["message"] == "判断记录文件当前无法读取，请先恢复它"
    assert path.read_bytes() == before
    assert _status(root, READ) == "confirmed"


def test_http_shrunk_ledger_asks_and_the_answer_restores(live):
    """9.3 through the app: a zero-byte ledger is the question, and
    「把缺少的补回文件」 answers it."""
    base, root, _ = live
    _call(base, "POST", "/api/judgements", {**BODY, "verdict": "confirmed"})
    ledger_mod.judgements_path(root).write_bytes(b"")
    status, payload = _call(base, "POST", "/api/edges/reject", BODY)
    assert (status, payload["state"]) == (409, "record_shrunk")
    assert payload["message"] == "记录文件比图谱少了 1 条判断" and len(payload["records"]["missing"]) == 1
    status, payload = _call(base, "POST", "/api/records/answer", {"file": "judgements", "answer": "restore"})
    assert status == 200 and payload["appended"] == 1
    assert [e.get("via") for e in _entries(root)] == ["recovered"]
    status, _ = _call(base, "POST", "/api/records/answer", {"file": "judgements", "answer": "file"})
    assert status == 409  # no question any more


def test_http_corrupt_arrangement_refuses_layout_writes_until_set_aside(live):
    base, root, _ = live
    path = canvas.canvas_record_path(root)
    path.write_text("{broken", encoding="utf-8")
    status, view = _call(base, "GET", "/api/canvas?scope=all")
    assert view["layout"]["state"] == "invalid" and view["positions"] == {}
    card = view["nodes"][0]["id"]
    body = {"project": view["project"], "scope": "all", "positions": {card: [1, 2]}}
    status, payload = _call(base, "POST", "/api/canvas/layout", body)
    assert (status, payload["state"]) == (409, "layout_unreadable")
    assert path.read_text(encoding="utf-8") == "{broken"
    status, payload = _call(base, "POST", "/api/records/answer", {"file": "canvas", "answer": "set_aside"})
    assert status == 200 and payload["moved_to"].startswith(".rce/backups/")
    status, _ = _call(base, "POST", "/api/canvas/layout", body)
    assert status == 200


# -- CLI -------------------------------------------------------------------------------------


def test_cli_confirm_with_note_withdraw_and_undo(tmp_path, capsys):
    root, _ = _project(tmp_path)
    args = [*READ, "--path", str(root)]
    assert cli.main(["confirm", *args, "--status", "rejected", "--note", "旧面板"]) == 0
    assert "auto -> rejected" in capsys.readouterr().out
    assert cli.main(["confirm", *args, "--status", "undone"]) == 0
    assert _status(root, READ) == "auto"
    assert cli.main(["confirm", *args, "--status", "confirmed"]) == 0
    assert cli.main(["confirm", *args, "--status", "withdrawn"]) == 0
    assert _status(root, READ) == "auto"
    entries = _entries(root)
    assert [e.get("verdict") for e in entries] == ["rejected", "undone", "confirmed", "withdrawn"]
    assert entries[0].get("note") == "旧面板" and all(e.get("via") == "cli" for e in entries)


def test_cli_confirm_refuses_a_hand_drawn_link(tmp_path, capsys):
    root, _ = _project(tmp_path)
    assert cli.main(["confirm", *READ[:3], "mapping", "--status", "rejected", "--path", str(root)]) == 1
    assert "mappings.toml" in capsys.readouterr().err
    assert _ledger_bytes(root) is None


def test_cli_review_and_records_verify(tmp_path, capsys):
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="cli", note="看过")
    (root / "s.py").write_text("import pandas as pd\ndf = pd.DataFrame()\ndf.to_csv('data/out.csv')\n")
    assert cli.main(["ingest", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["review", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Under review: 1" in out and "not_produced" in out and "note: 看过" in out
    assert cli.main(["status", "--path", str(root)]) == 0
    assert "Judgments under review: 1" in capsys.readouterr().out
    assert cli.main(["query", "script:s.py", "--path", str(root)]) == 0
    assert "under review: not_produced" in capsys.readouterr().out
    assert cli.main(["records", "--verify", str(root)]) == 0
    out = capsys.readouterr().out
    assert "judgements.toml -- 1 entr" in out and "Verify: the index's human state is what the record implies" in out
    # An index that drifted from the record fails verification.
    conn = _conn(root)
    try:
        db.write_judgement_state(conn, statuses={READ: "confirmed"}, states={}, applied=None)
    finally:
        conn.close()
    assert cli.main(["records", "--verify", str(root)]) == 1
    assert "the record implies" in capsys.readouterr().out


def test_cli_records_answer_file(tmp_path, capsys):
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="cli")
    judgements.judge(root, WRITE, "rejected", via="cli")
    path = ledger_mod.judgements_path(root)
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: text.rindex("\n[[judgement]]")] + "\n", encoding="utf-8")
    assert cli.main(["records", str(root)]) == 0
    assert "shrunk" in capsys.readouterr().out
    assert cli.main(["records", "--answer", "file", str(root)]) == 0
    assert "1 entr(y/ies) dropped" in capsys.readouterr().out
    assert _status(root, WRITE) == "auto" and _status(root, READ) == "confirmed"
    assert _state(root, READ)["outcome"] == "applied"


def test_readers_mark_a_link_under_review(tmp_path):
    """9.6 "Where it shows": 血缘 (the lineage report), MCP's status, the
    canvas -- a link under review is marked and never shown as applied."""
    from rce import lineage, mcp_server

    from test_records_judgements import _scan

    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "rejected", via="cli")
    (root / "s.py").write_text("import pandas as pd\ndf = pd.read_json('data/in.csv')\ndf.to_csv('data/out.csv')\n")
    _scan(root)
    conn = _conn(root)
    try:
        report = lineage.build_lineage_report(conn, root)
        summary = mcp_server.status_summary(conn)
        view = canvas.build_canvas(conn, root, "all")
    finally:
        conn.close()
    readers = [r for block in report["chains"] + report["orphans"] for r in block["readers"]]
    assert readers and all(r.get("review") is True for r in readers if r["script"] == "s.py")
    assert summary["review"] == 1
    assert "under review (see `rce review`): 1" in mcp_server.format_status_text(summary).lower()
    (link,) = [l for l in view["links"] if (l["src"], l["dst"], l["type"]) == READ[:3]]
    assert link["review"] and link["status"] == "auto"
