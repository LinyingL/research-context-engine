"""DESIGN.md Section 10 (task V6), phase B: the page -- the project menu
(10.1, 10.4), the add dialog (10.2), the scan's progress (10.2 step 3,
10.3), the no-project page (10.1) and the errors (8.8) -- and the calls the
page makes, in the order it makes them. The shell's half (10.7) is in
tests/test_webapp_macapp.py. Each test names the 10.8 acceptance scenario
it covers.

The page's pure wording is run under node against the REAL app.html, as
tests/test_v5_phase7.py does; the rest is pinned on the served source and
driven over HTTP the way the page drives it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from rce import paths
from rce import project as project_identity
from rce.records import situation as records_situation
from rce.webapp import registry, server

APP_HTML = Path(server.__file__).parent / "app.html"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _src() -> str:
    return APP_HTML.read_text(encoding="utf-8")


def _script() -> str:
    html = _src()
    return re.findall(r"<script>(.*?)</script>", html, re.S)[-1]


def _function(name: str) -> str:
    src = _src()
    start = src.index(f"function {name}(")
    return src[start: src.index("\n}\n", start)]


# -- a live engine, driven as the page drives it ---------------------------------------------------


class Live:
    def __init__(self, httpd: server.RceHTTPServer) -> None:
        self.httpd = httpd
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def _call(self, req: urllib.request.Request) -> tuple[int, Any]:
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, (json.loads(body) if "json" in ctype else body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def get(self, path: str) -> tuple[int, Any]:
        return self._call(urllib.request.Request(self.base + path))

    def post(self, path: str, body: dict | None = None) -> tuple[int, Any]:
        return self._call(urllib.request.Request(
            self.base + path, data=json.dumps(body or {}).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"},
        ))

    def wait_scan(self, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, gen = self.get("/api/generation")
            if gen.get("scanning") is None and gen.get("last_scan") is not None:
                return gen
            time.sleep(0.05)
        raise AssertionError("the scan did not finish")


@pytest.fixture
def engine(tmp_path_factory, monkeypatch):
    # A throwaway home too: inspecting checks the home folder's top-level
    # folders (10.2), and no test here should touch the real ones.
    home = tmp_path_factory.mktemp("home")
    (home / "Documents").mkdir()
    monkeypatch.setenv("HOME", str(home))
    httpd = server.build_server(None, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield Live(httpd)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _research_folder(root: Path) -> Path:
    (root / "code").mkdir(parents=True)
    (root / "code" / "clean.py").write_text('import pandas as pd\npd.read_csv("data/raw.csv")\n')
    (root / "data").mkdir()
    (root / "data" / "raw.csv").write_text("a,b\n1,2\n")
    (root / "paper.md").write_text("# Results\n")
    return root


def _labels() -> list[str]:
    return [e["label"] for e in registry.load()]


# -- 10.8 #1: first launch ------------------------------------------------------------------------


def test_first_launch_page_shows_only_the_welcome_and_asks_the_engine_for_nothing_else(engine):
    """10.8 #1 (first launch), 10.1 "No project yet": with an empty registry
    the served page carries the welcome -- two sentences on what RCE does,
    「添加项目…」, one line on what a project is -- and every tab shows only
    it: no view, notice, attempt button or 记录 while there is no project,
    and nothing asks the engine for a project's data (it would be refused)."""
    status, html = engine.get("/")
    assert status == 200
    welcome = html[html.index('<section id="welcome"'):html.index("</section>", html.index('<section id="welcome"'))]
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", welcome)
    assert len(paragraphs) == 3
    assert "脚本、数据和文稿" in paragraphs[0] and "判断" in paragraphs[1]
    assert paragraphs[2] == "项目就是存放一项研究的那个文件夹。"
    assert '<button id="welcome-add-btn" class="btn" type="button">添加项目…</button>' in welcome
    assert "error" not in welcome
    css = html[html.index("<style>"):html.index("</style>")]
    assert "body.no-project .welcome { display: block; }" in css
    assert ("body.no-project .view, body.no-project .notices, body.no-project #new-attempt-btn,\n"
            "body.no-project #records-btn { display: none !important; }") in css
    # the engine's half: no project, and every project endpoint refused
    _, projects = engine.get("/api/projects")
    assert projects["no_project"] is True and projects["current"] is None
    # the page's half: no project's data is asked for
    assert "if (state.noProject) return null; // 10.1" in _function("activateView")
    assert "if (state.noProject) return; // 10.1" in _function("refreshCurrentView")
    assert "if (state.noProject) {" in _src()[_src().index("async function loadProjectSummary"):][:900]
    assert "if (state.noProject) return; // 10.1" in _function("openAttemptForm")
    reload = _function("reloadAllViews")
    assert reload.index("if (state.noProject) return;") < reload.index("loadTreeView()")
    load = _src()[_src().index("async function loadProjects()"):]
    load = load[: load.index("\n}\n")]
    assert "const noProject = !!data.no_project || data.current == null;" in load
    assert "setNoProject(noProject);" in load


def test_first_launch_add_scans_with_progress_as_the_page_drives_it(engine, tmp_path):
    """10.8 #1: from the no-project page, 「添加项目…」 inspects, adds with the
    inspection's token and the display name typed in the dialog, and the
    header follows the scan from GET /api/generation (scanning, then
    last_scan); the views fill once it ends."""
    folder = _research_folder(tmp_path / "研究")
    status, insp = engine.post("/api/projects/inspect", {"path": str(folder)})
    assert status == 200 and insp["kind"] == "new_folder" and insp["can_add"] is True
    assert insp["label"] == "研究"  # the default display name the dialog shows
    assert insp["preview"]["counts"] == {"scripts": 1, "data": 1, "drafts": 1, "images": 0, "other": 0}
    status, res = engine.post("/api/projects/add", {"path": insp["path"], "label": "我的研究", "inspected": insp["inspected"]})
    assert status == 200 and res["scanning"] is True and res["current"] is not None
    gen = engine.wait_scan()
    assert gen["last_scan"]["ok"] is True and gen["last_scan"]["finished"]
    _, projects = engine.get("/api/projects")
    assert projects["no_project"] is False and [p["label"] for p in projects["projects"]] == ["我的研究"]
    status, summary = engine.get("/api/summary")
    assert status == 200
    # the page: a scan that started with the switch is news even if it ends first
    done = _function("addDone")
    assert "await reloadAllViews({ scanStarted: !!res.scanning });" in done
    assert "state.scanSeen = scanStarted ? null : undefined;" in _function("resetScanChip")


# -- the page's pure wording, under node ----------------------------------------------------------

_WORDING_RUNNER = r"""
const block = require("fs").readFileSync(0, "utf8");
eval(block + "; global.W = { groupedNumber, addPreviewText, addWritesText, scanProgressText, scanDoneText };");
const calls = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(calls.map(([fn, args]) => W[fn](...args))));
"""


def _wording(*calls: tuple[str, list[Any]]) -> list[Any]:
    html = _src()
    block = html[html.index("// -- Add-project wording (pure"):html.index("// -- end of add-project wording")]
    result = subprocess.run(
        [NODE, "-e", _WORDING_RUNNER, json.dumps(list(calls))],
        input=block, capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


def _preview(**over: Any) -> dict[str, Any]:
    base = {
        "source": "walk", "counts": {"scripts": 3, "data": 1, "drafts": 2, "images": 1, "other": 1},
        "to_scan": 7, "large": False, "dataless": 0, "truncated": False, "git": None,
    }
    base.update(over)
    return base


@needs_node
def test_preview_words_the_engines_counts_for_a_plain_folder():
    """10.8 #2 (a new folder, not a git repository): the dialog lists the
    preview's own numbers -- scripts, data, drafts, images, the total to
    scan -- and says what is not read; the numbers are the engine's (the
    scan's own inventory), never recounted on the page."""
    (out,) = _wording(("addPreviewText", [_preview()]))
    assert out["head"] == "RCE 会读取这个文件夹里的这些文件："
    assert out["items"] == [
        ["脚本（.py、.R、.Rmd）", "3"], ["数据文件", "1"], ["文稿（.md、.tex、.bib）", "2"], ["图片", "1"], ["共计要扫描", "7"],
    ]
    assert out["notes"] == [{"text": "另有 1 个文件不属于这几类，RCE 不读它们。", "tone": ""}]


@needs_node
def test_preview_names_tracked_and_untracked_files_of_a_git_repository():
    """10.8 #3 (a git repository with untracked files): RCE reads the files
    git tracks, how many those are, and how many more sit in the folder
    untracked and will not be read -- 「另有 N 个未被 git 跟踪的文件不会被扫描」;
    a git that could not be asked is said, not hidden."""
    git = {"tracked": 1234, "untracked": 5, "untracked_truncated": False, "error": None}
    out = _wording(
        ("addPreviewText", [_preview(source="git", git=git)]),
        ("addPreviewText", [_preview(source="git", git=dict(git, untracked=100000, untracked_truncated=True))]),
        ("addPreviewText", [_preview(source="git", git=dict(git, untracked=0))]),
        ("addPreviewText", [_preview(source="git", git={"tracked": None, "untracked": None, "untracked_truncated": False, "error": "fatal"})]),
        ("addPreviewText", [_preview(source="git", git=dict(git, untracked=None, error="count failed"))]),
    )
    assert out[0]["head"] == "这是一个 git 仓库：RCE 只读取 git 跟踪的文件，共 1,234 个。其中要扫描的有："
    assert {"text": "另有 5 个未被 git 跟踪的文件不会被扫描。", "tone": "warn"} in out[0]["notes"]
    assert {"text": "另有 至少 100,000 个未被 git 跟踪的文件不会被扫描。", "tone": "warn"} in out[1]["notes"]
    assert not any("未被 git 跟踪" in n["text"] for n in out[2]["notes"])
    assert "没能向 git 问出它跟踪哪些文件" in out[3]["head"]
    assert any("没能数出有多少文件未被 git 跟踪" in n["text"] for n in out[4]["notes"])


@needs_node
def test_preview_says_cloud_files_the_entry_cap_and_asks_twice_above_five_thousand():
    """10.2 "The preview": files still in the cloud are counted and said to
    be read once downloaded; counting stops at 100,000 and says so; above
    5,000 files to scan the preview says so (and the dialog asks a second
    time -- showLargeConfirm); size alone refuses nothing."""
    (out,) = _wording(("addPreviewText", [_preview(to_scan=6001, large=True, dataless=12, truncated=True)]))
    texts = [n["text"] for n in out["notes"]]
    assert "其中 12 个文件还在 iCloud 云端，下载到这台电脑之后才会被读取。" in texts
    assert "文件太多：数到 100,000 个就停下了，实际还有更多。" in texts
    assert "要扫描的文件超过 5,000 个，第一次扫描会花一些时间；加入之前会再问你一次。" in texts
    assert out["items"][-1] == ["共计要扫描", "6,001"]
    render = _function("renderInspection")
    assert "insp.preview && insp.preview.large && !addDialog.confirmLarge" in render
    assert "showLargeConfirm(subject, result, actions);" in render
    assert "go.disabled = !insp.can_add;" in render  # large is not a refusal
    confirm = _function("showLargeConfirm")
    assert "确定，加入并扫描" in confirm and "addDialog.confirmLarge = true; writeAdd(subject, [yes, no]);" in confirm


@needs_node
def test_what_adding_writes_is_said_per_kind():
    """10.2: the preview says what adding writes -- a `.rce/` folder inside
    the project holding its identity file and a short README, and an index
    under ~/.rce; nothing else in the folder. An RCE project that opens and
    one from before V5 are only written into the list (10.8 #6)."""
    out = _wording(
        ("addWritesText", [{"kind": "new_folder"}]),
        ("addWritesText", [{"kind": "pre_v5", "can_add": True}]),
        ("addWritesText", [{"kind": "rce_project", "can_add": True, "situation": {"situation": "normal"}}]),
        ("addWritesText", [{"kind": "rce_project", "can_add": True, "situation": {"situation": "moved"}}]),
        ("addWritesText", [{"kind": "rce_project", "can_add": True, "situation": {"situation": "no_index"}}]),
        ("addWritesText", [{"kind": "rce_project", "can_add": False, "situation": {"situation": "copy"}}]),
        ("addWritesText", [{"kind": "refused"}]),
    )
    assert out[0] == ("加入会写入：这个文件夹里的 .rce/（项目身份文件 project.toml 和一份简短的 README），"
                      "以及 ~/.rce 下的索引。文件夹里别的东西都不会被创建或改动。")
    assert out[1].startswith("加入只会把它写进项目列表。迁移旧记录")
    assert out[2] == "加入只会把它写进项目列表。"
    assert "新位置" in out[3] and "重新建立" in out[4]
    assert out[5] == "" and out[6] == ""


@needs_node
def test_the_scan_line_reads_step_and_count_then_done():
    """10.8 #1 and #10: 「正在扫描：<步骤>（n/m）」 while it runs; 「扫描完成」,
    or 「扫描完成，有 N 个文件暂时读不了」 (a failed read is not a failed
    addition), or the scan's own sentence when it did not finish."""
    out = _wording(
        ("scanProgressText", [{"step": "sources", "label": "读取脚本、数据和文稿", "n": 1, "m": 4}]),
        ("scanProgressText", [{"step": None, "label": None, "n": 0, "m": 4}]),
        ("scanDoneText", [{"ok": True, "unreadable_sources": []}]),
        ("scanDoneText", [{"ok": True, "unreadable_sources": ["a.py", "b.R"]}]),
        ("scanDoneText", [{"ok": False, "message": "扫描没有完成", "error": "boom"}]),
        ("scanDoneText", [{"ok": False, "message": None, "error": "boom"}]),
    )
    assert out == [
        "正在扫描：读取脚本、数据和文稿（1/4）", "正在扫描：准备中（0/4）", "扫描完成",
        "扫描完成，有 2 个文件暂时读不了", "扫描没有完成", "扫描没有完成",
    ]
    done = _function("showScanDone")
    assert "openRecordsPanel();" in done  # 「…暂时读不了」 links to the record view
    assert "setTimeout(hideScanChip, SCAN_DONE_MS)" in done
    assert "renderBlockingError(text, scanDoneText(last)" in done  # a scan that did not finish: 详情


# -- 8.8: every new code has its Chinese sentence and a 「详情」 ----------------------------------

_ERRORS_RUNNER = r"""
const block = require("fs").readFileSync(0, "utf8");
eval(block + "; global.E = { apiError, blockingErrorText };");
const bodies = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(bodies.map((b) => {
  const err = E.apiError("/api/x", { status: 409 }, b);
  return [E.blockingErrorText("框架句", err), err.detail || null, !!err.inspection];
})));
"""


@needs_node
def test_add_project_refusals_show_the_engines_chinese_sentence_and_its_detail_behind_the_toggle():
    """8.8 "Errors" for 10.8 #7 / #8 / #9 / #10: each new code's Chinese
    sentence comes from the engine (some name a project or a rule); the
    engine's own English is what 「详情」 reveals; an add refusal carries the
    fresh look at the folder, which the dialog shows instead (#8)."""
    src = _src()
    api_error = src[src.index("function apiError("):]
    api_error = api_error[: api_error.index("\n}\n") + 3]
    table = src[src.index("const BLOCKING_ERROR_TEXT = {"):src.index("// Replaces `el`'s content")]
    bodies = [
        {"error": "这个文件夹在项目「甲」里面", "state": "inside_project", "message": "这个文件夹在项目「甲」里面",
         "detail": "/x is inside the registered project /y"},
        {"error": "显示名称不能超过 100 个字符", "state": "bad_label", "message": "显示名称不能超过 100 个字符", "detail": "too long"},
        {"error": "这个项目正在扫描，请等它结束", "state": "scan_running", "message": "这个项目正在扫描，请等它结束", "detail": "running"},
        {"error": "这是旧版 RCE 项目：迁移之后才能重新扫描", "state": "frozen", "message": "这是旧版 RCE 项目：迁移之后才能重新扫描", "detail": "frozen"},
        {"error": "这个文件夹在你查看之后变了，请重新查看", "state": "inspected_changed", "message": "这个文件夹在你查看之后变了，请重新查看",
         "detail": "changed", "inspection": {"kind": "rce_project"}},
        {"error": "还没有项目：请先添加一个项目文件夹", "state": "no_project", "message": "还没有项目：请先添加一个项目文件夹"},
        {"error": "Something", "state": "some_other_code", "message": "不该被采用"},
        {"error": "x", "state": "project_moved", "message": "也不该被采用"},
    ]
    result = subprocess.run(
        [NODE, "-e", _ERRORS_RUNNER, json.dumps(bodies)],
        input=api_error + table, capture_output=True, text=True, check=True, timeout=30,
    )
    out = json.loads(result.stdout)
    assert out[0] == ["这个文件夹在项目「甲」里面", "/x is inside the registered project /y", False]
    assert out[1][0] == "显示名称不能超过 100 个字符"
    assert out[2][0] == "这个项目正在扫描，请等它结束"
    assert out[3][0] == "这是旧版 RCE 项目：迁移之后才能重新扫描"
    assert out[4] == ["这个文件夹在你查看之后变了，请重新查看", "changed", True]
    assert out[5][0] == "还没有项目：请先添加一个项目文件夹"
    assert out[6][0] == "框架句"  # an unknown code keeps the caller's framing
    assert out[7][0] == "项目已移动或已在别处认领，请重新打开"  # the page's own table wins
    render = _function("renderBlockingError")
    assert "typeof err.detail === \"string\" && err.detail) ? err.detail" in render


def test_every_new_action_reports_through_the_shared_helper():
    """8.8 "Errors": rename, remove, rescan, locate, switch, inspect and add
    each show a Chinese sentence with 「详情」, never a hover."""
    src = _src()
    for call in (
        'renderBlockingError(statusEl, "没能改名", err)',
        'renderBlockingError(statusEl, "没能从列表中移除", err)',
        'renderBlockingError(statusEl, "没能开始扫描", err)',
        'showHeaderError("没能使用这个位置", err)',
        'showHeaderError("切换项目失败", err)',
        'addStatus("err", "没能查看这个文件夹", err)',
        'addStatus("err", "没能加入这个文件夹", err)',
        'setStatus(statusEl, "err", "没能完成这个选择", err)',
    ):
        assert call in src, call
    assert 'showHeaderError("已加入项目，但没能开始扫描；可以在项目菜单里「重新扫描这个项目」", err)' in src
    assert "function addStatus(cls, text, err) {" in src and "setStatus(el, cls, text, err)" in src


# -- 10.1 / 10.4: the project menu -----------------------------------------------------------------


def test_the_project_menu_replaces_the_select_and_offers_what_10_1_lists():
    """10.8 #9 (the list), 10.1 "The project menu": one button 「项目：<名称> ▾」,
    always shown, replacing the <select> and 「移除失效项目」; the registered
    projects with the open one marked and a missing one greyed
    「找不到文件夹」 with 「选择新位置…」; a rule; 添加 / 重新扫描 / 重命名 /
    移除."""
    src = _src()
    assert 'id="project-switcher"' not in src and 'id="remove-missing-btn"' not in src and "<select id=" not in src
    assert ('<button id="project-menu-btn" class="project-menu-btn" type="button" aria-haspopup="menu" '
            'aria-expanded="false" aria-controls="project-menu">') in src
    assert '<div id="project-menu" class="project-menu hidden" role="menu" aria-label="项目"></div>' in src
    assert 'projectMenuBtn.textContent = "项目：" + (label || NO_PROJECT_NAME) + " ▾";' in src
    menu = _function("renderProjectMenu")
    order = [menu.index(s) for s in (
        'role: "menuitemradio", mark: current ? "✓" : ""', '"找不到文件夹"', 'pmItem("选择新位置…"',
        'mk("hr", "pm-rule")', 'pmItem("添加项目…"', 'pmItem("重新扫描这个项目"', 'pmItem("重命名显示名称…"',
        'pmItem("从列表中移除…"',
    )]
    assert order == sorted(order)
    assert 'rule.setAttribute("role", "separator");' in menu
    assert 'main.setAttribute("aria-checked", current ? "true" : "false");' in menu


def test_the_project_menu_is_keyboard_reachable_and_closes_on_escape_and_outside_clicks():
    """10.1: the menu opens from the keyboard on its first item, ↑ ↓ Home End
    move, Esc closes it (returning focus to the button) or leaves the
    rename / remove step, Tab and a click outside close it."""
    src = _src()
    assert "else openProjectMenu(e.detail === 0); // a keyboard click lands on the first item" in src
    btn = src[src.index('projectMenuBtn.addEventListener("keydown"'):]
    btn = btn[: btn.index("\n});\n")]
    assert '(e.key === "ArrowDown" || e.key === "ArrowUp") && !projectMenuOpen()' in btn and "openProjectMenu(true);" in btn
    keys = src[src.index('projectMenu.addEventListener("keydown"'):]
    keys = keys[: keys.index("\n});\n")]
    for snippet in ('e.key === "Escape"', "backToMenuList();", "closeProjectMenu(true);", '"ArrowDown"', '"ArrowUp"',
                    '"Home"', '"End"', 'e.key === "Tab") closeProjectMenu(false);', "e.stopPropagation();"):
        assert snippet in keys, snippet
    outside = src[src.index('document.addEventListener("mousedown"'):]
    assert "projectMenuOpen() && !projectMenuWrap.contains(e.target)" in outside[:200]
    assert "pm-item:focus-visible" in src
    # a re-render under the keyboard keeps its place
    load = src[src.index("async function loadProjects()"):]
    assert "const focused = menuItems().indexOf(document.activeElement);" in load[: load.index("\n}\n")]


def test_rename_changes_the_label_only_and_remove_says_what_stays(engine, tmp_path):
    """10.8 #9 and 10.4: 「重命名显示名称…」 (Enter saves) posts the label for
    the entry's id (a pre-V5 entry: its path); 「从列表中移除…」's sentence
    says the folder, its .rce/ records and its index stay, and adding it
    again brings everything back. Removing another project answers without
    `no_project`; removing the open one names what is served now -- the
    page reloads every view only then."""
    src = _src()
    rename = _function("renderRenameStep")
    assert 'if (e.key === "Enter") { e.preventDefault(); save.click(); }' in rename
    assert "只改项目列表里显示的名字，文件夹本身不会改名。" in rename
    assert ('apiPost("/api/projects/rename", entry.id ? { id: entry.id, label: label } : { path: entry.path, label: label })'
            in _function("saveRename"))
    assert _function("removeConfirmText").count("文件夹本身、里面的 .rce/ 记录和 ~/.rce 下的索引都原样保留；以后再添加这个文件夹，一切都会回来。") == 1
    remove = _function("removeFromMenu")
    assert 'apiPost("/api/projects/remove", { path: p.path })' in remove
    assert 'Object.prototype.hasOwnProperty.call(res, "no_project")' in remove
    assert "await reloadAllViews();" in remove and "await loadProjects();" in remove
    assert "let chosen = Math.max(0, projects.findIndex((p) => isCurrentEntry(p)));" in _function("renderRemoveStep")

    # the same calls, over HTTP
    a = _research_folder(tmp_path / "a")
    b = _research_folder(tmp_path / "b")
    for folder in (a, b):
        _, insp = engine.post("/api/projects/inspect", {"path": str(folder)})
        status, _ = engine.post("/api/projects/add", {"path": insp["path"], "label": folder.name, "inspected": insp["inspected"]})
        assert status == 200
        engine.wait_scan()
    entries = {e["label"]: e for e in registry.load()}
    status, res = engine.post("/api/projects/rename", {"id": entries["b"]["id"], "label": "乙"})
    assert status == 200 and res["entry"]["label"] == "乙" and (b / ".rce" / "project.toml").is_file()
    status, res = engine.post("/api/projects/rename", {"id": entries["b"]["id"], "label": "  "})
    assert status == 400 and res["state"] == "bad_label" and res["message"] == "显示名称不能为空"
    before = sorted(p.name for p in (a / ".rce").iterdir())
    status, res = engine.post("/api/projects/remove", {"path": entries["a"]["path"]})  # not the open one
    assert status == 200 and "no_project" not in res
    status, res = engine.post("/api/projects/remove", {"path": entries["b"]["path"]})  # the open one, the last
    assert status == 200 and res["no_project"] is True and res["current"] is None
    assert sorted(p.name for p in (a / ".rce").iterdir()) == before  # records untouched
    _, projects = engine.get("/api/projects")
    assert projects["no_project"] is True


def test_rescan_from_the_menu_shows_progress_and_a_second_request_says_one_is_running():
    """10.8 #10: 「重新扫描这个项目」 posts /api/projects/rescan; its progress
    comes from the generation poll; a refusal (one running, a pre-V5
    project) is said in the menu with 「详情」."""
    src = _src()
    rescan = _function("rescanFromMenu")
    assert 'apiPost("/api/projects/rescan", {})' in rescan and "refreshScanStatus();" in rescan
    assert 'renderBlockingError(statusEl, "没能开始扫描", err);' in rescan
    poll = src[src.index("async function pollGeneration()"):]
    poll = poll[: poll.index("\n}\n")]
    assert poll.index("updateScanChip(status);") < poll.index("if (lastGeneration === null)")
    assert "updateScanChip(status);" in _function("syncGeneration")
    assert 'id="scan-chip"' in src


def test_a_missing_entry_is_relocated_through_the_existing_locate_flow():
    """10.1 / 9.4: a missing entry's 「选择新位置…」 opens the shell's folder
    panel (purpose locate) and posts /api/projects/locate; in a plain
    browser it opens the entry, whose view asks the same question with a
    path field."""
    relocate = _function("relocateFromMenu")
    assert 'if (!shellCan("choose-folder")) {' in relocate and "await switchToProject(p);" in relocate
    assert 'const chosen = await chooseFolder("locate");' in relocate
    assert 'apiPost("/api/projects/locate", { id: p.id, path: chosen })' in relocate


# -- 10.2: the add dialog ---------------------------------------------------------------------------


def test_the_add_dialog_chooses_with_the_shell_or_a_path_field_and_looks_before_writing():
    """10.8 #1 and 10.2 step 1-2: the dialog opens the shell's folder panel
    (purpose add) when there is one, and always has a path field with
    「检查这个文件夹」; choosing asks the engine to look (inspect) and the
    only write is the button that says what it writes, carrying the token."""
    src = _src()
    opener = _function("openAddDialog")
    assert 'mkButton("检查这个文件夹", "btn"' in opener and 'input.id = "add-path";' in opener
    assert 'if (shellCan("choose-folder")) pickFolderForAdd(subject);' in opener
    assert 'const chosen = await chooseFolder("add");' in _function("pickFolderForAdd")
    assert 'apiPost("/api/projects/inspect", { path: path })' in _function("inspectForAdd")
    assert "return { path: addDialog.path, label: addDialog.label, inspected: addDialog.inspection.inspected };" in src
    assert src.count('apiPost("/api/projects/add", addBody())') == 2  # 加入… and a 9.4 answer
    render = _function("renderInspection")
    assert 'const verb = insp.kind === "new_folder" ? "加入并扫描" : "加入列表并打开";' in render
    assert 'mkButton("重新选择", "btn"' in render
    assert '"add-project": () => openAddDialog(),' in src
    assert 'document.getElementById("welcome-add-btn").addEventListener("click", () => openAddDialog());' in src
    assert 'pmItem("添加项目…", () => { closeProjectMenu(false); openAddDialog(); })' in src


def test_already_in_the_list_offers_the_switch_and_writes_nothing(engine, tmp_path):
    """10.8 #4: 「这个项目已经在列表里」 -- 「切换过去」 posts
    /api/projects/switch with the entry's path and id; nothing is written."""
    render = _function("renderInspection")
    assert 'mkButton("切换过去", "btn"' in render
    assert "await switchToProject({ path: entry.path, id: entry.id });" in render
    assert "它就是现在打开的项目。" in render

    folder = _research_folder(tmp_path / "proj")
    _, insp = engine.post("/api/projects/inspect", {"path": str(folder)})
    engine.post("/api/projects/add", {"path": insp["path"], "label": "proj", "inspected": insp["inspected"]})
    engine.wait_scan()
    registry_before = paths.rce_home().joinpath("projects.json").read_bytes()
    status, again = engine.post("/api/projects/inspect", {"path": str(folder)})
    assert status == 200 and again["kind"] == "already_registered" and again["message"] == "这个项目已经在列表里"
    assert again["entry"]["label"] == "proj"
    status, sw = engine.post("/api/projects/switch", {"path": again["entry"]["path"], "id": again["entry"]["id"]})
    assert status == 200 and sw["label"] == "proj"
    assert paths.rce_home().joinpath("projects.json").read_bytes() == registry_before


def test_a_copy_asks_the_9_4_question_in_the_dialog_and_nothing_is_written_until_answered(engine, tmp_path):
    """10.8 #5: the dialog shows the 9.4 question (the existing blocked-state
    rendering) for a copy; the registry is untouched until an answer is
    chosen; the answer adds (served blocked, still unregistered) then
    resolves, and the folder is registered under the name typed above."""
    src = _src()
    render = _function("renderInspection")
    assert "result.appendChild(renderSituation({ situation: insp.situation, message: \"\" }, {" in render
    assert "answer: (answer, statusEl, buttons) => answerForAdd(subject, answer, statusEl, buttons)," in render
    situation = src[src.index("function renderSituation(err, opts) {"):]
    situation = situation[: situation.index("\n}\n")]
    assert "const b = mkButton(label, \"btn\", () => answerWith(answer, status, buttons));" in situation
    answer = _function("answerForAdd")
    assert answer.index('apiPost("/api/projects/add", addBody())') < answer.index('apiPost("/api/project/resolve", { answer: answer })')
    assert "if (ANSWER_CONFIRM[answer] && !window.confirm(ANSWER_CONFIRM[answer])) return;" in answer

    original = _research_folder(tmp_path / "original")
    project_identity.init_project(original)
    copy = tmp_path / "copy"
    shutil.copytree(original, copy)
    status, insp = engine.post("/api/projects/inspect", {"path": str(copy)})
    assert status == 200 and insp["kind"] == "rce_project" and insp["can_add"] is False
    assert insp["situation"]["situation"] == "copy" and "fork" in insp["situation"]["answers"]
    copy_id = insp["situation"]["project_id"]
    assert _labels() == []
    status, added = engine.post("/api/projects/add", {"path": insp["path"], "label": "分支", "inspected": insp["inspected"]})
    assert status == 200 and added["blocked"] is not None and added["registered"] is False
    assert _labels() == []  # nothing written, the registry included, until the answer
    status, res = engine.post("/api/project/resolve", {"answer": "fork"})
    assert status == 200 and res["project_id"] != copy_id
    assert _labels() == ["分支"]


def test_a_changed_folder_is_looked_at_again_and_the_dialog_says_why(engine, tmp_path):
    """10.8 #8: a project.toml appears after inspecting -- adding is refused
    (409 inspected_changed with the fresh inspection), nothing is written,
    and the dialog re-renders from the fresh look with the reason above it
    (and its 「详情」)."""
    refused = _function("addRefused")
    assert "if (err.inspection && addDialogLive(subject)) {" in refused
    assert 'addDialog.why = blockingErrorText("没能加入这个文件夹", err);' in refused
    assert "addDialog.whyErr = err;" in refused and "showInspection(subject, err.inspection);" in refused
    assert 'if (addDialog.why) addStatus("err", addDialog.why, addDialog.whyErr);' in _function("showInspection")

    folder = _research_folder(tmp_path / "late")
    _, insp = engine.post("/api/projects/inspect", {"path": str(folder)})
    assert insp["kind"] == "new_folder"
    project_identity.init_project(folder)
    status, body = engine.post("/api/projects/add", {"path": insp["path"], "label": "late", "inspected": insp["inspected"]})
    assert status == 409 and body["state"] == "inspected_changed"
    assert body["message"] == "这个文件夹在你查看之后变了，请重新查看"
    assert body["inspection"]["kind"] == "rce_project" and body["inspection"]["can_add"] is True
    assert _labels() == []


def test_a_waiting_inspection_is_asked_again_every_two_seconds_until_it_changes():
    """10.8 #12 (the page's half): the waiting sentence, then the same path
    asked again every 2 s while the dialog is open and the kind is still
    waiting_permission; nothing hangs."""
    src = _src()
    assert "const ADD_REASK_MS = 2000;" in src
    show = _function("showInspection")
    assert 'if (insp.kind === "waiting_permission") {' in show
    assert "addDialog.timer = setTimeout(() => {" in show and "}, ADD_REASK_MS);" in show
    assert "inspectForAdd(subject, addDialog.path, addDialog.why, addDialog.whyErr);" in show
    inspect = _function("inspectForAdd")
    assert "if (!addDialogLive(subject) || addDialog.path !== path) return; // closed, or another folder chosen meanwhile" in inspect
    assert "RCE 每隔两秒会再问一次，页面不会卡住；也可以换一个文件夹。" in _function("renderInspection")


def test_refusals_are_one_sentence_with_nothing_to_press_but_choose_again(engine, tmp_path):
    """10.8 #7: a refused folder shows its sentence and only 「重新选择」;
    the sentences are the engine's, nothing is written."""
    render = _function("renderInspection")
    refused = render[render.index('if (insp.kind === "refused") {'):]
    refused = refused[: refused.index("    return;\n  }")]
    assert "actions.append(again());" in refused and "cancel()" not in refused
    home = Path.home()  # the engine fixture's throwaway home
    nested = _research_folder(tmp_path / "outer") / "code"
    _, insp = engine.post("/api/projects/inspect", {"path": str(tmp_path / "outer")})
    engine.post("/api/projects/add", {"path": insp["path"], "label": "外层", "inspected": insp["inspected"]})
    engine.wait_scan()
    cases = {
        "~": "请选择具体的项目文件夹，而不是「文稿」这样的总文件夹",
        str(home / "Documents"): "请选择具体的项目文件夹，而不是「文稿」这样的总文件夹",
        str(nested): "这个文件夹在项目「外层」里面",
        str(tmp_path / "nope"): "这个文件夹不存在",
        str(nested / "clean.py"): "这不是一个文件夹",
    }
    registry_before = paths.rce_home().joinpath("projects.json").read_bytes()
    for path, sentence in cases.items():
        status, res = engine.post("/api/projects/inspect", {"path": path})
        assert status == 200 and res["kind"] == "refused" and res["message"] == sentence, path
        assert res["can_add"] is False and res["refusal"]["detail"]
    assert paths.rce_home().joinpath("projects.json").read_bytes() == registry_before
    status, res = engine.post("/api/projects/inspect", {"path": "relative/path"})
    assert status == 400 and res["state"] == "bad_path" and res["message"] == "请输入完整的文件夹路径（以 / 或 ~ 开头）"
