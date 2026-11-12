"""C# calls into types that live outside the indexed sources (NuGet packages, DLLs).

The call resolver can only link a call to a node it indexed. A C# call whose
target type comes from a package therefore fell back to a guess (tier 9) whose
target does not exist, and the writer dropped the edge without a word: on a
real ASP.NET solution 87% of the calls vanished that way.

When the type the call is made on is known from the source alone, the call is
redirected to an ``ExternalClass`` node named after that type (simple name, the
key the INHERITS writer already uses for external parents), and the called
member is kept on the edge (``called_name``). The type is known when:

- the receiver is a field, property, parameter or local whose declared type the
  parser recorded (``inferred_obj_type``), e.g. ``_logger.LogInformation()``;
- the receiver is written as a type, e.g. ``Guid.NewGuid()``,
  ``string.IsNullOrEmpty()``, ``System.IO.Path.Combine()``;
- the call creates an object, e.g. ``new ArgumentNullException(...)``;
- the call has no receiver and the enclosing class inherits, directly or
  through classes of the repository, from an external class, e.g. ``Ok()`` in
  a controller deriving from ``ControllerBase``;
- the receiver is a property inherited from a well-known framework base class,
  e.g. ``User.FindFirst()`` in a controller is ``ClaimsPrincipal.FindFirst``.

Nothing here needs the .NET SDK or a package restore.
"""

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

EXTERNAL_CLASS_LABEL = "ExternalClass"

# C# keywords that alias a framework type: `string.Join` is `String.Join`.
_KEYWORD_TYPES = {
    "string": "String", "object": "Object", "bool": "Boolean", "byte": "Byte",
    "sbyte": "SByte", "char": "Char", "decimal": "Decimal", "double": "Double",
    "float": "Single", "int": "Int32", "uint": "UInt32", "long": "Int64",
    "ulong": "UInt64", "short": "Int16", "ushort": "UInt16",
    "nint": "IntPtr", "nuint": "UIntPtr",
}

# Namespace roots that never collide with a member name in practice.
_FRAMEWORK_ROOTS = ("System", "Microsoft")

# Bare names the parser captures as calls but that call nothing.
_NOT_CALLS = {"nameof", "typeof", "sizeof", "default", "checked", "unchecked"}

# Properties inherited from framework base classes, with their type. Without
# it `User.FindFirst()` in a controller reads as a static call on a type named
# `User`. Only bases whose members are used this way in practice are listed.
_HTTP_MEMBERS = {
    "HttpContext": "HttpContext", "Request": "HttpRequest", "Response": "HttpResponse",
    "User": "ClaimsPrincipal", "RouteData": "RouteData",
}
_CONTROLLER_BASE_MEMBERS = {
    **_HTTP_MEMBERS,
    "ModelState": "ModelStateDictionary", "Url": "IUrlHelper",
    "ControllerContext": "ControllerContext", "ObjectValidator": "IObjectModelValidator",
    "ProblemDetailsFactory": "ProblemDetailsFactory", "MetadataProvider": "IModelMetadataProvider",
}
INHERITED_MEMBER_TYPES: Dict[str, Dict[str, str]] = {
    "ControllerBase": _CONTROLLER_BASE_MEMBERS,
    "Controller": {
        **_CONTROLLER_BASE_MEMBERS,
        "ViewData": "ViewDataDictionary", "TempData": "ITempDataDictionary",
    },
    "PageModel": {
        **_HTTP_MEMBERS,
        "ModelState": "ModelStateDictionary", "Url": "IUrlHelper",
        "PageContext": "PageContext", "TempData": "ITempDataDictionary",
    },
    "ComponentBase": {},
    "Hub": {"Clients": "IHubCallerClients", "Groups": "IGroupManager", "Context": "HubCallerContext"},
    "DbContext": {"Database": "DatabaseFacade", "ChangeTracker": "ChangeTracker", "Model": "IModel"},
}

_PASCAL_SEGMENT = re.compile(r"[A-Z][A-Za-z0-9_]*\Z")
_INTERFACE_NAME = re.compile(r"I[A-Z]")


def _strip_generics(text: str) -> str:
    return text.split("<", 1)[0].strip()


def simple_type_name(text: str) -> str:
    """`Ns.Base<T>` -> `Base`."""
    return _strip_generics(text).rsplit(".", 1)[-1]


def _receiver_text(call: Dict[str, Any]) -> Optional[str]:
    """`Ns.Type.Method<T>` -> `Ns.Type`; None for a bare call."""
    full = (call.get("full_name") or "").strip()
    name = call.get("name") or ""
    if not full or not name:
        return None
    cut = full.rfind("." + name)
    if cut <= 0:
        return None
    return full[:cut].strip()


def _namespaces(file_data: Dict[str, Any]) -> Set[str]:
    out: Set[str] = set()
    for imp in file_data.get("imports", []):
        ns = imp.get("name") or ""
        if ns and not imp.get("alias"):
            out.add(ns)
    package = file_data.get("package_name")
    if package:
        out.add(package)
    return out


def _is_namespace(prefix: str, namespaces: Iterable[str]) -> bool:
    if prefix.split(".", 1)[0] in _FRAMEWORK_ROOTS:
        return True
    for ns in namespaces:
        if ns == prefix or ns.startswith(prefix + ".") or prefix.startswith(ns + "."):
            return True
    return False


def external_base_class(
    class_name: Optional[str],
    class_bases: Mapping[str, List[str]],
    repo_type_names: Set[str],
) -> Optional[str]:
    """First external class the repository class *class_name* derives from.

    Walks the bases declared in the repository; the first base not declared
    there and not named like an interface (`IFoo`) is the external parent.
    """
    seen: Set[str] = set()
    current = class_name
    while current and current not in seen:
        seen.add(current)
        bases = [simple_type_name(b) for b in class_bases.get(current, [])]
        next_internal = None
        for base in bases:
            if base in repo_type_names:
                if next_internal is None and base in class_bases:
                    next_internal = base
            elif not _INTERFACE_NAME.match(base):
                return base
        current = next_internal
    return None


def _enclosing_class(call: Dict[str, Any]) -> Optional[str]:
    ctx = call.get("class_context")
    if isinstance(ctx, (list, tuple)) and ctx:
        return ctx[0]
    return None


def external_receiver_type(
    call: Dict[str, Any],
    file_data: Dict[str, Any],
    repo_type_names: Set[str],
    local_member_names: Set[str],
    class_bases: Optional[Mapping[str, List[str]]] = None,
) -> Optional[str]:
    """Simple name of the external type a C# call is made on, or None.

    *repo_type_names* holds every type declared in the indexed sources: a type
    found there is internal and left to the normal resolver.
    *local_member_names* holds the fields, properties and variables declared in
    the caller's file, so `Logger.Log()` on a property named `Logger` is not
    taken for a static call on a type named `Logger`.
    *class_bases* maps each repository class to its declared bases.
    """
    class_bases = class_bases or {}
    name = call.get("name") or ""

    if call.get("is_constructor"):
        created = simple_type_name(name)
        return None if not created or created in repo_type_names else created

    inferred = call.get("inferred_obj_type")
    if inferred:
        simple = _KEYWORD_TYPES.get(inferred, inferred)
        return None if simple in repo_type_names else simple

    receiver = _receiver_text(call)
    if not receiver:
        # Bare `Ok()`: a member inherited from an external base class.
        if name in _NOT_CALLS or not _PASCAL_SEGMENT.match(name) or name in local_member_names:
            return None
        return external_base_class(_enclosing_class(call), class_bases, repo_type_names)

    receiver = _strip_generics(receiver)
    if receiver in _KEYWORD_TYPES:
        return _KEYWORD_TYPES[receiver]

    segments = receiver.split(".")
    if not all(_PASCAL_SEGMENT.match(s) for s in segments):
        return None  # `_field.M()`, `x.Prop.M()`, `Foo().M()`, `(T)x`…

    # `User.FindFirst()` in a controller: a property inherited from the base.
    base = external_base_class(_enclosing_class(call), class_bases, repo_type_names)
    if base and segments[0] in INHERITED_MEMBER_TYPES.get(base, {}):
        return INHERITED_MEMBER_TYPES[base][segments[0]] if len(segments) == 1 else None

    type_name = segments[-1]
    if type_name in repo_type_names:
        return None
    if len(segments) == 1:
        if type_name in local_member_names:
            return None
        return type_name
    # `A.B.C.M()`: a qualified type only when `A.B` is a namespace; otherwise a
    # member chain such as `HttpContext.User.M()` or `Encoding.UTF8.M()`.
    if not _is_namespace(".".join(segments[:-1]), _namespaces(file_data)):
        return None
    return type_name


def local_member_names(file_data: Dict[str, Any]) -> Set[str]:
    names = {p.get("name") for p in file_data.get("properties", [])}
    names.update(v.get("name") for v in file_data.get("variables", []))
    names.discard(None)
    return names  # type: ignore[return-value]


def repo_class_bases(all_file_data: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Declared bases of every C# class and record of the repository, by name."""
    out: Dict[str, List[str]] = {}
    for fd in all_file_data:
        if fd.get("lang") != "c_sharp":
            continue
        for label in ("classes", "records"):
            for item in fd.get(label, []):
                if item.get("name") and item.get("bases"):
                    out.setdefault(item["name"], list(item["bases"]))
    return out


def external_call_row(resolved: Dict[str, Any], type_name: str) -> Dict[str, Any]:
    """Re-target a resolved call row at the ExternalClass *type_name*."""
    return {
        **resolved,
        "target_label": EXTERNAL_CLASS_LABEL,
        "external_type": type_name,
        "called_file_path": "",
        "called_line_number": 0,
        "called_context": type_name,
        "confidence": 0.6,
        "confidence_label": "INFERRED",
        "resolution_tier": 5,
    }
