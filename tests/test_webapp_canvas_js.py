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
const out = window.RCECanvas._computeLayout(input.nodes, input.links, input.frames, input.opts);
process.stdout.write(JSON.stringify({ positions: out.positions, cycle: [...out.cycle], loose: out.loose, islands: out.islands }));
"""


def _node(node_id: str) -> dict[str, Any]:
    node_type, _, path = node_id.partition(":")
    return {"id": node_id, "type": node_type, "path": path}


def _link(link_id: str, frm: str, to: str) -> dict[str, Any]:
    return {"id": link_id, "from": frm, "to": to}


def _layout(
    nodes: list[str], links: list[dict[str, Any]], frames: list[Any] | None = None,
    *, fixed: dict[str, list[int]] | None = None, current: str | None = None, ghosts: set[str] = frozenset(),
) -> dict[str, Any]:
    payload = {
        "nodes": [dict(_node(n), ghost=n in ghosts) for n in nodes],
        "links": links,
        # A frame is a list of node ids, or (attempt id, node ids).
        "frames": [
            {"attempt_id": f[0], "node_ids": f[1]} if isinstance(f, tuple) else {"node_ids": f}
            for f in (frames or [])
        ],
        "opts": {"fixed": fixed or {}, "current": current},
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


# -- 8.4 as amended: islands, sub-columns, the loose block, packing -----------

DATASET_H, SCRIPT_H = 26 + 46 + 20 + 8, 26 + 46 + 2 * 20 + 8


def _height(node_id: str) -> int:
    return SCRIPT_H if node_id.startswith("script:") else DATASET_H


def _bbox(out: dict[str, Any], ids: list[str] | None = None) -> tuple[float, float, float, float]:
    ids = ids if ids is not None else list(out["positions"])
    xs = [out["positions"][i][0] for i in ids]
    ys = [out["positions"][i][1] for i in ids]
    x1 = max(out["positions"][i][0] + 220 for i in ids)
    y1 = max(out["positions"][i][1] + _height(i) for i in ids)
    return min(xs), min(ys), x1, y1


def _overlap(out: dict[str, Any]) -> list[tuple[str, str]]:
    rects = {i: (x, y, x + 220, y + _height(i)) for i, (x, y) in out["positions"].items()}
    ids = sorted(rects)
    return [
        (a, b) for k, a in enumerate(ids) for b in ids[k + 1:]
        if rects[a][0] < rects[b][2] and rects[b][0] < rects[a][2] and rects[a][1] < rects[b][3] and rects[b][1] < rects[a][3]
    ]


def _pipeline(tag: str, step: int, outputs: int = 2) -> tuple[list[str], list[dict[str, Any]]]:
    """raw -> NN-a.py -> mid -> (NN+1)-b.Rmd -> `outputs` figures."""
    raw, a = f"dataset:{tag}/raw.csv", f"script:{tag}/{step:02d}-a.py"
    mid, b = f"dataset:{tag}/mid.csv", f"script:{tag}/{step + 1:02d}-b.Rmd"
    figs = [f"figure:{tag}/{step + 1:02d}-fig{k}.pdf" for k in range(outputs)]
    links = [_link(f"{tag}r1", raw, a), _link(f"{tag}w1", a, mid), _link(f"{tag}r2", mid, b)]
    links += [_link(f"{tag}g{k}", b, f) for k, f in enumerate(figs)]
    return [raw, a, mid, b, *figs], links


def test_islands_split_into_separate_pipelines():
    n1, l1 = _pipeline("p1", 10)
    n2, l2 = _pipeline("p2", 20, outputs=1)
    out = _layout(n1 + n2, l1 + l2)
    assert [set(i) for i in out["islands"]] == [set(n1), set(n2)]  # bigger first
    assert _layout(n1 + n2, l1 + l2)["loose"] is None
    # Each island keeps its own layering from column 0 of its own band.
    x1 = _bbox(out, n1)
    x2 = _bbox(out, n2)
    assert out["positions"][n1[0]][0] == x1[0] and out["positions"][n2[0]][0] == x2[0]
    assert x2[0] - x1[2] >= 96 or x2[1] - x1[3] >= 96  # 96px between islands
    assert _overlap(out) == []


def test_a_ghost_joins_its_step_prefix_script_without_a_link():
    """8.4 step 1: the knitted 17-….pdf ghost is placed one layer right of
    17-….Rmd of its own attempt -- placement only, no link reported."""
    out = _layout(PIPELINE_NODES + [PDF17], PIPELINE_LINKS, frames=[("att17", [RMD17, PDF17])], ghosts={PDF17})
    assert len(out["islands"]) == 1 and PDF17 in out["islands"][0]
    assert out["positions"][PDF17][0] == out["positions"][RMD17][0] + 320
    assert out["loose"] is None and out["cycle"] == []
    # Without the attempt (another scope's frame, or none) it is loose.
    alone = _layout(PIPELINE_NODES + [PDF17], PIPELINE_LINKS, ghosts={PDF17})
    assert PDF17 not in alone["islands"][0] and alone["loose"] is not None
    # A different prefix is not its script.
    other = f"figure:{S}/19-其它.pdf"
    out = _layout(PIPELINE_NODES + [other], PIPELINE_LINKS, frames=[("att17", [RMD17, other])], ghosts={other})
    assert other not in out["islands"][0]


def test_a_layer_of_13_wraps_into_two_sub_columns():
    script = f"script:{S}/05-拆分.py"
    outs = [f"dataset:{S}/out{k:02d}.csv" for k in range(13)]
    out = _layout([script, *outs], [_link(f"w{k}", script, o) for k, o in enumerate(outs)])
    xs = sorted({out["positions"][o][0] for o in outs})
    assert len(xs) == 2 and xs[1] - xs[0] == 220 + 220  # 220px between sub-columns
    per = [sum(1 for o in outs if out["positions"][o][0] == x) for x in xs]
    assert per == [7, 6] and max(per) <= 12
    assert _overlap(out) == []
    twelve = _layout([script, *outs[:12]], [_link(f"w{k}", script, o) for k, o in enumerate(outs[:12])])
    assert len({twelve["positions"][o][0] for o in outs[:12]}) == 1


def test_a_wrapped_layer_widens_its_band_for_the_next_layer():
    script = f"script:{S}/05-拆分.py"
    outs = [f"dataset:{S}/out{k:02d}.csv" for k in range(13)]
    reader = f"script:{S}/06-读.py"
    links = [_link(f"w{k}", script, o) for k, o in enumerate(outs)] + [_link("r", outs[0], reader)]
    out = _layout([script, *outs, reader], links)
    right_of_band = max(out["positions"][o][0] for o in outs) + 220
    assert out["positions"][reader][0] - right_of_band == 100  # the usual column gap after the band


def test_loose_cards_gather_in_one_four_column_block_last():
    n1, l1 = _pipeline("p1", 10)
    loose = [f"dataset:散/z{k}.csv" for k in range(5)] + [f"figure:散/a{k}.png" for k in range(2)] + [f"script:散/m.py"]
    out = _layout(n1 + loose, l1)
    assert [set(i) for i in out["islands"]] == [set(n1)]
    block = out["loose"]
    assert block is not None
    xs = sorted({out["positions"][i][0] for i in loose})
    assert len(xs) == 4 and xs[0] == block["x"]
    # Ordered by type then path: datasets, then the script, then figures.
    by_row = sorted(loose, key=lambda i: (out["positions"][i][1], out["positions"][i][0]))
    assert by_row == sorted(loose[:5]) + [loose[7]] + sorted(loose[5:7])
    # Captioned: the cards start below the caption's room.
    # Captioned: the cards start below the caption, which clears the
    # title band (16 + 26) a frame would draw above them.
    assert min(out["positions"][i][1] for i in loose) - block["y"] == 64
    assert _overlap(out) == []


def test_packing_order_current_attempt_then_size_then_step():
    big, lb = _pipeline("big", 40, outputs=4)
    mid_a, la = _pipeline("ma", 30, outputs=2)
    mid_b, lbb = _pipeline("mb", 20, outputs=2)
    small, ls = _pipeline("sm", 50, outputs=0)
    out = _layout(big + mid_a + mid_b + small, lb + la + lbb + ls,
                  frames=[("cur", [small[1], small[3]])], current="cur")
    assert [set(i) for i in out["islands"]] == [set(small), set(big), set(mid_b), set(mid_a)]
    # Row-major: each next island starts right of, or below, the previous.
    corners = [_bbox(out, i)[:2] for i in out["islands"]]
    for (xa, ya), (xb, yb) in zip(corners, corners[1:]):
        assert yb > ya or (yb == ya and xb > xa)


def _chain(tag: str, step: int, scripts: int, outputs: int) -> tuple[list[str], list[dict[str, Any]]]:
    """raw -> script -> data -> script ... -> `outputs` figures of the last."""
    nodes = [f"dataset:{tag}/raw.csv"]
    links = []
    for k in range(scripts):
        script = f"script:{tag}/{step + k:02d}-s{k}.py"
        links.append(_link(f"{tag}r{k}", nodes[-1], script))
        nodes.append(script)
        if k < scripts - 1:
            data = f"dataset:{tag}/d{k}.csv"
            links.append(_link(f"{tag}w{k}", script, data))
            nodes.append(data)
    last = nodes[-1]
    for k in range(outputs):
        fig = f"figure:{tag}/{step + scripts - 1:02d}-fig{k}.pdf"
        links.append(_link(f"{tag}g{k}", last, fig))
        nodes.append(fig)
    return nodes, links


# A project shaped like the researcher's: many small pipelines of
# different depths and fan-outs, plus files nothing links.
SYNTHETIC_SHAPES = [(3, 6), (2, 5), (1, 4), (2, 2), (1, 1), (3, 1), (1, 3), (2, 4),
                    (1, 2), (1, 1), (2, 1), (1, 6), (1, 1), (2, 3), (1, 2), (1, 0), (2, 2)]


def _synthetic_120() -> tuple[list[str], list[dict[str, Any]]]:
    nodes: list[str] = []
    links: list[dict[str, Any]] = []
    for k, (scripts, outputs) in enumerate(SYNTHETIC_SHAPES):
        n, l = _chain(f"p{k:02d}", 4 * k + 1, scripts, outputs)
        nodes += n
        links += l
    nodes += [f"dataset:散/loose{k:02d}.csv" for k in range(120 - len(nodes))]
    assert len(nodes) == 120 and len(nodes) - len(links) >= 20
    return nodes, links


def test_a_120_card_graph_packs_into_a_page_not_a_tower():
    nodes, links = _synthetic_120()
    out = _layout(nodes, links)
    x0, y0, x1, y1 = _bbox(out)
    aspect = (x1 - x0) / (y1 - y0)
    assert 1.0 <= aspect <= 2.4, aspect
    assert _overlap(out) == []


def test_a_ten_card_scope_lays_out_compactly_in_a_120_card_project():
    """Acceptance found the default scope's 10 cards scattered over
    4,000px when laid out over the whole graph. Per view, the same 10
    cards (the scope's visible slice: its scripts, what they touch, one
    hop upstream and a ghost) fit in well under 1800 x 900."""
    nodes, links = _synthetic_120()
    # The scope's slice: two step scripts, what they read and write, one
    # extra input, and the knitted report waiting as a ghost.
    scope_nodes, scope_links = _chain("p00", 1, 2, 4)
    extra = "dataset:p00/aux.csv"
    ghost = "figure:p00/02-报告.pdf"
    scope_nodes = scope_nodes + [extra, ghost]
    scope_links = scope_links + [_link("aux", extra, "script:p00/01-s0.py")]
    assert len(scope_nodes) == 10
    frame = ("cur", [n for n in scope_nodes if n.startswith("script:")] + [ghost])
    out = _layout(scope_nodes, scope_links, frames=[frame], current="cur", ghosts={ghost})
    x0, y0, x1, y1 = _bbox(out)
    assert x1 - x0 < 1800 and y1 - y0 < 900, (x1 - x0, y1 - y0)
    assert out["loose"] is None and _overlap(out) == []
    assert len(nodes) == 120  # the project around it is irrelevant to the view


def test_saved_positions_are_untouched_and_only_order_their_neighbors():
    """A saved card is not placed (no position returned for it) and does not
    join islands; it only orders unsaved cards linked to it."""
    out = _layout(PIPELINE_NODES, PIPELINE_LINKS, fixed={MONTHLY: [5000, 7000]})
    assert MONTHLY not in out["positions"]
    assert all(MONTHLY not in i for i in out["islands"])
    # raw -> 16 is one island; 17 and 18 (linked only to the saved csv)
    # are islands of one -- linked, so not loose.
    assert out["loose"] is None
    assert {frozenset(i) for i in out["islands"]} == {frozenset({RAW, PY16}), frozenset({RMD17}), frozenset({RMD18})}
    # The fixed neighbor orders two unsaved cards in the same layer.
    a, b = f"script:{S}/30-a.py", f"script:{S}/31-b.py"
    lo, hi = f"dataset:{S}/lo.csv", f"dataset:{S}/hi.csv"
    src = f"dataset:{S}/src.csv"
    links = [_link("ra", src, a), _link("rb", src, b), _link("wa", a, hi), _link("wb", b, lo)]
    out = _layout([src, a, b, lo, hi], links, fixed={lo: [0, 0], hi: [0, 900]})
    assert out["positions"][b][1] < out["positions"][a][1]  # b's saved output sits higher


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


def test_layout_runs_on_the_view_alone():
    """8.4 as amended: auto-layout belongs to the view. One request for the
    scope's own cards -- the extra scope-all fetch that laid unsaved cards
    out over the whole graph is gone -- and the layout is run on that
    payload, saved positions passed in as fixed."""
    fetch = _CANVAS_SRC[_CANVAS_SRC.index("async function fetchCanvas"):]
    fetch = fetch[: fetch.index("\n  }\n")]
    assert "SCOPE_ALL" not in fetch and fetch.count("apiGet(") == 2  # the scope, or the default after a stale scope
    view = _CANVAS_SRC[_CANVAS_SRC.index("function layoutView"):]
    view = view[: view.index("\n  }\n")]
    assert "computeLayout(d.nodes, d.links, d.frames, { fixed: cv.positions" in view


def test_scope_switch_clears_selection_and_the_pinned_link_card():
    """8.7: they belong to the view that was left -- cleared before the
    new scope's payload arrives."""
    change = _CANVAS_SRC[_CANVAS_SRC.index("function changeScope"):]
    change = change[: change.index("\n  }\n")]
    assert change.index("selectLink(null)") < change.index("load(cv.container")
    assert "cv.selected = null" in change and "closePopover()" in change


def test_relayout_forgets_only_the_visible_cards_and_still_asks():
    relayout = _CANVAS_SRC[_CANVAS_SRC.index("function relayout"):]
    relayout = relayout[: relayout.index("\n  }\n")]
    assert "window.confirm(" in relayout and "将丢弃你手动摆放的位置" in relayout
    assert "cv.nodes.forEach((n, id) => { if (id in cv.positions) queuePosition(id, null); });" in relayout
    assert relayout.index("queuePosition(id, null)") < relayout.index("layoutView(true)") < relayout.index("fitAll()")


def test_a_saved_viewport_is_restored_only_if_it_shows_this_view():
    apply = _CANVAS_SRC[_CANVAS_SRC.index("function applyPayload"):]
    apply = apply[: apply.index("\n  }\n")]
    assert "vp && savedViewportFits(vp)" in apply
    check = _CANVAS_SRC[_CANVAS_SRC.index("function savedViewportFits"):]
    check = check[: check.index("\n  }\n")]
    assert "ids.some(" in check and "fit.zoom" in check


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
