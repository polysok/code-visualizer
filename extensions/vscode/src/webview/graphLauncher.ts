import * as fs from "fs";
import * as http from "http";
import * as https from "https";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";

const DEFAULT_SERVER = "http://localhost:9324";

/** `~/.code-visualizer/servers.json`: running `cviz graph` servers by repository root. */
function registryFile(): string {
  return process.env.CVIZ_SERVERS_FILE || path.join(os.homedir(), ".code-visualizer", "servers.json");
}

/** What only the user-level settings folder holds; a repository's index folder has none of these. */
const USER_DIR_MARKERS = ["global", "models", "servers.json", "events.jsonl", ".first_run_done"];

/** Repository of a file: nearest folder with `.code-visualizer/`, else the git root, else its folder. */
export function findRepoRoot(start: string): string {
  let dir = fs.existsSync(start) && fs.statSync(start).isFile() ? path.dirname(start) : start;
  const globalDir = path.join(os.homedir(), ".code-visualizer");
  const ancestors: string[] = [];
  for (let d = path.resolve(dir); ; d = path.dirname(d)) {
    ancestors.push(d);
    if (path.dirname(d) === d) break;
  }
  const legacyGlobal = path.join(os.homedir(), ".codegraphcontext");
  const local = ancestors.find((d) => [".code-visualizer", ".codegraphcontext" /* before the rename */].some((name) => {
    const c = path.join(d, name);
    return c !== globalDir && c !== legacyGlobal && fs.existsSync(c) && fs.statSync(c).isDirectory()
      && !USER_DIR_MARKERS.some((m) => fs.existsSync(path.join(c, m)));
  }));
  if (local) return local;
  return ancestors.find((d) => fs.existsSync(path.join(d, ".git"))) ?? path.resolve(dir);
}

/** URL of the running server whose repository contains `filePath` (server registry). */
export function registeredServer(filePath: string): string | undefined {
  let entries: Record<string, { url?: string; pid?: number }>;
  try {
    entries = JSON.parse(fs.readFileSync(registryFile(), "utf8"));
  } catch {
    return undefined;
  }
  const target = path.resolve(filePath).split(path.sep).join("/");
  let best: [string, string] | undefined;
  for (const [repo, entry] of Object.entries(entries)) {
    if (!entry?.url || !entry.pid) continue;
    try {
      process.kill(entry.pid, 0);
    } catch {
      continue; // dead process
    }
    if ((target === repo || target.startsWith(repo.replace(/\/+$/, "") + "/")) && (!best || repo.length > best[0].length)) {
      best = [repo, entry.url];
    }
  }
  return best?.[1];
}

/**
 * Opens the standalone CViz dependency-graph page (served by `cviz graph`) on a
 * file and line, instead of the in-editor webview.
 *
 * 1. POST /api/focus so an already-open graph tab recentres (no tab pile-up);
 * 2. otherwise open /graph?path=…&line=… in the default browser;
 * 3. if the server is not running, offer to start `cviz graph --watch` in a
 *    terminal, then retry.
 *
 * The page links back to the editor through `vscode://file/<path>:<line>`.
 */
export class GraphLauncher {
  private starting?: Promise<boolean>;
  private serverUrl = DEFAULT_SERVER;

  /** Explicit `cviz.graphServerUrl`, else the server of the file's repository, else the default. */
  private resolveServer(filePath: string): string {
    const explicit = vscode.workspace.getConfiguration("cviz").get<string>("graphServerUrl", "");
    const url = explicit || registeredServer(filePath) || DEFAULT_SERVER;
    return url.replace(/\/+$/, "");
  }

  pageUrl(filePath: string, line?: number): string {
    const params = new URLSearchParams({ path: filePath });
    if (line && line > 0) params.set("line", String(line));
    const ide = vscode.env.uriScheme === "cursor" ? "cursor" : "vscode";
    params.set("ide", ide);
    return `${this.serverUrl}/graph?${params.toString()}`;
  }

  async open(filePath: string, line?: number): Promise<void> {
    this.serverUrl = this.resolveServer(filePath);
    if (!(await this.alive())) {
      const started = await this.offerStart(filePath);
      if (!started) return;
    }
    const delivered = await this.focusExisting(filePath, line);
    if (delivered > 0) {
      vscode.window.setStatusBarMessage("CViz: graph tab recentred", 2500);
      return;
    }
    await vscode.env.openExternal(vscode.Uri.parse(this.pageUrl(filePath, line), true));
  }

  private async alive(): Promise<boolean> {
    try {
      const res = await this.request("GET", "/api/graph-config");
      return res.status < 500;
    } catch {
      return false;
    }
  }

  private async focusExisting(filePath: string, line?: number): Promise<number> {
    try {
      const res = await this.request("POST", "/api/focus", { path: filePath, line: line ?? null });
      if (res.status !== 200) return 0;
      return Number((JSON.parse(res.body) as { delivered?: number }).delivered ?? 0);
    } catch {
      return 0;
    }
  }

  private async offerStart(filePath: string): Promise<boolean> {
    if (this.starting) return this.starting;
    const url = new URL(this.serverUrl);
    const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    if (!local) {
      vscode.window.showErrorMessage(`CViz graph server not reachable at ${this.serverUrl}.`);
      return false;
    }
    const repoRoot = findRepoRoot(filePath);
    const choice = await vscode.window.showInformationMessage(
      `The CViz graph server of ${path.basename(repoRoot)} is not running.`,
      "Start it",
      "Cancel"
    );
    if (choice !== "Start it") return false;

    // One server per repository: it picks a free port and records itself in
    // the server registry, where its URL is found.
    const exe = vscode.workspace.getConfiguration("cviz").get<string>("executable", "cviz") || "cviz";
    const terminal = vscode.window.createTerminal({ name: `CViz graph (${path.basename(repoRoot)})`, cwd: repoRoot });
    terminal.sendText(`${exe} graph --no-browser --watch ${JSON.stringify(repoRoot)}`);
    const explicit = vscode.workspace.getConfiguration("cviz").get<string>("graphServerUrl", "");

    this.starting = (async () => {
      const deadline = Date.now() + 120000; // a first run indexes the repository
      while (Date.now() < deadline) {
        const found = explicit || registeredServer(filePath);
        if (found) {
          this.serverUrl = found.replace(/\/+$/, "");
          if (await this.alive()) return true;
        }
        await new Promise((r) => setTimeout(r, 500));
      }
      terminal.show();
      vscode.window.showErrorMessage("CViz graph server did not start; see the 'CViz graph' terminal.");
      return false;
    })();
    try {
      return await this.starting;
    } finally {
      this.starting = undefined;
    }
  }

  private request(method: "GET" | "POST", path: string, body?: unknown): Promise<{ status: number; body: string }> {
    const target = new URL(path, this.serverUrl + "/");
    const payload = body === undefined ? undefined : Buffer.from(JSON.stringify(body));
    const client = target.protocol === "https:" ? https : http;
    const headers: Record<string, string | number> = {};
    if (payload) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = payload.length;
    }
    const apiKey = process.env.CVIZ_API_KEY;
    if (apiKey) headers["X-API-Key"] = apiKey;
    return new Promise((resolve, reject) => {
      const req = client.request(target, { method, headers, timeout: 2000 }, (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c: Buffer) => chunks.push(c));
        res.on("end", () => resolve({ status: res.statusCode ?? 0, body: Buffer.concat(chunks).toString("utf8") }));
      });
      req.on("timeout", () => req.destroy(new Error("timeout")));
      req.on("error", reject);
      if (payload) req.write(payload);
      req.end();
    });
  }
}
