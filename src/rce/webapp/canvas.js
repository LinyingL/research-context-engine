/*
  RCE node canvas (DESIGN.md section 8, task V4 phase 2a): the 「画布」 view.

  Served verbatim by rce.webapp.server at GET /canvas.js (same read-fresh,
  same origin-check discipline as app.html) and loaded by app.html with a
  plain same-origin <script src> placed BEFORE the page's inline script.
  Nothing here runs at load time except defining window.RCECanvas: every
  function that touches app.html's own helpers (apiGet, apiPost,
  openFilePanel, renderViewFailure, clearProjectState, projectStateOf,
  verdictMarker, VERDICT_BADGE_CLASS, state) does so only when called, by
  which time the inline script has defined them. Wrapped in one IIFE so no
  name here can collide with the page's own top-level declarations.

  What this phase draws (8.0-8.4, 8.7, 8.8) -- rendering, navigation and
  moving cards; NO link editing yet (the next phase builds on the small
  named functions below):

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
    - the 8.4 layered layout for every card without a saved position (a
      saved position always wins), positions persisted debounced 400ms
      through POST /api/canvas/layout, which also keeps the viewport.

  The page never writes the graph from here: this phase's only write is
  canvas.json (UI state, section 8.6), and the server confines that to a
  path computed from the served root alone.
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
  const GRID = 24;            // 8.2: 24px dot grid
  const SNAP = 8;             // 8.3: 8px snap while Shift is held
  const ZOOM_MIN = 0.25;
  const ZOOM_MAX = 2.5;
  const SAVE_DEBOUNCE_MS = 400;
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

  const STORAGE_SCOPE_PREFIX = "rce.canvas.scope:";

  // -- State --------------------------------------------------------------------
  // positions: saved positions (server canvas.json ∪ this page's unsaved
  // moves) -- the ones that always win. auto: the 8.4 layout for every
  // visible card, used only where no saved position exists. pending: what
  // the next debounced POST will send (null = forget that saved position).
  const cv = {
    dom: null, container: null, data: null, scope: null,
    nodes: new Map(), positions: {}, auto: {}, cycle: new Set(),
    camera: { x: 0, y: 0, zoom: 1 }, needsFit: false,
    selected: null, hovered: null, query: "",
    pending: {}, viewportDirty: false, saveTimer: null,
    space: false, drag: null, clickTimer: null, lastClick: null,
    loadSeq: 0, nodeEls: new Map(), linkEls: new Map(), listenersBound: false,
  };

  // -- Small helpers ----------------------------------------------------------

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

  function errText(err) { return err instanceof Error ? err.message : String(err); }

  function storageGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }

  function storageSet(key, value) {
    try {
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (e) { /* private window / blocked storage: a convenience, not state */ }
  }

  // The scope is remembered per project (attempt ids are only meaningful
  // within one), keyed by the served root app.html's loadProjects() saw.
  function scopeKey() {
    return STORAGE_SCOPE_PREFIX + ((typeof state !== "undefined" && state.projectPath) || "");
  }

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

  // -- Layout (8.4) -------------------------------------------------------------
  // Longest-path layering over the flow direction (dataset -> script for
  // 读取, script -> dataset/figure for 写出/生成): a card nothing flows into
  // is layer 0, anything else is 1 + the max layer of what flows into it --
  // exactly 8.4's three rules in one. A cycle is broken at the edge that
  // closes it (found by a deterministic DFS in step order) and that edge is
  // reported back so it can be drawn dashed in clay.

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

  function findCycleLinks(order, graph, byId) {
    const cycle = new Set();
    const color = new Map(); // 1 = on the DFS stack, 2 = done
    function visit(id) {
      color.set(id, 1);
      const next = graph.outs.get(id).slice().sort((a, b) => compareByStep(byId.get(a.to), byId.get(b.to)));
      for (const l of next) {
        const c = color.get(l.to) || 0;
        if (c === 1) cycle.add(l.id);
        else if (c === 0) visit(l.to);
      }
      color.set(id, 2);
    }
    order.forEach((n) => { if (!color.get(n.id)) visit(n.id); });
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

  // A card with no links at all that belongs to an attempt frame -- the
  // knitted .pdf ghost is the case -- goes beside its frame-mates rather
  // than into column 0 far away from them: one column right of the
  // rightmost linked member for a dataset/figure (it is that step's
  // product, the very link the researcher is about to draw), the same
  // column for a script. Not in 8.4's text; the obvious reading of 8.1.
  function placeIsolatedFrameMembers(layer, graph, frames, byId, cycle) {
    const linked = (id) => graph.ins.get(id).some((l) => !cycle.has(l.id)) ||
      graph.outs.get(id).some((l) => !cycle.has(l.id));
    frames.forEach((f) => {
      const members = f.node_ids.filter((id) => byId.has(id));
      const anchors = members.filter(linked);
      if (!anchors.length) return;
      const right = Math.max(...anchors.map((id) => layer.get(id)));
      members.filter((id) => !linked(id)).forEach((id) => {
        layer.set(id, byId.get(id).type === "script" ? right : right + 1);
      });
    });
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

  function orderAndPlace(order, graph, layer, cycle, frameOf) {
    const columns = new Map();
    order.forEach((n) => {
      const k = layer.get(n.id);
      if (!columns.has(k)) columns.set(k, []);
      columns.get(k).push(n);
    });
    const positions = {};
    const centerY = new Map();
    [...columns.keys()].sort((a, b) => a - b).forEach((k) => {
      // One barycenter pass: mean center y of already-placed neighbors
      // (both directions, so a frame-placed isolate is not special), ties
      // by step prefix. Cards with no placed neighbor sort after, in step
      // order.
      const bary = new Map();
      columns.get(k).forEach((n) => {
        const ys = [];
        graph.ins.get(n.id).forEach((l) => { if (!cycle.has(l.id) && centerY.has(l.from)) ys.push(centerY.get(l.from)); });
        graph.outs.get(n.id).forEach((l) => { if (!cycle.has(l.id) && centerY.has(l.to)) ys.push(centerY.get(l.to)); });
        bary.set(n.id, ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : Infinity);
      });
      const col = columns.get(k).slice().sort((a, b) => {
        const ba = bary.get(a.id), bb = bary.get(b.id);
        if (ba !== bb) return ba < bb ? -1 : 1;
        return compareByStep(a, b);
      });
      // Packed top-down with ROW_GAP between cards; a card with a
      // barycenter is pulled toward it (never closer than ROW_GAP to the
      // card above), so a straight chain reads as a straight line.
      let bottom = -Infinity;
      let prevFrame;
      col.forEach((n) => {
        const h = nodeHeight(n.type);
        const b = bary.get(n.id);
        const frame = frameOf.has(n.id) ? frameOf.get(n.id) : -1;
        const gap = bottom === -Infinity || frame === prevFrame
          ? ROW_GAP : ROW_GAP + 2 * FRAME_PAD + FRAME_TITLE_H;
        prevFrame = frame;
        let y = bottom === -Infinity ? 0 : bottom + gap;
        if (Number.isFinite(b)) y = Math.max(bottom === -Infinity ? -Infinity : bottom + gap, b - h / 2);
        positions[n.id] = [k * COL_GAP, Math.round(y)];
        centerY.set(n.id, y + h / 2);
        bottom = y + h;
      });
    });
    return positions;
  }

  function computeLayout(nodes, links, frames) {
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const order = nodes.slice().sort(compareByStep);
    const graph = flowGraph(nodes, links);
    const cycle = findCycleLinks(order, graph, byId);
    const layer = assignLayers(order, graph, cycle);
    placeIsolatedFrameMembers(layer, graph, frames || [], byId, cycle);
    return { positions: orderAndPlace(order, graph, layer, cycle, frameOfNode(frames)), cycle };
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
    world.append(frames, links, nodes);
    svg.append(defs, bg, world);
    return { svg, pattern, world, frames, links, nodes };
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
    root.append(s.svg, t.bar, zoom, status, tip, empty);
    container.appendChild(root);
    cv.dom = Object.assign({ root, zoom, status, tip, empty }, s, t);
    bindSvgEvents(s.svg);
    bindGlobalEvents();
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
        "cv-socket sock-" + sock.carries + (orphan ? " orphan" : "")));
      const label = svgEl("text", { x: 12, y: y + 4 }, "cv-socket-label");
      label.textContent = fitRight(sock.label, mono, NODE_W / 2 - 16);
      g.appendChild(label);
    });
    s.outputs.forEach((sock, i) => {
      const y = socketY(i);
      g.appendChild(svgEl("circle", { cx: NODE_W, cy: y, r: SOCKET_R }, "cv-socket sock-" + sock.carries));
      const label = svgEl("text", { x: NODE_W - 12, y: y + 4, "text-anchor": "end" }, "cv-socket-label");
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

  function linkClass(link) {
    const cls = ["cv-link"];
    if (cv.cycle.has(link.id)) cls.push("cycle");
    else cls.push(link.human ? "human" : "machine");
    if (!link.human && link.status === "pending") cls.push("pending");
    return cls.join(" ");
  }

  function renderLink(link) {
    const ends = linkEnds(link);
    if (!ends) return null;
    const g = svgEl("g", { "data-link": link.id }, "cv-link-g");
    const d = linkPathD(ends);
    g.appendChild(svgEl("path", { d }, linkClass(link)));
    g.appendChild(svgEl("path", { d }, "cv-link-hit"));
    if (link.human && !cv.cycle.has(link.id)) {
      const m = linkMidpoint(ends);
      g.appendChild(svgEl("circle", { cx: m.x, cy: m.y, r: 3 }, "cv-link-dot"));
    }
    return g;
  }

  function updateLinkGeometry(link, g) {
    const ends = linkEnds(link);
    if (!ends) return;
    const d = linkPathD(ends);
    g.querySelectorAll("path").forEach((p) => p.setAttribute("d", d));
    const dot = g.querySelector(".cv-link-dot");
    if (dot) {
      const m = linkMidpoint(ends);
      dot.setAttribute("cx", m.x);
      dot.setAttribute("cy", m.y);
    }
  }

  function frameTone(verdict) {
    const marker = typeof verdictMarker === "function" ? verdictMarker(verdict) : null;
    const badge = marker && typeof VERDICT_BADGE_CLASS !== "undefined" ? VERDICT_BADGE_CLASS[marker] : "badge-plain";
    return TONE_BY_BADGE[badge] || "plain";
  }

  function frameRect(frame) {
    const members = frame.node_ids.filter((id) => cv.nodes.has(id));
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
      cv.dom.empty.textContent = cv.scope === "all"
        ? "图谱里还没有数据集、脚本或图表。运行 rce ingest 之后，这里会画出它们。"
        : "这个尝试还没有可画的文件。可以在上方把范围切换到「全部」。";
    }
  }

  function render() {
    renderFrames();
    renderLinks();
    renderNodes();
    renderEmpty();
    updateLit();
    applySearch();
  }

  function scopeOptionLabel(s) {
    const marker = typeof verdictMarker === "function" ? verdictMarker(s.verdict) : null;
    return (marker ? marker + " " : "") + "#" + String(s.number).replace(/^#/, "") + " · " + (s.title || "");
  }

  function renderScopeSelect() {
    const sel = cv.dom.scope;
    sel.innerHTML = "";
    const all = document.createElement("option");
    all.value = "all";
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
  }

  function viewSize() {
    const r = cv.dom.svg.getBoundingClientRect();
    return { w: r.width, h: r.height, left: r.left, top: r.top };
  }

  function setZoomAbout(zoom, sx, sy) {
    const z0 = cv.camera.zoom;
    const z = clamp(zoom, ZOOM_MIN, ZOOM_MAX);
    const wx = (sx - cv.camera.x) / z0, wy = (sy - cv.camera.y) / z0;
    cv.camera = { x: sx - wx * z, y: sy - wy * z, zoom: z };
    applyCamera();
    queueViewport();
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
  function fitTo(ids) {
    if (!cv.dom) return;
    const v = viewSize();
    if (!v.w || !v.h) { cv.needsFit = true; return; }
    cv.needsFit = false;
    const b = boundsOf(ids);
    if (!b) { cv.camera = { x: FIT_PAD, y: FIT_PAD + 40, zoom: 1 }; applyCamera(); return; }
    const top = 56; // the floating toolbar
    const availW = v.w - 2 * FIT_PAD, availH = v.h - top - 2 * FIT_PAD;
    const z = clamp(Math.min(availW / (b.x1 - b.x0), availH / (b.y1 - b.y0), 1), ZOOM_MIN, ZOOM_MAX);
    cv.camera = {
      x: (v.w - (b.x1 - b.x0) * z) / 2 - b.x0 * z,
      y: top + FIT_PAD + (availH - (b.y1 - b.y0) * z) / 2 - b.y0 * z,
      zoom: z,
    };
    applyCamera();
    queueViewport();
  }

  function fitAll() {
    fitTo([...cv.nodes.keys()]);
  }

  function fitToMatches() {
    const m = matchingIds();
    if (m && m.size) fitTo([...m]);
  }

  // -- Persistence (8.6) --------------------------------------------------------

  function scheduleSave() {
    clearTimeout(cv.saveTimer);
    cv.saveTimer = setTimeout(flushSave, SAVE_DEBOUNCE_MS);
  }

  function queuePosition(id, pos) {
    if (pos === null) delete cv.positions[id];
    else cv.positions[id] = pos;
    cv.pending[id] = pos;
    scheduleSave();
  }

  function queueViewport() {
    if (!cv.data) return;
    cv.viewportDirty = true;
    scheduleSave();
  }

  async function flushSave() {
    cv.saveTimer = null;
    const body = {};
    const sent = cv.pending;
    if (Object.keys(sent).length) body.positions = sent;
    if (cv.viewportDirty) body.viewport = { x: cv.camera.x, y: cv.camera.y, zoom: cv.camera.zoom };
    if (!Object.keys(body).length) return;
    cv.pending = {};
    cv.viewportDirty = false;
    try {
      await apiPost("/api/canvas/layout", body);
      hideStatus();
    } catch (err) {
      // Keep what failed for the next save, unless a newer move replaced it.
      Object.entries(sent).forEach(([id, p]) => { if (!(id in cv.pending)) cv.pending[id] = p; });
      if (body.viewport) cv.viewportDirty = true;
      showStatus("位置未能保存，下次移动时会重试", err);
    }
  }

  // 「重新排列」(8.4): forget every saved position of the visible cards,
  // after asking -- it discards hand placement, which is the user's work.
  function relayout() {
    if (!cv.data) return;
    if (!window.confirm("重新排列当前画布？\n\n将丢弃你手动摆放的位置。")) return;
    cv.nodes.forEach((n, id) => { if (id in cv.positions) queuePosition(id, null); });
    render();
    fitAll();
  }

  // -- Status chip (product language; engine English on hover, 8.8) ---------

  function showStatus(text, err) {
    if (!cv.dom) return;
    cv.dom.status.textContent = text;
    cv.dom.status.title = err ? errText(err) : "";
    cv.dom.status.classList.remove("hidden");
  }

  function hideStatus() {
    if (cv.dom) cv.dom.status.classList.add("hidden");
  }

  // -- Hover card for links (8.2) -------------------------------------------

  function linkTipText(link) {
    if (cv.cycle.has(link.id)) return "检测到循环 · " + link.evidence_hint;
    if (!link.human && link.status === "pending") return link.evidence_hint + " · 待确认";
    return link.evidence_hint;
  }

  function showTip(link, clientX, clientY) {
    const tip = cv.dom.tip;
    tip.textContent = linkTipText(link);
    tip.classList.toggle("human", !!link.human);
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

  // -- Pointer, wheel, gesture and keyboard handling (8.3) ------------------

  function svgPoint(e) {
    const v = viewSize();
    return { x: e.clientX - v.left, y: e.clientY - v.top };
  }

  function onPointerDown(e) {
    if (e.button !== 0 || !cv.data) return;
    if (cv.dom.menu && !cv.dom.menu.classList.contains("hidden")) cv.dom.menu.classList.add("hidden");
    const nodeEl = e.target.closest(".cv-node");
    hideTip();
    if (nodeEl && !cv.space) {
      const id = nodeEl.getAttribute("data-id");
      cv.drag = { kind: "node", id, el: nodeEl, sx: e.clientX, sy: e.clientY, start: posOf(id).slice(), moved: false };
    } else {
      cv.drag = { kind: "pan", sx: e.clientX, sy: e.clientY, cam: Object.assign({}, cv.camera), moved: false };
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
  }

  function onPointerMove(e) {
    const d = cv.drag;
    if (!d) return;
    const dx = e.clientX - d.sx, dy = e.clientY - d.sy;
    if (!d.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
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
    cv.drag = null;
    cv.dom.root.classList.remove("panning");
    try { cv.dom.svg.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
    if (d.kind === "pan") {
      if (d.moved) queueViewport();
      else if (cv.selected) select(null); // a click on empty canvas clears the selection
      return;
    }
    d.el.classList.remove("dragging");
    if (d.moved) queuePosition(d.id, cv.positions[d.id]);
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
      showStatus("无法打开 " + n.label, err);
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
    queueViewport();
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
    if (!cv.drag && cv.dom && !cv.dom.tip.classList.contains("hidden")) moveTip(e.clientX, e.clientY);
  }

  function bindSvgEvents(svg) {
    svg.addEventListener("pointerdown", onPointerDown);
    svg.addEventListener("pointermove", onPointerMove);
    svg.addEventListener("pointermove", onHoverMove);
    svg.addEventListener("pointerup", onPointerUp);
    svg.addEventListener("pointercancel", onPointerUp);
    svg.addEventListener("pointerover", onPointerOver);
    svg.addEventListener("pointerout", onPointerOut);
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
    return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  }

  function panelOpen() {
    const p = document.getElementById("panel");
    return !!(p && !p.classList.contains("hidden"));
  }

  function onKeyDown(e) {
    if (!canvasVisible() || typingTarget(e)) return;
    if (e.key === "Escape") {
      if (cv.drag && cv.drag.kind === "node") {
        moveNodeTo(cv.drag.id, cv.drag.el, cv.drag.start);
        cv.drag.el.classList.remove("dragging");
        cv.drag = null;
      }
      select(null);
      return;
    }
    if (panelOpen() || e.metaKey || e.ctrlKey || e.altKey) return;
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
  }

  function bindGlobalEvents() {
    if (cv.listenersBound) return;
    cv.listenersBound = true;
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("keyup", onKeyUp);
    document.addEventListener("click", onDocumentClick);
    window.addEventListener("blur", () => { cv.space = false; if (cv.dom) cv.dom.root.classList.remove("space"); });
  }

  // -- Loading ------------------------------------------------------------------

  function canvasUrl(scope) {
    return "/api/canvas" + (scope ? "?scope=" + encodeURIComponent(scope) : "");
  }

  // A remembered scope that no longer exists (the row was deleted from
  // the map, or the remembered one belongs to another project) falls back
  // to the server's default scope rather than to an error box.
  async function fetchCanvas() {
    const scope = cv.scope || storageGet(scopeKey());
    try {
      return await apiGet(canvasUrl(scope));
    } catch (err) {
      if (!scope || (typeof projectStateOf === "function" && projectStateOf(err))) throw err;
      storageSet(scopeKey(), null);
      cv.scope = null;
      return await apiGet(canvasUrl(null));
    }
  }

  function applyPayload(payload, opts) {
    const first = !cv.data;
    cv.data = payload;
    cv.scope = payload.scope.id;
    storageSet(scopeKey(), cv.scope);
    cv.nodes = new Map(payload.nodes.map((n) => [n.id, n]));
    // Saved positions win over the layout (8.4); this page's own unsaved
    // moves win over what the server last stored.
    cv.positions = Object.assign({}, payload.positions);
    Object.entries(cv.pending).forEach(([id, p]) => {
      if (p === null) delete cv.positions[id];
      else cv.positions[id] = p;
    });
    const layout = computeLayout(payload.nodes, payload.links, payload.frames);
    cv.auto = layout.positions;
    cv.cycle = layout.cycle;
    if (cv.selected && !cv.nodes.has(cv.selected)) cv.selected = null;
    if (cv.hovered && !cv.nodes.has(cv.hovered)) cv.hovered = null;
    renderScopeSelect();
    render();
    if (opts.fit) fitAll();
    else if (first) {
      const vp = payload.viewport;
      if (vp) { cv.camera = { x: vp.x, y: vp.y, zoom: clamp(vp.zoom, ZOOM_MIN, ZOOM_MAX) }; applyCamera(); }
      else fitAll();
    }
  }

  // Load (or, on a generation bump, re-load) the canvas into `container`.
  // A re-load keeps selection, camera, search and positions; only a scope
  // change or the first load moves the camera.
  async function load(container, opts) {
    opts = opts || {};
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
    applyPayload(payload, opts);
  }

  function changeScope(scope) {
    cv.scope = scope;
    storageSet(scopeKey(), scope);
    if (cv.container) load(cv.container, { fit: true });
  }

  // Called by app.html when the tab is shown: a fit requested while the
  // view was hidden (zero size) happens now.
  function activate() {
    if (cv.needsFit && cv.dom && cv.data) fitAll();
  }

  // A project switch: everything here belonged to the previous project.
  // Unsaved moves are dropped rather than posted -- the server now serves
  // the other project, and they must never land in its canvas.json.
  function reset() {
    clearTimeout(cv.saveTimer);
    clearTimeout(cv.clickTimer);
    cv.loadSeq++;
    Object.assign(cv, {
      data: null, scope: null, nodes: new Map(), positions: {}, auto: {}, cycle: new Set(),
      selected: null, hovered: null, pending: {}, viewportDirty: false, saveTimer: null,
      drag: null, lastClick: null, nodeEls: new Map(), linkEls: new Map(),
    });
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
    _state: cv,
  };
})();
