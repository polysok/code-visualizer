"""Receiver-type inference for TypeScript and Swift method calls.

Same contract as ``rust_types``: the parser describes a call's receiver as a
base type plus steps (``["field", name]``, ``["await"]``); the resolver
evaluates that chain here, reading class/struct property types across files
through the pre-scan ``imports_map`` (type name -> files).

Before this, ``this.mu.clear()`` on a ``Map`` was bound to an unrelated project
method ``Store.clear`` because ``clear`` was the only project symbol with that
name, and Swift calls on typed properties and parameters were dropped because
their type was recorded as ``Unknown``. A receiver whose type is outside the
project now yields no edge; one whose type is in the project binds to that
type's method.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

MAX_STEPS = 12

_TS_NULLISH = {"null", "undefined", "void"}


# ---------------------------------------------------------------------------
# Type names
# ---------------------------------------------------------------------------


def _strip_generics(text: str) -> str:
    depth, out = 0, []
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _generic_args(text: str) -> list:
    if "<" not in text or not text.rstrip().endswith(">"):
        return []
    inner = text[text.index("<") + 1: text.rstrip().rindex(">")]
    parts, depth, cur = [], 0, []
    for ch in inner:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _split_union(text: str) -> list:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


def type_name(lang: str, text: Optional[str]) -> Optional[str]:
    """Simple name of a declared type, or None when it is not a named type."""
    t = (text or "").strip()
    if t.startswith(":"):
        t = t[1:].strip()
    if not t:
        return None
    if lang == "typescript":
        t = re.sub(r"^(readonly|unique)\s+", "", t)
        members = [m for m in _split_union(t) if m not in _TS_NULLISH]
        if len(members) != 1:
            return None
        t = members[0]
        if t.endswith("[]"):
            return "Array"
        if t[:1] in "({['\"`" or t.startswith(("typeof ", "keyof ")):
            return None
    elif lang == "swift":
        t = re.sub(r"^(some|any|inout)\s+", "", t).rstrip("?!").strip()
        if t.startswith("["):
            return "Dictionary" if ":" in t else "Array"
        if t[:1] == "(":
            return None
    name = _strip_generics(t).strip().split(".")[-1].strip()
    if not name or not (name[0].isalpha() or name[0] == "_"):
        return None
    return name


def unwrap_await(lang: str, text: Optional[str]) -> Optional[str]:
    """``Promise<T>`` -> ``T`` (TypeScript ``await``)."""
    if lang == "typescript" and text and _strip_generics(text).strip().split(".")[-1] in ("Promise", "PromiseLike"):
        args = _generic_args(text)
        return args[0] if args else None
    return text


# ---------------------------------------------------------------------------
# Property types, parsed on demand and cached per file
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_field_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Dict[str, str]]]] = {}


def _parser_for(lang: str, path: str):
    from code_visualizer.utils.tree_sitter_manager import get_tree_sitter_manager

    manager = get_tree_sitter_manager()
    if lang == "typescript" and path.endswith(".tsx"):
        try:
            return manager.create_parser("tsx")
        except Exception:  # noqa: BLE001
            pass
    return manager.create_parser(lang)


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _ts_fields(root) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ("class_declaration", "abstract_class_declaration", "class", "interface_declaration"):
            name = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name is not None and body is not None:
                fields: Dict[str, str] = {}
                for member in body.named_children:
                    if member.type in ("public_field_definition", "property_signature"):
                        fname, ftype = member.child_by_field_name("name"), member.child_by_field_name("type")
                        if fname is not None and ftype is not None:
                            fields[_text(fname)] = _text(ftype)
                        elif fname is not None:
                            value = member.child_by_field_name("value")
                            if value is not None and value.type == "new_expression":
                                ctor = value.child_by_field_name("constructor")
                                if ctor is not None:
                                    fields[_text(fname)] = _text(ctor)
                    elif member.type == "method_definition" and member.child_by_field_name("name") is not None \
                            and _text(member.child_by_field_name("name")) == "constructor":
                        params = member.child_by_field_name("parameters")
                        for param in (params.named_children if params is not None else []):
                            is_property = any(c.type in ("accessibility_modifier", "readonly")
                                              or _text(c) == "readonly" for c in param.children)
                            pattern, ptype = param.child_by_field_name("pattern"), param.child_by_field_name("type")
                            if is_property and pattern is not None and ptype is not None:
                                fields[_text(pattern)] = _text(ptype)
                result.setdefault(_text(name), fields)
        stack.extend(node.named_children)
    return result


def _swift_fields(root) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ("class_declaration", "protocol_declaration"):
            name = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name is not None and body is not None:
                type_key = _text(name)
                fields = result.setdefault(type_key, {})  # extensions add to the same type
                for member in body.named_children:
                    if member.type not in ("property_declaration", "protocol_property_declaration"):
                        continue
                    fname, ftype = swift_property(member)
                    if fname and ftype:
                        fields.setdefault(fname, ftype)
        stack.extend(node.named_children)
    return result


def swift_property(node) -> Tuple[Optional[str], Optional[str]]:
    """(name, type text) of a Swift ``property_declaration``."""
    name = None
    pattern = node.child_by_field_name("name")
    if pattern is not None:
        ident = pattern.child_by_field_name("bound_identifier")
        if ident is None:
            ident = next((c for c in pattern.named_children if c.type == "simple_identifier"), None)
        name = _text(ident) if ident is not None else None
    ptype = None
    for child in node.named_children:
        if child.type == "type_annotation":
            inner = child.child_by_field_name("name") or (child.named_children[0] if child.named_children else None)
            ptype = _text(inner) if inner is not None else None
    if ptype is None:
        value = node.child_by_field_name("value")
        if value is not None and value.type in ("call_expression", "constructor_expression"):
            callee = value.named_children[0] if value.named_children else None
            if callee is not None and callee.type in ("simple_identifier", "user_type"):
                callee_text = _text(callee)
                if callee_text[:1].isupper():
                    ptype = callee_text
    return name, ptype


def fields_in_file(lang: str, path: str) -> Dict[str, Dict[str, str]]:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    key = (lang, path)
    with _cache_lock:
        hit = _field_cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        tree = _parser_for(lang, path).parse(Path(path).read_bytes())
        result = _ts_fields(tree.root_node) if lang == "typescript" else _swift_fields(tree.root_node)
    except Exception:  # noqa: BLE001 - inference is best effort
        result = {}
    with _cache_lock:
        _field_cache[key] = (mtime, result)
    return result


def _field_type(lang: str, type_key: str, field: str, imports_map: Dict[str, Sequence[str]],
                preferred: Sequence[str]) -> Optional[str]:
    for path in list(preferred) + list(imports_map.get(type_key, []) or []):
        fields = fields_in_file(lang, path).get(type_key)
        if fields is not None and field in fields:
            return fields[field]
    return None


# ---------------------------------------------------------------------------
# Chain evaluation
# ---------------------------------------------------------------------------


def evaluate_receiver(chain: Dict[str, Any], imports_map: Dict[str, Sequence[str]],
                      caller_path: Optional[str] = None) -> Optional[str]:
    """Simple type name of the receiver described by ``chain``, or None."""
    lang = chain.get("lang")
    current = chain.get("base")
    steps = chain.get("steps") or []
    if lang not in ("typescript", "swift") or not current or len(steps) > MAX_STEPS:
        return None
    preferred = [caller_path] if caller_path else []
    for step in steps:
        if step[0] == "await":
            current = unwrap_await(lang, current)
            if current is None:
                return None
            continue
        if step[0] != "field":
            return None
        owner = type_name(lang, current)
        if owner is None:
            return None
        current = _field_type(lang, owner, step[1], imports_map, preferred)
        if current is None:
            return None
    return type_name(lang, current)
