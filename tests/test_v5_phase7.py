"""The app's surfaces for the human record (DESIGN.md 9.3, 9.4, 9.5, 9.6
"Where it shows"; task V5 phase 7): the endpoints the page added (GET
/api/records, POST /api/project/reopen, a note on 标记为错误提取, the
history's basis, the link keys behind the 「待复核」 tags, the holders of a
refused retirement), the served copy, and the page's pure wording -- run
against the REAL app.html / canvas.js under node, as the canvas tests do."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import threading
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from rce import inventory, lineage, migration
from rce.records import judgements
from rce.records import ledger as ledger_mod
from rce.webapp import registry, server

from test_records_judgements import READ, _conn, _project, _scan

APP_HTML = Path(server.__file__).parent / "app.html"
CANVAS_JS = Path(server.__file__).parent / "canvas.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

BODY = dict(zip(("src", "dst", "type", "extractor"), READ))


# -- HTTP plumbing ------------------------------------------------------------------------


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


def _call(base_url: str, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None):
    parsed = urllib.parse.urlsplit(base_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
    data = None if body is None else json.dumps(body).encode("utf-8")
    try:
        conn.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return resp.status, raw.decode("utf-8")
    finally:
        conn.close()


def _ledger_text(root: Path) -> str | None:
    path = ledger_mod.judgements_path(root)
    return path.read_text(encoding="utf-8") if path.exists() else None


# -- the endpoints this phase added ----------------------------------------------------------


def test_scenario_12_history_shows_the_basis_each_verdict_was_made_on(live):
    """9.9 #12 in the app: the card's history is every entry, in order,
    each verdict with the basis it rested on (what was confirmed, not
    merely that it was), the note, and which ones an undo cancelled."""
    base, root, _ = live
    for verdict, note in (("confirmed", None), ("rejected", None), ("undone", None), ("confirmed", "看过"), ("withdrawn", None)):
        body = {**BODY, "verdict": verdict, **({"note": note} if note else {})}
        assert _call(base, "POST", "/api/judgements", body)[0] == 200
    status, payload = _call(base, "GET", "/api/history?" + urllib.parse.urlencode(BODY))
    assert status == 200 and payload["readable"] is True
    entries = payload["entries"]
    assert [e["verdict"] for e in entries] == ["confirmed", "rejected", "undone", "confirmed", "withdrawn"]
    assert entries[0]["basis"] == {"calls": ["read_csv"]} and entries[0]["basis_recorded"] == "at-judgment"
    assert entries[1]["cancelled"] is True and entries[0]["cancelled"] is False
    assert entries[2]["basis"] is None and entries[4]["basis"] is None  # an undo, a withdrawal rest on nothing
    assert entries[3]["note"] == "看过"


def test_reject_from_the_card_carries_its_note(live):
    """9.3: the card's optional 备注 goes with 标记为错误提取 -- into the
    ledger entry, through the same one write path."""
    base, root, _ = live
    status, payload = _call(base, "POST", "/api/edges/reject", {**BODY, "note": "读的是旧版面板"})
    assert status == 200 and payload["status"] == "rejected"
    assert payload["entry"]["note"] == "读的是旧版面板"
    assert 'note = "读的是旧版面板"' in _ledger_text(root)
    assert _call(base, "POST", "/api/edges/reject", {**BODY, "note": 5})[0] == 400


def test_records_endpoint_lists_each_kind_with_a_code_for_the_page(live):
    """9.2 「记录」: the inventory `rce records` prints, each row named by a
    stable code with its numbers, so the page words it in Chinese."""
    base, root, _ = live
    _call(base, "POST", "/api/judgements", {**BODY, "verdict": "confirmed"})
    status, payload = _call(base, "GET", "/api/records")
    assert status == 200 and payload["project_root"] == str(root)
    rows = {r["code"]: r for r in payload["rows"]}
    assert set(rows) >= {"judgements", "mappings", "attempts", "attempts_config", "canvas", "variables"}
    j = rows["judgements"]
    assert j["path"] == ".rce/judgements.toml" and j["facts"]["entries"] == 1 and j["facts"]["standing"] == 1
    assert j["facts"]["exists"] is True and j["facts"]["issues"] == []
    assert rows["mappings"]["facts"]["state"] == "absent"
    assert rows["canvas"]["facts"]["state"] == "absent"


def test_records_endpoint_names_a_problem_in_a_code_and_keeps_the_english(live):
    """A ledger RCE cannot read is listed with its issue (`invalid`, the
    line) for the page's sentence, and the engine's English for 「详情」."""
    base, root, _ = live
    _call(base, "POST", "/api/judgements", {**BODY, "verdict": "confirmed"})
    path = ledger_mod.judgements_path(root)
    path.write_bytes(path.read_bytes() + b"\n[[judgement]\n")
    status, payload = _call(base, "GET", "/api/records")
    j = next(r for r in payload["rows"] if r["code"] == "judgements")
    assert "invalid" in j["facts"]["issues"] and j["problems"]


def test_records_endpoint_answers_for_a_blocked_project_from_its_files(tmp_path):
    """A folder in a question (a copy) is listed from its own files: the
    inventory reads only and needs no index."""
    root, _ = _project(tmp_path)
    blocked = {"situation": "copy", "answers": ["fork"], "blocked": True}
    payload = server.records_payload(server.ServedProject(root, None, blocked=blocked))
    assert {r["code"] for r in payload["rows"]} >= {"judgements", "canvas"}


def test_scenario_1_move_while_open_then_reopen_and_choose_the_new_place(live, tmp_path):
    """9.9 #1 from the app: the folder is moved in Finder under a running
    engine; a judgment writes nothing at the old path and the page is told
    project_moved; 「重新打开」 finds the folder gone (「找不到项目文件夹」,
    answer `locate`); 「选择新位置…」 with a folder that is not this project
    attaches nothing; with the moved folder the project opens again, all
    records present, the index directory unchanged."""
    base, root, httpd = live
    registry.register(root, httpd.get_served().project_id)  # as `rce serve` does
    _call(base, "POST", "/api/judgements", {**BODY, "verdict": "confirmed"})
    before = _ledger_text(root)
    moved = tmp_path / "moved-here"
    os.rename(root, moved)
    status, payload = _call(base, "POST", "/api/judgements", {**BODY, "verdict": "rejected"})
    assert (status, payload["state"]) == (409, "project_moved")
    assert not root.exists() and _ledger_text(moved) == before
    status, payload = _call(base, "POST", "/api/project/reopen", {})
    assert status == 200 and payload["blocked"]["situation"] == "missing"
    assert payload["blocked"]["answers"] == ["locate"]
    pid = payload["project_id"]
    status, payload = _call(base, "GET", "/api/summary")
    assert (status, payload["state"]) == (409, "project_blocked")
    other = tmp_path / "unrelated"
    other.mkdir()
    status, payload = _call(base, "POST", "/api/projects/locate", {"id": pid, "path": str(other)})
    assert (status, payload.get("state")) == (409, "not_this_project"), payload
    status, payload = _call(base, "POST", "/api/projects/locate", {"id": pid, "path": str(moved)})
    assert status == 200 and payload["blocked"] is None and payload["project_id"] == pid
    status, summary = _call(base, "GET", "/api/summary")
    assert status == 200 and summary["project_root"] == str(moved.resolve())
    assert [e["path"] for e in registry.load()] == [str(moved.resolve())]  # one entry, at the new path
    status, payload = _call(base, "GET", "/api/history?" + urllib.parse.urlencode(BODY))
    assert [e["verdict"] for e in payload["entries"]] == ["confirmed"]


def test_reopen_of_a_healthy_project_reopens_it_and_writes_nothing(live):
    base, root, _ = live
    before = sorted(p.name for p in (root / ".rce").iterdir())
    status, payload = _call(base, "POST", "/api/project/reopen", {})
    assert status == 200 and payload["blocked"] is None and payload["read_only"] is False
    assert sorted(p.name for p in (root / ".rce").iterdir()) == before


def test_reopen_runs_the_origin_check_first(live):
    """The security model (module docstring): a foreign page cannot make
    the engine re-run its identity check."""
    base, _, _ = live
    status, _ = _call(base, "POST", "/api/project/reopen", {}, headers={"Origin": "http://evil.example"})
    assert status == 403
    status, _ = _call(base, "GET", "/api/records", headers={"Origin": "http://evil.example"})
    assert status == 403


def _rename_script(root: Path) -> None:
    os.rename(root / "s.py", root / "t.py")
    _scan(root)


def test_scenario_8d_review_marks_carry_the_link_and_a_candidate_takes_a_new_entry(tmp_path):
    """9.9 #8(d) in the app: rename the script -- the old judgment is under
    review with its reason, the renamed script's read is its candidate;
    applying the old verdict to the candidate is a NEW entry on the
    candidate and leaves the old one as it is. The 决策树 and 血缘 marks
    name the link they are about, so their 「待复核」 tag can open it."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "rejected", via="cli", note="旧版")
    _rename_script(root)
    conn = _conn(root)
    try:
        listed = judgements.review_items(conn)
        report = lineage.build_lineage_report(conn, root)
    finally:
        conn.close()
    item = next(i for i in listed["review"] if (i["src"], i["dst"]) == READ[:2])
    assert item["reason"] in ("endpoint_gone", "not_produced") and item["verdict"] == "rejected"
    candidate = item["candidates"][0]
    assert candidate["src"] == "script:t.py"
    readers = [r for c in report["orphans"] + report["chains"] for r in c.get("readers", []) if r.get("review")]
    assert readers and readers[0]["link"] == dict(zip(("src", "dst", "type", "extractor"), READ))
    before = _ledger_text(root)
    key = tuple(candidate[k] for k in ("src", "dst", "type", "extractor"))
    judgements.judge(root, key, "rejected", via="canvas")
    after = _ledger_text(root)
    assert after.startswith(before)  # appended, nothing re-emitted
    conn = _conn(root)
    try:
        still = {(i["src"], i["dst"]) for i in judgements.review_items(conn)["review"]}
    finally:
        conn.close()
    assert READ[:2] in still  # the old judgment is untouched, still waiting


def test_tree_marks_a_waiting_link_with_its_key(tmp_path):
    """9.6 "Where it shows" (决策树): the file row of a link under review
    carries the link it is about -- and only then (an ordinary row keeps
    its V4 shape)."""
    root, _ = _project(tmp_path)
    judgements.judge(root, READ, "confirmed", via="cli")
    (root / "s.py").write_text("import pandas as pd\ndf = pd.read_table('data/in.csv')\ndf.to_csv('data/out.csv')\n")
    _scan(root)
    conn = _conn(root)
    try:
        reads = server._connected_files(conn, "script:s.py", "reads")
        writes = server._connected_files(conn, "script:s.py", "writes")
    finally:
        conn.close()
    marked = [f for f in reads if f["review"]]
    assert marked and marked[0]["link"] == dict(zip(("src", "dst", "type", "extractor"), READ))
    assert marked[0]["judgement"]["reason"] == "not_produced"  # read_table is no reader the scan knows
    assert writes and all("link" not in f for f in writes)


def test_scenario_9_a_refused_retirement_names_its_holders_for_the_app(tmp_path):
    """9.5 step 5 / 9.12: a retirement refused because a process holds the
    old index carries who (pid and what), so the page can name it."""
    db_path = tmp_path / "graphs" / "abc" / "graph.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"")
    with pytest.raises(migration.MigrationRefused) as caught:
        migration.retire(
            tmp_path, "graphs/abc", db_path, engine_probe=lambda *_a: None,
            holder_probe=lambda _files: {4242: "python3 holder.py"},
        )
    assert caught.value.holders == [{"pid": 4242, "what": "has graph.db open: python3 holder.py"}]
    assert db_path.exists()
    result = migration.Migrated(tmp_path, "graphs/abc", ok=False, holders=caught.value.holders, stopped=str(caught.value))
    assert result.payload()["holders"][0]["pid"] == 4242


def test_inventory_rows_keep_their_english_for_the_cli(tmp_path):
    """The CLI's words are unchanged; the codes ride alongside."""
    root, _ = _project(tmp_path)
    rows = inventory.inventory(None, root)
    assert rows[0].kind == "Confirm/reject of machine links" and rows[0].code == "judgements"
    assert all(r.code for r in rows)


# -- the served page ------------------------------------------------------------------------


def test_served_page_carries_the_record_surfaces_in_product_language(live):
    """9.3-9.6 and the phase's wording: what the researcher meets is
    Chinese, on the shared helper, with the design's own phrases."""
    base, _, _ = live
    status, html = _call(base, "GET", "/")
    assert status == 200
    js = CANVAS_JS.read_text(encoding="utf-8")
    for mount in ('id="notices"', 'id="records-btn"'):
        assert mount in html
    for copy in (
        "待复核：", "仍然成立", "改为确认", "改为否决", "撤回", "把这条旧判断用到它上面",
        "来源文件暂不可读", "图谱里还没有这条关联", "记录冲突", "不计入待复核",
        "记录文件比图谱少了 ", "以文件为准", "把缺少的补回文件",
        "项目已移动或已在别处认领，请重新打开", "找不到项目文件夹（可能已移动）", "选择新位置…",
        "迁移这些记录", "这不是这个项目的", "迁移后才能记录判断", " 条里有 ", " 条的两端能在这个文件夹里扫到",
        "原位置暂时无法确认", "项目身份文件无法读取", "人工记录", "在 Finder 中显示", "最新快照：",
        "画布位置", "旧索引已移到：", "请先退出 RCE 与 MCP 服务",
    ):
        assert copy in html, copy
    for copy in ("确认这条连线", "标记为错误提取", "撤回", "撤销", "在待复核列表中查看", "备注（可选）",
                 "画布位置记录文件无法读取", "把它移到备份，重新开始摆放", "记录（新的在上）"):
        assert copy in js, copy


def test_review_wording_never_says_something_no_longer_exists():
    """9.6: the wording says what the scan did, never that something "no
    longer exists" -- a path the extractor could not resolve is not a
    deleted file."""
    html = APP_HTML.read_text(encoding="utf-8")
    start = html.index("// -- Judgment wording (pure")
    wording = html[start:html.index("// -- end of judgment wording")]
    review = html[html.index("// -- 待复核 (9.6)"):html.index("// -- 记录 (9.2")]
    for text in (wording, review):
        for phrase in ("不再存在", "已不存在", "已删除", "被删除了"):
            assert phrase not in text, phrase


def test_every_new_action_failure_goes_through_the_shared_helper():
    """8.8 "Errors": each new action's refusal is a Chinese sentence with
    the engine's text behind 「详情」, and the new refusal codes each have
    their own sentence."""
    html = APP_HTML.read_text(encoding="utf-8")
    js = CANVAS_JS.read_text(encoding="utf-8")
    assert 'setStatus(statusEl, "err", "没能记录这条判断", err)' in html
    assert 'setStatus(statusEl, "err", "没能完成这个选择", err)' in html
    assert 'setStatus(statusEl, "err", "没能使用这个位置", err)' in html
    assert 'setStatus(statusEl, "err", "没能完成这个回答", err)' in html
    assert 'if (err) renderBlockingError(el, text, err);' in html  # setStatus is the helper's
    for code in ("record_question_changed", "record_would_lose", "no_question", "answer_refused",
                 "not_this_project", "migration_refused"):
        assert f"  {code}: \"" in html, code
    assert 'showStatus(verdict === "withdrawn" ? "无法撤回" : "无法确认这条连线", err, { kind: "write" })' in js


def test_the_folder_picker_is_offered_only_when_the_shell_says_it_can():
    """「选择新位置…」: a path field always; the native picker only when
    the shell advertises `choose-folder` -- a browser, or a shell without
    it, falls back to the field with nothing lost."""
    html = APP_HTML.read_text(encoding="utf-8")
    assert 'if (shellCan("choose-folder"))' in html
    assert '{ type: "choose-folder", request: request }' in html
    assert "folderChosen(request, path)" in html


# -- the page's pure wording, under node ---------------------------------------------------------

_WORDING_RUNNER = r"""
const block = require("fs").readFileSync(0, "utf8");
eval(block + "; global.W = { verdictWord, whenText, basisLines, basisText, endName, linkText, historyEntryText, historyBasisText, reviewHoverText };");
const calls = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(calls.map(([fn, args]) => W[fn](...args))));
"""


def _wording(*calls: tuple[str, list[Any]]) -> list[Any]:
    html = APP_HTML.read_text(encoding="utf-8")
    block = html[html.index("// -- Judgment wording (pure"):html.index("// -- end of judgment wording")]
    result = subprocess.run(
        [NODE, "-e", _WORDING_RUNNER, json.dumps(list(calls))],
        input=block, capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


@needs_node
def test_the_hover_card_reads_as_the_design_example():
    """Item 3: 「你曾于 2026-10-04 否决 · 依据已变化，待复核」; a migrated
    judgment never shows the migration's time as its own (9.12); a
    conflict is 「记录冲突，待处理」."""
    j = {"outcome": "review", "verdict": "rejected", "at": "2026-10-04T21:15:03+02:00", "label": "依据已变化", "migrated": False}
    migrated = dict(j, migrated=True, at_label="迁移自旧索引（原判断时间未知）")
    out = _wording(("reviewHoverText", [j]), ("reviewHoverText", [migrated]), ("reviewHoverText", [{"outcome": "conflict"}]))
    assert out[0] == "你曾于 2026-10-04 否决 · 依据已变化，待复核"
    assert out[1] == "你曾否决（迁移自旧索引（原判断时间未知）） · 依据已变化，待复核"
    assert "2026-10-04" not in out[1]
    assert out[2] == "记录冲突，待处理"


@needs_node
def test_a_basis_is_rendered_readably():
    """9.6's table, as a person reads it: 调用：read.csv; a claim's
    sentence, number and metrics; an artifact; identity alone; the old
    index's several bases kept apart."""
    out = _wording(
        ("basisLines", [{"calls": ["read.csv", "read_dta"]}]),
        ("basisLines", [{"sentence": "精度为 0.87", "number": "0.87", "metrics": {"acc": 0.87}}]),
        ("basisLines", [{"artifacts": ["model.pkl"]}]),
        ("basisLines", [{}]),
        ("basisLines", [None]),
        ("basisLines", [{"old_index": ['{"calls":["open"]}', '{"calls":["read_csv"]}']}]),
    )
    assert out[0] == ["调用：read.csv、read_dta"]
    assert out[1] == ["论断：「精度为 0.87」", "数值：0.87", "指标：acc = 0.87"]
    assert out[2] == ["产物：model.pkl"]
    assert out[3] == ["只凭这条关联本身"]
    assert out[4] == []
    assert out[5] == ["旧索引里记着几种不同的依据：调用：open / 调用：read_csv"]


@needs_node
def test_history_lines_say_who_did_what_when_and_from_where():
    """The card's history (9.3): when, who, the act, from which surface;
    the basis each verdict rested on; a migrated entry's time is the
    label, never the migration's clock."""
    canvas_reject = {"verdict": "rejected", "at": "2026-10-04T21:15:03+02:00", "via": "canvas", "migrated": False,
                     "basis": {"calls": ["read.csv"]}, "basis_recorded": "at-judgment"}
    migrated = {"verdict": "confirmed", "at": "2026-10-05T09:00:00+02:00", "via": "migrated", "migrated": True,
                "basis": {"calls": ["read_csv"]}, "basis_recorded": "at-migration"}
    out = _wording(
        ("historyEntryText", [canvas_reject]),
        ("historyEntryText", [dict(canvas_reject, verdict="undone", via="cli")]),
        ("historyEntryText", [dict(canvas_reject, verdict="withdrawn", via="mcp")]),
        ("historyEntryText", [migrated]),
        ("historyEntryText", [dict(canvas_reject, via="recovered")]),
        ("historyBasisText", [canvas_reject]),
        ("historyBasisText", [migrated]),
        ("historyBasisText", [dict(canvas_reject, basis_recorded="not-produced")]),
        ("historyBasisText", [dict(canvas_reject, verdict="withdrawn")]),
    )
    assert out[0] == "2026-10-04 21:15 · 你在画布上否决"
    assert out[1] == "2026-10-04 21:15 · 你在命令行里撤销了上一步"
    assert out[2] == "2026-10-04 21:15 · 你通过 MCP撤回了判断"
    assert out[3] == "迁移自旧索引（原判断时间未知） · 确认" and "2026-10-05" not in out[3]
    assert out[4] == "2026-10-04 21:15 · 补回文件里缺少的判断：否决"
    assert out[5] == "依据 · 调用：read.csv"
    assert out[6] == "依据 · 调用：read_csv（迁移时核对过）"
    assert out[7] == "依据：当时的扫描没有得出这条关联"
    assert out[8] == ""


@needs_node
def test_a_link_reads_in_product_language():
    out = _wording(
        ("linkText", [{"src": "script:复现包/17-叙事.Rmd", "dst": "dataset:复现包/Data/panel.csv", "type": "reads"}]),
        ("linkText", [{"src": "claim:paper.tex#abc", "dst": "experiment:run1", "type": "backed_by"}]),
        ("linkText", [{"src": "script:a.py", "dst": "figure:f.png", "type": "odd_type"}]),
    )
    assert out == ["17-叙事.Rmd 读取 panel.csv", "论断（paper.tex） 依据 实验（run1）", "a.py odd_type f.png"]


# -- the link card's actions, under node ----------------------------------------------------------

_ACTIONS_RUNNER = r"""
global.window = {};
require(process.argv[1]);
const links = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(links.map((l) => window.RCECanvas._linkActions(l).map((a) => a.label))));
"""


@needs_node
def test_the_card_offers_judgments_on_machine_links_only():
    """Item 1: a machine link offers 「确认这条连线」 (unless a confirmation
    stands and applies), 「标记为错误提取」, and 「撤回」 while a verdict
    stands -- under review too; a hand-drawn link offers none of them (its
    authority is mappings.toml); a link still being written, nothing."""
    links = [
        {"human": False, "status": "auto"},
        {"human": False, "status": "confirmed"},
        {"human": False, "status": "auto", "review": True, "judgement": {"verdict": "rejected"}},
        {"human": False, "status": "pending", "conflict": True, "judgement": {"outcome": "conflict"}},
        {"human": True, "status": "confirmed"},
        {"human": True, "optimistic": True},
    ]
    result = subprocess.run(
        [NODE, "-e", _ACTIONS_RUNNER, str(CANVAS_JS)], input=json.dumps(links),
        capture_output=True, text=True, check=True, timeout=30,
    )
    out = json.loads(result.stdout)
    assert out[0] == ["确认这条连线", "标记为错误提取"]
    assert out[1] == ["标记为错误提取", "撤回"]
    assert out[2] == ["确认这条连线", "标记为错误提取", "撤回"]
    assert out[3] == ["确认这条连线", "标记为错误提取", "撤回"]
    assert out[4] == ["删除标注"]
    assert out[5] == []


@needs_node
def test_no_position_is_saved_over_an_arrangement_record_that_cannot_be_read():
    """Item 4 / 9.2: while canvas.json cannot be read, a move is not posted
    -- the file is left untouched and the page says so."""
    runner = r"""
global.window = { confirm: () => true };
global.posts = [];
global.apiPost = async (url, body) => { posts.push(url); return { ok: true }; };
require(process.argv[1]);
const C = window.RCECanvas, S = C._state;
const payload = { scope: { id: "all" }, nodes: [{ id: "script:a.py", type: "script", path: "a.py" }], links: [],
  frames: [], step_groups: [], positions: {}, scopes: [], project: "/p", layout: { state: "invalid", error: "not JSON" } };
S.data = payload; S.scope = "all"; S.project = "/p"; S.nodes = new Map([["script:a.py", payload.nodes[0]]]);
C._layoutView(true);
S.positions["script:a.py"] = [10, 10]; C._cardMoved("script:a.py");
(async () => {
  await C._flushSave();
  const blocked = C._layoutBlocked();
  payload.layout = { state: "ok" };
  S.positions["script:a.py"] = [20, 20]; C._cardMoved("script:a.py");
  await C._flushSave();
  process.stdout.write(JSON.stringify({ blocked, posts }));
})();
"""
    result = subprocess.run([NODE, "-e", runner, str(CANVAS_JS)], capture_output=True, text=True, check=True, timeout=30)
    out = json.loads(result.stdout)
    assert out["blocked"] is True
    assert out["posts"] == ["/api/canvas/layout"]  # only once the record could be read again
