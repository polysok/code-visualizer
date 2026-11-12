"""Which NuGet package, if any, each external C# type comes from.

Read from the sources alone, with no .NET SDK and no package restore:

- the ``.csproj`` files (``<PackageReference>``, ``<ProjectReference>``,
  ``<Using>``, ``<ImplicitUsings>``, the SDK) and the ``Directory.Build.props``
  / ``Directory.Packages.props`` files above them;
- the ``using`` directives and the namespace of each file calling the type.

A type is attributed to a package when the namespaces visible at every call
site agree on one: ``using Newtonsoft.Json;`` + ``<PackageReference
Include="Newtonsoft.Json">`` makes ``JsonConvert`` a ``Newtonsoft.Json`` type.
``System.*`` / ``Microsoft.*`` namespaces no declared package covers are the
.NET framework. When several packages stay possible the type is marked
``ambiguous`` with its candidates, rather than guessed.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ORIGIN_PACKAGE = "package"
ORIGIN_FRAMEWORK = "framework"
ORIGIN_AMBIGUOUS = "ambiguous"
ORIGIN_UNKNOWN = "unknown"
_FRAMEWORK = "<framework>"

# Namespaces `<ImplicitUsings>enable</ImplicitUsings>` adds, per SDK.
_IMPLICIT_BASE = (
    "System", "System.Collections.Generic", "System.IO", "System.Linq",
    "System.Net.Http", "System.Threading", "System.Threading.Tasks",
)
_IMPLICIT_HOSTING = (
    "Microsoft.Extensions.Configuration", "Microsoft.Extensions.DependencyInjection",
    "Microsoft.Extensions.Hosting", "Microsoft.Extensions.Logging",
)
IMPLICIT_USINGS = {
    "Microsoft.NET.Sdk": _IMPLICIT_BASE,
    "Microsoft.NET.Sdk.Web": _IMPLICIT_BASE + _IMPLICIT_HOSTING + (
        "System.Net.Http.Json", "Microsoft.AspNetCore.Builder", "Microsoft.AspNetCore.Hosting",
        "Microsoft.AspNetCore.Http", "Microsoft.AspNetCore.Routing",
    ),
    "Microsoft.NET.Sdk.Worker": _IMPLICIT_BASE + _IMPLICIT_HOSTING,
    "Microsoft.NET.Sdk.Razor": _IMPLICIT_BASE,
    "Microsoft.NET.Sdk.BlazorWebAssembly": _IMPLICIT_BASE + (
        "System.Net.Http.Json", "Microsoft.AspNetCore.Components",
        "Microsoft.AspNetCore.Components.Forms", "Microsoft.AspNetCore.Components.Routing",
        "Microsoft.AspNetCore.Components.Web", "Microsoft.AspNetCore.Components.Web.Virtualization",
        "Microsoft.AspNetCore.Components.WebAssembly.Http", "Microsoft.JSInterop",
    ),
}

_FRAMEWORK_ROOTS = ("System", "Microsoft")

# Framework types that any file sees through `using System;` or the implicit
# usings, next to package namespaces: without this list `String` in a test
# file importing Moq and Xunit would be "Moq, Xunit or the framework".
_FRAMEWORK_TYPES = frozenset("""
Action Activator AggregateException AppContext AppDomain ArgumentException
ArgumentNullException ArgumentOutOfRangeException Array ArraySegment Assembly
Attribute BitConverter Boolean Buffer Byte CancellationToken
CancellationTokenSource Char Console Convert CultureInfo DateOnly DateTime
DateTimeOffset DayOfWeek Debug Decimal Delegate Dictionary Directory
DirectoryInfo Double Encoding Enum Enumerable Environment EventArgs Exception
File FileInfo FileStream FormatException Func GC Guid HashCode HashSet
HttpClient HttpContent HttpMethod HttpRequestException HttpRequestMessage
HttpResponseMessage HttpStatusCode ICollection IComparable IDictionary
IDisposable IEnumerable IEnumerator IList IQueryable IReadOnlyCollection
IReadOnlyDictionary IReadOnlyList ISet IndexOutOfRangeException Int16 Int32
Int64 Interlocked InvalidCastException InvalidOperationException
JsonDocument JsonElement JsonSerializer JsonSerializerOptions KeyValuePair
Lazy List Math MathF MemoryStream NotImplementedException
NotSupportedException Nullable NullReferenceException Object
ObjectDisposedException OperationCanceledException Parallel Path Process
Queryable Random Regex SemaphoreSlim Single Span Stack Stopwatch Stream
StreamReader StreamWriter String StringBuilder StringComparer
StringComparison StringContent StringReader StringWriter Task TaskCompletionSource
Thread ThreadPool TimeOnly TimeSpan TimeoutException Timer Tuple Type UInt16
UInt32 UInt64 Uri UriBuilder ValueTask ValueTuple Version WebUtility
Claim ClaimsIdentity ClaimsPrincipal ClaimTypes CryptographicOperations
HttpUtility IPAddress JsonArray JsonContent JsonNode JsonObject
MediaTypeHeaderValue AuthenticationHeaderValue FormUrlEncodedContent
MultipartFormDataContent RandomNumberGenerator SHA256 SHA1 MD5 Aes
""".split()) | frozenset("""
ActionResult AuthenticateResult AuthenticationHandler AuthenticationProperties
AuthenticationScheme AuthenticationSchemeOptions AuthenticationTicket BackgroundService
BadRequestObjectResult ContentResult Controller ControllerBase CookieOptions
FileResult HealthCheckResult HttpContext HttpRequest HttpResponse
IActionResult IApplicationBuilder IConfiguration IConfigurationSection
IEndpointRouteBuilder IFormFile IHealthCheck IHost IHostEnvironment
IHostedService IHttpClientFactory IHttpContextAccessor ILogger ILoggerFactory
IMemoryCache IOptions IOptionsMonitor IServiceCollection IServiceProvider
IServiceScopeFactory IWebHostEnvironment JsonResult NoContentResult
NotFoundObjectResult NotFoundResult ObjectResult OkObjectResult OkResult
ProblemDetails QueryHelpers RequestDelegate Results StatusCodeResult
TypedResults UnauthorizedResult WebApplication WebApplicationBuilder
WebEncoders ModelStateDictionary IUrlHelper
""".split())

# Main types of widely used packages. A type listed here belongs to that
# package whatever the call site imports; `Assert` exists in several test
# frameworks, so the declared one wins.
_KNOWN_PACKAGE_TYPES: Dict[str, Tuple[str, ...]] = {
    "Mock": ("Moq",), "Times": ("Moq",), "It": ("Moq",), "MockBehavior": ("Moq",),
    "Assert": ("xunit", "xunit.assert", "NUnit", "MSTest.TestFramework"),
    "Fact": ("xunit",), "Theory": ("xunit",), "InlineData": ("xunit",),
    "ITestOutputHelper": ("xunit",),
    "Fixture": ("AutoFixture",), "IFixture": ("AutoFixture",),
    "OmitOnRecursionBehavior": ("AutoFixture",), "ThrowingRecursionBehavior": ("AutoFixture",),
    "AssertionOptions": ("FluentAssertions",),
    "JsonConvert": ("Newtonsoft.Json",), "JObject": ("Newtonsoft.Json",),
    "JToken": ("Newtonsoft.Json",), "JArray": ("Newtonsoft.Json",),
    "JsonSerializerSettings": ("Newtonsoft.Json",),
    "SqlMapper": ("Dapper",), "DynamicParameters": ("Dapper",), "CommandDefinition": ("Dapper",),
    "NpgsqlConnection": ("Npgsql",), "NpgsqlCommand": ("Npgsql",),
    "NpgsqlDataSource": ("Npgsql",), "NpgsqlParameter": ("Npgsql",),
    "Log": ("Serilog",), "LoggerConfiguration": ("Serilog",),
    "IMapper": ("AutoMapper",), "MapperConfiguration": ("AutoMapper",),
    "IMediator": ("MediatR",), "Policy": ("Polly",),
    "GraphServiceClient": ("Microsoft.Graph",),
    "OpenApiInfo": ("Microsoft.OpenApi",), "OpenApiSecurityScheme": ("Microsoft.OpenApi",),
    "OpenApiReference": ("Microsoft.OpenApi",),
}


@dataclass
class DotnetProject:
    path: Path
    sdk: str = "Microsoft.NET.Sdk"
    packages: Dict[str, str] = field(default_factory=dict)  # id -> version ("" when unknown)
    project_refs: List[Path] = field(default_factory=list)
    usings: Set[str] = field(default_factory=set)  # implicit + <Using Include>

    @property
    def directory(self) -> Path:
        return self.path.parent


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_xml(path: Path) -> Optional[ET.Element]:
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None


def _properties(root: ET.Element) -> Dict[str, str]:
    props: Dict[str, str] = {}
    for group in root.iter():
        if _local(group.tag) != "PropertyGroup":
            continue
        for prop in group:
            if prop.text and prop.text.strip():
                props[_local(prop.tag)] = prop.text.strip()
    return props


def _expand(value: str, props: Dict[str, str]) -> str:
    return re.sub(r"\$\(([A-Za-z0-9_.]+)\)", lambda m: props.get(m.group(1), ""), value or "").strip()


def _package_items(root: ET.Element, props: Dict[str, str], central: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in root.iter():
        if _local(item.tag) not in ("PackageReference", "PackageVersion"):
            continue
        pid = (item.get("Include") or item.get("Update") or "").strip()
        if not pid:
            continue
        version = item.get("Version") or item.get("VersionOverride")
        if version is None:
            child = next((c for c in item if _local(c.tag) in ("Version", "VersionOverride")), None)
            version = child.text if child is not None else None
        if _local(item.tag) == "PackageVersion":
            central[pid] = _expand(version or "", props)
            continue
        out[pid] = _expand(version or "", props) or central.get(pid, "")
    return out


def _directory_files(start: Path, root: Path, name: str) -> List[Path]:
    """`name` files from *root* down to *start* (outermost first)."""
    found: List[Path] = []
    cur = start
    while True:
        candidate = cur / name
        if candidate.is_file():
            found.append(candidate)
        if cur == root or cur.parent == cur:
            break
        cur = cur.parent
    return list(reversed(found))


def load_projects(repo: Path) -> List[DotnetProject]:
    """Every ``.csproj`` under *repo* (bin/obj excluded) with its declarations."""
    repo = repo.resolve()
    projects: List[DotnetProject] = []
    for csproj in sorted(repo.rglob("*.csproj")):
        if {"bin", "obj"} & set(csproj.relative_to(repo).parts):
            continue
        root = _read_xml(csproj)
        if root is None:
            continue
        props: Dict[str, str] = {}
        central: Dict[str, str] = {}
        packages: Dict[str, str] = {}
        inherited = _directory_files(csproj.parent, repo, "Directory.Packages.props") + \
            _directory_files(csproj.parent, repo, "Directory.Build.props")
        for props_file in inherited:
            props_root = _read_xml(props_file)
            if props_root is not None:
                props.update(_properties(props_root))
                packages.update(_package_items(props_root, props, central))
        props.update(_properties(root))
        packages.update(_package_items(root, props, central))

        project = DotnetProject(path=csproj, packages=packages)
        sdk = (root.get("Sdk") or "").split("/", 1)[0].strip()
        if sdk:
            project.sdk = sdk
        if props.get("ImplicitUsings", "").lower() in ("enable", "true"):
            project.usings.update(IMPLICIT_USINGS.get(project.sdk, _IMPLICIT_BASE))
        for item in root.iter():
            tag = _local(item.tag)
            if tag == "ProjectReference" and item.get("Include"):
                ref = (csproj.parent / item.get("Include").replace("\\", "/")).resolve()
                project.project_refs.append(ref)
            elif tag == "Using":
                if item.get("Include") and not item.get("Static") and not item.get("Alias"):
                    project.usings.add(item.get("Include").strip())
                if item.get("Remove"):
                    project.usings.discard(item.get("Remove").strip())
        projects.append(project)
    return projects


class _ProjectIndex:
    """Owning project of a file and the packages that project can see."""

    def __init__(self, projects: List[DotnetProject]):
        self.by_dir = {p.directory: p for p in projects}
        self.by_path = {p.path.resolve(): p for p in projects}
        self._packages: Dict[Path, Dict[str, str]] = {}

    def project_of(self, file_path: str) -> Optional[DotnetProject]:
        cur = Path(file_path).resolve().parent
        while True:
            if cur in self.by_dir:
                return self.by_dir[cur]
            if cur.parent == cur:
                return None
            cur = cur.parent

    def packages(self, project: DotnetProject) -> Dict[str, str]:
        """Own packages plus those of referenced projects (transitive)."""
        key = project.path.resolve()
        if key in self._packages:
            return self._packages[key]
        self._packages[key] = {}
        merged: Dict[str, str] = {}
        for ref in project.project_refs:
            dep = self.by_path.get(ref)
            if dep is not None:
                merged.update(self.packages(dep))
        merged.update(project.packages)
        self._packages[key] = merged
        return merged


def _strong(namespace: str, packages: Iterable[str]) -> Optional[str]:
    """Longest package id covering *namespace* (`Newtonsoft.Json.Linq` -> `Newtonsoft.Json`)."""
    best = None
    ns = namespace.lower()
    for pid in packages:
        p = pid.lower()
        if (ns == p or ns.startswith(p + ".")) and (best is None or len(pid) > len(best)):
            best = pid
    return best


def _weak(namespace: str, packages: Iterable[str]) -> Set[str]:
    """Packages under *namespace*: `using Acme.Services;` with only `Acme.Services.Sql`
    declared, the namespace most likely comes in with that package."""
    ns = namespace.lower()
    return {pid for pid in packages if pid.lower().startswith(ns + ".")}


def _file_namespaces(file_data: Dict[str, Any], project: Optional[DotnetProject],
                     global_usings: Set[str]) -> Tuple[Set[str], Set[str]]:
    """(explicit namespaces, enclosing namespaces) visible in a file."""
    explicit = {
        imp["name"] for imp in file_data.get("imports", [])
        if imp.get("name") and not imp.get("alias") and not imp.get("is_static")
    }
    explicit |= global_usings
    if project is not None:
        explicit |= project.usings
    enclosing: Set[str] = set()
    ns = file_data.get("package_name") or ""
    while ns:
        enclosing.add(ns)
        ns = ns.rsplit(".", 1)[0] if "." in ns else ""
    return explicit, enclosing


def _candidates(namespaces: Iterable[str], packages: Dict[str, str], weak_ok: bool,
                repo_namespaces: Set[str]) -> Tuple[Set[str], Set[str]]:
    strong: Set[str] = set()
    weak: Set[str] = set()
    for ns in namespaces:
        pid = _strong(ns, packages)
        if pid:
            strong.add(pid)
            continue
        if ns.split(".", 1)[0] in _FRAMEWORK_ROOTS:
            strong.add(_FRAMEWORK)
            continue
        if weak_ok and ns not in repo_namespaces:
            weak |= _weak(ns, packages)
    return strong, weak


def attribute_external_types(
    all_file_data: List[Dict[str, Any]],
    external_rows: Iterable[Dict[str, Any]],
    projects: List[DotnetProject],
) -> List[Dict[str, Any]]:
    """One ``{name, origin, package, package_version, candidates, evidence}`` per external type.

    ``evidence`` says how sure the attribution is: ``known-type`` (a framework
    or well-known package type), ``qualified-name`` (written as
    ``Ns.Type`` at a call site) or ``usings`` (the namespaces imported at every
    call site agree on it: probable, not certain).
    """
    index = _ProjectIndex(projects)
    files = {Path(fd["path"]).resolve().as_posix(): fd for fd in all_file_data if fd.get("lang") == "c_sharp"}
    repo_namespaces = {fd.get("package_name") for fd in files.values() if fd.get("package_name")}
    global_usings: Dict[Optional[Path], Set[str]] = {}
    for fp, fd in files.items():
        project = index.project_of(fp)
        key = project.path if project else None
        for imp in fd.get("imports", []):
            if imp.get("is_global") and imp.get("name") and not imp.get("alias") and not imp.get("is_static"):
                global_usings.setdefault(key, set()).add(imp["name"])

    sites: Dict[str, Set[Tuple[str, Optional[str]]]] = {}
    for row in external_rows:
        name = row.get("external_type")
        fp = row.get("caller_file_path")
        if not name or not fp:
            continue
        full = row.get("full_call_name") or ""
        parts = full.split("<", 1)[0].split(".")
        qualifier = ".".join(parts[: parts.index(name)]) if name in parts[1:] else None
        sites.setdefault(name, set()).add((Path(fp).resolve().as_posix(), qualifier))

    versions: Dict[str, str] = {}
    for project in projects:
        for pid, version in index.packages(project).items():
            if version and not versions.get(pid):
                versions[pid] = version

    # A package declared anywhere in the repository usually reaches the other
    # projects transitively: a qualified name is matched against all of them.
    repo_packages = {pid: v for project in projects for pid, v in project.packages.items()}

    out: List[Dict[str, Any]] = []
    for name, type_sites in sorted(sites.items()):
        site_sets: List[Set[str]] = []
        qualified: Set[str] = set()
        visible: Set[str] = set()
        for fp, qualifier in type_sites:
            fd = files.get(fp, {})
            project = index.project_of(fp)
            packages = index.packages(project) if project else {}
            visible |= set(packages)
            if qualifier:
                strong, weak = _candidates([qualifier], repo_packages, True, repo_namespaces)
                if len(strong) == 1:
                    qualified |= strong
            else:
                explicit, enclosing = _file_namespaces(fd, project, global_usings.get(project.path if project else None, set()))
                strong, weak = _candidates(explicit, packages, True, repo_namespaces)
                enclosing_strong, _ = _candidates(enclosing - {ns for ns in enclosing if ns.split(".", 1)[0] in _FRAMEWORK_ROOTS},
                                                  packages, False, repo_namespaces)
                strong |= enclosing_strong
            # A weak match is about the namespace -> package mapping, not about
            # the type: every namespace visible at the call site is a candidate.
            if strong | weak:
                site_sets.append(strong | weak)

        chosen, evidence = _choose(name, site_sets, qualified, visible)
        row = _attribution(name, chosen, versions)
        row["evidence"] = evidence if row["origin"] in (ORIGIN_PACKAGE, ORIGIN_FRAMEWORK) else None
        out.append(row)
    return out


_WELL_KNOWN_PACKAGES = {pid.lower() for pids in _KNOWN_PACKAGE_TYPES.values() for pid in pids}


def _choose(name: str, site_sets: List[Set[str]], qualified: Set[str],
            visible: Set[str]) -> Tuple[Set[str], str]:
    known = _KNOWN_PACKAGE_TYPES.get(name)
    if known:
        declared = [pid for pid in known if pid.lower() in {v.lower() for v in visible}]
        return {declared[0] if declared else known[0]}, "known-type"
    if name in _FRAMEWORK_TYPES:
        return {_FRAMEWORK}, "known-type"
    if len(qualified) == 1:
        return set(qualified), "qualified-name"
    chosen = _agree(site_sets)
    if len(chosen) > 1:
        # The implicit usings (`System`, ...) make the framework a candidate
        # everywhere; its common types are listed above, so an unlisted type
        # that a declared package can also provide is taken for a package type.
        chosen.discard(_FRAMEWORK)
    # The well-known packages whose main types are listed provide those, not
    # an unlisted type that happens to be used in a file importing them:
    # `using Newtonsoft.Json;` is there for JsonConvert, `using
    # Microsoft.Extensions.Logging;` for ILogger, even as the only candidate.
    chosen = {c for c in chosen if not _listed_types_only(c)}
    return chosen, "usings"


def _listed_types_only(package: str) -> bool:
    p = package.lower()
    return p in _WELL_KNOWN_PACKAGES or p.startswith("microsoft.extensions.")


def _agree(sets: List[Set[str]]) -> Set[str]:
    """Candidates every call site agrees on."""
    if not sets:
        return set()
    common = set.intersection(*sets)
    if not common:
        # Call sites disagree (types of the same simple name in different
        # packages): keep the most frequent candidates.
        counts = Counter(c for s in sets for c in s)
        top = max(counts.values())
        common = {c for c, n in counts.items() if n == top}
    return common


def _attribution(name: str, chosen: Set[str], versions: Dict[str, str]) -> Dict[str, Any]:
    versions = {k.lower(): v for k, v in versions.items()}
    row: Dict[str, Any] = {"name": name, "package": None, "package_version": None, "candidates": []}
    if not chosen:
        row["origin"] = ORIGIN_UNKNOWN
    elif chosen == {_FRAMEWORK}:
        row["origin"] = ORIGIN_FRAMEWORK
    elif len(chosen) == 1:
        pid = next(iter(chosen))
        row.update(origin=ORIGIN_PACKAGE, package=pid, package_version=versions.get(pid.lower()) or None)
    else:
        row.update(origin=ORIGIN_AMBIGUOUS, candidates=sorted(chosen))
    return row


def external_type_origins(
    all_file_data: List[Dict[str, Any]],
    call_groups: Tuple[List[Dict[str, Any]], ...],
) -> List[Dict[str, Any]]:
    """Attribution of the ExternalClass targets of *call_groups* (full index only).

    Needs every file of the repository: computed on a subset (incremental
    update), it would replace a well-founded attribution by a weaker one.
    """
    external_rows = [row for group in call_groups[10:12] for row in group]
    if not external_rows:
        return []
    roots = {Path(fd["repo_path"]) for fd in all_file_data if fd.get("lang") == "c_sharp" and fd.get("repo_path")}
    projects = [project for root in sorted(roots) for project in load_projects(root)]
    return attribute_external_types(all_file_data, external_rows, projects)
