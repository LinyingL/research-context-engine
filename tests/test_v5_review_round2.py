"""Second adversarial review of the V5 variable cards (DESIGN.md 9.3, 9.4,
9.11, 9.12; 8.8): one regression test per confirmed finding. Acceptance
scenarios of 9.11 are named in the docstrings (13 life of a card, 14
history is not overwritten, 15 survival, 17 the implementation moves)."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from rce import cli, inventory
from rce.records import cards, implementation
from rce.records import variables as V
from rce.records.trust import Trust
from rce.webapp import registry, server, watcher

from test_v5_phase9 import _apply, _stored
from test_variable_cards import _card, _decision, _new_confirmed, _no_entry_without_copy, _project, _text, _write

APP_HTML = Path(server.__file__).parent / "app.html"
CANVAS_JS = Path(server.__file__).parent / "canvas.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _log(root: Path, card: str = "topicshift") -> Path:
    return V.variables_dir(root) / card / "log.toml"


def _blocks(text: str) -> list[str]:
    head, *rest = text.split("\n[[entry]]")
    return [head, *("[[entry]]" + b for b in rest)]


# -- 「以文件为准」 with a number another history holds (9.3, 9.11) ---------------------------------


def test_scenario_15_a_lost_confirmation_whose_number_is_reused_keeps_its_wording(tmp_path, capsys):
    """9.11 #15 / 9.3: two Macs each confirmed a v2; the sync kept the other
    Mac's log. 「以文件为准」 leaves a `removed` entry naming the lost
    confirmation (and its frozen copy), so `rce records --clean` keeps that
    wording; the file's own v2 stays the version in use."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    after_v1 = _log(root).read_text(encoding="utf-8")
    cards.revise(root, "topicshift")
    _write(root, "topicshift", 2, _text(formula="B-wording"))
    b_entry = cards.confirm(root, "topicshift", attested="yes").entry
    b_frozen = _card(root).directory / b_entry.get("frozen")
    assert b_frozen.is_file()

    # What the sync delivered from Mac A: its own v2 (same seq), its text, its frozen copy.
    a_text = _text(formula="A-wording")
    a_hash = V.content_hash(V.parse_version(a_text))
    a_frozen = f"frozen/{a_hash.removeprefix('sha256:')}.toml"
    b_block = _blocks(_log(root).read_text(encoding="utf-8"))[-1]
    a_block = (b_block.replace(b_entry.id, "v-" + "a" * 32).replace(str(b_entry.get("content")), a_hash)
               .replace(str(b_entry.get("frozen")), a_frozen))
    _log(root).write_text(after_v1 + "\n" + a_block, encoding="utf-8")
    _write(root, "topicshift", 2, a_text)
    (_card(root).directory / a_frozen).write_text(a_text, encoding="utf-8")

    decision = _decision(root)
    assert decision.verdict is Trust.SHRUNK and [m["id"] for m in decision.missing] == [b_entry.id]
    done = cards.answer_shrunk(root, "topicshift", "file", expected_missing=[b_entry.id])
    (tomb,) = done.appended
    assert tomb.get("act") == "removed" and tomb.get("removes") == b_entry.id
    assert tomb.get("frozen") == b_entry.get("frozen") and tomb.get("content") == b_entry.get("content")
    card = _card(root)
    assert card.in_use == 2 and card.versions[2].entry.get("content") == a_hash  # the file's v2 stands
    assert card.versions[2].status == "in_use" and not card.questions

    capsys.readouterr()
    assert cli.main(["records", "--clean", "--yes", str(root)]) == 0
    assert b_frozen.is_file() and "B-wording" in b_frozen.read_text(encoding="utf-8")
    _no_entry_without_copy(root)
    assert cli.main(["review", str(root)]) == 0
    assert cli.main(["records", "--verify", str(root)]) == 0


def test_each_lost_wording_of_one_version_gets_its_own_removed_entry(tmp_path):
    """9.11: a lost confirmation AND its lost correction each leave a
    `removed` entry, so neither frozen copy is left unreferenced."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    aside = _log(root).read_bytes()
    cards.revise(root, "topicshift")
    _write(root, "topicshift", 2, _text(formula="second"))
    cards.confirm(root, "topicshift", attested="unknown")
    _write(root, "topicshift", 2, _text(formula="second, corrected"))
    cards.answer_edited(root, "topicshift", "correct")
    _log(root).write_bytes(aside)
    done = cards.answer_shrunk(root, "topicshift", "file")
    assert sorted(e.get("removes") for e in done.appended) == sorted(m["id"] for m in done.missing)
    assert len({e.get("frozen") for e in done.appended}) == 2
    assert _card(root).versions[2].status == "removed" and _card(root).next_number == 3
    assert cli.main(["records", "--clean", "--yes", str(root)]) == 0
    _no_entry_without_copy(root)


# -- code copies of a card whose directory has not arrived (9.11) ----------------------------------


def test_scenario_15_clean_keeps_the_code_copy_of_a_card_the_index_knows_but_the_disk_lacks(tmp_path, capsys):
    """9.11 #15: a card directory a sync has not delivered yet (the index
    still holds its entries) protects its `_code/` copy; a code copy that is
    missing anyway is 「确认记录引用的副本缺失」 and fails `--verify`."""
    root = tmp_path / "p"
    _project(root)
    entry = _new_confirmed(root).entry
    copy = V.variables_dir(root) / entry.get("checked")["script"]["copy"]
    card_dir = V.variables_dir(root) / "topicshift"
    away = tmp_path / "not-yet-synced"
    shutil.move(str(card_dir), str(away))
    capsys.readouterr()
    assert cli.main(["records", "--clean", "--yes", str(root)]) == 0
    assert copy.is_file(), capsys.readouterr().out
    shutil.move(str(away), str(card_dir))
    _no_entry_without_copy(root)

    copy.unlink()  # gone anyway (deleted by hand, a sync that lost it)
    view = _card(root).versions[1]
    assert view.code_missing and not view.copy_missing
    payload = cards.card_payload(root, _card(root))["versions"][0]
    assert payload["copy_missing"] == V.COPY_MISSING and payload["code_missing"] is True
    capsys.readouterr()
    assert cli.main(["variable", "show", "topicshift", str(root)]) == 0
    assert "the code copy is not there" in capsys.readouterr().out
    assert cli.main(["records", "--verify", str(root)]) == 1
    assert "names the code copy" in capsys.readouterr().out


# -- a card whose log cannot be trusted shows no status (9.11 "Safety") -----------------------------


@pytest.mark.parametrize("damage", ["invalid", "merged"])
def test_scenario_15_a_frozen_card_never_shows_a_confirmed_version_as_a_draft(tmp_path, damage):
    """9.11 #15: while the log is invalid, or holds two merged histories, no
    version is shown as 「草稿 · 改动不留版本」 (which would invite editing
    confirmed text) and none is offered for confirmation."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    cards.revise(root, "topicshift")
    _write(root, "topicshift", 2, _text(formula="second"))
    cards.confirm(root, "topicshift", attested="unknown")
    text = _log(root).read_text(encoding="utf-8")
    if damage == "invalid":
        _log(root).write_text(text + "garbage = [\n", encoding="utf-8")
    else:
        other = _blocks(text)[-1].replace('id = "v-', 'id = "v-0', 1).replace("formula", "formula")
        _log(root).write_text(text + "\n" + other, encoding="utf-8")
    card = _card(root)
    assert card.state == "frozen"
    assert {v.status for v in card.versions.values()} == {"unknown"}
    shown = cards.card_payload(root, card)["versions"]
    assert all(v["label"] == V.UNKNOWN_STATUS_LABEL for v in shown)
    assert all(v["label"] != V.DRAFT_LABEL for v in shown)
    with pytest.raises(cards.CardRefused) as err:
        cards.confirm(root, "topicshift", attested="yes")
    assert err.value.code == "untrusted"


# -- after a correction: the attestation stays with its confirmation (9.11) ------------------------


def test_scenario_14_after_a_correction_the_attestation_is_not_shown_beside_the_corrections_checks(tmp_path, capsys):
    """9.11 #14: 「这是更正」 records new checks and observations; the view
    says they were taken at the correction (and when), and shows the
    attestation with the confirmation's own checks -- never a 'yes' given
    about one output beside another output's fingerprint."""
    root = tmp_path / "p"
    _project(root)
    (root / "Data" / "ts_other.csv").write_text("month,topicshift\n2024-01,0.9\n")
    confirmed = _new_confirmed(root, attested="yes").entry
    _write(root, "topicshift", 1, _text(output="Data/ts_other.csv"))
    corrected = cards.answer_edited(root, "topicshift", "correct", version=1).entry
    v1 = cards.card_payload(root, _card(root))["versions"][0]
    assert v1["attested"] == "yes" and v1["attested_at"] == confirmed.at
    assert v1["attested_observed"]["output"]["path"] == "Data/ts.csv"
    assert v1["checked_act"] == "corrected" and v1["checked_at"] == corrected.at
    assert v1["observed"]["output"]["path"] == "Data/ts_other.csv"
    capsys.readouterr()
    assert cli.main(["variable", "show", "topicshift", str(root)]) == 0
    out = capsys.readouterr().out
    assert f"the text in force is the correction made at {corrected.at}" in out
    assert f"on disk at the correction at {corrected.at}" in out
    assert "on disk at confirmation" not in out


@needs_node
def test_the_page_words_a_corrections_checks_as_the_corrections():
    """9.11 / 8.8 in the page: 「…更正时对照文件的核对」 with its time, and the
    attestation beside the confirmation's own checks (a 是 beside the
    confirmation's 不符 is still shown as such)."""
    html = APP_HTML.read_text(encoding="utf-8")
    block = html[html.index("// -- Variable wording (pure"):html.index("// -- end of variable wording")]
    runner = r"""
const block = require("fs").readFileSync(0, "utf8");
eval(block + "; global.f = confirmationWording;");
const corrected = f({ attested: "yes", checked_act: "corrected", checked_at: "2026-10-05T06:38:00+02:00",
  checked: { reads: [{ dataset: "a.csv", result: "已核对", call: "read_excel" }] },
  attested_checked: { writes: { result: "不符", reason: "脚本没有写出这个文件" } } });
const plain = f({ attested: "no", checked_act: "confirmed", checked: {} });
process.stdout.write(JSON.stringify({ corrected, plain }));
"""
    out = json.loads(subprocess.run([NODE, "-e", runner], input=block, capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    c, p = out["corrected"], out["plain"]
    assert c["checksTitle"] == "2026-10-05 06:38 更正时对照文件的核对"
    assert "确认时" not in c["checksTitle"] and "更正时磁盘上" in c["observedTitle"]
    assert c["statement"].startswith("你当时的说明：输出按此口径生成 — 是") and "不符" in c["statement"]
    assert "不针对后来的更正" in c["statement"]
    assert p["checksTitle"] == "确认时对照文件的核对" and p["statement"] == "你当时的说明：输出按此口径生成 — 否"


# -- a confirmed version file deleted or re-encoded (9.11) -----------------------------------------


def test_scenario_14_a_confirmed_version_file_that_is_gone_is_asked_about_and_put_back(tmp_path, capsys):
    """9.11 #14: a confirmed v1.toml removed after its confirmation: the
    view shows the frozen text and asks 「v1 的版本文件在确认后不见了」; the
    one answer puts it back from the frozen copy, byte for byte."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    path = V.variables_dir(root) / "topicshift" / "v1.toml"
    original = path.read_bytes()
    path.unlink()
    card = _card(root)
    assert card.questions == [1] and card.versions[1].question_kind == "absent"
    payload = cards.card_payload(root, card)
    v1 = payload["versions"][0]
    assert v1["content"]["construction"]["formula"] == "TS_t = 1 − cos(p_t, p_{t−1})" and v1["content_from_frozen"]
    (q,) = payload["questions"]
    assert q["message"] == "v1 的版本文件在确认后不见了" and q["answers"] == ["new"]
    assert q["answer_labels"] == {"new": "按冻结副本放回"}
    with pytest.raises(cards.CardRefused):
        cards.answer_edited(root, "topicshift", "correct", version=1)
    done = cards.answer_edited(root, "topicshift", "new", version=1)
    assert done.new_version is None and path.read_bytes() == original
    assert not _card(root).questions and _card(root).next_number == 2


def test_scenario_14_a_confirmed_version_resaved_in_another_encoding_is_asked_about(tmp_path):
    """9.11 #14: v1.toml re-saved as GBK (no longer UTF-8) is a change of its
    content: asked; 「另存为新版本」 keeps the re-saved bytes as draft v2 and
    puts v1 back; 「这是更正」 is not offered (there is no text to record)."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    path = V.variables_dir(root) / "topicshift" / "v1.toml"
    original = path.read_bytes()
    gbk = _text(formula="TS_t = 1 - cos(p_t, p_{t-1})").replace("–", "-").encode("gbk")  # the editor's re-save
    path.write_bytes(gbk)
    card = _card(root)
    assert card.questions == [1] and card.versions[1].question_kind == "unreadable"
    assert cards.card_payload(root, card)["questions"][0]["answers"] == ["new"]
    done = cards.answer_edited(root, "topicshift", "new", version=1)
    assert done.new_version == 2
    assert path.read_bytes() == original and (path.parent / "v2.toml").read_bytes() == gbk


# -- the attestation question is asked (9.11, acceptance 13) ---------------------------------------


def test_scenario_13_the_confirmation_asks_the_attestation_question(tmp_path, capsys, monkeypatch):
    """9.11 #13: `rce variable confirm` asks, at a terminal, whether the
    output was built with this definition, and records the answer given;
    off a terminal it refuses rather than record an answer nobody gave; the
    core write path has no default answer."""
    root = tmp_path / "p"
    _project(root)
    cards.new_card(root, "topicshift")
    _write(root, "topicshift", 1, _text())
    with pytest.raises(TypeError):
        cards.confirm(root, "topicshift")  # no answer: nothing is recorded for the researcher
    assert cli.main(["variable", "confirm", "topicshift", str(root)]) == 1
    assert "--attest" in capsys.readouterr().err and not _card(root).entries

    answers = iter(["maybe", "yes"])
    asked: list[str] = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or next(answers))
    assert cli.main(["variable", "confirm", "topicshift", str(root)]) == 0
    assert asked == [cli.ATTEST_QUESTION, cli.ATTEST_QUESTION]
    assert _card(root).versions[1].entry.get("attested") == "yes"


def test_a_confirmation_is_tied_to_the_draft_it_was_asked_about(tmp_path, monkeypatch):
    """9.12: the draft saved again while the question was open -- the
    answer is not recorded against text the researcher never saw."""
    root = tmp_path / "p"
    _project(root)
    cards.new_card(root, "topicshift")
    _write(root, "topicshift", 1, _text(formula="formula A shown"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def answer(prompt):
        _write(root, "topicshift", 1, _text(formula="formula B never shown"))
        return "yes"

    monkeypatch.setattr("builtins.input", answer)
    assert cli.main(["variable", "confirm", "topicshift", str(root)]) == 1
    assert not _card(root).entries


# -- an interrupted 「另存为新版本」 (9.11) --------------------------------------------------------------


def test_scenario_14_an_interrupted_save_as_new_is_finished_by_answering_again(tmp_path, capsys):
    """9.11 #14: stopped after the edit is safe in v2.toml but before v1 is
    put back -- answering again finishes the same answer (it is not refused
    because "a draft is open", and makes no v3); leftover temp files of a
    killed write are removed by `rce records --clean`."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    card_dir = V.variables_dir(root) / "topicshift"
    original = (card_dir / "v1.toml").read_bytes()
    edited = _text(formula="edited by hand")
    _write(root, "topicshift", 1, edited)

    def stop(point: str) -> None:
        if point == "after_new_version":
            raise KeyboardInterrupt  # the process stops here

    with pytest.raises(KeyboardInterrupt):
        cards.answer_edited(root, "topicshift", "new", version=1, fault=stop)
    assert (card_dir / "v2.toml").read_text(encoding="utf-8") == edited and _card(root).questions == [1]
    done = cards.answer_edited(root, "topicshift", "new", version=1)
    assert done.new_version == 2 and not (card_dir / "v3.toml").exists()
    assert (card_dir / "v1.toml").read_bytes() == original and _card(root).draft == 2

    leftover = card_dir / ".v1.toml.4242.0123456789ab.rce-tmp"
    leftover.write_bytes(b"half")
    capsys.readouterr()
    assert cli.main(["records", "--clean", "--yes", str(root)]) == 0
    assert not leftover.exists() and (card_dir / "v1.toml").read_bytes() == original


# -- copies are never written through a symlink (9.11, the security model) -------------------------


def test_kept_copies_are_never_written_through_a_symlink(tmp_path):
    """The code copy and the frozen copy go into the project or nowhere: a
    `_code/` or `frozen/` that is a symlink (here to a folder outside the
    project) refuses the confirmation, nothing recorded, nothing written."""
    root = tmp_path / "p"
    _project(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    cards.new_card(root, "topicshift")
    _write(root, "topicshift", 1, _text())
    V.code_dir(root).symlink_to(outside, target_is_directory=True)
    with pytest.raises(cards.CardRefused) as err:
        cards.confirm(root, "topicshift", attested="yes")
    assert err.value.code == "invalid" and not list(outside.iterdir()) and not _card(root).entries

    V.code_dir(root).unlink()
    (V.variables_dir(root) / "topicshift" / "frozen").symlink_to(outside, target_is_directory=True)
    with pytest.raises(cards.CardRefused):
        cards.confirm(root, "topicshift", attested="yes")
    assert not list(outside.iterdir()) and not _card(root).entries


# -- a large input changed at the same size and mtime (9.11 acceptance 17) -------------------------


def test_scenario_17_a_remembered_hash_never_says_the_whole_file_is_unchanged(tmp_path, monkeypatch):
    """9.11 #17: after 「完整比对」 and 「口径未变」, a large input rewritten
    at the same size with its mtime put back is 「大小未变（内容未比对）」 --
    never 「全文未变」 from a hash remembered for that size and mtime -- and
    「完整比对」 then finds the change."""
    monkeypatch.setattr(V, "LARGE_FILE_BYTES", 16)
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    data = root / "Data" / "theme.csv"

    def same_size_edit(old: str, new: str) -> None:
        st = data.stat()
        data.write_text(data.read_text().replace(old, new))
        os.utime(data, ns=(st.st_atime_ns, st.st_mtime_ns))

    same_size_edit("a,3", "b,4")
    review = cards.full_compare(root, "topicshift")
    assert review["under_review"]
    cards.reaffirm(root, "topicshift", version=1, signature=review["signature"], data_version="重新下载")
    _apply(root)
    # The hash that full compare took is remembered, but it is not a reading of the bytes now.
    assert _stored(root)["topicshift"]["inputs"][0]["text"] == "大小未变（内容未比对）"
    assert not _stored(root)["topicshift"]["under_review"]
    same_size_edit("b,4", "c,5")
    _apply(root)
    shown = _stored(root)["topicshift"]["inputs"][0]
    assert shown["text"] == "大小未变（内容未比对）" and shown["coverage"] == "size"
    assert cards.full_compare(root, "topicshift")["under_review"]


def test_a_small_input_is_read_again_whatever_its_size_and_mtime_say(tmp_path):
    """9.11: below the threshold every comparison reads the bytes: an edit
    at the same size with the mtime put back is still found."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    _apply(root)
    data = root / "Data" / "theme.csv"
    st = data.stat()
    data.write_text(data.read_text().replace("a,3", "b,4"))
    os.utime(data, ns=(st.st_atime_ns, st.st_mtime_ns))
    _apply(root)
    shown = _stored(root)["topicshift"]
    assert shown["under_review"] and shown["inputs"][0]["text"] == "有变化"


# -- the watcher's cost (9.11 stage (b)) -----------------------------------------------------------


def test_the_watch_set_reads_the_cards_only_when_their_files_move(tmp_path, monkeypatch):
    """The watcher polls every 2 s: the cards are parsed again only when a
    stat of their files moved -- not on every poll -- and one listing of the
    card directories serves every card read."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    cards.new_card(root, "rv")
    reads = []
    real = V.read_card
    monkeypatch.setattr(V, "read_card", lambda *a, **k: reads.append(a[1]) or real(*a, **k))
    listings = []
    real_dirs = V.card_dirs
    monkeypatch.setattr(V, "card_dirs", lambda r: listings.append(r) or real_dirs(r))
    first = watcher.take_snapshot(root)
    assert len(reads) == 2 and len(listings) <= 3  # one listing for the cards, not one per card
    reads.clear()
    for _ in range(3):
        assert watcher.take_snapshot(root).impl_paths == first.impl_paths
    assert reads == []
    time.sleep(0.01)
    _write(root, "topicshift", 1, _text(script="other.py"))
    watcher.take_snapshot(root)
    assert reads  # a card file moved: read again


# -- the 「变量」 view on a moved or blocked project (9.4) -------------------------------------------


@pytest.fixture
def live(tmp_path):
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    httpd = server.build_server(root, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", root, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call(base: str, method: str, path: str, body: Any = None):
    parsed = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
    data = None if body is None else json.dumps(body).encode("utf-8")
    try:
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, json.loads(raw) if raw else None
    finally:
        conn.close()


def test_scenario_1_the_variables_view_says_the_project_moved_and_then_asks_where(live, tmp_path):
    """9.4 in the 「变量」 view: a folder moved under the engine is 409
    project_moved (never 「还没有版本」 with a wrong reason); after
    「重新打开」 it is the 「找不到项目文件夹」 situation with its answers --
    never an empty card list."""
    base, root, httpd = live
    registry.register(root, httpd.get_served().project_id)
    status, data = _call(base, "GET", "/api/variables")
    assert status == 200 and [c["id"] for c in data["cards"]] == ["topicshift"]
    os.rename(root, tmp_path / "moved")
    for path in ("/api/variables", "/api/variables/card?id=topicshift"):
        status, data = _call(base, "GET", path)
        assert (status, data["state"]) == (409, "project_moved"), path
    status, payload = _call(base, "POST", "/api/project/reopen", {})
    assert payload["blocked"]["situation"] == "missing"
    status, data = _call(base, "GET", "/api/variables")
    assert (status, data["state"]) == (409, "project_blocked")
    assert data["situation"]["situation"] == "missing" and data["situation"]["answers"] == ["locate"]


def test_the_confirmation_from_the_app_is_tied_to_the_draft_the_page_showed(live):
    """9.12: POST /api/variables/confirm and the edited answer carry the
    content hash the page showed; a file changed since is refused,
    nothing written, and the page is told to look again."""
    base, root, _ = live
    cards.revise(root, "topicshift")
    _write(root, "topicshift", 2, _text(formula="formula A shown on the page"))
    shown = [v for v in _call(base, "GET", "/api/variables/card?id=topicshift")[1]["versions"] if v["version"] == 2][0]
    _write(root, "topicshift", 2, _text(formula="formula B never shown"))
    status, data = _call(base, "POST", "/api/variables/confirm",
                         {"id": "topicshift", "attested": "yes", "content_hash": shown["content_hash"]})
    assert (status, data["state"]) == (409, "card_question_changed") and "重新查看" in data["message_zh"]
    assert _card(root).in_use == 1

    _write(root, "topicshift", 1, _text(formula="typo fixed"))
    card = _call(base, "GET", "/api/variables/card?id=topicshift")[1]
    asked = [q for q in card["questions"] if q["version"] == 1][0]
    _write(root, "topicshift", 1, _text(formula="typo fixed, then edited again"))
    status, data = _call(base, "POST", "/api/variables/answer", {
        "id": "topicshift", "question": "edited", "answer": "correct", "version": 1, "content_hash": asked["content_hash"]})
    assert (status, data["state"]) == (409, "card_question_changed")
    assert [e.get("act") for e in _card(root).entries] == ["confirmed"]


# -- the page: the review candidate, the migration note, the canvas chip ---------------------------


@needs_node
def test_a_candidate_the_old_verdict_was_applied_to_is_not_offered_again():
    """9.6/9.12: one click, one entry -- a candidate whose link already
    carries the old verdict says so and offers no second click."""
    html = APP_HTML.read_text(encoding="utf-8")
    block = html[html.index("// -- Judgment wording (pure"):html.index("// -- end of judgment wording")]
    runner = r"""
const block = require("fs").readFileSync(0, "utf8");
eval(block + "; global.f = candidateApplied;");
process.stdout.write(JSON.stringify([
  f({ status: "confirmed" }, "confirmed"), f({ status: "auto" }, "confirmed"),
  f({ status: "rejected" }, "rejected"), f({ status: "confirmed" }, "rejected"),
]));
"""
    out = json.loads(subprocess.run([NODE, "-e", runner], input=block, capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out == [True, False, True, False]
    item = html[html.index("function renderReviewItem("):html.index("function sourceFiles(")]
    assert "candidateApplied(c, item.verdict)" in item and "已用到这条关联上" in item
    panel = html[html.index("async function renderReviewPanel("):html.index("// -- 记录 (9.2")]
    assert "offer.origin" in panel and "scrollIntoView" in panel


@needs_node
def test_a_refused_retirement_keeps_the_engines_english_behind_details():
    """8.8: the holder's English description is behind 「详情」, never in the
    notice's body."""
    html = APP_HTML.read_text(encoding="utf-8")
    block = html[html.index("function tallyLines("):html.index("async function runMigration(")]
    runner = r"""
const block = require("fs").readFileSync(0, "utf8");
eval(block + "; global.f = migrationResultNote;");
process.stdout.write(JSON.stringify(f({ ok: false, results: [{ holders: [
  { pid: 52331, what: "a program listening on the RCE engine port 7357 that does not say which project it serves" }],
  stopped: "the old index is held" }] })));
"""
    note = json.loads(subprocess.run([NODE, "-e", runner], input=block, capture_output=True, text=True,
                                     check=True, timeout=30).stdout)
    assert not any("program listening" in line for line in note["lines"])
    assert any("52331" in line for line in note["lines"])
    assert "program listening" in note["detail"] and "the old index is held" in note["detail"]


@needs_node
def test_a_canvas_error_chip_has_a_close_control():
    """8.8 "Errors": a refused write's chip stays until read -- and can be
    put away with a visible 「关闭」, its cause possibly over by then."""
    runner = r"""
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.handlers = {}; this.attrs = {};
    this.className = ""; this.textContent = ""; this.title = "";
    const self = this;
    this.classList = {
      set: new Set(),
      add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); },
      toggle(c, on) { if (on === undefined ? !this.set.has(c) : on) this.set.add(c); else this.set.delete(c); },
      contains(c) { return this.set.has(c); },
    }; }
  set innerHTML(v) { this.children = []; }
  appendChild(c) { this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.children.push(c)); }
  addEventListener(n, f) { this.handlers[n] = f; }
  setAttribute(k, v) { this.attrs[k] = v; }
  querySelector() { return null; }
}
global.window = {};
global.document = { createElement: (t) => new El(t) };
global.renderBlockingError = (el, text, err) => { const s = new El("span"); s.textContent = text; el.appendChild(s); };
require(process.argv[1]);
const C = window.RCECanvas;
C._state.dom = { status: new El("div") };
C._showStatus("判断记录文件当前无法读取，请先恢复它", new Error("ledger invalid"), { kind: "write" });
const status = C._state.dom.status;
const close = status.children.find((c) => String(c.className).includes("cv-status-close"));
const shownBefore = !status.classList.contains("hidden");
close.handlers.click({ stopPropagation() {} });
process.stdout.write(JSON.stringify({ label: close.textContent, shownBefore, hiddenAfter: status.classList.contains("hidden") }));
"""
    out = json.loads(subprocess.run([NODE, "-e", runner, str(CANVAS_JS)], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out == {"label": "关闭", "shownBefore": True, "hiddenAfter": True}


# -- 「rce review」 says what it counts (9.11 stage (b)) ---------------------------------------------


def test_rce_review_says_a_card_waiting_on_its_draft_is_listed_but_not_counted(tmp_path, capsys):
    """9.11 #17: after 「口径已变」 the card waits on its draft -- `rce review`
    no longer says "0" and then lists it without explanation."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    (root / "build.py").write_text((root / "build.py").read_text() + "df = df.dropna()\n")
    _apply(root)
    cards.revise(root, "topicshift")
    capsys.readouterr()
    assert cli.main(["review", str(root)]) == 0
    out = capsys.readouterr().out
    assert "0 under review, 1 more waiting on a draft already opened (not counted)" in out


def test_scenario_8d_a_candidate_reports_the_verdict_applied_to_it(tmp_path):
    """9.9 #8(d) / 9.6: after the old verdict is applied to the candidate,
    the list says the candidate carries it (the page then offers no second
    click) -- the item itself stays under review, untouched."""
    from rce.records import judgements
    from test_records_judgements import READ, _conn, _project as _judged_project
    from test_v5_phase7 import _rename_script

    root, _ = _judged_project(tmp_path)
    judgements.judge(root, READ, "rejected", via="cli")
    _rename_script(root)
    conn = _conn(root)
    try:
        (item,) = [i for i in judgements.review_items(conn)["review"] if (i["src"], i["dst"]) == READ[:2]]
    finally:
        conn.close()
    candidate = item["candidates"][0]
    assert candidate["status"] != "rejected"
    judgements.judge(root, tuple(candidate[k] for k in ("src", "dst", "type", "extractor")), "rejected", via="canvas")
    conn = _conn(root)
    try:
        (item,) = [i for i in judgements.review_items(conn)["review"] if (i["src"], i["dst"]) == READ[:2]]
    finally:
        conn.close()
    assert item["candidates"][0]["status"] == "rejected"
