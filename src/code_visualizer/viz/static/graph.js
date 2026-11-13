/* CViz standalone dependency graph.
 *
 * URL parameters
 *   path + line        artifact enclosing file:line (what the IDE sends)
 *   path + name + line exact artifact (what the page itself writes back)
 *   symbol [+ file]    resolve by name (links from docs / other tools)
 *   depth (1-5), kinds (CALLS,INHERITS,IMPORTS), ide (vscode|cursor|rider|idea|zed)
 *   changes=<branch>   the Changes view: what the current state changes against that branch
 *
 * Interaction: a click adds the node's neighbours to the current graph, a
 * double click recentres on it, right click (or the side panel) opens the
 * code in the configured IDE.
 */
(function () {
  "use strict";

  // ── Settings (per browser; storage may be unavailable) ──────────────────
  const SETTINGS_KEY = "cviz.graph.settings";
  const defaults = { ide: "", method: "auto", template: "", pathMap: "", apiKey: "", showHeuristic: true, showFramework: false, showTests: true, theme: "system" };
  function loadSettings() {
    try { return Object.assign({}, defaults, JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}")); }
    catch (_) { return Object.assign({}, defaults); }
  }
  function saveSettings(s) {
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch (_) { /* private mode */ }
  }
  let settings = loadSettings();
  // "system" follows prefers-color-scheme; light/dark force it (graph.css :root[data-theme]).
  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }
  applyTheme(settings.theme);
  let serverConfig = { default_ide: "vscode", path_map: [] };

  const qs = new URLSearchParams(location.search);
  if (qs.get("ide")) settings.ide = qs.get("ide");

  // ── DOM ─────────────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const svg = d3.select("#graph");
  const statusEl = $("status");
  const toastEl = $("toast");
  const panel = $("panel");
  const ctx = $("ctx");

  const state = {
    center: null,          // center node object from the server
    nodes: [],             // array of nodes (d3 mutates x/y)
    links: [],
    byId: new Map(),
    depth: clampDepth(qs.get("depth")),
    kinds: parseKinds(qs.get("kinds")),
    layout: "force",
    selected: null,
    shown: null,     // node whose details the panel shows (hovered or clicked)
    filter: "",
    expandedIds: new Set(), // nodes the user expanded in place (kept across live reloads)
    addedResults: new Map(), // search results added beside the graph (id -> target), kept across live reloads
    lastTarget: null,      // last request, for reloads
    stale: false,
    mode: "graph",         // "graph": a neighbourhood; "changes": the Changes view
  };

  function clampDepth(v) { const n = parseInt(v || "1", 10); return isNaN(n) ? 1 : Math.max(1, Math.min(5, n)); }
  function parseKinds(v) {
    if (!v) return ["CALLS", "INHERITS"];
    return v.split(",").map((k) => k.trim().toUpperCase()).filter(Boolean);
  }

  // ── HTTP ────────────────────────────────────────────────────────────────
  function headers(json) {
    const h = {};
    if (settings.apiKey) h["X-API-Key"] = settings.apiKey;
    if (json) h["Content-Type"] = "application/json";
    return h;
  }
  async function api(path, params) {
    const url = new URL(path, location.origin);
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
    });
    const res = await fetch(url, { headers: headers(false) });
    let body = null;
    try { body = await res.json(); } catch (_) { /* not JSON */ }
    if (!res.ok) {
      const err = new Error((body && body.detail) || res.statusText || "Request failed");
      err.status = res.status;
      throw err;
    }
    return body;
  }
  async function post(path, body) {
    const res = await fetch(path, { method: "POST", headers: headers(true), body: JSON.stringify(body) });
    let data = null;
    try { data = await res.json(); } catch (_) { /* ignore */ }
    if (!res.ok) throw new Error((data && data.detail) || res.statusText);
    return data;
  }

  // External framework types and test code are left out on the server, before
  // the per-node cap, so that they do not take the room of what is shown.
  function filterParams() {
    return {
      hide_framework: settings.showFramework ? undefined : "true",
      hide_tests: settings.showTests ? undefined : "true",
    };
  }
  function updateFilterCounts(filtered) {
    const f = filtered || {};
    $("framework-count").textContent = f.framework ? `(${f.framework} hidden)` : "";
    $("tests-count").textContent = f.tests ? `(${f.tests} hidden)` : "";
  }

  // ── Status / toast ──────────────────────────────────────────────────────
  let toastTimer = null;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2600);
  }
  function setStatus(msg, isError) {
    if (!msg) { statusEl.hidden = true; return; }
    statusEl.textContent = msg;
    statusEl.classList.toggle("error", !!isError);
    statusEl.hidden = false;
  }

  // ── Paths and IDE links ─────────────────────────────────────────────────
  function clientPathRules() {
    return (settings.pathMap || "").split(/\n|;/).map((l) => l.trim()).filter((l) => l.includes("="))
      .map((l) => { const i = l.indexOf("="); return [l.slice(0, i).trim().replace(/\/+$/, ""), l.slice(i + 1).trim().replace(/\/+$/, "")]; })
      .filter(([a, b]) => a && b)
      .sort((x, y) => y[0].length - x[0].length);
  }
  function localPath(p) {
    if (!p) return p;
    const norm = p.replace(/\\/g, "/");
    for (const [from, to] of clientPathRules()) {
      if (norm === from || norm.startsWith(from + "/")) return to + norm.slice(from.length);
    }
    return p;
  }
  function currentIde() { return settings.ide || serverConfig.default_ide || "vscode"; }
  const IDE_NAMES = { vscode: "VS Code", cursor: "Cursor", rider: "Rider", idea: "IntelliJ", zed: "Zed" };
  const URL_TEMPLATES = {
    vscode: "vscode://file/{path}:{line}:1",
    cursor: "cursor://file/{path}:{line}:1",
    rider: "rider://open?file={qpath}&line={line}",
    idea: "idea://open?file={qpath}&line={line}",
    zed: "zed://file/{path}:{line}",
  };
  function ideUrl(path, line) {
    const tpl = settings.template || URL_TEMPLATES[currentIde()] || URL_TEMPLATES.vscode;
    let p = path.replace(/\\/g, "/");
    if (!p.startsWith("/")) p = "/" + p; // Windows drive paths: /C:/...
    const pathForUrl = encodeURI(p.slice(1)).replace(/#/g, "%23").replace(/\?/g, "%3F");
    return tpl
      .replace("{path}", pathForUrl)
      .replace("{qpath}", encodeURIComponent(path))
      .replace("{line}", String(line || 1));
  }
  // JetBrains IDEs document a command-line launcher (`rider --line N file`)
  // but no stable per-file URL scheme, so "auto" launches them through the
  // CViz server; VS Code, Cursor and Zed register vscode://, cursor://, zed://.
  const SERVER_LAUNCH_BY_DEFAULT = new Set(["rider", "idea"]);
  function openMethod() {
    if (settings.method !== "auto") return settings.method;
    if (settings.template) return "url";
    return SERVER_LAUNCH_BY_DEFAULT.has(currentIde()) ? "server" : "url";
  }
  async function openInIde(path, line) {
    if (!path) { toast("No source location for this node"); return; }
    const target = localPath(path);
    if (openMethod() === "server") {
      try {
        await post("/api/open", { path: target, line: line || 1, ide: currentIde() });
        toast(`Opened in ${IDE_NAMES[currentIde()] || currentIde()}`);
      } catch (e) { setStatus(`Could not open: ${e.message}`, true); }
      return;
    }
    // Inside the desktop app (a tab is an iframe), a custom-scheme navigation
    // goes nowhere: the app opens the URL with the system instead.
    if (window.parent !== window) {
      window.parent.postMessage({ type: "cviz:open-url", url: ideUrl(target, line) }, "*");
      return;
    }
    // A custom-scheme navigation does not leave the page; if no handler is
    // registered the browser silently ignores it — hence the server fallback.
    window.location.href = ideUrl(target, line);
  }

  // ── Loading ─────────────────────────────────────────────────────────────
  let loadSeq = 0;
  async function load(target, opts) {
    opts = opts || {};
    const seq = ++loadSeq;
    state.lastTarget = target;
    setChangesMode(false);
    setStatus("Loading…");
    $("empty").hidden = true;
    try {
      const data = await api("/api/neighborhood", {
        path: target.path,
        line: target.line,
        name: target.name,
        kind: target.kind,
        depth: state.depth,
        kinds: state.kinds.join(","),
        per_node: target.perNode,
        ...filterParams(),
      });
      if (seq !== loadSeq) return; // a newer request superseded this one
      setStatus("");
      state.stale = false;
      updateLive();
      updateFilterCounts(data.filtered);
      applyData(data, opts);
      markEntryPoint(data.center && data.center.id);
      if (!opts.noHistory) writeUrl(data.center, opts.replace);
      if (data.truncated) toast("Some neighbours are hidden (+N on nodes). Use Expand to see them.");
    } catch (e) {
      if (seq !== loadSeq) return;
      setStatus(e.message, true);
      if (!state.center) $("empty").hidden = false;
    }
  }

  function writeUrl(center, replace) {
    const p = new URLSearchParams();
    p.set("path", center.path || center.indexed_path || "");
    if (center.kind === "File") p.set("kind", "File");
    else {
      if (center.name) p.set("name", center.name);
      if (center.line) p.set("line", center.line);
    }
    if (state.depth !== 1) p.set("depth", state.depth);
    const k = state.kinds.join(",");
    if (k !== "CALLS,INHERITS") p.set("kinds", k);
    if (qs.get("ide")) p.set("ide", qs.get("ide"));
    const url = `${location.pathname}?${p.toString()}`;
    if (url === location.pathname + location.search) return;
    if (replace) history.replaceState({ center }, "", url);
    else history.pushState({ center }, "", url);
  }

  function targetFromNode(n) {
    if (n.kind === "File") return { path: n.path, kind: "File" };
    return { path: n.path, name: n.name, line: n.line, kind: n.kind };
  }

  function applyData(data, opts) {
    const keepPositions = opts && opts.keepPositions;
    const old = new Map(state.nodes.map((n) => [n.id, n]));
    state.center = data.center;
    state.nodes = data.nodes.map((n) => {
      const prev = keepPositions ? old.get(n.id) : null;
      return prev ? Object.assign(n, { x: prev.x, y: prev.y }) : n;
    });
    state.byId = new Map(state.nodes.map((n) => [n.id, n]));
    state.links = data.links.map((l) => Object.assign({}, l));
    // Grid position relative to the center: callers left, callees right,
    // bases above, derived below, one step per level.
    state.nodes.forEach((n) => {
      const [sx, sy] = roleSign(n);
      const d = n.role === "center" ? 0 : Math.max(1, n.depth || 1);
      n.col = sx * d; n.lvl = sy * d;
    });
    const center = state.byId.get(data.center.id);
    if (center) center.expanded = true;
    document.title = `${data.center.label} · CViz graph`;
    render();
    // Live reloads keep the user's selection; navigation selects the center.
    const kept = opts && opts.keepSelection && state.selected && state.byId.get(state.selected.id);
    const sel = kept || state.byId.get(data.center.id);
    select(sel);
    if (opts && opts.keepSelection) {
      // Replay the in-place expansions on top of the refreshed neighbourhood.
      const ids = [...state.expandedIds];
      const added = [...state.addedResults.values()];
      (async () => {
        for (const t of added) await addArtifact(t, { quiet: true });
        for (const id of ids) { const n = state.byId.get(id); if (n && !n.expanded) await expand(n, { quiet: true }); }
      })();
    } else {
      state.expandedIds.clear();
      state.addedResults.clear();
    }
  }

  function mergeData(data) {
    const anchor = state.byId.get(data.center.id);
    let added = 0;
    for (const n of data.nodes) {
      if (n.id === data.center.id) {
        if (anchor) anchor.hidden = n.hidden; // expansion reveals the counts
        continue;
      }
      if (state.byId.has(n.id)) continue;
      // Roles in the response are relative to the expanded node, so new
      // neighbours are laid out around it rather than around the center.
      const [sx, sy] = roleSign(n);
      n.col = (anchor ? anchor.col || 0 : 0) + sx;
      n.lvl = (anchor ? anchor.lvl || 0 : 0) + sy;
      n.depth = (anchor ? anchor.depth || 0 : 0) + 1;
      if (anchor) {
        n.x = (anchor.x || 0) + sx * 60 + (Math.random() - 0.5) * 40;
        n.y = (anchor.y || 0) + sy * 60 + (Math.random() - 0.5) * 40;
      }
      state.nodes.push(n);
      state.byId.set(n.id, n);
      added++;
    }
    const key = (l) => `${typeof l.source === "object" ? l.source.id : l.source}|${typeof l.target === "object" ? l.target.id : l.target}|${l.type}`;
    const seen = new Set(state.links.map(key));
    for (const l of data.links) if (!seen.has(key(l))) state.links.push(Object.assign({}, l));
    render(true);
    return added;
  }

  async function expand(node, opts) {
    if (!node || !node.path) return;
    const quiet = opts && opts.quiet;
    node.expanded = true;
    state.expandedIds.add(node.id);
    if (!quiet) setStatus(`Expanding ${node.label}…`);
    try {
      const t = targetFromNode(node);
      const data = await api("/api/neighborhood", {
        path: t.path, name: t.name, line: t.line, kind: t.kind,
        depth: 1, kinds: state.kinds.join(","), per_node: 500, max_nodes: 600,
        ...filterParams(),
      });
      setStatus("");
      const added = mergeData(data);
      if (!quiet) {
        select(state.byId.get(node.id));
        toast(added ? `+${added} node${added > 1 ? "s" : ""} from ${node.label}` : `No new neighbours for ${node.label}`);
      }
    } catch (e) {
      node.expanded = false;
      state.expandedIds.delete(node.id);
      setStatus(e.message, true);
    }
  }

  // Adds a search result (and its direct neighbours) to the current graph,
  // next to it, instead of replacing the graph.
  async function addArtifact(target, opts) {
    const quiet = opts && opts.quiet;
    if (!state.center) { await load(target); return; }
    const existing = target.id && state.byId.get(target.id);
    if (existing) {
      select(existing);
      if (!existing.expanded && existing.path) await expand(existing);
      return;
    }
    if (!quiet) setStatus(`Adding ${target.name || target.path}…`);
    try {
      const data = await api("/api/neighborhood", {
        path: target.path, name: target.name, line: target.line, kind: target.kind,
        depth: 1, kinds: state.kinds.join(","), per_node: 500, max_nodes: 600,
        ...filterParams(),
      });
      setStatus("");
      let node = state.byId.get(data.center.id);
      if (!node) {
        node = Object.assign({}, data.nodes.find((n) => n.id === data.center.id) || data.center, { role: "result" });
        const xs = state.nodes.map((n) => n.x || 0), cols = state.nodes.map((n) => n.col || 0);
        node.x = (xs.length ? Math.max(...xs) : 0) + 220;
        node.y = (state.nodes.reduce((s, n) => s + (n.y || 0), 0) / Math.max(1, state.nodes.length));
        node.col = (cols.length ? Math.max(...cols) : 0) + 2;
        node.lvl = 0; node.depth = 0;
        state.nodes.push(node);
        state.byId.set(node.id, node);
      }
      node.expanded = true;
      state.expandedIds.add(node.id);
      state.addedResults.set(node.id, target);
      mergeData(data);
      if (!quiet) { select(node); toast(`Added ${node.label}`); setTimeout(() => fit(true), 450); }
    } catch (e) {
      setStatus(e.message, true);
    }
  }

  // ── Rendering ───────────────────────────────────────────────────────────
  const zoom = d3.zoom().scaleExtent([0.05, 6]).on("zoom", (e) => root.attr("transform", e.transform));
  svg.call(zoom).on("dblclick.zoom", null);
  const defs = svg.append("defs");
  [["arrow", "var(--edge)"], ["arrow-inherit", "var(--edge-inherit)"], ["arrow-hl", "var(--accent)"]].forEach(([id, color]) => {
    defs.append("marker").attr("id", id).attr("viewBox", "0 -5 10 10").attr("refX", 10).attr("refY", 0)
      // Sized in user space: with 1px edges, stroke-relative heads would be tiny.
      .attr("markerUnits", "userSpaceOnUse").attr("markerWidth", 8).attr("markerHeight", 8).attr("orient", "auto")
      .append("path").attr("d", "M0,-5L10,0L0,5").attr("fill", color);
  });
  const root = svg.append("g");
  const linkLayer = root.append("g");
  const nodeLayer = root.append("g");
  let simulation = null;

  const ROLE_COLOR = {
    center: "var(--center)", caller: "var(--caller)", callee: "var(--callee)",
    parent: "var(--parent)", child: "var(--child)", member: "var(--member)", import: "var(--import)",
    result: "var(--result)",
  };
  // Changes view: modified entities take the center's colour, new ones green,
  // deleted ones red (hollow: they are not in the graph any more); the mark
  // before the label says it without colour too.
  const CHANGE_MARK = { added: "+ ", modified: "~ ", deleted: "− ", moved: "→ ", renamed: "→ ", undefined: "" };
  function nodeFill(n) {
    if (n.change === "added") return "var(--added)";
    if (n.change === "deleted") return "var(--danger)";
    if (n.change) return "var(--center)";
    return ROLE_COLOR[n.role] || "var(--member)";
  }
  function changeText(n) {
    switch (n.change) {
      case "deleted": return `deleted on this branch${n.members ? ` with its ${n.members} method${n.members > 1 ? "s" : ""}` : ""}`;
      case "moved": return `moved from ${n.moved_from}${n.body_changed ? ", and modified" : ""}`;
      case "renamed": return `renamed from ${n.renamed_from}${n.moved_from ? ` (${n.moved_from})` : ""}`;
      default: return `${n.change} on this branch`;
    }
  }
  const CLASS_LIKE = new Set(["Class", "Interface", "Struct", "Enum", "Trait", "Record", "ExternalClass"]);
  function symbolFor(n) {
    const size = 170; // every node the same size (the center is told apart by its colour)
    let type = d3.symbolCircle;
    if (CLASS_LIKE.has(n.kind)) type = d3.symbolSquare;
    else if (n.kind === "File") type = d3.symbolDiamond;
    else if (n.kind === "Module") type = d3.symbolTriangle;
    return d3.symbol().type(type).size(size)();
  }
  function radius() { return 8; }

  // Where an external type comes from (indexing reads the project files and
  // usings; "usings" is a deduction, "ambiguous" lists the candidates).
  function originClass(n) {
    if (n.kind !== "ExternalClass") return "";
    if (n.origin === "ambiguous" || n.origin === "unknown" || !n.origin) return " uncertain";
    if (n.evidence === "usings") return " probable";
    return "";
  }
  function originText(n) {
    if (n.kind !== "ExternalClass") return "(external)";
    switch (n.origin) {
      case "framework": return ".NET framework";
      case "package": {
        const pkg = `NuGet ${n.package}${n.package_version ? " " + n.package_version : ""}`;
        return n.evidence === "usings" ? `${pkg} (probable, from the usings)` : pkg;
      }
      case "ambiguous": return `One of: ${(n.candidates || []).join(", ")}`;
      default: return "Package or DLL not identified";
    }
  }
  function calledMembers(n) {
    const out = new Set();
    state.links.forEach((l) => {
      if ((l.target.id || l.target) === n.id) (l.members || []).forEach((m) => out.add(m === n.name ? `new ${m}()` : m));
    });
    return [...out].sort();
  }
  function hiddenCount(n) { return Object.values(n.hidden || {}).reduce((a, b) => a + b, 0); }

  // Horizontal flow: callers on the left, callees on the right; inheritance vertical.
  function roleSign(n) {
    switch (n.role) {
      case "caller": return [-1, 0];
      case "callee": return [1, 0];
      case "parent": return [0, -1];
      case "child": return [0, 1];
      case "member": return [1, 0];
      case "import": return [-1, 0];
      default: return [0, 0];
    }
  }

  // Heuristic call edges (resolved by name only) can be hidden; nodes that
  // were only reachable through them disappear with them.
  function visibleGraph() {
    if (settings.showHeuristic) return { nodes: state.nodes, links: state.links };
    const links = state.links.filter((l) => !l.heuristic);
    const keep = new Set();
    links.forEach((l) => { keep.add(l.source.id || l.source); keep.add(l.target.id || l.target); });
    const nodes = state.nodes.filter((n) => n.role === "center" || n.change || keep.has(n.id));
    return { nodes, links };
  }
  function heuristicCount() { return state.links.filter((l) => l.heuristic).length; }
  function updateHeuristicToggle() {
    const n = heuristicCount();
    $("heuristic-count").textContent = n ? `(${n})` : "";
  }

  function render(incremental) {
    const view = visibleGraph();
    state.viewNodes = view.nodes;
    state.viewLinks = view.links;
    const nodes = view.nodes;
    const links = view.links;
    updateHeuristicToggle();
    if (simulation) simulation.stop();

    const linkSel = linkLayer.selectAll("path.edge").data(links, (l) => `${l.source.id || l.source}|${l.target.id || l.target}|${l.type}`);
    linkSel.exit().remove();
    linkSel.enter().append("path")
      .merge(linkSel)
      .attr("class", (l) => `edge ${l.type}${l.heuristic ? " heuristic" : ""}${l.via ? " via" : ""}${l.broken ? " broken" : ""}`)
      .attr("marker-end", (l) => l.type === "INHERITS" ? "url(#arrow-inherit)" : "url(#arrow)");

    const nodeSel = nodeLayer.selectAll("g.node").data(nodes, (n) => n.id);
    nodeSel.exit().remove();
    const entered = nodeSel.enter().append("g")
      .on("click", (e, n) => { e.stopPropagation(); onNodeClick(n); })
      .on("dblclick", (e, n) => { e.stopPropagation(); e.preventDefault(); recenter(n); })
      .on("contextmenu", (e, n) => { e.preventDefault(); e.stopPropagation(); showCtx(e, n); })
      .on("mouseenter", (e, n) => { highlight(n); hoverDetails(n); })
      .on("mouseleave", () => { highlight(null); clearTimeout(hoverTimer); })
      .call(d3.drag().on("start", dragStart).on("drag", dragged).on("end", dragEnd));
    entered.append("path").attr("class", "shape");
    entered.append("text").attr("class", "label").attr("text-anchor", "middle");
    entered.append("text").attr("class", "badge");
    entered.append("title");

    const all = entered.merge(nodeSel);
    all.attr("class", (n) => `node${n.external ? " external" : ""}${n.change ? " changed " + n.change : ""}${originClass(n)}${state.selected && state.selected.id === n.id ? " selected" : ""}`);
    all.select("path.shape").attr("d", symbolFor).attr("fill", nodeFill);
    all.select("text.label")
      .attr("dy", (n) => -(radius(n) + 6))
      .text((n) => CHANGE_MARK[n.change] + (n.label.length > 28 ? n.label.slice(0, 26) + "…" : n.label));
    all.select("text.badge")
      .attr("x", (n) => radius(n) + 4).attr("dy", 4)
      .text((n) => { const h = hiddenCount(n); return h ? `+${h}` : ""; });
    all.select("title").text((n) => `${n.kind} ${n.name || ""}\n${n.path ? n.path + (n.line ? ":" + n.line : "") : originText(n)}`);

    applyFilter();
    layout(incremental);
  }

  function edgePath(l) {
    const s = l.source, t = l.target;
    if (s === t || s.id === t.id) {
      const r = radius(s) + 4;
      return `M${s.x},${s.y - r} a14,14 0 1,1 ${r},${r}`;
    }
    const dx = t.x - s.x, dy = t.y - s.y;
    const dist = Math.hypot(dx, dy) || 1;
    const rt = radius(t) + 3, rs = radius(s);
    const x1 = s.x + (dx / dist) * rs, y1 = s.y + (dy / dist) * rs;
    const x2 = t.x - (dx / dist) * rt, y2 = t.y - (dy / dist) * rt;
    return `M${x1},${y1}L${x2},${y2}`;
  }

  function tick() {
    linkLayer.selectAll("path.edge").attr("d", edgePath);
    nodeLayer.selectAll("g.node").attr("transform", (n) => `translate(${n.x},${n.y})`);
  }

  function layout(incremental) {
    const nodes = state.viewNodes;
    const links = state.viewLinks;
    const center = nodes.find((n) => n.role === "center");
    nodes.forEach((n) => { n.fx = null; n.fy = null; });
    if (center) { center.fx = 0; center.fy = 0; }

    if (state.layout === "tree" || state.layout === "radial") {
      positionStatic(state.layout);
      // Resolve link endpoints to objects for drawing.
      links.forEach((l) => {
        if (typeof l.source !== "object") l.source = state.byId.get(l.source);
        if (typeof l.target !== "object") l.target = state.byId.get(l.target);
      });
      state.viewLinks = links.filter((l) => l.source && l.target);
      tick();
      if (!incremental) fit(true);
      return;
    }

    const STEP = 220;
    simulation = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(links).id((n) => n.id).distance((l) => (l.type === "CONTAINS" ? 80 : 120)).strength(0.4))
      .force("charge", d3.forceManyBody().strength(nodes.length > 150 ? -160 : -380))
      .force("collide", d3.forceCollide().radius(34))
      .force("x", d3.forceX((n) => (n.col || 0) * STEP).strength((n) => (n.col ? 0.25 : 0.05)))
      .force("y", d3.forceY((n) => (n.lvl || 0) * STEP * 0.7).strength((n) => (n.lvl ? 0.25 : 0.05)))
      .on("tick", tick);
    if (!incremental) {
      // Pre-run so the first paint is already settled, then fit.
      simulation.stop();
      const n = Math.min(300, Math.ceil(Math.log(simulation.alphaMin()) / Math.log(1 - simulation.alphaDecay())));
      for (let i = 0; i < n; i++) simulation.tick();
      tick();
      fit(false);
      simulation.alpha(0.05).restart();
    } else {
      simulation.alpha(0.6).restart();
    }
  }

  function positionStatic(kind) {
    const groups = new Map();
    for (const n of state.viewNodes) {
      const key = `${n.col || 0}|${n.lvl || 0}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(n);
    }
    for (const [key, members] of groups) {
      const [col, lvl] = key.split("|").map(Number);
      members.sort((a, b) => (a.label || "").localeCompare(b.label || ""));
      if (kind === "tree") {
        const COL = 260, ROW = 34, LVL = 140;
        members.forEach((n, i) => {
          const offset = (i - (members.length - 1) / 2);
          if (lvl && !col) { n.x = offset * ROW * 4; n.y = lvl * LVL; }
          else { n.x = col * COL; n.y = lvl * LVL + offset * ROW; }
        });
      } else {
        const ring = Math.abs(col) + Math.abs(lvl);
        if (!ring) { members.forEach((n, i) => { n.x = i ? (i * 30) : 0; n.y = 0; }); continue; }
        const R = 200 * ring;
        const base = Math.atan2(lvl, col);
        const spread = Math.min(Math.PI * 0.9, members.length * 0.18);
        members.forEach((n, i) => {
          const a = members.length === 1 ? base : base - spread / 2 + (spread * i) / (members.length - 1);
          n.x = Math.cos(a) * R; n.y = Math.sin(a) * R;
        });
      }
    }
  }

  // The part of the graph not covered by the side panel or the changes list.
  function freeBox() {
    const svgBox = svg.node().getBoundingClientRect();
    // The right column holds the panel and the changes list.
    const shown = [panel, $("changes")].find((el) => !el.hidden);
    const sideBox = shown && window.innerWidth > 760 ? shown.getBoundingClientRect() : null;
    const left = 0;
    const right = sideBox ? Math.max(left + 200, sideBox.left - svgBox.left) : svgBox.width;
    return { left, width: right - left, height: svgBox.height };
  }

  function fit(animate) {
    const nodes = state.viewNodes || state.nodes;
    if (!nodes.length) return;
    // The side panel and the changes list float over the graph: frame the
    // nodes in the area they leave.
    const box = freeBox();
    const xs = nodes.map((n) => n.x || 0), ys = nodes.map((n) => n.y || 0);
    const minX = Math.min(...xs) - 80, maxX = Math.max(...xs) + 80;
    const minY = Math.min(...ys) - 60, maxY = Math.max(...ys) + 60;
    const scale = Math.max(0.05, Math.min(1.6, Math.min(box.width / (maxX - minX), box.height / (maxY - minY))));
    const t = d3.zoomIdentity.translate(box.left + box.width / 2 - scale * (minX + maxX) / 2, box.height / 2 - scale * (minY + maxY) / 2).scale(scale);
    (animate ? svg.transition().duration(350) : svg).call(zoom.transform, t);
  }

  function dragStart(e, n) { if (simulation && !e.active) simulation.alphaTarget(0.2).restart(); n.fx = n.x; n.fy = n.y; }
  function dragged(e, n) { n.fx = e.x; n.fy = e.y; if (!simulation) { n.x = e.x; n.y = e.y; tick(); } }
  function dragEnd(e, n) { if (simulation && !e.active) simulation.alphaTarget(0); if (n.role !== "center") { n.fx = null; n.fy = null; } }

  function highlight(n) {
    const ids = new Set();
    if (n) {
      ids.add(n.id);
      state.links.forEach((l) => { if (l.source.id === n.id || l.target.id === n.id) { ids.add(l.source.id); ids.add(l.target.id); } });
    }
    linkLayer.selectAll("path.edge")
      .classed("hl", (l) => !!n && (l.source.id === n.id || l.target.id === n.id))
      .attr("marker-end", (l) => (n && (l.source.id === n.id || l.target.id === n.id)) ? "url(#arrow-hl)" : (l.type === "INHERITS" ? "url(#arrow-inherit)" : "url(#arrow)"));
    if (!state.filter) nodeLayer.selectAll("g.node").classed("dim", (x) => !!n && !ids.has(x.id));
  }

  function applyFilter() {
    const q = state.filter.toLowerCase();
    nodeLayer.selectAll("g.node").classed("dim", (n) => !!q && !(`${n.label} ${n.path || ""}`.toLowerCase().includes(q)));
    linkLayer.selectAll("path.edge").classed("dim", !!q);
  }

  // ── Interaction ─────────────────────────────────────────────────────────
  // Single click adds the node's neighbours to the current graph; a double
  // click (or "Center here") replaces the graph with that node's
  // neighbourhood. The single-click action waits a moment so a double click
  // does not also expand.
  let clickTimer = null;
  function onNodeClick(n) {
    hideCtx();
    select(n);
    clearTimeout(clickTimer);
    if (!n.path || n.expanded) return;
    clickTimer = setTimeout(() => expand(n), 230);
  }
  function recenter(n) {
    clearTimeout(clickTimer);
    hideCtx();
    if (!n || !n.path || n.role === "center") { select(n); return; }
    load(targetFromNode(n), { keepPositions: true });
  }

  // The panel shows the node hovered last (after a short pause) or clicked;
  // clicking also selects it (outline, expansion): see onNodeClick.
  function select(n) {
    state.selected = n || null;
    nodeLayer.selectAll("g.node").classed("selected", (x) => !!n && x.id === n.id);
    if (!n) { state.shown = null; panel.hidden = true; return; }
    showDetails(n);
  }

  // Hover shows a node's details after a short pause, so that moving the
  // mouse across the graph (e.g. towards the panel) does not flicker it.
  const HOVER_DELAY_MS = 250;
  let hoverTimer = null;
  function hoverDetails(n) {
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => { if (state.shown !== n) showDetails(n); }, HOVER_DELAY_MS);
  }

  function showDetails(n) {
    state.shown = n;
    panel.hidden = false;
    const companion = companionOf(n.path || n.indexed_path);
    const role = n.change ? changeText(n) + (n.untested ? " · no test reaches it" : "")
      : n.uses_gone ? `still uses ${n.uses_gone.join(", ")}`
      : n.via_type ? `method of ${n.via_type}, that a changed method implements` : n.role;
    $("p-kind").textContent = `${n.kind}${role && role !== "center" ? " · " + role : ""}${n.external ? " · external" : ""}`
      + (companion ? ` · ${companion.name}` : "");
    $("p-name").textContent = n.label;
    $("p-path").textContent = n.path
      || (n.change === "deleted" ? `${n.old_rel}:${n.line} (before this branch)` : "(not in the indexed sources)");
    $("p-copy").hidden = !n.path;
    const ext = n.kind === "ExternalClass";
    ["p-origin", "p-origin-dt", "p-members", "p-members-dt"].forEach((id) => { $(id).hidden = !ext; });
    if (ext) {
      $("p-origin").textContent = originText(n);
      $("p-members").textContent = calledMembers(n).join(", ") || "—";
    }
    $("p-lines").textContent = n.line ? `${n.line}${n.end_line && n.end_line !== n.line ? "–" + n.end_line : ""}` : "—";
    $("p-cc").textContent = n.complexity != null ? String(n.complexity) : "—";
    const hidden = Object.entries(n.hidden || {}).filter(([, v]) => v);
    $("p-hidden").innerHTML = "";
    if (hidden.length) {
      $("p-hidden").textContent = hidden.map(([k, v]) => `${v} ${k}${v > 1 ? "s" : ""}`).join(", ") + " ";
      const more = document.createElement("button");
      more.className = "link"; more.textContent = "show";
      more.onclick = () => expand(n);
      $("p-hidden").appendChild(more);
    } else $("p-hidden").textContent = "none";
    $("p-open").textContent = `Open in ${IDE_NAMES[currentIde()] || currentIde()}`;
    $("p-open").disabled = !n.path;
    $("p-center").disabled = !n.path || n.role === "center";
    $("p-expand").disabled = !n.path;
    $("p-expand").textContent = n.expanded ? "Expand again" : "Expand";
    renderRelations(n);
    loadSource(n);
  }

  // Only the counts: the graph itself shows who calls whom.
  function renderRelations(n) {
    const shown = state.viewLinks || state.links;
    const count = (end) => shown.filter((l) => l.type === "CALLS" && (l[end].id || l[end]) === n.id).length;
    $("p-in-count").textContent = count("target");
    $("p-out-count").textContent = count("source");
  }

  const sourceCache = new Map();
  async function loadSource(n) {
    const pre = $("p-source");
    pre.textContent = "";
    if (n.code) { pre.appendChild(renderSource(n.code, n.old_rel || "", n.line || 1)); return; }  // deleted: the old code
    const p = n.indexed_path || n.path;
    if (!p || !n.line) { pre.textContent = "No source available."; return; }
    try {
      let text = sourceCache.get(p);
      if (text === undefined) {
        text = (await api("/api/file", { path: p })).content || "";
        sourceCache.set(p, text);
      }
      if (state.shown !== n) return;
      const lines = text.split("\n");
      const start = n.kind === "File" ? 1 : n.line;
      const end = Math.min(lines.length, (n.end_line && n.end_line >= start ? n.end_line : start + 20), start + 150);
      pre.appendChild(renderSource(lines.slice(start - 1, end).join("\n"), p, start));
    } catch (e) { pre.textContent = `Source unavailable: ${e.message}`; }
  }

  // ── Syntax highlighting (small tokenizer, no dependency) ────────────────
  // Comments, strings, numbers, keywords, literals, calls, types and
  // decorators/annotations, for the languages CViz indexes. Only the snippet is
  // tokenized: a block comment opened above it is not seen, which is harmless.
  const HASH_COMMENT = new Set(["py", "pyi", "rb", "sh", "bash", "zsh", "pl", "r", "jl", "ex", "exs", "nim", "cr", "yaml", "yml", "toml", "ps1"]);
  const DASH_COMMENT = new Set(["lua", "sql", "hs", "elm"]);
  const CHAR_QUOTE = new Set(["rs", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "java", "cs", "go", "kt", "kts", "scala", "swift"]);
  const TRIPLE_QUOTE = new Set(["py", "pyi", "kt", "kts", "swift", "scala", "cs", "java", "dart"]);
  const KEYWORDS = new Set((
    "abstract and as assert async await base break case catch class const continue crate def defer del delegate " +
    "do dyn elif else enum except export extends extension extern final finally fn for foreach from func function " +
    "get global go guard if impl implements import in include init inline instanceof interface internal is lambda " +
    "let loop match mod module move mut namespace new nonlocal not object of operator or out override package pass " +
    "private protected protocol pub public raise readonly record ref require return sealed select set static " +
    "struct super switch template throw throws trait try type typedef typeof union unsafe use using val var virtual " +
    "void volatile when where while with yield " +
    "int long short float double char bool boolean byte string str u8 u16 u32 u64 usize i8 i16 i32 i64 isize f32 f64 auto"
  ).split(" "));
  const LITERALS = new Set("true false null nil None True False undefined self Self this NaN".split(" "));

  function sourceTokens(code, ext) {
    const hash = HASH_COMMENT.has(ext) || ext === "php";
    const dash = DASH_COMMENT.has(ext);
    const slash = !HASH_COMMENT.has(ext) && !dash;
    const triple = TRIPLE_QUOTE.has(ext);
    const charOnly = CHAR_QUOTE.has(ext);
    const out = [];
    let plain = "";
    const push = (cls, text) => { if (plain) { out.push([null, plain]); plain = ""; } out.push([cls, text]); };
    const until = (from, stop) => { const k = code.indexOf(stop, from); return k === -1 ? code.length : k + stop.length; };
    const ident = /[A-Za-z_$][\w$]*/y;
    const num = /(?:0[xX][\da-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)[A-Za-z]*/y;
    const charLit = /'(?:\\.|[^\\'\n]){1,4}'/y;
    let i = 0;
    while (i < code.length) {
      const ch = code[i], two = code.substr(i, 2), three = code.substr(i, 3);
      let j = -1, cls = null;
      if (slash && two === "/*") { j = until(i + 2, "*/"); cls = "com"; }
      else if (slash && (two === "#[" || code.startsWith("#![", i))) { j = until(i + 2, "]"); cls = "meta"; }
      else if ((slash && two === "//") || (hash && ch === "#" && code[i + 1] !== "[" && code[i + 1] !== "!") || (dash && two === "--")) {
        j = code.indexOf("\n", i); if (j === -1) j = code.length; cls = "com";
      } else if (triple && (three === '"""' || (three === "'''" && !charOnly))) {
        j = until(i + 3, three); cls = "str";
      } else if (ch === '"' || ch === "`" || (ch === "'" && !charOnly)) {
        j = i + 1;
        while (j < code.length && code[j] !== ch && (ch === "`" || code[j] !== "\n")) j += code[j] === "\\" ? 2 : 1;
        j = Math.min(j + 1, code.length); cls = "str";
      } else if (ch === "'") {
        charLit.lastIndex = i;
        if (charLit.test(code)) { j = charLit.lastIndex; cls = "str"; }
      } else if (/\d/.test(ch) && !/[\w$]/.test(code[i - 1] || "")) {
        num.lastIndex = i; num.test(code); j = num.lastIndex; cls = "num";
      } else if (ch === "@" && /[A-Za-z_]/.test(code[i + 1] || "")) {
        ident.lastIndex = i + 1; ident.test(code); j = ident.lastIndex; cls = "meta";
      } else if (/[A-Za-z_$]/.test(ch)) {
        ident.lastIndex = i; ident.test(code); j = ident.lastIndex;
        const word = code.slice(i, j);
        const member = code[i - 1] === ".";  // obj.get(), obj.type: not keywords
        if (KEYWORDS.has(word) && !member) cls = "kw";
        else if (LITERALS.has(word)) cls = "lit";
        else if (/^\s*!?\s*\(/.test(code.slice(j, j + 40))) cls = "fn";
        else if (/^[A-Z]/.test(word) && /[a-z]/.test(word)) cls = "type";
        if (!cls) { plain += word; i = j; continue; }
      }
      if (cls && j > i) { push(cls, code.slice(i, j)); i = j; }
      else { plain += ch; i++; }
    }
    if (plain) out.push([null, plain]);
    return out;
  }

  function renderSource(code, path, firstLine) {
    const ext = (path.split(/[\\/]/).pop().split(".").pop() || "").toLowerCase();
    const frag = document.createDocumentFragment();
    let lineNo = firstLine;
    const lineNumber = () => {
      const no = document.createElement("span"); no.className = "ln-no"; no.textContent = lineNo++;
      frag.appendChild(no);
    };
    lineNumber();
    sourceTokens(code, ext).forEach(([cls, text]) => {
      text.split("\n").forEach((piece, k) => {
        if (k > 0) { frag.appendChild(document.createTextNode("\n")); lineNumber(); }
        if (!piece) return;
        if (!cls) { frag.appendChild(document.createTextNode(piece)); return; }
        const span = document.createElement("span"); span.className = cls; span.textContent = piece;
        frag.appendChild(span);
      });
    });
    frag.appendChild(document.createTextNode("\n"));
    return frag;
  }

  function showCtx(e, n) {
    ctx.hidden = false;
    ctx._node = n;
    ctx.querySelector('[data-act="open"]').disabled = !n.path;
    ctx.querySelector('[data-act="center"]').disabled = !n.path || n.role === "center";
    ctx.querySelector('[data-act="expand"]').disabled = !n.path;
    const w = ctx.offsetWidth || 190, h = ctx.offsetHeight || 140;
    ctx.style.left = Math.min(e.clientX, window.innerWidth - w - 8) + "px";
    ctx.style.top = Math.min(e.clientY, window.innerHeight - h - 8) + "px";
  }
  function hideCtx() { ctx.hidden = true; }
  ctx.addEventListener("click", (e) => {
    const act = e.target.dataset.act; const n = ctx._node;
    hideCtx();
    if (!n || !act) return;
    if (act === "open") openInIde(n.path, n.line);
    if (act === "center") recenter(n);
    if (act === "expand") expand(n);
    if (act === "copy") copy(`${localPath(n.path)}${n.line ? ":" + n.line : ""}`);
  });
  document.addEventListener("click", hideCtx);
  svg.on("click", () => { hideCtx(); });

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast("Copied"); }
    catch (_) { toast(text); }
  }

  // ── Toolbar wiring ──────────────────────────────────────────────────────
  $("depth").value = state.depth;
  $("depth-value").textContent = state.depth;
  $("depth").addEventListener("input", (e) => { $("depth-value").textContent = e.target.value; });
  $("depth").addEventListener("change", (e) => {
    state.depth = clampDepth(e.target.value);
    if (state.mode === "graph" && state.center) load(targetFromNode(state.center), { replace: true });
  });
  const kindBoxes = { CALLS: $("kind-calls"), INHERITS: $("kind-inherits"), IMPORTS: $("kind-imports") };
  Object.entries(kindBoxes).forEach(([k, box]) => {
    box.checked = state.kinds.includes(k);
    box.addEventListener("change", () => {
      state.kinds = Object.entries(kindBoxes).filter(([, b]) => b.checked).map(([kk]) => kk);
      if (!state.kinds.length) { box.checked = true; state.kinds = [k]; }
      if (state.mode === "graph" && state.center) load(targetFromNode(state.center), { replace: true });
    });
  });
  document.querySelectorAll("[data-layout]").forEach((b) => b.addEventListener("click", () => {
    state.layout = b.dataset.layout;
    document.querySelectorAll("[data-layout]").forEach((x) => x.classList.toggle("active", x === b));
    if (state.nodes.length) render();
  }));
  [["kind-framework", "showFramework"], ["kind-tests", "showTests"]].forEach(([id, key]) => {
    $(id).checked = !!settings[key];
    $(id).addEventListener("change", (e) => {
      settings[key] = e.target.checked;
      saveSettings(settings);
      reloadView({ replace: true });
    });
  });
  $("kind-heuristic").checked = settings.showHeuristic !== false;
  $("kind-heuristic").addEventListener("change", (e) => {
    settings.showHeuristic = e.target.checked;
    saveSettings(settings);
    if (state.nodes.length) {
      render();
      if (state.selected && !state.viewNodes.includes(state.selected)) select(state.byId.get(state.center.id) || null);
      else if (state.selected) select(state.selected);
    }
  });
  // Fit to screen (button, F key): clear what floats over the graph — the side
  // panel, an expanded chat, the entry points — then frame the whole graph.
  function fitToScreen() {
    select(null);
    if (!chatEl.classList.contains("chat-idle") && !chatEl.classList.contains("collapsed")) {
      chatEl.classList.add("collapsed");
      $("chat-toggle").setAttribute("aria-expanded", "false");
      $("chat-toggle").title = "Expand";
    }
    $("entries").classList.add("collapsed");
    $("entries-toggle").setAttribute("aria-expanded", "false");
    $("changes").classList.add("collapsed");
    $("ch-toggle").setAttribute("aria-expanded", "false");
    fit(true);
  }
  $("btn-fit").addEventListener("click", fitToScreen);
  $("btn-png").addEventListener("click", exportPng);
  $("p-close").addEventListener("click", () => select(null));
  // The panel's buttons act on the node it shows (hovered or clicked).
  $("p-open").addEventListener("click", () => state.shown && openInIde(state.shown.path, state.shown.line));
  $("p-center").addEventListener("click", () => state.shown && recenter(state.shown));
  $("p-expand").addEventListener("click", () => state.shown && expand(state.shown));
  $("p-copy").addEventListener("click", () => state.shown && copy(`${localPath(state.shown.path)}${state.shown.line ? ":" + state.shown.line : ""}`));

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) { if (e.key === "Escape") e.target.blur(); return; }
    if (e.key === "/") { e.preventDefault(); chatText.focus(); }
    else if (e.key === "f" || e.key === "F") fitToScreen();
    else if (e.key === "Escape") { hideCtx(); select(null); }
    else if (e.key === "o" && (state.shown || state.selected)) {
      const n = state.shown || state.selected;
      openInIde(n.path, n.line);
    }
  });

  window.addEventListener("popstate", () => {
    const p = new URLSearchParams(location.search);
    if (p.get("changes")) { openChanges(p.get("changes"), { noHistory: true }); return; }
    const t = targetFromParams(p);
    if (t) { state.depth = clampDepth(p.get("depth")); $("depth").value = state.depth; $("depth-value").textContent = state.depth; load(t, { noHistory: true }); }
  });


  // ── Settings ────────────────────────────────────────────────────────────
  $("btn-settings").addEventListener("click", () => {
    $("s-ide").value = currentIde();
    $("s-method").value = settings.method;
    $("s-template").value = settings.template;
    $("s-pathmap").value = settings.pathMap;
    $("s-apikey").value = settings.apiKey;
    $("s-theme").value = settings.theme || "system";
    $("settings").returnValue = "";  // Escape must not reuse the previous "save"
    showTab("general");
    fillAiSettings();
    $("settings").showModal();
  });
  $("s-theme").addEventListener("change", () => applyTheme($("s-theme").value));  // live preview
  $("settings").addEventListener("close", () => {
    if ($("settings").returnValue !== "save") { applyTheme(settings.theme); return; }
    settings = {
      ide: $("s-ide").value, method: $("s-method").value, template: $("s-template").value.trim(),
      pathMap: $("s-pathmap").value, apiKey: $("s-apikey").value.trim(),
      showHeuristic: settings.showHeuristic, theme: $("s-theme").value,
    };
    applyTheme(settings.theme);
    saveSettings(settings);
    if (state.selected) select(state.selected);
    connectEvents();
    if (aiDirty) saveAiSettings().then((ok) => toast(ok ? "Settings saved" : "Settings saved (AI settings not saved)"));
    else toast("Settings saved");
  });

  // ── Settings tabs ───────────────────────────────────────────────────────
  function showTab(name) {
    document.querySelectorAll("#settings [data-tab]").forEach((b) => {
      const on = b.dataset.tab === name;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll("#settings [data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== name; });
  }
  document.querySelectorAll("#settings [data-tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  // ── AI settings (stored server-side: /api/ai/*) ─────────────────────────
  const LLM_DEFAULT_URLS = { lmstudio: "http://127.0.0.1:1234", ollama: "http://127.0.0.1:11434", other: "" };
  let aiStatus = null;   // last /api/ai/status
  let aiDirty = false;
  let embedRunning = false;
  let embedBadge = "";   // live badge text while a vectorization runs
  function setEmbedBadge(text) { embedBadge = text; updateLive(); }

  async function refreshAiStatus() {
    try { aiStatus = await api("/api/ai/status"); } catch (_) { aiStatus = null; }
    // A vectorization started before the page opened (cviz graph start-up).
    const running = !!(aiStatus && aiStatus.job && aiStatus.job.running);
    if (running && !embedBadge) setEmbedBadge("vectorizing…");
    else if (!running && embedBadge) setEmbedBadge("");
    return aiStatus;
  }
  function llmReady() { return !!(aiStatus && aiStatus.llm_configured); }

  function describeEmbedState() {
    const el = $("ai-embed-state");
    if (!aiStatus) { el.textContent = "AI settings unavailable on this server."; return; }
    if (embedRunning) return;
    const s = aiStatus.stats || { items: 0, embedded: 0 };
    let text = `${s.embedded} / ${s.items} artifacts vectorized · ${aiStatus.embed_model}`;
    if (aiStatus.embed_problem) text = `Cannot vectorize: ${aiStatus.embed_problem}`;
    else if (aiStatus.index_error) text = `Search index unavailable: ${aiStatus.index_error}`;
    else if (!s.items) text = `Nothing indexed for search yet · ${aiStatus.embed_model}`;
    el.textContent = text;
  }

  function syncAiFieldVisibility() {
    const remote = $("ai-embed-provider").value === "openai";
    document.querySelectorAll('.ai-remote[data-for="embed"]').forEach((el) => { el.hidden = !remote; });
    $("ai-embed-model").placeholder = remote ? "e.g. text-embedding-nomic-embed-text-v1.5" : "Xenova/multilingual-e5-small";
    $("ai-embed-load").hidden = !remote;
  }

  async function fillAiSettings() {
    await refreshAiStatus();
    const c = (aiStatus && aiStatus.config) || {};
    $("ai-enabled").checked = String(c.SEMANTIC_SEARCH) === "true";
    $("ai-embed-provider").value = c.SEARCH_EMBED_PROVIDER || "local";
    $("ai-embed-url").value = c.SEARCH_EMBED_URL || "";
    $("ai-embed-model").value = c.SEARCH_EMBED_MODEL || "";
    $("ai-embed-key").value = "";
    $("ai-embed-key").placeholder = c.SEARCH_EMBED_API_KEY_SET ? "saved (type to replace)" : "none";
    $("ai-llm-provider").value = c.CHAT_LLM_PROVIDER || "lmstudio";
    $("ai-llm-url").value = c.CHAT_LLM_URL || "";
    $("ai-llm-model").value = c.CHAT_LLM_MODEL || "";
    $("ai-llm-key").value = "";
    $("ai-llm-key").placeholder = c.CHAT_LLM_API_KEY_SET ? "saved (type to replace)" : "none";
    syncAiFieldVisibility();
    describeEmbedState();
    aiDirty = false;
  }

  function aiValues() {
    const v = {
      SEMANTIC_SEARCH: $("ai-enabled").checked ? "true" : "false",
      SEARCH_EMBED_PROVIDER: $("ai-embed-provider").value,
      SEARCH_EMBED_URL: $("ai-embed-url").value.trim(),
      SEARCH_EMBED_MODEL: $("ai-embed-model").value.trim(),
      CHAT_LLM_PROVIDER: $("ai-llm-provider").value,
      CHAT_LLM_URL: $("ai-llm-url").value.trim(),
      CHAT_LLM_MODEL: $("ai-llm-model").value.trim(),
    };
    // An empty key field keeps the saved key.
    if ($("ai-embed-key").value.trim()) v.SEARCH_EMBED_API_KEY = $("ai-embed-key").value.trim();
    if ($("ai-llm-key").value.trim()) v.CHAT_LLM_API_KEY = $("ai-llm-key").value.trim();
    return v;
  }

  async function saveAiSettings() {
    try {
      await post("/api/ai/config", aiValues());
      aiDirty = false;
      $("ai-readonly").hidden = true;
      await refreshAiStatus();
      describeEmbedState();
      return true;
    } catch (e) {
      if (/this machine/i.test(e.message)) $("ai-readonly").hidden = false;
      setStatus(`AI settings not saved: ${e.message}`, true);
      return false;
    }
  }

  document.querySelectorAll('#settings [data-pane="ai"] input, #settings [data-pane="ai"] select')
    .forEach((el) => el.addEventListener("input", () => { aiDirty = true; }));
  $("ai-embed-provider").addEventListener("change", () => { aiDirty = true; syncAiFieldVisibility(); });
  $("ai-llm-provider").addEventListener("change", () => {
    aiDirty = true;
    const url = $("ai-llm-url").value.trim();
    if (!url || Object.values(LLM_DEFAULT_URLS).includes(url)) $("ai-llm-url").value = LLM_DEFAULT_URLS[$("ai-llm-provider").value] || url;
  });

  async function loadModels(target) {
    const isLlm = target === "llm";
    const url = $(isLlm ? "ai-llm-url" : "ai-embed-url").value.trim();
    if (!url) { toast("Enter the server URL first"); return; }
    const key = $(isLlm ? "ai-llm-key" : "ai-embed-key").value.trim();
    const button = $(isLlm ? "ai-llm-load" : "ai-embed-load");
    button.disabled = true;
    try {
      const { models } = await post("/api/ai/models", { url, api_key: key || null, use_saved_key: key ? null : target });
      const list = $(isLlm ? "ai-llm-models" : "ai-embed-models");
      list.innerHTML = "";
      models.forEach((m) => { const o = document.createElement("option"); o.value = m; list.appendChild(o); });
      toast(`${models.length} model${models.length === 1 ? "" : "s"} found: pick one in the Model field`);
      $(isLlm ? "ai-llm-model" : "ai-embed-model").focus();
    } catch (e) {
      toast(e.message);
    } finally { button.disabled = false; }
  }
  $("ai-llm-load").addEventListener("click", () => loadModels("llm"));
  $("ai-embed-load").addEventListener("click", () => loadModels("embed"));

  $("ai-embed-run").addEventListener("click", async () => {
    if (aiDirty && !(await saveAiSettings())) return;
    try {
      await post("/api/ai/embed", {});
      embedRunning = true;
      $("ai-embed-run").disabled = true;
      $("ai-embed-state").textContent = "Vectorizing…";
    } catch (e) { toast(e.message); }
  });

  function onEmbedEvent(ev) {
    const bar = $("ai-embed-progress");
    if (ev.type === "embed:progress") {
      embedRunning = true;
      setEmbedBadge(`vectorizing ${ev.done} / ${ev.total}`);
      bar.hidden = false; bar.max = ev.total || 1; bar.value = ev.done || 0;
      $("ai-embed-state").textContent = `Vectorizing… ${ev.done} / ${ev.total}`;
      $("ai-embed-run").disabled = true;
    } else if (ev.type === "embed:done") {
      bar.hidden = true;
      // The job flag drops just after the last repository's embed:done.
      const settle = () => refreshAiStatus().then(() => {
        if (aiStatus && aiStatus.job && aiStatus.job.running) { setTimeout(settle, 1500); return; }
        embedRunning = false;
        $("ai-embed-run").disabled = false;
        describeEmbedState();
      });
      settle();
      if (ev.error) toast(`Vectorization: ${ev.error}`);
    }
  }

  // ── Chat: natural-language search ───────────────────────────────────────
  const chatEl = $("chat"), chatLog = $("chat-log"), chatText = $("chat-text"), chatSend = $("chat-send");
  const chatHistory = [];   // {role, content} sent to /api/chat
  let chatBusy = false;

  function autoGrow() {
    chatText.style.height = "auto";
    chatText.style.height = Math.min(chatText.scrollHeight, 112) + "px";
    chatSend.disabled = chatBusy || !chatText.value.trim();
  }
  function scrollChat() {
    const until = performance.now() + 320;
    const step = () => { chatLog.scrollTop = chatLog.scrollHeight; if (performance.now() < until) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  }
  function dots() {
    const d = document.createElement("span"); d.className = "dots";
    d.innerHTML = "<i></i><i></i><i></i>";
    return d;
  }
  function addMessage(role) {
    chatEl.classList.remove("chat-idle", "collapsed");
    $("chat-toggle").setAttribute("aria-expanded", "true");
    const msg = document.createElement("div");
    msg.className = `msg ${role}`;
    const who = document.createElement("span");
    who.className = "who"; who.textContent = role === "user" ? "You" : "Search";
    msg.appendChild(who);
    chatLog.appendChild(msg);
    scrollChat();
    return msg;
  }
  // Plain text with `code` spans; never interpreted as HTML.
  function richText(el, text) {
    String(text).split(/(`[^`]+`)/).forEach((part) => {
      if (/^`[^`]+`$/.test(part)) { const c = document.createElement("code"); c.textContent = part.slice(1, -1); el.appendChild(c); }
      else el.appendChild(document.createTextNode(part));
    });
  }
  function hitTarget(h) { return { id: h.id, path: h.path, name: h.name, line: h.line, kind: h.kind }; }
  function hitList(hits, dimmed) {
    const ul = document.createElement("ul");
    ul.className = "hits" + (dimmed ? " dimmed" : "");
    hits.forEach((h) => {
      const li = document.createElement("li"); li.className = "hit";
      li.title = h.snippet || "";
      const dot = document.createElement("i"); dot.className = "kdot";
      const name = document.createElement("button");
      name.type = "button"; name.className = "hname"; name.textContent = h.name;
      name.title = "Show in the graph (recentre)";
      name.onclick = () => load(hitTarget(h));
      const kind = document.createElement("span"); kind.className = "hkind"; kind.textContent = h.kind;
      const where = document.createElement("button");
      where.type = "button"; where.className = "link hwhere";
      where.textContent = `${(h.path || "").split(/[\\/]/).pop()}:${h.line || 1}`;
      where.title = `Open ${h.path}:${h.line || 1} in the IDE`;
      where.onclick = () => openInIde(h.path, h.line);
      const add = document.createElement("button");
      add.type = "button"; add.className = "btn hadd"; add.textContent = "+";
      add.title = "Add to the current graph"; add.setAttribute("aria-label", `Add ${h.name} to the current graph`);
      add.onclick = () => addArtifact(hitTarget(h));
      li.append(dot, name, kind, where, add);
      ul.appendChild(li);
    });
    return ul;
  }
  function note(text, withDots) {
    const n = document.createElement("div"); n.className = "note";
    n.appendChild(document.createTextNode(text));
    if (withDots) n.appendChild(dots());
    return n;
  }
  function renderReply(msg, reply, pending) {
    msg.querySelectorAll(":scope > :not(.who)").forEach((el) => el.remove());
    if (reply.answer) { const a = document.createElement("div"); a.className = "answer"; richText(a, reply.answer); msg.appendChild(a); }
    if (reply.hits && reply.hits.length) msg.appendChild(hitList(reply.hits));
    else if (reply.search_hits && reply.search_hits.length) {
      msg.appendChild(note(`${reply.llm || "The model"} found no matching artifact. Search results:`));
      msg.appendChild(hitList(reply.search_hits, true));
    } else if (!pending) msg.appendChild(note("No artifact found. Is the repository vectorized? (Settings › AI search)"));
    if (pending) msg.appendChild(note(pending, true));
    if (reply.warning) msg.appendChild(note(reply.warning));
    scrollChat();
  }

  // The chat box also opens artifacts directly: a file path (src/app.py:42,
  // relative to the repository or absolute inside it) or a symbol name is
  // shown in the graph; anything else is a natural-language search.
  const SYMBOL_RE = /^[A-Za-z_$][\w$]*(?:(?:\.|::|#)[A-Za-z_$][\w$]*)*$/;
  // Extensions that make "name.ext" a file rather than a dotted symbol (Class.method).
  const FILE_EXT_RE = /\.(py|pyi|ipynb|js|jsx|mjs|cjs|ts|tsx|java|kt|kts|cs|go|rs|rb|php|swift|scala|dart|c|cc|cpp|cxx|h|hh|hpp|m|mm|ex|exs|erl|hs|ml|lua|pl|r|jl|el|clj|groovy|sql|xml|json|ya?ml|toml|md|html|css|scss|vue|svelte|sh|tf|hcl|proto)$/i;
  function looksLikePath(s) {
    return /[\\/]/.test(s) || /^[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,7}$/.test(s);
  }
  function surelyPath(s) { return /[\\/]/.test(s) || FILE_EXT_RE.test(s); }
  function directReply(msg, noteText, hits) {
    msg.querySelectorAll(":scope > :not(.who)").forEach((el) => el.remove());
    if (noteText) msg.appendChild(note(noteText));
    if (hits && hits.length) msg.appendChild(hitList(hits));
    scrollChat();
  }
  async function openDirect(text, msg) {
    const raw = text.trim();
    const quoted = /^`[^`\s]+`$/.test(raw);
    if (/\s/.test(raw) && !quoted) return null; // a sentence: search
    const token = raw.replace(/^`|`$/g, "");
    const m = token.match(/^(.*?)(?::(\d+))?(?::\d+)?$/);
    const loc = m[1];
    const line = m[2] ? parseInt(m[2], 10) : undefined;
    if (loc && looksLikePath(loc)) {
      const target = pathInRepo(loc);
      if (target.error) { directReply(msg, target.error); return "That path is outside the repository."; }
      try {
        const art = await api("/api/locate", { path: target.path, line });
        load({ path: target.path, line });
        directReply(msg, "Shown in the graph:", [art]);
        return `Shown ${art.name || target.path} in the graph.`;
      } catch (e) {
        if (surelyPath(loc) || !SYMBOL_RE.test(token)) {
          directReply(msg, `${loc} is not indexed: no artifact there. (${e.message})`);
          return "No artifact at that path.";
        }
      }
    }
    if (!SYMBOL_RE.test(token)) return null;
    const names = [token];
    const last = token.split(/\.|::|#/).pop();
    if (last !== token) names.push(last);
    for (const name of names) {
      let matches;
      try { matches = (await api("/api/resolve", { symbol: name })).matches || []; } catch (_) { continue; }
      if (!matches.length) continue;
      if (matches.length === 1) {
        load(matches[0]);
        directReply(msg, "Shown in the graph:", matches);
        return `Shown ${name} in the graph.`;
      }
      directReply(msg, `${matches.length} artifacts named "${name}": pick one.`, matches);
      return `${matches.length} artifacts named ${name}.`;
    }
    return null; // not a known symbol: search
  }

  async function sendChat(text) {
    if (chatBusy || !text) return;
    chatBusy = true;
    chatText.disabled = true;
    const userMsg = addMessage("user");
    const bubble = document.createElement("div"); bubble.className = "bubble"; bubble.textContent = text;
    userMsg.appendChild(bubble);
    chatHistory.push({ role: "user", content: text });
    const msg = addMessage("bot");
    msg.appendChild(dots());
    if (!aiStatus) await refreshAiStatus();
    let summary = "";
    try {
      // 0. A path or a symbol: shown directly.
      const direct = await openDirect(text, msg);
      if (direct) { summary = direct; return; }
      // 1. Instant: the hybrid search.
      const found = await api("/api/search", { q: text, limit: 5 });
      const withLlm = llmReady() && found.hits.length;
      renderReply(msg, { hits: found.hits, warning: found.warning }, withLlm ? `Refining with ${aiStatus.config.CHAT_LLM_MODEL}` : "");
      summary = found.hits.length ? "Results: " + found.hits.map((h) => h.name).join(", ") : "No result.";
      // 2. Slower: the LLM re-ranks and may answer.
      if (withLlm) {
        const reply = await post("/api/chat", { messages: chatHistory.slice(-12) });
        renderReply(msg, reply, "");
        summary = reply.answer || (reply.hits.length ? "Results: " + reply.hits.map((h) => h.name).join(", ") : "No matching artifact.");
      }
    } catch (e) {
      msg.classList.add("error");
      renderReply(msg, { hits: [], answer: `Error: ${e.message}` }, "");
      summary = "";
    } finally {
      if (summary) chatHistory.push({ role: "assistant", content: summary });
      chatBusy = false;
      chatText.disabled = false;
      autoGrow();
      chatText.focus();
    }
  }

  // Typing filters the graph (as the former "Filter nodes" field did); Enter
  // then opens the path / symbol or runs the search, and the emptied field
  // clears the filter.
  function filterFromChat() {
    state.filter = chatText.value.trim().replace(/^`|`$/g, "").replace(/(:\d+){1,2}$/, "");
    applyFilter();
  }
  chatText.addEventListener("input", () => { autoGrow(); filterFromChat(); });
  chatText.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("chat-form").requestSubmit(); }
  });
  $("chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = chatText.value.trim();
    if (!text) return;
    chatText.value = "";
    autoGrow();
    filterFromChat();
    sendChat(text);
  });
  $("chat-toggle").addEventListener("click", () => {
    const collapsed = chatEl.classList.toggle("collapsed");
    $("chat-toggle").setAttribute("aria-expanded", collapsed ? "false" : "true");
    $("chat-toggle").title = collapsed ? "Expand" : "Collapse";
  });
  $("chat-clear").addEventListener("click", () => {
    if (chatBusy) return;
    chatHistory.length = 0;
    chatLog.innerHTML = "";
    chatEl.classList.add("chat-idle");
    chatEl.classList.remove("collapsed");
    chatText.focus();
  });
  // The chat stays left of the side panel.
  // The legend: top right, just left of the side panel when it is open (CSS).
  const legend = $("legend");
  $("legend-toggle").addEventListener("click", (e) => {
    e.stopPropagation();
    const open = legend.classList.toggle("open");
    $("legend-toggle").setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.addEventListener("click", (e) => {
    if (legend.classList.contains("open") && !legend.contains(e.target)) {
      legend.classList.remove("open");
      $("legend-toggle").setAttribute("aria-expanded", "false");
    }
  });
  // "panel-open": something is shown in the right column (panel or changes).
  const sideOpen = () => document.body.classList.toggle("panel-open", !panel.hidden || !$("changes").hidden);
  const sideObserver = new MutationObserver(sideOpen);
  sideObserver.observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  sideObserver.observe($("changes"), { attributes: true, attributeFilter: ["hidden"] });
  sideOpen();
  refreshAiStatus();

  // ── PNG export ──────────────────────────────────────────────────────────
  function exportPng() {
    const el = svg.node();
    const clone = el.cloneNode(true);
    const cs = getComputedStyle(document.documentElement);
    // Inline the CSS variables so the standalone SVG renders with colours.
    let src = new XMLSerializer().serializeToString(clone);
    src = src.replace(/var\(--([\w-]+)\)/g, (_, v) => cs.getPropertyValue("--" + v).trim() || "#888");
    const style = `<style>.edge{fill:none;stroke:${cs.getPropertyValue("--edge")};stroke-width:1px}.node text{font:100 9px sans-serif;fill:${cs.getPropertyValue("--text")}}</style>`;
    src = src.replace(/(<svg[^>]*>)/, `$1${style}`);
    const img = new Image();
    img.onload = () => {
      const c = document.createElement("canvas");
      c.width = el.clientWidth * 2; c.height = el.clientHeight * 2;
      const g = c.getContext("2d");
      g.scale(2, 2);
      g.fillStyle = cs.getPropertyValue("--bg"); g.fillRect(0, 0, c.width, c.height);
      g.drawImage(img, 0, 0);
      const a = document.createElement("a");
      a.download = `${(state.center && state.center.label) || "cviz-graph"}.png`;
      a.href = c.toDataURL("image/png");
      a.click();
    };
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(src);
  }

  // ── Live updates (SSE) ──────────────────────────────────────────────────
  let source = null;
  let live = false;
  let reloadTimer = null;
  function updateLive(text) {
    const btn = $("live");
    btn.classList.toggle("on", live && !state.stale);
    btn.classList.toggle("stale", state.stale);
    btn.classList.toggle("busy", !!embedBadge);
    btn.title = embedBadge ? "Vectorizing the code for the natural-language search" : "Live updates";
    $("live-text").textContent = embedBadge || text || (state.stale ? "graph updated · reload" : live ? "live" : "offline");
  }
  function displayedFiles() {
    const set = new Set();
    state.nodes.forEach((n) => { if (n.indexed_path) set.add(n.indexed_path); if (n.path) set.add(n.path); });
    return set;
  }
  function scheduleReload() {
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(() => { reloadView({ noHistory: true }); initChanges(); }, 400);
  }
  function onGraphEvent(ev) {
    if (!state.center) return;
    if (state.mode === "changes") {
      // Any edit can add or remove a change: the whole view is recomputed.
      sourceCache.clear();
      scheduleReload();
      return;
    }
    const shown = displayedFiles();
    const touched = (ev.indexed_files || []).concat(ev.files || []);
    const repo = ev.indexed_repo || ev.repo;
    const hit = touched.some((f) => shown.has(f)) ||
      (ev.type === "index:done" && repo && [...shown].some((p) => p.startsWith(repo.replace(/\/+$/, "") + "/")));
    if (hit) {
      sourceCache.clear();
      scheduleReload();
      toast("Graph updated");
    } else {
      state.stale = true;
      updateLive(`updated ${new Date().toLocaleTimeString()}`);
    }
  }
  function connectEvents() {
    if (source) source.close();
    if (!window.EventSource) return;
    const url = new URL("/api/events", location.origin);
    if (settings.apiKey) url.searchParams.set("api_key", settings.apiKey);
    source = new EventSource(url);
    source.addEventListener("open", () => { live = true; updateLive(); });
    source.addEventListener("error", () => { live = false; updateLive(); });
    source.addEventListener("graph:changed", (e) => onGraphEvent(JSON.parse(e.data)));
    source.addEventListener("index:done", (e) => onGraphEvent(JSON.parse(e.data)));
    source.addEventListener("embed:progress", (e) => onEmbedEvent(JSON.parse(e.data)));
    source.addEventListener("embed:done", (e) => onEmbedEvent(JSON.parse(e.data)));
    source.addEventListener("focus", (e) => {
      const ev = JSON.parse(e.data);
      if (!ev.path) return;
      load({ path: ev.path, line: ev.line || undefined });
      try { window.focus(); } catch (_) { /* browsers may refuse */ }
    });
  }
  $("live").addEventListener("click", () => {
    if (state.center) { state.stale = false; updateLive(); sourceCache.clear(); reloadView({ noHistory: true }); }
  });

  // ── One tab only: hand the request to an already-open graph tab ─────────
  const channel = window.BroadcastChannel ? new BroadcastChannel("cviz-graph") : null;
  const tabId = Math.random().toString(36).slice(2);
  let claimed = false;
  if (channel) {
    channel.onmessage = (e) => {
      const m = e.data || {};
      if (m.type === "open" && m.from !== tabId && state.center) {
        channel.postMessage({ type: "taken", to: m.from });
        load(m.target);
      } else if (m.type === "taken" && m.to === tabId) {
        claimed = true;
      }
    };
  }
  function handOver(target) {
    return new Promise((resolve) => {
      if (!channel) return resolve(false);
      channel.postMessage({ type: "open", from: tabId, target });
      setTimeout(() => resolve(claimed), 250);
    });
  }

  // ── Symbol resolution / start-up ────────────────────────────────────────
  // /graph?symbol=foo[&file=bar.py]: one match is shown, homonyms are listed
  // in the chat to pick from.
  async function resolveSymbol(symbol, file) {
    try {
      const { matches } = await api("/api/resolve", { symbol, file });
      if (matches.length === 1) { load(matches[0]); return; }
      const msg = addMessage("bot");
      msg.appendChild(note(`${matches.length} artifacts named "${symbol}": pick one.`));
      msg.appendChild(hitList(matches));
      $("empty").hidden = false;
    } catch (e) { setStatus(e.message, true); $("empty").hidden = false; }
  }

  // ── Entry points: where to start reading the repository ────────────────
  const ENTRIES_COLLAPSED_KEY = "cviz.graph.entriesCollapsed";
  let entryPoints = [];
  async function loadEntryPoints() {
    try { entryPoints = (await api("/api/entry-points", { limit: 6 })).entry_points || []; }
    catch (_) { entryPoints = []; }
    const list = $("entries-list");
    list.innerHTML = "";
    entryPoints.forEach((e) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.id = e.id;
      b.title = `${e.path}:${e.line || 1}: calls ${e.calls} artifact${e.calls === 1 ? "" : "s"}, called by none`;
      const name = document.createElement("span"); name.className = "ename"; name.textContent = e.label || e.name;
      const file = document.createElement("span"); file.className = "efile";
      file.textContent = `${(e.path || "").split(/[\\/]/).pop()}:${e.line || 1}`;
      b.append(name, file);
      b.onclick = () => load(e);
      li.appendChild(b);
      list.appendChild(li);
    });
    $("entries").hidden = !entryPoints.length || !$("changes").hidden;
    markEntryPoint(state.center && state.center.id);
    return entryPoints;
  }
  function markEntryPoint(id) {
    document.querySelectorAll("#entries-list button").forEach((b) => b.classList.toggle("active", !!id && b.dataset.id === id));
  }
  $("entries-toggle").addEventListener("click", () => {
    const collapsed = $("entries").classList.toggle("collapsed");
    $("entries-toggle").setAttribute("aria-expanded", collapsed ? "false" : "true");
    try { localStorage.setItem(ENTRIES_COLLAPSED_KEY, collapsed ? "1" : ""); } catch (_) { /* no storage */ }
  });
  try {
    if (localStorage.getItem(ENTRIES_COLLAPSED_KEY)) {
      $("entries").classList.add("collapsed");
      $("entries-toggle").setAttribute("aria-expanded", "false");
    }
  } catch (_) { /* no storage */ }

  function targetFromParams(p) {
    const path = p.get("path");
    if (!path) return null;
    const line = parseInt(p.get("line") || "", 10);
    return { path, line: isNaN(line) ? undefined : line, name: p.get("name") || undefined, kind: p.get("kind") || undefined };
  }

  // The repository this server serves (one database per repository).
  function showRepo(repo) {
    const el = $("repo-label");
    if (!repo || !repo.name) { el.hidden = true; return; }
    $("repo-name").textContent = repo.name;
    el.dataset.path = repo.path + (repo.watched ? " (watched: live updates)" : " (not watched: start with --watch for live updates)");
    el.hidden = false;
    updateSourcesBadge({
      count: (repo.companions || []).length, pending: !!repo.companions_pending, running: false,
    });

    chatText.placeholder = "Filter the graph as you type  ( / )\nEnter: a path (src/app.py:42), a symbol or a question";
    chatText.title = `Typing filters the graph. Enter opens a file path of ${repo.name} (relative or absolute, `
      + "optional :line) or a symbol, or searches the code for a question.";
  }

  // A path typed on the landing page: relative to the served repository, and
  // refused (with the reason) when it is outside it and its companion sources.
  function pathInRepo(raw) {
    const repo = serverConfig.repo;
    if (!repo || !repo.path || !raw) return { path: raw };
    const root = repo.path.replace(/[\\/]+$/, "");
    const isAbs = /^([A-Za-z]:)?[\\/]/.test(raw);
    const path = isAbs ? raw : `${root}/${raw.replace(/^\.?[\\/]/, "")}`;
    const norm = (p) => p.replace(/\\/g, "/").toLowerCase();
    const under = (r) => norm(path) === norm(r) || norm(path).startsWith(norm(r).replace(/\/+$/, "") + "/");
    if (isAbs && (repo.companions || []).some((c) => under(c.path))) return { path };
    if (!under(root)) {
      return { error: `${raw} is not in ${repo.name} (${root}). This page shows ${repo.name} and its companion sources only (click its name to add one), `
        + `open the other repository with its own server (cd <repo> && cviz graph --watch .).` };
    }
    return { path };
  }

  // ── Companion sources: other repositories / folders indexed with this one ──
  // The count sits next to the repository name; the dialog lists them, adds,
  // removes, and re-indexes (the list only changes what the next index reads).
  const sourcesDlg = $("sources");
  let sourcesTimer = null;
  let sourcesWasRunning = false;
  const FOLDER_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16" fill="none" '
    + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

  // The repository name opens the sources dialog; "(+N)" counts the
  // companion sources (orange: changes not re-indexed yet; pulsing: re-indexing).
  function updateSourcesBadge(st) {
    const n = st.count || 0;
    const badge = $("sources-count");
    badge.textContent = st.running ? "(⟳)" : (n || st.pending ? `(+${n})` : "");
    badge.classList.toggle("pending", !!st.pending && !st.running);
    badge.classList.toggle("busy", !!st.running);
    const el = $("repo-label");
    el.title = `${el.dataset.path || ""}\n`
      + (st.running ? "Re-indexing with the companion sources…"
        : (n ? `${n} companion source${n > 1 ? "s" : ""} indexed with this repository` : "No companion source")
          + (st.pending ? " · changes not in the graph yet (re-index)" : ""))
      + "\nClick to add or remove repositories and folders indexed with this one";
  }

  function companionOf(path) {
    const p = (path || "").replace(/\\/g, "/");
    return ((serverConfig.repo && serverConfig.repo.companions) || [])
      .find((c) => p.startsWith(c.path.replace(/\/+$/, "") + "/"));
  }

  function srcItem(icon, name, meta, state, stateClass, action) {
    const li = document.createElement("li");
    li.innerHTML = icon;
    const text = document.createElement("span");
    text.className = "src-text";
    const nm = document.createElement("span"); nm.className = "src-name"; nm.textContent = name;
    const mt = document.createElement("span"); mt.className = "src-meta mono"; mt.textContent = meta;
    text.append(nm, mt);
    li.appendChild(text);
    if (state) {
      const st = document.createElement("span"); st.className = `src-state ${stateClass || ""}`; st.textContent = state;
      li.appendChild(st);
    }
    if (action) li.appendChild(action);
    return li;
  }

  function renderSources(data) {
    const list = $("src-list");
    list.innerHTML = "";
    $("src-main-name").textContent = data.main.name;
    const files = (n) => (n == null ? "" : ` · ${n} file${n === 1 ? "" : "s"}`);
    list.appendChild(srcItem(FOLDER_ICON, data.main.name, `${data.main.files != null ? files(data.main.files).slice(3) + " · " : ""}${data.main.path}`, "main", ""));
    data.sources.forEach((src) => {
      const rm = document.createElement("button");
      rm.type = "button"; rm.className = "btn"; rm.textContent = "Remove";
      rm.title = `Remove ${src.name} from the sources (its code leaves the graph at the next re-index)`;
      rm.disabled = data.reindex.running;
      rm.onclick = () => changeSource("remove", src.path);
      const [state, cls] = !src.exists ? ["not found", "bad"] : src.indexed ? ["indexed", "ok"] : ["not indexed yet", "todo"];
      list.appendChild(srcItem(FOLDER_ICON, src.name, `${src.display_path} · ${src.kind}${files(src.files)}`, state, cls, rm));
    });
    (data.removed_pending || []).forEach((path) => {
      const li = srcItem(FOLDER_ICON, path.split("/").pop(), path, "removed, still in the graph", "todo");
      li.classList.add("removed");
      list.appendChild(li);
    });
    if (!data.sources.length && !(data.removed_pending || []).length) {
      const li = document.createElement("li");
      li.className = "src-empty";
      li.textContent = "No companion source yet. Add the repositories whose packages this one uses: "
        + "their classes become real nodes instead of external types.";
      list.appendChild(li);
    }

    const run = data.reindex;
    $("src-pending").hidden = !data.pending || run.running || !data.can_reindex;
    $("src-pending-text").textContent = "The graph does not include these changes yet: re-index to apply them "
      + "(the graph is rebuilt; it can take a few minutes).";
    $("src-progress").hidden = !run.running;
    if (run.running) {
      $("src-progress-text").textContent = run.total
        ? `Re-indexing… ${run.done}/${run.total} files${run.message ? " · " + run.message : ""}`
        : "Re-indexing… (the graph is incomplete until it ends)";
      $("src-progress-bar").max = run.total || 1;
      $("src-progress-bar").value = run.done || 0;
    }
    $("src-add-btn").disabled = run.running;
    if (run.error && !run.running) showSourceError(`Re-index failed: ${run.error}`);

    serverConfig.repo = Object.assign(serverConfig.repo || {}, {
      companions: data.sources.map((x) => ({ name: x.name, path: x.path, indexed: x.indexed })),
      companions_pending: data.pending,
    });
    updateSourcesBadge({ count: data.sources.length, pending: data.pending, running: run.running });

    clearTimeout(sourcesTimer);
    if (run.running) sourcesTimer = setTimeout(refreshSources, 1000);
    else if (sourcesWasRunning && !run.error) toast("Graph re-indexed with its companion sources");
    sourcesWasRunning = run.running;
  }

  function showSourceError(msg) {
    $("src-error").textContent = msg || "";
    $("src-error").hidden = !msg;
  }

  async function refreshSources() {
    try { renderSources(await api("/api/sources")); }
    catch (e) { showSourceError(e.message); }
  }

  async function changeSource(action, path) {
    showSourceError("");
    try {
      renderSources(await post("/api/sources", { action, path }));
      if (action === "add") { $("src-path").value = ""; loadSourceSuggestions(); }
      else loadSourceSuggestions();
      return true;
    } catch (e) { showSourceError(e.message); return false; }
  }

  async function loadSourceSuggestions() {
    try {
      const { suggestions } = await api("/api/sources/suggestions");
      const box = $("src-suggest-list");
      box.innerHTML = "";
      suggestions.forEach((sg) => {
        const chip = document.createElement("button");
        chip.type = "button"; chip.className = "src-chip";
        chip.textContent = sg.name;
        if (sg.provides && sg.provides.length) {
          const sm = document.createElement("small");
          sm.textContent = ` · provides ${sg.provides.slice(0, 2).join(", ")}${sg.provides.length > 2 ? "…" : ""}`;
          chip.appendChild(sm);
        }
        chip.title = `Add ${sg.path}`;
        chip.onclick = () => changeSource("add", sg.path);
        box.appendChild(chip);
      });
      $("src-suggest").hidden = !suggestions.length;
    } catch (_) { $("src-suggest").hidden = true; }
  }

  $("repo-label").addEventListener("click", () => {
    showSourceError("");
    sourcesDlg.showModal();
    refreshSources();
    loadSourceSuggestions();
  });
  $("src-close").addEventListener("click", () => sourcesDlg.close());
  $("src-add").addEventListener("submit", (e) => {
    e.preventDefault();
    const path = $("src-path").value.trim();
    if (path) changeSource("add", path);
  });
  $("src-reindex").addEventListener("click", async () => {
    showSourceError("");
    try { renderSources(await post("/api/sources/reindex", {})); }
    catch (e) { showSourceError(e.message); }
  });

  // ── Changes: what the current state changes against a target branch ────
  // The server diffs the working tree with the point where the branch left
  // the target (like a pull request) and returns the entities added or
  // modified, their callers, and the interface methods they implement.
  let changesInfo = null;
  let changesSelect = null;  // node id to select once the view is loaded

  function reloadView(opts) {
    opts = opts || {};
    if (state.mode === "changes") loadChanges(state.changesTarget, { keep: true, noHistory: opts.noHistory, replace: true });
    else if (state.center) load(targetFromNode(state.center), { keepPositions: true, keepSelection: true, noHistory: opts.noHistory, replace: opts.replace });
  }

  function setChangesMode(on) {
    state.mode = on ? "changes" : "graph";
    document.body.classList.toggle("changes-mode", on);
    $("ch-show").hidden = on || $("changes").hidden || !state.changesTarget;
  }

  // The refresh and close icons only make sense once a branch is picked.
  function updateCompare() {
    const picked = !!$("ch-target").value;
    $("ch-target").classList.toggle("unpicked", !picked);
    $("ch-fetch").hidden = !picked;
    $("ch-close").hidden = !picked;
  }

  function relTime(seconds) {
    if (!seconds) return "never";
    const d = Date.now() / 1000 - seconds;
    if (d < 90) return "just now";
    if (d < 5400) return `${Math.round(d / 60)} min ago`;
    if (d < 129600) return `${Math.round(d / 3600)} h ago`;
    return `${Math.round(d / 86400)} days ago`;
  }

  function fillTargets(info) {
    changesInfo = info;
    const sel = $("ch-target");
    sel.innerHTML = "";
    // Nothing picked by default: comparing is a choice.
    const names = new Set(info.branches.map((b) => b.name));
    const wanted = names.has(state.changesTarget) ? state.changesTarget : "";
    const none = document.createElement("option");
    // Empty: until a branch is picked, the field is underlined (graph.css).
    none.value = ""; none.textContent = "";
    sel.appendChild(none);
    [["Local branches", false], ["Remote branches", true]].forEach(([label, remote]) => {
      const group = document.createElement("optgroup");
      group.label = label;
      info.branches.filter((b) => b.remote === remote).forEach((b) => {
        const o = document.createElement("option");
        o.value = b.name;
        o.textContent = b.name + (b.name === info.current ? "  (current)" : b.name === info.default ? "  (default)" : "");
        group.appendChild(o);
      });
      if (group.children.length) sel.appendChild(group);
    });
    sel.value = wanted;
    sel.title = wanted ? sel.title : "Compare the current state with a branch: what it adds, modifies or deletes, and who is impacted";
    $("ch-fetch").title = `Fetch the remote branches (git fetch) and compare again · last fetch: ${relTime(info.fetched_at)}`;
    updateCompare();
    return wanted;
  }

  async function initChanges() {
    let info;
    try { info = await api("/api/changes/branches"); } catch (_) { return false; }
    // Not a Git repository (or no git): no branch, nothing to compare.
    $("branch-label").hidden = !info.available || !info.current;
    $("compare").hidden = !info.available;
    if (!info.available) return false;
    $("branch-name").textContent = info.current === "HEAD" ? "(detached)" : info.current;
    $("branch-label").title = `Current branch: ${info.current}`;
    fillTargets(info);
    return true;
  }

  function openChanges(target, opts) {
    if (!target) return;
    $("changes").hidden = false;
    $("entries").hidden = true;
    $("ch-target").value = target;
    updateCompare();
    loadChanges(target, opts || {});
  }

  function closeChanges() {
    $("ch-target").value = "";
    state.changesTarget = null;
    updateCompare();
    $("changes").hidden = true;
    $("entries").hidden = !entryPoints.length;
    const wasOn = state.mode === "changes";
    setChangesMode(false);
    if (wasOn) {
      // The "Changes vs …" pseudo-center goes, whatever is shown next.
      state.center = null;
      document.title = "Code Visualizer";
      const back = state.lastTarget || entryPoints[0];
      if (back) load(back);
      else { state.nodes = []; state.links = []; render(); select(null); $("empty").hidden = false; }
    }
  }

  async function loadChanges(target, opts) {
    opts = opts || {};
    const seq = ++loadSeq;
    setStatus(`Comparing with ${target}…`);
    $("empty").hidden = true;
    try {
      const data = await api("/api/changes", { target, ...filterParams() });
      if (seq !== loadSeq) return;
      state.changesTarget = target;
      setChangesMode(true);
      state.stale = false;
      updateLive();
      setStatus(data.nodes.length ? "" : `No function or type changed since this branch left ${target}.`);
      data.center = { id: `Changes|${target}`, kind: "Changes", label: `Changes vs ${target}`, path: null, line: null };
      applyData(data, { keepPositions: opts.keep, keepSelection: opts.keep });
      $("ch-target").title = `Compared with ${data.base_short}, where this branch left ${target} (git merge-base), working tree included`;
      document.title = `Changes vs ${target} · CViz graph`;
      renderChangesList(data);
      if (changesSelect) { const n = state.byId.get(changesSelect); changesSelect = null; if (n) { select(n); focusNode(n); } }
      if (!opts.noHistory) {
        const url = `${location.pathname}?changes=${encodeURIComponent(target)}`;
        if (url !== location.pathname + location.search) {
          if (opts.replace) history.replaceState({ changes: target }, "", url);
          else history.pushState({ changes: target }, "", url);
        }
      }
      if (data.truncated) toast("Part of the changes or of their callers is hidden (+N on nodes).");
    } catch (e) {
      if (seq !== loadSeq) return;
      setStatus(e.message, true);
    }
  }

  function focusNode(n) {
    if (n.x == null) return;
    const box = freeBox();
    svg.transition().duration(350).call(zoom.translateTo, n.x, n.y, [box.left + box.width / 2, box.height / 2]);
  }

  const baseName = (p) => String(p || "").split(/[\\/]/).pop();

  function renderChangesList(data) {
    const sm = data.summary;
    const changedCount = sm.added + sm.modified + (sm.deleted || 0) + (sm.moved || 0) + (sm.renamed || 0);
    const parts = [[sm.added, "new"], [sm.deleted, "deleted"], [sm.moved, "moved"], [sm.renamed, "renamed"]]
      .filter(([k]) => k).map(([k, w]) => `${k} ${w}`);
    $("ch-summary").textContent = changedCount
      ? `${changedCount} entit${changedCount > 1 ? "ies" : "y"} changed${parts.length ? " (" + parts.join(", ") + ")" : ""}`
        + ` in ${sm.files} file${sm.files > 1 ? "s" : ""} · ${sm.impacted} impacted`
      : `${sm.files} file${sm.files === 1 ? "" : "s"} changed, no function or type among them`;
    const impactOf = new Map();
    data.links.forEach((l) => {
      if (l.type === "CONTAINS") return;
      const t = l.target.id || l.target;
      impactOf.set(t, (impactOf.get(t) || 0) + 1);
    });
    const byFile = new Map();
    data.nodes.filter((n) => n.change).forEach((n) => {
      if (!byFile.has(n.file)) byFile.set(n.file, []);
      byFile.get(n.file).push(n);
    });
    renderAlerts(data);
    const list = $("ch-list");
    list.innerHTML = "";
    const STATE = { added: "new file", deleted: "deleted", renamed: "renamed", modified: "" };
    data.files.forEach((f) => {
      const items = (byFile.get(f.rel) || []).sort((a, b) => (a.line || 0) - (b.line || 0));
      const head = document.createElement("li");
      head.className = "ch-file";
      const name = document.createElement("span"); name.className = "fname"; name.textContent = baseName(f.rel);
      head.title = f.old_path ? `${f.old_path} → ${f.path}` : f.path;
      const st = document.createElement("span"); st.className = "fstate";
      st.textContent = f.status === "deleted" ? "deleted"
        : !items.length ? (f.indexed ? (STATE[f.status] ? STATE[f.status] + " · " : "") + "no entity changed" : "not in the graph")
          : STATE[f.status];
      head.append(name, st);
      list.appendChild(head);
      items.forEach((n) => {
        const li = document.createElement("li");
        const b = document.createElement("button");
        b.type = "button"; b.dataset.id = n.id;
        b.title = `${n.kind} ${n.name}, ${changeText(n)}\n${n.path || n.old_path}:${n.line}`;
        const mark = document.createElement("span"); mark.className = `mark ${n.change}`; mark.textContent = CHANGE_MARK[n.change].trim();
        const nm = document.createElement("span"); nm.className = "ename"; nm.textContent = n.label;
        const imp = document.createElement("span"); imp.className = "impact";
        const k = impactOf.get(n.id) || 0;
        imp.textContent = k ? `← ${k}` : "";
        imp.title = k ? `${k} caller${k > 1 ? "s" : ""} or derived type${k > 1 ? "s" : ""} in the graph` : "";
        b.append(mark, nm);
        if (n.untested) {
          const u = document.createElement("span"); u.className = "untested"; u.textContent = "⚠";
          u.title = "No test reaches it (within 3 calls)";
          b.appendChild(u);
        }
        b.appendChild(imp);
        b.onclick = () => {
          if (state.mode !== "changes") { changesSelect = n.id; openChanges(state.changesTarget); return; }
          const node = state.byId.get(n.id);
          if (node) { select(node); focusNode(node); }
        };
        li.appendChild(b);
        list.appendChild(li);
      });
    });
    const notes = [];
    if (sm.hidden_changed) notes.push(`${sm.hidden_changed} more changed entities are not drawn.`);
    if (data.tests_found === false) notes.push("No test code found in this repository: changed functions are not checked against tests.");
    if (sm.lost_detected === false) notes.push("Deleted, moved and renamed entities are not detected here (no parser on this server).");
    if (sm.via_interfaces) notes.push("Callers through an interface or a base class are included: they may also reach other implementations.");
    notes.push("Impact is what the graph resolves: calls by reflection, by name or through dependency injection configuration may be missing.");
    $("ch-note").textContent = notes.join(" ");
  }

  // Alerts: what a reviewer should look at first. Each entity they name is a
  // button that selects its node.
  function pickNode(id) {
    const node = id && state.byId.get(id);
    if (node) { select(node); focusNode(node); }
  }
  function renderAlerts(data) {
    const box = $("ch-alerts");
    box.innerHTML = "";
    const ICON = { warning: "!", maybe: "?", info: "i" };
    const order = { warning: 0, maybe: 1, info: 2 };
    [...(data.alerts || [])].sort((a, b) => order[a.severity] - order[b.severity]).forEach((a) => {
      const li = document.createElement("li");
      li.className = a.severity;
      const icon = document.createElement("span"); icon.className = "aicon"; icon.textContent = ICON[a.severity] || "i";
      const text = document.createElement("div"); text.className = "atext";
      const title = document.createElement("div"); title.textContent = a.title;
      text.appendChild(title);
      if (a.refs) {
        a.refs.forEach((r) => {
          const b = document.createElement("button");
          b.type = "button"; b.className = "aref mono";
          b.textContent = `${baseName(r.rel)}:${r.line}  ${r.text}`;
          b.title = `${r.path}:${r.line}\nClick: select the code there; double-click: open it in the IDE`;
          b.onclick = () => pickNode(r.node);
          b.ondblclick = () => openInIde(r.path, r.line);
          text.appendChild(b);
        });
      } else if (a.type === "untested" || a.type === "new_external") {
        const d = document.createElement("div"); d.className = "adetail";
        if (a.type === "new_external") d.append("called by ");
        a.nodes.forEach((id) => {
          const n = data.nodes.find((x) => x.id === id);
          if (!n) return;
          const c = document.createElement("button");
          c.type = "button"; c.className = "achip"; c.textContent = n.label;
          c.onclick = () => pickNode(id);
          d.appendChild(c);
        });
        text.appendChild(d);
      } else if (a.detail) {
        const d = document.createElement("div"); d.className = "adetail mono"; d.textContent = baseName(a.detail);
        d.title = a.detail;
        text.appendChild(d);
      }
      li.append(icon, text);
      box.appendChild(li);
    });
  }

  $("ch-close").addEventListener("click", closeChanges);
  $("ch-show").addEventListener("click", () => openChanges(state.changesTarget));
  $("ch-target").addEventListener("change", (e) => {
    if (e.target.value) openChanges(e.target.value);
    else closeChanges();
  });
  $("ch-toggle").addEventListener("click", () => {
    const collapsed = $("changes").classList.toggle("collapsed");
    $("ch-toggle").setAttribute("aria-expanded", collapsed ? "false" : "true");
  });
  $("ch-fetch").addEventListener("click", async () => {
    const btn = $("ch-fetch");
    btn.disabled = true;
    setStatus("Fetching the remote branches…");
    try {
      fillTargets(await post("/api/changes/fetch", {}));
      setStatus("");
      toast("Remote branches fetched");
      if (state.mode === "changes") loadChanges($("ch-target").value, { keep: true, replace: true });
    } catch (e) { setStatus(e.message, true); }
    finally { btn.disabled = false; }
  });

  // Inside the desktop app, the tab bar has the logo and the About window;
  // a theme picked in another tab applies here too.
  if (window.parent !== window) {
    document.body.classList.add("embedded");
    window.addEventListener("message", (e) => {
      const data = e.data || {};
      if (e.source === window.parent && data.type === "cviz:theme" && data.theme !== settings.theme) {
        settings.theme = data.theme;
        applyTheme(settings.theme);
        saveSettings(settings);
      }
    });
  }

  // ── About: the logo before "live" opens the version and the credits ────
  $("about-btn").addEventListener("click", () => {
    $("about-version").textContent = serverConfig.version || "unknown";
    $("about").showModal();
  });
  $("about-close").addEventListener("click", () => $("about").close());
  $("about").addEventListener("click", (e) => { if (e.target === $("about")) $("about").close(); });  // click outside

  async function init() {
    try { serverConfig = Object.assign(serverConfig, await api("/api/graph-config")); } catch (_) { /* older server */ }
    showRepo(serverConfig.repo);
    connectEvents();
    updateLive();
    const target = targetFromParams(qs);
    const changesAvailable = initChanges();
    if (qs.get("changes") && await changesAvailable) {
      openChanges(qs.get("changes"), { replace: true });
      loadEntryPoints();
      return;
    }
    if (target) {
      if (await handOver(target)) {
        setStatus("Shown in the graph tab that was already open. You can close this tab.");
        setTimeout(() => { try { window.close(); } catch (_) { /* not script-opened */ } }, 300);
        return;
      }
      load(target, { replace: true });
    } else if (qs.get("symbol")) {
      resolveSymbol(qs.get("symbol"), qs.get("file") || undefined);
    }
    const entries = await loadEntryPoints();
    if (!target && !qs.get("symbol") && !state.center && state.mode === "graph") {
      if (entries.length) load(entries[0], { replace: true });
      else $("empty").hidden = false;
    }
  }

  // ── Resizable side panel: drag its left edge, double-click it to reset ──
  const PANEL_WIDTH_KEY = "cviz.graph.panelWidth";
  const EDGE = 6;
  function setPanelWidth(px) {
    if (px == null) { document.body.style.removeProperty("--panel-w"); return null; }
    const w = Math.round(Math.max(280, Math.min(px, window.innerWidth - 240)));
    document.body.style.setProperty("--panel-w", w + "px");
    return w;
  }
  try { const saved = Number(localStorage.getItem(PANEL_WIDTH_KEY)); if (saved) setPanelWidth(saved); } catch (_) { /* no storage */ }
  // The handle is the left edge of the whole right column (the changes list
  // and the panel), not of one of them.
  const side = $("side");
  const onEdge = (e) => window.innerWidth > 760 && Math.abs(e.clientX - side.getBoundingClientRect().left) <= EDGE;
  side.addEventListener("pointermove", (e) => { if (!side.classList.contains("resizing")) side.classList.toggle("edge-hover", onEdge(e)); });
  side.addEventListener("pointerleave", () => { if (!side.classList.contains("resizing")) side.classList.remove("edge-hover"); });
  side.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !onEdge(e)) return;
    e.preventDefault();
    side.classList.add("resizing"); document.body.classList.add("resizing-panel");
    const right = side.getBoundingClientRect().right;
    let width = null;
    const move = (ev) => { width = setPanelWidth(right - ev.clientX); };
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up); document.removeEventListener("pointercancel", up);
      side.classList.remove("resizing", "edge-hover"); document.body.classList.remove("resizing-panel");
      if (width) {
        try { localStorage.setItem(PANEL_WIDTH_KEY, String(width)); } catch (_) { /* no storage */ }
        if (state.nodes.length) fit(true);  // keep the graph visible beside the wider/narrower column
      }
    };
    // On the document: the pointer leaves the column while it is dragged.
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", up);
  });
  side.addEventListener("dblclick", (e) => {
    if (!onEdge(e)) return;
    setPanelWidth(null);
    try { localStorage.removeItem(PANEL_WIDTH_KEY); } catch (_) { /* no storage */ }
    if (state.nodes.length) fit(true);
  });

  window.addEventListener("resize", () => { if (state.nodes.length && state.layout !== "force") fit(false); });
  init();
})();
