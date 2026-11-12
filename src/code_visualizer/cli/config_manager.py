# src/code_visualizer/cli/config_manager.py
"""
Configuration management for Code Visualizer.
Handles reading, writing, and validating configuration settings.
Also manages the context system (config.yaml) alongside the existing .env file.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, List
try:
    from rich.console import Console
    from rich.table import Table
    console = Console()
except Exception:
    # Lightweight fallback when 'rich' is not installed (tests or minimal envs)
    class _TableFallback:
        def __init__(self, show_header=True, header_style=None):
            self._cols = []
            self._rows = []

        def add_column(self, name, **_kwargs):
            self._cols.append(name)

        def add_row(self, *cells):
            self._rows.append([str(c) for c in cells])

        def __str__(self) -> str:
            # Simple text table rendering
            out = []
            if self._cols:
                out.append(" | ".join(self._cols))
                out.append("-" * max(10, len(out[0])))
            for r in self._rows:
                out.append(" | ".join(r))
            return "\n".join(out)

    class _ConsoleFallback:
        def print(self, *args, **kwargs):
            # Mimic rich.console.Console.print by delegating to built-in print
            end = kwargs.get("end", "\n")
            sep = kwargs.get("sep", " ")
            for a in args:
                if isinstance(a, _TableFallback):
                    built = str(a)
                    print(built, end=end)
                else:
                    print(a, end=end)

    Table = _TableFallback
    console = _ConsoleFallback()
import os
import yaml


def _atomic_write_text(path: Path, content: str, *, secure: bool = False) -> None:
    """Write *content* to *path* atomically (temp file + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    if secure:
        os.chmod(path, 0o600)

# Configuration file location
CONFIG_DIR = Path.home() / ".code-visualizer"
CONFIG_FILE = CONFIG_DIR / ".env"

# Keys that used to pin the database directory. The database now always lives
# in <repo>/.code-visualizer/ (resolve_context); these keys are ignored and
# never loaded from a project's .env.
DB_PATH_ENV_KEYS = frozenset({"LADYBUGDB_PATH"})

# Database credential keys (stored in same .env file but not managed as config)
# (none: LadybugDB is embedded; kept for the callers that filter on it)
DATABASE_CREDENTIAL_KEYS: frozenset = frozenset()

# Default configuration values
DEFAULT_CONFIG = {
    "DEFAULT_DATABASE": "ladybugdb",
    "INDEX_VARIABLES": "true",
    "ALLOW_DB_DELETION": "false",
    "DEBUG_LOGS": "false",
    "DEBUG_LOG_PATH": str(Path.home() / "mcp_debug.log"),
    "ENABLE_APP_LOGS": "CRITICAL",
    "LIBRARY_LOG_LEVEL": "WARNING",
    "LOG_FILE_PATH": str(CONFIG_DIR / "logs" / "cviz.log"),
    "MAX_FILE_SIZE_MB": "10",
    "IGNORE_TEST_FILES": "false",
    "IGNORE_HIDDEN_FILES": "true",
    "ENABLE_AUTO_WATCH": "false",
    "COMPLEXITY_THRESHOLD": "10",
    "MAX_DEPTH": "unlimited",
    "PARALLEL_WORKERS": "4",
    "CACHE_ENABLED": "true",
    "IGNORE_DIRS": "node_modules,venv,.venv,env,.env,dist,build,target,out,.git,.idea,.vscode,__pycache__",
    "INDEX_SOURCE": "true",
    # SCIP indexer feature flag (default off — existing Tree-sitter behaviour unchanged)
    "SCIP_INDEXER": "false",
    "SCIP_LANGUAGES": "python,typescript,javascript,go,rust,java,dart,cpp,c,csharp",
    "SCIP_LOCAL_INDEXER_TIMEOUT_SECONDS": "300",
    "SKIP_EXTERNAL_RESOLUTION": "false",
    # 0 = unlimited; any positive integer caps MCP tool response size.
    "MAX_TOOL_RESPONSE_TOKENS": "0",
    # 0 = unlimited; any positive integer caps response size in raw characters.
    # When both MAX_TOOL_RESPONSE_TOKENS and MAX_PROMPT_CHARS are set, the
    # stricter (smaller) effective character budget wins.
    "MAX_PROMPT_CHARS": "0",
    # JSON object mapping tool names to integer result-count limits.
    # Example: {"find_code": 20, "analyze_code_relationships": 10, "find_dead_code": 30}
    "TOOL_RESULT_LIMITS": "{}",
    # Post-indexing resolution phases (default off)
    "ENABLE_INHERIT_RESOLVE": "false",
    "ENABLE_VECTOR_RESOLVE": "false",
    "CVIZ_EMBEDDING_MODEL": "local",
    "CVIZ_EMBEDDING_BATCH_SIZE": "256",
    # Default fuzzy matching behavior for `cviz find name` (overridable per-command with --fuzzy/--no-fuzzy)
    "FUZZY_SEARCH": "true",
    "REDACT_SECRETS": "false",
    # Default LLM model names used for graph queries when no value is explicitly configured
    "OPENAI_MODEL": "gpt-4o",
    "ANTHROPIC_MODEL": "claude-3-5-sonnet-20241022",
    # Optional API key for the HTTP API gateway. Empty = auth disabled
    # (backward compatible). When set, HTTP endpoints require the key via
    # `Authorization: Bearer <key>` or `X-API-Key`. May also be set via the
    # CVIZ_API_KEY environment variable (which takes priority).
    "CVIZ_API_KEY": "",
}

# Configuration key descriptions
CONFIG_DESCRIPTIONS = {
    "DEFAULT_DATABASE": "Database backend (only ladybugdb is supported)",
    "INDEX_VARIABLES": "Index variable nodes in the graph (lighter graph if false)",
    "ALLOW_DB_DELETION": "Allow full database deletion commands",
    "DEBUG_LOGS": "Enable debug logging (for development/troubleshooting)",
    "DEBUG_LOG_PATH": "Path to debug log file",
    "ENABLE_APP_LOGS": "Application log level (DEBUG|INFO|WARNING|ERROR|CRITICAL|DISABLED)",
    "LIBRARY_LOG_LEVEL": "Log level for third-party libraries (asyncio, urllib3) (DEBUG|INFO|WARNING|ERROR|CRITICAL)",
    "LOG_FILE_PATH": "Path to application log file",
    "MAX_FILE_SIZE_MB": "Maximum file size to index (in MB)",
    "IGNORE_TEST_FILES": "Skip test files during indexing",
    "IGNORE_HIDDEN_FILES": "Skip hidden files/directories",
    "ENABLE_AUTO_WATCH": "Automatically watch directory after indexing",
    "COMPLEXITY_THRESHOLD": "Cyclomatic complexity warning threshold",
    "MAX_DEPTH": "Maximum directory depth for indexing (unlimited or number)",
    "PARALLEL_WORKERS": "Parallel parse workers for indexing and the file watcher (clamped to 32)",
    "CACHE_ENABLED": "Enable caching for faster re-indexing",
    "IGNORE_DIRS": "Comma-separated list of directory names to ignore during indexing",
    "INDEX_SOURCE": "Store full source code in graph database (for faster indexing use false, for better performance use true)",
    "SCIP_INDEXER": "Use SCIP-based indexing for higher accuracy call/inheritance resolution (requires scip-<lang> tools installed)",
    "SCIP_LANGUAGES": "Comma-separated languages to index via SCIP when SCIP_INDEXER=true (python,typescript,javascript,go,rust,java,dart,cpp,c,csharp)",
    "SCIP_LOCAL_INDEXER_TIMEOUT_SECONDS": "Timeout in seconds for the local SCIP indexer subprocess (default 300). Raise it for large repositories whose indexer runs longer than 5 minutes; a value <= 0 or non-numeric falls back to 300.",
    "SKIP_EXTERNAL_RESOLUTION": "Skip resolution attempts for external library method calls (recommended for enterprise large Java/Spring codebases)",
    "MAX_TOOL_RESPONSE_TOKENS": "Maximum tokens per MCP tool response (0 = unlimited). Truncates oversized payloads and appends a notice.",
    "MAX_PROMPT_CHARS": "Maximum characters per MCP tool response (0 = unlimited). When set alongside MAX_TOOL_RESPONSE_TOKENS the stricter limit wins. Truncated payloads receive a visible [CViz] notice and a stdout warning is emitted.",
    "TOOL_RESULT_LIMITS": "JSON object mapping tool names to max result counts, e.g. {\"find_code\": 20, \"analyze_code_relationships\": 10}. Missing keys use built-in defaults.",
    # Post-indexing resolution phases
    "ENABLE_INHERIT_RESOLVE": (
        "[Phase 5] Re-resolve ambiguous same-file CALLS edges using the inheritance graph (INHERITS relationships). "
        "When enabled, methods called on an interface or abstract class are re-pointed to the correct concrete "
        "implementation based on the class hierarchy, reducing tier-7 fallback edges. "
        "WHEN TO ENABLE: any Java/Kotlin/C# codebase that uses inheritance or interface-based DI (e.g. Spring, OSGi). "
        "PREREQUISITES: run 'cviz index' first so INHERITS edges exist in the graph. No extra tools needed. "
        "COST: adds ~1-5 min per 50K functions at the end of each 'cviz index' run. Safe to toggle on/off — only adds new edges, never removes existing ones."
    ),
    "ENABLE_VECTOR_RESOLVE": (
        "[Phase 4 + Phase 5 tiebreaker] Generate semantic embeddings for all Function nodes and use vector "
        "similarity as a tiebreaker when inheritance resolution alone cannot distinguish between multiple candidates. "
        "Phase 4 writes a 384-dim embedding to every Function node; Phase 5 queries those embeddings during re-resolution. "
        "WHEN TO ENABLE: large codebases (>10K functions) where inheritance alone leaves many ambiguous calls "
        "(tier-7 fallbacks still high after ENABLE_INHERIT_RESOLVE). Also useful for cross-language repos. "
        "PREREQUISITES: (1) fastembed must be installed — run 'pip install fastembed'. "
        "(3) ENABLE_INHERIT_RESOLVE should also be true — vector is a tiebreaker for Phase 5, not a replacement. "
        "COST: Phase 4 takes ~15 min per 50K functions on CPU (first run only; incremental updates are fast). "
        "Embedding model (~40 MB) is downloaded automatically on first use from HuggingFace."
    ),
    "CVIZ_EMBEDDING_MODEL": (
        "Embedding backend for ENABLE_VECTOR_RESOLVE. "
        "'local' uses fastembed (BAAI/bge-small-en-v1.5, 384-dim, runs on CPU, no GPU or API key needed). "
        "'openai' uses OpenAI text-embedding-3-small (requires OPENAI_API_KEY env var, costs money per token). "
        "Default: local"
    ),
    "CVIZ_EMBEDDING_BATCH_SIZE": (
        "Number of function texts to embed per batch when ENABLE_VECTOR_RESOLVE=true. "
        "Larger values are faster but use more RAM. Default: 256. Reduce to 64 if you hit memory errors."
    ),
    "FUZZY_SEARCH": (
        "Enable fuzzy matching by default for `cviz find name` (true|false). "
        "Per-invocation overrides are available via --fuzzy / --no-fuzzy."
    ),
    "REDACT_SECRETS": (
        "Scan string literals and variable values for likely secrets (API keys, tokens, "
        "passwords, connection strings) during indexing and replace them with [REDACTED] "
        "before writing to the graph database. When false (default), secrets are still "
        "detected and a warning is logged, but values are stored verbatim."
    ),
    "OPENAI_MODEL": (
        "Default OpenAI model used for graph queries. "
        "Requires OPENAI_API_KEY environment variable. "
        "Default: gpt-4o"
    ),
    "ANTHROPIC_MODEL": (
        "Default Anthropic model used for graph queries. "
        "Requires ANTHROPIC_API_KEY environment variable. "
        "Default: claude-3-5-sonnet-20241022"
    ),
    "CVIZ_API_KEY": (
        "Optional API key protecting the HTTP API gateway. Empty = "
        "unauthenticated (backward compatible). When set, HTTP endpoints "
        "require it via `Authorization: Bearer <key>` or the `X-API-Key` "
        "header. The CVIZ_API_KEY environment variable overrides this value."
    ),
}

# Valid values for each config key
CONFIG_VALIDATORS = {
    "DEFAULT_DATABASE": ["ladybugdb"],
    "INDEX_VARIABLES": ["true", "false"],
    "ALLOW_DB_DELETION": ["true", "false"],
    "DEBUG_LOGS": ["true", "false"],
    "ENABLE_APP_LOGS": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "DISABLED"],
    "LIBRARY_LOG_LEVEL": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    "IGNORE_TEST_FILES": ["true", "false"],
    "IGNORE_HIDDEN_FILES": ["true", "false"],
    "ENABLE_AUTO_WATCH": ["true", "false"],
    "CACHE_ENABLED": ["true", "false"],
    "INDEX_SOURCE": ["true", "false"],
    "SCIP_INDEXER": ["true", "false"],
    "SKIP_EXTERNAL_RESOLUTION": ["true", "false"],
    "ENABLE_INHERIT_RESOLVE": ["true", "false"],
    "ENABLE_VECTOR_RESOLVE": ["true", "false"],
    "CVIZ_EMBEDDING_MODEL": ["local", "openai"],
    "FUZZY_SEARCH": ["true", "false"],
    "REDACT_SECRETS": ["true", "false"],
}

# Natural-language search of the graph page (semantic/settings.py owns them).
from ..semantic import settings as _semantic_settings  # noqa: E402

DEFAULT_CONFIG.update(_semantic_settings.DEFAULTS)
CONFIG_DESCRIPTIONS.update(_semantic_settings.DESCRIPTIONS)
CONFIG_VALIDATORS.update(_semantic_settings.VALIDATORS)

SUPPORTED_DATABASES: List[str] = CONFIG_VALIDATORS["DEFAULT_DATABASE"]
DATABASE_CLI_HELP = (
    "Database backend ("
    + "|".join(SUPPORTED_DATABASES)
    + "). Defaults to DEFAULT_DATABASE from config."
)

DEFAULT_CVIZIGNORE_PATTERNS = """\
# Default .cvizignore patterns
# Lines starting with # are comments; blank lines are ignored.
# Patterns follow .gitignore syntax.

node_modules/
venv/
.venv/
env/
.env/
dist/
build/
target/
out/
obj/
.terraform/
.terragrunt-cache/
*.lock.hcl
.git/
.idea/
.vscode/
__pycache__/
*.pyc
*.pyo
*.egg-info/
.tox/
.mypy_cache/
.pytest_cache/
coverage/
.next/
*.css
*.scss
*.sass
*.less
*.styl
*.pcss
*.postcss
*.min.js
*.min.css
*.map
"""


def resolve_model_name(provider: str, configured_value: Optional[str] = None) -> str:
    """Get the model it should use for an LLM provider.

    If the user configured a model, it uses that. Otherwise, it falls back to the default
    model for the provider (like OPENAI_MODEL or ANTHROPIC_MODEL).

    Args:
        provider: The name of the provider, like "openai" or "anthropic" (case-insensitive).
        configured_value: The model name from the user's config, if they set one.

    Returns:
        The model name it should use, or an empty string if it doesn't know the provider.
    """
    if configured_value and configured_value.strip():
        return configured_value.strip()
    key = f"{provider.upper()}_MODEL"
    return DEFAULT_CONFIG.get(key, "")


def normalize_config_path(value: str, *, absolute: bool = False, base_dir: Optional[Path] = None) -> str:
    """Normalize config path values.

    - Expands ``~`` and environment variables.
    - Optionally resolves to an absolute path.
    """
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path_obj = Path(expanded)
    if absolute and not path_obj.is_absolute():
        path_obj = (base_dir or Path.cwd()) / path_obj
    if absolute:
        return str(path_obj.resolve())
    return str(path_obj)


def ensure_config_dir(path: Optional[Path] = None):
    """
    Ensure that the configuration directory exists.
    Creates the directory and a logs subdirectory if they do not already exist.
    """
    path = path or CONFIG_DIR
    path.mkdir(parents=True, exist_ok=True)
    (path / "logs").mkdir(parents=True, exist_ok=True)


def ensure_global_cvizignore() -> bool:
    """Create ``~/.code-visualizer/global/.cvizignore`` with sensible defaults
    if it does not already exist.  Returns True when a new file was created."""
    cvizignore_path = CONFIG_DIR / "global" / ".cvizignore"
    if cvizignore_path.exists():
        return False
    cvizignore_path.parent.mkdir(parents=True, exist_ok=True)
    cvizignore_path.write_text(DEFAULT_CVIZIGNORE_PATTERNS)
    return True



# Name of this server's entry in the MCP clients' "mcpServers" (mcp.json).
MCP_SERVER_KEY = "code-visualizer"


def load_config() -> Dict[str, str]:
    """
    Load configuration with priority support.
    Priority order (highest to lowest):
    1. Environment variables (always highest priority)
    2. Local .env file (ONLY in per-repo mode or when CVIZ_LOAD_PROJECT_ENV=1)
    3. Global ~/.code-visualizer/.env
    
    BUG FIX: In global/named context mode, local repo .env files are now properly ignored
    to prevent silent database redirection when working inside cloned repositories.
    
    Note: Does NOT create config directory - caller must call ensure_config_dir() first if needed.
    """
    # Start with defaults
    config = DEFAULT_CONFIG.copy()
    
    # Load global config
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        config[key.strip()] = value.strip()
        except Exception as e:
            console.print(f"[red]Error loading global config: {e}[/red]")
    
    # Load local .env file ONLY if should_apply_project_dotenv() returns True
    # This respects context mode (per-repo vs global/named) and environment overrides
    local_env = find_local_env()
    if local_env and local_env.exists():
        try:
            with open(local_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()
                        
                        # In per-repo mode: allow all config keys to be overridden
                        # In global/named mode: local .env should not be loaded at all (find_local_env returns None)
                        # But if it somehow gets through, only allow non-DB-path keys for safety
                        if key in DB_PATH_ENV_KEYS:
                            # CRITICAL: Never let local .env override DB paths in global mode
                            # This prevents silent database redirection
                            continue
                        
                        if key in DEFAULT_CONFIG or key in DATABASE_CREDENTIAL_KEYS:
                            config[key] = value
        except Exception as e:
            console.print(f"[yellow]Warning: Error loading local .env: {e}[/yellow]")
    
    # Environment variables have highest priority - always override everything
    for key in DEFAULT_CONFIG.keys():
        env_value = os.getenv(key)
        if env_value is not None:
            config[key] = env_value
    
    # Also check for database credential environment variables
    for key in DATABASE_CREDENTIAL_KEYS:
        env_value = os.getenv(key)
        if env_value is not None:
            config[key] = env_value
    
    return config


def should_apply_project_dotenv() -> bool:
    """True when the cwd-local ``.code-visualizer/.env`` should merge with the global config.

    Every repository has its own ``.code-visualizer/``, so its settings apply
    by default. Set ``CVIZ_IGNORE_PROJECT_ENV=1`` to skip them
    (``CVIZ_LOAD_PROJECT_ENV=1`` is kept for compatibility).
    """
    if os.getenv("CVIZ_IGNORE_PROJECT_ENV", "").strip().lower() in ("1", "true", "yes"):
        return False
    if os.getenv("CVIZ_LOAD_PROJECT_ENV", "").strip().lower() in ("1", "true", "yes"):
        return True
    return True


def find_local_env() -> Optional[Path]:
    """Find project configuration in the current directory only.

    Prefer ``.code-visualizer/.env`` to ``.env``. Parent directories are
    deliberately excluded, just as they are for local context resolution:
    starting CViz in one project must not import another project's settings.
    """
    if not should_apply_project_dotenv():
        return None

    current = Path.cwd()
    for candidate in (current / ".code-visualizer" / ".env", current / ".env"):
        if candidate.is_file() and candidate != CONFIG_FILE:
            return candidate
    return None


def code_visualizer_dotenv_at_cwd(cwd: Optional[Path] = None) -> Optional[Path]:
    """
    Return ``<cwd>/.code-visualizer/.env`` if that file exists, else None.

    *cwd* defaults to ``Path.cwd()``. Parent directories are **not** searched—same rule as
    local context resolution (``find_local_cviz_dir``).
    """
    root = (cwd or Path.cwd()).resolve()
    candidate = root / ".code-visualizer" / ".env"
    return candidate if candidate.exists() else None


def save_config(config: Dict[str, str], preserve_db_credentials: bool = True):
    """
    Save configuration to file.
    If preserve_db_credentials is True, existing database credentials will be preserved.
    If preserve_db_credentials is False, credentials from config dict will be written.
    """
    ensure_config_dir()
    
    # Determine which credentials to write
    credentials_to_write = {}
    
    if preserve_db_credentials and CONFIG_FILE.exists():
        # Load existing credentials from file to preserve them
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        if key in DATABASE_CREDENTIAL_KEYS:
                            credentials_to_write[key] = value.strip()
        except Exception:
            pass
        # Merge credentials from the config dict (handles both new and updated values)
        for key in DATABASE_CREDENTIAL_KEYS:
            if key in config:
                credentials_to_write[key] = config[key]
    else:
        # Use credentials from the config dict being passed in
        for key in DATABASE_CREDENTIAL_KEYS:
            if key in config:
                credentials_to_write[key] = config[key]
    
    try:
        lines = [
            "# Code Visualizer Configuration",
            f"# Location: {CONFIG_FILE}",
            "",
        ]
        if credentials_to_write:
            lines.append("# ===== Database Credentials =====")
            for key in sorted(DATABASE_CREDENTIAL_KEYS):
                if key in credentials_to_write:
                    lines.append(f"{key}={credentials_to_write[key]}")
            lines.append("")
        lines.append("# ===== Configuration Settings =====")
        for key, value in sorted(config.items()):
            if key in DATABASE_CREDENTIAL_KEYS:
                continue
            description = CONFIG_DESCRIPTIONS.get(key, "")
            if description:
                lines.append(f"# {description}")
            lines.append(f"{key}={value}")
            lines.append("")
        _atomic_write_text(CONFIG_FILE, "\n".join(lines), secure=True)
        console.print(f"[green]✅ Configuration saved to {CONFIG_FILE}[/green]")
    except Exception as e:
        console.print(f"[red]Error saving config: {e}[/red]")


def validate_config_value(key: str, value: str) -> tuple[bool, Optional[str]]:
    """
    Validate a configuration value.
    Returns (is_valid, error_message)
    """
    # Skip validation for database credentials (they have their own validation elsewhere)
    if key in DATABASE_CREDENTIAL_KEYS:
        return True, None
    
    # Strip quotes that might be in the value
    value = value.strip().strip("'\"")
    
    # Check if key exists
    if key not in DEFAULT_CONFIG:
        available_keys = ", ".join(sorted(DEFAULT_CONFIG.keys()))
        return False, f"Unknown config key: {key}. Available keys: {available_keys}"
    
    # Validate against specific validators if they exist
    if key in CONFIG_VALIDATORS:
        valid_values = CONFIG_VALIDATORS[key]
        if value.lower() not in [v.lower() for v in valid_values]:
            return False, f"Invalid value for {key}. Must be one of: {', '.join(valid_values)}"
    
    # Specific validation for numeric values
    if key == "MAX_FILE_SIZE_MB":
        try:
            size = int(value)
            if size <= 0:
                return False, "MAX_FILE_SIZE_MB must be a positive number"
        except ValueError:
            return False, "MAX_FILE_SIZE_MB must be a number"
    
    if key == "COMPLEXITY_THRESHOLD":
        try:
            threshold = int(value)
            if threshold <= 0:
                return False, "COMPLEXITY_THRESHOLD must be a positive number"
        except ValueError:
            return False, "COMPLEXITY_THRESHOLD must be a number"
    
    if key == "PARALLEL_WORKERS":
        try:
            workers = int(value)
            if workers <= 0 or workers > 32:
                return False, "PARALLEL_WORKERS must be between 1 and 32"
        except ValueError:
            return False, "PARALLEL_WORKERS must be a number"

    if key == "MAX_TOOL_RESPONSE_TOKENS":
        try:
            limit = int(value)
            if limit < 0:
                return False, "MAX_TOOL_RESPONSE_TOKENS must be 0 (unlimited) or a positive integer"
        except ValueError:
            return False, "MAX_TOOL_RESPONSE_TOKENS must be an integer (0 = unlimited)"

    if key == "MAX_PROMPT_CHARS":
        try:
            limit = int(value)
            if limit < 0:
                return False, "MAX_PROMPT_CHARS must be 0 (unlimited) or a positive integer"
        except ValueError:
            return False, "MAX_PROMPT_CHARS must be an integer (0 = unlimited)"

    if key == "TOOL_RESULT_LIMITS":
        import json as _json
        try:
            parsed = _json.loads(value)
            if not isinstance(parsed, dict):
                return False, "TOOL_RESULT_LIMITS must be a JSON object, e.g. {\"find_code\": 20}"
            for k, v in parsed.items():
                if not isinstance(v, int) or v < 1:
                    return False, f"TOOL_RESULT_LIMITS: value for '{k}' must be a positive integer"
        except _json.JSONDecodeError:
            return False, "TOOL_RESULT_LIMITS must be valid JSON, e.g. {\"find_code\": 20, \"find_dead_code\": 30}"
    
    if key == "MAX_DEPTH":
        if value.lower() != "unlimited":
            try:
                depth = int(value)
                if depth <= 0:
                    return False, "MAX_DEPTH must be 'unlimited' or a positive number"
            except ValueError:
                return False, "MAX_DEPTH must be 'unlimited' or a number"
    
    if key in ("LOG_FILE_PATH", "DEBUG_LOG_PATH"):
        # Validate path is writable
        log_path = Path(normalize_config_path(value, absolute=True))
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, f"Cannot create log directory: {e}"
    
    return True, None


def get_config_value(key: str) -> Optional[str]:
    """Get a specific configuration value."""
    config = load_config()
    return config.get(key)


def is_db_deletion_allowed() -> bool:
    """True when destructive delete/clear operations are permitted."""
    return str(get_config_value("ALLOW_DB_DELETION") or "false").strip().lower() == "true"


def set_config_value(key: str, value: str) -> bool:
    """Set a configuration value. Returns True if successful."""

    # Ensure config directory exists
    ensure_config_dir()
    
    # Validate
    is_valid, error_msg = validate_config_value(key, value)
    if not is_valid:
        console.print(f"[red]❌ {error_msg}[/red]")
        return False
    
    # Load, update, and save
    config = load_config()
    config[key] = value
    save_config(config)
    
    console.print(f"[green]✅ Set {key} = {value}[/green]")

    # Surface a missing embedding backend at the moment of intent, not after
    # an index run that silently generated nothing (#1597).
    if key == "ENABLE_VECTOR_RESOLVE" and value.lower() == "true":
        try:
            from code_visualizer.tools.indexing.embeddings import probe_embedding_backend
            ok, detail = probe_embedding_backend()
            if not ok:
                console.print(
                    f"[bold yellow]⚠ Vector resolve is enabled but cannot run yet: "
                    f"{detail}. Indexing will proceed without embeddings until "
                    f"this is installed.[/bold yellow]"
                )
        except Exception:
            pass
    return True


def reset_config():
    """Reset configuration to defaults (preserves database credentials)."""
    import shutil
    from datetime import datetime

    ensure_config_dir()
    if CONFIG_FILE.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.{stamp}.bak")
        shutil.copy2(CONFIG_FILE, backup)
        console.print(f"[dim]Backed up current config to {backup}[/dim]")
    save_config(DEFAULT_CONFIG.copy(), preserve_db_credentials=True)
    console.print("[green]✅ Configuration reset to defaults[/green]")
    console.print("[cyan]Note: Database credentials were preserved[/cyan]")



_FIRST_RUN_MARKER = CONFIG_DIR / ".first_run_done"


def _print_welcome_banner() -> None:
    """Print a one-time welcome message explaining where the graphs live."""
    console.print()
    console.print("[bold green]Welcome to Code Visualizer![/bold green]")
    console.print()
    console.print("Each repository has its own code graph, in its [cyan].code-visualizer/[/cyan] folder")
    console.print("(never versioned). Run CViz from the repository, or pass its path:")
    console.print("  [dim]cd my-repo && cviz graph --watch .[/dim]")
    console.print()


def ensure_first_run_bootstrap(show_welcome: bool = False) -> bool:
    """Run one-time setup for brand-new installs.

    Creates default config files and the global .cvizignore silently.
    Returns True when bootstrap was performed.
    """
    if _FIRST_RUN_MARKER.exists():
        return False

    ensure_config_dir()
    ensure_global_cvizignore()
    if show_welcome:
        _print_welcome_banner()

    _FIRST_RUN_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _FIRST_RUN_MARKER.write_text("1")
    return True


def ensure_config_file():
    """
    Create default .env config file on first run if it does not exist.
    """
    ensure_config_dir()

    if CONFIG_FILE.exists():
        return False  # file already exists

    save_config(DEFAULT_CONFIG.copy(), preserve_db_credentials=False)
    return True  # file was created




def show_config():
    """Display current configuration in a nice table."""
    created = ensure_config_file()
    if created:
        console.print(
            f"[green]🆕 Created default configuration at {CONFIG_FILE}[/green]\n"
        )
    config = load_config()
    
    # Separate database credentials from configuration
    db_creds = {k: v for k, v in config.items() if k in DATABASE_CREDENTIAL_KEYS}
    config_settings = {k: v for k, v in config.items() if k not in DATABASE_CREDENTIAL_KEYS}
    
    # Show database credentials if they exist
    if db_creds:
        console.print("\n[bold cyan]Database Credentials[/bold cyan]")
        db_table = Table(show_header=True, header_style="bold magenta")
        db_table.add_column("Credential", style="cyan", width=20)
        db_table.add_column("Value", style="green", width=30)
        
        for key in sorted(db_creds.keys()):
            value = db_creds[key]
            # Mask password
            if "PASSWORD" in key:
                value = "********" if value else "(not set)"  
            db_table.add_row(key, value)
        
        console.print(db_table)
    
    # Show configuration settings
    console.print("\n[bold cyan]Configuration Settings[/bold cyan]")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Setting", style="cyan", width=25)
    table.add_column("Value", style="green", width=20)
    table.add_column("Description", style="dim", width=50)
    
    for key in sorted(config_settings.keys()):
        value = config_settings[key]
        description = CONFIG_DESCRIPTIONS.get(key, "")

        # Never print secret-like values (e.g. the HTTP API key) in plaintext.
        if "API_KEY" in key.upper() and value:
            value = "********"

        # Highlight non-default values
        if value != DEFAULT_CONFIG.get(key):
            value_style = "[bold yellow]" + value + "[/bold yellow]"
        else:
            value_style = value

        table.add_row(key, value_style, description)
    
    console.print(table)
    console.print(f"\n[cyan]Config file: {CONFIG_FILE}[/cyan]")


# =============================================================================
# CONTEXT SYSTEM  (config.yaml)
# =============================================================================

CONTEXT_CONFIG_FILE = CONFIG_DIR / "config.yaml"

# Valid mode values
VALID_MODES = ["global", "per-repo", "named"]


@dataclass
class ResolvedContext:
    """Result of resolve_context() — everything needed to instantiate the DB."""
    mode: str             # always "per-repo": one database per repository
    context_name: str     # kept for compatibility, always ""
    database: str         # ladybugdb
    db_path: str          # <repo>/.code-visualizer/db/ladybugdb
    cvizignore_path: str   # <repo>/.code-visualizer/.cvizignore
    is_local: bool = True
    repo_root: str = ""   # the repository the database belongs to


class NotInAProjectError(ValueError):
    """CViz was run outside of any project folder (e.g. the home directory)."""


LOCAL_DIR_NAME = ".code-visualizer"


def find_local_cviz_dir(start: Optional[Path] = None) -> Optional[Path]:
    """The ``.code-visualizer/`` of *start* or of its nearest parent that has one.

    The global ``~/.code-visualizer`` (configuration) is never a repository's.
    """
    current = Path(start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    from .server_registry import is_user_config_dir

    global_dir = CONFIG_DIR.resolve()
    for directory in (current, *current.parents):
        candidate = directory / LOCAL_DIR_NAME
        if candidate.is_dir() and candidate.resolve() != global_dir and not is_user_config_dir(candidate):
            return candidate
    return None


def find_repo_root(start: Optional[Path] = None) -> Path:
    """Repository that *start* (a file or folder) belongs to.

    1. the nearest folder that already has a ``.code-visualizer/``;
    2. else the git work tree root (``.git`` directory or worktree file);
    3. else *start* itself (its folder, for a file).
    """
    current = Path(start or Path.cwd()).resolve()
    if current.is_file() or (not current.exists() and current.suffix):
        current = current.parent
    local = find_local_cviz_dir(current)
    if local is not None:
        return local.parent
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return directory
    return current


def _init_local_dir(local: Path) -> None:
    """Create ``<repo>/.code-visualizer/`` with its own ``.gitignore``.

    The ``*`` pattern keeps the whole folder (database, settings) out of git
    without touching the project's own ``.gitignore``.
    """
    created = not local.exists()
    (local / "db").mkdir(parents=True, exist_ok=True)
    ignore = local / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# Code Visualizer index of this repository: not versioned\n*\n", encoding="utf-8")
    if created:
        console.print(f"[dim]Created the CViz index folder of this repository: {local}[/dim]")


def resolve_context(
    cli_context: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> ResolvedContext:
    """The database of the repository containing *cwd* (default: the current folder).

    CViz keeps one database per repository, in ``<repo>/.code-visualizer/``
    (see :func:`find_repo_root`). Pass the path a command works on as *cwd*
    so that, e.g., ``cviz graph --watch ~/proj`` uses ``~/proj``'s database
    wherever it is run from. ``CVIZ_RUNTIME_DB_PATH`` (``--path``) overrides
    the database location. *cli_context* is accepted for compatibility and
    ignored: named and global contexts no longer exist.
    """
    start = Path(cwd or Path.cwd()).resolve()
    database = "ladybugdb"

    override = os.environ.get("CVIZ_RUNTIME_DB_PATH")
    if override:
        root = find_repo_root(start)
        return ResolvedContext(
            mode="per-repo", context_name="", database=database,
            db_path=str(Path(override).expanduser()),
            cvizignore_path=str(root / LOCAL_DIR_NAME / ".cvizignore"), repo_root=root.as_posix(),
        )

    local = find_local_cviz_dir(start)
    if local is None:
        mapping = get_workspace_mapping(start)
        if mapping and Path(mapping["context_path"]).is_dir():
            local = Path(mapping["context_path"])
    if local is None:
        root = find_repo_root(start)
        home = Path.home().resolve()
        if root == home or (root / LOCAL_DIR_NAME).resolve() == CONFIG_DIR.resolve() or root == Path(root.anchor):
            raise NotInAProjectError(
                f"{start} is not inside a project folder. Run CViz from (or pass the path of) "
                "the repository to index: its database is kept in <repo>/.code-visualizer/."
            )
        local = root / LOCAL_DIR_NAME
    _init_local_dir(local)
    return ResolvedContext(
        mode="per-repo", context_name="", database=database,
        db_path=str(local / "db" / database),
        cvizignore_path=str(local / ".cvizignore"),
        repo_root=local.parent.resolve().as_posix(),
    )


@dataclass
class DiscoveredContext:
    """A .code-visualizer folder found in a child directory."""
    path: str            # absolute path to the parent repo directory
    cviz_path: str        # absolute path to the .code-visualizer directory
    repo_name: str       # name of the parent directory
    database: str        # backend from local config.yaml, or default
    db_path: str         # resolved db path
    cvizignore_path: str  # path to .cvizignore if present


def discover_child_contexts(
    start: Optional[Path] = None,
    max_depth: int = 1,
) -> List[DiscoveredContext]:
    """Walk child directories of *start* up to *max_depth* levels looking for
    ``.code-visualizer/`` folders that represent per-repo databases.

    Returns a list of :class:`DiscoveredContext` for each match found.
    The global ``~/.code-visualizer`` is always excluded.
    """
    start = (start or Path.cwd()).resolve()
    global_dir = CONFIG_DIR.resolve()
    results: List[DiscoveredContext] = []

    def _scan(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(directory.iterdir())
        except PermissionError:
            return
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            candidate = entry / ".code-visualizer"
            if candidate.exists() and candidate.is_dir() and candidate.resolve() != global_dir:
                local_db = "ladybugdb"
                local_yaml = candidate / "config.yaml"
                if local_yaml.exists():
                    try:
                        with open(local_yaml, encoding="utf-8") as f:
                            raw = yaml.safe_load(f) or {}
                        local_db = raw.get("database", "ladybugdb")
                    except Exception:
                        pass
                results.append(DiscoveredContext(
                    path=str(entry),
                    cviz_path=str(candidate),
                    repo_name=entry.name,
                    database=local_db,
                    db_path=str(candidate / "db" / local_db),
                    cvizignore_path=str(candidate / ".cvizignore"),
                ))
            if depth < max_depth:
                _scan(entry, depth + 1)

    _scan(start, 1)
    return results


# =============================================================================
# WORKSPACE MAPPINGS  (global persistence of CWD -> context path)
# =============================================================================

def _load_workspace_mappings() -> Dict[str, Dict[str, str]]:
    """Load the ``workspace_mappings`` section from config.yaml."""
    if not CONTEXT_CONFIG_FILE.exists():
        return {}
    try:
        with open(CONTEXT_CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("workspace_mappings", {}) or {}
    except Exception:
        return {}


def _save_workspace_mappings(mappings: Dict[str, Dict[str, str]]) -> None:
    """Write *mappings* back into the ``workspace_mappings`` key of config.yaml,
    preserving all other keys."""
    ensure_config_dir()
    raw: Dict[str, Any] = {}
    if CONTEXT_CONFIG_FILE.exists():
        try:
            with open(CONTEXT_CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            raw = {}
    raw["workspace_mappings"] = mappings
    try:
        _atomic_write_text(
            CONTEXT_CONFIG_FILE,
            yaml.dump(raw, default_flow_style=False, sort_keys=False),
        )
    except Exception as e:
        console.print(f"[red]Error saving workspace mappings: {e}[/red]")


def get_workspace_mapping(cwd: Path) -> Optional[Dict[str, str]]:
    """Look up a saved workspace mapping for *cwd*.

    Returns a dict with ``context_path`` and ``database`` keys, or None.
    """
    mappings = _load_workspace_mappings()
    return mappings.get(str(cwd.resolve()))


def save_workspace_mapping(cwd: Path, context_path: Path) -> None:
    """Persist an association from *cwd* to a ``.code-visualizer`` directory."""
    context_path = context_path.resolve()
    local_db = "ladybugdb"
    local_yaml = context_path / "config.yaml"
    if local_yaml.exists():
        try:
            with open(local_yaml, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            local_db = raw.get("database", "ladybugdb")
        except Exception:
            pass

    mappings = _load_workspace_mappings()
    mappings[str(cwd.resolve())] = {
        "context_path": str(context_path),
        "database": local_db,
    }
    _save_workspace_mappings(mappings)


def remove_workspace_mapping(cwd: Path) -> bool:
    """Delete a saved workspace mapping. Returns True if one was removed."""
    mappings = _load_workspace_mappings()
    key = str(cwd.resolve())
    if key in mappings:
        del mappings[key]
        _save_workspace_mappings(mappings)
        return True
    return False


def list_workspace_mappings() -> Dict[str, Dict[str, str]]:
    """Return all saved workspace mappings."""
    return _load_workspace_mappings()
