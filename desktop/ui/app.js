// The tab shell: one tab per repository, each an iframe on the graph page of
// that repository's `cviz graph` server (started on first view, see lib.rs).
(function () {
  "use strict";

  const tauri = window.__TAURI__;
  const invoke = tauri.core.invoke;
  const STORE_KEY = "cviz.desktop.tabs";
  const MAX_LOG = 300;
  const $ = (id) => document.getElementById(id);

  /** @type {{path: string, name: string, state: string, url?: string, error?: string, log: string[], view: HTMLElement, tab: HTMLElement}[]} */
  let tabs = [];
  let active = null;

  const baseName = (p) => p.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || p;

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
        frame.src = t.url;
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
    const log = document.createElement("pre");
    log.textContent = t.log.join("\n");
    box.append(h, what, log);
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
  }

  async function start(t) {
    t.state = "starting"; t.error = undefined; t.log = [];
    renderTab(t); renderView(t);
    try {
      const server = await invoke("open_repo", { path: t.path });
      t.url = server.url; t.state = "ready";
    } catch (e) {
      t.state = "error"; t.error = String(e);
    }
    if (tabs.includes(t)) { renderTab(t); renderView(t); }
  }

  function activate(t) {
    active = t;
    tabs.forEach((x) => { x.view.classList.toggle("active", x === t); renderTab(x); });
    $("home").hidden = !!t;
    if (t && t.state === "idle") start(t);
    if (t) t.tab.scrollIntoView({ block: "nearest", inline: "nearest" });
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

  async function pickRepository() {
    const path = await tauri.dialog.open({ directory: true, multiple: false, title: "Open a repository" });
    if (typeof path === "string" && path) addTab(path);
  }

  // The output of `cviz graph`, shown while the tab starts (or if it fails).
  tauri.event.listen("repo-log", (event) => {
    const { path, line } = event.payload;
    const t = tabs.find((x) => x.path === path);
    if (!t) return;
    t.log.push(line);
    if (t.log.length > MAX_LOG) t.log.splice(0, t.log.length - MAX_LOG);
    if (t.state !== "ready" && t === active) {
      const pre = t.view.querySelector("pre");
      if (pre) { pre.textContent = t.log.join("\n"); pre.scrollTop = pre.scrollHeight; }
    }
  });

  // IDE links from the graph pages (see openInIde in graph.js).
  window.addEventListener("message", (event) => {
    const data = event.data || {};
    if (data.type === "cviz:open-url" && typeof data.url === "string") {
      invoke("open_url", { url: data.url }).catch((e) => console.warn(e));
    }
  });

  // About: the logo of the tab bar (the graph pages hide theirs in the app).
  $("about-btn").addEventListener("click", async () => {
    try { $("about-version").textContent = await tauri.app.getVersion(); } catch (_) { /* unknown */ }
    $("about").showModal();
  });
  $("about-close").addEventListener("click", () => $("about").close());
  $("about").addEventListener("click", (e) => {
    const link = e.target.closest("a[href]");
    if (link) { e.preventDefault(); invoke("open_url", { url: link.href }).catch(() => {}); return; }
    if (e.target === $("about")) $("about").close();  // click outside
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
