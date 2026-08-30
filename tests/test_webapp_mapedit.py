"""Tests for rce.webapp.mapedit (task V3 phase 3): editing the researcher's
own attempt-timeline Markdown through the config, with backup + atomic
write + re-ingest.

Everything runs against a realistic CJK fixture project in tmp_path --
never a real research project. The round-trip tests deliberately assert
through the REAL ingest parser and a real graph.db (write with mapedit,
re-ingest with rce.ingest.attempts, read the node back), since the whole
point of this module is that what it writes is exactly what ingest reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rce import db
from rce.ingest import attempts as attempts_ingest
from rce.webapp import mapedit


# -- fixture project -----------------------------------------------------------

_CONFIG_TOML = """\
file = "00-项目地图.md"
heading = "二、尝试途径总年表"

[columns]
id = "#"
date = "时间"
description = "途径"
variables = "变量→因变量(频率)"
result = "结果"
verdict = "判决"
"""

# Mirrors the real project map's shape (same lineage as
# tests/test_ingest_attempts.py's TABLE_MD): CJK headers, bold + emoji
# verdicts, a "14a" split-row id, prose and a second heading after the table.
_MAP_MD = """\
# 项目地图

## 二、尝试途径总年表

| # | 时间 | 途径 | 变量→因变量(频率) | 结果 | 判决 |
|---|------|------|--------------------|------|------|
| 1 | 07-07 | 初稿路径 | 信息熵→汇率(月) | “显著” | ☠️ 死路 |
| 14a | 07-10 | **冻结变体** | 立场熵(日) | 从未跑通 | ☠️ 放弃 |
| 16 | 07-26 | TopicShift→波动率 (16-18) | TopicShift→RV(月) | t=2.91 | ✅ **现行** |

## 三、其他

后面的正文不属于年表。
"""


def _make_project(root: Path, map_md: str = _MAP_MD) -> Path:
    rce_dir = root / ".rce"
    rce_dir.mkdir(parents=True, exist_ok=True)
    (rce_dir / "attempts.toml").write_text(_CONFIG_TOML, encoding="utf-8")
    (root / "00-项目地图.md").write_text(map_md, encoding="utf-8")
    conn = db.connect(rce_dir / "graph.db")
    try:
        db.migrate(conn)
    finally:
        conn.close()
    return root


def _map_text(root: Path) -> str:
    return (root / "00-项目地图.md").read_text(encoding="utf-8")


def _reingest_and_get(root: Path, number: str) -> dict | None:
    """The real round trip this module exists for: run the REAL attempts
    ingest over the edited file and read the node back from the graph."""
    conn = db.connect(root / ".rce" / "graph.db")
    try:
        config = attempts_ingest.load_config(root)
        attempts_ingest.ingest_attempts_repo(conn, root, config)
        return db.get_node(conn, f"attempt:00-项目地图.md#{number}")
    finally:
        conn.close()


_FIELDS_17 = {
    "date": "08-30",
    "description": "价格边际路径 (19)",
    "variables": "基差符号→DV(日)",
    "result": "未跑",
    "verdict": "🕒 待办",
}


# -- preview: pure, no write ---------------------------------------------------


def test_preview_append_returns_diff_and_writes_nothing(tmp_path):
    _make_project(tmp_path)
    before = _map_text(tmp_path)

    preview = mapedit.preview_edit(tmp_path, "append", "17", _FIELDS_17)

    assert _map_text(tmp_path) == before  # pure -- not one byte written
    assert preview["file"] == "00-项目地图.md"
    assert preview["old_row"] is None
    assert "| 17 |" in preview["new_row"] and "价格边际路径" in preview["new_row"]
    assert "+| 17 |" in preview["diff"] and "---" in preview["diff"]  # unified diff shape
    assert not (tmp_path / ".rce" / "backups").exists()  # preview never backs up either


def test_preview_update_returns_old_and_new_row(tmp_path):
    _make_project(tmp_path)
    preview = mapedit.preview_edit(tmp_path, "update", "16", {"verdict": "☠️ 放弃"})
    assert "✅ **现行**" in preview["old_row"]
    assert "☠️ 放弃" in preview["new_row"]
    assert "TopicShift→波动率 (16-18)" in preview["new_row"]  # untouched cell carried over


# -- append: column order, style, placement ------------------------------------


def test_append_preserves_column_order_and_style_and_reingests(tmp_path):
    _make_project(tmp_path)

    result = mapedit.apply_edit(tmp_path, "append", "17", _FIELDS_17)

    lines = _map_text(tmp_path).splitlines()
    # Placed at the table's end -- right after row 16, before the blank
    # line and the next heading -- with the same |-delimited style.
    idx_16 = next(i for i, line in enumerate(lines) if line.startswith("| 16 |"))
    assert lines[idx_16 + 1] == "| 17 | 08-30 | 价格边际路径 (19) | 基差符号→DV(日) | 未跑 | 🕒 待办 |"
    assert lines[idx_16 + 2] == ""  # nothing else moved
    assert result["ingest_error"] is None

    # ...and the write path's own re-ingest already mirrored it into the graph.
    conn = db.connect(tmp_path / ".rce" / "graph.db")
    try:
        node = db.get_node(conn, "attempt:00-项目地图.md#17")
    finally:
        conn.close()
    assert node is not None
    assert node["attrs"]["date"] == "08-30"
    assert node["attrs"]["step_refs"] == ["19"]  # parsed exactly like a hand-written row
    assert node["human_fields"]["verdict"] == "🕒 待办"


def test_append_respects_a_reordered_header_with_extra_column(tmp_path):
    """Column order comes from the file's ACTUAL header, never from config
    key order -- including an extra column the config does not map, which
    stays empty (it belongs to the researcher)."""
    reordered = _MAP_MD.replace(
        "| # | 时间 | 途径 | 变量→因变量(频率) | 结果 | 判决 |",
        "| # | 判决 | 途径 | 备注 | 变量→因变量(频率) | 结果 | 时间 |",
    ).replace(
        "|---|------|------|--------------------|------|------|",
        "|---|------|------|------|--------------------|------|------|",
    ).replace(
        "| 1 | 07-07 | 初稿路径 | 信息熵→汇率(月) | “显著” | ☠️ 死路 |",
        "| 1 | ☠️ 死路 | 初稿路径 | x | 信息熵→汇率(月) | “显著” | 07-07 |",
    ).replace(
        "| 14a | 07-10 | **冻结变体** | 立场熵(日) | 从未跑通 | ☠️ 放弃 |",
        "| 14a | ☠️ 放弃 | **冻结变体** | x | 立场熵(日) | 从未跑通 | 07-10 |",
    ).replace(
        "| 16 | 07-26 | TopicShift→波动率 (16-18) | TopicShift→RV(月) | t=2.91 | ✅ **现行** |",
        "| 16 | ✅ **现行** | TopicShift→波动率 (16-18) | x | TopicShift→RV(月) | t=2.91 | 07-26 |",
    )
    _make_project(tmp_path, reordered)

    mapedit.apply_edit(tmp_path, "append", "17", _FIELDS_17)

    lines = _map_text(tmp_path).splitlines()
    new_row = next(line for line in lines if line.startswith("| 17 |"))
    assert new_row == "| 17 | 🕒 待办 | 价格边际路径 (19) |  | 基差符号→DV(日) | 未跑 | 08-30 |"
    node = _reingest_and_get(tmp_path, "17")
    assert node is not None and node["attrs"]["date"] == "08-30"


def test_append_missing_fields_become_empty_cells(tmp_path):
    _make_project(tmp_path)
    mapedit.apply_edit(tmp_path, "append", "17", {"date": "08-30"})
    node = _reingest_and_get(tmp_path, "17")
    assert node is not None
    assert node["attrs"]["description"] == "" and node["human_fields"]["verdict"] == ""


# -- update: only the requested cells move -------------------------------------


def test_update_edits_only_requested_cells(tmp_path):
    _make_project(tmp_path)
    before_lines = _map_text(tmp_path).splitlines()

    mapedit.apply_edit(tmp_path, "update", "16", {"verdict": "☠️ 放弃：协方差锁死", "result": "t=2.91（复核后不稳）"})

    after_lines = _map_text(tmp_path).splitlines()
    assert len(after_lines) == len(before_lines)
    (changed,) = [i for i, (a, b) in enumerate(zip(before_lines, after_lines)) if a != b]
    assert before_lines[changed].startswith("| 16 |")
    # The two requested cells moved; every other cell of that row is
    # byte-for-byte what it was, decoration included.
    assert after_lines[changed] == (
        "| 16 | 07-26 | TopicShift→波动率 (16-18) | TopicShift→RV(月) "
        "| t=2.91（复核后不稳） | ☠️ 放弃：协方差锁死 |"
    )
    node = _reingest_and_get(tmp_path, "16")
    assert node["human_fields"] == {"verdict": "☠️ 放弃：协方差锁死", "result": "t=2.91（复核后不稳）"}


def test_update_leaves_other_rows_untouched_including_decoration(tmp_path):
    _make_project(tmp_path)
    mapedit.apply_edit(tmp_path, "update", "16", {"verdict": "x"})
    text = _map_text(tmp_path)
    assert "| 14a | 07-10 | **冻结变体** | 立场熵(日) | 从未跑通 | ☠️ 放弃 |" in text


# -- pipe escaping: round trip through the REAL parser -------------------------


def test_pipe_in_cell_round_trips_through_real_ingest(tmp_path):
    _make_project(tmp_path)

    mapedit.apply_edit(tmp_path, "append", "17", {"description": "A|B 途径", "verdict": "✅"})

    assert "A\\|B 途径" in _map_text(tmp_path)  # written escaped...
    node = _reingest_and_get(tmp_path, "17")
    assert node["attrs"]["description"] == "A|B 途径"  # ...parsed back verbatim
    # ...and the table's other rows still parse (the escape never leaked
    # a cell boundary into the row).
    assert _reingest_and_get(tmp_path, "16") is not None


def test_update_with_pipe_round_trips_too(tmp_path):
    _make_project(tmp_path)
    mapedit.apply_edit(tmp_path, "update", "1", {"result": "p<0.05 | 不稳"})
    node = _reingest_and_get(tmp_path, "1")
    assert node["human_fields"]["result"] == "p<0.05 | 不稳"


# -- validation refusals -------------------------------------------------------


def test_append_rejects_existing_number(tmp_path):
    _make_project(tmp_path)
    with pytest.raises(mapedit.MapEditError, match="already exists"):
        mapedit.apply_edit(tmp_path, "append", "16", {"date": "08-30"})
    with pytest.raises(mapedit.MapEditError, match="already exists"):
        mapedit.preview_edit(tmp_path, "append", "14a", {})


def test_update_rejects_unknown_number(tmp_path):
    _make_project(tmp_path)
    with pytest.raises(mapedit.MapEditError, match="no row"):
        mapedit.apply_edit(tmp_path, "update", "99", {"verdict": "x"})


def test_update_rejects_ambiguous_duplicate_number(tmp_path):
    duplicated = _MAP_MD.replace(
        "| 16 | 07-26 |",
        "| 1 | 07-26 |",  # a second physical row labeled "1"
    )
    _make_project(tmp_path, duplicated)
    with pytest.raises(mapedit.MapEditError, match="2 rows"):
        mapedit.preview_edit(tmp_path, "update", "1", {"verdict": "x"})


def test_newlines_in_field_rejected(tmp_path):
    _make_project(tmp_path)
    before = _map_text(tmp_path)
    with pytest.raises(mapedit.MapEditError, match="newline"):
        mapedit.apply_edit(tmp_path, "append", "17", {"result": "第一行\n第二行"})
    assert _map_text(tmp_path) == before


def test_unknown_field_key_rejected(tmp_path):
    _make_project(tmp_path)
    with pytest.raises(mapedit.MapEditError, match="unknown field"):
        mapedit.preview_edit(tmp_path, "append", "17", {"id": "18"})


def test_fields_are_trimmed(tmp_path):
    _make_project(tmp_path)
    mapedit.apply_edit(tmp_path, "append", " 17 ", {"date": "  08-30  "})
    node = _reingest_and_get(tmp_path, "17")
    assert node is not None and node["attrs"]["date"] == "08-30"


def test_decorated_number_rejected(tmp_path):
    """"**16**" would only collide with "16" after the parser cleaned it --
    too late for the duplicate check -- so decorated numbers are refused
    outright."""
    _make_project(tmp_path)
    with pytest.raises(mapedit.MapEditError, match="plain text"):
        mapedit.preview_edit(tmp_path, "append", "**17**", {})


def test_update_with_no_fields_rejected(tmp_path):
    _make_project(tmp_path)
    with pytest.raises(mapedit.MapEditError, match="no fields"):
        mapedit.preview_edit(tmp_path, "update", "16", {})


def test_missing_config_propagates_attempts_config_error(tmp_path):
    (tmp_path / ".rce").mkdir()
    with pytest.raises(attempts_ingest.AttemptsConfigError):
        mapedit.preview_edit(tmp_path, "append", "1", {})


def test_unlocatable_table_propagates_table_not_found(tmp_path):
    _make_project(tmp_path, _MAP_MD.replace("## 二、尝试途径总年表", "## 改名了"))
    with pytest.raises(attempts_ingest.AttemptsTableNotFoundError):
        mapedit.preview_edit(tmp_path, "append", "17", {})


# -- backup + pruning ----------------------------------------------------------


def test_apply_backs_up_original_before_writing(tmp_path):
    _make_project(tmp_path)
    original = _map_text(tmp_path)

    result = mapedit.apply_edit(tmp_path, "append", "17", _FIELDS_17)

    backup_path = tmp_path / result["backup"]
    assert backup_path.parent == tmp_path / ".rce" / "backups"
    assert backup_path.name.startswith("00-项目地图.md.") and backup_path.name.endswith(".md")
    assert backup_path.read_text(encoding="utf-8") == original  # the ORIGINAL, verbatim


def test_backups_pruned_to_newest_20(tmp_path):
    _make_project(tmp_path)
    backups_dir = tmp_path / ".rce" / "backups"
    backups_dir.mkdir(parents=True)
    # 25 pre-existing backups with sortable (zero-padded) stamps, oldest first.
    for i in range(25):
        (backups_dir / f"00-项目地图.md.20260101T0000{i:02d}000000Z.md").write_text("old")
    # ...plus an unrelated file that must never be touched by pruning.
    (backups_dir / "unrelated.txt").write_text("keep me")

    result = mapedit.apply_edit(tmp_path, "append", "17", _FIELDS_17)

    kept = sorted(p.name for p in backups_dir.iterdir() if p.name.startswith("00-项目地图.md."))
    assert len(kept) == mapedit.BACKUP_KEEP
    assert Path(str(result["backup"])).name in kept  # the newest (just written) survived
    assert kept[0] == "00-项目地图.md.20260101T00000600" + "0000Z.md"  # oldest 6 pruned
    assert (backups_dir / "unrelated.txt").exists()


# -- atomic write --------------------------------------------------------------


def test_injected_replace_failure_leaves_original_intact(tmp_path, monkeypatch):
    _make_project(tmp_path)
    original = _map_text(tmp_path)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(mapedit.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        mapedit.apply_edit(tmp_path, "append", "17", _FIELDS_17)

    assert _map_text(tmp_path) == original  # not one byte of the map moved
    # ...and no half-written tmp file was left behind next to it.
    leftovers = [p.name for p in tmp_path.iterdir() if "rce-edit-tmp" in p.name]
    assert leftovers == []


# -- fidelity refusals ---------------------------------------------------------


def test_non_utf8_source_refused(tmp_path):
    _make_project(tmp_path)
    (tmp_path / "00-项目地图.md").write_bytes("| # |".encode("utf-16"))
    with pytest.raises(mapedit.MapEditError, match="UTF-8"):
        mapedit.preview_edit(tmp_path, "append", "17", {})


def test_crlf_file_round_trips_with_crlf(tmp_path):
    _make_project(tmp_path, _MAP_MD.replace("\n", "\r\n"))
    mapedit.apply_edit(tmp_path, "append", "17", _FIELDS_17)
    raw = (tmp_path / "00-项目地图.md").read_bytes()
    assert b"\r\n| 17 | 08-30 |" in raw
    assert b"\n|" not in raw.replace(b"\r\n", b"")  # no lone-\n lines introduced


def test_mixed_newlines_refused(tmp_path):
    mixed = _MAP_MD.replace("# 项目地图\n", "# 项目地图\r\n", 1)  # one CRLF in an LF file
    _make_project(tmp_path, "")
    (tmp_path / "00-项目地图.md").write_bytes(mixed.encode("utf-8"))
    with pytest.raises(mapedit.MapEditError, match="line-ending"):
        mapedit.preview_edit(tmp_path, "append", "17", {})


# -- post-write ingest containment --------------------------------------------


def test_missing_graph_db_contained_as_ingest_error(tmp_path):
    """The graph vanishing mid-serve must not block the user's own file
    edit (the file is the authority): the write happens, is backed up, and
    the ingest failure comes back contained -- never a fresh conjured db."""
    _make_project(tmp_path)
    (tmp_path / ".rce" / "graph.db").unlink()

    result = mapedit.apply_edit(tmp_path, "append", "17", _FIELDS_17)

    assert "graph.db" in str(result["ingest_error"])
    assert "| 17 |" in _map_text(tmp_path)  # file written regardless
    assert not (tmp_path / ".rce" / "graph.db").exists()  # nothing conjured
