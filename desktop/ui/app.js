// The tab shell: one tab per repository, each an iframe on the graph page of
// that repository's `cviz graph` server (started on first view, see lib.rs).
(function () {
  "use strict";

  const tauri = window.__TAURI__;
  const invoke = tauri.core.invoke;
  const STORE_KEY = "cviz.desktop.tabs";
  // The graph pages' settings (theme, IDE…): their own storage does not
  // outlive the app (a tab is an iframe from another origin), so the app keeps
  // them and hands them to every page it opens (graph.html).
  const PAGE_STORE_KEY = "cviz.desktop.pageStorage";
  const PAGE_SETTINGS_KEY = "cviz.graph.settings";
  const MAX_LOG = 300;
  const $ = (id) => document.getElementById(id);

  /** @type {{path: string, name: string, state: string, url?: string, error?: string, log: string[], view: HTMLElement, tab: HTMLElement}[]} */
  let tabs = [];
  let active = null;

  const baseName = (p) => p.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || p;

  let pageStore = {};
  try { pageStore = JSON.parse(localStorage.getItem(PAGE_STORE_KEY) || "{}") || {}; } catch (_) { pageStore = {}; }
  function pageTheme() {
    try { return (JSON.parse(pageStore[PAGE_SETTINGS_KEY] || "{}") || {}).theme || "system"; } catch (_) { return "system"; }
  }
  // "system" follows the OS; light / dark force it, as in the graph pages.
  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }
  applyTheme(pageTheme());

  function save() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({
        tabs: tabs.map((t) => t.path), active: active ? active.path : null,
      }));
    } catch (_) { /* no storage: tabs are not kept */ }
  }

  function renderTab(t) {
    t.tab.innerHTML = "";
    const state = document.createElement("span");
    state.className = `state ${t.state}`;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = t.name;
    const close = document.createElement("button");
    close.type = "button"; close.className = "close"; close.textContent = "×";
    close.title = "Close"; close.setAttribute("aria-label", `Close ${t.name}`);
    close.onclick = (e) => { e.stopPropagation(); closeTab(t); };
    t.tab.append(state, name, close);
    t.tab.title = t.path + ({ starting: " · starting…", error: " · could not start" }[t.state] || "");
    t.tab.classList.toggle("active", t === active);
    t.tab.setAttribute("aria-selected", String(t === active));
  }

  function renderView(t) {
    if (t.state === "ready") {
      if (!t.view.querySelector("iframe")) {
        t.view.innerHTML = "";
        const frame = document.createElement("iframe");
        // An artifact asked for by an IDE before the page was up (ide-open).
        const query = t.pendingFocus ? `?${focusQuery(t.pendingFocus)}` : "";
        t.pendingFocus = null;
        frame.src = `${t.url}${query}#cviz-storage=${encodeURIComponent(JSON.stringify(pageStore))}`;
        frame.title = `Dependency graph of ${t.name}`;
        t.view.appendChild(frame);
      }
      return;
    }
    t.view.innerHTML = "";
    const box = document.createElement("div");
    box.className = `starting${t.state === "error" ? " error" : ""}`;
    const h = document.createElement("h2");
    h.textContent = t.state === "error" ? `${t.name}: the graph server did not start` : `Opening ${t.name}…`;
    const what = document.createElement("p");
    what.className = "what";
    what.textContent = t.state === "error" ? t.error
      : "The first time, the repository is indexed: it can take a few minutes on a large one.";
    const progress = document.createElement("div");
    progress.className = "progress";
    progress.innerHTML = '<div class="phase"></div><div class="bar"><i></i></div><div class="numbers"></div>';
    const log = document.createElement("pre");
    log.textContent = t.log.join("\n");
    box.append(h, what, progress, log);
    if (t.state === "error") {
      const actions = document.createElement("div");
      actions.className = "actions";
      const retry = document.createElement("button");
      retry.type = "button"; retry.className = "btn primary"; retry.textContent = "Try again";
      retry.onclick = () => start(t);
      actions.appendChild(retry);
      box.appendChild(actions);
    }
    t.view.appendChild(box);
    log.scrollTop = log.scrollHeight;
    renderProgress(t);
  }

  // The "Indexing: <phase> [<done>/<total> (<pct>%)]" lines of cviz graph
  // (cli_helpers._index_progress_line): the phase, a bar for the counted
  // phases, the time spent and, once a counted phase is under way, the time left.
  const PROGRESS_LINE = /^Indexing: (.+?)(?: (\d+)\/(\d+) \((\d+)%\))?$/;
  const duration = (ms) => {
    const s = Math.max(0, Math.round(ms / 1000));
    return s >= 60 ? `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s` : `${s} s`;
  };
  function readProgress(t, line) {
    const m = PROGRESS_LINE.exec(line);
    if (!m) return false;
    const [, phase, done, total] = m;
    const now = Date.now();
    if (!t.progress || t.progress.phase !== phase) t.progress = { phase, since: now, firstDone: Number(done || 0) };
    t.progress.done = done === undefined ? null : Number(done);
    t.progress.total = total === undefined ? null : Number(total);
    return true;
  }
  function renderProgress(t) {
    const box = t.view.querySelector(".progress");
    if (!box) return;
    const p = t.progress;
    box.hidden = !p && t.state !== "starting";
    const elapsed = t.startedAt ? duration(Date.now() - t.startedAt) : "";
    if (!p) {
      box.querySelector(".phase").textContent = "Starting the graph server…";
      box.querySelector(".bar").hidden = true;
      box.querySelector(".numbers").textContent = elapsed;
      return;
    }
    const counted = p.total > 0 && p.done !== null;
    box.querySelector(".phase").textContent = p.phase.startsWith("done") ? `Indexed, ${p.phase}` : p.phase[0].toUpperCase() + p.phase.slice(1);
    box.querySelector(".bar").hidden = !counted;
    let numbers = elapsed;
    if (counted) {
      box.querySelector(".bar i").style.width = `${(100 * p.done / p.total).toFixed(1)}%`;
      numbers = `${p.done.toLocaleString()} / ${p.total.toLocaleString()} files · ${elapsed}`;
      const doneHere = p.done - p.firstDone, spent = Date.now() - p.since;
      if (doneHere > 0 && spent > 3000) numbers += ` · about ${duration(spent / doneHere * (p.total - p.done))} left for this step`;
    }
    box.querySelector(".numbers").textContent = numbers;
  }
  // The time spent moves on between two lines.
  setInterval(() => tabs.forEach((t) => { if (t.state === "starting") renderProgress(t); }), 1000);

  async function start(t) {
    t.state = "starting"; t.error = undefined; t.log = []; t.progress = null; t.startedAt = Date.now();
    renderTab(t); renderView(t);
    try {
      const server = await invoke("open_repo", { path: t.path });
      t.url = server.url; t.state = "ready";
    } catch (e) {
      t.state = "error"; t.error = String(e);
    }
    if (tabs.includes(t)) { renderTab(t); renderView(t); }
    updateSettingsButton();
  }

  // The gear opens the settings of the graph page shown (graph.js); they
  // apply to every tab (the app keeps them, see PAGE_STORE_KEY).
  function updateSettingsButton() {
    $("settings-btn").disabled = !(active && active.state === "ready");
  }

  function activate(t) {
    active = t;
    tabs.forEach((x) => { x.view.classList.toggle("active", x === t); renderTab(x); });
    $("home").hidden = !!t;
    if (t && t.state === "idle") start(t);
    if (t) t.tab.scrollIntoView({ block: "nearest", inline: "nearest" });
    updateSettingsButton();
    save();
  }

  function addTab(path, opts) {
    const existing = tabs.find((t) => t.path === path);
    if (existing) { activate(existing); return existing; }
    const t = { path, name: baseName(path), state: "idle", log: [] };
    t.tab = document.createElement("button");
    t.tab.type = "button"; t.tab.className = "tab"; t.tab.setAttribute("role", "tab");
    t.tab.onclick = () => activate(t);
    t.view = document.createElement("section");
    t.view.className = "view";
    $("tabs").appendChild(t.tab);
    $("views").appendChild(t.view);
    tabs.push(t);
    renderTab(t);
    if (!opts || opts.activate !== false) activate(t);
    else save();
    return t;
  }

  function closeTab(t) {
    const i = tabs.indexOf(t);
    if (i < 0) return;
    invoke("close_repo", { path: t.path }).catch(() => {});
    tabs.splice(i, 1);
    t.tab.remove(); t.view.remove();
    if (active === t) activate(tabs[i] || tabs[i - 1] || null);
    else save();
  }

  // ── From the IDE (lib.rs, ide_link): the tab of the file's repository,
  // opened if needed, centred on the artifact. ─────────────────────────
  const norm = (p) => String(p).replace(/\\/g, "/").replace(/\/+$/, "");
  const focusQuery = (f) => {
    const params = new URLSearchParams({ path: f.path });
    if (f.line) params.set("line", String(f.line));
    return params.toString();
  };
  /** The open tab whose folder holds *file* (the deepest one). */
  function tabFor(file) {
    const target = norm(file);
    let best = null;
    for (const t of tabs) {
      const root = norm(t.path);
      if ((target === root || target.startsWith(root + "/")) && (!best || root.length > norm(best.path).length)) best = t;
    }
    return best;
  }
  tauri.event.listen("ide-open", (event) => {
    const { path, line, repo } = event.payload || {};
    if (!path) return;
    const focus = { path, line: line || null };
    const t = tabFor(path) || addTab(repo || path.replace(/[\\/][^\\/]*$/, ""), { activate: false });
    const frame = t.view.querySelector("iframe");
    if (t.state === "ready" && frame) frame.contentWindow.postMessage({ type: "cviz:focus", ...focus }, "*");
    else t.pendingFocus = focus;  // the page will open on it (renderView)
    activate(t);
  });

  async function pickRepository() {
    const path = await tauri.dialog.open({ directory: true, multiple: false, title: "Open a repository" });
    if (typeof path === "string" && path) addTab(path);
  }

  // The output of `cviz graph`, shown while the tab starts (or if it fails).
  tauri.event.listen("repo-log", (event) => {
    const { path, line } = event.payload;
    const t = tabs.find((x) => x.path === path);
    if (!t) return;
    if (readProgress(t, line)) { if (t === active) renderProgress(t); return; }
    t.log.push(line);
    if (t.log.length > MAX_LOG) t.log.splice(0, t.log.length - MAX_LOG);
    if (t.state !== "ready" && t === active) {
      const pre = t.view.querySelector("pre");
      if (pre) { pre.textContent = t.log.join("\n"); pre.scrollTop = pre.scrollHeight; }
    }
  });

  // From the graph pages: IDE links (openInIde in graph.js) and their
  // settings (graph.html); a theme change reaches the other tabs too.
  window.addEventListener("message", (event) => {
    const data = event.data || {};
    if (data.type === "cviz:reveal") {
      // A page recentred by an IDE through its server (/api/focus): show it.
      const t = tabs.find((x) => { const f = x.view.querySelector("iframe"); return f && f.contentWindow === event.source; });
      if (t) activate(t);
      invoke("focus_window").catch(() => {});
    } else if (data.type === "cviz:open-url" && typeof data.url === "string") {
      invoke("open_url", { url: data.url }).catch((e) => console.warn(e));
    } else if (data.type === "cviz:storage" && typeof data.key === "string" && data.key.startsWith("cviz.graph.")) {
      const before = pageTheme();
      if (data.value === null) delete pageStore[data.key];
      else pageStore[data.key] = String(data.value);
      try { localStorage.setItem(PAGE_STORE_KEY, JSON.stringify(pageStore)); } catch (_) { /* not kept */ }
      const theme = pageTheme();
      if (theme !== before) {
        applyTheme(theme);
        document.querySelectorAll(".view iframe").forEach((frame) => {
          if (frame.contentWindow !== event.source) frame.contentWindow.postMessage({ type: "cviz:theme", theme }, "*");
        });
      }
    }
  });

  // About: the logo of the tab bar (the graph pages hide theirs in the app).
  $("about-btn").addEventListener("click", async () => {
    try { $("about-version").textContent = await tauri.app.getVersion(); } catch (_) { /* unknown */ }
    $("about").showModal();
    document.body.classList.add("dialog-open");
  });
  $("about").addEventListener("close", () => {
    document.body.classList.remove("dialog-open");
    $("about-btn").blur();  // the focus given back to the logo would stay drawn on it
  });
  $("about-close").addEventListener("click", () => $("about").close());
  $("about").addEventListener("click", (e) => {
    const link = e.target.closest("a[href]");
    if (link) { e.preventDefault(); invoke("open_url", { url: link.href }).catch(() => {}); return; }
    if (e.target === $("about")) $("about").close();  // click outside
  });

  $("settings-btn").addEventListener("click", () => {
    const frame = active && active.view.querySelector("iframe");
    if (frame) frame.contentWindow.postMessage({ type: "cviz:open-settings" }, "*");
  });

  $("add").addEventListener("click", pickRepository);
  $("home-open").addEventListener("click", pickRepository);
  document.addEventListener("keydown", (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && (e.key === "o" || e.key === "t")) { e.preventDefault(); pickRepository(); }
    else if (mod && /^[1-9]$/.test(e.key)) {
      const t = tabs[Number(e.key) - 1];
      if (t) { e.preventDefault(); activate(t); }
    } else if (e.ctrlKey && e.key === "Tab" && tabs.length) {
      e.preventDefault();
      const i = tabs.indexOf(active);
      activate(tabs[(i + (e.shiftKey ? tabs.length - 1 : 1)) % tabs.length]);
    }
  });

  // The tabs of the last session come back; only the active one starts now.
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch (_) { saved = null; }
  if (saved && Array.isArray(saved.tabs)) {
    saved.tabs.forEach((p) => addTab(p, { activate: false }));
    activate(tabs.find((t) => t.path === saved.active) || tabs[0] || null);
  } else {
    activate(null);
  }
})();
