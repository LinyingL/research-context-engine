/*
  RCE node canvas (DESIGN.md section 8, task V4 phases 2a-2b): the 「画布」 view.

  Served verbatim by rce.webapp.server at GET /canvas.js (same read-fresh,
  same origin-check discipline as app.html) and loaded by app.html with a
  plain same-origin <script src> placed BEFORE the page's inline script.
  Nothing here runs at load time except defining window.RCECanvas: every
  function that touches app.html's own helpers (apiGet, apiPost,
  openFilePanel, renderViewFailure, renderBlockingError, clearProjectState,
  projectStateOf,
  verdictMarker, VERDICT_BADGE_CLASS, state) does so only when called, by
  which time the inline script has defined them. Wrapped in one IIFE so no
  name here can collide with the page's own top-level declarations.

  What phase 2a draws (8.0-8.4, 8.7, 8.8) -- rendering, navigation and
  moving cards:

    - an SVG canvas on --paper with a 24px dot grid in --line (dots, not
      lines -- lines fight the Béziers); pan by dragging empty canvas,
      two-finger scroll or Space+drag; zoom 25%-250% about the cursor with
      Cmd/Ctrl+wheel and pinch (ctrl+wheel in Chromium, gesture events in
      WebKit -- the native shell is WebKit);
    - cards exactly per 8.2: type rule + mono type label in the title bar,
      basename in the UI face, directory in mono truncated from the LEFT so
      the meaningful tail survives, sockets on the card edge per the 8.1
      table; states selected / missing / orphan input / ghost;
    - links as horizontal-tangent cubic Béziers under the cards: machine
      links thin ink at 45%, pending dashed; human mappings thicker clay
      with a midpoint dot -- never confusable at a glance (Section 4's
      machine annotation vs. human judgement, made visible);
    - attempt frames behind their step files, titled in the decision
      tree's own verdict colors, never interactive;
    - the 8.4 layered layout for every card the view has not pinned; each
      view (scope) keeps its own arrangement and viewport, pinned whole on
      the first move and persisted debounced 400ms through POST
      /api/canvas/layout, which names the view it writes.

  What phase 2b adds (8.1 grammar, 8.2 link states, 8.3, 8.5) -- editing:

    - drag from an OUTPUT socket to draw a link; compatible inputs pulse,
      the rest dim; the drop is checked against ONE table (LINK_RULES)
      that mirrors rce.ingest.mappings.GRAMMAR -- an incompatible drop
      gets its one-line refusal, a drop on nothing does nothing;
    - the confirm popover (assertion line, 备注, 确认标注 / 取消) -> POST
      /api/mappings/add, the human link drawn optimistically and
      reconciled by the re-fetch, taken back with a Chinese chip (engine
      text on hover) if the write fails;
    - click a link to select it and pin its card; right-click or the
      card's text button: 删除标注 (human, also Backspace/Delete) ->
      /api/mappings/delete, 标记为错误提取 (machine) -> /api/edges/reject
      with 「撤销」 -> /api/edges/restore offered on the chip.

  What V5 adds (DESIGN.md 9.3, 9.6 "Where it shows"; task V5 phase 7):

    - a machine link's pinned card judges it -- 「确认这条连线」,
      「标记为错误提取」, and while a verdict stands 「撤回」 -- each with an
      optional inline 备注, each undoable for 10 seconds (an `undone`
      entry), and shows the link's history from the ledger, newest first,
      with the basis each verdict was made on; a hand-drawn link shows no
      judgment actions (its authority is mappings.toml);
    - a link whose old judgment waits for review is drawn as the machine
      link it is now plus a small ochre ring, and its hover card says what
      the researcher once decided and why it waits; a candidate says it
      may correspond to such a judgment;
    - an arrangement record that cannot be read is said, left untouched,
      and no position is saved over it.

  The page never writes the graph directly from here. Its writes are
  the arrangement record .rce/canvas.json (sections 8.6, 9.2) and the
  canvas write endpoints, which take only paths/ids and resolve every file
  location server-side (mappings.toml from the served root alone, confined
  like every other path); a status change is a judgment, appended to
  .rce/judgements.toml by the server's one human write path and only then
  reflected in the graph (9.1). Every drop is re-validated by the server --
  the JS grammar only spares a round trip.
*/
"use strict";

window.RCECanvas = (function () {
  // The SVG namespace is an identifier, not a fetch: the page still loads
  // nothing from outside this process.
  const SVG_NS = "http://www.w3.org/2000/svg";

  // -- Geometry (8.2 / 8.4) ---------------------------------------------------
  const NODE_W = 220;
  const TITLE_H = 26;
  const BODY_H = 46;          // basename line + directory line
  const ROW_H = 20;           // one socket row
  const NODE_PAD_B = 8;
  const SOCKET_R = 5;         // 10px circles
  const COL_GAP = 320;        // 8.4: columns 320px apart
  const ROW_GAP = 24;         // 8.4: rows packed with 24px gaps
  // 8.4 step 3: a layer taller than 12 cards wraps into sub-columns
  // "220px apart". Read as the space BETWEEN sub-columns: read as a pitch
  // it would equal NODE_W, the cards would touch and one sub-column's
  // output sockets would sit exactly on the next one's input sockets.
  const SUBCOL_MAX = 12;
  const SUBCOL_PITCH = NODE_W + 220;
  const LOOSE_MIN_COLS = 4;   // 8.4 step 4: the 「未连线」 block is at least 4 columns wide
  const LOOSE_GAP = 24;       // ...its cards 24px apart, like the rows
  // Room above the block for its caption, clear of the title band an
  // attempt frame draws above the same cards (FRAME_PAD + FRAME_TITLE_H):
  // the caption must not read as part of a frame's title.
  const LOOSE_CAPTION_H = 64;
  const ISLAND_GAP = 96;      // 8.4 step 5: 96px between islands
  const PAGE_ASPECT = 1.6;    // ...on a page shaped like the window
  const PACK_STEPS = 15;      // ...its target width tried up to 2.5 x the floor
  const GRID = 24;            // 8.2: 24px dot grid
  const SNAP = 8;             // 8.3: 8px snap while Shift is held
  const ZOOM_MIN = 0.25;
  const ZOOM_MAX = 2.5;
  const SAVE_DEBOUNCE_MS = 400;
  const RESIZE_DEBOUNCE_MS = 150;
  const CLICK_DELAY_MS = 280; // a second click inside this window is a double-click
  const DRAG_THRESHOLD = 3;
  const FIT_PAD = 48;
  const FRAME_PAD = 16;
  const FRAME_TITLE_H = 26;

  const TYPE_LABEL = { dataset: "数据集", script: "脚本", figure: "图表" };
  // 8.1's socket table. `carries` picks the socket's color: what flows
  // through it (a dataset or a figure) -- the card's own type color lives
  // on its title rule.
  const SOCKETS = {
    dataset: { inputs: [{ label: "来源", carries: "data" }], outputs: [{ label: "数据", carries: "data" }] },
    script: {
      inputs: [{ label: "读取", carries: "data" }],
      outputs: [{ label: "写出", carries: "data" }, { label: "生成", carries: "figure" }],
    },
    figure: { inputs: [{ label: "生成自", carries: "figure" }], outputs: [] },
  };
  const TONE_BY_BADGE = {
    "badge-olive": "olive", "badge-ochre": "ochre", "badge-clay": "clay",
    "badge-gray": "gray", "badge-plain": "plain",
  };

  // 8.1's socket grammar -- the whole of what a human may draw -- as ONE
  // table: an output socket (card type + output index into SOCKETS) -> the
  // card type whose single input it may join, the edge type that makes,
  // and the one-line refusal for any other drop. Every card has exactly
  // one input (index 0), so the target needs no socket name. It mirrors
  // rce.ingest.mappings.GRAMMAR (entry direction: from = this card, to =
  // the target) and a test pins the two together; the server stays the
  // authority, this only spares the user a round trip and lights the
  // right sockets while dragging.
  const LINK_RULES = {
    "dataset:0": { toType: "script", type: "reads", refusal: "只能把数据集接到脚本的「读取」插口" },
    "script:0": { toType: "dataset", type: "writes", refusal: "「写出」只能接到数据集的「来源」插口" },
    "script:1": { toType: "figure", type: "generates", refusal: "「生成」只能接到图表的「生成自」插口" },
  };
  const DUPLICATE_TEXT = "这条映射已存在";
  // Sockets are 10px to the eye but at least this many SCREEN pixels to
  // the pointer, at every zoom (8.3 edge case: hit-testable at 25%) -- the
  // hit test is geometric, so the visual never grows.
  const SOCKET_HIT_PX = 12;
  const NOTICE_MS = 5000;     // a refusal or a done-notice fades on its own
  const UNDO_MS = 10000;      // 「撤销」 stays offered this long
  const SCOPE_ALL = "all";    // rce.webapp.canvas.SCOPE_ALL


  // -- State --------------------------------------------------------------------
  // positions: THIS view's arrangement (8.4: each view keeps its own) --
  // what canvas.json holds for the scope, plus this page's writes not yet
  // sent for it; pinned: the view has an arrangement, so the layout never
  // moves its cards again. auto: the 8.4 layout of the cards the
  // arrangement does not hold yet; loose: the 「未连线」 cards' rect;
  // layoutKey: what auto was computed for. save: what the next debounced
  // POST sends, for the ONE view it was made in (emptySave); inflight:
  // writes sent and not yet answered, oldest first. project: which project
  // the payload on screen is of, echoed by every write. autoFit: the
  // camera is a fit the researcher has not touched since, so a resize
  // re-fits it (8.4).
  const cv = {
    dom: null, container: null, data: null, scope: null, project: null,
    nodes: new Map(), positions: {}, pinned: false, auto: {}, cycle: new Set(), loose: null, looseIds: [], layoutKey: null,
    camera: { x: 0, y: 0, zoom: 1 }, needsFit: false, autoFit: true, resizeObs: null, resizeTimer: null,
    selected: null, hovered: null, query: "",
    save: emptySave(), saveTimer: null, inflight: [],
    space: false, drag: null, clickTimer: null, lastClick: null,
    loadSeq: 0, nodeEls: new Map(), linkEls: new Map(), listenersBound: false,
    // Editing (phase 2b): the selected link (its hover card pinned), the
    // open confirm popover, the open right-click menu, links drawn
    // optimistically while their write is in flight (keyed by the entry
    // they assert), and what the chip at the bottom currently says.
    selectedLink: null, pop: null, ctx: null, optimistic: new Map(),
    statusKind: null, statusTimer: null,
    // The cards of the link just confirmed, which the camera keeps on
    // screen through the re-layout that link causes (keepCamera).
    focus: null,
    // V5: the selected link's ledger history ({id, entries, readable,
    // error}), and the 备注 typed into its card (kept across re-renders).
    history: null, cardNote: { id: null, value: "" },
  };

  // -- Small helpers ----------------------------------------------------------

  // A layout write for one view (8.6) of one project: its positions (null
  // = forget one), its viewport (undefined = unchanged, null = forget it),
  // and whether it first forgets the view's whole arrangement (「重新排列」).
  function emptySave() {
    return { scope: null, project: null, positions: {}, viewport: undefined, reset: false };
  }

  function hasOwn(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }

  function svgEl(tag, attrs, cls) {
    const el = document.createElementNS(SVG_NS, tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    if (cls) el.setAttribute("class", cls);
    return el;
  }

  function htmlEl(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

  // The numeric step prefix of a path's basename ("16-构建指标.py" -> 16),
  // the tie-break that keeps step order (8.4 step 2).
  function stepKey(path) {
    const base = String(path).split("/").pop();
    const m = /^(\d+)/.exec(base);
    return m ? Number(m[1]) : Infinity;
  }

  function compareByStep(a, b) {
    const ka = stepKey(a.path), kb = stepKey(b.path);
    if (ka !== kb) return ka < kb ? -1 : 1;
    return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
  }

  // -- Text fitting -------------------------------------------------------------
  // SVG text does not ellipsize; a 2D canvas measures with the page's own
  // font stacks (read from the :root tokens, so nothing is restated here).

  let measureCtx = null;

  function fontStack(token) {
    return getComputedStyle(document.documentElement).getPropertyValue(token).trim() || "sans-serif";
  }

  function textWidth(text, font) {
    if (!measureCtx) measureCtx = document.createElement("canvas").getContext("2d");
    measureCtx.font = font;
    return measureCtx.measureText(text).width;
  }

  function fitRight(text, font, maxW) {
    if (textWidth(text, font) <= maxW) return text;
    let lo = 0, hi = text.length;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (textWidth(text.slice(0, mid) + "…", font) <= maxW) lo = mid; else hi = mid - 1;
    }
    return text.slice(0, lo) + "…";
  }

  // 8.2: a directory is truncated from the LEFT -- the tail is the part
  // that tells two copies apart.
  function fitLeft(text, font, maxW) {
    if (textWidth(text, font) <= maxW) return text;
    let lo = 0, hi = text.length;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (textWidth("…" + text.slice(text.length - mid), font) <= maxW) lo = mid; else hi = mid - 1;
    }
    return "…" + text.slice(text.length - lo);
  }

  // -- Card geometry ------------------------------------------------------------

  function socketRows(type) {
    const s = SOCKETS[type] || SOCKETS.dataset;
    return Math.max(s.inputs.length, s.outputs.length, 1);
  }

  function nodeHeight(type) {
    return TITLE_H + BODY_H + socketRows(type) * ROW_H + NODE_PAD_B;
  }

  function socketY(index) {
    return TITLE_H + BODY_H + index * ROW_H + ROW_H / 2;
  }

  function posOf(id) {
    return cv.positions[id] || cv.auto[id] || [0, 0];
  }

  // Which output socket a link leaves from and which input it enters
  // (8.1): a script's 写出 for a dataset, its 生成 for a figure; every
  // card has exactly one input. A link out of a card with no outputs (a
  // script reading a figure file) leaves from the title bar's edge.
  function outSocketIndex(fromNode, toNode) {
    const outs = (SOCKETS[fromNode.type] || {}).outputs || [];
    if (!outs.length) return -1;
    if (fromNode.type === "script" && toNode.type === "figure") return 1;
    return 0;
  }

  function linkEnds(link) {
    const from = cv.nodes.get(link.from), to = cv.nodes.get(link.to);
    if (!from || !to) return null;
    const [fx, fy] = posOf(from.id), [tx, ty] = posOf(to.id);
    const oi = outSocketIndex(from, to);
    return {
      x1: fx + NODE_W, y1: fy + (oi < 0 ? TITLE_H / 2 : socketY(oi)),
      x2: tx, y2: ty + socketY(0),
    };
  }

  // ComfyUI's curve: cubic Bézier with horizontal tangents at both ends.
  // A link that runs backwards (its target sits left of its source -- a
  // cycle's closing edge, or a card the user dragged) still leaves to the
  // right and enters from the left, but swings below both cards instead of straight
  // back through them, so it is never hidden under the cards it joins.
  const BACK_LOOP_DX = 80;
  const BACK_LOOP_DROP = 140;

  function bezier(e) {
    if (e.x2 < e.x1 + 2 * SOCKET_R) {
      const drop = Math.max(e.y1, e.y2) + BACK_LOOP_DROP;
      return { c1x: e.x1 + BACK_LOOP_DX, c1y: drop, c2x: e.x2 - BACK_LOOP_DX, c2y: drop };
    }
    const dx = Math.max(40, Math.abs(e.x2 - e.x1) * 0.5);
    return { c1x: e.x1 + dx, c1y: e.y1, c2x: e.x2 - dx, c2y: e.y2 };
  }

  function linkPathD(e) {
    const c = bezier(e);
    return `M${e.x1},${e.y1} C${c.c1x},${c.c1y} ${c.c2x},${c.c2y} ${e.x2},${e.y2}`;
  }

  function linkMidpoint(e) {
    const c = bezier(e);
    return {
      x: (e.x1 + 3 * c.c1x + 3 * c.c2x + e.x2) / 8,
      y: (e.y1 + 3 * c.c1y + 3 * c.c2y + e.y2) / 8,
    };
  }

  // -- Link grammar and socket hit-testing (8.1 / 8.3) -------------------------
  // Pure functions over plain node objects ({id, type, path, label?}) so
  // the node test runner can pin them without a DOM.

  function nodeLabel(n) {
    return n.label || String(n.path).split("/").pop();
  }

  // Can output socket `outIndex` of `fromNode` join input `inIndex` of
  // `toNode`? Returns {ok: true, type, from, to} -- the mappings.toml entry
  // the link would append (from/to are project-relative paths, entry
  // direction) -- or {ok: false, reason} with the one-line refusal in
  // product language. `links` are the drawn links: an existing HUMAN link
  // asserting the same entry is the server's 409 said early (8.5); a
  // machine link with the same ends is no obstacle -- the human may assert
  // what the machine also found, and both lines show.
  function checkConnection(fromNode, outIndex, toNode, inIndex, links) {
    const rule = LINK_RULES[fromNode.type + ":" + outIndex];
    if (!rule) return { ok: false, reason: "这个插口不能连线" };
    if (!toNode || toNode.id === fromNode.id || inIndex !== 0 || toNode.type !== rule.toType) {
      return { ok: false, reason: rule.refusal };
    }
    const dup = (links || []).some((l) => l.human && l.from === fromNode.id && l.to === toNode.id && l.type === rule.type);
    if (dup) return { ok: false, reason: DUPLICATE_TEXT };
    return { ok: true, type: rule.type, from: fromNode.path, to: toNode.path };
  }

  // The confirm popover's one assertion line (8.3's own example:
  // 「17-….Rmd 生成 → 17-….pdf」). A script is the actor of both its verbs,
  // so a 读取 names the script first; the arrow still points the way the
  // data flows, matching the link on screen.
  function assertionText(fromNode, toNode, type) {
    const a = nodeLabel(fromNode), b = nodeLabel(toNode);
    if (type === "reads") return b + " 读取 ← " + a;
    if (type === "writes") return a + " 写出 → " + b;
    if (type === "generates") return a + " 生成 → " + b;
    return a + " → " + b;
  }

  // The nearest socket on `side` ("in" | "out") of any card within
  // `radius` world units of (wx, wy), or null: {id, index, dist}. `posFn`
  // maps a card id to its [x, y]. Geometric rather than DOM-targeted so
  // the hit area scales with 1/zoom while the drawn circle does not, and
  // so it works under pointer capture (where every event targets the svg).
  function socketAt(nodes, posFn, side, wx, wy, radius) {
    let best = null;
    nodes.forEach((n) => {
      const s = SOCKETS[n.type] || SOCKETS.dataset;
      const list = side === "in" ? s.inputs : s.outputs;
      const [x, y] = posFn(n.id);
      const sx = side === "in" ? x : x + NODE_W;
      list.forEach((_, i) => {
        const d = Math.hypot(wx - sx, wy - (y + socketY(i)));
        if (d <= radius && (!best || d < best.dist)) best = { id: n.id, index: i, dist: d };
      });
    });
    return best;
  }

  // -- Layout (8.4) -------------------------------------------------------------
  // Longest-path layering over the flow direction (dataset -> script for
  // 读取, script -> dataset/figure for 写出/生成): a card nothing flows into
  // is layer 0, anything else is 1 + the max layer of what flows into it --
  // exactly 8.4's three rules in one. A cycle is broken at the edge that
  // closes it and that edge is reported back so it can be drawn dashed in
  // clay. "The edge that closes it" is decided in two passes (adversarial
  // review of the V4 work): machine links first, by a deterministic DFS in
  // step order (the only order a machine extraction has); then the human
  // mappings in the order they were asserted (`entry`, their position in
  // .rce/mappings.toml -- new entries are appended), each one that would
  // close a loop over what is already accepted being the closing edge.
  // So when the researcher draws the link that makes a loop, THAT link is
  // the one marked -- not an older machine edge the DFS happened to visit
  // last.

  function flowGraph(nodes, links) {
    const ids = new Set(nodes.map((n) => n.id));
    const outs = new Map(), ins = new Map();
    nodes.forEach((n) => { outs.set(n.id, []); ins.set(n.id, []); });
    links.forEach((l) => {
      if (!ids.has(l.from) || !ids.has(l.to)) return;
      outs.get(l.from).push(l);
      ins.get(l.to).push(l);
    });
    return { outs, ins };
  }

  // Assertion order of a human link: its entry index in the mappings file;
  // an optimistic link (no entry yet) is the newest of all.
  function assertionRank(l) {
    return Number.isFinite(l.entry) ? l.entry : Infinity;
  }

  function findCycleLinks(order, graph, byId) {
    const cycle = new Set();
    const color = new Map(); // 1 = on the DFS stack, 2 = done
    function visit(id) {
      color.set(id, 1);
      const next = graph.outs.get(id).filter((l) => !l.human)
        .sort((a, b) => compareByStep(byId.get(a.to), byId.get(b.to)));
      for (const l of next) {
        const c = color.get(l.to) || 0;
        if (c === 1) cycle.add(l.id);
        else if (c === 0) visit(l.to);
      }
      color.set(id, 2);
    }
    order.forEach((n) => { if (!color.get(n.id)) visit(n.id); });

    // Accepted (acyclic so far) adjacency: machine links minus their own
    // closing edges, then each human link in assertion order.
    const accepted = new Map();
    order.forEach((n) => accepted.set(n.id, []));
    const human = [];
    graph.outs.forEach((links) => links.forEach((l) => {
      if (l.human) human.push(l);
      else if (!cycle.has(l.id)) accepted.get(l.from).push(l.to);
    }));
    function reaches(from, to) {
      const seen = new Set([from]);
      const stack = [from];
      while (stack.length) {
        const id = stack.pop();
        if (id === to) return true;
        for (const next of accepted.get(id)) {
          if (!seen.has(next)) { seen.add(next); stack.push(next); }
        }
      }
      return false;
    }
    human.sort((a, b) => assertionRank(a) - assertionRank(b) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    human.forEach((l) => {
      if (reaches(l.to, l.from)) cycle.add(l.id);
      else accepted.get(l.from).push(l.to);
    });
    return cycle;
  }

  function assignLayers(order, graph, cycle) {
    const layer = new Map();
    function layerOf(id) {
      if (layer.has(id)) return layer.get(id);
      layer.set(id, 0); // provisional; the DAG (cycle edges dropped) never revisits
      let best = 0;
      for (const l of graph.ins.get(id)) {
        if (cycle.has(l.id)) continue;
        best = Math.max(best, layerOf(l.from) + 1);
      }
      layer.set(id, best);
      return best;
    }
    order.forEach((n) => layerOf(n.id));
    return layer;
  }

  // Which frame (attempt) a card sits in, for spacing only: two stacked
  // cards in different frames need room for both frames' padding and the
  // lower frame's title, or one frame's title hides under the other's
  // cards. First frame wins for a file shared by two attempts.
  function frameOfNode(frames) {
    const of = new Map();
    (frames || []).forEach((f, i) => f.node_ids.forEach((id) => { if (!of.has(id)) of.set(id, i); }));
    return of;
  }

  // 8.4 step 1's placement-only adjacency: a card with no link in this
  // view that belongs to an attempt whose step SCRIPT shares its numeric
  // step prefix (`17-….pdf` with `17-….Rmd`) is placed as if that script
  // wrote it -- never an asserted edge, never drawn, never in the payload.
  // 8.1 states it for ghosts; it is applied to every unlinked card, since
  // 8.4 step 4 defines loose cards as "no links and no step-prefix
  // script" (a ghost has no links by construction, so ghosts are the
  // common case). Each prefix of each attempt has ONE anchor -- its first
  // linked script in step order, else its first script -- and an anchor
  // never anchors to anything itself, so these edges can never form a
  // loop. Returns [{id, from, to, virtual: true}].
  function stepAnchorLinks(nodes, linkedIds, frames, byId) {
    const out = [];
    const anchored = new Set();
    (frames || []).forEach((f) => {
      const members = f.node_ids.filter((id) => byId.has(id)).map((id) => byId.get(id));
      const byPrefix = new Map();
      members.forEach((n) => {
        const k = stepKey(n.path);
        if (!Number.isFinite(k)) return;
        if (!byPrefix.has(k)) byPrefix.set(k, []);
        byPrefix.get(k).push(n);
      });
      byPrefix.forEach((group) => {
        const scripts = group.filter((n) => n.type === "script").sort(compareByStep);
        if (!scripts.length) return;
        const anchor = scripts.find((n) => linkedIds.has(n.id)) || scripts[0];
        group.slice().sort(compareByStep).forEach((n) => {
          if (n.id === anchor.id || linkedIds.has(n.id) || anchored.has(n.id)) return;
          anchored.add(n.id);
          out.push({ id: "step:" + anchor.id + "->" + n.id, from: anchor.id, to: n.id, virtual: true });
        });
      });
    });
    return out;
  }

  // Connected components of `ids` over `links` taken as undirected
  // (8.4 step 1), each in step order, in order of their first card.
  function connectedIslands(order, links) {
    const ids = new Set(order.map((n) => n.id));
    const adj = new Map(order.map((n) => [n.id, []]));
    links.forEach((l) => {
      if (!ids.has(l.from) || !ids.has(l.to)) return;
      adj.get(l.from).push(l.to);
      adj.get(l.to).push(l.from);
    });
    const seen = new Set();
    const islands = [];
    order.forEach((n) => {
      if (seen.has(n.id)) return;
      const members = new Set([n.id]);
      const stack = [n.id];
      seen.add(n.id);
      while (stack.length) {
        const id = stack.pop();
        adj.get(id).forEach((next) => {
          if (!seen.has(next)) { seen.add(next); members.add(next); stack.push(next); }
        });
      }
      islands.push(order.filter((m) => members.has(m.id)));
    });
    return islands;
  }

  // Lay out ONE island at its own origin (8.4 steps 2-3): longest-path
  // layers left to right, one barycenter pass per layer, step-prefix
  // tie-break, a layer taller than SUBCOL_MAX cards wrapped into
  // side-by-side sub-columns inside a band that widens to hold them. The
  // result is normalized so its top-left card corner is (0, 0).
  //
  // `fixedY` maps a card id to the world center y of a SAVED neighbor
  // (8.4: saved cards take part only as fixed neighbors for the
  // barycenter). A saved card's y is in world space and the island is not
  // yet placed, so it orders cards only among those with no placed
  // neighbor inside the island, and never pulls a card's y.
  function layoutIsland(island, graph, cycle, frameOf, fixedY) {
    const layer = assignLayers(island, graph, cycle);
    const columns = new Map();
    island.forEach((n) => {
      const k = layer.get(n.id);
      if (!columns.has(k)) columns.set(k, []);
      columns.get(k).push(n);
    });
    const positions = {};
    const centerY = new Map();
    let bandX = 0;
    [...columns.keys()].sort((a, b) => a - b).forEach((k) => {
      const bary = new Map(), fixed = new Map();
      columns.get(k).forEach((n) => {
        const ys = [];
        graph.ins.get(n.id).forEach((l) => { if (!cycle.has(l.id) && centerY.has(l.from)) ys.push(centerY.get(l.from)); });
        graph.outs.get(n.id).forEach((l) => { if (!cycle.has(l.id) && centerY.has(l.to)) ys.push(centerY.get(l.to)); });
        bary.set(n.id, ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : Infinity);
        const fs = fixedY.get(n.id) || [];
        fixed.set(n.id, fs.length ? fs.reduce((a, b) => a + b, 0) / fs.length : Infinity);
      });
      const col = columns.get(k).slice().sort((a, b) => {
        const ba = bary.get(a.id), bb = bary.get(b.id);
        if (ba !== bb) return ba < bb ? -1 : 1;
        const fa = fixed.get(a.id), fb = fixed.get(b.id);
        if (fa !== fb) return fa < fb ? -1 : 1;
        return compareByStep(a, b);
      });
      // Balanced sub-columns of at most SUBCOL_MAX (13 -> 7 + 6), filled
      // column by column so barycenter order reads top-down, then across.
      const nsub = Math.ceil(col.length / SUBCOL_MAX);
      const per = Math.ceil(col.length / nsub);
      for (let s = 0; s < nsub; s++) {
        const chunk = col.slice(s * per, (s + 1) * per);
        const x = bandX + s * SUBCOL_PITCH;
        // Packed top-down with ROW_GAP between cards; in a single column a
        // card with a barycenter is pulled toward it (never closer than
        // ROW_GAP to the card above), so a straight chain reads straight.
        // A wrapped layer is packed tight: the pull would only re-tower it.
        let bottom = -Infinity;
        let prevFrame;
        chunk.forEach((n) => {
          const h = nodeHeight(n.type);
          const b = nsub === 1 ? bary.get(n.id) : Infinity;
          const frame = frameOf.has(n.id) ? frameOf.get(n.id) : -1;
          const gap = bottom === -Infinity || frame === prevFrame
            ? ROW_GAP : ROW_GAP + 2 * FRAME_PAD + FRAME_TITLE_H;
          prevFrame = frame;
          let y = bottom === -Infinity ? 0 : bottom + gap;
          if (Number.isFinite(b)) y = Math.max(bottom === -Infinity ? -Infinity : bottom + gap, b - h / 2);
          positions[n.id] = [x, Math.round(y)];
          centerY.set(n.id, y + h / 2);
          bottom = y + h;
        });
      }
      bandX += (nsub - 1) * SUBCOL_PITCH + COL_GAP;
    });
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    island.forEach((n) => {
      const [x, y] = positions[n.id];
      x0 = Math.min(x0, x); y0 = Math.min(y0, y);
      x1 = Math.max(x1, x + NODE_W); y1 = Math.max(y1, y + nodeHeight(n.type));
    });
    island.forEach((n) => { positions[n.id] = [positions[n.id][0] - x0, positions[n.id][1] - y0]; });
    return { positions, w: x1 - x0, h: y1 - y0 };
  }

  // 8.4 step 4's shape: "at least 4 columns, more when needed to bring
  // the block toward 1.6:1" -- a page, not a strip. Of the column counts
  // from 4 up to one row, the one whose block (caption included, every row
  // `cardH` tall) is closest to PAGE_ASPECT; the narrowest on a tie. Pure.
  function looseColumns(count, cardH) {
    if (count <= LOOSE_MIN_COLS) return LOOSE_MIN_COLS;
    let best = LOOSE_MIN_COLS, bestMiss = Infinity;
    for (let c = LOOSE_MIN_COLS; c <= count; c++) {
      const rows = Math.ceil(count / c);
      const w = c * (NODE_W + LOOSE_GAP) - LOOSE_GAP;
      const h = LOOSE_CAPTION_H + rows * (cardH + ROW_GAP) - ROW_GAP;
      const miss = Math.abs(Math.log(w / h / PAGE_ASPECT));
      if (miss < bestMiss - 1e-9) { best = c; bestMiss = miss; }
      if (w / h > PAGE_ASPECT) break; // only wider from here
    }
    return best;
  }

  // 8.4 step 4: loose cards in one grid block shaped by looseColumns,
  // ordered by type then path, under the 「未连线」 caption (whose height
  // the block includes, so packing leaves it room).
  const TYPE_ORDER = { dataset: 0, script: 1, figure: 2 };

  function layoutLoose(cards) {
    const sorted = cards.slice().sort((a, b) => {
      const ta = a.type in TYPE_ORDER ? TYPE_ORDER[a.type] : 9;
      const tb = b.type in TYPE_ORDER ? TYPE_ORDER[b.type] : 9;
      if (ta !== tb) return ta - tb;
      return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
    });
    const cols = looseColumns(sorted.length, Math.max(...sorted.map((n) => nodeHeight(n.type))));
    const positions = {};
    let y = LOOSE_CAPTION_H, w = 0;
    for (let r = 0; r * cols < sorted.length; r++) {
      const row = sorted.slice(r * cols, (r + 1) * cols);
      let rowH = 0;
      row.forEach((n, c) => {
        positions[n.id] = [c * (NODE_W + LOOSE_GAP), y];
        rowH = Math.max(rowH, nodeHeight(n.type));
        w = Math.max(w, c * (NODE_W + LOOSE_GAP) + NODE_W);
      });
      y += rowH + ROW_GAP;
    }
    return { positions, w, h: y - ROW_GAP };
  }

  // 8.4 step 5's order: the island holding the current attempt's scripts
  // first, then card count descending, ties by the smallest step prefix
  // (then the first id, so the order is total).
  function islandOrder(islands, currentScripts) {
    const meta = islands.map((cards, i) => ({
      cards, i,
      current: cards.some((n) => currentScripts.has(n.id)),
      step: Math.min(...cards.map((n) => stepKey(n.path))),
      first: cards.map((n) => n.id).sort()[0],
    }));
    meta.sort((a, b) => {
      if (a.current !== b.current) return a.current ? -1 : 1;
      if (a.cards.length !== b.cards.length) return b.cards.length - a.cards.length;
      if (a.step !== b.step) return a.step < b.step ? -1 : 1;
      return a.first < b.first ? -1 : a.first > b.first ? 1 : 0;
    });
    return meta.map((m) => m.cards);
  }

  // Shelf-pack blocks ({w, h}) in order: left to right, a new row (96px
  // below the tallest of the last) when the next block would push the row
  // past `W`. Returns each block's top-left and the page's size.
  //
  // `obstacles` are the saved cards' world rects [x0, y0, x1, y1]. 8.4
  // keeps them out of steps 1-5 but says nothing of where the packed page
  // lands relative to them; packing from the origin blind to them put
  // unsaved cards UNDER a card the researcher had nudged (verifier
  // finding on bb76a9f). A block that would come within ISLAND_GAP of one
  // -- the gap islands keep from each other -- moves right past it, and
  // wraps to a new row if that crosses W (an empty row never wraps, so
  // the loop always ends: x only grows within a row, y only across rows).
  function packRows(items, W, obstacles) {
    const at = [];
    let x = 0, y = 0, rowH = 0, right = 0;
    items.forEach((b) => {
      if (x > 0 && x + b.w > W) { y += rowH + ISLAND_GAP; x = 0; rowH = 0; }
      for (;;) {
        const hit = (obstacles || []).find((o) => x < o[2] + ISLAND_GAP && o[0] - ISLAND_GAP < x + b.w
          && y < o[3] + ISLAND_GAP && o[1] - ISLAND_GAP < y + b.h);
        if (!hit) break;
        x = hit[2] + ISLAND_GAP;
        if (rowH > 0 && x + b.w > W) { y += rowH + ISLAND_GAP; x = 0; rowH = 0; }
      }
      at.push([x, y]);
      right = Math.max(right, x + b.w);
      x += b.w + ISLAND_GAP;
      rowH = Math.max(rowH, b.h);
    });
    return { at, w: right, h: y + rowH };
  }

  // The 8.4 layout of the cards of ONE view. Pure: plain objects in, plain
  // objects out, so the node test runner pins it without a DOM.
  //
  //   nodes  -- the visible cards ({id, type, path, ghost?})
  //   links  -- the visible links ({id, from, to, human?, entry?})
  //   frames -- the view's DRAWN attempt frames ({attempt_id?, node_ids}),
  //             for frame-title spacing
  //   opts   -- {fixed: {id: [x, y]} the view's pinned positions (never
  //             moved), current: the current attempt's id, groups: every
  //             attempt's step files in this view ({attempt_id, node_ids};
  //             default `frames`) -- 8.4 step 1's step-prefix companion
  //             and step 5's current-attempt-first order need them in 全部
  //             too, where no frame is drawn (8.7)}
  //
  // Returns {positions} for the UNPINNED cards only, {cycle} (link ids that
  // close a loop -- over every visible link, pinned ends or not), {loose}
  // (the world rect, caption included, of the 「未连线」 cards -- pinned or
  // not, so the caption stays over them once the view is pinned -- or
  // null), {looseIds} (every loose card, pinned or not: a frame is drawn
  // around its members outside them) and {islands} (card ids per island,
  // in packing order).
  function computeLayout(nodes, links, frames, opts) {
    opts = opts || {};
    frames = frames || [];
    const groups = opts.groups || frames;
    const fixed = opts.fixed || {};
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const order = nodes.slice().sort(compareByStep);
    const cycle = findCycleLinks(order, flowGraph(nodes, links), byId);

    const isFixed = (id) => Object.prototype.hasOwnProperty.call(fixed, id) && byId.has(id);
    const visibleLinks = links.filter((l) => byId.has(l.from) && byId.has(l.to));
    const linkedIds = new Set();
    visibleLinks.forEach((l) => { linkedIds.add(l.from); linkedIds.add(l.to); });
    const anchors = stepAnchorLinks(nodes, linkedIds, groups, byId);
    anchors.forEach((l) => { linkedIds.add(l.from); linkedIds.add(l.to); });

    // Saved cards take no part in steps 1-5: islands, layers and packing
    // are over the unsaved cards and the links among them; a link to a
    // saved card only feeds that card's world y into the barycenter.
    const free = order.filter((n) => !isFixed(n.id));
    const freeIds = new Set(free.map((n) => n.id));
    const freeLinks = visibleLinks.concat(anchors).filter((l) => freeIds.has(l.from) && freeIds.has(l.to));
    const graph = flowGraph(free, freeLinks);
    const fixedY = new Map();
    visibleLinks.concat(anchors).forEach((l) => {
      if (cycle.has(l.id)) return;
      [[l.from, l.to], [l.to, l.from]].forEach(([a, b]) => {
        if (!freeIds.has(a) || !isFixed(b)) return;
        if (!fixedY.has(a)) fixedY.set(a, []);
        fixedY.get(a).push(fixed[b][1] + nodeHeight(byId.get(b).type) / 2);
      });
    });

    const loose = free.filter((n) => !linkedIds.has(n.id));
    const linkedFree = free.filter((n) => linkedIds.has(n.id));
    const currentFrame = groups.find((f) => opts.current && f.attempt_id === opts.current);
    const currentScripts = new Set(currentFrame
      ? currentFrame.node_ids.filter((id) => byId.has(id) && byId.get(id).type === "script") : []);
    const frameOf = frameOfNode(frames);
    const islands = islandOrder(connectedIslands(linkedFree, freeLinks), currentScripts)
      .map((cards) => Object.assign({ cards }, layoutIsland(cards, graph, cycle, frameOf, fixedY)));

    // 8.4 step 5: rows left to right, wrapping past the target width W =
    // max(widest island, √(1.6 × total island area)). That W assumes a
    // perfect packing; greedy rows of pipeline-wide islands plus the
    // 「未连线」 block waste enough of each row that on realistic graphs the
    // page came out taller than wide (0.8 : 1), the tower 8.4 exists to
    // prevent, and 全部 no longer fit at 25%. So W is the floor: the target
    // is widened in 10% steps and the packing whose page is closest to the
    // stated 1.6 : 1 is kept (the narrowest on a tie).
    const area = islands.reduce((s, b) => s + b.w * b.h, 0);
    const widest = islands.reduce((m, b) => Math.max(m, b.w), 0);
    const W0 = Math.max(widest, Math.sqrt(1.6 * area));
    const items = islands.slice();
    let looseBlock = null;
    if (loose.length) {
      looseBlock = Object.assign({ cards: loose, loose: true }, layoutLoose(loose));
      items.push(looseBlock);
    }
    const obstacles = order.filter((n) => isFixed(n.id)).map((n) => {
      const [x, y] = fixed[n.id];
      return [x, y, x + NODE_W, y + nodeHeight(n.type)];
    });
    let best = null;
    for (let k = 0; k <= PACK_STEPS; k++) {
      const p = packRows(items, W0 * (1 + k / 10), obstacles);
      const miss = p.h ? Math.abs(Math.log(p.w / p.h / PAGE_ASPECT)) : 0;
      if (!best || miss < best.miss - 1e-9) best = Object.assign(p, { miss });
    }
    const positions = {};
    items.forEach((b, i) => {
      const [x, y] = best.at[i];
      b.x = x; b.y = y;
      b.cards.forEach((n) => { positions[n.id] = [x + b.positions[n.id][0], y + b.positions[n.id][1]]; });
    });
    // Loose over EVERY card: a pinned loose card is still loose (its
    // frame must not stretch to it; the caption still names it).
    const looseAll = order.filter((n) => !linkedIds.has(n.id));
    let looseRect = null;
    if (looseAll.length) {
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      looseAll.forEach((n) => {
        const [x, y] = isFixed(n.id) ? fixed[n.id] : positions[n.id];
        x0 = Math.min(x0, x); y0 = Math.min(y0, y);
        x1 = Math.max(x1, x + NODE_W); y1 = Math.max(y1, y + nodeHeight(n.type));
      });
      looseRect = { x: x0, y: y0 - LOOSE_CAPTION_H, w: x1 - x0, h: y1 - y0 + LOOSE_CAPTION_H };
    }
    return {
      positions, cycle,
      loose: looseRect,
      looseIds: looseAll.map((n) => n.id),
      islands: islands.map((b) => b.cards.map((n) => n.id)),
    };
  }

  // -- DOM scaffold ---------------------------------------------------------------

  function buildToolbar() {
    const bar = htmlEl("div", "cv-toolbar");
    const scope = htmlEl("select", "cv-scope");
    scope.title = "范围";
    scope.setAttribute("aria-label", "范围");
    scope.addEventListener("change", () => changeScope(scope.value));
    const search = htmlEl("input", "cv-search");
    search.type = "search";
    search.placeholder = "查找节点…";
    search.setAttribute("aria-label", "查找节点");
    search.addEventListener("input", () => { cv.query = search.value.trim(); applySearch(); });
    search.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); fitToMatches(); }
      else if (e.key === "Escape") { search.value = ""; cv.query = ""; applySearch(); search.blur(); }
    });
    const button = (text, title, fn) => {
      const b = htmlEl("button", "cv-btn", text);
      b.type = "button";
      if (title) b.title = title;
      b.addEventListener("click", fn);
      return b;
    };
    const more = htmlEl("div", "cv-more");
    const menu = htmlEl("div", "cv-menu hidden");
    const relayoutBtn = htmlEl("button", "cv-menu-item", "重新排列");
    relayoutBtn.type = "button";
    relayoutBtn.addEventListener("click", () => { menu.classList.add("hidden"); relayout(); });
    menu.appendChild(relayoutBtn);
    const moreBtn = button("⋯", "更多", (e) => { e.stopPropagation(); menu.classList.toggle("hidden"); });
    moreBtn.setAttribute("aria-label", "更多");
    more.append(moreBtn, menu);
    bar.append(
      scope, search,
      button("适应全部", "适应全部（F）", () => fitAll()),
      button("100%", "实际大小", () => zoomAboutCenter(1)),
      button("＋", "放大", () => zoomAboutCenter(cv.camera.zoom * 1.2)),
      button("－", "缩小", () => zoomAboutCenter(cv.camera.zoom / 1.2)),
      more,
    );
    return { bar, scope, search, menu };
  }

  function buildSvg() {
    const svg = svgEl("svg", { "aria-label": "画布" }, "cv-svg");
    const defs = svgEl("defs");
    const pattern = svgEl("pattern", { id: "cv-dots", width: GRID, height: GRID, patternUnits: "userSpaceOnUse" });
    pattern.appendChild(svgEl("circle", { cx: GRID / 2, cy: GRID / 2, r: 1 }, "cv-dot"));
    // The title bar's left rule must follow the card's rounded corner; one
    // shared clip in each card's own coordinates does it for all of them.
    const clip = svgEl("clipPath", { id: "cv-title-clip" });
    clip.appendChild(svgEl("rect", { x: 0, y: 0, width: NODE_W, height: TITLE_H + 8, rx: 6 }));
    defs.append(pattern, clip);
    const bg = svgEl("rect", { x: 0, y: 0, width: "100%", height: "100%", fill: "url(#cv-dots)" }, "cv-bg");
    const world = svgEl("g", null, "cv-world");
    const frames = svgEl("g", null, "cv-frames");
    const links = svgEl("g", null, "cv-links");
    const nodes = svgEl("g", null, "cv-nodes");
    // The link being drawn sits ABOVE the cards (it must stay visible
    // while it crosses them to reach a socket); drawn links stay below.
    const draft = svgEl("g", null, "cv-draft");
    world.append(frames, links, nodes, draft);
    svg.append(defs, bg, world);
    return { svg, pattern, world, frames, links, nodes, draft };
  }

  function ensureDom(container) {
    cv.container = container;
    if (cv.dom && container.contains(cv.dom.root)) return cv.dom;
    container.innerHTML = "";
    const root = htmlEl("div", "cv-root");
    const s = buildSvg();
    const t = buildToolbar();
    const zoom = htmlEl("div", "cv-zoom", "100%");
    const status = htmlEl("div", "cv-status hidden");
    const tip = htmlEl("div", "cv-tip hidden");
    const empty = htmlEl("div", "cv-empty hidden");
    const linkCard = htmlEl("div", "cv-linkcard hidden");
    const ctxMenu = htmlEl("div", "cv-menu cv-ctx hidden");
    const notice = htmlEl("div", "cv-notice hidden");
    root.append(s.svg, t.bar, zoom, status, tip, empty, linkCard, ctxMenu, notice);
    container.appendChild(root);
    cv.dom = Object.assign({ root, zoom, status, tip, empty, linkCard, ctxMenu, notice }, s, t);
    // A click on the chip dismisses it -- unless it offers an action: a
    // near miss on 「撤销」 must not silently throw the undo away.
    status.addEventListener("click", (e) => {
      if (e.target.closest && e.target.closest(".err-detail")) return; // selecting the engine text
      if (!status.querySelector(".cv-status-action")) hideStatus();
    });
    bindSvgEvents(s.svg);
    bindGlobalEvents();
    if (typeof ResizeObserver === "function") {
      if (cv.resizeObs) cv.resizeObs.disconnect();
      cv.resizeObs = new ResizeObserver(onResize);
      cv.resizeObs.observe(root);
    }
    applyCamera();
    return cv.dom;
  }

  // -- Rendering ----------------------------------------------------------------

  function nodeClass(n) {
    const cls = ["cv-node", "t-" + n.type];
    if (n.ghost) cls.push("ghost");
    if (n.missing) cls.push("missing");
    if (n.id === cv.selected) cls.push("selected");
    if (n.id === cv.hovered) cls.push("hover");
    return cls.join(" ");
  }

  function renderSockets(g, n) {
    const s = SOCKETS[n.type] || SOCKETS.dataset;
    const mono = "11px " + fontStack("--font-mono");
    s.inputs.forEach((sock, i) => {
      const y = socketY(i);
      const orphan = i === 0 && n.orphan_input;
      g.appendChild(svgEl("circle", { cx: 0, cy: y, r: SOCKET_R },
        "cv-socket in sock-" + sock.carries + (orphan ? " orphan" : "")));
      const label = svgEl("text", { x: 12, y: y + 4 }, "cv-socket-label in");
      label.textContent = fitRight(sock.label, mono, NODE_W / 2 - 16);
      g.appendChild(label);
    });
    s.outputs.forEach((sock, i) => {
      const y = socketY(i);
      g.appendChild(svgEl("circle", { cx: NODE_W, cy: y, r: SOCKET_R }, "cv-socket out sock-" + sock.carries));
      const label = svgEl("text", { x: NODE_W - 12, y: y + 4, "text-anchor": "end" }, "cv-socket-label out");
      label.textContent = sock.label;
      g.appendChild(label);
    });
  }

  function renderNode(n) {
    const g = svgEl("g", { "data-id": n.id }, nodeClass(n));
    const h = nodeHeight(n.type);
    const tip = svgEl("title");
    tip.textContent = n.path + (n.ghost ? "（尚未入图）" : "") + (n.missing ? "（文件不存在）" : "");
    g.appendChild(tip);
    g.appendChild(svgEl("rect", { x: 0, y: 0, width: NODE_W, height: h, rx: 6 }, "cv-card"));
    const head = svgEl("g", { "clip-path": "url(#cv-title-clip)" });
    head.appendChild(svgEl("rect", { x: 0, y: 0, width: 3, height: TITLE_H }, "cv-rule"));
    g.appendChild(head);
    g.appendChild(svgEl("line", { x1: 0, y1: TITLE_H, x2: NODE_W, y2: TITLE_H }, "cv-title-sep"));
    const type = svgEl("text", { x: 12, y: 17 }, "cv-type");
    type.textContent = TYPE_LABEL[n.type] || n.type;
    g.appendChild(type);
    const tagText = n.missing ? "文件不存在" : (n.ghost ? "尚未入图" : "");
    if (tagText) {
      const tag = svgEl("text", { x: NODE_W - 10, y: 17, "text-anchor": "end" }, n.missing ? "cv-tag cv-tag-missing" : "cv-tag cv-tag-ghost");
      tag.textContent = tagText;
      g.appendChild(tag);
    }
    const name = svgEl("text", { x: 12, y: TITLE_H + 20 }, "cv-name");
    name.textContent = fitRight(n.label, "13px " + fontStack("--font-sans"), NODE_W - 24);
    g.appendChild(name);
    const dir = svgEl("text", { x: 12, y: TITLE_H + 37 }, "cv-dir");
    dir.textContent = fitLeft(n.dir ? n.dir + "/" : "./", "11px " + fontStack("--font-mono"), NODE_W - 24);
    g.appendChild(dir);
    renderSockets(g, n);
    placeNode(g, n.id);
    return g;
  }

  function placeNode(g, id) {
    const [x, y] = posOf(id);
    g.setAttribute("transform", `translate(${x},${y})`);
  }

  // A cycle-closing link KEEPS its human/machine class and only adds
  // "cycle" (dashed): a human mapping on a loop must still be a human link
  // at a glance -- thick clay, midpoint dot -- never a copy of the machine
  // style (8.2's one rule that may not bend; adversarial review of V4).
  function linkClass(link) {
    const cls = ["cv-link", link.human ? "human" : "machine"];
    if (cv.cycle.has(link.id)) cls.push("cycle");
    if (!link.human && link.status === "pending") cls.push("pending");
    if (link.optimistic) cls.push("optimistic");
    return cls.join(" ");
  }

  function renderLink(link) {
    const ends = linkEnds(link);
    if (!ends) return null;
    const g = svgEl("g", { "data-link": link.id }, "cv-link-g" + (link.id === cv.selectedLink ? " selected" : ""));
    const d = linkPathD(ends);
    g.appendChild(svgEl("path", { d }, linkClass(link)));
    g.appendChild(svgEl("path", { d }, "cv-link-hit"));
    if (link.human) {
      const m = linkMidpoint(ends);
      g.appendChild(svgEl("circle", { cx: m.x, cy: m.y, r: 3 }, "cv-link-dot"));
    } else if (link.review || link.conflict) {
      // 9.6: the old judgment waits -- a small ochre ring, never a human dot.
      const m = linkMidpoint(ends);
      g.appendChild(svgEl("circle", { cx: m.x, cy: m.y, r: 4.5 }, "cv-review-mark"));
    }
    return g;
  }

  function updateLinkGeometry(link, g) {
    const ends = linkEnds(link);
    if (!ends) return;
    const d = linkPathD(ends);
    g.querySelectorAll("path").forEach((p) => p.setAttribute("d", d));
    g.querySelectorAll(".cv-link-dot, .cv-review-mark").forEach((dot) => {
      const m = linkMidpoint(ends);
      dot.setAttribute("cx", m.x);
      dot.setAttribute("cy", m.y);
    });
  }

  function frameTone(verdict) {
    const marker = typeof verdictMarker === "function" ? verdictMarker(verdict) : null;
    const badge = marker && typeof VERDICT_BADGE_CLASS !== "undefined" ? VERDICT_BADGE_CLASS[marker] : "badge-plain";
    return TONE_BY_BADGE[badge] || "plain";
  }

  // Which of a frame's visible members its rectangle is drawn around. A
  // step file with no link and no step-prefix script (a helper `clean.R`
  // in step_files) is a loose card (8.4 step 4) and sits in the 「未连线」
  // block at the end of the page; a frame stretched out to it enclosed
  // unrelated cards and the caption, claiming them for the attempt
  // (verifier finding on bb76a9f). So the frame is drawn around its
  // members outside that block, and around the loose ones only when it
  // has no other. Pure, for the node tests.
  function frameMembers(memberIds, looseIds) {
    const loose = new Set(looseIds);
    const placed = memberIds.filter((id) => !loose.has(id));
    return placed.length ? placed : memberIds;
  }

  function frameRect(frame) {
    const members = frameMembers(frame.node_ids.filter((id) => cv.nodes.has(id)), cv.looseIds);
    if (!members.length) return null;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    members.forEach((id) => {
      const [x, y] = posOf(id);
      x0 = Math.min(x0, x); y0 = Math.min(y0, y);
      x1 = Math.max(x1, x + NODE_W); y1 = Math.max(y1, y + nodeHeight(cv.nodes.get(id).type));
    });
    return { x: x0 - FRAME_PAD, y: y0 - FRAME_PAD - FRAME_TITLE_H, w: x1 - x0 + 2 * FRAME_PAD, h: y1 - y0 + 2 * FRAME_PAD + FRAME_TITLE_H };
  }

  // Frames are scope, not objects (8.1): computed from wherever the
  // member cards sit right now, never moving them, never hit-testable.
  function renderFrames() {
    const host = cv.dom.frames;
    host.innerHTML = "";
    (cv.data.frames || []).forEach((f) => {
      const r = frameRect(f);
      if (!r) return;
      const g = svgEl("g", null, "cv-frame tone-" + frameTone(f.verdict));
      g.appendChild(svgEl("rect", { x: r.x, y: r.y, width: r.w, height: r.h, rx: 8 }, "cv-frame-rect"));
      const title = svgEl("text", { x: r.x + 12, y: r.y + 18 }, "cv-frame-title");
      const label = "#" + String(f.number).replace(/^#/, "") + " · " + (f.title || "");
      title.textContent = fitRight(label, "600 12px " + fontStack("--font-sans"), Math.max(40, r.w - 24));
      g.appendChild(title);
      host.appendChild(g);
    });
    // 8.4 step 4: the loose cards' block is captioned, quietly, and is no
    // frame -- it groups cards by what they lack, not by an attempt.
    if (cv.loose) {
      const t = svgEl("text", { x: cv.loose.x, y: cv.loose.y + 14 }, "cv-loose-title");
      t.textContent = "未连线";
      host.appendChild(t);
    }
  }

  function renderLinks() {
    const host = cv.dom.links;
    host.innerHTML = "";
    cv.linkEls = new Map();
    (cv.data.links || []).forEach((link) => {
      const g = renderLink(link);
      if (!g) return;
      cv.linkEls.set(link.id, { g, link });
      host.appendChild(g);
    });
  }

  function renderNodes() {
    const host = cv.dom.nodes;
    host.innerHTML = "";
    cv.nodeEls = new Map();
    (cv.data.nodes || []).forEach((n) => {
      const g = renderNode(n);
      cv.nodeEls.set(n.id, g);
      host.appendChild(g);
    });
  }

  function renderEmpty() {
    const empty = !(cv.data.nodes || []).length;
    cv.dom.empty.classList.toggle("hidden", !empty);
    if (empty) {
      cv.dom.empty.textContent = cv.scope === SCOPE_ALL
        ? "图谱里还没有数据集、脚本或图表。运行 rce ingest 之后，这里会画出它们。"
        : "这个尝试还没有可画的文件。可以在上方把范围切换到「全部」。";
    }
  }

  function render() {
    if (!cv.dom) return; // nothing drawn yet (and the node tests run without a DOM)
    renderFrames();
    renderLinks();
    renderNodes();
    renderEmpty();
    updateLit();
    applySearch();
    renderLinkCard();
    if (cv.drag && cv.drag.kind === "link" && cv.drag.moved) {
      if (cv.nodes.has(cv.drag.fromId)) markLinkTargets(cv.drag);
      else endLinkDrag(false); // its source left the graph mid-drag
    }
  }

  function scopeOptionLabel(s) {
    const marker = typeof verdictMarker === "function" ? verdictMarker(s.verdict) : null;
    return (marker ? marker + " " : "") + "#" + String(s.number).replace(/^#/, "") + " · " + (s.title || "");
  }

  function renderScopeSelect() {
    if (!cv.dom) return; // the node tests run applyPayload without a DOM
    const sel = cv.dom.scope;
    sel.innerHTML = "";
    const all = document.createElement("option");
    all.value = SCOPE_ALL;
    all.textContent = "全部";
    sel.appendChild(all);
    (cv.data.scopes || []).forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = scopeOptionLabel(s) + (s.current ? "（当前）" : "");
      opt.title = s.verdict || "";
      sel.appendChild(opt);
    });
    sel.value = cv.scope;
  }

  // -- Highlight: hover, selection, search ---------------------------------------
  // A link brightens to full opacity when either end is hovered or
  // selected (8.2); search highlights matches and dims the rest without
  // changing scope (8.7).

  function updateLit() {
    const ends = new Set([cv.hovered, cv.selected].filter(Boolean));
    cv.linkEls.forEach(({ g, link }) => {
      g.classList.toggle("lit", ends.has(link.from) || ends.has(link.to));
      g.classList.toggle("selected", link.id === cv.selectedLink);
    });
    cv.nodeEls.forEach((g, id) => {
      g.classList.toggle("selected", id === cv.selected);
      g.classList.toggle("hover", id === cv.hovered);
    });
  }

  function matchingIds() {
    const q = cv.query.toLowerCase();
    if (!q) return null;
    const ids = new Set();
    cv.nodes.forEach((n) => { if (n.path.toLowerCase().includes(q)) ids.add(n.id); });
    return ids;
  }

  function applySearch() {
    const matches = matchingIds();
    cv.nodeEls.forEach((g, id) => {
      g.classList.toggle("match", !!matches && matches.has(id));
      g.classList.toggle("dim", !!matches && !matches.has(id));
    });
    cv.linkEls.forEach(({ g, link }) => {
      g.classList.toggle("dim", !!matches && !(matches.has(link.from) && matches.has(link.to)));
    });
  }

  function setHovered(id) {
    if (cv.hovered === id) return;
    cv.hovered = id;
    updateLit();
  }

  function select(id) {
    cv.selected = id;
    if (id && cv.selectedLink) { selectLink(null); return; }
    updateLit();
  }

  // -- Camera -------------------------------------------------------------------

  function applyCamera() {
    if (!cv.dom) return;
    const { x, y, zoom } = cv.camera;
    const t = `translate(${x},${y}) scale(${zoom})`;
    cv.dom.world.setAttribute("transform", t);
    cv.dom.pattern.setAttribute("patternTransform", t);
    cv.dom.zoom.textContent = Math.round(zoom * 100) + "%";
    updateLinkDrag();   // a link drag survives pan and zoom
    positionLinkCard();
  }

  function viewSize() {
    if (!cv.dom) return { w: 0, h: 0, left: 0, top: 0 };
    const r = cv.dom.svg.getBoundingClientRect();
    return { w: r.width, h: r.height, left: r.left, top: r.top };
  }

  function setZoomAbout(zoom, sx, sy) {
    const z0 = cv.camera.zoom;
    const z = clamp(zoom, ZOOM_MIN, ZOOM_MAX);
    const wx = (sx - cv.camera.x) / z0, wy = (sy - cv.camera.y) / z0;
    cv.camera = { x: sx - wx * z, y: sy - wy * z, zoom: z };
    applyCamera();
    userCamera();
  }

  function zoomAboutCenter(zoom) {
    const v = viewSize();
    setZoomAbout(zoom, v.w / 2, v.h / 2);
  }

  function boundsOf(ids) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    ids.forEach((id) => {
      const n = cv.nodes.get(id);
      if (!n) return;
      const [x, y] = posOf(id);
      x0 = Math.min(x0, x); y0 = Math.min(y0, y);
      x1 = Math.max(x1, x + NODE_W); y1 = Math.max(y1, y + nodeHeight(n.type));
    });
    (cv.data && cv.data.frames || []).forEach((f) => {
      if (!f.node_ids.some((id) => ids.includes(id))) return;
      const r = frameRect(f);
      if (r) { x0 = Math.min(x0, r.x); y0 = Math.min(y0, r.y); x1 = Math.max(x1, r.x + r.w); y1 = Math.max(y1, r.y + r.h); }
    });
    return Number.isFinite(x0) ? { x0, y0, x1, y1 } : null;
  }

  // Fit never moves cards, only the camera (8.4); never zooms past 100%
  // just because a scope is small -- a fitted single card at 250% reads
  // as a bug, not as "everything".
  // The camera that fits `ids`, or null when none of them is drawn; the
  // view must have a size.
  function fitCamera(ids) {
    const v = viewSize();
    const b = boundsOf(ids);
    if (!b) return null;
    const top = 56; // the floating toolbar
    const availW = v.w - 2 * FIT_PAD, availH = v.h - top - 2 * FIT_PAD;
    const z = clamp(Math.min(availW / (b.x1 - b.x0), availH / (b.y1 - b.y0), 1), ZOOM_MIN, ZOOM_MAX);
    return {
      x: (v.w - (b.x1 - b.x0) * z) / 2 - b.x0 * z,
      y: top + FIT_PAD + (availH - (b.y1 - b.y0) * z) / 2 - b.y0 * z,
      zoom: z,
    };
  }

  // Camera only: a fit is not a viewport the researcher chose, so it is
  // not saved -- the view stays "fitted" and re-fits on resize (8.4).
  function fitTo(ids) {
    if (!cv.dom) return;
    const v = viewSize();
    if (!v.w || !v.h) { cv.needsFit = true; return; }
    cv.needsFit = false;
    cv.camera = fitCamera(ids) || { x: FIT_PAD, y: FIT_PAD + 40, zoom: 1 };
    applyCamera();
  }

  // The automatic fit: entering a view with no saved viewport, a resize
  // while that fit is untouched, 「重新排列」.
  function autoFitAll() {
    cv.autoFit = true;
    fitTo([...cv.nodes.keys()]);
  }

  // 适应全部 (button, F, menu): the researcher asks for the fit, so the
  // view's camera is "fitted" again -- its saved viewport is forgotten and
  // a resize re-fits it, exactly as on first entering the view.
  function fitAll() {
    autoFitAll();
    queueViewport(true);
  }

  function fitToMatches() {
    const m = matchingIds();
    if (m && m.size) { fitTo([...m]); userCamera(); }
  }

  // The researcher panned or zoomed: from now on the camera is theirs --
  // saved for this view, and no longer re-fitted on resize.
  function userCamera() {
    cv.autoFit = false;
    queueViewport();
  }

  // 8.4: "until they pan or zoom, it re-fits when the window is resized".
  // Debounced: a window drag fires many resizes and one fit is enough.
  function onResize() {
    clearTimeout(cv.resizeTimer);
    cv.resizeTimer = setTimeout(() => {
      cv.resizeTimer = null;
      if (cv.autoFit && cv.dom && cv.data && !cv.drag) fitTo([...cv.nodes.keys()]);
    }, RESIZE_DEBOUNCE_MS);
  }

  // -- Persistence (8.4, 8.6) ---------------------------------------------------
  // Every write names the view it was made in. A write queued in one view
  // and still unsent when the scope changes goes out first, for that view.

  function scheduleSave() {
    clearTimeout(cv.saveTimer);
    cv.saveTimer = setTimeout(flushSave, SAVE_DEBOUNCE_MS);
  }

  function saveSlot() {
    if (cv.save.scope !== null && cv.save.scope !== cv.scope) flushSave();
    cv.save.scope = cv.scope;
    cv.save.project = cv.project;
    return cv.save;
  }

  function queuePosition(id, pos) {
    if (pos === null) delete cv.positions[id];
    else cv.positions[id] = pos;
    saveSlot().positions[id] = pos;
    scheduleSave();
  }

  // The current camera as this view's viewport, or (forget) none.
  function queueViewport(forget) {
    if (!cv.data || !cv.scope) return;
    saveSlot().viewport = forget ? null : { x: cv.camera.x, y: cv.camera.y, zoom: cv.camera.zoom };
    scheduleSave();
  }

  // 8.4's pin on first move: a card drag just ended. The moved card and
  // every visible card this view has no position for yet are saved -- in
  // a view not yet pinned that is every card (the whole arrangement is
  // pinned in one POST, so nudging one card can never pull it out of its
  // pipeline on the next layout); in a pinned view it is the moved card
  // plus any card that appeared since and was placed by the layout.
  function cardMoved(id) {
    cv.pinned = true;
    cv.nodes.forEach((n, nid) => {
      if (nid === id || !hasOwn(cv.positions, nid)) queuePosition(nid, posOf(nid).slice());
    });
  }

  async function flushSave() {
    clearTimeout(cv.saveTimer);
    cv.saveTimer = null;
    const slot = cv.save;
    cv.save = emptySave();
    if (!slot.scope) return;
    if (layoutBlocked()) return; // nothing is written over a record RCE cannot read (9.2)
    // A project frozen until it is migrated (9.12), or opened read-only:
    // the arrangement lives in this page only -- no POST, so no chip.
    if (layoutFrozen()) return;
    const body = { project: slot.project, scope: slot.scope };
    if (slot.reset) body.reset = true;
    if (Object.keys(slot.positions).length) body.positions = slot.positions;
    if (slot.viewport !== undefined) body.viewport = slot.viewport;
    if (Object.keys(body).length === 2) return;
    // Until it is answered, a re-fetch may still be answered from the file
    // as it was before this write: applyPayload lays it over that.
    cv.inflight.push(slot);
    try {
      await apiPost("/api/canvas/layout", body);
      hideStatus("save");
    } catch (err) {
      // A write for a project the server no longer serves (switched from
      // another window) was refused and stays dropped: it is a picture of
      // the other project. Otherwise keep what failed for the next save of
      // the same view, under anything newer: its reset first, then its
      // positions unless a newer write replaced them. A failed write for a
      // view already left is dropped.
      if ((err && err.state === "project_changed") || slot.project !== cv.project) return;
      const now = cv.save;
      if (now.scope === null || now.scope === slot.scope) {
        now.scope = slot.scope;
        now.project = slot.project;
        if (!now.reset) {
          now.reset = slot.reset;
          Object.entries(slot.positions).forEach(([id, p]) => { if (!hasOwn(now.positions, id)) now.positions[id] = p; });
        }
        if (now.viewport === undefined) now.viewport = slot.viewport;
      }
      showStatus("位置未能保存，下次移动时会重试", err);
    } finally {
      const i = cv.inflight.indexOf(slot);
      if (i >= 0) cv.inflight.splice(i, 1);
    }
  }

  // This view's arrangement as this page knows it: what canvas.json holds
  // for it (`saved`), then this page's writes for it still in flight, then
  // those not sent yet -- a re-fetch answered from the file before a POST
  // landed must not undo the move (verifier finding).
  function arrangementOf(saved) {
    let positions = Object.assign({}, saved);
    cv.inflight.concat([cv.save]).forEach((slot) => {
      if (slot.scope !== cv.scope || slot.project !== cv.project) return;
      if (slot.reset) positions = {};
      Object.entries(slot.positions).forEach(([id, p]) => {
        if (p === null) delete positions[id];
        else positions[id] = p;
      });
    });
    return positions;
  }

  // 「重新排列」(8.4): forget THIS view's arrangement (other views keep
  // theirs), after asking -- it discards the researcher's own placement --
  // then lay the view out afresh and fit. The camera goes back to a fit
  // too: the view is as it was before anything was moved in it.
  function relayout() {
    if (!cv.data) return;
    if (!window.confirm("重新排列当前视图？\n\n将丢弃你在这个视图里摆放的位置。")) return;
    const slot = saveSlot();
    slot.reset = true;
    slot.positions = {};
    slot.viewport = null;
    scheduleSave();
    cv.positions = {};
    cv.pinned = false;
    layoutView(true);
    render();
    autoFitAll();
  }

  // -- Status chip (product language; engine text behind 「详情」, 8.8) -------

  // One chip, bottom-left beside the zoom readout, for everything the
  // canvas has to say: a failed save, a refused drop, a failed write, the
  // 「撤销」 offer after 标记为错误提取. `opts.kind` names who owns the
  // message so only its owner clears it (a successful position save must
  // not wipe an undo offer); `opts.ms` makes it fade; `opts.action` adds
  // one small text button; `opts.notice` is the neutral (non-error) look.
  // With `err`, the chip is an error that blocked what the researcher just
  // tried (8.8 "Errors"): app.html's renderBlockingError shows the coded
  // sentence when the engine named the cause (`mapping_exists`,
  // `human_link`, ...) or else `text`, plus the 「详情」 toggle -- never a
  // hover-only reason. A click on an action-less chip dismisses it (see
  // ensureDom), except on its 详情 button or the revealed text.
  function showStatus(text, err, opts) {
    if (!cv.dom) return;
    opts = opts || {};
    const el = cv.dom.status;
    el.innerHTML = "";
    el.title = "";
    if (err) renderBlockingError(el, text, err);
    else el.appendChild(htmlEl("span", "cv-status-text", text));
    el.classList.toggle("notice", !!opts.notice);
    if (opts.action) {
      const b = htmlEl("button", "cv-status-action", opts.action.label);
      b.type = "button";
      b.addEventListener("click", (e) => { e.stopPropagation(); opts.action.run(); });
      el.appendChild(b);
    } else if (err) {
      // An error stays until the researcher has read it -- and can always
      // be put away with a visible control (its cause may be over by then:
      // a ledger restored, a file back).
      const close = htmlEl("button", "cv-status-action cv-status-close", "关闭");
      close.type = "button";
      close.setAttribute("aria-label", "关闭");
      close.addEventListener("click", (e) => { e.stopPropagation(); hideStatus(); });
      el.appendChild(close);
    }
    el.classList.remove("hidden");
    cv.statusKind = opts.kind || "save";
    clearTimeout(cv.statusTimer);
    cv.statusTimer = opts.ms ? setTimeout(() => hideStatus(), opts.ms) : null;
  }

  // `kind` given: hide only if the chip still says that owner's message.
  function hideStatus(kind) {
    if (!cv.dom) return;
    if (kind && cv.statusKind !== kind) return;
    clearTimeout(cv.statusTimer);
    cv.statusTimer = null;
    cv.statusKind = null;
    cv.dom.status.classList.add("hidden");
  }

  // -- Hover card for links (8.2) -------------------------------------------

  function linkTipText(link) {
    if (cv.cycle.has(link.id)) return "检测到循环 · " + link.evidence_hint;
    // 9.6: a link whose judgment waits is never shown as an ordinary one:
    // 「你曾于 2026-10-04 否决 · 依据已变化，待复核」 (app.html's wording).
    if (link.conflict) return "记录冲突，待处理 · " + link.evidence_hint;
    if (link.review && link.judgement) {
      const said = typeof reviewHoverText === "function" ? reviewHoverText(link.judgement) : "待复核 · " + link.judgement.label;
      return said + " · " + link.evidence_hint;
    }
    if (link.candidate_hint) return link.candidate_hint + " · " + link.evidence_hint;
    if (!link.human && link.status === "pending") return link.evidence_hint + " · 待确认";
    return link.evidence_hint;
  }

  function showTip(link, clientX, clientY) {
    if (link.id === cv.selectedLink) return; // its card is already pinned
    const tip = cv.dom.tip;
    tip.textContent = linkTipText(link);
    tip.classList.toggle("human", !!link.human);
    tip.classList.toggle("review", !!(link.review || link.conflict));
    tip.classList.remove("hidden");
    moveTip(clientX, clientY);
  }

  function moveTip(clientX, clientY) {
    const v = viewSize();
    cv.dom.tip.style.left = (clientX - v.left + 14) + "px";
    cv.dom.tip.style.top = (clientY - v.top + 14) + "px";
  }

  function hideTip() {
    if (cv.dom) cv.dom.tip.classList.add("hidden");
  }

  // -- Editing: drawing a link (8.3) ------------------------------------------
  // Press on an OUTPUT socket, drag, drop on an input socket. While the
  // pointer moves, compatible inputs pulse (a --clay-soft halo) and the
  // rest dim; the curve snaps to a compatible socket under the pointer.
  // Drop on a compatible socket -> the confirm popover; on an incompatible
  // one -> its one-line refusal; anywhere else -> nothing at all. The
  // drag lives in WORLD coordinates re-derived from the last pointer
  // position on every camera change, which is what lets it survive a
  // two-finger pan or a pinch mid-drag.

  function worldPoint(clientX, clientY) {
    const v = viewSize();
    return {
      x: (clientX - v.left - cv.camera.x) / cv.camera.zoom,
      y: (clientY - v.top - cv.camera.y) / cv.camera.zoom,
    };
  }

  function screenPoint(wx, wy) {
    return { x: wx * cv.camera.zoom + cv.camera.x, y: wy * cv.camera.zoom + cv.camera.y };
  }

  function hitRadius() {
    return Math.max(SOCKET_R + 3, SOCKET_HIT_PX / cv.camera.zoom);
  }

  function visibleNodes() {
    return (cv.data && cv.data.nodes) || [];
  }

  function socketPoint(id, side, index) {
    const [x, y] = posOf(id);
    return { x: side === "in" ? x : x + NODE_W, y: y + socketY(index) };
  }

  // The link being drawn: horizontal tangents like every link, but never
  // the back-loop -- it follows the pointer, wherever that is.
  function drawDraft(fromId, outIndex, end) {
    const a = socketPoint(fromId, "out", outIndex);
    const dx = Math.max(40, Math.abs(end.x - a.x) * 0.5);
    let path = cv.dom.draft.querySelector("path");
    if (!path) {
      path = svgEl("path", null, "cv-draft-path");
      cv.dom.draft.appendChild(path);
    }
    path.setAttribute("d", `M${a.x},${a.y} C${a.x + dx},${a.y} ${end.x - dx},${end.y} ${end.x},${end.y}`);
  }

  function clearDraft() {
    if (cv.dom) cv.dom.draft.innerHTML = "";
  }

  // Light every card's input for the drag in progress: halo where the
  // drop would be accepted, dim where it would be refused. Re-applied
  // after a re-render, since a generation bump may land mid-drag.
  function markLinkTargets(d) {
    clearLinkTargets();
    const from = cv.nodes.get(d.fromId);
    d.compat = new Map();
    cv.nodeEls.forEach((g, id) => {
      const ok = !!from && checkConnection(from, d.outIndex, cv.nodes.get(id), 0, cv.data.links).ok;
      d.compat.set(id, ok);
      g.classList.add(ok ? "link-ok" : "link-no");
      if (ok) {
        const halo = svgEl("circle", { cx: 0, cy: socketY(0), r: SOCKET_R + 4 }, "cv-socket-halo");
        g.insertBefore(halo, g.querySelector(".cv-socket.in"));
      }
    });
    if (d.hot) {
      const g = cv.nodeEls.get(d.hot.id);
      if (g) g.classList.add("link-hot");
    }
  }

  function clearLinkTargets() {
    cv.nodeEls.forEach((g) => {
      g.classList.remove("link-ok", "link-no", "link-hot");
      g.querySelectorAll(".cv-socket-halo").forEach((h) => h.remove());
    });
  }

  function startLinkDrag(hit, e) {
    cv.drag = {
      kind: "link", fromId: hit.id, outIndex: hit.index, pointerId: e.pointerId,
      sx: e.clientX, sy: e.clientY, cx: e.clientX, cy: e.clientY,
      moved: false, compat: new Map(), hot: null,
    };
  }

  function updateLinkDrag() {
    const d = cv.drag;
    if (!d || d.kind !== "link" || !d.moved || !cv.dom) return;
    const w = worldPoint(d.cx, d.cy);
    const t = socketAt(visibleNodes(), posOf, "in", w.x, w.y, hitRadius());
    const hot = t && d.compat.get(t.id) ? t : null;
    if ((hot && hot.id) !== (d.hot && d.hot.id)) {
      if (d.hot && cv.nodeEls.get(d.hot.id)) cv.nodeEls.get(d.hot.id).classList.remove("link-hot");
      if (hot && cv.nodeEls.get(hot.id)) cv.nodeEls.get(hot.id).classList.add("link-hot");
    }
    d.hot = hot;
    drawDraft(d.fromId, d.outIndex, hot ? socketPoint(hot.id, "in", hot.index) : w);
  }

  // `keepDraft`: the drop opened the popover, which keeps the curve on
  // screen (pinned to its target socket) while it asks.
  function endLinkDrag(keepDraft) {
    const d = cv.drag;
    cv.drag = null;
    clearLinkTargets();
    if (cv.dom) cv.dom.root.classList.remove("linking");
    if (!keepDraft) clearDraft();
    if (d && cv.dom) {
      try { cv.dom.svg.releasePointerCapture(d.pointerId); } catch (err) { /* already released */ }
    }
  }

  function dropLink(d) {
    const w = worldPoint(d.cx, d.cy);
    const target = socketAt(visibleNodes(), posOf, "in", w.x, w.y, hitRadius());
    const from = cv.nodes.get(d.fromId);
    if (!target || !from) { endLinkDrag(false); return; } // dropped on nothing: nothing happens
    const to = cv.nodes.get(target.id);
    const check = checkConnection(from, d.outIndex, to, target.index, cv.data.links);
    if (!check.ok) {
      endLinkDrag(false);
      showStatus(check.reason, null, { kind: "refusal", ms: NOTICE_MS });
      return;
    }
    drawDraft(d.fromId, d.outIndex, socketPoint(to.id, "in", target.index));
    endLinkDrag(true);
    openPopover(check, from, to, target);
  }

  // -- Editing: the confirm popover (8.3) ------------------------------------
  // The canvas's only write dialog: the assertion in one line, an optional
  // 备注, 确认标注 / 取消. Keys typed in it never reach the canvas (its
  // own keydown stops them), so F, Space and Backspace in the 备注 field
  // are just text.

  function openPopover(check, from, to, target) {
    closePopover();
    const el = htmlEl("div", "cv-pop");
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-label", "确认标注");
    const line = htmlEl("div", "cv-pop-line", assertionText(from, to, check.type));
    line.title = check.from + "\n→ " + check.to;
    const field = htmlEl("label", "cv-pop-field");
    const input = htmlEl("input", "form-input cv-pop-note");
    input.type = "text";
    input.placeholder = "备注（可选）";
    input.setAttribute("aria-label", "备注");
    input.maxLength = 200;
    field.appendChild(input);
    const actions = htmlEl("div", "cv-pop-actions");
    const ok = htmlEl("button", "btn btn-primary", "确认标注");
    ok.type = "button";
    ok.addEventListener("click", () => confirmPopover());
    const cancel = htmlEl("button", "btn", "取消");
    cancel.type = "button";
    cancel.addEventListener("click", () => closePopover());
    actions.append(ok, cancel);
    el.append(line, field, actions);
    el.addEventListener("keydown", (e) => {
      e.stopPropagation(); // nothing typed here is a canvas shortcut
      if (e.key === "Escape") { e.preventDefault(); closePopover(); }
      else if (e.key === "Enter" && e.target === input && !e.isComposing) { e.preventDefault(); confirmPopover(); }
    });
    el.addEventListener("keyup", (e) => e.stopPropagation());
    el.addEventListener("pointerdown", (e) => e.stopPropagation());
    cv.dom.root.appendChild(el);
    cv.pop = { el, input, check, fromId: from.id, toId: to.id };
    const s = screenPoint(socketPoint(to.id, "in", target.index).x, socketPoint(to.id, "in", target.index).y);
    placeFloating(el, s.x + 14, s.y + 14);
    input.focus();
  }

  function closePopover() {
    if (!cv.pop) return;
    cv.pop.el.remove();
    cv.pop = null;
    clearDraft();
  }

  // Keep a floating HTML element (popover, pinned card, menu) inside the
  // canvas, preferring below-right of the anchor point.
  function placeFloating(el, x, y) {
    const v = viewSize();
    const w = el.offsetWidth, h = el.offsetHeight;
    if (x + w > v.w - 8) x = Math.max(8, x - w - 28);
    if (y + h > v.h - 8) y = Math.max(8, y - h - 28);
    el.style.left = Math.round(x) + "px";
    el.style.top = Math.round(y) + "px";
  }

  function localDate() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function entryKey(from, to, type) {
    return JSON.stringify([from, to, type]);
  }

  // Confirm: draw the human link at once (optimistic), write the mapping,
  // then re-fetch -- applyPayload swaps the optimistic link for the real
  // one the re-ingest produced (and a ghost endpoint for a real card). A
  // failed write takes the optimistic link back and says why.
  async function confirmPopover() {
    const p = cv.pop;
    if (!p) return;
    const note = p.input.value.trim();
    closePopover();
    cv.focus = [p.toId, p.fromId]; // the camera holds on these through the re-layout
    const key = entryKey(p.check.from, p.check.to, p.check.type);
    const link = {
      id: "optimistic:" + key, from: p.fromId, to: p.toId, type: p.check.type,
      extractor: "mapping", status: "confirmed", human: true, optimistic: true,
      evidence_hint: "你于 " + localDate() + " 标注" + (note ? " · " + note : ""),
    };
    cv.optimistic.set(key, link);
    cv.data.links.push(link);
    rerenderLinks();
    const body = { from: p.check.from, to: p.check.to, type: p.check.type };
    if (note) body.note = note;
    let res;
    try {
      res = await apiPost("/api/mappings/add", body);
    } catch (err) {
      dropOptimistic(key);
      cv.focus = null;
      showStatus("无法标注：映射没有写入", err, { kind: "write" }); // mapping_exists -> 「这条映射已存在」
      return;
    }
    if (res && res.ingest_error) {
      // The FILE holds the entry (the truth, 8.5); only the graph lags. The
      // server leaves the change visible to the watcher, whose next poll
      // re-runs the mappings ingest -- so the link stays drawn (optimistic,
      // marked as waiting) until mergeOptimistic swaps in the real one,
      // instead of vanishing and inviting a redraw that 「这条映射已存在」
      // would refuse. One message for the one cause: this chip, not also
      // the header's 重扫失败 (adversarial review of the V4 work).
      link.syncing = true;
      showStatus("已写入映射文件，图谱稍后自动同步", res.ingest_error, { kind: "write" });
      rerenderLinks();
      return;
    }
    await refresh();
    dropOptimistic(key); // reconciled by now; never let it outlive its write
  }

  function dropOptimistic(key) {
    const link = cv.optimistic.get(key);
    cv.optimistic.delete(key);
    if (!link || !cv.data) return;
    const before = cv.data.links.length;
    cv.data.links = cv.data.links.filter((l) => l !== link);
    if (cv.data.links.length !== before) rerenderLinks();
  }

  // An optimistic link stays drawn across re-fetches until the real one
  // (same entry, extractor mapping) arrives in a payload.
  function mergeOptimistic(payload) {
    cv.optimistic.forEach((link, key) => {
      const real = payload.links.some((l) => l.human && l.from === link.from && l.to === link.to && l.type === link.type);
      if (real) cv.optimistic.delete(key);
      else payload.links.push(link);
    });
  }

  function rerenderLinks() {
    renderLinks();
    updateLit();
    applySearch();
    renderLinkCard();
  }

  function refresh() {
    return cv.container ? load(cv.container) : Promise.resolve();
  }

  // -- Editing: selecting a link, and what can be done to it (8.3) ----------
  // A click selects a link and pins its hover card; the pinned card holds
  // the same action as the right-click menu as a small text button (a
  // trackpad has no comfortable right-click). Human link: 删除标注 (the
  // entry leaves mappings.toml). Machine link: 标记为错误提取 (status
  // rejected through the human-only path), undoable from the chip.

  // What may be done to a link. A hand-drawn link: 删除标注 (its truth is
  // mappings.toml, 8.5). A machine link (9.3): confirm it unless a
  // confirmation already stands and applies, mark it a wrong extraction,
  // and withdraw a verdict that stands -- waiting for review or not. The
  // card's 备注 field (`note`) goes with each; the right-click menu has none.
  function linkActions(link, note) {
    if (link.optimistic) return [];
    if (link.human) return [{ label: "删除标注", run: () => deleteHumanLink(link) }];
    const j = link.judgement;
    const waiting = !!(link.review || link.conflict);
    const stands = waiting ? !!(j && (j.verdict === "confirmed" || j.verdict === "rejected") || link.conflict)
      : link.status === "confirmed" || link.status === "rejected";
    const n = () => (note ? note() : "");
    const out = [];
    if (waiting || link.status !== "confirmed") out.push({ label: "确认这条连线", run: () => judgeLink(link, "confirmed", n()) });
    out.push({ label: "标记为错误提取", run: () => rejectMachineLink(link, n()) });
    if (stands) out.push({ label: "撤回", run: () => judgeLink(link, "withdrawn", n()) });
    return out;
  }

  function linkAssertion(link) {
    const from = cv.nodes.get(link.from), to = cv.nodes.get(link.to);
    return from && to ? assertionText(from, to, link.type) : "";
  }

  function selectLink(id) {
    cv.selectedLink = id;
    if (id) cv.selected = null;
    updateLit();
    renderLinkCard();
  }

  function renderLinkCard() {
    if (!cv.dom) return;
    const card = cv.dom.linkCard;
    const entry = cv.selectedLink ? cv.linkEls.get(cv.selectedLink) : null;
    card.innerHTML = "";
    if (!entry) { card.classList.add("hidden"); return; }
    const link = entry.link;
    // A re-render (every refresh) must not take the 备注 being typed away.
    const typing = document.activeElement && document.activeElement.classList &&
      document.activeElement.classList.contains("cv-linkcard-note") && card.contains(document.activeElement);
    card.innerHTML = "";
    card.classList.toggle("human", !!link.human);
    card.classList.toggle("review", !!(link.review || link.conflict));
    card.appendChild(htmlEl("div", "cv-linkcard-assert", linkAssertion(link)));
    card.appendChild(htmlEl("div", "cv-linkcard-hint", linkTipText(link)));
    if (link.optimistic) card.appendChild(htmlEl("div", "cv-linkcard-hint", link.syncing ? "已写入映射文件，等待图谱同步…" : "正在写入…"));
    const machine = !link.human && !link.optimistic;
    const blocked = machine && typeof humanRecordsBlocked === "function" ? humanRecordsBlocked() : null;
    let noteInput = null;
    if (machine && !blocked) {
      if (cv.cardNote.id !== link.id) cv.cardNote = { id: link.id, value: "" };
      noteInput = htmlEl("input", "cv-linkcard-note");
      noteInput.type = "text";
      noteInput.placeholder = "备注（可选）";
      noteInput.value = cv.cardNote.value;
      noteInput.addEventListener("input", () => { cv.cardNote.value = noteInput.value; });
      card.appendChild(noteInput);
    }
    const actions = blocked ? [] : linkActions(link, noteInput ? () => noteInput.value.trim() : null);
    if (actions.length) {
      const row = htmlEl("div", "cv-linkcard-actions");
      actions.forEach((a) => {
        const b = htmlEl("button", "cv-text-btn", a.label);
        b.type = "button";
        b.addEventListener("click", () => a.run());
        row.appendChild(b);
      });
      if (machine && (link.review || link.conflict) && typeof openReviewPanel === "function") {
        const b = htmlEl("button", "cv-text-btn", "在待复核列表中查看");
        b.type = "button";
        b.addEventListener("click", () => openReviewPanel(link));
        row.appendChild(b);
      }
      card.appendChild(row);
    }
    if (blocked) card.appendChild(htmlEl("div", "cv-linkcard-hint", blocked));
    if (machine) card.appendChild(renderHistory(link));
    card.classList.remove("hidden");
    positionLinkCard();
    if (typing && noteInput) {
      noteInput.focus();
      noteInput.setSelectionRange(noteInput.value.length, noteInput.value.length);
    }
  }

  // The link's history from the ledger (9.3: the file is its own history),
  // newest first: who did what, when, from where, the note, and the basis
  // each verdict was made on. An entry an undo cancelled is struck through.
  function renderHistory(link) {
    const box = htmlEl("div", "cv-linkcard-history");
    const h = cv.history && cv.history.id === link.id ? cv.history : null;
    if (!h) { loadHistory(link); box.appendChild(htmlEl("div", "cv-linkcard-hint", "载入记录中…")); return box; }
    if (h.error) {
      const e = htmlEl("div", "cv-linkcard-hint");
      renderBlockingError(e, "没能读到这条连线的记录", h.error);
      box.appendChild(e);
      return box;
    }
    if (!h.readable) { box.appendChild(htmlEl("div", "cv-linkcard-hint", "判断记录文件当前无法读取，记录暂时看不到。")); return box; }
    if (!h.entries.length) {
      // A pre-V5 project: its judgments still sit in the old index (9.5).
      const waiting = typeof state === "object" && state && state.summary && state.summary.needs_migration;
      box.appendChild(htmlEl("div", "cv-linkcard-hint", waiting
        ? "旧版本里的判断还在旧索引中，迁移后才会出现在这里。" : "还没有你对这条连线的判断。"));
      return box;
    }
    box.appendChild(htmlEl("div", "cv-linkcard-history-title", "记录（新的在上）"));
    h.entries.slice().reverse().forEach((e) => {
      const row = htmlEl("div", "cv-hist" + (e.cancelled ? " cancelled" : ""));
      row.appendChild(htmlEl("div", "cv-hist-what", historyEntryText(e) + (e.cancelled ? "（已撤销）" : "")));
      if (e.note) row.appendChild(htmlEl("div", "cv-hist-sub", "备注：" + e.note));
      const basis = historyBasisText(e);
      if (basis) row.appendChild(htmlEl("div", "cv-hist-sub", basis));
      box.appendChild(row);
    });
    return box;
  }

  async function loadHistory(link) {
    if (cv.history && cv.history.id === link.id && cv.history.loading) return;
    const mine = { id: link.id, loading: true, entries: [], readable: true, error: null };
    cv.history = mine;
    const q = ["src", "dst", "type", "extractor"].map((k) => k + "=" + encodeURIComponent(link[k])).join("&");
    try {
      const data = await apiGet("/api/history?" + q);
      mine.entries = data.entries || [];
      mine.readable = data.readable !== false;
    } catch (err) {
      mine.error = err;
    }
    mine.loading = false;
    if (cv.history === mine && cv.selectedLink === link.id) renderLinkCard();
  }

  function positionLinkCard() {
    if (!cv.dom || cv.dom.linkCard.classList.contains("hidden")) return;
    const entry = cv.selectedLink ? cv.linkEls.get(cv.selectedLink) : null;
    const ends = entry && linkEnds(entry.link);
    if (!ends) return;
    const m = linkMidpoint(ends);
    const s = screenPoint(m.x, m.y);
    placeFloating(cv.dom.linkCard, s.x + 12, s.y + 12);
  }

  function openCtxMenu(link, clientX, clientY) {
    const menu = cv.dom.ctxMenu;
    menu.innerHTML = "";
    linkActions(link).forEach((a) => {
      const b = htmlEl("button", "cv-menu-item", a.label);
      b.type = "button";
      b.addEventListener("click", (e) => { e.stopPropagation(); closeCtxMenu(); a.run(); });
      menu.appendChild(b);
    });
    if (!menu.childNodes.length) return;
    menu.classList.remove("hidden");
    cv.dom.linkCard.classList.add("hidden"); // one floating thing at a time
    cv.ctx = true;
    const v = viewSize();
    placeFloating(menu, clientX - v.left + 2, clientY - v.top + 2);
  }

  function closeCtxMenu() {
    if (!cv.ctx) return;
    cv.ctx = false;
    if (cv.dom) cv.dom.ctxMenu.classList.add("hidden");
    renderLinkCard(); // the selected link's card comes back
  }

  function onContextMenu(e) {
    closeCtxMenu();
    const linkEl = e.target.closest(".cv-link-g");
    const entry = linkEl && cv.linkEls.get(linkEl.getAttribute("data-link"));
    if (!entry) return; // elsewhere: the platform's own menu
    e.preventDefault();
    hideTip();
    selectLink(entry.link.id);
    openCtxMenu(entry.link, e.clientX, e.clientY);
  }

  async function deleteHumanLink(link) {
    if (!link.human || link.optimistic) return;
    const from = cv.nodes.get(link.from), to = cv.nodes.get(link.to);
    if (!from || !to) return;
    if (!window.confirm("删除这条标注？\n\n" + assertionText(from, to, link.type) +
      "\n\n它会从 .rce/mappings.toml 中删去（删除前自动备份）。")) return;
    let res;
    try {
      res = await apiPost("/api/mappings/delete", { from: from.path, to: to.path, type: link.type });
    } catch (err) {
      showStatus("无法删除标注：映射文件没有改动", err, { kind: "write" });
      return;
    }
    selectLink(null);
    if (res && res.ingest_error) {
      showStatus("已从映射文件删去，图谱稍后自动同步", res.ingest_error, { kind: "write" });
    }
    await refresh();
  }

  async function rejectMachineLink(link, note) {
    if (link.human || link.optimistic) return;
    if (!window.confirm("把这条连线标记为错误提取？\n\n" + linkAssertion(link) + "\n" + link.evidence_hint +
      "\n\n它会从画布上消失，之后可以撤销。")) return;
    const body = { src: link.src, dst: link.dst, type: link.type, extractor: link.extractor };
    try {
      await apiPost("/api/edges/reject", note ? Object.assign({ note: note }, body) : body);
    } catch (err) {
      showStatus("无法标记为错误提取", err, { kind: "write" }); // human_link -> use 「删除标注」
      return;
    }
    selectLink(null);
    cv.cardNote = { id: null, value: "" };
    cv.history = null;
    showStatus("已标记为错误提取", null, {
      kind: "undo", notice: true, ms: UNDO_MS,
      action: { label: "撤销", run: () => restoreLink(body) },
    });
    await refreshAll();
  }

  async function restoreLink(body) {
    hideStatus("undo");
    try {
      await apiPost("/api/edges/restore", body);
    } catch (err) {
      showStatus("无法撤销", err, { kind: "write" });
      return;
    }
    await refreshAll();
  }

  // 「确认这条连线」 / 「撤回」 from the card (9.3): one ledger entry through
  // POST /api/judgements, then 「撤销」 offered for 10 seconds -- an
  // `undone` entry naming it, so a mis-click is taken back on the record.
  async function judgeLink(link, verdict, note) {
    if (link.human || link.optimistic) return;
    const body = { src: link.src, dst: link.dst, type: link.type, extractor: link.extractor, verdict: verdict };
    if (note) body.note = note;
    try {
      await apiPost("/api/judgements", body);
    } catch (err) {
      showStatus(verdict === "withdrawn" ? "无法撤回" : "无法确认这条连线", err, { kind: "write" });
      return;
    }
    cv.cardNote = { id: null, value: "" };
    cv.history = null;
    showStatus(verdict === "withdrawn" ? "已撤回判断，连线按机器的结果显示" : "已确认这条连线", null, {
      kind: "undo", notice: true, ms: UNDO_MS,
      action: { label: "撤销", run: () => undoJudgement(body) },
    });
    await refreshAll();
  }

  async function undoJudgement(body) {
    hideStatus("undo");
    try {
      await apiPost("/api/judgements", { src: body.src, dst: body.dst, type: body.type, extractor: body.extractor, verdict: "undone" });
    } catch (err) {
      showStatus("无法撤销", err, { kind: "write" });
      return;
    }
    cv.history = null;
    await refreshAll();
  }

  // After a judgment, the header's counts and any open 待复核 list follow
  // too, not only this picture (app.html's refresh; this view's own load
  // without it).
  function refreshAll() {
    if (typeof refreshCurrentView === "function") return refreshCurrentView();
    return refresh();
  }

  // -- Pointer, wheel, gesture and keyboard handling (8.3) ------------------

  function svgPoint(e) {
    const v = viewSize();
    return { x: e.clientX - v.left, y: e.clientY - v.top };
  }

  function onPointerDown(e) {
    if (e.button !== 0 || !cv.data) return;
    if (cv.dom.menu && !cv.dom.menu.classList.contains("hidden")) cv.dom.menu.classList.add("hidden");
    closeCtxMenu();
    closePopover(); // a press anywhere on the canvas is 取消
    hideTip();
    if (!cv.space) {
      // An output socket wins over the card under it: that press draws.
      const w = worldPoint(e.clientX, e.clientY);
      const hit = socketAt(visibleNodes(), posOf, "out", w.x, w.y, hitRadius());
      if (hit) {
        startLinkDrag(hit, e);
        cv.dom.svg.setPointerCapture(e.pointerId);
        e.preventDefault();
        return;
      }
    }
    const nodeEl = e.target.closest(".cv-node");
    if (nodeEl && !cv.space) {
      const id = nodeEl.getAttribute("data-id");
      cv.drag = {
        kind: "node", id, el: nodeEl, sx: e.clientX, sy: e.clientY, start: posOf(id).slice(), moved: false,
        hadPosition: hasOwn(cv.positions, id),
      };
    } else {
      const linkEl = cv.space ? null : e.target.closest(".cv-link-g");
      cv.drag = {
        kind: "pan", sx: e.clientX, sy: e.clientY, cam: Object.assign({}, cv.camera), moved: false,
        linkId: linkEl ? linkEl.getAttribute("data-link") : null,
      };
      cv.dom.root.classList.add("panning");
    }
    cv.dom.svg.setPointerCapture(e.pointerId);
    e.preventDefault();
  }

  function moveNodeTo(id, el, pos) {
    cv.positions[id] = pos;
    placeNode(el, id);
    cv.linkEls.forEach(({ g, link }) => {
      if (link.from === id || link.to === id) updateLinkGeometry(link, g);
    });
    renderFrames();
    positionLinkCard();
  }

  function onPointerMove(e) {
    const d = cv.drag;
    if (!d) return;
    const dx = e.clientX - d.sx, dy = e.clientY - d.sy;
    if (d.kind === "link") { d.cx = e.clientX; d.cy = e.clientY; }
    if (!d.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    if (d.kind === "link") {
      if (!d.moved) { d.moved = true; cv.dom.root.classList.add("linking"); markLinkTargets(d); }
      updateLinkDrag();
      return;
    }
    d.moved = true;
    if (d.kind === "pan") {
      cv.camera = { x: d.cam.x + dx, y: d.cam.y + dy, zoom: d.cam.zoom };
      applyCamera();
      return;
    }
    d.el.classList.add("dragging");
    let x = d.start[0] + dx / cv.camera.zoom, y = d.start[1] + dy / cv.camera.zoom;
    if (e.shiftKey) { x = Math.round(x / SNAP) * SNAP; y = Math.round(y / SNAP) * SNAP; }
    moveNodeTo(d.id, d.el, [Math.round(x), Math.round(y)]);
  }

  function onPointerUp(e) {
    const d = cv.drag;
    if (!d) return;
    if (d.kind === "link") {
      if (e.type === "pointercancel") endLinkDrag(false);
      else if (d.moved) dropLink(d);
      else { endLinkDrag(false); onNodeClick(d.fromId); } // a click on a socket is a click on its card
      return;
    }
    cv.drag = null;
    cv.dom.root.classList.remove("panning");
    try { cv.dom.svg.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
    if (d.kind === "pan") {
      if (d.moved) userCamera();
      else if (d.linkId && cv.linkEls.has(d.linkId)) selectLink(d.linkId);
      else { // a click on empty canvas clears the selection
        if (cv.selected) select(null);
        if (cv.selectedLink) selectLink(null);
      }
      return;
    }
    d.el.classList.remove("dragging");
    if (d.moved) cardMoved(d.id);
    else onNodeClick(d.id);
  }

  // Click = select + the existing slide-out panel; double-click = the
  // existing POST /api/open. The panel opens a beat late on purpose: its
  // backdrop would otherwise swallow the second click of a double-click.
  function onNodeClick(id) {
    const now = Date.now();
    select(id);
    if (cv.lastClick && cv.lastClick.id === id && now - cv.lastClick.t < CLICK_DELAY_MS) {
      clearTimeout(cv.clickTimer);
      cv.lastClick = null;
      openWithDefaultApp(id);
      return;
    }
    cv.lastClick = { id, t: now };
    clearTimeout(cv.clickTimer);
    cv.clickTimer = setTimeout(() => {
      cv.lastClick = null;
      const n = cv.nodes.get(id);
      if (n && typeof openFilePanel === "function") openFilePanel(n.path, n.type === "script" ? "script" : undefined);
    }, CLICK_DELAY_MS);
  }

  async function openWithDefaultApp(id) {
    const n = cv.nodes.get(id);
    if (!n) return;
    try {
      await apiPost("/api/open", { path: n.path, reveal: false });
    } catch (err) {
      showStatus("无法打开 " + n.label, err, { kind: "open" }); // not "save": a pan's save must not hide it
    }
  }

  function onWheel(e) {
    if (!cv.data) return;
    e.preventDefault();
    const unit = e.deltaMode === 1 ? 16 : (e.deltaMode === 2 ? 400 : 1);
    if (e.ctrlKey || e.metaKey) {
      const p = svgPoint(e);
      const dy = clamp(e.deltaY * unit, -60, 60);
      setZoomAbout(cv.camera.zoom * Math.exp(-dy * 0.01), p.x, p.y);
      return;
    }
    cv.camera = { x: cv.camera.x - e.deltaX * unit, y: cv.camera.y - e.deltaY * unit, zoom: cv.camera.zoom };
    applyCamera();
    userCamera();
  }

  // WebKit (Safari, and the native shell's WKWebView) reports a trackpad
  // pinch as gesture events rather than ctrl+wheel.
  function onGestureStart(e) {
    e.preventDefault();
    cv.gestureZoom = cv.camera.zoom;
  }

  function onGestureChange(e) {
    e.preventDefault();
    if (cv.gestureZoom === undefined) return;
    const p = svgPoint(e);
    setZoomAbout(cv.gestureZoom * e.scale, p.x, p.y);
  }

  function onGestureEnd(e) {
    e.preventDefault();
    cv.gestureZoom = undefined;
  }

  function onPointerOver(e) {
    if (cv.drag) return;
    const nodeEl = e.target.closest(".cv-node");
    if (nodeEl) { setHovered(nodeEl.getAttribute("data-id")); return; }
    const linkEl = e.target.closest(".cv-link-g");
    if (linkEl) {
      const entry = cv.linkEls.get(linkEl.getAttribute("data-link"));
      if (entry) showTip(entry.link, e.clientX, e.clientY);
    }
  }

  function onPointerOut(e) {
    const from = e.target.closest(".cv-node, .cv-link-g");
    const to = e.relatedTarget && e.relatedTarget.closest ? e.relatedTarget.closest(".cv-node, .cv-link-g") : null;
    if (from === to) return;
    if (from && from.classList.contains("cv-node")) setHovered(null);
    if (from && from.classList.contains("cv-link-g")) hideTip();
  }

  function onHoverMove(e) {
    if (cv.drag || !cv.dom) return;
    if (!cv.dom.tip.classList.contains("hidden")) moveTip(e.clientX, e.clientY);
    const w = worldPoint(e.clientX, e.clientY);
    cv.dom.root.classList.toggle("on-socket", !cv.space && !!socketAt(visibleNodes(), posOf, "out", w.x, w.y, hitRadius()));
  }

  function bindSvgEvents(svg) {
    svg.addEventListener("pointerdown", onPointerDown);
    svg.addEventListener("pointermove", onPointerMove);
    svg.addEventListener("pointermove", onHoverMove);
    svg.addEventListener("pointerup", onPointerUp);
    svg.addEventListener("pointercancel", onPointerUp);
    svg.addEventListener("pointerover", onPointerOver);
    svg.addEventListener("pointerout", onPointerOut);
    svg.addEventListener("contextmenu", onContextMenu);
    svg.addEventListener("wheel", onWheel, { passive: false });
    svg.addEventListener("gesturestart", onGestureStart);
    svg.addEventListener("gesturechange", onGestureChange);
    svg.addEventListener("gestureend", onGestureEnd);
  }

  function canvasVisible() {
    return !!(cv.container && !cv.container.classList.contains("hidden") && cv.dom);
  }

  function typingTarget(e) {
    const t = e.target;
    if (t && t.closest && t.closest(".cv-pop, .cv-linkcard, .cv-ctx")) return true; // the canvas's own floating UI
    return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  }

  function panelOpen() {
    const p = document.getElementById("panel");
    return !!(p && !p.classList.contains("hidden"));
  }

  function onKeyDown(e) {
    if (!canvasVisible() || typingTarget(e)) return;
    if (e.key === "Escape") {
      // One Esc undoes the innermost thing: menu, popover, link drag,
      // then (as before) a card drag plus the selection.
      if (cv.ctx) { closeCtxMenu(); return; }
      if (cv.pop) { closePopover(); return; }
      if (cv.drag && cv.drag.kind === "link") { endLinkDrag(false); return; }
      if (cv.drag && cv.drag.kind === "node") {
        moveNodeTo(cv.drag.id, cv.drag.el, cv.drag.start);
        // Back where the layout had it: not a position this view holds.
        if (!cv.drag.hadPosition) delete cv.positions[cv.drag.id];
        cv.drag.el.classList.remove("dragging");
        cv.drag = null;
      }
      select(null);
      if (cv.selectedLink) selectLink(null);
      return;
    }
    if (panelOpen() || e.metaKey || e.ctrlKey || e.altKey) return;
    if ((e.key === "Backspace" || e.key === "Delete") && cv.selectedLink) {
      e.preventDefault();
      const entry = cv.linkEls.get(cv.selectedLink);
      if (entry && entry.link.human) deleteHumanLink(entry.link);
      return;
    }
    if (e.key === " ") {
      e.preventDefault(); // never scroll the page from the canvas
      if (!cv.space) { cv.space = true; cv.dom.root.classList.add("space"); }
    } else if (e.key === "f" || e.key === "F") {
      e.preventDefault();
      fitAll();
    }
  }

  function onKeyUp(e) {
    if (e.key === " " && cv.space) {
      cv.space = false;
      if (cv.dom) cv.dom.root.classList.remove("space");
    }
  }

  function onDocumentClick(e) {
    if (cv.dom && !cv.dom.menu.classList.contains("hidden") && !e.target.closest(".cv-more")) {
      cv.dom.menu.classList.add("hidden");
    }
    if (cv.ctx && !e.target.closest(".cv-ctx")) closeCtxMenu();
  }

  function bindGlobalEvents() {
    if (cv.listenersBound) return;
    cv.listenersBound = true;
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("keyup", onKeyUp);
    document.addEventListener("click", onDocumentClick);
    window.addEventListener("blur", () => { cv.space = false; if (cv.dom) cv.dom.root.classList.remove("space"); });
    if (typeof ResizeObserver !== "function") window.addEventListener("resize", onResize);
  }

  // -- Loading ------------------------------------------------------------------

  function canvasUrl(scope) {
    return "/api/canvas" + (scope ? "?scope=" + encodeURIComponent(scope) : "");
  }

  // The scope is NOT remembered across page loads: 8.7 says the selector
  // "defaults to the current attempt", and a scope restored from
  // browser storage replaced that default for good -- pick 全部 once and every
  // later launch opened on the whole-graph hairball 8.7 exists to avoid,
  // and kept opening on an old attempt after a new row was marked ✅
  // (adversarial review of the V4 work). A first load therefore asks the
  // server for its default; within one page the chosen scope survives
  // re-fetches (cv.scope). A chosen scope that no longer exists (the row
  // was deleted from the map) falls back to the default, not to an error.
  //
  // One request: auto-layout belongs to the view (8.4), so the cards of
  // THIS scope are all it needs. (The first V4 build also fetched scope
  // "all" to lay unsaved cards out over the whole graph; on real data
  // that scattered the default view over 4,000px.)
  async function fetchCanvas() {
    const scope = cv.scope;
    try {
      return await apiGet(canvasUrl(scope));
    } catch (err) {
      if (!scope || (typeof projectStateOf === "function" && projectStateOf(err))) throw err;
      cv.scope = null;
      return apiGet(canvasUrl(null));
    }
  }

  // The current attempt (8.7's default scope), whose island packs first.
  function currentAttemptId(payload) {
    const cur = (payload.scopes || []).find((s) => s.current);
    return cur ? cur.id : null;
  }

  // Lay the view out (8.4) over its own cards, the arrangement's fixed.
  // Re-run only when what is drawn changes -- the scope, the cards, the
  // links or which cards the arrangement holds -- or when 「重新排列」 asks.
  // In a pinned view that places only cards the arrangement lacks, clear
  // of the pinned ones; nothing pinned ever moves.
  function layoutView(force) {
    const d = cv.data;
    const key = [
      d.scope.id, d.nodes.map((n) => n.id).join("\n"), d.links.map((l) => l.id).join("\n"),
      Object.keys(cv.positions).sort().join("\n"),
    ].join("\f");
    if (!force && key === cv.layoutKey) return false;
    cv.layoutKey = key;
    const layout = computeLayout(d.nodes, d.links, d.frames, {
      fixed: cv.positions, current: currentAttemptId(d), groups: d.step_groups,
    });
    cv.auto = layout.positions;
    cv.cycle = layout.cycle;
    cv.loose = layout.loose;
    cv.looseIds = layout.looseIds;
    return true;
  }

  // 8.4 lays a view out fresh whenever what it draws changes, so a link
  // the researcher just confirmed (8.3's draw -> confirm -> re-render)
  // re-packs every unsaved card while the camera stayed put -- the new link
  // and both its ends could leave the screen (verifier finding on
  // bb76a9f). Cards must move; the camera need not. It follows the first
  // of `anchors` present before and after, so that card stays exactly
  // where it was on screen, then pans the least needed to show the cards
  // of `show` (world rects) if they fit. Pure, for the node tests.
  function keepCamera(camera, view, before, after, anchors, show) {
    const cam = { x: camera.x, y: camera.y, zoom: camera.zoom };
    const id = anchors.find((a) => before[a] && after[a]);
    if (id) {
      cam.x += (before[id][0] - after[id][0]) * cam.zoom;
      cam.y += (before[id][1] - after[id][1]) * cam.zoom;
    }
    if (!show.length || !view.w || !view.h) return cam;
    const m = 24;
    [["x", 0, 2, view.w], ["y", 1, 3, view.h]].forEach(([k, lo, hi, size]) => {
      const s0 = cam[k] + Math.min(...show.map((r) => r[lo])) * cam.zoom;
      const s1 = cam[k] + Math.max(...show.map((r) => r[hi])) * cam.zoom;
      if (s1 - s0 > size - 2 * m) return; // does not fit: the anchor alone holds
      if (s0 < m) cam[k] += m - s0;
      else if (s1 > size - m) cam[k] -= s1 - (size - m);
    });
    return cam;
  }

  // What the camera holds on through a re-layout, best first: the cards
  // of the link just confirmed (its drop target first -- under the
  // pointer), the selected card, then the card nearest the view's center.
  function cameraAnchors() {
    const ids = (cv.focus || []).concat(cv.selected ? [cv.selected] : []);
    const v = viewSize();
    if (v.w && v.h) {
      const cx = (v.w / 2 - cv.camera.x) / cv.camera.zoom, cy = (v.h / 2 - cv.camera.y) / cv.camera.zoom;
      let best = null, bd = Infinity;
      cv.nodes.forEach((n, id) => {
        const [x, y] = posOf(id);
        const d = (x + NODE_W / 2 - cx) ** 2 + (y + nodeHeight(n.type) / 2 - cy) ** 2;
        if (d < bd) { bd = d; best = id; }
      });
      if (best) ids.push(best);
    }
    return ids;
  }

  // A payload for the view on screen (a re-fetch) or for one being
  // entered (first load, scope switch, project switch, a stale scope that
  // fell back to the default). Entering: the view's own saved viewport
  // (8.6), else fit all (8.4). Staying: the camera holds unless an
  // UNPINNED view re-laid itself out because its links changed -- then
  // keepCamera holds the card being worked on in place.
  function applyPayload(payload) {
    // Another project under the same page (the server was switched from
    // another window): a new picture, and nothing queued here is its.
    const otherProject = !!cv.data && cv.project !== (payload.project || null);
    if (otherProject) {
      clearTimeout(cv.saveTimer);
      cv.save = emptySave();
    }
    const entering = !cv.data || otherProject || cv.data.scope.id !== payload.scope.id;
    // Where the camera's anchor cards sit now, before a re-layout moves them.
    const before = {};
    if (!entering) cameraAnchors().forEach((id) => { before[id] = posOf(id).slice(); });
    // A card drag in progress survives a re-fetch (8.4: nothing moves
    // unless the researcher moves it): where it is now, laid over below.
    const drag = cv.drag && cv.drag.kind === "node" ? cv.drag : null;
    const held = drag && drag.moved && !entering && cv.nodes.has(drag.id) ? posOf(drag.id).slice() : null;
    cv.data = payload;
    mergeOptimistic(payload);
    cv.scope = payload.scope.id;
    cv.project = payload.project || null;
    cv.nodes = new Map(payload.nodes.map((n) => [n.id, n]));
    cv.positions = arrangementOf(payload.positions);
    const slot = cv.save.scope === cv.scope && cv.save.project === cv.project ? cv.save : emptySave();
    cv.pinned = Object.keys(cv.positions).length > 0;
    const relaid = layoutView(entering);
    if (drag) {
      if (held && cv.nodes.has(drag.id)) cv.positions[drag.id] = held;
      else if (!drag.moved && !entering && cv.nodes.has(drag.id)) {
        drag.start = posOf(drag.id).slice(); // not moved yet: it starts from where it is drawn now
        drag.hadPosition = hasOwn(cv.positions, drag.id);
      } else {
        cv.drag = null; // its card left the view, or the view changed under it
        if (!drag.hadPosition) delete cv.positions[drag.id];
      }
    }
    if (relaid && !entering && !cv.pinned) {
      // An unpinned view re-laid out under the researcher's hand.
      const ids = Object.keys(before).filter((id) => cv.nodes.has(id));
      const after = {};
      ids.forEach((id) => { after[id] = posOf(id); });
      const show = (cv.focus || []).filter((id) => cv.nodes.has(id)).map((id) => {
        const [x, y] = posOf(id);
        return [x, y, x + NODE_W, y + nodeHeight(cv.nodes.get(id).type)];
      });
      cv.camera = keepCamera(cv.camera, viewSize(), before, after, ids, show);
      applyCamera();
      if (!cv.autoFit) queueViewport();
    }
    if (relaid || entering) cv.focus = null;
    if (cv.selected && !cv.nodes.has(cv.selected)) cv.selected = null;
    if (cv.hovered && !cv.nodes.has(cv.hovered)) cv.hovered = null;
    if (cv.selectedLink && !payload.links.some((l) => l.id === cv.selectedLink)) cv.selectedLink = null;
    renderScopeSelect();
    render();
    if (cv.drag && cv.drag.kind === "node") { // re-bound to the card just drawn
      cv.drag.el = cv.nodeEls.get(cv.drag.id) || cv.drag.el;
      if (cv.drag.moved) cv.drag.el.classList.add("dragging");
    }
    renderLayoutNotice(payload.layout);
    if (entering) {
      const vp = slot.viewport !== undefined ? slot.viewport : payload.viewport;
      if (vp) {
        cv.autoFit = false;
        cv.camera = { x: vp.x, y: vp.y, zoom: clamp(vp.zoom, ZOOM_MIN, ZOOM_MAX) };
        applyCamera();
      } else autoFitAll();
    }
  }

  // Load (or, on a generation bump, re-load) the canvas into `container`.
  // A re-load keeps selection, camera, search and positions; only entering
  // a view (first load, scope change) moves the camera.
  async function load(container) {
    const seq = ++cv.loadSeq;
    ensureDom(container);
    let payload;
    try {
      payload = await fetchCanvas();
    } catch (err) {
      if (seq !== cv.loadSeq) return;
      cv.dom = null;
      cv.data = null;
      renderViewFailure(container, err);
      return;
    }
    if (seq !== cv.loadSeq) return;
    if (typeof clearProjectState === "function") clearProjectState();
    ensureDom(container);
    container.dataset.loaded = "1";
    // The selected link's history may have moved with the record.
    if (cv.history && !cv.history.loading) cv.history = null;
    applyPayload(payload);
  }

  // 9.2: an arrangement record RCE cannot read is said, left untouched,
  // and nothing is saved over it until it is repaired or set aside.
  function layoutFrozen() {
    return !!(cv.data && cv.data.frozen);
  }

  function layoutBlocked() {
    const layout = cv.data && cv.data.layout;
    return !!(layout && layout.state && ["ok", "absent", "legacy"].indexOf(layout.state) < 0);
  }

  function renderLayoutNotice(layout) {
    if (!cv.dom) return;
    const el = cv.dom.notice;
    el.innerHTML = "";
    if (!layoutBlocked()) { el.classList.add("hidden"); return; }
    if (layout.state === "dataless") {
      el.appendChild(htmlEl("div", "cv-notice-title", "画布位置记录文件正在从云端下载…"));
      el.appendChild(htmlEl("div", null, "下载完成前先按自动排列显示；这期间不保存位置。"));
    } else {
      el.appendChild(htmlEl("div", "cv-notice-title", "画布位置记录文件无法读取"));
      el.appendChild(htmlEl("div", null,
        "这次不显示你摆放的位置，" + (layout.file || ".rce/canvas.json") + " 保持原样；修好之前，移动卡片不会被保存。"));
      const row = htmlEl("div", "cv-linkcard-actions");
      const b = htmlEl("button", "cv-text-btn", "把它移到备份，重新开始摆放");
      b.type = "button";
      b.addEventListener("click", () => setAsideLayout());
      row.appendChild(b);
      el.appendChild(row);
    }
    const why = htmlEl("div", "cv-hist-sub");
    if (layout.error) renderBlockingError(why, "", layout.error);
    el.appendChild(why);
    el.classList.remove("hidden");
  }

  async function setAsideLayout() {
    if (!window.confirm("把无法读取的画布位置记录移到 .rce/backups/？\n\n文件不会被删除；之后移动卡片会开始一份新的记录。")) return;
    try {
      await apiPost("/api/records/answer", { file: "canvas", answer: "set_aside" });
    } catch (err) {
      showStatus("没能移开画布位置记录", err, { kind: "write" });
      return;
    }
    await refresh();
  }

  // 8.7: the selection and a pinned link card belong to the view that was
  // left -- cleared at once, not when the new payload arrives (a link
  // present in both scopes kept its card floating over the new view).
  // So does anything half-done on a link: a drag, its confirm popover,
  // the right-click menu.
  function changeScope(scope) {
    if (cv.drag && cv.drag.kind === "link") endLinkDrag(false);
    closePopover();
    closeCtxMenu();
    cv.selected = null;
    cv.hovered = null;
    cv.focus = null;
    selectLink(null);
    flushSave(); // what was moved or panned in the view being left is that view's
    cv.scope = scope;
    if (cv.container) load(cv.container);
  }

  // Called by app.html when the tab is shown: a fit requested while the
  // view was hidden (zero size) happens now.
  function activate() {
    if (cv.needsFit && cv.dom && cv.data) fitTo([...cv.nodes.keys()]);
  }

  // A project switch: everything here belonged to the previous project.
  // Unsaved moves are dropped rather than posted -- the server now serves
  // the other project, and they must never land in its canvas.json.
  function reset() {
    clearTimeout(cv.saveTimer);
    clearTimeout(cv.clickTimer);
    clearTimeout(cv.resizeTimer);
    cv.loadSeq++;
    // Nothing being drawn, asked or offered belongs to the next project:
    // an open popover's entry, an optimistic link, an undo for a link
    // that lives in the previous project's graph.
    if (cv.drag && cv.drag.kind === "link") endLinkDrag(false);
    closePopover();
    closeCtxMenu();
    hideStatus();
    Object.assign(cv, {
      data: null, scope: null, nodes: new Map(), positions: {}, pinned: false, auto: {}, cycle: new Set(),
      loose: null, looseIds: [], layoutKey: null, selected: null, hovered: null, save: emptySave(), saveTimer: null,
      inflight: [], project: null,
      autoFit: true, resizeTimer: null,
      drag: null, lastClick: null, nodeEls: new Map(), linkEls: new Map(),
      selectedLink: null, optimistic: new Map(), focus: null,
      history: null, cardNote: { id: null, value: "" },
    });
    if (cv.dom) cv.dom.notice.classList.add("hidden");
    if (cv.dom) cv.dom.linkCard.classList.add("hidden");
    if (cv.dom) {
      cv.dom.search.value = "";
      cv.query = "";
    }
  }

  return {
    load, activate, reset, fitAll,
    zoomIn: () => zoomAboutCenter(cv.camera.zoom * 1.2),
    zoomOut: () => zoomAboutCenter(cv.camera.zoom / 1.2),
    zoomReset: () => zoomAboutCenter(1),
    // Exposed for the next phase (link editing) and for inspection; not
    // part of any server contract.
    _computeLayout: computeLayout,
    _looseColumns: looseColumns,
    _layoutView: layoutView,
    _cardMoved: cardMoved,
    _flushSave: flushSave,
    _applyPayload: applyPayload,
    _relayout: relayout,
    _keepCamera: keepCamera,
    _frameMembers: frameMembers,
    _checkConnection: checkConnection,
    _assertionText: assertionText,
    _socketAt: socketAt,
    _linkRules: LINK_RULES,
    _linkActions: linkActions,
    _layoutBlocked: layoutBlocked,
    _queueViewport: queueViewport,
    _showStatus: showStatus,
    _state: cv,
  };
})();
