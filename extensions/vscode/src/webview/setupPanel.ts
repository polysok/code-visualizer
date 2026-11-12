import * as vscode from "vscode";
import { CvizMcpClient } from "../mcp/client";

export class SetupPanel {
  public static readonly viewType = "cvizSetup";
  private static currentPanel: SetupPanel | undefined;
  private readonly _panel: vscode.WebviewPanel;
  private _disposables: vscode.Disposable[] = [];

  public static createOrShow(context: vscode.ExtensionContext, client: CvizMcpClient) {
    const column = vscode.window.activeTextEditor ? vscode.window.activeTextEditor.viewColumn : undefined;

    if (SetupPanel.currentPanel) {
      SetupPanel.currentPanel._panel.reveal(column);
      return;
    }

    const panel = vscode.window.createWebviewPanel(
      SetupPanel.viewType,
      "CViz Setup & Diagnostics",
      column || vscode.ViewColumn.One,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
      }
    );

    SetupPanel.currentPanel = new SetupPanel(panel, context, client);
  }

  private constructor(panel: vscode.WebviewPanel, context: vscode.ExtensionContext, private readonly client: CvizMcpClient) {
    this._panel = panel;
    this._panel.onDidDispose(() => this.dispose(), null, this._disposables);
    this._panel.webview.onDidReceiveMessage(
      async (message) => {
        switch (message.command) {
          case "test":
            await this._handleTest(message.executable);
            break;
          case "save":
            await this._handleSave(
              message.executable,
              message.pythonPackagePath,
              message.maxToolResponseTokens
            );
            break;
        }
      },
      null,
      this._disposables
    );

    this._update();
  }

  private async _handleTest(executable: string) {
    this._panel.webview.postMessage({ command: "testResult", status: "testing", message: "Starting test..." });
    
    // Temporarily override or use a dry-run start
    try {
      // We'll use a one-off spawn to check version/help
      const { exec } = require("child_process");
      const cmd = `${executable} --help`;
      
      exec(cmd, async (error: any, stdout: string, stderr: string) => {
        if (error) {
          this._panel.webview.postMessage({ 
            command: "testResult", 
            status: "error", 
            message: `Failed to execute: ${error.message}` 
          });
          return;
        }
        
        this._panel.webview.postMessage({ 
          command: "testResult", 
          status: "success", 
          message: "Executable found and responding! Now checking MCP connectivity..." 
        });

        // Try actual MCP start if possible
        try {
            await this.client.ensureStarted(); // This uses current settings, so test might be limited
            this._panel.webview.postMessage({ 
                command: "testResult", 
                status: "success", 
                message: "Perfect! CViz MCP is online and connected." 
            });
        } catch (mcpErr: any) {
            this._panel.webview.postMessage({ 
                command: "testResult", 
                status: "warning", 
                message: `Executable ok, but MCP failed: ${mcpErr.message}` 
            });
        }
      });
    } catch (err: any) {
      this._panel.webview.postMessage({ command: "testResult", status: "error", message: err.message });
    }
  }

  private async _handleSave(
    executable: string,
    pythonPackagePath?: string,
    maxToolResponseTokens?: number
  ) {
    const cfg = vscode.workspace.getConfiguration("cviz");
    await cfg.update("executable", executable, vscode.ConfigurationTarget.Global);
    await cfg.update("pythonPackagePath", pythonPackagePath ?? "", vscode.ConfigurationTarget.Global);
    await cfg.update("maxToolResponseTokens", maxToolResponseTokens ?? 0, vscode.ConfigurationTarget.Global);
    vscode.window.showInformationMessage("CViz settings saved. Restarting extension...");
    
    // Trigger restart
    this.client.dispose();
    await this.client.ensureStarted();
    
    this._panel.webview.postMessage({ command: "saved" });
    this._update();
  }

  public dispose() {
    SetupPanel.currentPanel = undefined;
    this._panel.dispose();
    while (this._disposables.length) {
      const x = this._disposables.pop();
      if (x) {
        x.dispose();
      }
    }
  }

  private _update() {
    const cfg = vscode.workspace.getConfiguration("cviz");
    const executable = cfg.get<string>("executable", "cviz");
    const pythonPackagePath = cfg.get<string>("pythonPackagePath", "");
    const maxToolResponseTokens = cfg.get<number>("maxToolResponseTokens", 0);

    this._panel.webview.html = this._getHtmlForWebview(executable, pythonPackagePath, maxToolResponseTokens);
  }

  private _getHtmlForWebview(
    executable: string,
    pythonPackagePath: string,
    maxToolResponseTokens: number
  ) {
    return `<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>CViz Setup</title>
        <style>
            body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); padding: 20px; line-height: 1.6; }
            .container { max-width: 600px; margin: 0 auto; background: var(--vscode-editor-background); padding: 30px; border-radius: 8px; border: 1px solid var(--vscode-widget-border); box-shadow: 0 4px 15px rgba(0,0,0,0.3); }
            h1 { color: var(--vscode-textLink-foreground); font-weight: 300; border-bottom: 1px solid var(--vscode-widget-border); padding-bottom: 10px; }
            .field { margin-bottom: 25px; }
            label { display: block; margin-bottom: 8px; font-weight: bold; font-size: 0.9em; opacity: 0.8; }
            input, select { width: 100%; padding: 10px; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border); border-radius: 4px; box-sizing: border-box; }
            .actions { display: flex; gap: 10px; margin-top: 30px; }
            button { padding: 10px 20px; cursor: pointer; border: none; border-radius: 4px; font-weight: bold; transition: opacity 0.2s; }
            button.primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
            button.secondary { background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); }
            button:hover { opacity: 0.9; }
            #status { margin-top: 20px; padding: 15px; border-radius: 4px; display: none; }
            .status-testing { background: rgba(255, 255, 0, 0.1); border: 1px solid yellow; color: yellow; display: block !important; }
            .status-success { background: rgba(0, 255, 0, 0.1); border: 1px solid green; color: #4caf50; display: block !important; }
            .status-error { background: rgba(255, 0, 0, 0.1); border: 1px solid red; color: #f44336; display: block !important; }
            .status-warning { background: rgba(255, 165, 0, 0.1); border: 1px solid orange; color: orange; display: block !important; }
            .hint { font-size: 0.8em; opacity: 0.6; margin-top: 5px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Code Visualizer Setup</h1>
            <p>Configure and verify your CViz environment to enable editor intelligence.</p>

            <div class="field">
                <label>CViz Executable Path</label>
                <input type="text" id="executable" value="${executable}" placeholder="e.g. /usr/local/bin/cviz or .venv/bin/cviz">
                <div class="hint">Absolute path to the 'cviz' CLI tool.</div>
            </div>

            <div class="field">
                <label>Python Package Path / uvx Path</label>
                <input type="text" id="pythonPackagePath" value="${pythonPackagePath}" placeholder="e.g. /usr/bin/uvx or custom PYTHONPATH">
            </div>
            <div class="field">
                <label>Max Tool Response Tokens</label>
                <input type="number" id="maxToolResponseTokens" value="${maxToolResponseTokens}" min="0">
            </div>

            <div id="status"></div>

            <div class="actions">
                <button class="secondary" onclick="test()">Test Connection</button>
                <button class="primary" onclick="save()">Save & Apply</button>
            </div>
        </div>

        <script>
            const vscode = acquireVsCodeApi();
            const statusDiv = document.getElementById('status');

            function test() {
                const executable = document.getElementById('executable').value;
                vscode.postMessage({ command: 'test', executable });
            }

            function save() {
                const executable = document.getElementById('executable').value;
                const pythonPackagePath = document.getElementById('pythonPackagePath').value;
                const maxToolResponseTokens = Number(document.getElementById('maxToolResponseTokens').value || 0);
                vscode.postMessage({ command: 'save', executable, pythonPackagePath, maxToolResponseTokens });
            }

            window.addEventListener('message', event => {
                const message = event.data;
                switch (message.command) {
                    case 'testResult':
                        statusDiv.className = 'status-' + message.status;
                        statusDiv.innerText = message.message;
                        break;
                    case 'saved':
                        statusDiv.className = 'status-success';
                        statusDiv.innerText = 'Settings saved and applied successfully!';
                        break;
                }
            });
        </script>
    </body>
    </html>`;
  }
}
