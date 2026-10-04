"""Tests for the canvas's client-side layout (DESIGN.md section 8.4; task
V4 phase 2a), run against the REAL `src/rce/webapp/canvas.js` under node.

8.4's rules are binding and live only in the browser (the layout is
computed client-side, no library), so they are pinned here by loading the
served file into node with a stub `window` and calling the layout function
it exposes (`RCECanvas._computeLayout`). Nothing else in the file runs at
load time -- it only defines `window.RCECanvas` -- which is what makes this
possible without a DOM.

node is a developer tool, not a dependency of this package (pyproject
dependencies stay empty): these tests skip where node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from rce.webapp import server

CANVAS_JS = Path(server.__file__).parent / "canvas.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

S = "复现包_分步"
PY16 = f"script:{S}/16-构建指标.py"
RMD17 = f"script:{S}/17-叙事更替与汇率波动.Rmd"
RMD18 = f"script:{S}/18-采用.Rmd"
PDF17 = f"figure:{S}/17-叙事更替与汇率波动.pdf"
RAW = f"dataset:{S}/数据/raw_news.csv"
MONTHLY = f"dataset:{S}/数据/topicshift_monthly.csv"

_RUNNER = """
global.window = {};
require(process.argv[1]);
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = window.RCECanvas._computeLayout(input.nodes, input.links, input.frames);
process.stdout.write(JSON.stringify({ positions: out.positions, cycle: [...out.cycle] }));
"""


def _node(node_id: str) -> dict[str, Any]:
    node_type, _, path = node_id.partition(":")
    return {"id": node_id, "type": node_type, "path": path}


def _link(link_id: str, frm: str, to: str) -> dict[str, Any]:
    return {"id": link_id, "from": frm, "to": to}


def _layout(nodes: list[str], links: list[dict[str, Any]], frames: list[list[str]] | None = None) -> dict[str, Any]:
    payload = {
        "nodes": [_node(n) for n in nodes],
        "links": links,
        "frames": [{"node_ids": ids} for ids in (frames or [])],
    }
    result = subprocess.run(
        [NODE, "-e", _RUNNER, str(CANVAS_JS)],
        input=json.dumps(payload), capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


def _column(out: dict[str, Any], node_id: str) -> int:
    x = out["positions"][node_id][0]
    assert x % 320 == 0, "columns are 320px apart (8.4 step 3)"
    return x // 320


# The researcher's own pipeline (8.4 step 1's worked example), as the canvas
# draws it in scope "all": raw -> 16 -> monthly -> 17/18.
PIPELINE_NODES = [RMD18, RMD17, MONTHLY, PY16, RAW]
PIPELINE_LINKS = [
    _link("r16", RAW, PY16), _link("w16", PY16, MONTHLY),
    _link("r17", MONTHLY, RMD17), _link("r18", MONTHLY, RMD18),
]


def test_longest_path_layers_put_17_and_18_two_columns_right_of_16():
    out = _layout(PIPELINE_NODES, PIPELINE_LINKS)
    cols = {n: _column(out, n) for n in PIPELINE_NODES}
    assert cols == {RAW: 0, PY16: 1, MONTHLY: 2, RMD17: 3, RMD18: 3}
    assert out["cycle"] == []


def test_ties_within_a_layer_keep_step_order_and_rows_never_overlap():
    """17 above 18 (numeric step prefix), at least a 24px gap apart."""
    out = _layout(PIPELINE_NODES, PIPELINE_LINKS)
    y17, y18 = out["positions"][RMD17][1], out["positions"][RMD18][1]
    script_height = 26 + 46 + 2 * 20 + 8
    assert y17 < y18
    assert y18 - y17 >= script_height + 24


def test_cycle_is_broken_at_its_closing_edge_and_reported():
    """16 reads the csv it writes: the DFS (in step order) enters 16 first,
    so the edge that closes the loop is csv -> 16's 读取, and only it."""
    links = [_link("w", PY16, MONTHLY), _link("r", MONTHLY, PY16)]
    out = _layout([MONTHLY, PY16], links)
    assert out["cycle"] == ["r"]
    assert _column(out, PY16) == 0 and _column(out, MONTHLY) == 1


def test_a_human_link_that_closes_a_loop_is_the_one_marked():
    """Adversarial review of the V4 work: the researcher draws 17.Rmd 写出
    raw_news.csv, closing raw -> 16 -> monthly -> 17 -> raw. 8.4 breaks a
    cycle "at the edge that closes them" -- the link just drawn, not the
    machine read raw -> 16 that a step-order DFS would have blamed."""
    links = PIPELINE_LINKS + [dict(_link("h", RMD17, RAW), human=True, entry=1)]
    out = _layout(PIPELINE_NODES, links)
    assert out["cycle"] == ["h"]
    assert _column(out, RAW) == 0 and _column(out, RMD17) == 3  # the pipeline keeps its shape


def test_human_links_close_loops_in_the_order_they_were_asserted():
    """Two human links that only form a loop together: the later entry in
    .rce/mappings.toml is the closing one, and an optimistic link (no entry
    yet) is newer than every written one."""
    a = dict(_link("a", PY16, MONTHLY), human=True, entry=2)
    b = dict(_link("b", MONTHLY, PY16), human=True, entry=1)
    assert _layout([MONTHLY, PY16], [a, b])["cycle"] == ["a"]
    b_new = dict(_link("b", MONTHLY, PY16), human=True)
    assert _layout([MONTHLY, PY16], [a, b_new])["cycle"] == ["b"]


def test_unlinked_ghost_sits_beside_its_frame_not_in_column_zero():
    """The knitted .pdf of 8.1 (no links yet) goes one column right of its
    attempt's linked script -- where the link the researcher is about to
    draw will run -- instead of far left with the raw inputs."""
    out = _layout(PIPELINE_NODES + [PDF17], PIPELINE_LINKS, frames=[[RMD17, PDF17]])
    assert _column(out, PDF17) == _column(out, RMD17) + 1


def test_cards_of_different_frames_get_room_for_the_frame_title():
    """Stacked cards from two attempts are spaced so the lower frame's
    title is not hidden under the upper frame (frames never move cards;
    the layout leaves them room)."""
    out = _layout(PIPELINE_NODES, PIPELINE_LINKS, frames=[[RMD17], [RMD18]])
    y17, y18 = out["positions"][RMD17][1], out["positions"][RMD18][1]
    script_height = 26 + 46 + 2 * 20 + 8
    assert y18 - (y17 + script_height) >= 24 + 2 * 16 + 26


# -- Link editing (DESIGN.md 8.1 grammar, 8.3; task V4 phase 2b) ---------------
#
# The drop-time grammar check, the assertion line and the socket hit test
# are pure functions of plain node objects, exposed on RCECanvas like the
# layout, so they are pinned the same way: the real file under node.

_CALL_RUNNER = """
global.window = {};
require(process.argv[1]);
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const C = window.RCECanvas;
let out;
if (input.fn === "rules") out = C._linkRules;
else if (input.fn === "socketAt") {
  const pos = input.positions;
  out = C._socketAt(input.nodes, (id) => pos[id], input.side, input.x, input.y, input.radius);
} else out = C["_" + input.fn](...input.args);
process.stdout.write(JSON.stringify(out === undefined ? null : out));
"""


def _call(fn: str, **payload: Any) -> Any:
    result = subprocess.run(
        [NODE, "-e", _CALL_RUNNER, str(CANVAS_JS)],
        input=json.dumps({"fn": fn, **payload}), capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


def _check(frm: str, out_index: int, to: str | None, in_index: int = 0, links: list[dict[str, Any]] | None = None):
    return _call("checkConnection", args=[_node(frm), out_index, _node(to) if to else None, in_index, links or []])


def test_js_link_rules_mirror_the_mappings_grammar_exactly():
    """One table in the JS, one in the ingest: every rule the canvas offers
    is an entry the server accepts, and every entry type is drawable."""
    from rce.ingest import mappings

    rules = _call("rules")
    derived = {rule["type"]: (key.split(":")[0], rule["toType"]) for key, rule in rules.items()}
    assert derived == mappings.GRAMMAR
    assert len(rules) == len(mappings.GRAMMAR)


@pytest.mark.parametrize(
    "frm, out_index, to, edge_type",
    [
        (MONTHLY, 0, RMD17, "reads"),       # 数据集.数据 -> 脚本.读取
        (PY16, 0, MONTHLY, "writes"),       # 脚本.写出 -> 数据集.来源
        (RMD17, 1, PDF17, "generates"),     # 脚本.生成 -> 图表.生成自
    ],
)
def test_compatible_pairings_name_the_entry_in_file_direction(frm, out_index, to, edge_type):
    out = _check(frm, out_index, to)
    assert out == {
        "ok": True, "type": edge_type,
        "from": frm.partition(":")[2], "to": to.partition(":")[2],
    }


@pytest.mark.parametrize(
    "frm, out_index, to, reason",
    [
        (MONTHLY, 0, PDF17, "只能把数据集接到脚本的「读取」插口"),
        (MONTHLY, 0, RAW, "只能把数据集接到脚本的「读取」插口"),
        (PY16, 0, PDF17, "「写出」只能接到数据集的「来源」插口"),
        (RMD17, 1, MONTHLY, "「生成」只能接到图表的「生成自」插口"),
        (RMD17, 1, RMD18, "「生成」只能接到图表的「生成自」插口"),
    ],
)
def test_incompatible_pairings_are_refused_in_product_language(frm, out_index, to, reason):
    assert _check(frm, out_index, to) == {"ok": False, "reason": reason}


def test_a_card_cannot_link_to_itself_or_to_a_non_input():
    assert _check(PY16, 0, PY16)["ok"] is False
    assert _check(MONTHLY, 0, RMD17, in_index=1)["ok"] is False
    assert _check(PDF17, 0, RMD17)["ok"] is False  # a figure has no outputs


def test_duplicate_human_mapping_is_refused_but_a_machine_twin_is_not():
    """8.5: the same from/to/type twice is 「这条映射已存在」; a mapping that
    repeats or contradicts a MACHINE edge is allowed."""
    human = {"id": "h", "from": RMD17, "to": PDF17, "type": "generates", "human": True}
    machine = dict(human, id="m", human=False)
    assert _check(RMD17, 1, PDF17, links=[human]) == {"ok": False, "reason": "这条映射已存在"}
    assert _check(RMD17, 1, PDF17, links=[machine])["ok"] is True


def test_assertion_line_matches_the_design_example():
    rmd = {**_node(RMD17), "label": "17-叙事更替与汇率波动.Rmd"}
    pdf = {**_node(PDF17), "label": "17-叙事更替与汇率波动.pdf"}
    assert _call("assertionText", args=[rmd, pdf, "generates"]) == (
        "17-叙事更替与汇率波动.Rmd 生成 → 17-叙事更替与汇率波动.pdf"
    )
    # A script is the actor of both its verbs; the arrow follows the data.
    assert _call("assertionText", args=[_node(MONTHLY), _node(RMD17), "reads"]) == (
        "17-叙事更替与汇率波动.Rmd 读取 ← topicshift_monthly.csv"
    )
    assert _call("assertionText", args=[_node(PY16), _node(MONTHLY), "writes"]) == (
        "16-构建指标.py 写出 → topicshift_monthly.csv"
    )


def test_socket_hit_test_picks_the_nearest_socket_within_the_radius():
    """A script's 写出 (index 0) and 生成 (index 1) sit 20 world units apart
    on its right edge at y = 26 + 46 + 10 (+ 20); the hit test returns the
    nearer one, honours the radius, and keeps inputs and outputs apart."""
    nodes = [_node(PY16)]
    positions = {PY16: [100, 50]}
    out0_y, out1_y = 50 + 82, 50 + 102
    hit = _call("socketAt", nodes=nodes, positions=positions, side="out", x=322, y=out1_y - 3, radius=12)
    assert (hit["id"], hit["index"]) == (PY16, 1)
    hit = _call("socketAt", nodes=nodes, positions=positions, side="out", x=318, y=out0_y + 2, radius=12)
    assert (hit["id"], hit["index"]) == (PY16, 0)
    assert _call("socketAt", nodes=nodes, positions=positions, side="out", x=340, y=out0_y, radius=12) is None
    # At 25% zoom the caller passes 12 / 0.25 = 48 world units: still hit.
    assert _call("socketAt", nodes=nodes, positions=positions, side="out", x=340, y=out0_y, radius=48) is not None
    hit = _call("socketAt", nodes=nodes, positions=positions, side="in", x=101, y=out0_y, radius=12)
    assert (hit["id"], hit["index"]) == (PY16, 0)
    assert _call("socketAt", nodes=nodes, positions=positions, side="in", x=320, y=out0_y, radius=12) is None


# -- Adversarial review of the V4 work: static pins on canvas.js / app.html ------

_CANVAS_SRC = CANVAS_JS.read_text(encoding="utf-8")
_APP_SRC = (CANVAS_JS.parent / "app.html").read_text(encoding="utf-8")


def test_scope_is_never_restored_from_browser_storage():
    """8.7: the selector defaults to the current attempt. A remembered
    scope replaced that default for good."""
    assert "localStorage" not in _CANVAS_SRC


def test_layout_runs_on_the_whole_graph_so_positions_are_global():
    """8.7 "positions are global": the auto layout is computed from the
    scope-all payload whenever the view is narrower."""
    fetch = _CANVAS_SRC[_CANVAS_SRC.index("async function fetchCanvas"):]
    fetch = fetch[: fetch.index("\n  }\n")]
    assert "apiGet(canvasUrl(SCOPE_ALL))" in fetch
    apply = _CANVAS_SRC[_CANVAS_SRC.index("function applyPayload"):]
    apply = apply[: apply.index("\n  }\n")]
    assert "computeLayout(whole.nodes, layoutLinks, whole.frames)" in apply


def test_a_cycle_link_keeps_its_human_or_machine_class_and_its_dot():
    link_class = _CANVAS_SRC[_CANVAS_SRC.index("function linkClass"):]
    link_class = link_class[: link_class.index("\n  }\n")]
    assert '["cv-link", link.human ? "human" : "machine"]' in link_class
    render = _CANVAS_SRC[_CANVAS_SRC.index("function renderLink("):]
    render = render[: render.index("\n  }\n")]
    assert "if (link.human) {" in render and "cv.cycle" not in render
    assert ".cv-link.human.cycle { stroke-dasharray: 6 4; }" in _APP_SRC


def test_engine_errors_are_hover_titles_not_visible_english():
    """8.8: the visible text is the Chinese framing; the engine's English
    lives on the hover title."""
    assert "Something went wrong" not in _APP_SRC
    box = _APP_SRC[_APP_SRC.index("function renderErrorBox"):]
    box = box[: box.index("\n}\n")]
    assert "box.title = " in box and "出了点问题" in box
    assert '"移除失效项目失败（悬停查看原因）"' in _APP_SRC
