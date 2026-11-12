# src/code_visualizer/cli/setup_wizard.py
from InquirerPy import prompt
from rich.console import Console
import subprocess
import os
from pathlib import Path
import json
import re
import sys
import shutil
import yaml 
from code_visualizer.cli.config_manager import MCP_SERVER_KEY, normalize_config_path

console = Console()

def _strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments and trailing commas from a JSONC document.

    VS Code and its forks write settings.json as JSONC. json.loads rejects both,
    and treating that rejection as "no settings yet" discards the user's file.
    """
    out = []
    i, n = 0, len(text)
    in_string = escaped = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    stripped = "".join(out)
    # A trailing comma before } or ] is legal JSONC, not JSON.
    return re.sub(r",(\s*[}\]])", r"\1", stripped)


def _check_write_access(path: Path) -> bool:
    target = path if path.exists() else path.parent

    while not target.exists() and target != target.parent:
        target = target.parent

    return os.access(target, os.W_OK)








def find_jetbrains_mcp_config():
    bases = [
        Path.home() / ".config" / "JetBrains",
        Path.home() / "Library/Application Support/JetBrains",
        Path.home() / "AppData/Roaming/JetBrains"
    ]
    configs = []
    for base in bases:
        if base.exists():
            for folder in base.iterdir():  # each IDE/version
                options = folder / "options"
                mcp_file = options / "mcpServer.xml"
                if mcp_file.exists():
                    configs.append(mcp_file)
                    print(mcp_file)
                    return configs
    # Always return a list: callers store this directly in the config_paths
    # mapping and iterate it, so returning None (no JetBrains install, or no
    # mcpServer.xml yet) raised a TypeError instead of falling through to the
    # "configure manually" path.
    return configs


def convert_mcp_json_to_yaml():
    json_path = Path.cwd() / "mcp.json"
    yaml_path = Path.cwd() / "devfile.yaml"
    if json_path.exists():
        with open(json_path, "r") as json_file:
            mcp_config = json.load(json_file)
        if not _check_write_access(yaml_path):
            console.print(
                f"[bold red]Permission denied:[/bold red] Cannot write to {yaml_path}"
            )
            return

        try:
            with open(yaml_path, "w") as yaml_file:
                yaml.dump(mcp_config, yaml_file, default_flow_style=False)
        except PermissionError:
            console.print(
                f"[bold red]Permission denied while writing:[/bold red] {yaml_path}"
            )
            return
        console.print(f"[green]Generated devfile.yaml for Amazon Q Developer at {yaml_path}[/green]")

def _print_opencode_mcp_instructions(mcp_config: dict) -> None:
    """OpenCode manages MCP in its own UI; we only print the stdio snippet + doc link."""
    console.print("\n[bold cyan]OpenCode[/bold cyan]")
    console.print(
        "Register a stdio MCP server in OpenCode using the same command, args, and env as below "
        "(mirror your generated mcp.json so OpenCode and the CLI share one database)."
    )
    console.print(
        "\n[dim]Vendor guide:[/dim] https://opencode.ai/docs/ko/mcp-servers/#_top"
    )
    console.print("\n[bold]Suggested MCP server JSON:[/bold]")
    console.print(json.dumps(mcp_config, indent=2))

def _configure_goose(mcp_config):
    """Configures Goose CLI with the MCP server."""
    # Define paths
    paths = [
        Path.home() / ".config" / "goose" / "config.yaml", # Linux/macOS
        Path.home() / "AppData" / "Roaming" / "Block" / "goose" / "config.yaml", # Windows default
        Path.home() / "AppData" / "Roaming" / "goose" / "config.yaml" # Windows alternative
    ]
    
    target_path = None
    for path in paths:
        if path.exists():
            target_path = path
            break
            
    if not target_path:
        # Check parents
        for path in paths:
            if path.parent.exists():
                target_path = path
                break
                
    if not target_path:
        # If no config found, default to the first standard path and ensure directory exists
        target_path = paths[0]
        
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            console.print(f"[green]Created new configuration directory at: {target_path.parent}[/green]")
        except PermissionError:
            console.print(
                f"[bold red]Permission denied:[/bold red] Cannot create {target_path.parent}"
            )
            console.print(
                "[yellow]Please run with appropriate permissions or create the directory manually.[/yellow]"
            )
            return
            
        except Exception as e:
             console.print(f"[yellow]Current paths checked: {[str(p) for p in paths]}[/yellow]")
             console.print(f"[yellow]Could not create configuration directory: {e}[/yellow]")
             console.print("Please add the MCP configuration manually.")
             return

    console.print(f"Using configuration file at: {target_path}")

    try:
        # Load existing config or start fresh
        if target_path.exists():
            with open(target_path, "r") as f:
                try:
                    config = yaml.safe_load(f) or {}
                except yaml.YAMLError as e:
                    console.print(f"[red]Error parsing existing Goose configuration: {e}[/red]")
                    console.print("[yellow]Aborting to prevent data loss. Please fix your config.yaml and try again.[/yellow]")
                    return
        else:
            config = {}
            
        if "extensions" in config and not isinstance(config["extensions"], dict):
            console.print("[red]Error: The 'extensions' field in the Goose configuration must be a mapping (dictionary).[/red]")
            console.print("[yellow]Aborting to prevent overwriting an invalid 'extensions' value. Please fix your config.yaml and try again.[/yellow]")
            return

        if "extensions" not in config:
            config["extensions"] = {}
            
        # Transform mcp_config to Goose format
        if "mcpServers" in mcp_config and MCP_SERVER_KEY in mcp_config["mcpServers"]:
            cviz_config = mcp_config["mcpServers"][MCP_SERVER_KEY]
            
            # Ensure args are in list format before writing Goose configuration
            cmd = cviz_config.get("command", "cviz")
            raw_args = cviz_config.get("args")

            if raw_args is None:
                args = ["mcp", "start"]
            elif isinstance(raw_args, str):
                # Allow a single string and treat it as a single argument
                args = [raw_args]
            elif isinstance(raw_args, list):
                args = raw_args
            else:
                console.print("[red]Error: Invalid type for 'args' in MCP configuration. Expected a list of arguments or a string.[/red]")
                return
            
            goose_ext = {
                "enabled": True,
                "name": "Code Visualizer",
                "type": "stdio",
                "cmd": cmd,
                "args": args,
                "envs": cviz_config.get("env", {})
            }
            
            config["extensions"]["code_visualizer"] = goose_ext
            
            if not _check_write_access(target_path):
                console.print(
                    f"[bold red]Permission denied:[/bold red] Cannot write to {target_path}"
                )
                return

            try:
                with open(target_path, "w") as f:
                    yaml.dump(config, f, default_flow_style=False)
            except PermissionError:
                console.print(
                    f"[bold red]Permission denied while writing:[/bold red] {target_path}"
                )
                return
                
            console.print("[green]Successfully updated Goose configuration.[/green]")
        else:
            console.print("[red]Error: Invalid MCP configuration structure.[/red]")
        
    except Exception as e:
        console.print(f"[red]Failed to update Goose configuration: {e}[/red]")

def _configure_ide(mcp_config):
    """Asks user for their IDE and configures it automatically."""
    questions = [
        {
            "type": "confirm",
            "message": "Automatically configure your IDE/CLI (VS Code, Cursor, Windsurf, Zed, Claude, Gemini, Cline, RooCode, ChatGPT Codex, Amazon Q Developer, Aider, Kiro, Goose, Antigravity, OpenCode)?",
            "name": "configure_ide",
            "default": True,
        }
    ]
    result = prompt(questions)
    if not result or not result.get("configure_ide"):
        console.print("\n[cyan]Skipping automatic IDE configuration. You can add the MCP server manually.[/cyan]")
        return

    ide_questions = [
        {
            "type": "list",
            "message": "Choose your IDE/CLI to configure:",
            "choices": ["VS Code", "Cursor", "Windsurf", "Zed", "Claude code", "Gemini CLI", "ChatGPT Codex", "Cline", "RooCode", "Amazon Q Developer", "JetBrainsAI", "Aider", "Kiro", "Goose", "Antigravity", "OpenCode", "None of the above"],
            "name": "ide_choice",
        }
    ]
    ide_result = prompt(ide_questions)
    ide_choice = ide_result.get("ide_choice")

    if not ide_choice or ide_choice == "None of the above":
        console.print("\n[cyan]You can add the MCP server manually to your IDE/CLI.[/cyan]")
        return

    if ide_choice == "OpenCode":
        _print_opencode_mcp_instructions(mcp_config)
        console.print(
            "\n[green]When you have pasted this into OpenCode, reload MCP and run "
            "`cviz mcp start` from a terminal to verify the server starts cleanly.[/green]"
        )
        return

    if ide_choice in ["VS Code", "Cursor", "Windsurf", "Zed", "Claude code", "Gemini CLI", "ChatGPT Codex", "Cline", "RooCode", "Amazon Q Developer", "JetBrainsAI", "Aider", "Kiro", "Goose", "Antigravity"]:
        console.print(f"\n[bold cyan]Configuring for {ide_choice}...[/bold cyan]")

        if ide_choice == "Amazon Q Developer":
            convert_mcp_json_to_yaml()
            return  
        
        if ide_choice == "Goose":
            _configure_goose(mcp_config)
            return
        
        config_paths = {
            "VS Code": [
                Path.home() / ".config" / "Code" / "User" / "settings.json",
                Path.home() / "Library" / "Application Support" / "Code" / "User" / "settings.json",
                Path.home() / "AppData" / "Roaming" / "Code" / "User" / "settings.json"
            ],
            "Cursor": [
                Path.home() / ".cursor" / "settings.json",
                Path.home() / ".config" / "cursor" / "settings.json",
                Path.home() / "Library" / "Application Support" / "cursor" / "settings.json",
                Path.home() / "AppData" / "Roaming" / "cursor" / "settings.json",
                Path.home() / ".config" / "Cursor" / "User" / "settings.json",
            ],
            "Windsurf": [
                Path.home() / ".windsurf" / "settings.json",
                Path.home() / ".config" / "windsurf" / "settings.json",
                Path.home() / "Library" / "Application Support" / "windsurf" / "settings.json",
                Path.home() / "AppData" / "Roaming" / "windsurf" / "settings.json",
                Path.home() / ".config" / "Windsurf" / "User" / "settings.json",
            ],
            "Zed": [
                Path.home() / ".config" / "zed" / "settings.json",
                Path.home() / "AppData" / "Roaming" / "Zed" / "settings.json"
            ],
            "Claude code": [
                Path.home() / ".claude.json"
            ],
            "Gemini CLI": [
                Path.home() / ".gemini" / "settings.json"
            ],
            "ChatGPT Codex": [
                Path.home() / ".openai" / "mcp_settings.json",
                Path.home() / ".config" / "openai" / "settings.json",
                Path.home() / "AppData" / "Roaming" / "OpenAI" / "settings.json"
            ],
            "Cline": [
                Path.home() / ".config" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
                Path.home() / ".config" / "Code - OSS" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
                Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
                Path.home() / "AppData" / "Roaming" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json"
            ],

            "JetBrainsAI": find_jetbrains_mcp_config(), #only for jetbrains ide

            "RooCode": [
                Path.home() / ".config" / "Code" / "User" / "settings.json",   # Linux 
                Path.home() / "AppData" / "Roaming" / "Code" / "User" / "settings.json",  # Windows
                Path.home() / "Library" / "Application Support" / "Code" / "User" / "settings.json"  # macOS
            ],
            "Aider": [
                Path.home() / ".aider" / "settings.json",
                Path.home() / ".config" / "aider" / "settings.json",
                Path.home() / "Library" / "Application Support" / "aider" / "settings.json",
                Path.home() / "AppData" / "Roaming" / "aider" / "settings.json",
                Path.home() / ".config" / "Aider" / "User" / "settings.json",
            ],
            "Kiro": [
                Path.home() / ".kiro" / "settings" / "mcp.json",                                   # macOS / Linux / Windows (user-level global)
                Path.home() / ".config" / "kiro" / "settings" / "mcp.json",                         # Linux (XDG config)
                Path.home() / "AppData" / "Roaming" / "Kiro" / "settings" / "mcp.json",             # Windows
            ],
            "Antigravity": [
                Path.home() / ".antigravity" / "mcp_settings.json",                                # macOS / Linux / Windows (user-level global)
                Path.home() / ".config" / "antigravity" / "mcp_settings.json",                     # Linux (XDG config)
                Path.home() / "AppData" / "Roaming" / "Antigravity" / "mcp_settings.json",         # Windows
            ]
        }

        target_path = None
        paths_to_check = config_paths.get(ide_choice, [])
        for path in paths_to_check:
            if path.exists():
                target_path = path
                break
        
        if not target_path:
            # If file doesn't exist, check if parent directory exists
            for path in paths_to_check:
                if path.parent.exists():
                    target_path = path
                    break
        
        if not target_path:
            console.print(f"[yellow]Could not automatically find or create the configuration directory for {ide_choice}.[/yellow]")
            console.print("Please add the MCP configuration manually from the `mcp.json` file generated above.")
            return

        console.print(f"Using configuration file at: {target_path}")
        
        had_comments = False
        try:
            raw = target_path.read_text()
            try:
                settings = json.loads(raw)
            except json.JSONDecodeError:
                try:
                    settings = json.loads(_strip_jsonc(raw))
                    had_comments = True
                except json.JSONDecodeError:
                    # Never fall back to {}: this file gets written back below, so
                    # treating an unreadable file as an empty one destroys it.
                    console.print(
                        f"[bold red]Could not parse {target_path}.[/bold red] "
                        "Leaving it untouched so nothing is lost."
                    )
                    console.print(
                        "Please add the MCP configuration manually from the `mcp.json` file generated above."
                    )
                    return
        except FileNotFoundError:
            settings = {}

        if not isinstance(settings, dict):
            console.print(f"[red]Error: Configuration file at {target_path} is not a valid JSON object.[/red]")
            return

        if ide_choice == "Zed":
            if "context_servers" not in settings:
                settings["context_servers"] = {}
            settings["context_servers"].update(mcp_config["mcpServers"])
        else:
            if "mcpServers" not in settings:
                settings["mcpServers"] = {}
            settings["mcpServers"].update(mcp_config["mcpServers"])

        try:
            if not _check_write_access(target_path):
                console.print(
                    f"[bold red]Permission denied:[/bold red] Cannot write to {target_path}"
                )
                console.print(
                    "[yellow]Please check file permissions or run with appropriate privileges.[/yellow]"
                )
                return

            if target_path.exists():
                backup_path = target_path.with_suffix(target_path.suffix + ".cviz-backup")
                shutil.copy2(target_path, backup_path)
                console.print(f"[dim]Backed up existing configuration to {backup_path}[/dim]")
            if had_comments:
                console.print(
                    "[yellow]Note: this file contained comments. JSON output cannot keep them, "
                    "so they are dropped in the rewritten file (the backup above still has them).[/yellow]"
                )
            try:
                with open(target_path, "w") as f:
                    json.dump(settings, f, indent=2)
            except PermissionError:
                console.print(
                    f"[bold red]Permission denied while writing:[/bold red] {target_path}"
                )
                return
            console.print(f"[green]Successfully updated {ide_choice} configuration.[/green]")
        except Exception as e:
            console.print(f"[red]Failed to write to configuration file: {e}[/red]")




def get_project_root() -> Path:
    """Always return the directory where the user runs `cviz` (CWD)."""
    return Path.cwd()

def run_command(command, console, shell=False, check=True, input_text=None):
    """
    Runs a command, captures its output, and handles execution.
    Returns the completed process object on success, None on failure.
    """
    cmd_str = command if isinstance(command, str) else ' '.join(command)
    
    # Mask passwords from being printed out
    if "set-initial-password" in cmd_str:
        import re
        cmd_str = re.sub(r'(set-initial-password\s+)(\S+)', r'\g<1>********', cmd_str)
        
    console.print(f"[cyan]$ {cmd_str}[/cyan]")
    try:
        process = subprocess.run(
            command,
            shell=shell,
            check=check,
            capture_output=True,  # Always capture to control what gets displayed
            text=True,
            timeout=300,
            input=input_text
        )
        return process
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]Error executing command:[/bold red] {cmd_str}")
        if e.stdout:
            console.print(f"[red]STDOUT: {e.stdout}[/red]")
        if e.stderr:
            console.print(f"[red]STDERR: {e.stderr}[/red]")
        return None
    except subprocess.TimeoutExpired:
        console.print(f"[bold red]Command timed out:[/bold red] {cmd_str}")
        return None



def configure_mcp_client():
    """
    Configure MCP client (IDE/CLI) integration.
    This function sets up the MCP server configuration for the user's IDE.
    Includes all current configuration values in the env section.
    """
    console.print("[bold cyan]MCP Client Configuration[/bold cyan]\n")
    console.print("This will configure Code Visualizer integration with your IDE or CLI tool.")
    console.print("Code Visualizer uses the embedded LadybugDB database (no database setup needed).\n")
    
    # Load current configuration (includes project-local .env if present)
    try:
        from code_visualizer.cli.config_manager import load_config
        config = load_config()
    except Exception as e:
        console.print(f"[yellow]Warning: Could not load configuration: {e}[/yellow]")
        config = {}
    
    # Build env section with all configuration values
    env_vars = {}
    
    # Add the configuration values, normalizing path-related settings. API keys
    # (LLM / embeddings of the graph page) stay in the CViz config: MCP client
    # files are often shared or committed.
    for key, value in config.items():
        if key.upper().endswith("API_KEY"):
            continue
        # Expand ~/$VARS and convert relative paths to absolute for MCP env
        if "PATH" in key and value:
            value = normalize_config_path(value, absolute=True)

        env_vars[key] = value

    # Generate MCP configuration
    cviz_path = shutil.which("cviz")
    pipx_path = shutil.which("pipx")

    if cviz_path:
        command = cviz_path
        args = ["mcp", "start"]
    elif pipx_path:
        command = pipx_path
        args = ["run", "code_visualizer", "mcp", "start"]
    else:
        command = sys.executable
        args = ["-m", "code_visualizer", "mcp", "start"]

    # Create MCP config with complete env section
    mcp_config = {
        "mcpServers": {
            MCP_SERVER_KEY: {
                "command": command,
                "args": args,
                "env": env_vars,
                "tools": {
                    # All the tools only read the graph.
                    "alwaysAllow": [
                        "graph_overview", "search_code", "traverse_graph",
                        "read_code", "execute_cypher_query",
                    ],
                    "disabledTools": [],
                    "disabled": False
                },
                "disabled": False,
                "alwaysAllow": []
            }
        }
    }

    console.print("\n[bold green]Configuration generated![/bold green]")
    console.print("Copy the following JSON and add it to your MCP server configuration file:")
    console.print(json.dumps(mcp_config, indent=2))

    # Save to file for convenience
    mcp_file = Path.cwd() / "mcp.json"

    if not _check_write_access(mcp_file):
        console.print(
            f"[bold red]Permission denied:[/bold red] Cannot write to {mcp_file}"
        )
        return

    try:
        with open(mcp_file, "w") as f:
            json.dump(mcp_config, f, indent=2)
    except PermissionError:
        console.print(
            f"[bold red]Permission denied while writing:[/bold red] {mcp_file}"
        )
        return
    console.print(f"\n[cyan]Configuration saved to: {mcp_file}[/cyan]")
    
    # Configure IDE automatically
    _configure_ide(mcp_config)
    
    console.print("\n[bold green]✅ MCP Client configuration complete![/bold green]")
    console.print("[cyan]You can now run 'cviz mcp start' to launch the server.[/cyan]")
    console.print("[yellow]💡 Tip: To update MCP config after changing settings, re-run 'cviz mcp setup'[/yellow]\n")









