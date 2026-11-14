import * as fs from "fs";
import * as http from "http";
import * as os from "os";
import * as path from "path";

/** `~/.code-visualizer/desktop.json`: the IDE endpoint of the running desktop app. */
function desktopFile(): string {
  return process.env.CVIZ_DESKTOP_FILE || path.join(os.homedir(), ".code-visualizer", "desktop.json");
}

/**
 * Hands the artifact to the running desktop app, which comes forward on the
 * repository's tab (desktop/src-tauri, ide_link). False when no app answers.
 */
export async function openInDesktop(filePath: string, line: number | undefined, repo: string): Promise<boolean> {
  let record: { url?: string; token?: string };
  try {
    record = JSON.parse(fs.readFileSync(desktopFile(), "utf8"));
  } catch {
    return false;
  }
  const url = String(record.url || "").replace(/\/+$/, "");
  if (!record.token || !/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(url)) return false;
  const payload = Buffer.from(JSON.stringify({ path: filePath, line: line ?? null, repo }));
  return new Promise((resolve) => {
    const req = http.request(`${url}/open`, {
      method: "POST",
      timeout: 2000,
      headers: { "Content-Type": "application/json", "Content-Length": payload.length, "X-CViz-Token": record.token! },
    }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("timeout", () => req.destroy());
    req.on("error", () => resolve(false));
    req.end(payload);
  });
}
