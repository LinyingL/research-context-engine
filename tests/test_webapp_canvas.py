"""Tests for the node canvas's data API (DESIGN.md section 8.1, 8.5, 8.6,
8.7; task V4 phase 1b): `rce.webapp.canvas` directly, and the six new
endpoints of `rce.webapp.server` over a real loopback server.

The fixture is a realistic pipeline built by the REAL ingests, not by
hand-written rows, so the canvas is tested against exactly the graph shape
the researcher's project produces: a `.py` step that reads a raw csv and
writes `topicshift_monthly.csv`, two `.Rmd` steps that read that csv, a
knitted `.pdf` sitting next to the 17 step with no extractor that could
link it (the ghost case of 8.1), all under a CJK directory name, and an
attempts table with ☠️ / ✅ / 🕒 rows. Everything lives in tmp_path; the
conftest-wide RCE_HOME keeps the graph and canvas.json there too.
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from rce import db, paths
from rce.ingest import attempts as attempts_ingest
from rce.ingest import dataflow as dataflow_ingest
from rce.ingest import files as files_ingest
from rce.ingest import mappings as mappings_ingest
from rce.webapp import canvas, server


STEPS = "复现包_分步"
PY16 = f"script:{STEPS}/16-构建指标.py"
RMD17 = f"script:{STEPS}/17-叙事更替与汇率波动.Rmd"
RMD18 = f"script:{STEPS}/18-采用.Rmd"
PDF17 = f"figure:{STEPS}/17-叙事更替与汇率波动.pdf"
RAW = f"dataset:{STEPS}/数据/raw_news.csv"
MONTHLY = f"dataset:{STEPS}/数据/topicshift_monthly.csv"
A16, A17, A18 = "attempt:map.md#16", "attempt:map.md#17", "attempt:map.md#18"

_CONFIG = f"""\
file = "map.md"
heading = "H"
steps_dir = "{STEPS}"

[columns]
id = "#"
date = "date"
description = "desc"
variables = "vars"
result = "result"
verdict = "verdict"
"""


def _map(verdicts: tuple[str, str, str] = ("☠️", "✅", "🕒")) -> str:
    v16, v17, v18 = verdicts
    return (
        "## H\n\n| # | date | desc | vars | result | verdict |\n|---|---|---|---|---|---|\n"
        f"| 16 | 2026-09-01 | 构建 TopicShift (16) | v | r | {v16} |\n"
        f"| 17 | 2026-09-03 | TopicShift→波动 (17) | v | r | {v17} |\n"
        f"| 18 | 2026-09-05 | 采用 (18) | v | r | {v18} |\n"
    )


def _ingest(root: Path) -> None:
    """Exactly the CLI's calls: attempts, dataflow over the filesystem
    inventory, mappings."""
    conn = db.connect(paths.graph_db_path(root))
    try:
        attempts_ingest.ingest_attempts_repo(conn, root, attempts_ingest.load_config(root))
        inventory = files_ingest.list_source_files(root)
        dataflow_ingest.ingest_dataflow_repo(conn, root, inventory["py"], inventory["r"], inventory["rmd"])
        mappings_ingest.ingest_mappings(conn, root)
    finally:
        conn.close()


def _make_pipeline(root: Path, verdicts: tuple[str, str, str] = ("☠️", "✅", "🕒")) -> None:
    steps = root / STEPS
    (steps / "数据").mkdir(parents=True)
    (root / ".rce").mkdir()
    (root / ".rce" / "attempts.toml").write_text(_CONFIG, encoding="utf-8")
    (root / "map.md").write_text(_map(verdicts), encoding="utf-8")
    (steps / "16-构建指标.py").write_text(
        "import pandas as pd\n"
        f'df = pd.read_csv("{STEPS}/数据/raw_news.csv")\n'
        f'df.to_csv("{STEPS}/数据/topicshift_monthly.csv")\n',
        encoding="utf-8",
    )
    (steps / "17-叙事更替与汇率波动.Rmd").write_text(
        f'---\ntitle: x\n---\n\n```{{r}}\nd <- read.csv("{STEPS}/数据/topicshift_monthly.csv")\n```\n',
        encoding="utf-8",
    )
    (steps / "18-采用.Rmd").write_text(
        f'```{{r}}\nd <- read.csv("{STEPS}/数据/topicshift_monthly.csv")\n```\n', encoding="utf-8",
    )
    (steps / "17-叙事更替与汇率波动.pdf").write_bytes(b"%PDF-1.4\n")
    (steps / "数据" / "raw_news.csv").write_text("a\n")
    (steps / "数据" / "topicshift_monthly.csv").write_text("a\n")
    paths.ensure_graph_dir(root)
    conn = db.connect(paths.graph_db_path(root))
    try:
        db.migrate(conn)
    finally:
        conn.close()
    _ingest(root)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _make_pipeline(root)
    return root


def _canvas(root: Path, scope: str | None = None) -> dict[str, Any]:
    conn = db.connect(paths.graph_db_path(root))
    try:
        return canvas.build_canvas(conn, root, scope)
    finally:
        conn.close()


def _nodes(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in payload["nodes"]}


def _link_keys(payload: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    return {(l["src"], l["dst"], l["type"], l["extractor"]) for l in payload["links"]}


def _set_status(root: Path, src: str, dst: str, edge_type: str, status: str) -> None:
    conn = db.connect(paths.graph_db_path(root))
    try:
        db.set_edge_status(conn, src, dst, edge_type, "dataflow", status)
    finally:
        conn.close()


# -- default scope (8.7) ----------------------------------------------------------


def test_default_scope_is_the_single_check_mark_row(project):
    payload = _canvas(project)
    assert payload["scope"]["id"] == A17
    assert [s["id"] for s in payload["scopes"]] == [A16, A17, A18]
    assert [s["current"] for s in payload["scopes"]] == [False, True, False]


def test_default_scope_is_most_recent_when_two_rows_carry_the_check_mark(tmp_path):
    root = tmp_path / "proj"
    _make_pipeline(root, ("✅", "✅", "☠️"))
    assert _canvas(root)["scope"]["id"] == A18


def test_default_scope_is_most_recent_when_no_row_carries_the_check_mark(tmp_path):
    """🕒 is an *active* verdict for the dead-variable check, but 8.7 names
    the ✅ row only -- with none, the most recent row wins."""
    root = tmp_path / "proj"
    _make_pipeline(root, ("🕒", "☠️", "☠️"))
    assert _canvas(root)["scope"]["id"] == A18


def test_default_attempt_prefers_numbered_rows_and_handles_none():
    def node(number: str, verdict: str = "") -> dict[str, Any]:
        return {"id": number, "attrs": {"number": number, "source_file": "m"}, "human_fields": {"verdict": verdict}}

    assert canvas.default_attempt([]) is None
    assert canvas.default_attempt([node("2"), node("10"), node("x")])["id"] == "10"
    assert canvas.default_attempt([node("9a"), node("9")])["id"] == "9a"


def test_no_attempts_defaults_to_all(conn, tmp_path):
    payload = canvas.build_canvas(conn, tmp_path, None)
    assert payload["scope"] == {"id": "all"}
    assert payload["scopes"] == [] and payload["frames"] == [] and payload["nodes"] == []


def test_unknown_scope_raises(project):
    with pytest.raises(canvas.UnknownScopeError):
        _canvas(project, "attempt:map.md#99")


# -- scoped contents: steps, touched files, one hop upstream (8.7) ----------------


def test_scope_shows_steps_touched_files_and_one_hop_upstream(project):
    """#17's step script and its knitted pdf; the csv it reads; and 16.py,
    which WRITES that csv -- one hop upstream along writes -> reads. Not
    18.Rmd (a sibling reader, downstream of 16, not upstream of 17) and not
    raw_news.csv (16's own input: two hops)."""
    payload = _canvas(project, A17)
    assert set(_nodes(payload)) == {RMD17, PDF17, MONTHLY, PY16}
    assert _link_keys(payload) == {
        (RMD17, MONTHLY, "reads", "dataflow"),
        (PY16, MONTHLY, "writes", "dataflow"),
    }
    assert payload["frames"] == [{
        "attempt_id": A17, "number": "17", "title": "TopicShift→波动 (17)", "verdict": "✅",
        "node_ids": [RMD17, PDF17],
    }]


def test_scope_all_shows_every_node_and_draws_no_frame(project):
    """8.7 as amended: frames are drawn in attempt views only. The layout
    still learns every attempt's step files (step_groups) for 8.4's
    step-prefix placement and current-attempt-first order."""
    payload = _canvas(project, "all")
    assert set(_nodes(payload)) == {PY16, RMD17, RMD18, PDF17, RAW, MONTHLY}
    assert len(payload["links"]) == 4
    assert payload["frames"] == []
    assert [g["attempt_id"] for g in payload["step_groups"]] == [A16, A17, A18]
    assert payload["step_groups"][1] == {"attempt_id": A17, "node_ids": [RMD17, PDF17]}
    assert payload["scope"] == {"id": "all"}


def test_an_attempt_scope_groups_exactly_its_frame(project):
    payload = _canvas(project, A17)
    assert payload["step_groups"] == [{"attempt_id": A17, "node_ids": [RMD17, PDF17]}]


def test_node_fields_split_label_and_cjk_directory(project):
    node = _nodes(_canvas(project, "all"))[MONTHLY]
    assert node == {
        "id": MONTHLY, "type": "dataset", "path": f"{STEPS}/数据/topicshift_monthly.csv",
        "label": "topicshift_monthly.csv", "dir": f"{STEPS}/数据",
        "missing": False, "orphan_input": False, "ghost": False,
    }


def test_links_carry_flow_direction_and_evidence_hint(project):
    links = {(l["src"], l["type"]): l for l in _canvas(project, "all")["links"]}
    reads = links[(RMD17, "reads")]
    assert (reads["from"], reads["to"]) == (MONTHLY, RMD17)
    assert reads["evidence_hint"] == "dataflow 提取 · 第 6 行"
    assert reads["human"] is False and reads["date"] is None and reads["note"] is None
    writes = links[(PY16, "writes")]
    assert (writes["from"], writes["to"]) == (PY16, MONTHLY)


def test_evidence_hint_counts_several_call_sites():
    edge = {
        "extractor": "dataflow",
        "evidence": {"occurrences": [{"line": 22}, {"line": 15}, {"line": 15}, {"line": 40}]},
    }
    assert canvas.evidence_hint(edge) == "dataflow 提取 · 第 15 行等 3 处"
    assert canvas.evidence_hint({"extractor": "dataflow", "evidence": {"occurrences": [{}]}}) == "dataflow 提取"


# -- ghosts (8.1) ------------------------------------------------------------------


def test_knitted_pdf_is_a_ghost_figure(project):
    node = _nodes(_canvas(project, A17))[PDF17]
    assert node["ghost"] is True and node["type"] == "figure" and node["missing"] is False


def test_ghost_typing_follows_the_extension_and_skips_unknown_ones(tmp_path):
    root = tmp_path / "proj"
    _make_pipeline(root)
    steps = root / STEPS
    (steps / "17-附表.csv").write_text("x\n")
    (steps / "17-草稿.docx").write_bytes(b"PK")
    _ingest(root)  # step_files re-resolved
    nodes = _nodes(_canvas(root, A17))
    assert nodes[f"dataset:{STEPS}/17-附表.csv"]["ghost"] is True
    assert not any(i.endswith("17-草稿.docx") for i in nodes)


def test_ghost_absent_when_its_file_is_gone_from_disk(project):
    (project / STEPS / "17-叙事更替与汇率波动.pdf").unlink()
    assert PDF17 not in _nodes(_canvas(project, A17))


def test_ghost_never_stats_a_step_path_outside_the_root(tmp_path):
    """`steps_dir` is config-influenced: a step file that resolves outside
    the project is never stat'ed, so it can never surface as a ghost."""
    root = tmp_path / "proj"
    _make_pipeline(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "17-泄露.pdf").write_bytes(b"%PDF")
    conn = db.connect(paths.graph_db_path(root))
    try:
        node = db.get_node(conn, A17)
        attrs = dict(node["attrs"], step_files=["../../outside/17-泄露.pdf"])
        db.upsert_node(conn, A17, "attempt", title=node["title"], attrs=attrs)
        payload = canvas.build_canvas(conn, root, A17)
    finally:
        conn.close()
    assert payload["nodes"] == []


def test_ghost_becomes_real_after_a_mapping_is_added(project):
    """8.1's transition: the mapping ingest upserts the node under the very
    id the ghost carried, so the saved position follows it."""
    _save(project, {"scope": A17, "positions": {PDF17: [640, 80]}})
    mappings_ingest.add_mapping(
        project, f"{STEPS}/17-叙事更替与汇率波动.Rmd", f"{STEPS}/17-叙事更替与汇率波动.pdf",
        "generates", note="knitr 渲染产出", date="2026-09-06",
    )
    _ingest(project)
    payload = _canvas(project, A17)
    node = _nodes(payload)[PDF17]
    assert node["ghost"] is False and node["type"] == "figure"
    assert payload["positions"][PDF17] == [640.0, 80.0]
    human = [l for l in payload["links"] if l["human"]]
    assert len(human) == 1
    assert human[0]["extractor"] == "mapping" and human[0]["status"] == "confirmed"
    assert (human[0]["from"], human[0]["to"]) == (RMD17, PDF17)
    assert human[0]["evidence_hint"] == "你于 2026-09-06 标注 · knitr 渲染产出"
    assert (human[0]["date"], human[0]["note"]) == ("2026-09-06", "knitr 渲染产出")


# -- missing / orphan flags (8.2) --------------------------------------------------


def test_missing_flag_is_a_fresh_disk_fact(project):
    (project / STEPS / "16-构建指标.py").unlink()
    nodes = _nodes(_canvas(project, "all"))
    assert nodes[PY16]["missing"] is True
    assert nodes[RMD17]["missing"] is False


def test_orphan_input_is_the_lineage_definition(project):
    nodes = _nodes(_canvas(project, "all"))
    assert nodes[RAW]["orphan_input"] is True  # read by 16, written by nobody
    assert nodes[MONTHLY]["orphan_input"] is False  # 16 writes it
    assert nodes[PY16]["orphan_input"] is False  # never for a script


def test_rejected_writer_makes_its_dataset_an_orphan(project):
    _set_status(project, PY16, MONTHLY, "writes", "rejected")
    nodes = _nodes(_canvas(project, "all"))
    assert nodes[MONTHLY]["orphan_input"] is True


# -- statuses (8.2) ----------------------------------------------------------------


def test_rejected_links_are_omitted_and_pending_ones_kept(project):
    _set_status(project, RMD18, MONTHLY, "reads", "rejected")
    _set_status(project, RMD17, MONTHLY, "reads", "pending")
    links = {(l["src"], l["type"]): l for l in _canvas(project, "all")["links"]}
    assert (RMD18, "reads") not in links
    assert links[(RMD17, "reads")]["status"] == "pending"


def test_rejected_upstream_writer_drops_out_of_the_scope(project):
    """The one-hop rule follows only links the canvas shows."""
    _set_status(project, PY16, MONTHLY, "writes", "rejected")
    assert PY16 not in _nodes(_canvas(project, A17))


# -- canvas.json (8.6) -------------------------------------------------------------


def _save(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    conn = db.connect(paths.graph_db_path(root))
    try:
        return canvas.save_layout(conn, root, body)
    finally:
        conn.close()


def _write_state(root: Path, data: Any) -> Path:
    path = paths.canvas_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


EMPTY = {"positions": {}, "viewport": None}


def test_layout_missing_file_is_empty(tmp_path):
    assert canvas.load_views(tmp_path) == {}
    assert canvas.load_layout(tmp_path, "all") == EMPTY


def test_layout_corrupt_file_degrades_to_empty(tmp_path):
    path = _write_state(tmp_path, "{not json")
    assert canvas.load_layout(tmp_path, "all") == EMPTY
    path.write_bytes(b"\xff\xfe")
    assert canvas.load_layout(tmp_path, "all") == EMPTY
    for data in ([1, 2], {"views": [1]}, {"views": {"all": "x"}}):
        _write_state(tmp_path, data)
        assert canvas.load_views(tmp_path) == {}


def test_layout_legacy_flat_file_is_nothing_saved(project):
    """8.6: the first canvas's flat file held GLOBAL positions; read as any
    one view's arrangement it would pin that view to another view's
    picture. An older-format file is nothing saved, and the next write
    replaces it with the per-view shape."""
    path = _write_state(project, {"positions": {PY16: [1, 2]}, "viewport": {"x": 0, "y": 0, "zoom": 1}})
    assert canvas.load_views(project) == {}
    payload = _canvas(project, A17)
    assert payload["positions"] == {} and payload["viewport"] is None
    _save(project, {"scope": A17, "positions": {RMD17: [5, 6]}})
    assert json.loads(path.read_text()) == {"views": {A17: {"positions": {RMD17: [5.0, 6.0]}, "viewport": None}}}


def test_layout_corrupt_entries_drop_individually(tmp_path):
    _write_state(tmp_path, {"views": {
        "all": {
            "positions": {"a": [1, 2], "b": [1], "c": "x", "d": [True, 2], "e": [3.5, -4]},
            "viewport": {"x": 1, "y": 2, "zoom": 0},
        },
        "attempt:x#1": [1],
        "": {"positions": {"a": [1, 2]}},
    }})
    assert canvas.load_views(tmp_path) == {
        "all": {"positions": {"a": [1.0, 2.0], "e": [3.5, -4.0]}, "viewport": None},
    }


def test_layout_merge_changes_only_named_ids_and_null_deletes(project):
    _save(project, {"scope": "all", "positions": {PY16: [1, 2], RAW: [3, 4]}, "viewport": {"x": 0, "y": 0, "zoom": 1}})
    view = _save(project, {"scope": "all", "positions": {RAW: None, MONTHLY: [5, 6]}})
    assert view == canvas.load_layout(project, "all") == {
        "positions": {PY16: [1.0, 2.0], MONTHLY: [5.0, 6.0]},
        "viewport": {"x": 0.0, "y": 0.0, "zoom": 1.0},
    }
    _save(project, {"scope": "all", "viewport": None})
    assert canvas.load_layout(project, "all") == {"positions": {PY16: [1.0, 2.0], MONTHLY: [5.0, 6.0]}, "viewport": None}


def test_each_view_keeps_its_own_arrangement_and_viewport(project):
    """8.4 as amended: the same card may sit in different places in two
    views; saving in one never shows in another."""
    _save(project, {"scope": A17, "positions": {RMD17: [10, 20], PY16: [30, 40]}, "viewport": {"x": 1, "y": 2, "zoom": 0.5}})
    _save(project, {"scope": "all", "positions": {RMD17: [900, 900]}})
    in_17, in_all, in_18 = _canvas(project, A17), _canvas(project, "all"), _canvas(project, A18)
    assert in_17["positions"] == {RMD17: [10.0, 20.0], PY16: [30.0, 40.0]}
    assert in_17["viewport"] == {"x": 1.0, "y": 2.0, "zoom": 0.5}
    assert in_all["positions"] == {RMD17: [900.0, 900.0]} and in_all["viewport"] is None
    assert in_18["positions"] == {} and in_18["viewport"] is None


def test_reset_forgets_one_views_arrangement_only(project):
    _save(project, {"scope": A17, "positions": {RMD17: [10, 20], PY16: [30, 40]}, "viewport": {"x": 1, "y": 2, "zoom": 1}})
    _save(project, {"scope": "all", "positions": {RMD17: [900, 900]}})
    view = _save(project, {"scope": A17, "reset": True})
    assert view["positions"] == {} and view["viewport"] == {"x": 1.0, "y": 2.0, "zoom": 1.0}
    assert canvas.load_layout(project, "all")["positions"] == {RMD17: [900.0, 900.0]}
    # Reset first, then the body's own positions: a pin right after a reset.
    view = _save(project, {"scope": A17, "reset": True, "positions": {PY16: [7, 8]}, "viewport": None})
    assert view == {"positions": {PY16: [7.0, 8.0]}, "viewport": None}


def test_a_write_is_bounded_by_the_graph(project):
    """8.6: a scope the project does not have is refused, and a view only
    takes positions for cards it shows, so the file cannot grow keys a
    page invents. Deleting (null) any id only shrinks it."""
    with pytest.raises(canvas.UnknownScopeError):
        _save(project, {"scope": "attempt:map.md#99", "positions": {PY16: [1, 2]}})
    assert not paths.canvas_state_path(project).exists()
    _save(project, {"scope": A17, "positions": {"dataset:gone.csv": None, PDF17: [1, 2]}})  # a ghost is a card
    assert canvas.load_layout(project, A17)["positions"] == {PDF17: [1.0, 2.0]}


def test_a_card_the_view_no_longer_shows_is_skipped_not_refused(project):
    """A card can leave the view between the page's fetch and its save (an
    output deleted while the canvas is open). Its position is skipped; the
    rest of the write -- the researcher's move -- lands (verifier finding:
    refusing the whole write made every later save of the view fail)."""
    view = _save(project, {"scope": A17, "positions": {RMD18: [1, 2], RMD17: [3, 4]}})  # 18 is not in #17's view
    assert view["positions"] == {RMD17: [3.0, 4.0]}
    _save(project, {"scope": "all", "positions": {"dataset:invented.csv": [1, 2]}, "viewport": {"x": 0, "y": 0, "zoom": 1}})
    assert canvas.load_layout(project, "all") == {"positions": {}, "viewport": {"x": 0.0, "y": 0.0, "zoom": 1.0}}


def test_a_write_keeps_views_of_attempts_the_graph_lacks_right_now(project):
    """A row cut and pasted back in the map (an autosaving editor) removes
    its attempt for a moment; an unrelated write meanwhile (a pan in 全部)
    must not forget that attempt's pinned arrangement (verifier finding)."""
    _write_state(project, {"views": {"attempt:map.md#99": {"positions": {"x": [1, 2]}}, A18: {"positions": {RMD18: [1, 2]}}}})
    _save(project, {"scope": "all", "positions": {RAW: [3, 4]}})
    assert set(canvas.load_views(project)) == {"all", A18, "attempt:map.md#99"}
    assert canvas.load_layout(project, "attempt:map.md#99")["positions"] == {"x": [1.0, 2.0]}


def test_layout_save_recovers_a_corrupt_file(project):
    path = _write_state(project, "garbage")
    _save(project, {"scope": "all", "positions": {RAW: [1, 2]}})
    assert json.loads(path.read_text()) == {"views": {"all": {"positions": {RAW: [1.0, 2.0]}, "viewport": None}}}


def test_layout_is_never_backed_up_and_never_in_the_project(project):
    _save(project, {"scope": "all", "positions": {PY16: [1, 2]}})
    assert not (project / ".rce" / "backups").exists()
    assert not (project / ".rce" / "canvas.json").exists()
    assert paths.canvas_state_path(project).exists()


@pytest.mark.parametrize("body", [
    {"positions": {PY16: [1, 2]}},
    {"scope": "", "positions": {PY16: [1, 2]}},
    {"scope": 3},
    {"scope": "all", "reset": "yes"},
    {"scope": "all", "positions": []},
    {"scope": "all", "positions": {PY16: [1, 2, 3]}},
    {"scope": "all", "positions": {PY16: [1, float("nan")]}},
    {"scope": "all", "positions": {PY16: [1, float("inf")]}},
    {"scope": "all", "positions": {PY16: [True, 2]}},
    {"scope": "all", "positions": {PY16: ["1", 2]}},
    {"scope": "all", "positions": {"": [1, 2]}},
    {"scope": "all", "viewport": {"x": 0, "y": 0}},
    {"scope": "all", "viewport": {"x": 0, "y": 0, "zoom": 0}},
    {"scope": "all", "viewport": {"x": 0, "y": 0, "zoom": 1, "extra": 1}},
    {"scope": "all", "viewport": [0, 0, 1]},
    {"scope": "all", "something": 1},
])
def test_layout_rejects_bad_shapes(project, body):
    with pytest.raises(canvas.LayoutShapeError):
        _save(project, body)
    assert not paths.canvas_state_path(project).exists()


def test_positions_in_payload_are_only_the_visible_ones(project):
    _save(project, {"scope": A17, "positions": {RMD17: [1, 2], PY16: [3, 4]}})
    # PY16 is in #17's view only one hop upstream; scoped to 16 it is a step.
    conn = db.connect(paths.graph_db_path(project))
    try:
        db.set_edge_status(conn, PY16, MONTHLY, "writes", "dataflow", "rejected")
    finally:
        conn.close()
    assert _canvas(project, A17)["positions"] == {RMD17: [1.0, 2.0]}


# -- HTTP: the six endpoints -------------------------------------------------------


@pytest.fixture
def live(tmp_path: Path):
    root = tmp_path / "proj"
    _make_pipeline(root)
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
        return resp.status, (json.loads(raw) if raw else None)
    finally:
        conn.close()


_PDF_MAPPING = {
    "from": f"{STEPS}/17-叙事更替与汇率波动.Rmd",
    "to": f"{STEPS}/17-叙事更替与汇率波动.pdf",
    "type": "generates",
}


def test_http_canvas_default_all_and_unknown_scope(live):
    base, _, _ = live
    status, payload = _call(base, "GET", "/api/canvas")
    assert status == 200 and payload["scope"]["id"] == A17
    status, payload = _call(base, "GET", "/api/canvas?scope=all")
    assert status == 200 and len(payload["nodes"]) == 6
    status, payload = _call(base, "GET", "/api/canvas?scope=" + urllib.parse.quote(A18))
    assert status == 200 and payload["scope"]["id"] == A18
    status, payload = _call(base, "GET", "/api/canvas?scope=nope")
    assert status == 404 and "nope" in payload["error"]


def test_http_canvas_degraded_project_reports_its_state(live):
    base, root, _ = live
    paths.graph_db_path(root).unlink()
    status, payload = _call(base, "GET", "/api/canvas")
    assert status == 400 and payload["state"] == "graph_missing"


def test_http_mapping_add_writes_ingests_and_returns_the_link(live):
    base, root, httpd = live
    generation = httpd.watcher.status_payload()["generation"]
    status, payload = _call(base, "POST", "/api/mappings/add", dict(_PDF_MAPPING, note="knitr 渲染产出"))
    assert status == 200, payload
    assert payload["ok"] is True and payload["ingest_error"] is None
    assert payload["generation"] > generation
    link = payload["link"]
    assert link["human"] is True and link["status"] == "confirmed"
    assert (link["from"], link["to"], link["type"]) == (RMD17, PDF17, "generates")
    assert "knitr 渲染产出" in link["evidence_hint"]
    text = (root / ".rce" / "mappings.toml").read_text(encoding="utf-8")
    assert "手工标注的映射" in text and 'type = "generates"' in text
    _, view = _call(base, "GET", "/api/canvas")
    assert _nodes(view)[PDF17]["ghost"] is False


def test_http_mapping_add_blank_note_is_no_note(live):
    base, root, _ = live
    status, _ = _call(base, "POST", "/api/mappings/add", dict(_PDF_MAPPING, note="  "))
    assert status == 200
    assert "note" not in (root / ".rce" / "mappings.toml").read_text(encoding="utf-8")


def test_http_mapping_add_duplicate_is_409_with_state(live):
    base, _, _ = live
    assert _call(base, "POST", "/api/mappings/add", _PDF_MAPPING)[0] == 200
    status, payload = _call(base, "POST", "/api/mappings/add", _PDF_MAPPING)
    assert status == 409 and payload["state"] == "mapping_exists"
    assert "already exists" in payload["error"]


@pytest.mark.parametrize("body, fragment", [
    (dict(_PDF_MAPPING, type="reads"), "'reads' goes from a dataset"),
    (dict(_PDF_MAPPING, to="../../etc/passwd.csv"), "outside the project"),
    (dict(_PDF_MAPPING, to="/etc/x.pdf"), "absolute"),
    (dict(_PDF_MAPPING, note="a b"), "line"),
    ({"from": "a.py", "to": "b.pdf"}, "non-empty string keys"),
    (dict(_PDF_MAPPING, note=3), "'note' must be a string"),
])
def test_http_mapping_add_refusals_write_nothing(live, body, fragment):
    base, root, _ = live
    status, payload = _call(base, "POST", "/api/mappings/add", body)
    assert status == 400 and fragment in payload["error"], payload
    assert "state" not in payload
    assert not (root / ".rce" / "mappings.toml").exists()


def test_http_mapping_add_refuses_when_the_graph_is_missing(live):
    base, root, _ = live
    paths.graph_db_path(root).unlink()
    status, payload = _call(base, "POST", "/api/mappings/add", _PDF_MAPPING)
    assert status == 400 and payload["state"] == "graph_missing"
    assert not (root / ".rce" / "mappings.toml").exists()


def test_http_mapping_delete_removes_entry_and_edge(live):
    base, root, _ = live
    _call(base, "POST", "/api/mappings/add", _PDF_MAPPING)
    status, payload = _call(base, "POST", "/api/mappings/delete", _PDF_MAPPING)
    assert status == 200 and payload["ok"] is True and payload["removed"] == 1
    assert payload["backup"].startswith(".rce/backups/mappings.toml.")
    _, view = _call(base, "GET", "/api/canvas")
    assert not any(l["human"] for l in view["links"])
    assert _nodes(view)[PDF17]["ghost"] is True  # back to a ghost: its node was the mapping's own


def test_http_mapping_delete_unknown_is_404(live):
    base, _, _ = live
    status, payload = _call(base, "POST", "/api/mappings/delete", _PDF_MAPPING)
    assert status == 404 and "error" in payload


def test_http_mapping_write_absorbs_only_the_mappings_file(live):
    """A map-file save the watcher has not ingested yet survives a canvas
    write: the next poll still sees and ingests it."""
    base, root, httpd = live
    httpd.watcher.poll_once()  # baseline
    (root / "map.md").write_text(
        _map() + "| 19 | 2026-09-07 | 新行 (19) | v | r | 🕒 |\n", encoding="utf-8",
    )
    assert _call(base, "POST", "/api/mappings/add", _PDF_MAPPING)[0] == 200
    assert httpd.watcher.poll_once() is True
    _, view = _call(base, "GET", "/api/canvas")
    assert "attempt:map.md#19" in {s["id"] for s in view["scopes"]}


def test_http_mapping_write_with_a_failed_reingest_is_retried_by_the_watcher(live, monkeypatch):
    """Adversarial review of the V4 work: when the post-write re-ingest
    fails (e.g. 'database is locked' under a CLI ingest), the file change
    must NOT be absorbed into the watcher's baseline -- the next poll
    retries it and the link lands -- and the failure is reported once (the
    response's ingest_error, the canvas chip), not also as the watcher's
    last_error (the header chip)."""
    base, root, httpd = live
    httpd.watcher.poll_once()  # baseline
    real = server._reingest_mappings
    monkeypatch.setattr(server, "_reingest_mappings", lambda r: (_ for _ in ()).throw(RuntimeError("database is locked")))
    generation = httpd.watcher.status_payload()["generation"]
    status, payload = _call(base, "POST", "/api/mappings/add", _PDF_MAPPING)
    assert status == 200 and payload["ingest_error"] == "database is locked"
    assert payload["generation"] == generation  # nothing in the graph changed
    assert httpd.watcher.status_payload()["last_error"] is None  # one message, not two

    monkeypatch.setattr(server, "_reingest_mappings", real)
    assert httpd.watcher.poll_once() is True  # the change was left visible
    _, view = _call(base, "GET", "/api/canvas")
    assert any(l["human"] and (l["from"], l["to"]) == (RMD17, PDF17) for l in view["links"])
    assert _call(base, "POST", "/api/mappings/add", _PDF_MAPPING)[1]["state"] == "mapping_exists"


def _edge_body(src: str, dst: str, edge_type: str, extractor: str = "dataflow") -> dict[str, str]:
    return {"src": src, "dst": dst, "type": edge_type, "extractor": extractor}


def test_http_reject_then_restore_round_trip(live):
    base, _, httpd = live
    body = _edge_body(RMD18, MONTHLY, "reads")
    generation = httpd.watcher.status_payload()["generation"]
    status, payload = _call(base, "POST", "/api/edges/reject", body)
    assert status == 200 and payload["link"]["status"] == "rejected"
    assert payload["generation"] == generation + 1
    _, view = _call(base, "GET", "/api/canvas?scope=all")
    assert (RMD18, MONTHLY, "reads", "dataflow") not in _link_keys(view)
    assert _call(base, "POST", "/api/edges/reject", body)[0] == 200  # idempotent

    status, payload = _call(base, "POST", "/api/edges/restore", body)
    assert status == 200 and payload["link"]["status"] == "auto"
    _, view = _call(base, "GET", "/api/canvas?scope=all")
    assert (RMD18, MONTHLY, "reads", "dataflow") in _link_keys(view)


def test_http_restore_puts_back_a_human_confirmation(live):
    """Adversarial review of the V4 work: undoing 标记为错误提取 on a link
    the researcher had confirmed (`rce confirm`) must restore `confirmed`,
    not demote the human's judgement to the machine status `auto`. The
    recorded prior status survives a re-ingest in between."""
    base, root, httpd = live
    body = _edge_body(RMD18, MONTHLY, "reads")
    conn = db.connect(paths.graph_db_path(root))
    try:
        db.set_edge_status(conn, RMD18, MONTHLY, "reads", "dataflow", "confirmed")
    finally:
        conn.close()
    assert _call(base, "POST", "/api/edges/reject", body)[1]["link"]["status"] == "rejected"
    assert _call(base, "POST", "/api/edges/reject", body)[0] == 200  # second click keeps the memory
    httpd.watcher._reingest(root, steps_changed=True)  # a machine re-ingest in between

    status, payload = _call(base, "POST", "/api/edges/restore", body)
    assert status == 200 and payload["link"]["status"] == "confirmed"
    conn = db.connect(paths.graph_db_path(root))
    try:
        edge = next(e for e in db.query_edges(conn, src=RMD18, dst=MONTHLY, type="reads"))
        assert edge["status"] == "confirmed"
        assert db.STATUS_BEFORE_REJECT_KEY not in edge["evidence"]
    finally:
        conn.close()


def test_http_restore_of_a_live_link_is_409(live):
    base, _, _ = live
    status, payload = _call(base, "POST", "/api/edges/restore", _edge_body(RMD18, MONTHLY, "reads"))
    assert status == 409 and "not rejected" in payload["error"]


def test_http_reject_refuses_a_human_mapping(live):
    base, root, _ = live
    _call(base, "POST", "/api/mappings/add", _PDF_MAPPING)
    for action in ("reject", "restore"):
        status, payload = _call(base, "POST", f"/api/edges/{action}", _edge_body(RMD17, PDF17, "generates", "mapping"))
        assert status == 400 and payload["state"] == "human_link"
    _, view = _call(base, "GET", "/api/canvas")
    assert any(l["human"] and l["status"] == "confirmed" for l in view["links"])


def test_http_reject_unknown_or_non_canvas_edge_is_404(live):
    base, root, _ = live
    status, _ = _call(base, "POST", "/api/edges/reject", _edge_body(RMD18, RAW, "reads"))
    assert status == 404
    conn = db.connect(paths.graph_db_path(root))
    try:  # an edge between non-canvas nodes (a claim backed by a reference)
        db.upsert_node(conn, "claim:c", "claim", title="c")
        db.upsert_node(conn, "reference:r", "reference", title="r")
        db.upsert_edge(conn, "claim:c", "reference:r", "backed_by", "claims", evidence={"x": 1}, confidence=1.0, status="pending")
    finally:
        conn.close()
    status, _ = _call(base, "POST", "/api/edges/reject", _edge_body("claim:c", "reference:r", "backed_by", "claims"))
    assert status == 404


def test_http_edge_body_must_be_four_strings(live):
    base, _, _ = live
    status, payload = _call(base, "POST", "/api/edges/reject", {"src": PY16, "dst": MONTHLY, "type": "writes"})
    assert status == 400 and "extractor" in payload["error"]


def _proj(root: Path) -> str:
    return str(Path(root).resolve())


def test_http_layout_merge_per_view_and_bad_body(live):
    base, root, _ = live
    q17 = "/api/canvas?scope=" + urllib.parse.quote(A17)
    status, payload = _call(base, "POST", "/api/canvas/layout", {"project": _proj(root), "scope": A17, "positions": {PY16: [10, 20], RMD17: [30, 40]}})
    assert status == 200 and payload["positions"] == 2 and payload["scope"] == A17
    status, _ = _call(base, "POST", "/api/canvas/layout", {"project": _proj(root), "scope": A17, "positions": {PY16: None}, "viewport": {"x": 1, "y": 2, "zoom": 1.5}})
    assert status == 200
    _, view = _call(base, "GET", q17)
    assert view["positions"] == {RMD17: [30.0, 40.0]}
    assert view["viewport"] == {"x": 1.0, "y": 2.0, "zoom": 1.5}
    # The default scope is #17: same view, same arrangement.
    _, default = _call(base, "GET", "/api/canvas")
    assert default["positions"] == view["positions"]
    # 全部 is a different view: nothing of #17's arrangement shows there.
    _, whole = _call(base, "GET", "/api/canvas?scope=all")
    assert whole["positions"] == {} and whole["viewport"] is None and whole["frames"] == []
    status, payload = _call(base, "POST", "/api/canvas/layout", {"project": _proj(root), "scope": A17, "positions": {PY16: [1, "x"]}})
    assert status == 400 and "finite" in payload["error"]
    status, payload = _call(base, "POST", "/api/canvas/layout", {"project": _proj(root), "positions": {PY16: [1, 2]}})
    assert status == 400 and "scope" in payload["error"]


def test_http_layout_reset_forgets_the_view(live):
    base, root, _ = live
    _call(base, "POST", "/api/canvas/layout", {"project": _proj(root), "scope": "all", "positions": {RAW: [1, 2], PY16: [3, 4]}})
    status, payload = _call(base, "POST", "/api/canvas/layout", {"project": _proj(root), "scope": "all", "reset": True})
    assert status == 200 and payload["positions"] == 0
    _, whole = _call(base, "GET", "/api/canvas?scope=all")
    assert whole["positions"] == {}


def test_http_layout_refuses_an_invented_scope_or_card(live):
    base, root, _ = live
    status, payload = _call(base, "POST", "/api/canvas/layout", {"project": _proj(root), "scope": "attempt:map.md#99", "positions": {PY16: [1, 2]}})
    assert status == 404 and "#99" in payload["error"]
    status, payload = _call(base, "POST", "/api/canvas/layout", {"project": _proj(root), "scope": "anything", "viewport": {"x": 0, "y": 0, "zoom": 1}})
    assert status == 404
    assert not paths.canvas_state_path(root).exists()
    status, payload = _call(base, "POST", "/api/canvas/layout", {"project": _proj(root), "scope": A17, "positions": {RMD18: [1, 2]}})
    assert status == 200 and payload["positions"] == 0  # a card #17 does not show is skipped


def test_http_layout_must_name_the_project_its_page_shows(live, tmp_path):
    """A page still showing project B posts after another window switched
    the server to A: the write is refused (409, state project_changed) and
    A's canvas.json is untouched (verifier finding). GET /api/canvas names
    the project every write must echo."""
    base, root, _ = live
    _, view = _call(base, "GET", "/api/canvas")
    assert view["project"] == _proj(root)
    body = {"scope": "all", "positions": {PY16: [1, 2]}}
    status, payload = _call(base, "POST", "/api/canvas/layout", dict(body, project=str(tmp_path)))
    assert status == 409 and payload.get("state") == "project_changed"
    status, payload = _call(base, "POST", "/api/canvas/layout", body)
    assert status == 400 and "project" in payload["error"]
    assert not paths.canvas_state_path(root).exists()
    status, _ = _call(base, "POST", "/api/canvas/layout", dict(body, project=view["project"]))
    assert status == 200 and canvas.load_layout(root, "all")["positions"] == {PY16: [1.0, 2.0]}


def test_http_layout_corrupt_file_degrades_on_read(live):
    base, root, _ = live
    paths.canvas_state_path(root).write_text("{{{")
    status, view = _call(base, "GET", "/api/canvas")
    assert status == 200 and view["positions"] == {} and view["viewport"] is None


# -- cross-origin and wrong-Host rejection, for EVERY new endpoint -----------------


_ENDPOINTS = [
    ("GET", "/api/canvas", None),
    ("POST", "/api/canvas/layout", {"scope": "all", "positions": {PY16: [1, 2]}}),
    ("POST", "/api/mappings/add", _PDF_MAPPING),
    ("POST", "/api/mappings/delete", _PDF_MAPPING),
    ("POST", "/api/edges/reject", _edge_body(RMD18, MONTHLY, "reads")),
    ("POST", "/api/edges/restore", _edge_body(RMD18, MONTHLY, "reads")),
]


def _untouched(root: Path) -> None:
    """Nothing any of the endpoints could write has moved."""
    assert not (root / ".rce" / "mappings.toml").exists()
    assert not paths.canvas_state_path(root).exists()
    conn = db.connect(paths.graph_db_path(root))
    try:
        assert all(e["status"] == "auto" for e in db.query_edges(conn))
    finally:
        conn.close()


@pytest.mark.parametrize("method, path, body", _ENDPOINTS)
def test_http_new_endpoints_reject_foreign_origin(live, method, path, body):
    base, root, _ = live
    status, payload = _call(
        base, method, path, body, headers={"Content-Type": "text/plain", "Origin": "http://evil.example"},
    )
    assert status == 403 and "Origin" in payload["error"]
    _untouched(root)


@pytest.mark.parametrize("method, path, body", _ENDPOINTS)
def test_http_new_endpoints_reject_wrong_host(live, method, path, body):
    base, root, _ = live
    status, payload = _call(base, method, path, body, headers={"Host": "attacker.example:1234"})
    assert status == 403 and "Host" in payload["error"]
    _untouched(root)


@pytest.mark.parametrize("method, path, body", _ENDPOINTS[:3])
def test_http_new_endpoints_accept_the_portless_loopback_origin(live, method, path, body):
    """The Safari/WebKit shape (8.9 "Origin"): the shell adds no new one."""
    base, root, _ = live
    if path == "/api/canvas/layout":
        body = dict(body, project=_proj(root))
    status, _ = _call(base, method, path, body, headers={"Origin": "http://127.0.0.1"})
    assert status == 200
