"""DESIGN.md 11.4 (last paragraph) / 11.5 #6 and #7: the 「文献」 view --
`GET /api/citations`, the online setting, 「打开 PDF」 and 「在 Zotero 中打开」,
the candidates confirmed and rejected through the ledger, and the page.
Every test names the 11.5 scenario it belongs to.

The Zotero library is the Zotero-shaped fixture of tests/test_v7_citations.py
(no test reads a real library); `open` is recorded, never run; the network
is a recording stand-in that fails the test if anything is asked.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import urllib.request
from pathlib import Path

import pytest

from rce import literature, paths
from rce import project as project_identity
from rce.ingest import pipeline
from rce.records import ledger as ledger_mod
from rce.records import situation as records_situation
from rce.webapp import literature_api, registry, server

from test_v6_add_project import _live
from test_v6_page import NODE, _src, needs_node
from test_v7_citations import _conn, _scan, zotero  # noqa: F401 -- the Zotero fixture

DRAFT = """# Introduction

Simon（1955）提出有限理性。Jones (2001) again.

Chen & Peng (2010) argue it.

# Data

See doi:10.2307/1884852 for the data.
"""


@pytest.fixture
def no_network(monkeypatch):
    """Anything that would leave the machine is recorded and fails."""
    asked: list[str] = []
    real = urllib.request.urlopen

    def guarded(url, *args, **kwargs):
        full = str(getattr(url, "full_url", url))
        if full.startswith("http://127.0.0.1:"):  # the test's own client, to the engine under test
            return real(url, *args, **kwargs)
        asked.append(full)
        raise AssertionError(f"network asked: {full}")

    monkeypatch.setattr(urllib.request, "urlopen", guarded)
    monkeypatch.setattr(literature, "FETCH", lambda url, timeout: guarded(url))
    return asked


@pytest.fixture
def opened(monkeypatch):
    """`open` recorded, never run; the Zotero program 'installed'."""
    calls: list[list[str]] = []
    monkeypatch.setattr(server, "_is_macos", lambda: True)
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append(list(args)))
    monkeypatch.setattr(literature, "zotero_installed", lambda: True)
    return calls


@pytest.fixture
def lit_server(tmp_path, zotero, no_network):  # noqa: F811
    root = tmp_path / "proj"
    root.mkdir()
    (root / "paper.md").write_text(DRAFT, encoding="utf-8")
    project_identity.init_project(root)
    _scan(root)
    registry.register(Path(paths._canonical_path(root)), records_situation.classify(root).project_id)
    live, thread = _live(root)
    try:
        yield live, root
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()
        thread.join(timeout=5)


def _cites(payload):
    return {(c["surname"] or c["doi"]): c for d in payload["drafts"] for c in d["citations"]}


def _graph_edges(root):
    conn = _conn(root)
    try:
        rows = conn.execute(
            "SELECT src, dst, status, evidence FROM edges WHERE extractor = 'citations' AND type = 'cites'"
        ).fetchall()
    finally:
        conn.close()
    return [(s, d, st, json.loads(ev)) for s, d, st, ev in rows]


# -- 11.5 #6: the summary equals the graph ------------------------------------------------------


def test_11_5_6_summary_counts_equal_the_graph(lit_server):
    live, root = lit_server
    status, data = live.get("/api/citations")
    assert status == 200 and data["scanned"] is True
    assert data["totals"] == {"citations": 4, "resolved": 1, "pending": 2, "unresolved": 1, "unreadable": 0}
    edges = _graph_edges(root)
    # Every link the view names is in the graph with the status it shows, and
    # every `cites` link of the graph is one the view accounts for.
    named = set()
    for c in _cites(data).values():
        froms = [c["section"]] + c["claims"]
        for t in c["targets"]:
            for f in froms:
                named.add((f, t["node"]))
            for link in t["links"]:
                assert (link["src"], link["dst"], link["status"]) in {(s, d, st) for s, d, st, _ in edges}
    assert {(s, d) for s, d, _st, _ev in edges} == named
    occurrences = {st: {(o["line"], o["text"]) for s, d, s2, ev in edges if s2 == st for o in ev["occurrences"]}
                   for st in ("auto", "pending")}
    assert len(occurrences["auto"]) == data["totals"]["resolved"]
    assert len(occurrences["pending"] - occurrences["auto"]) == data["totals"]["pending"]
    # Drafts with the most not yet resolved first; zotero state; setting off.
    assert data["zotero"]["status"] == "read" and data["lookup"] == {"enabled": False}


def test_11_5_6_each_citation_carries_text_reference_how_and_addresses(lit_server, opened):
    live, _root = lit_server
    _, data = live.get("/api/citations")
    cites = _cites(data)
    doi = cites["10.2307/1884852"]
    assert (doi["text"], doi["how"], doi["state"]) == ("10.2307/1884852", "doi", "resolved")
    t = doi["targets"][0]
    assert t["source"] == "zotero" and t["title"] == "A Behavioral Model of Rational Choice"
    assert t["doi_url"] == "https://doi.org/10.2307/1884852"
    assert t["zotero_url"] == "zotero://select/library/items/SIMON55K"
    assert t["pdf"] == {"item_key": "SIMON55K", "attachment_key": "ATTSIM01", "file": "Simon - 1955.pdf"}
    assert t["links"] == [] and t["judged"] is None  # resolved by an identifier: nothing to confirm
    jones = cites["Jones"]
    assert (jones["how"], jones["status"], jones["state"]) == ("zotero_candidates", "pending", "pending")
    assert [x["zotero_key"] for x in jones["targets"]] == ["JONES01A", "JONES01B"]
    assert all(len(x["links"]) == 1 and x["links"][0]["status"] == "pending" for x in jones["targets"])
    assert all(x["pdf"] is None for x in jones["targets"])
    chen = cites["Chen"]
    assert (chen["state"], chen["targets"]) == ("unresolved", [])
    assert data["zotero"]["installed"] is True


def test_11_5_6_confirm_and_reject_a_candidate_one_ledger_entry_each(lit_server):
    live, root = lit_server
    _, data = live.get("/api/citations")
    cites = _cites(data)
    good = cites["Jones"]["targets"][0]["links"][0]
    bad = cites["Simon"]["targets"][0]["links"][0]
    key = {k: good[k] for k in ("src", "dst", "type", "extractor")}
    status, res = live.post("/api/judgements", dict(key, verdict="confirmed"))
    assert status == 200 and res["status"] == "confirmed"
    entries = ledger_mod.load_judgements(root).ledger.entries
    assert [(e.get("verdict"), e.get("dst")) for e in entries] == [("confirmed", "ref:zotero:JONES01A")]
    status, res = live.post("/api/judgements", dict({k: bad[k] for k in key}, verdict="rejected"))
    assert status == 200
    entries = ledger_mod.load_judgements(root).ledger.entries
    assert [(e.get("verdict"), e.get("dst")) for e in entries] == [
        ("confirmed", "ref:zotero:JONES01A"), ("rejected", "ref:doi:10.2307/1884852"),
    ]
    _, after = live.get("/api/citations")
    cites = _cites(after)
    assert cites["Jones"]["state"] == "resolved" and cites["Jones"]["targets"][0]["judged"] == "confirmed"
    assert cites["Jones"]["targets"][1]["judged"] is None
    assert cites["Simon"]["state"] == "unresolved" and cites["Simon"]["targets"][0]["judged"] == "rejected"
    assert after["totals"] == {"citations": 4, "resolved": 2, "pending": 0, "unresolved": 2, "unreadable": 0}
    # The graph agrees after a rescan, and the ledger is untouched by it.
    before = ledger_mod.judgements_path(root).read_bytes()
    _scan(root)
    statuses = {(s, d): st for s, d, st, _ in _graph_edges(root)}
    assert statuses[(good["src"], good["dst"])] == "confirmed" and statuses[(bad["src"], bad["dst"])] == "rejected"
    assert ledger_mod.judgements_path(root).read_bytes() == before


# -- 11.5 #6: 「打开 PDF」 opens only the item's attachment inside Zotero's storage ----------------------


def test_11_5_6_open_pdf_opens_the_database_file_inside_storage(lit_server, opened, zotero):  # noqa: F811
    live, _root = lit_server
    status, res = live.post("/api/zotero/open-attachment", {"item_key": "SIMON55K"})
    assert status == 200 and res["opened"] == "Simon - 1955.pdf"
    storage = (zotero.dir / "storage").resolve()
    assert opened == [["open", str(storage / "ATTSIM01" / "Simon - 1955.pdf")]]
    # A path in the request is never read: the database's file is opened.
    status, _ = live.post("/api/zotero/open-attachment", {"item_key": "SIMON55K", "attachment_key": "ATTSIM01",
                                                           "path": "/etc/passwd"})
    assert status == 200 and opened[-1] == ["open", str(storage / "ATTSIM01" / "Simon - 1955.pdf")]


def _add_attachment(z, parent_key, att_key, *, content_type, path, link_mode=0):
    conn = sqlite3.connect(z.path)
    parent = conn.execute("SELECT itemID FROM items WHERE key = ?", (parent_key,)).fetchone()[0]
    item_id = conn.execute("SELECT max(itemID) FROM items").fetchone()[0] + 1
    conn.execute("INSERT INTO items (itemID, itemTypeID, libraryID, key) VALUES (?, 2, 1, ?)", (item_id, att_key))
    conn.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, linkMode, contentType, path) VALUES (?, ?, ?, ?, ?)",
        (item_id, parent, link_mode, content_type, path),
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("body, code, http", [
    ({}, "bad_key", 400),
    ({"path": "/etc/passwd"}, "bad_key", 400),
    ({"item_key": "../../../etc/passwd"}, "bad_key", 400),
    ({"item_key": "simon55k"}, "bad_key", 400),
    ({"item_key": "SIMON55K", "attachment_key": "../x"}, "bad_key", 400),
    ({"item_key": ["SIMON55K"]}, "bad_key", 400),
    ({"item_key": "NOSUCHIT"}, "no_item", 404),
    ({"item_key": "SIMON55K", "attachment_key": "ATTSOD01"}, "no_attachment", 404),  # another item's
    ({"item_key": "SODER10A"}, "missing", 404),  # named, not on disk
    ({"item_key": "JONES01A"}, "not_pdf", 409),  # no attachment at all
])
def test_11_5_6_open_pdf_refuses_crafted_payloads_and_opens_nothing(lit_server, opened, body, code, http):
    live, _root = lit_server
    status, res = live.post("/api/zotero/open-attachment", body)
    assert (status, res["state"]) == (http, "literature_" + code)
    assert res["message_zh"] and res["detail"]
    assert opened == []


def test_11_5_6_open_pdf_refuses_what_the_database_names_outside_storage(lit_server, opened, zotero, tmp_path):  # noqa: F811
    """A symlinked attachment folder leading out of storage, a name that is
    not a plain file name, a linked (not imported) file, an HTML snapshot
    named .pdf, a file only in the cloud: nothing is opened."""
    live, _root = lit_server
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.pdf").write_bytes(b"%PDF-1.4\n")
    zotero.add("ESCAPE01", title="Escape", date="2001", creators=[("Escape", "E.", 1)])
    _add_attachment(zotero, "ESCAPE01", "ESCATT01", content_type="application/pdf", path="storage:evil.pdf")
    (zotero.dir / "storage").mkdir(exist_ok=True)
    (zotero.dir / "storage" / "ESCATT01").symlink_to(outside, target_is_directory=True)
    zotero.add("DOTDOT01", title="Dots", date="2001", creators=[("Dots", "D.", 1)])
    _add_attachment(zotero, "DOTDOT01", "DOTATT01", content_type="application/pdf", path="storage:../../evil.pdf")
    zotero.add("LINKED01", title="Linked", date="2001", creators=[("Linked", "L.", 1)])
    _add_attachment(zotero, "LINKED01", "LNKATT01", content_type="application/pdf", path=str(outside / "evil.pdf"),
                    link_mode=2)
    zotero.add("HTMLPG01", title="Snapshot", date="2001", creators=[("Html", "H.", 1)])
    _add_attachment(zotero, "HTMLPG01", "HTMATT01", content_type="text/html", path="storage:page.pdf")
    (zotero.dir / "storage" / "HTMATT01").mkdir()
    (zotero.dir / "storage" / "HTMATT01" / "page.pdf").write_text("<html>")
    for key, code in (("ESCAPE01", "outside"), ("DOTDOT01", "not_pdf"), ("LINKED01", "not_pdf"), ("HTMLPG01", "not_pdf")):
        status, res = live.post("/api/zotero/open-attachment", {"item_key": key})
        assert res["state"] == "literature_" + code, key
        assert status in (403, 409)
    real = paths.is_dataless
    try:
        paths.is_dataless = lambda p: str(p).endswith("Simon - 1955.pdf")
        status, res = live.post("/api/zotero/open-attachment", {"item_key": "SIMON55K"})
    finally:
        paths.is_dataless = real
    assert (status, res["state"]) == (409, "literature_cloud")
    assert opened == []


def test_11_5_6_open_pdf_with_no_library_opens_nothing(lit_server, opened, monkeypatch, tmp_path):
    live, _root = lit_server
    monkeypatch.setenv(literature.DATA_DIR_ENV_VAR, str(tmp_path / "no-zotero"))
    status, res = live.post("/api/zotero/open-attachment", {"item_key": "SIMON55K"})
    assert (status, res["state"], res["message_zh"]) == (409, "literature_library", "没能读取 Zotero 文献库")
    assert opened == []


# -- 「在 Zotero 中打开」 ------------------------------------------------------------------------------


def test_11_5_6_open_in_zotero_only_a_validated_key_and_only_when_installed(lit_server, opened, monkeypatch):
    live, _root = lit_server
    status, res = live.post("/api/zotero/open-item", {"item_key": "SIMON55K"})
    assert status == 200 and opened == [["open", "zotero://select/library/items/SIMON55K"]]
    for body, code in (({"item_key": "x;rm -rf"}, "bad_key"), ({"item_key": "NOSUCHIT"}, "no_item"), ({}, "bad_key")):
        status, res = live.post("/api/zotero/open-item", body)
        assert res["state"] == "literature_" + code
    monkeypatch.setattr(literature, "zotero_installed", lambda: False)
    status, res = live.post("/api/zotero/open-item", {"item_key": "SIMON55K"})
    assert (status, res["state"]) == (409, "literature_no_zotero")
    assert opened == [["open", "zotero://select/library/items/SIMON55K"]]
    _, data = live.get("/api/citations")
    assert data["zotero"]["installed"] is False


def test_11_5_6_zotero_installed_asks_spotlight_and_launches_nothing(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="/Applications/Zotero.app\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(literature, "_installed_cache", None)
    monkeypatch.setattr(literature.sys, "platform", "darwin")
    assert literature.zotero_installed() is True
    assert calls == [["mdfind", "kMDItemCFBundleIdentifier == 'org.zotero.zotero'"]]
    assert literature.zotero_installed() is True and len(calls) == 1  # cached
    monkeypatch.setattr(literature, "_installed_cache", None)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    assert literature.zotero_installed() is False
    monkeypatch.setattr(literature, "_installed_cache", None)
    monkeypatch.setattr(literature.sys, "platform", "linux")
    assert literature.zotero_installed() is False


def test_11_5_6_zotero_select_url_from_a_valid_key_only():
    assert literature.zotero_select_url("ABCD2345") == "zotero://select/library/items/ABCD2345"
    for bad in ("abcd2345", "ABCD234", "ABCD/345", "ABCD23456", "", None):
        with pytest.raises(ValueError):
            literature.zotero_select_url(bad)


# -- the online setting: nothing leaves while off -----------------------------------------------------


def test_11_5_5_setting_toggled_and_nothing_asked_while_off(lit_server, no_network):
    live, root = lit_server
    status, res = live.post("/api/citations/lookup-setting", {"on": True})
    assert status == 200 and res["lookup"] == {"enabled": True}
    settings = json.loads(literature.settings_path().read_text(encoding="utf-8"))
    assert settings == {"doi_online_lookup": True}  # the switch, nothing else
    _, data = live.get("/api/citations")
    assert data["lookup"] == {"enabled": True} and no_network == []  # the view never asks
    status, res = live.post("/api/citations/lookup-setting", {"on": False})
    assert status == 200 and res["lookup"] == {"enabled": False}
    _scan(root)  # a scan while off asks nothing either
    assert no_network == []
    status, res = live.post("/api/citations/lookup-setting", {"on": "yes"})
    assert (status, res["state"]) == (400, "literature_bad_setting")
    assert literature.doi_lookup_enabled() is False


# -- 11.5 #7: origin first; nothing written that is a secret --------------------------------------------


@pytest.mark.parametrize("method, endpoint", [
    ("GET", "/api/citations"), ("POST", "/api/citations/lookup-setting"),
    ("POST", "/api/zotero/open-attachment"), ("POST", "/api/zotero/open-item"),
])
@pytest.mark.parametrize("headers", [{"Origin": "http://evil.example"}, {"Host": "evil.example:80"}])
def test_11_5_7_every_new_endpoint_refuses_cross_origin(lit_server, opened, method, endpoint, headers):
    live, _root = lit_server
    sent = {"Host": f"127.0.0.1:{live.port}", "Content-Type": "application/json", **headers}
    status, _ = live.raw(method, endpoint, sent, {"on": True, "item_key": "SIMON55K"})
    assert status == 403
    assert opened == [] and literature.doi_lookup_enabled() is False


def test_11_5_6_with_no_drafts_and_before_any_scan(tmp_path, no_network):
    root = tmp_path / "empty"
    root.mkdir()
    project_identity.init_project(root)
    conn = _conn(root)
    try:
        from rce import db

        db.migrate(conn)
        data = literature_api.citations_payload(conn, root)
        assert data["scanned"] is False and data["drafts"] == [] and data["totals"]["citations"] == 0
        with records_situation.write_guard(root):
            pipeline.ingest_sources(conn, root)
        data = literature_api.citations_payload(conn, root)
        assert data["scanned"] is True and data["drafts"] == []
    finally:
        conn.close()


# -- the page -------------------------------------------------------------------------------------------

_LIT_RUNNER = r"""
const block = require("fs").readFileSync(0, "utf8");
eval(block + "; global.W = { litSummaryText, litDraftCountText, litHowText, litBadge, litRefText, litZoteroText, litLinksFor };");
const calls = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(calls.map(([fn, args]) => W[fn](...args))));
"""


def _wording(*calls):
    src = _src()
    start = src.index("// -- Literature wording (pure; 11.4)")
    block = src[start:src.index("// -- end of literature wording", start)]
    result = subprocess.run([NODE, "-e", _LIT_RUNNER, json.dumps(list(calls))], input=block,
                            capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


@needs_node
def test_11_5_6_page_wording_summary_how_and_badges():
    zot = {"source": "zotero", "title": "T", "authors": ["A", "B", "C", "D"], "year": 1955, "venue": "QJE"}
    out = _wording(
        ("litSummaryText", [{"citations": 1629, "resolved": 483, "pending": 2, "unresolved": 1144}]),
        ("litDraftCountText", [{"citations": 3, "resolved": 1, "pending": 0, "unresolved": 2}]),
        ("litHowText", [{"how": "doi"}, zot]),
        ("litHowText", [{"how": "doi"}, {"source": "crossref"}]),
        ("litHowText", [{"how": "doi"}, {"source": None}]),
        ("litHowText", [{"how": "entry"}, {"source": "entry"}]),
        ("litHowText", [{"how": "entry"}, zot]),
        ("litHowText", [{"how": "zotero_candidates"}, {"source": "zotero", "judged": "confirmed"}]),
        ("litBadge", [{"state": "resolved", "targets": [{}]}]),
        ("litBadge", [{"state": "pending", "targets": [{}, {}]}]),
        ("litBadge", [{"state": "unresolved", "targets": []}]),
        ("litBadge", [{"state": "unresolved", "targets": [{"judged": "rejected"}]}]),
        ("litRefText", [zot]),
        ("litRefText", [{"source": "entry", "entry_text": "Simon, H. (1955). A model."}]),
        ("litRefText", [{"source": None, "doi": "10.1/x"}]),
        ("litZoteroText", [{"status": "absent"}]),
        ("litLinksFor", [{"links": [{"status": "pending"}, {"status": "confirmed"}]}, "confirmed"]),
        ("litLinksFor", [{"links": [{"status": "pending"}, {"status": "confirmed"}]}, "undone"]),
    )
    assert out[0] == "1629 处引用：已对上 483，待确认 2，未找到 1144"
    assert out[1] == "3 处 · 已对上 1 · 未找到 2"
    assert out[2:8] == ["Zotero", "DOI 联网", "文中 DOI", "文末条目", "文末条目 · Zotero", "Zotero · 你已确认"]
    assert [b["text"] for b in out[8:12]] == ["已对上", "2 个候选，待确认", "未找到", "未找到（候选都已否决）"]
    assert out[12] == "A、B、C 等 · 1955 · T · QJE"
    assert out[13] == "Simon, H. (1955). A model." and out[14] == "DOI 10.1/x"
    assert out[15] == "没有找到 Zotero 文献库：只用文稿自己的参考文献和 DOI 对照"
    assert out[16] == [{"status": "pending"}] and out[17] == [{"status": "confirmed"}]


def test_11_5_6_page_tab_setting_lazy_drafts_and_the_one_write_path():
    src = _src()
    assert '<button class="tab" data-view="literature" role="tab" aria-selected="false">文献</button>' in src
    assert '<section id="view-literature" class="view hidden"></section>' in src
    start = src.index("// -- 文献 (DESIGN.md 11.4")
    view = src[start:src.index("// -- end of the 文献 view", start)]
    assert 'apiGet("/api/citations")' in view
    assert "用 DOI 联网查文献信息" in view and "只发送 DOI，不发送文稿内容" in view
    assert 'apiPost("/api/citations/lookup-setting", { on: wanted })' in view
    # Judgments only through the one write path; openers take keys, never a path.
    assert "postJudgement(link, verdict)" in view
    assert set(__import__("re").findall(r'"(/api/[\w/-]+)"', view)) == {
        "/api/citations", "/api/citations/lookup-setting", "/api/zotero/open-item", "/api/zotero/open-attachment",
    }
    assert "{ item_key: t.pdf.item_key, attachment_key: t.pdf.attachment_key }" in view and "path:" not in view
    assert 'a.rel = "noopener noreferrer";' in view and "innerHTML = \"\"" in view and ".innerHTML = d" not in view
    # 在 Zotero 中打开 only when installed; 打开 PDF only for an attachment on this machine.
    assert "if (t.zotero_url && data.zotero && data.zotero.installed)" in view and "if (t.pdf)" in view
    # Lazy: a draft's citations are built only when it is opened.
    draft = view[view.index("function renderLitDraft("):view.index("function renderLitCitation(")]
    assert "if (opened) fill();" in draft and "litState.open.add(d.file); fill();" in draft
    # The empty states.
    for text in ("还没有读过文稿里的引用", "这个项目里没有文稿", "文稿里没有找到引用"):
        assert text in view
