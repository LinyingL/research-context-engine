"""Variable cards in the app, and the review of a moved implementation
(DESIGN.md 9.11 "In the app", "When the implementation moves", "Nothing
changed always says how far it looked"; task V5 phase 9): the stage-(b)
comparison and its coverage, 「口径未变」 / 「全部口径未变」 / 「口径已变」,
「完整比对」, the 「变量」 endpoints (origin-checked, path-confined, writing
only what RCE authors), the watcher noticing a moved script, and the 「变量」
view's copy. Acceptance 17 of 9.11 is named in each test that covers it."""

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

from rce import cli, db
from rce.records import cards, implementation
from rce.records import variables as V
from rce.records.situation import index_db_path
from rce.webapp import server

from test_variable_cards import _project, _text, _write

APP_HTML = Path(server.__file__).parent / "app.html"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

RMD = """---
title: build
---

Prose with a # that is not code.

```{r setup, include=FALSE}
library(readr)
```

```{r build}
# 构建
df <- read_csv("Data/theme.csv")
write_csv(df, "Data/ts.csv")
```
"""

PY_FN = '''import pandas as pd


def topicshift(df):
    """The construction."""
    return df.diff()


def other(df):
    return df


pd.read_csv("Data/theme.csv").to_csv("Data/ts.csv")
'''


# -- helpers ----------------------------------------------------------------------------------


def _apply(root: Path) -> dict[str, Any]:
    """What every scan's last step does for the cards: apply them (and run
    stage (b)) under the project lock; returns the review list's card part."""
    ident = cards._identity_now(root)
    conn = db.connect(index_db_path(ident.id))
    try:
        cards.apply_cards(conn, root, identity=ident)
        return implementation.review_groups(conn)
    finally:
        conn.close()


def _stored(root: Path) -> dict[str, Any]:
    conn = db.connect(index_db_path(cards._identity_now(root).id))
    try:
        return implementation.stored(conn)
    finally:
        conn.close()


def _confirmed(root: Path, card: str, text: str) -> None:
    cards.new_card(root, card)
    _write(root, card, 1, text)
    cards.confirm(root, card, attested="yes")


def _two_on_one_script(tmp_path: Path) -> Path:
    root = tmp_path / "p"
    _project(root)
    _confirmed(root, "topicshift", _text())
    _confirmed(root, "rv", _text(field="rv").replace("TopicShift（叙事更替）", "RV"))
    assert _apply(root)["groups"] == []
    return root


def _entries(root: Path, card: str) -> list[dict[str, Any]]:
    return [dict(e.data) for e in V.open_card(root, card).entries]


# -- 17: the implementation moves ----------------------------------------------------------------


def test_scenario_17_a_comment_edit_raises_nothing_and_says_how_far_it_looked(tmp_path):
    """9.11 #17: edit a comment in the script -- nothing; and the view says
    「全文未变」, never a bare "unchanged"."""
    root = _two_on_one_script(tmp_path)
    script = root / "build.py"
    script.write_text("# a new comment\n" + script.read_text().replace("\n", "  # why\n", 1) + "\n\n")
    assert _apply(root)["groups"] == []
    review = _stored(root)["topicshift"]
    assert review["under_review"] is False
    assert review["script"]["state"] == "same" and review["script"]["text"] == "全文未变"
    assert review["inputs"][0]["text"] == "全文未变"


def test_scenario_17_a_code_change_is_one_item_naming_every_card_on_the_script(tmp_path):
    """9.11 #17: change the code -- ONE review item for the script, naming
    every card that points at it, with the design's wording."""
    root = _two_on_one_script(tmp_path)
    (root / "build.py").write_text((root / "build.py").read_text() + "df = df.dropna()\n")
    groups = _apply(root)
    assert groups["count"] == 1
    (group,) = groups["groups"]
    assert group["script"] == "build.py" and group["message"] == "此脚本的改动涉及 2 个变量"
    assert sorted(c["card"] for c in group["cards"]) == ["rv", "topicshift"]
    for member in group["cards"]:
        assert [r["label"] for r in member["reasons"]] == ["实现脚本在确认后有改动"]
        assert member["cannot_tell"] == implementation.CANNOT_TELL
    assert groups["cannot_tell"].startswith("RCE 只看得出")


def test_scenario_17_all_unchanged_appends_one_reaffirmed_per_card(tmp_path):
    """9.11 #17: 「全部口径未变」 appends one `reaffirmed` per card, with the
    new fingerprints and the coverage they were taken under; the item goes."""
    root = _two_on_one_script(tmp_path)
    (root / "build.py").write_text((root / "build.py").read_text() + "df = df.dropna()\n")
    (group,) = _apply(root)["groups"]
    for member in group["cards"]:
        cards.reaffirm(root, member["card"], version=member["version"], signature=member["signature"], via="app")
    for card in ("topicshift", "rv"):
        acts = [e["act"] for e in _entries(root, card)]
        assert acts == ["confirmed", "reaffirmed"]
        entry = _entries(root, card)[-1]
        assert entry["reasons"] == ["script_changed"] and entry["via"] == "app"
        assert entry["coverage"]["script"] == "full"
        assert entry["checked"]["script"]["code"].startswith("sha256:")
        assert (V.variables_dir(root) / entry["checked"]["script"]["copy"]).is_file()  # copy first, entry last
        assert entry["observed"]["inputs"][0]["sha256"]
    assert _apply(root)["groups"] == []
    assert _stored(root)["rv"]["baseline_act"] == "reaffirmed"


def test_scenario_17_changed_on_one_opens_its_next_draft(tmp_path):
    """9.11 #17: 「口径已变」 on one card opens its next draft; that card then
    waits on the draft (not counted), the other is still asked."""
    root = _two_on_one_script(tmp_path)
    (root / "build.py").write_text((root / "build.py").read_text() + "df = df.dropna()\n")
    _apply(root)
    path = cards.revise(root, "rv")
    assert path.name == "v2.toml" and V.open_card(root, "rv").draft == 2
    groups = _apply(root)
    (group,) = groups["groups"]
    by_card = {m["card"]: m for m in group["cards"]}
    assert by_card["rv"]["counted"] is False and by_card["rv"]["draft_note"].startswith("已打开草稿 v2")
    assert by_card["topicshift"]["counted"] is True and groups["count"] == 1


def test_a_stale_answer_is_refused_and_writes_nothing(tmp_path):
    """9.12 applied to stage (b): an answer belongs to the comparison shown."""
    root = _two_on_one_script(tmp_path)
    (root / "build.py").write_text((root / "build.py").read_text() + "df = df.dropna()\n")
    (group,) = _apply(root)["groups"]
    shown = group["cards"][0]
    (root / "build.py").write_text((root / "build.py").read_text() + "df = df.fillna(0)\n")
    before = _entries(root, shown["card"])
    with pytest.raises(cards.CardRefused) as err:
        cards.reaffirm(root, shown["card"], version=shown["version"], signature=shown["signature"])
    assert err.value.code == "question_changed"
    assert _entries(root, shown["card"]) == before
    with pytest.raises(cards.CardRefused) as err:
        cards.reaffirm(root, shown["card"], version=9, signature=shown["signature"])
    assert err.value.code == "no_question"


def _rmd_card(tmp_path: Path) -> Path:
    root = tmp_path / "p"
    _project(root)
    (root / "build.Rmd").write_text(RMD)
    text = _text(script="build.Rmd").replace('field = "topicshift"\n', 'field = "topicshift"\nchunk = "build"\n')
    _confirmed(root, "topicshift", text)
    assert _apply(root)["groups"] == []
    return root


def test_scenario_17_a_change_outside_the_named_chunk_raises_nothing(tmp_path):
    """9.11 #17: with `chunk` named, a change outside the chunk raises
    nothing -- and the view says 「指定范围未变（范围外未比对）」."""
    root = _rmd_card(tmp_path)
    rmd = root / "build.Rmd"
    rmd.write_text(rmd.read_text().replace("library(readr)", "library(readr)\nlibrary(dplyr)"))
    assert _apply(root)["groups"] == []
    script = _stored(root)["topicshift"]["script"]
    assert script["text"] == "指定范围未变（范围外未比对）" and script["region"] == "chunk:build"
    rmd.write_text(rmd.read_text().replace('write_csv(df', 'df <- df[-1, ]\nwrite_csv(df'))
    (group,) = _apply(root)["groups"]
    assert group["cards"][0]["reasons"][0]["label"] == "实现脚本在确认后有改动"


def test_scenario_17_a_removed_chunk_raises_its_reason(tmp_path):
    """9.11 #17: a removed chunk raises 「找不到该代码块」; 「口径未变」 cannot
    settle it (the card names the chunk) and says what can."""
    root = _rmd_card(tmp_path)
    rmd = root / "build.Rmd"
    rmd.write_text(rmd.read_text().replace("```{r build}", "```{r construct}"))
    (group,) = _apply(root)["groups"]
    member = group["cards"][0]
    assert [r["label"] for r in member["reasons"]] == ["找不到该代码块"]
    assert member["script"]["text"] == "找不到该代码块"
    with pytest.raises(cards.CardRefused) as err:
        cards.reaffirm(root, "topicshift", version=1, signature=member["signature"])
    assert err.value.code == "region_missing" and "这是更正" in err.value.message_zh


def test_a_named_function_is_located_exactly(tmp_path):
    """9.11: `function` narrows the comparison like `chunk`: a change in
    another function raises nothing; a removed one raises 「找不到该函数」."""
    root = tmp_path / "p"
    _project(root)
    (root / "build.py").write_text(PY_FN)
    text = _text().replace('field = "topicshift"\n', 'field = "topicshift"\nfunction = "topicshift"\n')
    _confirmed(root, "topicshift", text)
    (root / "build.py").write_text(PY_FN.replace("return df\n", "return df.copy()\n"))
    assert _apply(root)["groups"] == []
    assert _stored(root)["topicshift"]["script"]["text"] == "指定范围未变（范围外未比对）"
    (root / "build.py").write_text(PY_FN.replace("def topicshift(df)", "def topic_shift(df)"))
    (group,) = _apply(root)["groups"]
    assert [r["label"] for r in group["cards"][0]["reasons"]] == ["找不到该函数"]


def test_scenario_17_an_unreadable_script_raises_nothing(tmp_path):
    """9.11 #17: make the script unreadable -- nothing comes under review;
    the view says it was not compared."""
    root = _two_on_one_script(tmp_path)
    script = root / "build.py"
    script.write_text(script.read_text() + "df = df.dropna()\n")
    script.chmod(0)
    try:
        if os.access(script, os.R_OK):
            pytest.skip("running as a user who can read anything")
        assert _apply(root)["groups"] == []
        assert _stored(root)["rv"]["script"]["text"] == "暂时无法读取，未比对"
    finally:
        script.chmod(0o644)
    script.unlink()
    assert _apply(root)["groups"] == []


def test_scenario_17_a_large_input_is_compared_by_size_and_full_compare_finds_the_change(tmp_path, monkeypatch):
    """9.11 #17: an input above the size threshold whose bytes change at the
    same size: 「大小未变（内容未比对）」 (with its mtime kept) and 「修改时间
    变化，内容未比对」 (with it moved) -- never an unqualified "unchanged" --
    and 「完整比对」 then finds the change; 「口径未变」 then needs the new
    data_version note."""
    monkeypatch.setattr(V, "LARGE_FILE_BYTES", 16)  # the threshold, injectable
    root = _two_on_one_script(tmp_path)
    data = root / "Data" / "theme.csv"
    before = data.stat()
    data.write_text(data.read_text().replace("a,3", "b,4"))
    os.utime(data, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert data.stat().st_size == before.st_size
    assert _apply(root)["groups"] == []
    shown = _stored(root)["topicshift"]["inputs"][0]
    assert shown["text"] == "大小未变（内容未比对）" and shown["large"] is True
    os.utime(data, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
    _apply(root)
    assert _stored(root)["topicshift"]["inputs"][0]["text"] == "修改时间变化，内容未比对"

    review = cards.full_compare(root, "topicshift")
    assert review["under_review"] and [r["label"] for r in review["reasons"]] == ["输入数据在确认后有变化"]
    assert review["needs_data_version"] is True
    # The hash stays known while the file's size and mtime do: a later scan
    # still sees the change (it is not lost on the next application) -- for
    # both cards built on that input, one item each (only a script groups).
    groups = _apply(root)
    assert groups["count"] == 2 and sorted(g["key"] for g in groups["groups"]) == ["card:rv", "card:topicshift"]
    with pytest.raises(cards.CardRefused) as err:
        cards.reaffirm(root, "topicshift", version=1, signature=review["signature"])
    assert err.value.code == "incomplete" and "数据版本" in err.value.message_zh
    entry = cards.reaffirm(root, "topicshift", version=1, signature=review["signature"],
                           data_version="2017-01 至 2024-12，2026-10 重新下载")
    assert entry.get("data_version").startswith("2017-01") and entry.get("reasons") == ["input_changed"]
    assert entry.get("coverage")["inputs"] == [{"dataset": "Data/theme.csv", "coverage": "full"}]


def test_a_confirmation_hashes_a_large_input_so_a_full_compare_has_something_to_compare(tmp_path, monkeypatch):
    """The scan compares a large input by size; the confirmation (an explicit
    act) records its content hash too, beside its size and mtime."""
    monkeypatch.setattr(V, "LARGE_FILE_BYTES", 16)
    root = tmp_path / "p"
    _project(root)
    _confirmed(root, "topicshift", _text())
    (inp,) = _entries(root, "topicshift")[0]["observed"]["inputs"]
    assert {"sha256", "size", "mtime"} <= set(inp)
    assert "code" in _entries(root, "topicshift")[0]["checked"]["script"]


def test_a_card_whose_version_was_edited_compares_the_definition_in_force(tmp_path):
    """The frozen text is what is in force while the edit is asked about:
    the comparison follows it, not the edited file."""
    root = tmp_path / "p"
    _project(root)
    _confirmed(root, "topicshift", _text())
    _write(root, "topicshift", 1, _text(script="elsewhere.py"))
    assert V.open_card(root, "topicshift").questions == [1]
    _apply(root)
    assert _stored(root)["topicshift"]["script"]["path"] == "build.py"


def test_rce_review_prints_the_cards_too(tmp_path, capsys):
    """9.6: `rce review` prints the same list the app shows."""
    root = _two_on_one_script(tmp_path)
    (root / "build.py").write_text((root / "build.py").read_text() + "df = df.dropna()\n")
    capsys.readouterr()
    assert cli.main(["review", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Variable cards whose implementation moved: 1" in out
    assert "build.py: 2 card(s)" in out and "rv v1" in out and "script_changed" in out


# -- the watcher -------------------------------------------------------------------------------


def test_the_watcher_notices_a_moved_script_and_compares_again(tmp_path):
    """The implementing script and inputs join the watch set: a change
    re-applies the record (no ingest), and the item appears."""
    root = _two_on_one_script(tmp_path)
    httpd = server.build_server(root, 0, watch_interval=0.05)
    try:
        watcher = httpd.watcher
        assert watcher.poll_once() in (False, True)  # baseline
        (root / "build.py").write_text((root / "build.py").read_text() + "df = df.dropna()\n")
        assert watcher.poll_once() is True
        assert watcher.status_payload()["last_error"] is None
        assert _apply(root)["count"] == 1
    finally:
        httpd.server_close()


# -- the endpoints -------------------------------------------------------------------------------


@pytest.fixture
def live(tmp_path):
    root = tmp_path / "p"
    _project(root)
    _confirmed(root, "topicshift", _text())
    httpd = server.build_server(root, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", root
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call(base: str, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None):
    parsed = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
    data = None if body is None else json.dumps(body).encode("utf-8")
    try:
        conn.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, json.loads(raw) if raw else None
    finally:
        conn.close()


def test_the_list_and_the_card_read_everything_the_view_shows(live):
    base, root = live
    cards.new_card(root, "rv")
    status, data = _call(base, "GET", "/api/variables")
    assert status == 200
    rows = {r["id"]: r for r in data["cards"]}
    assert rows["topicshift"]["name"] == "TopicShift（叙事更替）" and rows["topicshift"]["in_use"] == 1
    assert rows["rv"]["draft"] == 1 and rows["rv"]["in_use"] is None
    assert data["empty_help"]["command"] == "rce variable new <id>"
    status, card = _call(base, "GET", "/api/variables/card?id=TOPICSHIFT")
    assert status == 200 and card["id"] == "topicshift"
    (v1,) = card["versions"]
    assert v1["status"] == "in_use" and v1["attested"] == "yes"
    assert v1["checked"]["script"]["result"] == "已核对" and v1["checked"]["reads"][0]["result"] == "已核对"
    assert v1["observed"]["output"]["path"] == "Data/ts.csv"
    assert v1["reference"]["resolves"] is True and v1["reference"]["label"].startswith("topicshift@v1·")
    assert [h["act"] for h in card["history"]] == ["confirmed"]
    assert card["implementation"]["script"]["text"] == "全文未变"
    status, missing = _call(base, "GET", "/api/variables/card?id=nope")
    assert status == 404 and missing["state"] == "card_no_such_card" and missing["message_zh"]


def test_the_kept_code_and_the_frozen_copy_are_served_as_text(live):
    """「查看当时的代码」 and the frozen copy, by the log entry that names them."""
    base, root = live
    entry = _entries(root, "topicshift")[0]
    (root / "build.py").write_text("print('changed since')\n")
    status, code = _call(base, "GET", f"/api/variables/code?id=topicshift&entry={entry['id']}")
    assert status == 200 and 'pd.read_csv("Data/theme.csv")' in code["text"] and code["truncated"] is False
    status, frozen = _call(base, "GET", f"/api/variables/frozen?id=topicshift&entry={entry['id']}")
    assert status == 200 and "TS_t = 1 − cos" in frozen["text"]
    status, bad = _call(base, "GET", "/api/variables/code?id=topicshift&entry=v-nope")
    assert status == 404


def test_a_copy_that_leads_outside_its_folder_is_refused(live, tmp_path):
    """Resolve, then relative_to: a code copy replaced by a symlink to a file
    outside `.rce/variables/_code/` is not read."""
    base, root = live
    entry = _entries(root, "topicshift")[0]
    copy = V.variables_dir(root) / entry["checked"]["script"]["copy"]
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    copy.unlink()
    copy.symlink_to(secret)
    status, data = _call(base, "GET", f"/api/variables/code?id=topicshift&entry={entry['id']}")
    assert status == 400 and "outside" not in json.dumps(data.get("text", ""))
    assert data["state"] == "card_invalid"


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/api/variables", None),
    ("GET", "/api/variables/card?id=topicshift", None),
    ("GET", "/api/variables/code?id=topicshift&entry=x", None),
    ("GET", "/api/variables/frozen?id=topicshift&entry=x", None),
    ("POST", "/api/variables/confirm", {"id": "topicshift", "attested": "yes"}),
    ("POST", "/api/variables/revise", {"id": "topicshift"}),
    ("POST", "/api/variables/reaffirm", {"items": [{"id": "topicshift", "version": 1, "signature": "x"}]}),
    ("POST", "/api/variables/abandon", {"id": "topicshift", "note": "x"}),
    ("POST", "/api/variables/revive", {"id": "topicshift", "note": "x"}),
    ("POST", "/api/variables/answer", {"id": "topicshift", "question": "edited", "answer": "new", "version": 1}),
    ("POST", "/api/variables/full-compare", {"id": "topicshift"}),
])
def test_every_variables_endpoint_refuses_a_foreign_origin_and_a_wrong_host(live, method, path, body):
    base, root = live
    before = sorted(p.read_bytes() for p in V.variables_dir(root).rglob("*") if p.is_file())
    status, data = _call(base, method, path, body, {"Origin": "http://evil.example"})
    assert status == 403 and "Origin" in data["error"]
    status, data = _call(base, method, path, body, {"Host": "evil.example"})
    assert status == 403 and "Host" in data["error"]
    assert sorted(p.read_bytes() for p in V.variables_dir(root).rglob("*") if p.is_file()) == before


def test_the_app_writes_only_what_rce_authors(live):
    """Confirm (with the attestation), revise, abandon / revive with a note:
    each one entry in the log; the researcher's v<n>.toml is never written
    (revise copies it to the next number, exclusively)."""
    base, root = live
    v1 = (V.variables_dir(root) / "topicshift" / "v1.toml").read_bytes()
    status, data = _call(base, "POST", "/api/variables/revise", {"id": "topicshift"})
    assert status == 200 and data["version"] == 2 and data["file"] == ".rce/variables/topicshift/v2.toml"
    status, data = _call(base, "POST", "/api/variables/revise", {"id": "topicshift"})
    assert status == 409 and data["state"] == "card_draft_open" and data["message_zh"] == "已有草稿 v2，请先确认它"
    _write(root, "topicshift", 2, _text(formula="TS_t = JS(p_t, p_{t−1})"))
    status, data = _call(base, "POST", "/api/variables/confirm", {"id": "topicshift", "attested": "maybe"})
    assert status == 400 and data["state"] == "card_invalid"
    shown = [v for v in _call(base, "GET", "/api/variables/card?id=topicshift")[1]["versions"] if v["version"] == 2][0]
    status, data = _call(base, "POST", "/api/variables/confirm", {"id": "topicshift", "attested": "no"})
    assert status == 400 and data["state"] == "card_invalid"  # not tied to the draft the page showed
    status, data = _call(base, "POST", "/api/variables/confirm",
                         {"id": "topicshift", "attested": "no", "content_hash": shown["content_hash"]})
    assert status == 200 and data["version"] == 2
    status, data = _call(base, "POST", "/api/variables/abandon", {"id": "topicshift", "note": "  "})
    assert status == 400 and data["message_zh"] == "请写下理由"
    assert _call(base, "POST", "/api/variables/abandon", {"id": "topicshift", "note": "被 JS 散度版取代"})[0] == 200
    assert _call(base, "POST", "/api/variables/revive", {"id": "topicshift", "note": "又用上了"})[0] == 200
    log = _entries(root, "topicshift")
    assert [e["act"] for e in log] == ["confirmed", "confirmed", "abandoned", "revived"]
    assert {e["via"] for e in log[1:]} == {"app"} and log[1]["attested"] == "no"
    assert (V.variables_dir(root) / "topicshift" / "v1.toml").read_bytes() == v1
    status, data = _call(base, "POST", "/api/variables/new", {"id": "x"})
    assert status == 400 and data["state"] == "card_invalid"
    assert not (V.variables_dir(root) / "x").exists()


def test_the_edited_question_and_the_review_are_answered_from_the_app(live):
    base, root = live
    _write(root, "topicshift", 1, _text(formula="TS_t = 1 − cos(p_t, p_{t-1})  (typo fixed)"))
    status, card = _call(base, "GET", "/api/variables/card?id=topicshift")
    assert card["questions"][0]["message"] == "v1 的定义在确认后被改动了"
    assert card["questions"][0]["answer_labels"] == {"new": "另存为新版本", "correct": "这是更正"}
    status, data = _call(base, "POST", "/api/variables/answer",
                         {"id": "topicshift", "question": "edited", "answer": "correct", "version": 1,
                          "content_hash": card["questions"][0]["content_hash"]})
    assert status == 200 and data["entry"]
    (root / "build.py").write_text((root / "build.py").read_text() + "df = df.dropna()\n")
    _apply(root)
    status, review = _call(base, "GET", "/api/review")
    assert review["count"] == 1 and review["cards"]["groups"][0]["message"] == "此脚本的改动涉及 1 个变量"
    member = review["cards"]["groups"][0]["cards"][0]
    status, summary = _call(base, "GET", "/api/summary")
    assert summary["review"] == 1
    status, data = _call(base, "POST", "/api/variables/reaffirm",
                         {"items": [{"id": "topicshift", "version": member["version"], "signature": member["signature"]}]})
    assert status == 200 and len(data["done"]) == 1 and isinstance(data["generation"], int)
    assert _call(base, "GET", "/api/review")[1]["count"] == 0
    assert [e["act"] for e in _entries(root, "topicshift")] == ["confirmed", "corrected", "reaffirmed"]


def test_a_card_on_a_read_only_or_moved_project_writes_nothing(live, tmp_path):
    """The server's write guard covers the card actions: a folder moved
    under the page is refused with `project_moved`, nothing written."""
    base, root = live
    moved = tmp_path / "moved"
    root.rename(moved)
    status, data = _call(base, "POST", "/api/variables/abandon", {"id": "topicshift", "note": "x"})
    assert status == 409 and data["state"] == "project_moved"
    assert [e["act"] for e in _entries(moved, "topicshift")] == ["confirmed"]


# -- the page --------------------------------------------------------------------------------------


def test_the_page_carries_the_variables_view_in_product_language(live):
    """9.11 "In the app": the fourth tab, the card page's wording, the
    review's answers, the empty state -- all Chinese, no form for the
    researcher's text."""
    base, _ = live
    parsed = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
    conn.request("GET", "/")
    html = conn.getresponse().read().decode("utf-8")
    conn.close()
    assert 'data-view="variables"' in html and ">变量</button>" in html
    for copy in (
        "草稿 · 改动不留版本", "在编辑器中打开", "确认这一版", "修订口径", "弃用", "恢复", "查看当时的代码",
        "磁盘上的输出文件：", "你当时的说明：输出按此口径生成 — ", "口径未变", "口径已变", "全部口径未变",
        "完整比对", "数据版本说明", "已核对", "不符", "未核对",
    ):
        assert copy in html, copy
    assert '"variables": () => activateView("variables")' in html
    view = html[html.index("// -- 变量 (9.11"):html.index("// -- end of the 变量 view")]
    assert "<textarea" not in view and "form-textarea" not in view  # no form for the researcher's text
    assert "renderBlockingError" in view or "setStatus(" in view
    assert ".innerHTML = " not in view.replace('panelBody.innerHTML = ""', "").replace('host.innerHTML = ""', "")


_WORDING_RUNNER = r"""
const block = require("fs").readFileSync(0, "utf8");
eval(block + "; global.W = { attestedText, checkLine, observedLine, coverageLine };");
const calls = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(calls.map(([fn, args]) => W[fn](...args))));
"""


def _wording(*calls: tuple[str, list[Any]]) -> list[Any]:
    html = APP_HTML.read_text(encoding="utf-8")
    block = html[html.index("// -- Variable wording (pure"):html.index("// -- end of variable wording")]
    result = subprocess.run([NODE, "-e", _WORDING_RUNNER, json.dumps(list(calls))],
                            input=block, capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


@needs_node
def test_the_attestation_is_the_researchers_statement_and_a_yes_beside_a_mismatch_is_shown_as_such():
    """9.11: the attestation is worded as the researcher's own statement; an
    observation as an observation; a 是 standing beside a 不符 is shown as
    exactly that, not resolved."""
    yes, unknown, beside, obs, check, unchecked = _wording(
        ("attestedText", ["yes", {"writes": {"result": "已核对"}}]),
        ("attestedText", ["unknown", {}]),
        ("attestedText", ["yes", {"writes": {"result": "不符", "reason": "脚本没有写出这个文件"}}]),
        ("observedLine", ["output", {"path": "Data/ts.csv", "sha256": "0d4e9a1b2c", "size": 18230}]),
        ("checkLine", ["writes", {"result": "已核对", "call": "to_csv"}]),
        ("checkLine", ["field", {"result": "未核对", "reason": "输出文件不是 CSV"}]),
    )
    assert yes == "你当时的说明：输出按此口径生成 — 是"
    assert unknown == "你当时的说明：输出按此口径生成 — 不确定"
    assert beside.startswith("你当时的说明：输出按此口径生成 — 是") and "不符" in beside
    assert obs.startswith("确认时磁盘上的输出文件：Data/ts.csv") and "0d4e9a1b" in obs
    assert check == "写出输出文件 · 已核对（to_csv）"
    assert unchecked == "表头字段 · 未核对 · 输出文件不是 CSV"


@needs_node
def test_every_comparison_is_worded_with_how_far_it_looked():
    """9.11: never the bare phrase; each coverage has its own words."""
    lines = _wording(
        ("coverageLine", [{"path": "build.py", "state": "same", "text": "全文未变"}, "script"]),
        ("coverageLine", [{"dataset": "Data/x.csv", "state": "same", "text": "大小未变（内容未比对）"}, "input"]),
    )
    assert lines == ["实现脚本 build.py：全文未变", "输入 Data/x.csv：大小未变（内容未比对）"]
    html = APP_HTML.read_text(encoding="utf-8")
    assert "实现及数据未变化" not in html
