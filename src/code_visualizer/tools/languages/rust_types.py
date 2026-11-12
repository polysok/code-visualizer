"""Receiver-type inference for Rust method calls.

Without it, `run.control.lock()` (a `std::sync::Mutex`) was resolved by name
alone to whatever project function was called `lock`, producing confident-
looking but wrong edges. The parser describes a receiver as a *base type* plus
a list of *steps* (field accesses, method calls, `?`, `.await`); the resolver
evaluates that chain here, looking struct fields up across files through the
pre-scan `imports_map` (struct name -> files).

The evaluation is deliberately conservative: any step it cannot follow returns
``None`` and the call keeps its previous resolution behaviour. When it does
reach a type, the resolver only links the call to a method of that type — a
type outside the project (``Mutex``, ``Vec``, ``HashMap``…) means "external
call", so no edge is created.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Smart pointers / guards / references whose methods and fields are those of
# the wrapped type (auto-deref).
DEREF_WRAPPERS = frozenset({
    "Arc", "Rc", "Box", "State", "Cow", "Pin", "Ref", "RefMut", "Weak",
    "MutexGuard", "RwLockReadGuard", "RwLockWriteGuard", "OwnedMutexGuard",
    "MappedMutexGuard", "ManuallyDrop",
})

# `container.method()` returning (something that derefs to) the first type
# argument: Mutex::lock, RefCell::borrow, Option::unwrap, Result::expect...
_INNER_ACCESSORS = {
    "Mutex": {"lock", "try_lock", "blocking_lock", "lock_owned", "get_mut", "into_inner"},
    "RwLock": {"read", "write", "try_read", "try_write", "blocking_read", "blocking_write",
               "get_mut", "into_inner"},
    "RefCell": {"borrow", "borrow_mut", "try_borrow", "try_borrow_mut", "get_mut", "into_inner"},
    "Cell": {"get", "get_mut", "into_inner"},
    "OnceCell": {"get", "get_or_init"},
    "OnceLock": {"get", "get_or_init"},
    "Option": {"unwrap", "expect", "unwrap_or_default", "unwrap_unchecked", "as_ref", "as_mut",
               "as_deref", "as_deref_mut", "take", "get_or_insert_with", "insert"},
    "Result": {"unwrap", "expect", "unwrap_or_default", "unwrap_unchecked"},
    "LockResult": {"unwrap", "expect"},
}
# Methods that keep the receiver's type.
_SAME_TYPE_METHODS = frozenset({"clone", "to_owned", "borrow", "as_ref", "as_mut", "deref", "deref_mut",
                                "unwrap", "expect"})

MAX_STEPS = 12


# ---------------------------------------------------------------------------
# Type text handling
# ---------------------------------------------------------------------------


def _split_top_level(text: str, sep: str = ",") -> List[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def parse_type(text: Optional[str]) -> Tuple[Optional[str], List[str]]:
    """``&'a mut tokio::sync::Mutex<Option<S>>`` -> (``Mutex``, [``Option<S>``])."""
    t = (text or "").strip()
    while True:
        before = t
        if t.startswith("&"):
            t = t[1:].lstrip()
        if t.startswith("'"):
            t = t.split(None, 1)[1] if " " in t else ""
        for prefix in ("mut ", "dyn ", "impl ", "const "):
            if t.startswith(prefix):
                t = t[len(prefix):].lstrip()
        if t == before:
            break
    if not t or t[0] in "([" or t.startswith("fn"):
        return None, []
    if "+" in t and "<" not in t.split("+", 1)[0]:
        t = t.split("+", 1)[0].strip()  # `Trait + Send`
    name, args = t, []
    if "<" in t and t.endswith(">"):
        name = t[: t.index("<")]
        args = [a for a in _split_top_level(t[t.index("<") + 1: -1]) if a and not a.startswith("'")]
    name = name.strip().rsplit("::", 1)[-1]
    if not name or not (name[0].isalpha() or name[0] == "_"):
        return None, []
    return name, args


def deref(text: Optional[str], self_type: Optional[str] = None) -> Optional[str]:
    """Strip auto-deref wrappers: ``State<'_, Arc<Run>>`` -> ``Run``."""
    current = text
    for _ in range(8):
        name, args = parse_type(current)
        if name == "Self" and self_type:
            current = self_type
            continue
        if name in DEREF_WRAPPERS and args:
            current = args[0]
            continue
        return current
    return current


def simple_name(text: Optional[str], self_type: Optional[str] = None) -> Optional[str]:
    name, _ = parse_type(deref(text, self_type))
    if name == "Self":
        name, _ = parse_type(deref(self_type))
    return name


# ---------------------------------------------------------------------------
# Struct fields, parsed on demand and cached per file
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_field_cache: Dict[str, Tuple[float, Dict[str, Dict[str, str]]]] = {}


def _rust_parser():
    from code_visualizer.utils.tree_sitter_manager import get_tree_sitter_manager

    manager = get_tree_sitter_manager()
    return manager.create_parser("rust")


def struct_fields_in_file(path: str) -> Dict[str, Dict[str, str]]:
    """``{struct: {field: type_text}}`` for named-field structs in *path*."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _cache_lock:
        hit = _field_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    result: Dict[str, Dict[str, str]] = {}
    try:
        source = Path(path).read_bytes()
        tree = _rust_parser().parse(source)
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "struct_item":
                name = node.child_by_field_name("name")
                body = node.child_by_field_name("body")
                if name is not None and body is not None and body.type == "field_declaration_list":
                    fields: Dict[str, str] = {}
                    for decl in body.named_children:
                        if decl.type != "field_declaration":
                            continue
                        fname = decl.child_by_field_name("name")
                        ftype = decl.child_by_field_name("type")
                        if fname is not None and ftype is not None:
                            fields[fname.text.decode("utf-8", "replace")] = ftype.text.decode("utf-8", "replace")
                    result.setdefault(name.text.decode("utf-8", "replace"), fields)
            stack.extend(node.named_children)
    except Exception:  # noqa: BLE001 - inference is best effort
        result = {}
    with _cache_lock:
        _field_cache[path] = (mtime, result)
    return result


def _field_type(struct: str, field: str, imports_map: Dict[str, Sequence[str]],
                preferred_paths: Sequence[str] = ()) -> Optional[str]:
    for path in list(preferred_paths) + list(imports_map.get(struct, []) or []):
        fields = struct_fields_in_file(path).get(struct)
        if fields is not None and field in fields:
            return fields[field]
    return None


# ---------------------------------------------------------------------------
# Chain evaluation
# ---------------------------------------------------------------------------


def evaluate_receiver(chain: Dict[str, Any], imports_map: Dict[str, Sequence[str]],
                      caller_path: Optional[str] = None) -> Optional[str]:
    """Simple type name of a receiver described by the parser, or ``None``.

    ``chain`` = ``{"base": type_text, "self_type": impl type or None,
    "steps": [["field", name] | ["call", method] | ["try"] | ["await"]]}``.
    """
    current = chain.get("base")
    self_type = chain.get("self_type")
    steps = chain.get("steps") or []
    if not current or len(steps) > MAX_STEPS:
        return None
    preferred = [caller_path] if caller_path else []
    for step in steps:
        kind = step[0]
        resolved = deref(current, self_type)
        name, args = parse_type(resolved)
        if name is None:
            return None
        if kind == "field":
            ftype = _field_type(name, step[1], imports_map, preferred)
            if ftype is None:
                return None
            current = ftype.replace("Self", self_type) if self_type and "Self" in ftype else ftype
        elif kind == "call":
            method = step[1]
            if method in _INNER_ACCESSORS.get(name, ()) and args:
                current = args[0]
            elif method in _SAME_TYPE_METHODS:
                current = resolved
            else:
                return None  # unknown return type
        elif kind == "try":
            current = args[0] if name in ("Result", "Option", "LockResult") and args else resolved
        elif kind == "await":
            current = resolved
        else:
            return None
    return simple_name(current, self_type)
