import test from "node:test";
import assert from "node:assert/strict";
import { renderSetupHtml } from "../webview/setupTemplate";

test("setup panel renders all config controls", () => {
  const html = renderSetupHtml("cviz", "/usr/bin/uvx", 1200);
  assert.ok(html.includes("id=\"executable\""));
  assert.ok(html.includes("id=\"pythonPackagePath\""));
  assert.ok(html.includes("id=\"maxToolResponseTokens\""));
  assert.ok(!html.includes("dbMode") && !html.includes("neo4j"));
  assert.ok(html.includes("Test Connection"));
  assert.ok(html.includes("Save & Apply"));
});
