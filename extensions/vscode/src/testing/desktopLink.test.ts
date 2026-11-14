import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "fs";
import * as http from "http";
import * as os from "os";
import * as path from "path";
import { AddressInfo } from "net";
import { openInDesktop } from "../webview/desktopLink";

test("the running desktop app receives the artifact, with its token", async () => {
  const received: unknown[] = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const ok = req.url === "/open" && req.headers["x-cviz-token"] === "secret";
      if (ok) received.push(JSON.parse(body));
      res.writeHead(ok ? 200 : 403).end();
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const port = (server.address() as AddressInfo).port;
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "cviz-")), "desktop.json");
  process.env.CVIZ_DESKTOP_FILE = file;
  try {
    fs.writeFileSync(file, JSON.stringify({ url: `http://127.0.0.1:${port}`, token: "secret" }));
    assert.equal(await openInDesktop("/r/a.ts", 4, "/r"), true);
    assert.deepEqual(received, [{ path: "/r/a.ts", line: 4, repo: "/r" }]);

    fs.writeFileSync(file, JSON.stringify({ url: `http://127.0.0.1:${port}`, token: "wrong" }));
    assert.equal(await openInDesktop("/r/a.ts", 4, "/r"), false);
    fs.writeFileSync(file, JSON.stringify({ url: "http://example.com:80", token: "secret" }));
    assert.equal(await openInDesktop("/r/a.ts", 4, "/r"), false);
    fs.unlinkSync(file);
    assert.equal(await openInDesktop("/r/a.ts", 4, "/r"), false);
    assert.equal(received.length, 1);
  } finally {
    delete process.env.CVIZ_DESKTOP_FILE;
    server.close();
  }
});
