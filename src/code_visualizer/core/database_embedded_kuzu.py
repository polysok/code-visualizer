# src/code_visualizer/core/database_embedded_kuzu.py
"""
Shared embedded graph-database layer for Kùzu-dialect engines (KùzuDB, LadybugDB).

Backend-specific modules (``database_kuzu``, ``database_ladybug``) supply an
``EmbeddedBackendSpec`` and thin manager subclass; all Cypher translation,
connection pooling, and schema logic lives here.
"""
import gc
import importlib
import os
import queue
import time
import threading
import warnings
import re
import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List, ClassVar

from code_visualizer.core.graph_query import GraphQueryInterface
from ..utils.cypher_ddl import is_schema_ddl
from code_visualizer.utils.debug_log import debug_log, info_logger, error_logger, warning_logger

# Default cap for ladybug/kuzu buffer pools (bytes). Both libraries otherwise
# default to ~80% of system RAM, which lets long-running gateways grow huge.
DEFAULT_EMBEDDED_BUFFER_POOL_BYTES = 4 * 1024**3

# Floor for the availability-derived default, so a briefly-busy machine does
# not shrink the pool into pathological territory.
MIN_DEFAULT_BUFFER_POOL_BYTES = 256 * 1024**2


def _cgroup_available_bytes():
    """Memory headroom under the container's cgroup limit, or None when
    unconfined. /proc/meminfo shows the HOST inside a container, so a pod
    capped at 1 GiB on a 64 GiB host would otherwise size a 4 GiB pool and
    be OOM-killed natively at first touch."""
    # cgroup v2 (unified hierarchy)
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            raw = f.read().strip()
        if raw != "max":
            limit = int(raw)
            usage = 0
            try:
                with open("/sys/fs/cgroup/memory.current") as f:
                    usage = int(f.read().strip())
            except (OSError, ValueError):
                pass
            return max(limit - usage, 0)
    except (OSError, ValueError):
        pass
    # cgroup v1
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
            limit = int(f.read().strip())
        if limit < 1 << 60:  # v1 reports ~8 EiB when unlimited
            usage = 0
            try:
                with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as f:
                    usage = int(f.read().strip())
            except (OSError, ValueError):
                pass
            return max(limit - usage, 0)
    except (OSError, ValueError):
        pass
    return None


def _available_memory_bytes():
    """The tighter of host MemAvailable (/proc/meminfo) and the cgroup
    memory headroom, or None where neither is readable (macOS…)."""
    host = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    host = int(line.split()[1]) * 1024
                    break
    except OSError:
        pass
    cgroup = _cgroup_available_bytes()
    if host is not None and cgroup is not None:
        return min(host, cgroup)
    return host if host is not None else cgroup


def resolve_embedded_buffer_pool_size() -> int:
    """
    Resolve ``buffer_pool_size`` for embedded ``Database(...)`` constructors.

    Reads ``CVIZ_EMBEDDED_BUFFER_POOL_MB`` (integer MiB). Unset uses an
    adaptive default: 4 GiB, capped at half of currently-available memory
    (floored at 256 MiB) — on a memory-constrained host (CI runners sharing
    RAM with other containers, small VMs) a fixed 4 GiB pool made LadybugDB
    fault natively (SIGSEGV, parity run exit -11) when the reservation could
    not be backed. An explicit env value is always honored as-is; ``0`` opts
    back into the library default (~80% of system memory).
    """
    raw = os.getenv("CVIZ_EMBEDDED_BUFFER_POOL_MB")
    if raw is None or str(raw).strip() == "":
        available = _available_memory_bytes()
        if available is None:
            return DEFAULT_EMBEDDED_BUFFER_POOL_BYTES
        adaptive = max(MIN_DEFAULT_BUFFER_POOL_BYTES, available // 2)
        return min(DEFAULT_EMBEDDED_BUFFER_POOL_BYTES, adaptive)
    try:
        mb = int(str(raw).strip())
    except ValueError:
        warning_logger(
            f"Invalid CVIZ_EMBEDDED_BUFFER_POOL_MB={raw!r}; "
            f"using default {DEFAULT_EMBEDDED_BUFFER_POOL_BYTES} bytes"
        )
        return DEFAULT_EMBEDDED_BUFFER_POOL_BYTES
    if mb < 0:
        warning_logger(
            f"Invalid CVIZ_EMBEDDED_BUFFER_POOL_MB={raw!r}; "
            f"using default {DEFAULT_EMBEDDED_BUFFER_POOL_BYTES} bytes"
        )
        return DEFAULT_EMBEDDED_BUFFER_POOL_BYTES
    if mb == 0:
        return 0
    return mb * 1024**2


@dataclass(frozen=True)
class EmbeddedBackendSpec:
    """Configuration for a Kùzu-dialect embedded graph backend."""

    backend_id: str
    python_module: str
    display_name: str
    path_env_var: str
    config_key: str
    default_dir: str
    install_hint: str


class EmbeddedCompatState:
    """
    Shared fail-fast state for unsupported query types (fulltext/module_deps).
    Lives on the manager (long-lived) so that disabling a query type sticks
    across sessions instead of resetting with every new session wrapper.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.disabled_query_types = set()
        self.logged_disabled_query_types = set()


class EmbeddedGraphManager(GraphQueryInterface):
    """
    Manages an embedded Kùzu-dialect database connection as a per-subclass singleton.
    """

    BACKEND_SPEC: ClassVar[EmbeddedBackendSpec]

    _instance = None
    _db = None
    _pool = None
    _lock = threading.Lock()         # Guards singleton initialisation only.
    _write_lock = threading.RLock()  # Serialises every write query (reentrant for fallback).
    _query_lock = threading.RLock()  # Kept for backward compat / legacy wrappers if any.
    _compat_state = EmbeddedCompatState()  # Shared across all sessions of this process.

    def __new__(cls, *args, **kwargs):
        """Standard singleton pattern implementation."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(EmbeddedGraphManager, cls).__new__(cls)
        return cls._instance

    def __init__(self, db_path: Optional[str] = None):
        """
        Initializes the manager with default database path or explicit overrides.
        """
        spec = self.BACKEND_SPEC
        new_db_path = db_path or os.getenv("CVIZ_RUNTIME_DB_PATH")
        if not new_db_path:
            # One database per repository: the one of the current folder.
            from code_visualizer.cli.config_manager import resolve_context

            new_db_path = resolve_context().db_path

        if hasattr(self, '_initialized') and getattr(self, 'db_path', None) == new_db_path:
            return

        if hasattr(self, '_initialized') and getattr(self, 'db_path', None) != new_db_path:
            self.close_driver()

        self._initialized = False
        self.name = spec.backend_id
        self.db_path = new_db_path
        os.makedirs(Path(self.db_path).parent, exist_ok=True)
        self._initialized = True

    def get_driver(self):
        """Gets the embedded driver. Initialises the database and connection pool."""
        spec = self.BACKEND_SPEC
        if self._db is None:
            with self._lock:
                if self._db is None:
                    backend = importlib.import_module(spec.python_module)
                    self._set_aside_unreplayable_wal(spec)
                    max_retries = 5
                    for attempt in range(max_retries):
                        try:
                            buffer_pool_size = resolve_embedded_buffer_pool_size()
                            if buffer_pool_size == 0:
                                pool_msg = "library default (~80% of system memory)"
                            else:
                                pool_msg = f"{buffer_pool_size} bytes"
                            info_logger(
                                f"Initializing {spec.display_name} at {self.db_path} "
                                f"(buffer_pool_size={pool_msg})"
                            )
                            if buffer_pool_size == 0:
                                # 0 means "library default": omit the kwarg
                                # rather than trusting every backend to treat
                                # a literal 0 that way.
                                self._db = backend.Database(self.db_path)
                            else:
                                self._db = backend.Database(
                                    self.db_path, buffer_pool_size=buffer_pool_size
                                )

                            # Initialise connection pool
                            self._pool = queue.Queue()
                            # Start with 10 connections in the pool
                            for _ in range(10):
                                self._pool.put(backend.Connection(self._db))
                            
                            # Use one connection from the pool to initialise schema
                            temp_conn = self._pool.get()
                            try:
                                self._conn = temp_conn # Temporary assignment for _initialize_schema
                                self._load_index_extensions()
                                self._initialize_schema()
                                self._conn = None
                            finally:
                                self._pool.put(temp_conn)

                            info_logger(f"{spec.display_name} connection established and schema verified")
                            break
                        except ImportError:
                            error_logger(f"{spec.display_name} is not installed. {spec.install_hint}")
                            raise ValueError(f"{spec.display_name} core missing.")
                        except Exception as e:
                            if "lock" in str(e).lower() and attempt < max_retries - 1:
                                wait = 0.5 * (2 ** attempt)
                                warning_logger(
                                    f"{spec.display_name} lock contention, retrying in {wait:.1f}s "
                                    f"({attempt+1}/{max_retries})..."
                                )
                                self._db = None
                                time.sleep(wait)
                            else:
                                error_logger(f"Failed to initialize {spec.display_name}: {e}")
                                raise

        return EmbeddedDriverWrapper(
            self._db,
            self._pool,
            self._write_lock,
            compat_state=type(self)._compat_state,
            display_name=spec.display_name,
            backend_id=spec.backend_id,
        )

    # Opens the database in a throw-away process: replaying an unreplayable
    # WAL can crash the interpreter (segfault), which no try/except catches.
    _WAL_PROBE = (
        "import sys, importlib\n"
        "lib = importlib.import_module(sys.argv[2])\n"
        "db = lib.Database(sys.argv[1], buffer_pool_size=256 * 1024 * 1024)\n"
        "lib.Connection(db).execute('RETURN 1')\n"
        "db.close()\n"
    )

    def _set_aside_unreplayable_wal(self, spec) -> None:
        """Recover a database whose WAL cannot be replayed.

        LadybugDB replays the WAL when it opens the database, before any
        extension can be loaded. A WAL holding writes to the search indexes
        (fts / vector extensions) left by a process that did not close the
        database cleanly (crash, killed terminal) is then unreplayable: the
        open fails ("Corrupted wal file") or crashes. A WAL left on disk means
        exactly that kind of unclean exit, so it is tested in a separate
        process; when it cannot be replayed it is renamed (kept, not deleted)
        and the database restarts from its last checkpoint. The files changed
        since are found again by their content fingerprints (cviz update, the
        watcher's start-up catch-up).
        """
        import subprocess
        import sys

        wal = Path(f"{self.db_path}.wal")
        if not wal.exists():
            return
        try:
            probe = subprocess.run(
                [sys.executable, "-c", self._WAL_PROBE, str(self.db_path), spec.python_module],
                capture_output=True, text=True, timeout=300,
            )
        except Exception as exc:  # noqa: BLE001 - cannot probe: open normally
            debug_log(f"WAL probe skipped: {exc}")
            return
        if probe.returncode == 0 or "lock" in (probe.stderr or "").lower():
            return  # replayable, or held by another process (reported by the open)
        if not wal.exists():
            return
        backup = wal.with_name(f"{wal.name}.unreplayable-{time.strftime('%Y%m%d-%H%M%S')}")
        wal.rename(backup)
        detail = (probe.stderr or "").strip().splitlines()[-1:] or [f"exit code {probe.returncode}"]
        message = (
            f"{spec.display_name}: the write-ahead log of {self.db_path} could not be replayed "
            f"({detail[0]}); it was moved to {backup.name} and the database restarts from its "
            "last checkpoint. Run 'cviz update' (or restart 'cviz graph --watch') to re-apply "
            "the files changed since."
        )
        warning_logger(message)
        print(f"Warning: {message}", file=sys.stderr)

    def checkpoint(self) -> None:
        """Flush the WAL into the database file (best effort).

        Done after every write operation: a WAL holding search-index writes
        cannot be replayed if the process then dies without closing the
        database (see _set_aside_unreplayable_wal). ~30 ms.
        """
        if self._db is None:
            return
        try:
            with self.get_driver().session() as session:
                session.run_native("CHECKPOINT")
        except Exception as exc:  # noqa: BLE001
            debug_log(f"CHECKPOINT failed: {exc}")

    def _initialize_schema(self):
        """Creates Node and Rel tables if they don't exist."""
        # Using a set of helper methods to define tables
        # Kuzu's Cypher for checking tables can be limited, 
        # but we can wrap in try-except or check metadata.
        
        node_tables = [
            ("Repository", "path STRING, name STRING, is_dependency BOOLEAN, indexed_at STRING, commit_hash STRING, PRIMARY KEY (path)"),
            ("File", "path STRING, name STRING, relative_path STRING, package_name STRING, language STRING, is_dependency BOOLEAN, content_hash STRING, PRIMARY KEY (path)"),
            ("Directory", "path STRING, name STRING, PRIMARY KEY (path)"),
            ("Module", "name STRING, lang STRING, full_import_name STRING, path STRING, line_number INT64, PRIMARY KEY (name)"),
            # For types with composite keys (name, path, line_number), we use a 'uid'
            ("Function", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, cyclomatic_complexity INT64, context STRING, context_type STRING, class_context STRING, class_context_line INT64, module_context STRING, is_dependency BOOLEAN, decorators STRING[], args STRING[], arg_types STRING[], http_method STRING, http_path STRING, embedding DOUBLE[], visibility STRING, modifiers STRING[], is_composable BOOLEAN, PRIMARY KEY (uid)"),
            ("Class", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, node_type STRING, is_dependency BOOLEAN, decorators STRING[], visibility STRING, modifiers STRING[], PRIMARY KEY (uid)"),
            ("Variable", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, source STRING, docstring STRING, lang STRING, value STRING, context STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("Trait", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("Interface", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, decorators STRING[], visibility STRING, modifiers STRING[], PRIMARY KEY (uid)"),
            ("Macro", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("Struct", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("Enum", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("EnumMember", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, lang STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("Union", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("Annotation", "uid STRING, name STRING, path STRING, line_number INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("Record", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("Property", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("Parameter", "uid STRING, name STRING, path STRING, function_line_number INT64, PRIMARY KEY (uid)"),
            ("Mixin", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("Extension", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, PRIMARY KEY (uid)"),
            ("Object", "uid STRING, name STRING, path STRING, line_number INT64, occurrence_index INT64, end_line INT64, source STRING, docstring STRING, lang STRING, is_dependency BOOLEAN, decorators STRING[], visibility STRING, modifiers STRING[], PRIMARY KEY (uid)"),
            ("DbTable", "fqn STRING, name STRING, datasource_name STRING, table_type STRING, comment STRING, path STRING, PRIMARY KEY (fqn)"),
            ("Datasource", "name STRING, kind STRING, host STRING, env STRING, PRIMARY KEY (name)"),
            ("DbColumn", "uid STRING, name STRING, table_fqn STRING, type STRING, nullable BOOLEAN, datasource_name STRING, is_primary_key BOOLEAN, PRIMARY KEY (uid)"),
            ("RedisKeyPattern", "uid STRING, pattern STRING, datasource_name STRING, key_type STRING, example_key STRING, count INT64, PRIMARY KEY (uid)"),
            ("ExternalClass", "name STRING, path STRING, origin STRING, package STRING, package_version STRING, evidence STRING, candidates STRING[], PRIMARY KEY (name)"),
            # Build-system entities (#1603): these were never declared, so the
            # whole Gradle/Maven build graph silently failed to write on the
            # embedded backends. Composite natural keys use a synthesized uid.
            ("GradleModule", "name STRING, build_file STRING, path STRING, repo_path STRING, PRIMARY KEY (name)"),
            ("MavenModule", "uid STRING, group_id STRING, artifact_id STRING, version STRING, packaging STRING, pom_path STRING, path STRING, repo_path STRING, PRIMARY KEY (uid)"),
            ("ExternalLibrary", "uid STRING, group_id STRING, artifact_id STRING, version STRING, PRIMARY KEY (uid)")
        ]
        
        # rel_tables: list of (table_name, schema, use_group)
        # use_group=True  -> CREATE REL TABLE GROUP (for multi FROM..TO bindings)
        # use_group=False -> CREATE REL TABLE          (single binding)
        rel_tables = [
            # Note: in KùzuDB, some labels (e.g. `Macro`, `Property`, `Union`) are treated as reserved
            # keywords in CREATE REL TABLE statements. We must escape them with backticks
            # or the rel table creation will fail silently, leading to runtime
            # "Binder exception: Table CONTAINS does not exist".
            ("CONTAINS", """
                FROM File TO Function, FROM File TO Class, FROM File TO Variable, FROM File TO Trait, FROM File TO Interface, 
                FROM File TO `Macro`, FROM File TO Struct, FROM File TO Enum, FROM File TO `Union`, FROM File TO Annotation, 
                FROM File TO Record, FROM File TO `Property`, FROM File TO Mixin, FROM File TO Extension, FROM File TO Module, 
                FROM File TO Object, FROM File TO EnumMember,
                FROM Repository TO Directory, FROM Directory TO Directory, FROM Directory TO File, FROM Repository TO File, 
                FROM Class TO Function, FROM Module TO Function, FROM Interface TO Function, FROM Struct TO Function, 
                FROM Record TO Function, FROM Trait TO Function, FROM Object TO Function, FROM Mixin TO Function,
                FROM Extension TO Function, FROM Class TO Class, FROM Class TO Interface, FROM Class TO Struct, 
                FROM Class TO Variable, FROM Class TO EnumMember,
                FROM Module TO Class, FROM Module TO Module, FROM `Macro` TO `Macro`, FROM Function TO Function
            """, True),
            ("CALLS", """
                FROM Function TO Function, FROM Function TO Class, FROM Function TO Interface, FROM Function TO Trait, 
                FROM Function TO Struct, FROM Function TO Enum, FROM Function TO Record, FROM Function TO `Union`,
                FROM Function TO Mixin, FROM Function TO Extension, FROM Function TO Object, FROM Function TO Parameter,
                FROM Class TO Function, FROM Class TO Class, FROM Class TO Interface, FROM Class TO Trait, 
                FROM Class TO Struct, FROM Class TO Enum, FROM Class TO Record, FROM Class TO `Union`,
                FROM Interface TO Function, FROM Interface TO Class, FROM Interface TO Interface,
                FROM Trait TO Function, FROM Trait TO Class, FROM Trait TO Interface,
                FROM Mixin TO Function, FROM Mixin TO Class, FROM Mixin TO Interface,
                FROM Extension TO Function, FROM Extension TO Class, FROM Extension TO Interface,
                FROM Object TO Function, FROM Object TO Class, FROM Object TO Interface,
                FROM `Union` TO Function, FROM `Union` TO Class, FROM `Union` TO Interface,
                FROM `Macro` TO Function, FROM `Macro` TO Class, FROM `Macro` TO Interface,
                FROM File TO Function, FROM File TO Class, FROM File TO Interface, FROM File TO Trait, 
                FROM File TO Struct, FROM File TO Enum, FROM File TO Record, FROM File TO `Union`,
                FROM Function TO File, FROM Function TO ExternalClass, FROM File TO ExternalClass,
                FROM Variable TO Function, FROM Variable TO Class, FROM Variable TO Interface,
                line_number INT64, args STRING[], full_call_name STRING, args_key STRING, confidence DOUBLE, resolution_tier INT64, 
                confidence_label STRING, source STRING, resolution_method STRING, called_name STRING
            """, True),
            # Same bindings as CALLS. writer.py emits HEURISTIC_CALLS for
            # resolution tier >= 8, but the table was never declared here, so on
            # Kùzu those edges could not be written and every query matching
            # [:CALLS|HEURISTIC_CALLS] failed outright with
            # "Binder exception: Table HEURISTIC_CALLS does not exist" —
            # which broke `cviz analyze dead-code` completely on this backend.
            ("HEURISTIC_CALLS", """
                FROM Function TO Function, FROM Function TO Class, FROM Function TO Interface, FROM Function TO Trait, 
                FROM Function TO Struct, FROM Function TO Enum, FROM Function TO Record, FROM Function TO `Union`,
                FROM Function TO Mixin, FROM Function TO Extension, FROM Function TO Object, FROM Function TO Parameter,
                FROM Class TO Function, FROM Class TO Class, FROM Class TO Interface, FROM Class TO Trait, 
                FROM Class TO Struct, FROM Class TO Enum, FROM Class TO Record, FROM Class TO `Union`,
                FROM Interface TO Function, FROM Interface TO Class, FROM Interface TO Interface,
                FROM Trait TO Function, FROM Trait TO Class, FROM Trait TO Interface,
                FROM Mixin TO Function, FROM Mixin TO Class, FROM Mixin TO Interface,
                FROM Extension TO Function, FROM Extension TO Class, FROM Extension TO Interface,
                FROM Object TO Function, FROM Object TO Class, FROM Object TO Interface,
                FROM `Union` TO Function, FROM `Union` TO Class, FROM `Union` TO Interface,
                FROM `Macro` TO Function, FROM `Macro` TO Class, FROM `Macro` TO Interface,
                FROM File TO Function, FROM File TO Class, FROM File TO Interface, FROM File TO Trait, 
                FROM File TO Struct, FROM File TO Enum, FROM File TO Record, FROM File TO `Union`,
                FROM Function TO File,
                FROM Variable TO Function, FROM Variable TO Class, FROM Variable TO Interface,
                line_number INT64, args STRING[], full_call_name STRING, args_key STRING, confidence DOUBLE, resolution_tier INT64, 
                confidence_label STRING, source STRING, resolution_method STRING, called_name STRING
            """, True),
            ("IMPORTS", "FROM File TO Module, alias STRING, full_import_name STRING, imported_name STRING, line_number INT64, lang STRING", False),
            ("PREVIEWS", "FROM Function TO Function, line_number INT64", False),
            ("INHERITS", """
                FROM Class TO Class, FROM Class TO Trait, FROM Class TO Interface, FROM Class TO Struct, FROM Class TO Enum, FROM Class TO `Union`, FROM Class TO Record, FROM Class TO Mixin, FROM Class TO Extension, FROM Class TO Module, FROM Class TO Object, FROM Class TO ExternalClass,
                FROM Trait TO Class, FROM Trait TO Trait, FROM Trait TO Interface, FROM Trait TO Struct, FROM Trait TO Enum, FROM Trait TO `Union`, FROM Trait TO Record, FROM Trait TO Mixin, FROM Trait TO Extension, FROM Trait TO Module, FROM Trait TO Object, FROM Trait TO ExternalClass,
                FROM Interface TO Class, FROM Interface TO Trait, FROM Interface TO Interface, FROM Interface TO Struct, FROM Interface TO Enum, FROM Interface TO `Union`, FROM Interface TO Record, FROM Interface TO Mixin, FROM Interface TO Extension, FROM Interface TO Module, FROM Interface TO Object, FROM Interface TO ExternalClass,
                FROM Struct TO Class, FROM Struct TO Trait, FROM Struct TO Interface, FROM Struct TO Struct, FROM Struct TO Enum, FROM Struct TO `Union`, FROM Struct TO Record, FROM Struct TO Mixin, FROM Struct TO Extension, FROM Struct TO Module, FROM Struct TO Object, FROM Struct TO ExternalClass,
                FROM Enum TO Class, FROM Enum TO Trait, FROM Enum TO Interface, FROM Enum TO Struct, FROM Enum TO Enum, FROM Enum TO `Union`, FROM Enum TO Record, FROM Enum TO Mixin, FROM Enum TO Extension, FROM Enum TO Module, FROM Enum TO Object, FROM Enum TO ExternalClass,
                FROM `Union` TO Class, FROM `Union` TO Trait, FROM `Union` TO Interface, FROM `Union` TO Struct, FROM `Union` TO Enum, FROM `Union` TO `Union`, FROM `Union` TO Record, FROM `Union` TO Mixin, FROM `Union` TO Extension, FROM `Union` TO Module, FROM `Union` TO Object, FROM `Union` TO ExternalClass,
                FROM Record TO Class, FROM Record TO Trait, FROM Record TO Interface, FROM Record TO Struct, FROM Record TO Enum, FROM Record TO `Union`, FROM Record TO Record, FROM Record TO Mixin, FROM Record TO Extension, FROM Record TO Module, FROM Record TO Object, FROM Record TO ExternalClass,
                FROM Mixin TO Class, FROM Mixin TO Trait, FROM Mixin TO Interface, FROM Mixin TO Struct, FROM Mixin TO Enum, FROM Mixin TO `Union`, FROM Mixin TO Record, FROM Mixin TO Mixin, FROM Mixin TO Extension, FROM Mixin TO Module, FROM Mixin TO Object, FROM Mixin TO ExternalClass,
                FROM Extension TO Class, FROM Extension TO Trait, FROM Extension TO Interface, FROM Extension TO Struct, FROM Extension TO Enum, FROM Extension TO `Union`, FROM Extension TO Record, FROM Extension TO Mixin, FROM Extension TO Extension, FROM Extension TO Module, FROM Extension TO Object, FROM Extension TO ExternalClass,
                FROM Module TO Class, FROM Module TO Trait, FROM Module TO Interface, FROM Module TO Struct, FROM Module TO Enum, FROM Module TO `Union`, FROM Module TO Record, FROM Module TO Mixin, FROM Module TO Extension, FROM Module TO Module, FROM Module TO Object, FROM Module TO ExternalClass,
                FROM Object TO Class, FROM Object TO Trait, FROM Object TO Interface, FROM Object TO Struct, FROM Object TO Enum, FROM Object TO `Union`, FROM Object TO Record, FROM Object TO Mixin, FROM Object TO Extension, FROM Object TO Module, FROM Object TO Object, FROM Object TO ExternalClass,
                FROM Variable TO Class, FROM Variable TO Trait, FROM Variable TO Interface, FROM Variable TO Struct, FROM Variable TO Enum, FROM Variable TO `Union`, FROM Variable TO Record, FROM Variable TO Mixin, FROM Variable TO Extension, FROM Variable TO Module, FROM Variable TO Object, FROM Variable TO Variable, FROM Variable TO ExternalClass,
                FROM Class TO Variable, FROM Trait TO Variable, FROM Interface TO Variable, FROM Struct TO Variable, FROM Enum TO Variable, FROM `Union` TO Variable, FROM Record TO Variable, FROM Mixin TO Variable, FROM Extension TO Variable, FROM Module TO Variable, FROM Object TO Variable,
                confidence_label STRING
            """, True),
            ("HAS_PARAMETER", "FROM Function TO Parameter", False),
            ("INCLUDES", "FROM Class TO Module", False),
            ("IMPLEMENTS", "FROM Class TO Interface, FROM Class TO Class, FROM Struct TO Interface, FROM Record TO Interface, FROM Mixin TO Interface, FROM Extension TO Interface, FROM Enum TO Interface, FROM Object TO Interface, FROM `Union` TO Interface, FROM Trait TO Interface, FROM Module TO Module, FROM Module TO Interface, confidence_label STRING", True),
            ("METACLASS", "FROM Class TO Class, line_number INT64, confidence_label STRING", False),
            ("DECORATED_BY", "FROM Function TO Function, FROM Class TO Function, line_number INT64", True),
            ("COMPANION_OF", "FROM Object TO Class", False),
            ("EMBEDS", "FROM Struct TO Struct, line_number INT64", False),
            ("PARTIAL_OF", "FROM Class TO Class, line_number INT64, confidence_label STRING", False),
            ("PART_OF", "FROM File TO File", False),
            ("INJECTS", "FROM Class TO Class, field_name STRING, inject_line INT64, confidence_label STRING", False),
            ("BINDS", "FROM Interface TO Class, FROM Class TO Class, FROM Interface TO Interface, line_number INT64, provider STRING, confidence_label STRING", True),
            ("MAPS_TO", "FROM Class TO DbTable, datastore STRING, line_number INT64", False),
            ("READS", "FROM Function TO DbTable, line_number INT64", False),
            ("WRITES", "FROM Function TO DbTable, line_number INT64", False),
            ("STORED_IN", "FROM DbTable TO Datasource, FROM RedisKeyPattern TO Datasource", True),
            ("HAS_COLUMN", "FROM DbTable TO DbColumn", False),
            # Build-system relationships
            ("MODULE_DEPENDS_ON", "FROM GradleModule TO GradleModule, FROM MavenModule TO MavenModule, configuration STRING, scope STRING", True),
            ("USES_LIBRARY", "FROM GradleModule TO ExternalLibrary, FROM MavenModule TO ExternalLibrary, configuration STRING, scope STRING", True),
            ("CHILD_MODULE", "FROM MavenModule TO MavenModule", False),
        ]

        for table_name, schema in node_tables:
            try:
                self._conn.execute(f"CREATE NODE TABLE `{table_name}`({schema})")
            except Exception as e:
                if "already exists" not in str(e).lower():
                    warning_logger(f"Kuzu Schema Node Error ({table_name}): {e}")
                    debug_log(f"Kuzu Schema Node Error ({table_name}): {e}")

        for table_name, schema, use_group in rel_tables:
            try:
                if use_group:
                    # KùzuDB requires CREATE REL TABLE GROUP for multi-binding relationships
                    self._conn.execute(f"CREATE REL TABLE GROUP `{table_name}`({schema})")
                else:
                    self._conn.execute(f"CREATE REL TABLE `{table_name}`({schema})")
            except Exception as e:
                if "already exists" not in str(e).lower():
                    warning_logger(f"Kuzu Schema Rel Error ({table_name}): {e}")
                    debug_log(f"Kuzu Schema Rel Error ({table_name}): {e}")


    # Extensions whose indexes (natural-language search, semantic/graph_index)
    # must be loaded before any write: LadybugDB refuses to insert into or
    # delete from a table carrying an index whose extension is not loaded.
    _INDEX_EXTENSIONS = {"vector": "HNSW"}

    def _load_index_extensions(self):
        """LOAD the vector extension; INSTALL it only when an index needs it.

        LOAD of an installed extension is local and cheap. INSTALL downloads it
        (extension.ladybugdb.com), so it is attempted only when the database
        already has an index of that type: without it, every write to the
        indexed tables would fail.
        """
        index_types = None
        for ext, index_type in self._INDEX_EXTENSIONS.items():
            try:
                self._conn.execute(f"LOAD {ext}")
                continue
            except Exception:
                pass
            if index_types is None:
                index_types = set()
                try:
                    res = self._conn.execute("CALL SHOW_INDEXES() RETURN index_type")
                    while res.has_next():
                        index_types.add(res.get_next()[0])
                except Exception:
                    pass
            if index_type not in index_types:
                continue
            try:
                self._conn.execute(f"INSTALL {ext}")
                self._conn.execute(f"LOAD {ext}")
            except Exception as e:
                warning_logger(
                    f"LadybugDB extension '{ext}' could not be installed ({e}). The search "
                    f"indexes need it: writes to indexed tables will fail until it loads."
                )

    def close_driver(self):
        """Closes the connection pool and releases database resources."""
        if self._db is not None:
            with self._lock:
                if self._db is None:
                    return
                info_logger(f"Closing {self.BACKEND_SPEC.display_name} connection pool")
                # Clear and close all connections in the pool
                while self._pool is not None and not self._pool.empty():
                    try:
                        conn = self._pool.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        conn.close()
                    except Exception:
                        pass
                self._pool = None
                # Explicitly close the Database object — kuzu.Database has no
                # __del__, so Python GC alone cannot release the C++ resources.
                # Without this call the process hangs on exit because the
                # embedded Kùzu engine keeps background threads alive.
                try:
                    # kuzu exposes is_closed as a METHOD on some builds and a
                    # bool ATTRIBUTE on others (0.11.x). Calling the attribute
                    # raised "'bool' object is not callable" — which this
                    # except swallowed, so the close NEVER ran and the
                    # background threads it exists to stop stayed alive.
                    closed = self._db.is_closed
                    if callable(closed):
                        closed = closed()
                    if not closed:
                        self._db.close()
                        info_logger(f"{self.BACKEND_SPEC.display_name} database closed successfully")
                except Exception as e:
                    warning_logger(f"Error closing {self.BACKEND_SPEC.display_name} database: {e}")
                self._db = None
                gc.collect()

    def is_connected(self) -> bool:
        """Checks if the database connection is currently active."""
        if self._db is None:
            return False
        try:
            # Borrow a connection to test
            conn = self._pool.get(timeout=1.0)
            try:
                conn.execute("RETURN 1")
                return True
            finally:
                self._pool.put(conn)
        except Exception:
            return False
    
    def get_backend_type(self) -> str:
        """Returns the database backend type."""
        return self.BACKEND_SPEC.backend_id

    @classmethod
    def validate_config(cls, db_path: str = None) -> Tuple[bool, Optional[str]]:
        if db_path:
            db_dir = Path(db_path).parent
            if not os.access(db_dir, os.W_OK) and db_dir.exists():
                return False, f"Cannot write to directory: {db_dir}"
        return True, None

    @classmethod
    def test_connection(cls, db_path: str = None) -> Tuple[bool, Optional[str]]:
        spec = cls.BACKEND_SPEC
        try:
            importlib.import_module(spec.python_module)
            return True, None
        except ImportError:
            return False, f"{spec.display_name} is not installed. {spec.install_hint}"


class EmbeddedDriverWrapper:
    def __init__(
        self,
        db,
        pool_or_lock,
        write_lock=None,
        compat_state=None,
        display_name: str = "Embedded",
        backend_id: str = "embedded",
    ):
        self.db = db
        self._compat_state = compat_state
        self._display_name = display_name
        self._backend_id = backend_id
        if hasattr(pool_or_lock, "acquire"):
            self._pool = None
            self._write_lock = pool_or_lock
            self._query_lock = pool_or_lock
        else:
            self._pool = pool_or_lock
            self._write_lock = write_lock or threading.Lock()
            self._query_lock = self._write_lock
    def session(self, **kwargs):
        """Accepts and ignores Neo4j-driver-style kwargs (e.g. default_access_mode)."""
        compat_state = getattr(self, "_compat_state", None)
        pool = getattr(self, "_pool", None)
        display_name = getattr(self, "_display_name", "Embedded")
        backend_id = getattr(self, "_backend_id", "embedded")
        if pool is not None:
            return EmbeddedSessionWrapper(
                pool, getattr(self, "_write_lock", None),
                compat_state=compat_state, display_name=display_name,
                backend_id=backend_id,
            )
        else:
            db = getattr(self, "db", None) or getattr(self, "conn", None)
            write_lock = getattr(self, "_write_lock", None) or getattr(self, "_query_lock", None)
            return EmbeddedSessionWrapper(
                db, write_lock, compat_state=compat_state, display_name=display_name,
                backend_id=backend_id,
            )
    def close(self):
        pass

    def get_backend_type(self) -> str:
        return getattr(self, "_backend_id", "embedded")


def _always_comma_join() -> bool:
    """Comma-join every MATCH chain of an UNWIND query, as before (see 1.5e).

    By default a chain made only of primary-key lookups stays chained, which
    LadybugDB binds correctly and plans much faster.
    CVIZ_LADYBUG_COMMA_MATCH=true restores the old behaviour.
    """
    return os.environ.get("CVIZ_LADYBUG_COMMA_MATCH", "false").strip().lower() in ("1", "true", "yes", "on")


def _row_replay_enabled() -> bool:
    """Single-row replay of batched relationship MERGEs (LadybugDB #1710 workaround)."""
    return os.environ.get("CVIZ_LADYBUG_ROW_REPLAY", "false").strip().lower() in ("1", "true", "yes", "on")




class EmbeddedSessionWrapper:
    def __init__(self, pool_or_conn, write_lock=None, compat_state=None, display_name: str = "Embedded",
                 backend_id: str = "embedded"):
        self._write_lock = write_lock or threading.Lock()
        self._query_lock = self._write_lock
        self._display_name = display_name
        self._backend_id = backend_id
        # Disabled-query-type state is shared via the manager's compat_state so
        # fail-fast disabling persists across sessions. A standalone session
        # (tests / legacy callers) gets its own private state.
        state = compat_state or EmbeddedCompatState()
        self._compat_state = state
        self._disabled_query_types = state.disabled_query_types
        self._logged_disabled_query_types = state.logged_disabled_query_types
        self._state_lock = state.lock
        
        # Backward compatibility check: check if it's a pool or connection
        if hasattr(pool_or_conn, "get") and not hasattr(pool_or_conn, "execute"):
            self._pool = pool_or_conn
            try:
                # The embedded manager is a per-subclass singleton: switching
                # db_path closes the old database and drains its pool. A caller
                # holding a STALE driver reference then blocked here forever
                # with no diagnostic. Fail loudly instead.
                self.conn = self._pool.get(timeout=30)
            except queue.Empty:
                raise RuntimeError(
                    "No database connection available after 30s — this driver's "
                    "connection pool is exhausted or its database manager was "
                    "closed/switched to another path. Re-acquire the driver via "
                    "get_database_manager().get_driver() instead of holding a "
                    "stale reference."
                )
        else:
            self._pool = None
            self.conn = pool_or_conn


        self.uid_map = {
            'Function': ['name', 'path', 'line_number', 'occurrence_index'],
            'Class': ['name', 'path', 'line_number', 'occurrence_index'],
            'Variable': ['name', 'path', 'line_number', 'occurrence_index'],
            'Trait': ['name', 'path', 'line_number', 'occurrence_index'],
            'Interface': ['name', 'path', 'line_number', 'occurrence_index'],
            'Macro': ['name', 'path', 'line_number', 'occurrence_index'],
            'Struct': ['name', 'path', 'line_number', 'occurrence_index'],
            'Enum': ['name', 'path', 'line_number', 'occurrence_index'],
            'EnumMember': ['name', 'path', 'line_number', 'occurrence_index'],
            'Union': ['name', 'path', 'line_number', 'occurrence_index'],
            'Annotation': ['name', 'path', 'line_number'],
            'Record': ['name', 'path', 'line_number', 'occurrence_index'],
            'Property': ['name', 'path', 'line_number', 'occurrence_index'],
            'Parameter': ['name', 'path', 'function_line_number'],
            'Mixin': ['name', 'path', 'line_number', 'occurrence_index'],
            'Extension': ['name', 'path', 'line_number', 'occurrence_index'],
            'Object': ['name', 'path', 'line_number', 'occurrence_index'],
            # Datasource entities: Kùzu cannot declare composite primary keys,
            # so these tables are keyed on a synthesized uid (#see write_datasource_graph)
            'DbColumn': ['name', 'table_fqn'],
            'RedisKeyPattern': ['pattern', 'datasource_name'],
            'MavenModule': ['group_id', 'artifact_id'],
            'ExternalLibrary': ['group_id', 'artifact_id']
        }
    
    def __enter__(self):
        """Enter context manager - return self for 'with' statement."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager - Return connection to the pool."""
        if self.conn and self._pool is not None:
            self._pool.put(self.conn)
        return False  # Don't suppress exceptions


    @staticmethod
    def _sanitize_value(v):
        """Recursively coerce Python types that KuzuDB cannot bind (tuples, sets, etc.)."""
        if isinstance(v, tuple):
            return [EmbeddedSessionWrapper._sanitize_value(i) for i in v]
        if isinstance(v, set):
            return [EmbeddedSessionWrapper._sanitize_value(i) for i in v]
        if isinstance(v, list):
            sanitized = [EmbeddedSessionWrapper._sanitize_value(i) for i in v]

            # Kuzu infers list-of-dict values as a struct vector. If rows in an
            # UNWIND batch have different keys, referencing a missing row.field
            # causes binder errors like "Invalid struct field name".
            if sanitized and all(isinstance(i, dict) for i in sanitized):
                all_keys = set()
                for item in sanitized:
                    all_keys.update(item.keys())
                if all_keys:
                    key_order = sorted(all_keys)
                    # Detect relationship batches — these should NOT be
                    # deduplicated because distinct relationship edges may
                    # serialize identically yet represent different graph edges
                    # (e.g. multiple CALLS from the same caller to the same
                    # target at different call sites).
                    _REL_BATCH_KEYS = {
                        "child_name", "parent_name", "resolved_parent_file_path",
                        "caller_name", "called_name", "caller_file_path",
                        "called_file_path", "caller_line_number",
                        "func_name", "arg_name",
                        "caller_symbol", "callee_name",
                        "injector_class", "injected_class",
                        "interface_name",
                    }
                    is_rel_batch = bool(all_keys & _REL_BATCH_KEYS)

                    normalized_rows = []
                    seen_rows = set() if not is_rel_batch else None
                    for item in sanitized:
                        row = {k: item.get(k) for k in key_order}
                        # NULL values in MERGE key fields (especially line_number)
                        # can cause repeated non-matching MERGEs. Normalize to a
                        # sentinel for stable identity in Kuzu.
                        for int_key in ("line_number", "function_line_number", "end_line"):
                            if int_key in row and row[int_key] is None:
                                row[int_key] = -1
                        if seen_rows is not None:
                            row_key = json.dumps(row, sort_keys=True, default=str)
                            if row_key in seen_rows:
                                continue
                            seen_rows.add(row_key)
                        normalized_rows.append(row)
                    return normalized_rows

            return sanitized
        if isinstance(v, dict):
            return {k: EmbeddedSessionWrapper._sanitize_value(val) for k, val in v.items()}
        return v

    def _classify_query_type(self, query: str) -> str:
        if "db.idx.fulltext.createNodeIndex" in query or "db.index.fulltext.queryNodes" in query:
            return "fulltext"
        if "MATCH (file:File)-[imp:IMPORTS]->(module:Module" in query:
            return "module_deps"
        if "INHERITS" in query or "IMPLEMENTS" in query:
            return "inheritance_resolution"
        if "CALLS" in query:
            return "calls_resolution"
            
        # Write query detection
        query_upper = query.upper()
        write_keywords = {"MERGE", "CREATE", "SET", "DELETE", "REMOVE", "DROP", "ALTER", "COPY"}
        if any(k in query_upper for k in write_keywords):
            return "write"
            
        return "generic"

    def _is_query_type_disabled(self, query_type: str) -> bool:
        if query_type == "generic":
            return False
        with self._state_lock:
            return query_type in self._disabled_query_types

    def _log_query_type_skip_once(self, query_type: str) -> None:
        if query_type == "generic":
            return
        with self._state_lock:
            if query_type in self._logged_disabled_query_types:
                return
            self._logged_disabled_query_types.add(query_type)
        warning_logger(
            f"{self._display_name} compatibility guard active: skipping "
            f"'{query_type}' queries after prior incompatibility."
        )

    def _should_fail_fast(self, query_type: str, error: Exception) -> bool:
        err = str(error).lower()
        if query_type == "fulltext":
            return "parser exception" in err or "invalid input <call db." in err
        if query_type == "module_deps":
            return "variable file is not in scope" in err or "binder exception" in err
        # Do NOT fail-fast for calls_resolution or inheritance_resolution.
        # The writer iterates over label pairs with its own try/except for
        # binder exceptions.  Disabling the entire query type here would
        # silently drop valid edges for label pairs that DO have schema
        # bindings, causing silent mismatches.
        return False

    def _disable_query_type(self, query_type: str, error: Exception) -> None:
        if query_type == "generic":
            return
        with self._state_lock:
            self._disabled_query_types.add(query_type)
            already_logged = query_type in self._logged_disabled_query_types
            if not already_logged:
                self._logged_disabled_query_types.add(query_type)
        if not already_logged:
            warning_logger(
                f"Kuzu compatibility guard: disabling '{query_type}' queries for this run after error: {error}"
            )

    @staticmethod
    def _contains_integer(value):
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, dict):
            return any(EmbeddedSessionWrapper._contains_integer(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return any(EmbeddedSessionWrapper._contains_integer(v) for v in value)
        return False

    @staticmethod
    def _contains_numeric_batch(value):
        if isinstance(value, dict):
            return any(EmbeddedSessionWrapper._contains_numeric_batch(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            if len(value) > 1 and EmbeddedSessionWrapper._contains_integer(value):
                return True
            return any(EmbeddedSessionWrapper._contains_numeric_batch(v) for v in value)
        return False

    def _execute_bound(self, query, parameters):
        return self._execute_bound_once(query, parameters)

    def _execute_bound_once(self, query, parameters):
        """Avoid Ladybug 0.19's implicit cache for multi-row numeric parameters.

        The driver's list-of-struct cache signature examines only its first
        row. Rebinding a later row across integer widths can silently turn
        line 186 into -70 (#1730), corrupting graph identities. A fresh public
        prepared statement binds the actual current types. Scalars, single-row
        replay batches and other backends retain their existing cached path.
        """
        numeric_batch = any(self._contains_numeric_batch(value) for value in parameters.values())
        if self._backend_id != "ladybugdb" or not numeric_batch:
            return self.conn.execute(query, parameters)

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message="The use of separate prepare.*", category=DeprecationWarning
            )
            prepared = self.conn.prepare(query, parameters)
        try:
            return self.conn.execute(prepared, parameters)
        finally:
            prepared.close()

    def run_native(self, statement, /, **parameters):
        """Execute LadybugDB Cypher as-is, without the Neo4j-dialect translation.

        For statements written for LadybugDB itself (extension procedures,
        ``ALTER TABLE``, properties outside the translator's static schema
        such as the search columns). Errors are raised, never swallowed.
        """
        with self._write_lock:
            result = self._execute_bound(statement, parameters)
        return EmbeddedResultWrapper(result)

    def run(self, query, **parameters):
        query_type = self._classify_query_type(query)
        if self._is_query_type_disabled(query_type):
            self._log_query_type_skip_once(query_type)
            return EmbeddedResultWrapper(None)

        # 0. Sanitize parameters (convert tuples/sets → lists throughout)
        parameters = {k: self._sanitize_value(v) for k, v in parameters.items()}
        # 1. Translate Query
        debug_log(f"Original Query: {query[:200]}")
        translated_query, translated_params = self._translate_query(query, parameters)
        debug_log(f"Translated Query: {translated_query[:200]}")
        try:
            # Force the per-row loop fallback ONLY for UNWIND batches containing a
            # node-MERGE. Kùzu's MERGE pipeline mis-binds when a merged node's key
            # value repeats NON-adjacently across the batch: in
            # [(A,P), (B,X), (C,P)] the C row binds to X's node instead of
            # re-matching P (reproduced on Kùzu 0.11.3; see #1605). Relationship-only
            # MERGEs (MATCH … MATCH … MERGE (a)-[r]->(b)) do not exhibit the bug —
            # verified with interleaved duplicate endpoint keys and duplicate pairs —
            # so they now run batched, which removes the bulk of the per-row planner
            # overhead (~2.4x on a full index of the Python sample project).
            # Only `UNWIND $param AS row` shapes are guarded — that is the single
            # shape the recovery path below can rewrite into a per-row loop. Read
            # queries that unwind a bound list (`WITH relationships(p) AS rels
            # UNWIND rels AS r`) must not match, or the fabricated exception would
            # surface to the caller as a hard failure.
            # Node-only UNWIND writes (`MERGE (n:Label {…}) SET n += row`, no
            # relationship pattern) have always batched through the SET-expansion
            # path and the per-row rewriter cannot handle their bare `SET n += row`,
            # so the guard additionally requires a relationship pattern.
            _has_node_merge = re.search(r"MERGE\s*\(\s*\w+\s*:", query)
            if (
                re.search(r"UNWIND\s+\$\w+\s+AS\s+\w+", query)
                and ("-[" in query or "]->" in query)
                and _has_node_merge
                and not getattr(self, "_skip_unwind_fallback", False)
            ):
                raise Exception("unordered_map::at (forced fallback: node-MERGE inside UNWIND mis-binds repeated keys)")

            # 2. Execute under the lock. _write_lock (name kept for backward
            # compat) now serializes ALL access, reads included: kuzu.Connection
            # is not thread-safe, and concurrent asyncio.to_thread tool calls can
            # otherwise race a read against a write on the same connection.
            with self._write_lock:
                result = self._execute_bound(translated_query, translated_params)

                # LadybugDB (0.19.x) state-dependently drops SOME rows of a
                # batched relationship-only UNWIND MERGE — no error is raised,
                # the MERGE just writes nothing for those rows (a 17-row
                # Struct-CONTAINS batch wrote 12; #1710). Locally an immediate
                # identical re-run bound the stragglers, but on CI the drop
                # survives re-execution, so after the batched pass every row
                # is replayed as a single-row batch through the SAME translated
                # query. MERGE is idempotent, so the union is safe; only
                # ladybug pays the cost. The generic per-row rewrite path is
                # deliberately NOT used here — its query surgery loses ~51
                # CONTAINS edges (the #1612 signature).
                # Off by default since LadybugDB 0.19.1: the drop no longer
                # reproduces (same edges with and without, parser goldens
                # green) and the replay was ~70% of a full index (280 s ->
                # 106 s on 158 files). CVIZ_LADYBUG_ROW_REPLAY=true restores it.
                if (
                    getattr(self, "_backend_id", "") == "ladybugdb"
                    and _row_replay_enabled()
                    and "MERGE" in query
                    and ("-[" in query or "]->" in query)
                    and "RETURN" not in query.upper()
                ):
                    _u = re.search(r"UNWIND\s+\$(\w+)\s+AS\s+\w+", translated_query)
                    _rows = _u and translated_params.get(_u.group(1))
                    if isinstance(_rows, list) and len(_rows) > 1:
                        # Best-effort only: the batched pass above already
                        # wrote what it could, so a failing single must NEVER
                        # propagate — the first singles attempt let one raise
                        # and the escaping exception aborted the writer's
                        # remaining batches, costing ~51 CONTAINS edges (the
                        # union of idempotent MERGEs cannot otherwise shrink).
                        _single_failures = 0
                        _first_single_error = None
                        for _row in _rows:
                            single = dict(translated_params)
                            single[_u.group(1)] = [_row]
                            try:
                                self._execute_bound(translated_query, single)
                            except Exception as _se:
                                _single_failures += 1
                                if _first_single_error is None:
                                    _first_single_error = str(_se)[:160]
                        if _single_failures:
                            # warning, not debug: this is the only trace of a
                            # silently short ladybug write, and parity CI
                            # never sees debug output (#1730).
                            warning_logger(
                                f"Ladybug single-row replay: {_single_failures}/{len(_rows)} "
                                f"rows errored (first: {_first_single_error}) — batched pass "
                                f"result stands — query: {query[:90]}"
                            )

            return EmbeddedResultWrapper(result)
        except Exception as e:
            if self._should_fail_fast(query_type, e):
                self._disable_query_type(query_type, e)
                return EmbeddedResultWrapper(None)

            # Log non-fatal schema collisions at debug level instead of swallowing
            # them silently. This preserves the "idempotent CREATE" behaviour while
            # still emitting a traceable message for unexpected collisions.
            err_str = str(e).lower()
            if "already exists" in err_str:
                debug_log(f"Kuzu idempotent collision (already exists) — query: {query[:120]}")
                return EmbeddedResultWrapper(None)
            
            if "binder exception" in err_str:
                debug_log(f"Kuzu binder exception (expected during label matching check) — query: {query[:120]}... Error: {e}")
                raise e
            
            # Fallback for KuzuDB UNWIND bug (unordered_map::at)
            if "unordered_map::at" in err_str and "UNWIND" in query:
                unwind_m = re.search(r'UNWIND\s+\$(\w+)\s+AS\s+(\w+)', query)
                if unwind_m:
                    batch_param = unwind_m.group(1)
                    row_var = unwind_m.group(2)
                    batch_data = parameters.get(batch_param)
                    if isinstance(batch_data, list):
                        loop_query = re.sub(r'UNWIND\s+\$\w+\s+AS\s+\w+', '', query, count=1)
                        # Find all row.prop usages and replace with $row_prop
                        props_used = set(re.findall(rf'{row_var}\.(\w+)', loop_query))
                        for p in props_used:
                            loop_query = loop_query.replace(f"{row_var}.{p}", f"${row_var}_{p}")
                        # The row var itself may still appear bare in WITH lists
                        # ("WITH tbl, t") once the UNWIND is gone; a dangling
                        # reference binder-errors and the row is silently
                        # dropped. Scrub it from WITH clauses.
                        loop_query = re.sub(
                            rf'(WITH\s+[^\n]*?),\s*{row_var}\b(?!\.)', r'\1', loop_query
                        )
                        loop_query = re.sub(
                            rf'WITH\s+{row_var}\s*,\s*', 'WITH ', loop_query
                        )
                        loop_query = re.sub(
                            rf'WITH\s+{row_var}\b(?!\.)\s*\n', 'WITH 1 AS _row_scrubbed\n', loop_query
                        )
                        
                        last_result = None
                        dropped = 0
                        first_drop_error = None
                        for item in batch_data:
                            loop_params = parameters.copy()
                            loop_params.pop(batch_param, None)
                            for p in props_used:
                                loop_params[f"{row_var}_{p}"] = item.get(p)
                            if "uid" in item:
                                loop_params[f"{row_var}_uid"] = item["uid"]
                            try:
                                last_result = self.run(loop_query, **loop_params)
                            except Exception as nested_e:
                                nested_err_str = str(nested_e).lower()
                                if "binder" in nested_err_str or "cannot find a valid label" in nested_err_str:
                                    # Expected: label-pair probing legitimately
                                    # misses on schemaless shapes.
                                    continue
                                # Transient failure (lock contention, buffer
                                # pressure): retry the ROW once before dropping
                                # it — writes are MERGE-idempotent. Silent
                                # drops here were LadybugDB's missing-edge
                                # signature in the parity run (#1612).
                                try:
                                    last_result = self.run(loop_query, **loop_params)
                                except Exception as retry_e:
                                    dropped += 1
                                    if first_drop_error is None:
                                        first_drop_error = str(retry_e)[:160]
                        if dropped:
                            if dropped == len(batch_data) and dropped > 0:
                                # Every row failing is systematic breakage, not
                                # transient pressure — raise so the caller's
                                # own retry/failure accounting still engages.
                                raise RuntimeError(
                                    f"Per-row fallback failed for ALL {dropped} rows "
                                    f"(first error: {first_drop_error}) — query: {query[:90]}"
                                )
                            warning_logger(
                                f"Per-row fallback dropped {dropped}/{len(batch_data)} "
                                f"rows after retry (first error: {first_drop_error}) — "
                                f"query: {query[:90]}"
                            )
                        return last_result or EmbeddedResultWrapper(None)


            error_logger(f"Kuzu Query failed: {query[:100]}... Error: {e}")
            debug_log(f"Kuzu Query failed: {query[:100]}... Error: {e}")
            raise e
    _MATCH_CHAIN_RE = re.compile(
        r'(?<!OPTIONAL )\bMATCH\s*\([^()]*\)(?:\s*,\s*\([^()]*\))*'
        r'(?:\s+MATCH\s*\([^()]*\)(?:\s*,\s*\([^()]*\))*)+'
    )

    def _is_key_lookup(self, pattern: str, pk_map: Dict[str, str]) -> bool:
        """``v:Label {pk: …}``: one property, the label's primary key."""
        m = re.fullmatch(r'\s*\w+\s*:\s*`?(\w+)`?\s*\{([^{}]*)\}\s*', pattern)
        if not m:
            return False
        label, props_str = m.groups()
        pk = pk_map.get(label) or ('uid' if label in self.uid_map else None)
        keys = [k.strip() for k, _ in re.findall(r'(\w+)\s*:\s*([^,{}]+)', props_str)]
        return pk is not None and keys == [pk]

    def _join_match_chains(self, query: str, pk_map: Dict[str, str]) -> str:
        """1.5e: comma-join each MATCH chain, unless it holds key lookups only."""
        always = _always_comma_join()

        def _join(m):
            chain = m.group(0)
            patterns = re.findall(r'\(([^()]*)\)', chain)
            if not always and all(self._is_key_lookup(p, pk_map) for p in patterns):
                return chain
            return re.sub(r'\s+MATCH\s*(?=\()', ', ', chain)

        return self._MATCH_CHAIN_RE.sub(_join, query)

    _UID_LINE_PARTS = ("line_number", "function_line_number", "end_line")

    def _match_by_uid(self, query: str, row_var: str, batch_data: list, parameters: Dict[str, Any]) -> str:
        """Rewrite ``MATCH (v:Label {<uid components>})`` into a uid lookup.

        Only for labels keyed on a synthesized uid, only when the pattern
        names every uid component (from ``row.x`` or ``$param``) and every row
        has a usable value for each (not None; not -1 for a line, which 1.5b
        replaces by a fallback hash). The uid is then exactly the one 1.5b
        computed when the node was merged, so the MATCH finds the same node.
        Anything else is left as it is.
        """
        node_re = re.compile(r'\((\w+):([^\s\{\)]+)\s*\{([^}]+)\}\)')
        # Right to left, so the offsets of the patterns still to visit hold.
        for m in reversed(list(node_re.finditer(query))):
            var_name, label_raw, props_str = m.groups()
            label = label_raw.strip('`')
            pk_parts = self.uid_map.get(label)
            if not pk_parts or 'uid' in props_str:
                continue
            if re.search(r'MERGE\s*$', query[: m.start()]):
                continue  # a MERGE pattern: 1.5b owns it
            props = dict(
                (k.strip(), v.strip())
                for k, v in re.findall(r'(\w+)\s*:\s*([^,{}]+)', props_str)
            )
            if set(props) - set(pk_parts) or set(pk_parts) - set(props):
                continue  # extra or missing keys: not a pure primary-key match
            sources = []
            for part in pk_parts:
                ref = props[part]
                row_ref = re.fullmatch(rf'{re.escape(row_var)}\.(\w+)', ref)
                param_ref = re.fullmatch(r'\$(\w+)', ref)
                if row_ref:
                    sources.append(("row", row_ref.group(1), part))
                elif param_ref and param_ref.group(1) in parameters:
                    sources.append(("param", param_ref.group(1), part))
                else:
                    sources = None
                    break
            if not sources:
                continue

            def _component(kind, name, part, item):
                val = item.get(name) if kind == "row" else parameters.get(name)
                if val is None or (part in self._UID_LINE_PARTS and val == -1):
                    return None
                if isinstance(val, bool) or not isinstance(val, (str, int)):
                    return None
                return str(val)

            uids = []
            for item in batch_data:
                if not isinstance(item, dict):
                    uids = None
                    break
                parts = [_component(k, n, p, item) for k, n, p in sources]
                if any(c is None for c in parts):
                    uids = None
                    break
                uids.append(''.join(parts))
            if not uids:
                continue
            field = f"cviz_uid_{var_name}"
            for item, uid in zip(batch_data, uids):
                item[field] = uid
            query = (
                query[: m.start()]
                + f"({var_name}:{label_raw} {{uid: {row_var}.{field}}})"
                + query[m.end():]
            )
        return query

    def _translate_query(self, query: str, parameters: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Translates the Neo4j-dialect Cypher the code writes into LadybugDB Cypher."""
        PK_MAP = {
            'Repository': 'path',
            'File': 'path',
            'Directory': 'path',
            'Module': 'name',
            'DbTable': 'fqn',
            'ExternalClass': 'name'
        }
        
        # 0. Define Schema Map (Strict property filtering)
        SCHEMA_MAP = {
            'Repository': {'path', 'name', 'is_dependency', 'indexed_at', 'commit_hash'},
            'File': {'path', 'name', 'relative_path', 'package_name', 'language', 'is_dependency', 'content_hash'},
            'Directory': {'path', 'name'},
            'Module': {'name', 'lang', 'full_import_name', 'path', 'line_number'},
            'Function': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'cyclomatic_complexity', 'context', 'context_type', 'class_context', 'class_context_line', 'module_context', 'is_dependency', 'decorators', 'args', 'arg_types', 'http_method', 'http_path', 'visibility', 'modifiers', 'is_composable'},
            'Class': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'node_type', 'is_dependency', 'decorators', 'visibility', 'modifiers'},
            'Variable': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'source', 'docstring', 'lang', 'value', 'context', 'is_dependency'},
            'Trait': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'is_dependency'},
            'Interface': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'is_dependency', 'decorators', 'visibility', 'modifiers'},
            'Macro': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'is_dependency'},
            'Struct': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'is_dependency'},
            'Enum': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'is_dependency'},
            'EnumMember': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'lang', 'is_dependency'},
            'Union': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'is_dependency'},
            'Annotation': {'uid', 'name', 'path', 'line_number', 'end_line', 'source', 'docstring', 'lang', 'is_dependency'},
            'Record': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'is_dependency'},
            'Property': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'is_dependency'},
            'Parameter': {'uid', 'name', 'path', 'function_line_number'},
            'Mixin': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'is_dependency'},
            'Extension': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'is_dependency'},
            'Object': {'uid', 'name', 'path', 'line_number', 'occurrence_index', 'end_line', 'source', 'docstring', 'lang', 'is_dependency', 'decorators', 'visibility', 'modifiers'}
        }

        # 1. Translate SET n += $props  and  SET n = $props  (map merge/assign)
        if "SET" in query and "= $" in query:
            match = re.search(r'SET\s+(\w+)\s*\+?=\s*\$(\w+)', query)
            if match:
                node_var = match.group(1)
                param_name = match.group(2)
                
                # Determine label used for node_var to filter properties
                def_match = re.search(rf'\({node_var}:(\w+)', query)
                label = def_match.group(1) if def_match else None
                
                props_dict = parameters.get(param_name, {})
                if isinstance(props_dict, dict):
                    set_clauses = []
                    new_params = parameters.copy()
                    
                    allowed_props = SCHEMA_MAP.get(label, set()) if label else None
                    pk_field = PK_MAP.get(label, 'uid')

                    for k, v in props_dict.items():
                        if isinstance(v, (dict, list)) and k != 'args' and k != 'decorators' and k != 'modifiers':
                            continue
                        
                        if allowed_props and k not in allowed_props:
                           continue
                           
                        if k == pk_field or k == 'uid':
                            continue
                            
                        clean_k = f"{param_name}_{k}"
                        set_clauses.append(f"{node_var}.{k} = ${clean_k}")
                        new_params[clean_k] = v
                        
                    if set_clauses:
                        query = query.replace(match.group(0), "SET " + ", ".join(set_clauses))
                        new_params.pop(param_name, None)
                        parameters = new_params
                    else:
                        query = query.replace(match.group(0), "")

        # 1.5: Handle UNWIND-specific patterns before standard UID injection.
        # When queries use UNWIND $batch AS row, two things need translation:
        #   a) SET n += row  (map merge unsupported in KuzuDB) → explicit property SETs
        #   b) MERGE uid injection from row fields (row.name, row.line_number, …)
        unwind_m = re.search(r'UNWIND\s+\$(\w+)\s+AS\s+(\w+)', query)
        if unwind_m:
            # (1.5-pre, the comma-join of MATCH chains, runs at the end: 1.5e.)
            batch_param = unwind_m.group(1)
            row_var = unwind_m.group(2)
            batch_data = parameters.get(batch_param)

            if isinstance(batch_data, list) and batch_data:
                # 1.5a: Expand  SET node_var += row_var  →  SET node_var.p1 = row_var.p1, …
                set_plus_re = re.compile(
                    rf'SET\s+(\w+)\s*\+=\s*{re.escape(row_var)}\b'
                )
                set_m = set_plus_re.search(query)
                if set_m:
                    node_var = set_m.group(1)
                    label_m = re.search(rf'\({re.escape(node_var)}:(\w+)', query)
                    label = label_m.group(1).strip('`') if label_m else None
                    allowed = SCHEMA_MAP.get(label, set()) if label else None

                    sample = batch_data[0]
                    parts = []
                    pk_field = PK_MAP.get(label, 'uid')
                    for k in sample:
                        if k == 'uid' or k == pk_field:
                            continue
                        if allowed and k not in allowed:
                            continue
                        parts.append(f"{node_var}.{k} = {row_var}.{k}")

                    replacement = ("SET " + ", ".join(parts)) if parts else ""
                    query = set_plus_re.sub(replacement, query, count=1)

                # 1.5b: Inject uid into MERGE clauses that reference UNWIND row fields.
                # CREATE clauses get it too (the writer creates the symbols of a
                # file new to the graph, see GraphWriter.add_file_to_graph) and
                # keep their pattern properties inline.
                merge_re = re.compile(
                    r'(?:MERGE|CREATE)\s+\((\w+):([^\s\{]+)\s*\{([^}]+)\}\)'
                )
                for m in list(merge_re.finditer(query)):
                     var_name, label_raw, props_str = m.groups()
                     label = label_raw.strip('`')
                     if label not in self.uid_map:
                         continue

                     pk_parts = self.uid_map[label]
                     all_ok = True

                     for item in batch_data:
                         uid_components = []
                         for part in pk_parts:
                             row_ref = re.search(
                                 rf'\b{part}\s*:\s*{re.escape(row_var)}\.(\w+)',
                                 props_str,
                             )
                             param_ref = re.search(
                                 rf'\b{part}\s*:\s*\$(\w+)', props_str
                             )
                             if row_ref:
                                 val = item.get(row_ref.group(1))
                                 # Consider -1 as missing for line numbers to force fallback,
                                 # ensuring uniqueness when the primary key is incomplete
                                 if val is not None and not (part in ("line_number", "function_line_number", "end_line") and val == -1):
                                     uid_components.append(str(val))
                                 else:
                                     # Missing values are common in parser output for some
                                     # languages. Use a deterministic placeholder component
                                     # to keep UID generation stable and unique enough.
                                     row_hash = hashlib.md5(str(sorted([(k, v) for k, v in item.items() if k != 'uid'])).encode()).hexdigest()[:8]
                                     uid_components.append(f"__fallback_{row_hash}")
                             elif param_ref:
                                 val = parameters.get(param_ref.group(1))
                                 if val is not None:
                                     uid_components.append(str(val))
                                 else:
                                     uid_components.append(f"__missing_{part}")
                             elif part == 'occurrence_index':
                                 # Legacy write paths (bundle import, older queries)
                                 # MERGE without occurrence_index in the pattern;
                                 # treat them as ordinal 0 so their uid matches the
                                 # writer's uid for non-colliding symbols (#1393).
                                 uid_components.append(str(item.get('occurrence_index', 0) or 0))
                             else:
                                 all_ok = False
                                 break

                         if all_ok:
                             raw_uid = ''.join(uid_components)
                             item['uid'] = raw_uid
                         else:
                             all_ok = False
                             break

                     if all_ok:
                         old_block = '{' + props_str + '}'
                         new_block = (
                             '{' + props_str + f', uid: {row_var}.uid' + '}'
                         )
                         query = query.replace(old_block, new_block, 1)

                         # Kuzu node tables are keyed by uid for these labels, so MERGE
                         # should match on the primary key only. Matching on additional
                         # non-PK fields can still lead to duplicate PK insert attempts.
                         # The displaced pattern properties must be re-applied with an
                         # explicit SET, though: unlike Neo4j, the uid-only MERGE no
                         # longer assigns them on create, and queries whose follow-up
                         # SET does not repeat them (e.g. MavenModule/ExternalLibrary
                         # writes) would otherwise leave the key columns NULL and break
                         # every later MATCH on them.
                         displaced_pairs = [
                             (k.strip(), v.strip())
                             for k, v in re.findall(r'(\w+)\s*:\s*([^,{}]+)', props_str)
                             if k.strip() != 'uid'
                         ]
                         displaced_set = ''
                         if displaced_pairs:
                             assignments = ', '.join(
                                 f"{var_name}.{k} = {v}" for k, v in displaced_pairs
                             )
                             displaced_set = f" SET {assignments}"
                         query = re.sub(
                             rf"MERGE\s+\({re.escape(var_name)}:{re.escape(label_raw)}\s*\{{[^}}]*uid:\s*{re.escape(row_var)}\.uid[^}}]*\}}\)",
                             f"MERGE ({var_name}:{label_raw} {{uid: {row_var}.uid}}){displaced_set}",
                             query,
                             count=1,
                         )

                # 1.5b': MATCH by primary key. A MATCH on the uid's components
                # (name, path, line_number…) scans the whole label table for
                # every batch, so a full index slowed down as the graph grew
                # (quadratic). When every row holds every component, the uid
                # is the one 1.5b gave the node (same concatenation), and the
                # MATCH becomes a primary-key lookup on it.
                # Not when every chain is comma-joined (the old behaviour):
                # `MATCH (a {uid}), (b {uid})` is the slowest shape of all.
                if not _always_comma_join():
                    query = self._match_by_uid(query, row_var, batch_data, parameters)

                # 1.5c: Strip explicit SET clauses for properties not in the schema
                # (e.g. SET m.alias = row.alias when Module has no 'alias' column)
                def _filter_set_clause(m_set):
                    full = m_set.group(0)
                    assignments = re.split(r',\s*(?=\w+\.\w+\s*=)', full[4:])  # skip "SET "
                    kept = []
                    for a in assignments:
                        a = a.strip()
                        prop_m = re.match(r'(\w+)\.(\w+)\s*=', a)
                        if prop_m:
                            nvar = prop_m.group(1)
                            prop_name = prop_m.group(2)
                            lbl_m = re.search(rf'\({re.escape(nvar)}:(\w+)', query)
                            if lbl_m:
                                lbl = lbl_m.group(1).strip('`')
                                allowed_s = SCHEMA_MAP.get(lbl)
                                if allowed_s and prop_name not in allowed_s:
                                    continue
                                if prop_name == PK_MAP.get(lbl, 'uid') or prop_name == 'uid':
                                    continue
                        kept.append(a)
                    if kept:
                        return "SET " + ", ".join(kept)
                    return ""

                # Only apply to explicit SET lines (not SET +=, already handled)
                if '+=' not in query:
                    query = re.sub(
                        r'SET\s+\w+\.\w+\s*=\s*[^,\n]+(?:\s*,\s*\w+\.\w+\s*=\s*[^,\n]+)*',
                        _filter_set_clause,
                        query,
                    )

                # 1.5d: Translate ON CREATE SET / ON MATCH SET → plain SET (KuzuDB compat)
                query = re.sub(r'\bON\s+CREATE\s+SET\b', 'SET', query, flags=re.IGNORECASE)
                query = re.sub(r'\bON\s+MATCH\s+SET\b', 'SET', query, flags=re.IGNORECASE)

            # 1.5e: Comma-join consecutive MATCH clauses (#1710). LadybugDB's
            # planner silently mis-binds UNWIND → MATCH (a {key: row.x}) →
            # MATCH (b {prop: row.y}): after a primary-key lookup, the next
            # MATCH's predicates on row fields are lost, so a row binds nodes
            # of other rows (or none) and the MERGEs below write wrong edges,
            # with no error. The comma form `MATCH (a {…}), (b {…})` is
            # semantically identical for non-optional patterns and binds
            # correctly. Only simple node patterns (no relationship arrows,
            # no parens inside) directly adjacent to the next MATCH are
            # joined, and never across OPTIONAL MATCH.
            # A chain made only of primary-key lookups binds correctly
            # chained, and the comma form plans it as a join whose cost grows
            # with the tables (35 s against 14 ms for a 500-row CALLS batch
            # on 160,000 functions): such chains stay as they are. 1.5b'
            # above turns most writer MATCHes into such lookups.
            query = self._join_match_chains(query, PK_MAP)

        # 2. Handle UID injection for MERGE (non-UNWIND queries)
        # We look for MERGE (v:Label {props})
        merge_pattern = r'MERGE\s+\((\w+):([^\s\{]+)\s*\{([^}]+)\}\)'
        matches = list(re.finditer(merge_pattern, query))
        for m in matches:
            var_name, label_raw, props_str = m.groups()
            label = label_raw.strip('`').strip(':')
            if label in self.uid_map:
                # Skip if uid already injected (by UNWIND handler above)
                if 'uid:' in props_str:
                    continue
                pk_parts = self.uid_map[label]
                can_build_uid = True
                uid_val = ""
                for part in pk_parts:
                    p_match = re.search(rf'{part}:\s*\$(\w+)', props_str)
                    if p_match:
                        p_val = parameters.get(p_match.group(1))
                        if p_val is not None:
                            uid_val += str(p_val)
                        else: can_build_uid = False; break
                    else: can_build_uid = False; break
                
                if can_build_uid:
                    uid_param = f"__uid_{var_name}"
                    old_block = f"{{{props_str}}}"
                    new_block = f"{{{props_str}, uid: ${uid_param}}}"
                    if old_block in query:
                         query = query.replace(old_block, new_block)
                    else:
                         warning_logger(f"Kuzu UID injection: could not find props block in query for label '{label}'")
                    
                    parameters[uid_param] = uid_val

        query, parameters = self._rewrite_kuzu_compat_patterns(query, parameters)

        # 3. Escape keywords as labels
        labels_to_escape = ['Macro', 'Union', 'Property', 'CONTAINS', 'CALLS'] # Only critical keywords
        for label in labels_to_escape:
            query = re.sub(rf':{label}\b', f':`{label}`', query)

        # Translate (n:Label1 OR n:Label2 ...) to label(n) IN ['Label1', 'Label2', ...]
        def poly_replacer(match):
            full_match = match.group(0)
            var_name = match.group(1)
            # Find all labels associated with this variable in the OR chain
            labels = re.findall(rf'{var_name}:([a-zA-Z0-9_`]+)', full_match)
            # Strip backticks from labels
            labels = [l.strip('`') for l in labels]
            return f"label({var_name}) IN {json.dumps(labels)}"
        
        # Regex to match (n:Label1 OR n:Label2 OR n:Label3)
        query = re.sub(r'\((\w+):[a-zA-Z0-9_`]+(?:\s+OR\s+\1:[a-zA-Z0-9_`]+)+\)', poly_replacer, query)
        
        # Translate single WHERE n:Label to label(n) = 'Label'
        # This is more complex because we don't want to match MATCH/MERGE
        # For now, we only target where it appears after WHERE or AND/OR
        def single_label_replacer(match):
            prefix = match.group(1)
            var_name = match.group(2)
            label = match.group(3).strip('`')
            return f"{prefix}label({var_name}) = '{label}'"
            
        query = re.sub(r'(WHERE\s+|AND\s+|OR\s+|WHEN\s+)(\w+):([a-zA-Z0-9_`]+)', single_label_replacer, query, flags=re.IGNORECASE)

        # Handle NOT n:Label → NOT label(n) = 'Label'
        def not_label_replacer(match):
            prefix = match.group(1)
            var_name = match.group(2)
            label_name = match.group(3).strip('`')
            return f"{prefix}NOT label({var_name}) = '{label_name}'"
        query = re.sub(r'(WHERE\s+|AND\s+|OR\s+)NOT\s+(\w+):([a-zA-Z0-9_`]+)', not_label_replacer, query, flags=re.IGNORECASE)

        # 4. Polymorphic matches and label access. Kùzu has no labels();
        # rewrite labels(<var>)[0] for ANY variable name — the literal
        # "labels(n)[0]" form missed n1/n2 and the simulator's relationship
        # query reached the engine unsupported (#1512).
        query = re.sub(r'labels\((\w+)\)\s*\[\s*0\s*\]', r'label(\1)', query)

        query = query.replace("coalesce(", "COALESCE(")
        query = re.sub(r'\btype\(', 'label(', query)

        # General ON CREATE/MATCH SET → SET (also covers non-UNWIND queries)
        query = re.sub(r'\bON\s+CREATE\s+SET\b', 'SET', query, flags=re.IGNORECASE)
        query = re.sub(r'\bON\s+MATCH\s+SET\b', 'SET', query, flags=re.IGNORECASE)

        # Anchored to the leading keyword: a substring test also matched
        # these words inside a string literal, silently turning ordinary
        # data queries into "RETURN 1".
        if is_schema_ddl(query):
            return "RETURN 1", {}

        # 5. Cleanup unused parameters (Kuzu is strict)
        used_params = set(re.findall(r'\$(\w+)', query))
        parameters = {k: v for k, v in parameters.items() if k in used_params}

        return query, parameters

    def _rewrite_kuzu_compat_patterns(self, query: str, parameters: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Rewrite Neo4j-dialect query patterns to LadybugDB-compatible variants."""

        # Kuzu requires explicit carrying of bindings into OPTIONAL MATCH and
        # alias-based ORDER BY after RETURN DISTINCT.
        if (
            "MATCH (file:File)-[imp:IMPORTS]->(module:Module" in query
            and (
                "OPTIONAL MATCH (repo:Repository)-[:CONTAINS]->(file)" in query
                or "OPTIONAL MATCH (repo:Repository)-[:`CONTAINS`]->(file)" in query
            )
        ):
            query = query.replace(
                "OPTIONAL MATCH (repo:Repository)-[:CONTAINS]->(file)",
                "WITH file, imp, module\n                OPTIONAL MATCH (repo:Repository)-[:CONTAINS]->(file)",
            )
            query = query.replace(
                "OPTIONAL MATCH (repo:Repository)-[:`CONTAINS`]->(file)",
                "WITH file, imp, module\n                OPTIONAL MATCH (repo:Repository)-[:`CONTAINS`]->(file)",
            )
            query = query.replace(
                "ORDER BY file.is_dependency ASC, file.path",
                "ORDER BY file_is_dependency ASC, importer_file_path",
            )

        if "ORDER BY other_module.name" in query and "other_module.name as imported_module" in query:
            query = query.replace("ORDER BY other_module.name", "ORDER BY imported_module")

        # Import-batch compatibility: ensure optional fields exist on all UNWIND
        # rows and avoid writing unsupported Module.alias on Kuzu schema.
        if (
            "UNWIND $batch AS row" in query
            and "MERGE (m:Module {name: row.name})" in query
            and "row.full_import_name" in query
        ):
            batch = parameters.get("batch")
            if isinstance(batch, list):
                normalized_batch = []
                for item in batch:
                    if isinstance(item, dict):
                        normalized = dict(item)
                        normalized.setdefault("full_import_name", normalized.get("name"))
                        normalized.setdefault("imported_name", normalized.get("name"))
                        normalized.setdefault("alias", "")
                        normalized.setdefault("line_number", None)
                        normalized.setdefault("lang", "")
                        normalized_batch.append(normalized)
                    else:
                        normalized_batch.append(item)
                parameters = {**parameters, "batch": normalized_batch}

            query = query.replace("SET m.alias = row.alias,", "SET")

        return query, parameters


class EmbeddedRecord:
    def __init__(self, data_dict):
        self._data = data_dict
        self._keys = list(data_dict.keys())
    
    def data(self):
        return self._data
    
    def keys(self):
        return self._keys
    
    def items(self):
        return self._data.items()
    
    def values(self):
        return list(self._data.values())
    
    def __len__(self):
        return len(self._data)
    
    def __getitem__(self, key):
        # Support both dict-style (by name) and list-style (by index) access
        if isinstance(key, int):
            # Integer index - get by position
            if 0 <= key < len(self._keys):
                return self._data[self._keys[key]]
            raise IndexError(f"Index {key} out of range")
        else:
            # String key - get by column name
            return self._data[key]
    
    def get(self, key, default=None):
        return self._data.get(key, default)

class EmbeddedResultWrapper:
    def __init__(self, result):
        self.result = result
        self._consumed = False
    def consume(self):
        self._consumed = True
        return self
    def single(self):
        records = self.data_raw()
        return EmbeddedRecord(records[0]) if records else None
    def data_raw(self) -> List[Dict[str, Any]]:
        if not self.result: return []
        records = []
        cols = self.result.get_column_names()
        while self.result.has_next():
            row = self.result.get_next()
            record = {}
            for i, val in enumerate(row):
                # Handle Kuzu Node/Rel objects for visualization compatibility
                processed_val = val
                try:
                    # Kuzu 0.11+ objects often have a specific structure
                    if hasattr(val, '__class__') and 'Node' in str(val.__class__):
                        processed_val = val
                        if not hasattr(processed_val, 'labels'):
                            processed_val.labels = [val.get_label_name()]
                        if not hasattr(processed_val, 'id'):
                           props = val.get_properties()
                           processed_val.id = props.get('uid', props.get('path', str(id(val))))
                        if not hasattr(processed_val, 'properties'):
                            processed_val.properties = val.get_properties()
                    
                    elif hasattr(val, '__class__') and 'Rel' in str(val.__class__):
                        processed_val = val
                        if not hasattr(processed_val, 'type'):
                            processed_val.type = val.get_label_name()
                        if not hasattr(processed_val, 'src_node'):
                            processed_val.src_node = val.get_src_id()['offset']
                        if not hasattr(processed_val, 'dest_node'):
                            processed_val.dest_node = val.get_dst_id()['offset']
                        if not hasattr(processed_val, 'properties'):
                            processed_val.properties = val.get_properties()
                except Exception:
                    pass
                
                record[cols[i]] = processed_val
            records.append(record)
        return records

    def data(self) -> List[Dict[str, Any]]:
        # Return raw dict data, not EmbeddedRecord.data()
        return self.data_raw()

    def __iter__(self):
        return iter([EmbeddedRecord(r) for r in self.data_raw()])
