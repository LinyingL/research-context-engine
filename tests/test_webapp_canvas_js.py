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
import math
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
process.stdout.write(JSON.stringify({ positions: out.positions, cycle: [...out.cycle], loose: out.loose, looseIds: out.looseIds, islands: out.islands }));
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


def test_unsaved_cards_are_packed_clear_of_saved_cards():
    """Verifier finding on bb76a9f: the researcher nudges 16-构建指标.py
    50px (saved at (367, 74)); on the next layout the rest of its pipeline
    was packed from the origin blind to it and 17-….Rmd landed under it.
    Saved cards still take no part in steps 1-5; packing only keeps every
    island and the 「未连线」 block 96px clear of them."""
    n2, l2 = _pipeline("p2", 30)
    loose = [f"dataset:散/z{k}.csv" for k in range(3)]
    for fixed in ({PY16: [367, 74]}, {PY16: [0, 0]}, {PY16: [367, 74], RMD18: [900, 300]}):
        out = _layout(PIPELINE_NODES + n2 + loose, PIPELINE_LINKS + l2, fixed=fixed)
        placed = {i: p for i, p in out["positions"].items()}
        placed.update(fixed)
        assert set(fixed).isdisjoint(out["positions"])  # still never moved
        assert _overlap({"positions": placed}) == [], fixed
        for sid, (sx, sy) in fixed.items():
            for cid, (x, y) in out["positions"].items():
                apart_x = max(x - (sx + 220), sx - (x + 220))
                apart_y = max(y - (sy + _height(sid)), sy - (y + _height(cid)))
                assert max(apart_x, apart_y) >= 96, (sid, cid)


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
    assert "computeLayout(d.nodes, d.links, d.frames, {" in view
    assert "fixed: cv.positions, current: currentAttemptId(d), groups: d.step_groups" in view


def test_scope_switch_clears_selection_and_the_pinned_link_card():
    """8.7: they belong to the view that was left -- cleared before the
    new scope's payload arrives."""
    change = _CANVAS_SRC[_CANVAS_SRC.index("function changeScope"):]
    change = change[: change.index("\n  }\n")]
    assert change.index("selectLink(null)") < change.index("load(cv.container")
    assert "cv.selected = null" in change and "closePopover()" in change


def test_each_view_restores_its_own_viewport_with_no_cross_view_heuristic():
    """8.4/8.6 as amended: each view keeps its own viewport, so the camera
    restores it on entering the view, else fits -- the old "restore only if
    it shows a card of this view" guess is gone."""
    assert "savedViewportFits" not in _CANVAS_SRC
    apply = _CANVAS_SRC[_CANVAS_SRC.index("function applyPayload"):]
    apply = apply[: apply.index("\n  }\n")]
    assert "if (vp) {" in apply and "} else autoFitAll();" in apply


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


def test_a_relayout_keeps_the_anchor_card_where_it_was_on_screen():
    """Verifier finding on bb76a9f: confirming a link re-packs every unsaved
    card (8.4: laid out fresh), and the camera stayed put, so the new link
    and both of its ends could leave the screen. The cards still move; the
    camera follows the drop target so it stays under the pointer."""
    cam = {"x": 100, "y": 50, "zoom": 0.5}
    view = {"w": 1280, "h": 840}
    before = {"to": [320, 0], "from": [1596, 442]}
    after = {"to": [960, 52], "from": [640, 124]}
    out = _call("keepCamera", args=[cam, view, before, after, ["to", "from"], []])
    assert out["zoom"] == 0.5
    # Screen position of the anchor is unchanged: cam + world * zoom.
    assert out["x"] + 960 * 0.5 == 100 + 320 * 0.5 and out["y"] + 52 * 0.5 == 50 + 0 * 0.5
    # An anchor that is gone falls through to the next one.
    out = _call("keepCamera", args=[cam, view, {"from": [0, 0]}, {"from": [10, 20]}, ["to", "from"], []])
    assert (out["x"], out["y"]) == (95, 40)
    # No anchor at all: the camera is left alone.
    assert _call("keepCamera", args=[cam, view, {}, {}, [], []]) == cam


def test_a_relayout_pans_the_least_needed_to_show_both_ends_of_the_new_link():
    cam = {"x": 0, "y": 0, "zoom": 1}
    view = {"w": 1280, "h": 840}
    before, after = {"to": [100, 100]}, {"to": [100, 100]}
    # The other end landed off the right edge; both fit, so pan left just enough.
    show = [[100, 100, 320, 200], [1100, 300, 1320, 400]]
    out = _call("keepCamera", args=[cam, view, before, after, ["to"], show])
    assert out["x"] == -(1320 - (1280 - 24)) and out["y"] == 0
    # Too far apart to show both: the anchor alone holds.
    show = [[100, 100, 320, 200], [3000, 300, 3220, 400]]
    assert _call("keepCamera", args=[cam, view, before, after, ["to"], show])["x"] == 0


def test_confirming_a_link_focuses_its_two_cards_for_the_relayout():
    confirm = _CANVAS_SRC[_CANVAS_SRC.index("async function confirmPopover"):]
    confirm = confirm[: confirm.index("\n  }\n")]
    assert "cv.focus = [p.toId, p.fromId];" in confirm
    assert confirm.index("cv.focus = [p.toId, p.fromId];") < confirm.index("await refresh()")
    apply = _CANVAS_SRC[_CANVAS_SRC.index("function applyPayload"):]
    apply = apply[: apply.index("\n  }\n")]
    assert "const relaid = layoutView(entering);" in apply
    assert "if (relaid && !entering && !cv.pinned) {" in apply  # unpinned views only (8.4)
    assert apply.index("cameraAnchors()") < apply.index("cv.positions = ") < apply.index("keepCamera(")


def test_a_frame_is_not_stretched_out_to_its_member_in_the_loose_block():
    """Verifier finding on bb76a9f: attempt A's helper `clean.R` (no links,
    no step prefix) is loose per 8.4 step 4 and sits in the 「未连线」
    block; frame A was drawn as the bounding box of 14-a.py and clean.R,
    enclosing m1.csv, an unrelated unused.csv and the caption. The frame
    is drawn around A's members outside the block."""
    a, clean = "script:A/14-a.py", "script:A/clean.R"
    b, c = "script:B/20-b.py", "script:C/21-c.py"
    r1, m1, r2, m2, m3 = (f"dataset:x/{n}.csv" for n in ("r1", "m1", "r2", "m2", "m3"))
    unused = "dataset:x/unused.csv"
    links = [_link("1", r1, a), _link("2", a, m1), _link("3", r2, b), _link("4", b, m2),
             _link("5", m2, c), _link("6", c, m3)]
    frames = [("A", [a, clean]), ("B", [b]), ("C", [c])]
    out = _layout([r1, a, m1, clean, r2, b, m2, c, m3, unused], links, frames=frames)
    assert set(out["looseIds"]) == {clean, unused}
    members = _call("frameMembers", args=[[a, clean], out["looseIds"]])
    assert members == [a]
    x0, y0, x1, y1 = _bbox(out, members)
    for other in (unused, m1):
        ox, oy = out["positions"][other]
        assert not (ox < x1 and x0 < ox + 220 and oy < y1 and y0 < oy + _height(other)), other
    # An attempt whose every visible member is loose keeps its frame.
    assert _call("frameMembers", args=[[clean], [clean, unused]]) == [clean]
    assert _call("frameMembers", args=[[a, b], []]) == [a, b]


# -- 8.4 / 8.6 as amended: each view keeps its own arrangement ---------------
#
# The pin-on-first-move flow is state, not a pure function, but it needs no
# DOM: the real file runs under node with `apiPost` and `window.confirm`
# stubbed, a view's payload put in place through the exposed state, and the
# writes it would POST recorded.

_SCENARIO_RUNNER = """
global.window = { confirm: () => { global.asked = (global.asked || 0) + 1; return true; } };
global.posts = [];
global.apiPost = async (url, body) => { posts.push({ url, body: JSON.parse(JSON.stringify(body)) }); return { ok: true }; };
require(process.argv[1]);
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const C = window.RCECanvas, S = C._state;
function enter(payload) {
  S.data = payload;
  S.scope = payload.scope.id;
  S.nodes = new Map(payload.nodes.map((n) => [n.id, n]));
  S.positions = Object.assign({}, payload.positions || {});
  S.pinned = Object.keys(S.positions).length > 0;
  C._layoutView(true);
}
function drag(id, to) { S.positions[id] = to; C._cardMoved(id); }
(async () => {
  const out = await (new Function("C", "S", "enter", "drag", "input", "return (async () => {" + input.script + "})();"))(C, S, enter, drag, input);
  process.stdout.write(JSON.stringify({ out: out === undefined ? null : out, posts, asked: global.asked || 0 }));
})();
"""


def _scenario(script: str, **payload: Any) -> dict[str, Any]:
    result = subprocess.run(
        [NODE, "-e", _SCENARIO_RUNNER, str(CANVAS_JS)],
        input=json.dumps({"script": script, **payload}), capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


def _view(scope: str, nodes: list[str], links: list[dict[str, Any]], positions: dict[str, list[int]] | None = None):
    return {
        "scope": {"id": scope}, "nodes": [_node(n) for n in nodes], "links": links,
        "frames": [], "step_groups": [], "positions": positions or {}, "scopes": [],
    }


def test_the_first_move_pins_every_visible_card_in_one_post():
    """8.4: the first time a card is moved in a view, every visible card's
    position at that moment is saved for that view -- one POST naming the
    scope -- and the cards not moved are saved exactly where the layout had
    them. A later move saves only the moved card."""
    view = _view("attempt:map.md#17", PIPELINE_NODES, PIPELINE_LINKS)
    run = _scenario("""
      enter(input.view);
      const auto = JSON.parse(JSON.stringify(S.auto));
      drag(input.first, [5, 7]);
      await C._flushSave();
      drag(input.second, [900, 40]);
      await C._flushSave();
      return { auto, pinned: S.pinned };
    """, view=view, first=PY16, second=RMD18)
    assert run["out"]["pinned"] is True
    first, second = run["posts"]
    assert first["url"] == second["url"] == "/api/canvas/layout"
    assert first["body"]["scope"] == "attempt:map.md#17"
    expected = {i: run["out"]["auto"][i] for i in PIPELINE_NODES if i != PY16}
    expected[PY16] = [5, 7]
    assert first["body"]["positions"] == expected
    assert "reset" not in first["body"]
    assert second["body"] == {"scope": "attempt:map.md#17", "positions": {RMD18: [900, 40]}}


def test_a_move_in_a_pinned_view_also_saves_cards_that_appeared_since():
    """8.4: a card that appears in a pinned view is placed by the layout
    clear of the pinned cards, and joins the arrangement at the next move."""
    pinned = {RAW: [0, 0], PY16: [320, 0], MONTHLY: [640, 0], RMD17: [960, 0]}
    view = _view("all", PIPELINE_NODES, PIPELINE_LINKS, positions=pinned)
    run = _scenario("""
      enter(input.view);
      const placed = S.auto[input.fresh];
      drag(input.moved, [10, 500]);
      await C._flushSave();
      return { placed, auto: Object.keys(S.auto) };
    """, view=view, fresh=RMD18, moved=RAW)
    assert run["out"]["auto"] == [RMD18]  # only the new card is laid out
    x, y = run["out"]["placed"]
    for sid, (sx, sy) in pinned.items():
        apart_x = max(x - (sx + 220), sx - (x + 220))
        apart_y = max(y - (sy + _height(sid)), sy - (y + _height(RMD18)))
        assert max(apart_x, apart_y) >= 96, sid
    (post,) = run["posts"]
    assert post["body"] == {"scope": "all", "positions": {RAW: [10, 500], RMD18: run["out"]["placed"]}}


def test_writes_go_to_the_view_they_were_made_in():
    """A move queued in one view and unsent when the scope changes is sent
    for THAT view, never for the next one."""
    run = _scenario("""
      enter(input.a);
      drag(input.id, [1, 2]);
      enter(input.b);
      drag(input.id, [3, 4]);
      await C._flushSave();
      await new Promise((r) => setTimeout(r, 0));
    """, a=_view("attempt:map.md#17", [PY16], []), b=_view("all", [PY16], []), id=PY16)
    assert [p["body"] for p in run["posts"]] == [
        {"scope": "attempt:map.md#17", "positions": {PY16: [1, 2]}},
        {"scope": "all", "positions": {PY16: [3, 4]}},
    ]


def test_relayout_asks_then_forgets_this_views_arrangement():
    """「重新排列」 asks 「将丢弃你在这个视图里摆放的位置」, then sends the reset
    for this view (its viewport forgotten too: it fits again) and lays the
    view out afresh -- every card back to the automatic layout."""
    fresh = _scenario("enter(input.view); return S.auto;", view=_view("all", PIPELINE_NODES, PIPELINE_LINKS))["out"]
    pinned = {i: [k * 1000, 3000] for k, i in enumerate(PIPELINE_NODES)}
    run = _scenario("""
      enter(input.view);
      C._relayout();
      await C._flushSave();
      return { pinned: S.pinned, positions: S.positions, auto: S.auto };
    """, view=_view("all", PIPELINE_NODES, PIPELINE_LINKS, positions=pinned))
    assert run["asked"] == 1
    assert run["out"]["pinned"] is False and run["out"]["positions"] == {}
    assert run["out"]["auto"] == fresh
    (post,) = run["posts"]
    assert post["body"] == {"scope": "all", "reset": True, "viewport": None}
    relayout = _CANVAS_SRC[_CANVAS_SRC.index("function relayout"):]
    relayout = relayout[: relayout.index("\n  }\n")]
    assert "将丢弃你在这个视图里摆放的位置" in relayout


@pytest.mark.parametrize("count, cols", [(3, 4), (10, 4), (60, 7), (200, 13)])
def test_the_loose_block_is_page_shaped(count, cols):
    """8.4 step 4: at least 4 columns, more when needed to bring the block
    toward 1.6:1 (rows of dataset cards, the caption included)."""
    assert _call("looseColumns", args=[count, DATASET_H]) == cols
    rows = -(-count // cols)
    w = cols * 244 - 24
    h = 64 + rows * (DATASET_H + 24) - 24
    if count >= 60:
        assert 1.3 <= w / h <= 2.0, w / h
        for other in (cols - 1, cols + 1):  # its neighbours are further from 1.6
            r = -(-count // other)
            ow, oh = other * 244 - 24, 64 + r * (DATASET_H + 24) - 24
            assert abs(math.log(ow / oh / 1.6)) >= abs(math.log(w / h / 1.6))


def test_a_large_loose_block_is_laid_out_wide():
    loose = [f"dataset:散/z{k:03d}.csv" for k in range(60)]
    out = _layout(loose, [])
    assert len({out["positions"][i][0] for i in loose}) == 7
    assert _overlap(out) == []


def test_pinned_loose_cards_stay_loose_and_keep_their_caption():
    """Once a view is pinned its loose cards are fixed, but they are still
    「未连线」: the caption stays over them and frames do not stretch to them."""
    loose = [f"dataset:散/z{k}.csv" for k in range(3)]
    fresh = _layout(PIPELINE_NODES + loose, PIPELINE_LINKS)
    pinned = _layout(PIPELINE_NODES + loose, PIPELINE_LINKS, fixed=fresh["positions"])
    assert pinned["positions"] == {}
    assert set(pinned["looseIds"]) == set(loose)
    assert pinned["loose"] == fresh["loose"]


def test_step_groups_place_companions_in_a_view_without_frames():
    """全部 draws no frame (8.7) but its layout still sits a ghost beside its
    step-prefix script and packs the current attempt first (step_groups)."""
    out = _layout(PIPELINE_NODES + [PDF17], PIPELINE_LINKS, frames=[], ghosts={PDF17})
    assert PDF17 in out["looseIds"]
    payload = {
        "nodes": [_node(n) for n in PIPELINE_NODES + [PDF17]], "links": PIPELINE_LINKS, "frames": [],
        "opts": {"fixed": {}, "current": None, "groups": [{"attempt_id": "a17", "node_ids": [RMD17, PDF17]}]},
    }
    result = subprocess.run(
        [NODE, "-e", _RUNNER, str(CANVAS_JS)], input=json.dumps(payload),
        capture_output=True, text=True, check=True, timeout=30,
    )
    out = json.loads(result.stdout)
    assert out["looseIds"] == [] and _column(out, PDF17) == _column(out, RMD17) + 1


def test_resize_refits_only_until_the_researcher_moves_the_camera():
    """8.4: until they pan or zoom, the camera re-fits on resize; fits are
    not saved as the view's viewport, pans and zooms are."""
    resize = _CANVAS_SRC[_CANVAS_SRC.index("function onResize"):]
    resize = resize[: resize.index("\n  }\n")]
    assert "cv.autoFit &&" in resize and "RESIZE_DEBOUNCE_MS" in resize
    assert "new ResizeObserver(onResize)" in _CANVAS_SRC
    user = _CANVAS_SRC[_CANVAS_SRC.index("function userCamera"):]
    user = user[: user.index("\n  }\n")]
    assert "cv.autoFit = false;" in user and "queueViewport();" in user
    fit = _CANVAS_SRC[_CANVAS_SRC.index("function fitTo("):]
    fit = fit[: fit.index("\n  }\n")]
    assert "queueViewport" not in fit
    for handler in ("function onWheel", "function setZoomAbout", "function onPointerUp"):
        body = _CANVAS_SRC[_CANVAS_SRC.index(handler):]
        body = body[: body.index("\n  }\n")]
        assert "userCamera()" in body, handler
