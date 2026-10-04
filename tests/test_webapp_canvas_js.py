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
