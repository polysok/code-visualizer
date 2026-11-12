"""
This module implements the live file-watching functionality using the `watchdog` library.
It observes directories for changes and triggers updates to the code graph.
"""

import os
import threading
from pathlib import Path
import typing

from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

if typing.TYPE_CHECKING:
    from pathspec import PathSpec
    from ..tools.graph_builder import GraphBuilder
    from ..core.jobs import JobManager

from . import graph_events
from .cvizignore import build_ignore_spec
from ..tools.indexing.constants import DEFAULT_IGNORE_PATTERNS
from ..cli.config_manager import get_config_value
from ..utils.content_hash import file_content_hash
from ..utils.debug_log import debug_log, info_logger, error_logger, warning_logger


POLLING_ENV_VAR = "CVIZ_WATCH_POLLING"
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def should_use_polling_observer(use_polling: typing.Optional[bool] = None) -> bool:
    if use_polling is not None:
        return use_polling
    return os.getenv(POLLING_ENV_VAR, "").strip().lower() in TRUE_ENV_VALUES


def _watch_state_file() -> Path:
    return graph_events.events_file().with_name("watch-state.json")


_state_lock = threading.Lock()


def _read_watch_state() -> dict:
    try:
        import json

        return json.loads(_watch_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_watch_state(repo: str, ts: float) -> None:
    """Remember when *repo* was last brought up to date (best effort)."""
    import json

    with _state_lock:
        state = _read_watch_state()
        state[repo] = ts
        path = _watch_state_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass


class RepositoryEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        graph_builder: "GraphBuilder",
        repo_path: Path,
        debounce_interval=2.0,
        perform_initial_scan: bool = True,
        cvizignore_path: str = None,
        ignore_spec: "PathSpec" = None,
        sync_on_start: bool = False,
        full_resync_hours: float = 0,
        catch_up_on_start: bool = False,
        semantic_inline: bool = False,
    ):
        super().__init__()
        self.graph_builder = graph_builder
        # One-shot updates (cviz update, the page's /api/update) refresh the
        # search data before returning; the live watcher does it in a thread.
        self._semantic_inline = semantic_inline
        self.repo_path = repo_path.resolve()
        self.debounce_interval = debounce_interval
        self.timers = {}
        # Guards self.timers, and serialises the graph updates that the
        # per-path debounce timers would otherwise run concurrently.
        self._timers_lock = threading.Lock()
        self._update_lock = threading.RLock()

        self.ignore_root = self.repo_path
        self.ignore_spec = ignore_spec
        self.cvizignore_path = cvizignore_path
        self._load_ignore_spec(cvizignore_path)

        self.all_file_data = []
        self.imports_map = {}

        if sync_on_start:
            self.synchronize_with_disk()
        elif perform_initial_scan:
            self._initial_scan()
        elif catch_up_on_start:
            self.catch_up_with_disk()

        # Safety net against drift of the incremental path: periodically
        # re-synchronise the whole repository with the disk (0 = disabled).
        self._resync_interval = max(0.0, float(full_resync_hours or 0)) * 3600
        self._resync_timer: typing.Optional[threading.Timer] = None
        self._stopped = False
        self._schedule_full_resync()

    # ------------------------------------------------------------------
    # Start-up catch-up: only what changed while nothing was watching
    # ------------------------------------------------------------------

    # Above this share of the repository's files, one full synchronisation is
    # cheaper than the incremental path (which also re-links the neighbours).
    FULL_SYNC_RATIO = 0.5

    def _record_sync(self) -> None:
        _write_watch_state(self.repo_path.as_posix(), __import__("time").time())

    def changes_on_disk(self) -> typing.Tuple[typing.List[str], typing.List[str], int]:
        """Files that differ from the graph: ``(changed, removed, total files)``.

        Compares each file's content fingerprint (git's blob id formula) with
        the one stored on its ``File`` node, so whatever changed the disk -
        edits, branch switches, ``git restore``, uncommitted work - only the
        files whose content really differs are reported. A file without a
        stored fingerprint (new, or indexed before fingerprints existed)
        counts as changed.
        """
        current = {f.resolve().as_posix(): f for f in self._iter_supported_files()}
        stored = {
            p.replace("\\", "/"): h
            for p, h in self.graph_builder.get_repo_file_hashes(self.repo_path).items()
        }
        # Indexed files that are gone (or now ignored). Indexed files that
        # still exist but are not code (generic File nodes) are left alone.
        removed = sorted(
            p for p in stored
            if p not in current and (not Path(p).exists() or self._should_ignore(p))
        )
        changed = sorted(
            p for p, f in current.items()
            if stored.get(p) is None or stored[p] != file_content_hash(f)
        )
        return changed, removed, len(current)

    def catch_up_with_disk(self) -> dict:
        """Bring the graph in line with the disk, touching only what differs.

        Used when a watcher starts, by ``cviz update`` (git hooks) and by the
        page server's ``/api/update``.
        """
        changed, removed, total_files = self.changes_on_disk()
        if not changed and not removed:
            info_logger(f"[WATCH] {self.repo_path} is up to date")
            self._record_sync()
            return {"changed": 0, "removed": 0, "mode": "none"}
        if len(changed) > max(1, total_files) * self.FULL_SYNC_RATIO:
            info_logger(f"[WATCH] {len(changed)} of {total_files} files changed; full resync")
            with self._update_lock:
                self.synchronize_with_disk()
            return {"changed": len(changed), "removed": len(removed), "mode": "full"}

        info_logger(f"[WATCH] updating {len(changed)} changed and {len(removed)} removed file(s)")
        self.apply_changes(changed, removed)
        return {"changed": len(changed), "removed": len(removed), "mode": "incremental"}

    def apply_changes(self, changed: typing.Sequence[str], removed: typing.Sequence[str]) -> set:
        """Batch incremental update of several files; returns the files re-linked.

        Same steps as one file event (_handle_modification), done once for the
        whole batch: the changed files are rebuilt, then the files calling
        into them, inheriting from them or mentioning a new symbol are
        re-linked in a single pass.
        """
        with self._update_lock:
            changed = [Path(p).resolve().as_posix() for p in changed]
            removed = [Path(p).resolve().as_posix() for p in removed]
            targets = set(changed) | set(removed)

            if not self.imports_map:
                # One-shot handler: the cross-file import map is needed to
                # resolve calls; a pre-scan is much cheaper than a parse.
                self.imports_map = self.graph_builder.pre_scan_imports(self._resolution_files())
            else:
                for p in sorted(targets):
                    self._update_imports_map_for_file(Path(p))

            callers: set = set()
            for p in targets:
                callers |= {c for c in self.graph_builder.get_caller_file_paths(p) if c}
            inheritors: set = set()
            for p in changed:
                inheritors |= {c for c in self.graph_builder.get_inheritance_neighbor_paths(p) if c}
            callers = {p for p in callers if not self._should_ignore(p)} - targets
            inheritors = {p for p in inheritors if not self._should_ignore(p)} - targets

            for p in removed:
                self.graph_builder.delete_file_from_graph(p)
            new_names: set = set()
            for p in changed:
                before = self._symbol_names_in_file(p)
                self.graph_builder.update_file_in_graph(Path(p), self.repo_path, self.imports_map)
                new_names |= self._symbol_names_in_file(p) - before

            affected = set(changed) | callers | inheritors
            late = self._files_mentioning(new_names, exclude=affected | set(removed))
            if late:
                info_logger(f"[INCREMENTAL] {len(late)} file(s) mention new symbol(s); re-linking them")
            affected |= late

            others = sorted(affected - set(changed))
            if others:
                self.graph_builder.delete_outgoing_calls_from_files(others)
            if inheritors:
                self.graph_builder.delete_inherits_for_files(sorted(inheritors))

            supported = self.graph_builder.parsers.keys()
            subset = []
            for path_str in sorted(affected):
                p = Path(path_str)
                if p.exists() and p.suffix in supported and not self._should_ignore(p):
                    parsed = self.graph_builder.parse_file(self.repo_path, p)
                    if "error" not in parsed:
                        subset.append(parsed)
            file_class_lookup = self._type_lookup()
            self.graph_builder.link_function_calls(subset, self.imports_map, file_class_lookup)
            self.graph_builder.link_inheritance(subset, self.imports_map)
            self._run_optional_passes(changed)

            self.graph_builder.checkpoint()
            info_logger(f"[INCREMENTAL] {len(changed)} changed, {len(removed)} removed, "
                        f"{len(affected) - len(changed)} re-linked")
            graph_events.publish(
                graph_events.GRAPH_CHANGED, files=sorted(affected | set(removed)),
                repo=self.repo_path.as_posix(),
            )
            self._record_sync()
            if changed:
                self._semantic_sync(changed)
            return affected

    def _schedule_full_resync(self) -> None:
        if not self._resync_interval or self._stopped:
            return
        timer = threading.Timer(self._resync_interval, self._run_full_resync)
        timer.daemon = True
        self._resync_timer = timer
        timer.start()

    def _run_full_resync(self) -> None:
        try:
            with self._update_lock:
                if not self._stopped:
                    self.synchronize_with_disk()
        except Exception as exc:  # noqa: BLE001 - keep the watcher alive
            error_logger(f"[WATCH] scheduled full resync failed: {exc}")
        finally:
            self._schedule_full_resync()

    def _load_ignore_spec(self, cvizignore_path: str = None) -> None:
        if self.ignore_spec is not None:
            return
        try:
            self.ignore_spec, resolved = build_ignore_spec(
                ignore_root=self.ignore_root,
                default_patterns=DEFAULT_IGNORE_PATTERNS,
                explicit_path=cvizignore_path,
            )
            if resolved:
                debug_log(f"Watcher using ignore file: {resolved}")
        except OSError as e:
            self.ignore_spec = None
            warning_logger(f"Could not load ignore rules: {e}")

    def _should_ignore(self, path: str | Path) -> bool:
        path_obj = Path(path).resolve()
        ignore_root = getattr(self, "ignore_root", self.repo_path)

        ignore_dirs_str = get_config_value("IGNORE_DIRS") or ""
        if ignore_dirs_str:
            ignore_dirs = {d.strip().lower() for d in ignore_dirs_str.split(",") if d.strip()}
            try:
                parts = {p.lower() for p in path_obj.relative_to(ignore_root).parent.parts}
                if parts.intersection(ignore_dirs):
                    return True
            except ValueError:
                pass

        ignore_spec = getattr(self, "ignore_spec", None)
        if not ignore_spec:
            return False

        try:
            rel = path_obj.relative_to(ignore_root).as_posix()
        except ValueError:
            return False

        return ignore_spec.match_file(rel)

    def _is_supported_code_file(self, path: str | Path) -> bool:
        path_obj = Path(path)
        return (
            path_obj.is_file()
            and path_obj.suffix in self.graph_builder.parsers
            and not self._should_ignore(path_obj)
        )

    def _iter_supported_files(self) -> list[Path]:
        from ..tools.indexing.discovery import discover_files_to_index

        supported = self.graph_builder.parsers.keys()
        # Forward the explicit ignore file and re-apply this handler's own spec.
        # Without them, discovery fell back to the repo-local .cvizignore, so the
        # initial scan and disk sync indexed files that later change events
        # would skip — the graph kept entries the user asked to exclude, and
        # they went stale on edit.
        files, _ = discover_files_to_index(
            self.repo_path,
            cvizignore_path=getattr(self, "cvizignore_path", None),
            supported_extensions=set(supported),
        )
        return [f for f in files if not self._should_ignore(f)]

    # Companion sources (core.companions) are indexed with the repository but
    # not watched: their symbols take part in resolving the calls of the
    # changed files, through the import map and the type lookup.
    def _companion_files(self) -> list[Path]:
        from ..tools.indexing.discovery import discover_files_to_index
        from .companions import optional_roots

        out: list[Path] = []
        for root in optional_roots(self.repo_path):
            own_ignore = root / ".code-visualizer" / ".cvizignore"
            files, _ = discover_files_to_index(
                root, cvizignore_path=str(own_ignore) if own_ignore.is_file() else None,
                supported_extensions=set(self.graph_builder.parsers.keys()),
            )
            out.extend(files)
        return out

    def _resolution_files(self) -> list[Path]:
        return self._iter_supported_files() + self._companion_files()

    def _type_lookup(self) -> dict:
        from .companions import optional_roots

        lookup = self.graph_builder.get_repo_class_lookup(self.repo_path)
        for root in optional_roots(self.repo_path):
            lookup.update(self.graph_builder.get_repo_class_lookup(root))
        return lookup

    def _initial_scan(self):
        info_logger(f"Initial scan: {self.repo_path}")
        all_files = self._iter_supported_files()

        self.imports_map = self.graph_builder.pre_scan_imports(all_files)

        for f in all_files:
            parsed = self.graph_builder.parse_file(self.repo_path, f)
            if "error" not in parsed:
                self.all_file_data.append(parsed)

        repo_name = self.repo_path.name
        repo_path_str = self.repo_path.resolve().as_posix()

        self.graph_builder.add_repository_to_graph(self.repo_path, is_dependency=False)

        for fd in self.all_file_data:
            self.graph_builder.add_file_to_graph(
                fd, repo_name, self.imports_map, repo_path_str=repo_path_str
            )

        self.graph_builder.link_function_calls(self.all_file_data, self.imports_map)
        self.graph_builder.link_inheritance(self.all_file_data, self.imports_map)

        self.all_file_data.clear()
        info_logger("Initial scan complete")
        self.graph_builder.checkpoint()
        graph_events.publish(graph_events.INDEX_DONE, repo=repo_path_str)
        self._record_sync()
        self._semantic_sync()

    def synchronize_with_disk(self) -> None:
        info_logger(f"Syncing: {self.repo_path}")

        current_files = self._iter_supported_files()
        current_paths = {p.resolve().as_posix() for p in current_files}
        # Normalize stored paths lexically only: older Windows indexes stored
        # backslash paths, but resolving a foreign-platform path against the
        # local filesystem would prefix cwd and mark every file stale.
        indexed = {
            p.replace("\\", "/")
            for p in self.graph_builder.get_repo_file_paths(self.repo_path)
        }

        self.imports_map = self.graph_builder.pre_scan_imports(
            list(current_files) + self._companion_files())

        for stale in indexed - current_paths:
            self.graph_builder.delete_file_from_graph(stale)

        refreshed = []
        refreshed_paths: list[str] = []
        for p in current_files:
            fd = self.graph_builder.update_file_in_graph(
                p, self.repo_path, self.imports_map
            )
            if fd and "error" not in fd:
                refreshed.append(fd)
                refreshed_paths.append(p.resolve().as_posix())

        if refreshed_paths:
            # Only clear edges originating from the files we touched — do not
            # wipe the entire repo call graph like delete_relationship_links().
            self.graph_builder.delete_outgoing_calls_from_files(refreshed_paths)
            self.graph_builder.delete_inherits_for_files(refreshed_paths)
            self.graph_builder.link_function_calls(refreshed, self.imports_map)
            self.graph_builder.link_inheritance(refreshed, self.imports_map)

        info_logger("Sync complete")
        self.graph_builder.checkpoint()
        graph_events.publish(graph_events.INDEX_DONE, repo=self.repo_path.as_posix())
        self._record_sync()
        self._semantic_sync()

    def _debounce(self, event_path, action):
        # Timers are keyed per path, so N files changed inside the debounce
        # window fire N handler threads concurrently. Those handlers do
        # read-modify-write on the shared imports_map and interleave
        # delete/add/delete_outgoing_calls for overlapping caller sets, so one
        # can delete edges another just created. A branch switch or `git pull`
        # is the normal trigger. _handle_modification now takes _update_lock.
        def _run():
            # Drop the fired timer: entries were never removed, so self.timers
            # grew without bound for the life of the watcher.
            with self._timers_lock:
                if self.timers.get(event_path) is timer:
                    del self.timers[event_path]
            action()

        with self._timers_lock:
            existing = self.timers.get(event_path)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(self.debounce_interval, _run)
            self.timers[event_path] = timer
        timer.start()

    def cancel_timers(self):
        self._stopped = True
        timer = getattr(self, "_resync_timer", None)
        if timer is not None:
            timer.cancel()
        with self._timers_lock:
            for t in self.timers.values():
                t.cancel()
            self.timers.clear()

    def _update_imports_map_for_file(self, changed_path: Path):
        """Re-scan a single file and merge its contributions into self.imports_map."""
        changed_str = str(changed_path.resolve())
        for symbol in list(self.imports_map.keys()):
            old_list = self.imports_map[symbol]
            if changed_str in old_list:
                new_list = [p for p in old_list if p != changed_str]
                if new_list:
                    self.imports_map[symbol] = new_list
                else:
                    del self.imports_map[symbol]
        if changed_path.exists():
            new_map = self.graph_builder.pre_scan_imports([changed_path])
            for symbol, paths in new_map.items():
                if symbol not in self.imports_map:
                    self.imports_map[symbol] = []
                self.imports_map[symbol].extend(paths)

    def _incremental_parse_workers(self) -> int:
        """Bounded worker count for re-parsing files affected by one watch event.

        Delegates to the indexing pipeline's helper so ``PARALLEL_WORKERS`` is
        resolved in one place, including the cap of 32. The fallback stays at 4:
        the watcher runs during editing, and should not take a full-index thread
        pool on every save.
        """
        from ..tools.indexing.pipeline import (
            WATCHER_DEFAULT_PARALLEL_WORKERS,
            get_parallel_workers,
        )

        return get_parallel_workers(default=WATCHER_DEFAULT_PARALLEL_WORKERS)

    def _handle_modification(self, event_path_str: str):
        """Incremental update: re-parse and re-link only the changed file and its neighbours."""
        # Serialised: concurrent handlers previously did read-modify-write on
        # the shared imports_map (lost updates) and interleaved
        # delete_file_from_graph / add_file_to_graph /
        # delete_outgoing_calls_from_files for overlapping caller sets, so one
        # could delete edges another had just created.
        with self._update_lock:
            self._handle_modification_locked(event_path_str)

    def _handle_modification_locked(self, event_path_str: str):
        info_logger(f"File change detected (incremental update): {event_path_str}")
        changed_path = Path(event_path_str)
        if self._should_ignore(changed_path):
            debug_log(f"Ignored watcher update based on .cvizignore: {changed_path}")
            return

        changed_path_str = changed_path.resolve().as_posix()
        supported_extensions = self.graph_builder.parsers.keys()

        # A file touched without a content change (a branch switch rewrites
        # files identical on both branches, an editor re-saves) changes
        # nothing in the graph.
        if changed_path.exists():
            try:
                stored = self.graph_builder.get_file_hash(changed_path_str)
            except Exception:  # noqa: BLE001 - unknown: process the file
                stored = None
            if stored is not None and stored == file_content_hash(changed_path):
                debug_log(f"[INCREMENTAL] {changed_path_str} unchanged; skipped")
                return

        caller_paths = {
            p
            for p in self.graph_builder.get_caller_file_paths(changed_path_str)
            if p and not self._should_ignore(p)
        }
        inheritor_paths = {
            p
            for p in self.graph_builder.get_inheritance_neighbor_paths(changed_path_str)
            if p and not self._should_ignore(p)
        }
        affected_paths = {changed_path_str} | caller_paths | inheritor_paths
        info_logger(
            f"[INCREMENTAL] affected={len(affected_paths)} files "
            f"(callers={len(caller_paths)}, inheritors={len(inheritor_paths)})"
        )

        if not self.imports_map:
            # Watching without a start-up scan: build the cross-file import
            # map once (repository and companion sources), as apply_changes does.
            self.imports_map = self.graph_builder.pre_scan_imports(self._resolution_files())
        else:
            self._update_imports_map_for_file(changed_path)

        names_before = self._symbol_names_in_file(changed_path_str)
        self.graph_builder.update_file_in_graph(changed_path, self.repo_path, self.imports_map)
        names_after = self._symbol_names_in_file(changed_path_str)

        # A symbol that did not exist before may now be the target of calls
        # (or a base class) written earlier in other files. Those calls were
        # never persisted — an unresolved call leaves no trace in the graph —
        # so the files that mention the new name must be re-linked too.
        late_paths = self._files_mentioning(names_after - names_before, exclude=affected_paths)
        if late_paths:
            info_logger(
                f"[INCREMENTAL] {len(late_paths)} file(s) mention new symbol(s) "
                f"{sorted(names_after - names_before)[:5]} — re-linking them"
            )
            affected_paths |= late_paths

        # Every file in affected_paths is re-parsed below and fed back into
        # link_function_calls, so every one of them needs its outgoing CALLS
        # cleared first. Clearing only caller_paths left the inheritance-only
        # neighbours to have their edges re-created on top of the existing ones.
        # The changed file itself is excluded:
        # update_file_in_graph above already deleted and rebuilt it.
        other_callers = list(affected_paths - {changed_path_str})
        other_inheritors = list(inheritor_paths)
        if other_callers:
            self.graph_builder.delete_outgoing_calls_from_files(other_callers)
        if other_inheritors:
            self.graph_builder.delete_inherits_for_files(other_inheritors)

        subset_file_data = []
        for path_str in affected_paths:
            p = Path(path_str)
            if p.exists() and p.suffix in supported_extensions and not self._should_ignore(p):
                parsed = self.graph_builder.parse_file(self.repo_path, p)
                if "error" not in parsed:
                    subset_file_data.append(parsed)

        file_class_lookup = self._type_lookup()

        info_logger(f"[INCREMENTAL] Re-linking {len(subset_file_data)} files...")
        self.graph_builder.link_function_calls(
            subset_file_data, self.imports_map, file_class_lookup
        )
        self.graph_builder.link_inheritance(subset_file_data, self.imports_map)

        self._run_optional_passes([changed_path_str])

        self.graph_builder.checkpoint()
        info_logger(f"[INCREMENTAL] Done. Graph refresh for {event_path_str} complete! ✅")
        graph_events.publish(
            graph_events.GRAPH_CHANGED, files=affected_paths, repo=self.repo_path.as_posix()
        )
        self._record_sync()
        self._semantic_sync([changed_path_str])

    def _run_optional_passes(self, changed_paths: typing.Sequence[str]) -> None:
        """Config-gated passes after a re-link (ENABLE_VECTOR_RESOLVE / ENABLE_INHERIT_RESOLVE)."""
        try:
            from code_visualizer.cli.config_manager import get_config_value as _gcv
            _vector_enabled = (_gcv("ENABLE_VECTOR_RESOLVE") or "false").lower() == "true"
            _inherit_enabled = (_gcv("ENABLE_INHERIT_RESOLVE") or "false").lower() == "true"
        except Exception as _cfg_e:
            warning_logger(f"[PHASE4/5] Could not read config flags: {_cfg_e}")
            _vector_enabled = False
            _inherit_enabled = False

        if _vector_enabled:
            # Probe once per handler: with no backend installed, the old code
            # re-attempted (and re-failed) the import on every file event and
            # only said so at a suppressed log level (#1597).
            if not hasattr(self, "_embed_backend_ok"):
                from code_visualizer.tools.indexing.embeddings import probe_embedding_backend
                self._embed_backend_ok, _detail = probe_embedding_backend()
                if not self._embed_backend_ok:
                    error_logger(
                        f"[EMBED] ENABLE_VECTOR_RESOLVE=true but incremental embeddings "
                        f"cannot run: {_detail} (reported once; further file events skip this)"
                    )
            if self._embed_backend_ok:
                try:
                    from code_visualizer.tools.indexing.embeddings import EmbeddingPipeline
                    embed_pipeline = EmbeddingPipeline(self.graph_builder.driver)
                    for path_str in changed_paths:
                        embed_pipeline.invalidate_for_file(path_str)
                    embed_pipeline.run(str(self.repo_path))
                    info_logger(f"[EMBED] Incremental embedding complete for {len(changed_paths)} file(s)")
                except Exception as _e:
                    warning_logger(f"[EMBED] Incremental embedding failed: {_e}")

        if _inherit_enabled:
            try:
                from code_visualizer.tools.indexing.resolution.post_resolution import run_inheritance_reresolve
                _vector_resolver = None
                if _vector_enabled:
                    try:
                        from code_visualizer.tools.indexing.vector_resolver import VectorResolver
                        _vector_resolver = VectorResolver(self.graph_builder.driver)
                    except Exception as _ve:
                        warning_logger(f"[VECTOR] Resolver unavailable for watcher: {_ve}")
                n_improved = run_inheritance_reresolve(
                    self.graph_builder.driver, str(self.repo_path), _vector_resolver
                )
                info_logger(f"[INHERIT-RESOLVE] Incremental: {n_improved} edges improved")
            except Exception as _e:
                warning_logger(f"[INHERIT-RESOLVE] Incremental failed: {_e}")

    # Names that are too generic to be worth a repository scan.
    _LATE_LINK_MIN_NAME_LEN = 3

    def _symbol_names_in_file(self, path_str: str) -> set:
        try:
            with self.graph_builder.driver.session() as session:
                result = session.run(
                    "MATCH (n) WHERE n.path = $path "
                    "AND (n:Function OR n:Class OR n:Interface OR n:Struct OR n:Enum OR n:Trait OR n:Record) "
                    "RETURN DISTINCT n.name AS name",
                    path=path_str,
                )
                rows = result.data() if hasattr(result, "data") else [dict(r) for r in result]
        except Exception as exc:  # noqa: BLE001 - best effort
            debug_log(f"[INCREMENTAL] could not list symbols of {path_str}: {exc}")
            return set()
        return {r["name"] for r in rows if r.get("name") and r["name"] != "<module>"}

    def _files_mentioning(self, names: set, exclude: set) -> set:
        """Supported files (outside *exclude*) whose text contains a name.

        Bounded by ``CVIZ_WATCH_LATE_LINK_MAX_FILES`` (default 200): beyond
        that the change is too broad for an incremental pass and a full
        ``cviz update`` / scheduled resync is the right tool.
        """
        import re as _re

        names = {n for n in names if len(n) >= self._LATE_LINK_MIN_NAME_LEN}
        if not names:
            return set()
        try:
            limit = int(os.getenv("CVIZ_WATCH_LATE_LINK_MAX_FILES", "200"))
        except ValueError:
            limit = 200
        pattern = _re.compile(
            rb"(?<![A-Za-z0-9_])(?:" + b"|".join(_re.escape(n.encode()) for n in sorted(names)) + rb")(?![A-Za-z0-9_])"
        )
        found: set = set()
        for f in self._iter_supported_files():
            p = f.resolve().as_posix()
            if p in exclude:
                continue
            try:
                if pattern.search(f.read_bytes()):
                    found.add(p)
            except OSError:
                continue
            if len(found) > limit:
                warning_logger(
                    f"[INCREMENTAL] new symbol(s) {sorted(names)[:5]} mentioned in more than {limit} "
                    "files; skipping late re-link. Run 'cviz update' to reconcile."
                )
                return set()
        return found

    def on_created(self, event):
        if not event.is_directory and self._is_supported_code_file(event.src_path):
            self._debounce(event.src_path, lambda: self._handle_modification(event.src_path))

    def on_modified(self, event):
        if not event.is_directory and self._is_supported_code_file(event.src_path):
            self._debounce(event.src_path, lambda: self._handle_modification(event.src_path))

    def on_deleted(self, event):
        if not event.is_directory:
            self._debounce(event.src_path, lambda: self._handle_modification(event.src_path))

    def on_moved(self, event):
        if event.is_directory:
            return
        # Both endpoints matter. Only dest_path was handled, so the node for
        # the *old* path and all of its symbols stayed in the graph forever:
        # every rename duplicated every symbol in the file, and a normal
        # refactoring session or a `git checkout` between branches accumulated
        # them indefinitely until find_callers started returning dead paths.
        src_path = getattr(event, "src_path", None)
        if src_path:
            self._debounce(src_path, lambda: self._handle_removal(src_path))
        self._debounce(event.dest_path, lambda: self._handle_modification(event.dest_path))

    def _handle_removal(self, path_str):
        """Drop a path that no longer exists (the source side of a rename)."""
        with self._update_lock:
            try:
                removed = Path(path_str).resolve().as_posix()
                # Files calling into the removed one lose those edges with the
                # node; re-link them so their calls follow the symbol to its
                # new location (rename/move) instead of silently vanishing.
                callers = {
                    p
                    for p in self.graph_builder.get_caller_file_paths(removed)
                    if p and p != removed and not self._should_ignore(p)
                }
                self.graph_builder.delete_file_from_graph(str(Path(path_str).resolve()))
                info_logger(f"[WATCH] removed stale node for moved file: {path_str}")
                # Forget the symbols the old path exported, or callers would
                # keep resolving to a file that no longer exists.
                self._update_imports_map_for_file(Path(path_str))
                relinked = self._relink_files(callers)
                self.graph_builder.checkpoint()
                graph_events.publish(
                    graph_events.GRAPH_CHANGED,
                    files=[removed, *relinked],
                    repo=self.repo_path.as_posix(),
                )
            except Exception as exc:  # noqa: BLE001 - a watcher must not die on one file
                error_logger(f"[WATCH] failed to remove {path_str}: {exc}")

    # -- natural-language search (semantic/) ---------------------------------
    def _semantic_sync(self, paths=None) -> None:
        """Refresh the search data of *paths* in the graph (whole repo when None).

        Runs in a background thread: embedding must not delay the next file
        event. Does nothing unless SEMANTIC_SEARCH is enabled.
        """
        try:
            from code_visualizer.semantic import settings as ai_settings
            if not ai_settings.enabled():
                return
        except Exception:  # noqa: BLE001
            return

        repo = self.repo_path.resolve().as_posix()
        driver = self.graph_builder.driver

        if self._semantic_inline:
            try:
                from code_visualizer.semantic.indexer import sync
                result = sync(driver.session, repo, paths)
                if result.get("error"):
                    warning_logger(f"[SEARCH] embeddings not updated: {result['error']}")
            except Exception as exc:  # noqa: BLE001 - search must never break an update
                warning_logger(f"[SEARCH] index refresh failed: {exc}")
            return

        def _run():
            try:
                from code_visualizer.semantic.indexer import sync
                result = sync(driver.session, repo, paths)
                if result.get("error"):
                    warning_logger(f"[SEARCH] embeddings not updated: {result['error']}")
            except Exception as exc:  # noqa: BLE001 - search must never break the watcher
                warning_logger(f"[SEARCH] index refresh failed: {exc}")

        threading.Thread(target=_run, name="cviz-semantic-sync", daemon=True).start()

    def _relink_files(self, paths: set) -> list:
        """Re-parse *paths* and rebuild their outgoing CALLS edges."""
        supported = self.graph_builder.parsers.keys()
        existing = [
            p for p in sorted(paths)
            if Path(p).exists() and Path(p).suffix in supported and not self._should_ignore(p)
        ]
        if not existing:
            return []
        self.graph_builder.delete_outgoing_calls_from_files(existing)
        parsed = []
        for p in existing:
            fd = self.graph_builder.parse_file(self.repo_path, Path(p))
            if "error" not in fd:
                parsed.append(fd)
        file_class_lookup = self._type_lookup()
        self.graph_builder.link_function_calls(parsed, self.imports_map, file_class_lookup)
        return existing


def incremental_update(
    graph_builder: "GraphBuilder",
    repo_path: typing.Union[str, Path],
    cvizignore_path: typing.Optional[str] = None,
) -> dict:
    """One-shot update of an indexed repository to the state of the disk.

    Only the files whose content differs from the graph are re-processed (see
    :meth:`RepositoryEventHandler.changes_on_disk`). The search data of the
    changed files is refreshed before returning.
    """
    handler = RepositoryEventHandler(
        graph_builder,
        Path(repo_path),
        perform_initial_scan=False,
        cvizignore_path=cvizignore_path,
        semantic_inline=True,
    )
    try:
        result = handler.catch_up_with_disk()
    finally:
        handler.cancel_timers()
    if result["mode"] != "none":
        graph_events.publish(graph_events.INDEX_DONE, repo=handler.repo_path.as_posix())
    return result


class CodeWatcher:
    def __init__(
        self,
        graph_builder: "GraphBuilder",
        job_manager="JobManager",
        use_polling: typing.Optional[bool] = None,
    ):
        self.graph_builder = graph_builder
        observer_cls = PollingObserver if should_use_polling_observer(use_polling) else Observer
        self.observer = observer_cls()

        self.watched_paths = set()
        self.watches = {}
        self.handlers = {}

    def watch_directory(
        self,
        path: str,
        perform_initial_scan: bool = True,
        cvizignore_path: str = None,
        sync_on_start: bool = False,
        full_resync_hours: float = 0,
        catch_up_on_start: bool = False,
    ):
        path_obj = Path(path).resolve()
        path_str = str(path_obj)

        if path_str in self.watched_paths:
            return {"message": "Already watching"}

        extra: dict = {}
        if full_resync_hours:
            extra["full_resync_hours"] = full_resync_hours
        if catch_up_on_start:
            extra["catch_up_on_start"] = True
        handler = RepositoryEventHandler(
            self.graph_builder,
            path_obj,
            perform_initial_scan=perform_initial_scan,
            sync_on_start=sync_on_start,
            cvizignore_path=cvizignore_path,
            **extra,
        )

        watch = self.observer.schedule(handler, path_str, recursive=True)

        self.watches[path_str] = watch
        self.handlers[path_str] = handler
        self.watched_paths.add(path_str)

        return {"message": f"Watching {path_str}"}

    def unwatch_directory(self, path: str):
        path_str = str(Path(path).resolve())

        handler = self.handlers.pop(path_str, None)
        if handler:
            handler.cancel_timers()

        watch = self.watches.pop(path_str, None)
        if watch:
            self.observer.unschedule(watch)

        self.watched_paths.discard(path_str)

        return {"message": f"Stopped watching {path_str}"}

    def list_watched_paths(self):
        return list(self.watched_paths)

    def start(self):
        if not self.observer.is_alive():
            self.observer.start()

    def stop(self):
        for h in self.handlers.values():
            h.cancel_timers()
        self.handlers.clear()

        # A stopped watchdog Observer cannot be restarted, so the watch state
        # must not survive it: a stale entry makes watch_directory answer
        # "Already watching" for a dead observer and the graph silently goes
        # stale (#1519).
        self.watched_paths.clear()
        self.watches.clear()

        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join()
