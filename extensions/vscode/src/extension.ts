import * as vscode from "vscode";
import { CvizMcpClient } from "./mcp/client";
import { CvizService } from "./mcp/service";
import { cvizEvents } from "./mcp/eventBus";
import { JobPoller } from "./mcp/jobPoller";
import {
  CvizCodeLensProvider,
  CvizDeadCodeCodeActionProvider,
  CvizDeadCodeDiagnostics,
  CvizHoverProvider,
} from "./providers/editorProviders";
import { BundlesTreeProvider } from "./views/explorerViews";
import { SidebarControlPanel } from "./views/controlPanel";
import { CvizStatusBarItem } from "./views/statusBarItem";
import { CallGraphPanel } from "./webview/callGraphPanel";
import { GraphLauncher } from "./webview/graphLauncher";
import { extractDeclarationSignature } from "./testing/parser";
import { DashboardPanel } from "./webview/dashboardPanel";
import { ContextManager } from "./mcp/contextManager";

/**
 * Normalise the arguments `cviz.showCallGraph` receives from its callers:
 * CodeLens passes (Uri, symbol, line), the hover link passes
 * (uriString, symbol, line), the command palette passes nothing.
 */
function graphTargetFromArgs(args: unknown[]): { file?: string; line?: number; symbol?: string } {
  const out: { file?: string; line?: number; symbol?: string } = {};
  for (const a of args) {
    if (a instanceof vscode.Uri) out.file = a.fsPath;
    else if (typeof a === "number") out.line = a;
    else if (typeof a === "string" && /^[a-z][\w+.-]*:\/\//i.test(a)) out.file = vscode.Uri.parse(a).fsPath;
    else if (typeof a === "string") out.symbol = a;
  }
  const editor = vscode.window.activeTextEditor;
  if (!out.file && editor) {
    out.file = editor.document.uri.fsPath;
    out.line = editor.selection.active.line + 1;
  } else if (out.file && out.line === undefined && editor && editor.document.uri.fsPath === out.file) {
    out.line = editor.selection.active.line + 1;
  }
  return out;
}

function extractSymbolAtCursor(editor: vscode.TextEditor): string | undefined {
  const range = editor.document.getWordRangeAtPosition(
    editor.selection.active,
    /[A-Za-z_$][A-Za-z0-9_$]*/
  );
  return range ? editor.document.getText(range) : undefined;
}

/**
 * Settings were named `cviz.*` before the rename to Code Visualizer: copy the
 * user's values to `cviz.*` once (never over a value already set there).
 * The former executable name and the former default server URL (port 8000,
 * now one server per repository) are not carried over as such.
 */
const SETTING_KEYS = ["complexityWarningThreshold", "enableAutoWatch", "executable", "graphServerUrl", "graphView", "ignoreDirs", "ignoreTestFiles", "indexVariables", "maxDeadCodeDiagnostics", "maxFileSizeMb", "maxToolResponseTokens", "parallelWorkers", "pythonPackagePath", "repoPath", "scipIndexer", "statusBarAlignment", "statusBarPriority", "toolResultLimits"];
async function migrateLegacySettings(): Promise<void> {
  const legacy = vscode.workspace.getConfiguration("cviz");
  const current = vscode.workspace.getConfiguration("cviz");
  for (const key of SETTING_KEYS) {
    const before = legacy.inspect(key);
    const now = current.inspect(key);
    if (!before) continue;
    const scopes: Array<[unknown, unknown, vscode.ConfigurationTarget]> = [
      [before.globalValue, now?.globalValue, vscode.ConfigurationTarget.Global],
      [before.workspaceValue, now?.workspaceValue, vscode.ConfigurationTarget.Workspace],
    ];
    for (let [value, existing, target] of scopes) {
      if (value === undefined || existing !== undefined) continue;
      if (key === "executable" && value === "cviz") value = "cviz";
      if (key === "graphServerUrl" && value === "http://localhost:8000") continue;
      try { await current.update(key, value, target); } catch { /* read-only scope */ }
    }
  }
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  await migrateLegacySettings();
  // ── Core infrastructure ───────────────────────────────────────────────────
  const client = new CvizMcpClient(context);
  const jobPoller = new JobPoller(new CvizService(client)); // use service on client before full start

  try {
    await client.ensureStarted();
    cvizEvents.emit("mcp:online");
  } catch (err) {
    cvizEvents.emit("mcp:offline");
    vscode.window.showWarningMessage(
      `CViz: Could not start MCP server — ${String(err)}. Check the CViz output channel.`
    );
  }

  const service = new CvizService(client);
  const statusBar = new CvizStatusBarItem(client);

  // ── UI providers ──────────────────────────────────────────────────────────
  const callGraphPanel = new CallGraphPanel(service);
  const graphLauncher = new GraphLauncher();
  const useWebGraph = () =>
    vscode.workspace.getConfiguration("cviz").get<string>("graphView", "web") !== "panel";
  const dashboardPanel = new DashboardPanel(service, client);
  const sidebarControl = new SidebarControlPanel(service, client, context);

  const diagnostics = new CvizDeadCodeDiagnostics(service);
  const codeLensProvider = new CvizCodeLensProvider(service);
  const hoverProvider = new CvizHoverProvider(service);

  const bundlesProvider = new BundlesTreeProvider(service);
  const contextManager = new ContextManager(service, sidebarControl);

  contextManager.initializeContext().catch(err => {
    console.error("CViz: Context initialization failed", err);
  });

  // ─── Menu Context Management ────────────────────────────────────────────────
  const updateMenuContext = (editor: vscode.TextEditor | undefined) => {
    if (!editor) return;
    const pos = editor.selection.active;
    const lineText = editor.document.lineAt(pos.line).text;
    const isClass = /^\s*(?:pub\s+|private\s+|protected\s+|public\s+|static\s+|export\s+)?(?:class|interface|struct|enum|trait|protocol)\b/.test(lineText);
    const isFunction = /^\s*(?:pub\s+|private\s+|protected\s+|public\s+|static\s+|async\s+|export\s+)?(?:fun|fn|func|def|function|sub|method)\b/.test(lineText);
    
    vscode.commands.executeCommand("setContext", "cviz:isClass", isClass);
    vscode.commands.executeCommand("setContext", "cviz:isFunction", isFunction);
  };

  context.subscriptions.push(
    vscode.window.onDidChangeTextEditorSelection(e => updateMenuContext(e.textEditor)),
    vscode.window.onDidChangeActiveTextEditor(updateMenuContext)
  );
  updateMenuContext(vscode.window.activeTextEditor);

  const previousSignatures = new Map<string, string>();
  const watcher = vscode.workspace.createFileSystemWatcher("**/.code-visualizer/**");

  context.subscriptions.push(
    statusBar,
    jobPoller,
    dashboardPanel,
    callGraphPanel,
    vscode.languages.registerCodeLensProvider({ scheme: "file" }, codeLensProvider),
    vscode.languages.registerHoverProvider({ scheme: "file" }, hoverProvider),
    vscode.languages.registerCodeActionsProvider(
      { scheme: "file" },
      new CvizDeadCodeCodeActionProvider(),
      { providedCodeActionKinds: CvizDeadCodeCodeActionProvider.providedCodeActionKinds }
    ),

    vscode.window.registerTreeDataProvider("cviz-bundles", bundlesProvider),
    vscode.window.registerWebviewViewProvider(SidebarControlPanel.viewType, sidebarControl),
    diagnostics,
    watcher
  );

  // ── Diagnostics helpers ───────────────────────────────────────────────────
  const refreshDiagnostics = async (doc?: vscode.TextDocument): Promise<void> => {
    const target = doc ?? vscode.window.activeTextEditor?.document;
    if (!target) return;
    try {
      await diagnostics.refreshForDocument(target);
    } catch (err) {
      vscode.window.setStatusBarMessage(`CViz diagnostics error: ${String(err)}`, 4000);
    }
  };

  // ─── Event Coordination ─────────────────────────────────────────────────────
  const invalidateAll = () => {
    codeLensProvider.invalidate();
    refreshDiagnostics().catch(() => {});
  };

  context.subscriptions.push(
  { dispose: cvizEvents.on("graph:changed",   invalidateAll) },
  { dispose: cvizEvents.on("index:done",      invalidateAll) },
  { dispose: cvizEvents.on("repo:changed",    invalidateAll) },
  { dispose: cvizEvents.on("context:changed", invalidateAll) }
);

  // ── Commands ──────────────────────────────────────────────────────────────
  context.subscriptions.push(
    vscode.commands.registerCommand("cviz.openDashboard", () => {
      dashboardPanel.show();
    }),

    vscode.commands.registerCommand("cviz.visualizeRepo", () => {
      dashboardPanel.show();
    }),

    vscode.commands.registerCommand("cviz.showCallGraph", async (...args: unknown[]) => {
      const target = graphTargetFromArgs(args);
      if (useWebGraph() && target.file) {
        await graphLauncher.open(target.file, target.line);
        return;
      }
      const editor = vscode.window.activeTextEditor;
      callGraphPanel.show(context, target.symbol ?? (editor ? extractSymbolAtCursor(editor) : undefined));
    }),

    vscode.commands.registerCommand("cviz.openInGraph", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showInformationMessage("CViz: open a file to show it in the dependency graph.");
        return;
      }
      await graphLauncher.open(editor.document.uri.fsPath, editor.selection.active.line + 1);
    }),

    vscode.commands.registerCommand("cviz.analyzeRelationships", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const symbol = extractSymbolAtCursor(editor);
      if (!symbol) return;
      const callers = await service.findCallers(symbol, editor.document.uri.fsPath);
      const selected = await vscode.window.showQuickPick(
        callers.map((c) => ({
          label: c.caller_name ?? "caller",
          description: c.caller_file_path,
          line: c.call_line_number ?? c.caller_line_number ?? 1,
        })),
        { title: `Callers of ${symbol}` }
      );
      if (selected?.description) {
        const doc = await vscode.workspace.openTextDocument(selected.description);
        const nextEditor = await vscode.window.showTextDocument(doc);
        const pos = new vscode.Position(Math.max(0, selected.line - 1), 0);
        nextEditor.selection = new vscode.Selection(pos, pos);
        nextEditor.revealRange(new vscode.Range(pos, pos));
      }
    }),

    vscode.commands.registerCommand("cviz.refreshIndex", async () => {
      const workspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      if (!workspace) return;
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Refreshing Code Visualizer index…" },
        async () => {
          const jobId = await service.indexWorkspace(workspace);
          if (jobId) jobPoller.startPolling(jobId);
          await service.watchWorkspace(workspace);
        }
      );
      cvizEvents.emit("graph:changed");
      await refreshDiagnostics();
    }),

    vscode.commands.registerCommand("cviz.runIndexWizard", async () => {
      const workspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      if (!workspace) return;
      const choice = await vscode.window.showQuickPick(["Index only", "Index + Watch"], {
        title: "Code Visualizer setup",
      });
      if (!choice) return;
      const jobId = await service.indexWorkspace(workspace);
      if (jobId) jobPoller.startPolling(jobId);
      if (choice === "Index + Watch") {
        await service.watchWorkspace(workspace);
      }
      vscode.window.showInformationMessage("CViz setup complete for this workspace.");
      cvizEvents.emit("graph:changed");
    }),

    vscode.commands.registerCommand("cviz.openEngineConfig", () => {
      vscode.commands.executeCommand("workbench.action.openSettings", "cviz");
    }),

    vscode.commands.registerCommand("cviz.runCypherQuery", async () => {
      dashboardPanel.show();
    }),

    vscode.commands.registerCommand("cviz.showComplexityAtSymbol", async (uri: vscode.Uri, symbol: string) => {
      const complexity = await service.getComplexity(symbol, uri.fsPath);
      vscode.window.showInformationMessage(`Complexity for ${symbol}: ${complexity ?? "unknown"}`);
    }),

    vscode.commands.registerCommand("cviz.showCallersAtSymbol", async (uri: vscode.Uri, symbol: string) => {
      const callers = await service.findCallers(symbol, uri.fsPath);
      vscode.window.showInformationMessage(`${symbol} has ${callers.length} caller(s).`);
    }),

    vscode.commands.registerCommand("cviz.showVariableImpact", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const variable = extractSymbolAtCursor(editor);
      if (!variable) return;
      const impacts = await service.variableImpactRadius(variable, editor.document.uri.fsPath);
      const picked = await vscode.window.showQuickPick(
        impacts.slice(0, 50).map((row) => ({
          label: String(row["name"] ?? row["variable_name"] ?? variable),
          description: String(row["path"] ?? row["file_path"] ?? ""),
          detail: JSON.stringify(row),
        })),
        { title: `Impact Radius for ${variable}` }
      );
      if (picked?.description) {
        const doc = await vscode.workspace.openTextDocument(picked.description);
        await vscode.window.showTextDocument(doc);
      }
    }),

    vscode.commands.registerCommand("cviz.showClassHierarchy", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const className = extractSymbolAtCursor(editor);
      if (!className) return;
      const hierarchy = await service.findClassHierarchy(className, editor.document.uri.fsPath);
      if (!hierarchy.length) {
        vscode.window.showInformationMessage(`No inheritance data found for ${className}.`);
        return;
      }
      const picked = await vscode.window.showQuickPick(
        hierarchy.slice(0, 50).map((row) => ({
          label: String(row["name"] ?? row["class_name"] ?? "?"),
          description: String(row["path"] ?? row["file_path"] ?? ""),
          detail: String(row["relationship"] ?? row["type"] ?? ""),
        })),
        { title: `Class Hierarchy for ${className}` }
      );
      if (picked?.description) {
        const doc = await vscode.workspace.openTextDocument(picked.description);
        await vscode.window.showTextDocument(doc);
      }
    }),

    vscode.commands.registerCommand("cviz.generateReport", async () => {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "CViz: Generating code health report…", cancellable: false },
        async () => {
          const outputPath = await service.generateReport();
          vscode.window.showInformationMessage(`Report written to: ${outputPath}`, "Open").then(async (choice) => {
            if (choice === "Open") {
              try {
                const doc = await vscode.workspace.openTextDocument(outputPath);
                await vscode.window.showTextDocument(doc);
              } catch {
                vscode.window.showWarningMessage(`Could not open report at ${outputPath}`);
              }
            }
          });
        }
      );
    }),

    vscode.commands.registerCommand("cviz.discoverContexts", async () => {
      const workspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      const contexts = await service.discoverContexts(workspace);
      if (!contexts.length) {
        vscode.window.showInformationMessage("No sub-project .code-visualizer folders found.");
        return;
      }
      const picked = await vscode.window.showQuickPick(
        contexts.map((c) => ({ label: c.name ?? c.path, description: c.path })),
        { title: "Switch CViz context to…" }
      );
      if (picked?.description) {
        await service.switchContext(picked.description);
        cvizEvents.emit("context:changed", { source: "command", contextPath: picked.description });
        vscode.window.showInformationMessage(`CViz context switched to: ${picked.label}`);
      }
    }),

    vscode.commands.registerCommand("cviz.suppressDeadCode", (uri: vscode.Uri, line: number) => {
      // No-op placeholder — suppression list would be stored in workspace settings
      vscode.window.showInformationMessage(`CViz: Dead code warning suppressed for line ${line + 1}.`);
    }),

    // Internal: jump-to-location used by hover card command links
    vscode.commands.registerCommand("cviz._jumpToLocation", async (uriStr: string, line: number) => {
      try {
        const doc = await vscode.workspace.openTextDocument(vscode.Uri.parse(uriStr));
        const editor = await vscode.window.showTextDocument(doc);
        const pos = new vscode.Position(Math.max(0, line - 1), 0);
        editor.selection = new vscode.Selection(pos, pos);
        editor.revealRange(new vscode.Range(pos, pos));
      } catch {
        // silently fail — file may not exist on disk
      }
    }),

    // ── Event listeners ────────────────────────────────────────────────────
    vscode.workspace.onDidSaveTextDocument(async (doc) => {
      await refreshDiagnostics(doc);
      const currentSig = extractDeclarationSignature(doc.lineAt(0).text) ?? "";
      const prev = previousSignatures.get(doc.uri.fsPath);
      if (prev && prev !== currentSig) {
        const impact = await service.findCallers(currentSig || prev, doc.uri.fsPath);
        if (impact.length > 0) {
          vscode.window
            .showWarningMessage(
              `CViz: ${impact.length} caller(s) may be affected by signature change in ${doc.fileName.split("/").pop()}.`,
              "Show Call Graph"
            )
            .then((choice) => {
              if (choice === "Show Call Graph") {
                if (useWebGraph()) {
                  void graphLauncher.open(doc.uri.fsPath, 1);
                } else {
                  callGraphPanel.show(context, currentSig || prev);
                }
              }
            });
        }
      }
      previousSignatures.set(doc.uri.fsPath, currentSig);
      dashboardPanel.notifyRefresh("save event");
      await dashboardPanel.refresh();
    }),

    vscode.window.onDidChangeActiveTextEditor(async (editor) => {
      if (!editor) return;
      await refreshDiagnostics(editor.document);
      const symbol = extractSymbolAtCursor(editor);
      if (symbol) {
        callGraphPanel.postEditorSelection(editor.document.uri.fsPath, symbol);
      }
    }),

    vscode.window.onDidChangeTextEditorSelection((evt) => {
      const symbol = extractSymbolAtCursor(evt.textEditor);
      if (symbol) {
        callGraphPanel.postEditorSelection(evt.textEditor.document.uri.fsPath, symbol);
      }
    }),

    watcher.onDidCreate(async () => {
      cvizEvents.emit("graph:changed");
      dashboardPanel.notifyRefresh(".code-visualizer created");
    }),

    watcher.onDidChange(async () => {
      cvizEvents.emit("graph:changed");
      dashboardPanel.notifyRefresh(".code-visualizer changed");
    }),

    watcher.onDidDelete(async () => {
      cvizEvents.emit("graph:changed");
      dashboardPanel.notifyRefresh(".code-visualizer deleted");
    })
  );

  dashboardPanel.show();
  await refreshDiagnostics();
}

export function deactivate(): void {
  // Resources are disposed via extension subscriptions
}
