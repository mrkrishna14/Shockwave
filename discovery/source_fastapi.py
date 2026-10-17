"""Static extraction of literal FastAPI routes from provider source (never executed).

Provider source is untrusted data: modules are parsed with ``ast.parse`` and never
imported, so nothing in the scanned repository runs. The scanner emits only routes
whose full path it can prove from string literals. Anything else becomes an
``unsupported_patterns`` entry (an explicit coverage gap) instead of a guessed route,
because a wrong path would later be reconciled into a spurious implementation-only or
documented-not-found endpoint.

Resolution runs in two phases. Phase 1 walks each module in statement order and
records symbolic facts: what each name refers to (imports, constructor calls,
aliases), route registrations, ``include_router`` edges and unsupported operations.
Phase 2 resolves those symbols across modules inside the source root only (absolute
imports are source-root-relative module paths, or, in the src layout, paths relative to
a ``src`` directory that is not a package; relative imports follow the importing file's
package), creates one instance per ``FastAPI(...)`` / ``APIRouter(...)`` call site and
walks the include graph from every application, composing prefixes outermost first.
A route registration whose receiver is not such an instance (another library's app or
router, a module, or a name defined outside the source root) is reported as
``unresolved-router`` when it looks like a route. The include walk is iterative where
it can be deep and charged to a work budget; running out of budget is reported as a
coverage gap, not a crash.

Two FastAPI behaviours are modelled because they change which paths are really
served: ``include_router`` copies the routes that exist when it runs (a route added
later in the same scope is not served through that include), and an application
attached with ``app.mount`` is served below the mount path, so its routes are not
reported as top-level routes. Every emitted route has confidence ``direct``: it is a
literal registration observed in the source, never an inference.
"""

from __future__ import annotations

import ast
import itertools
import re
import warnings
from collections import Counter, deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from discovery._source_files import (
    EXCERPT_WINDOW,
    SourceFile,
    bounded_notes,
    excerpt_text,
    format_location,
    read_source_files,
    suffix_filter,
    unique_routes,
    unresolved_summary,
)
from discovery.records import (
    HTTP_METHODS_LOWER,
    SourceRoute,
    SourceScan,
    normalize_path,
)

__all__ = ["scan_fastapi_routes"]

_APP_CONSTRUCTORS = frozenset({"fastapi.FastAPI", "fastapi.applications.FastAPI"})
_ROUTER_CONSTRUCTORS = frozenset({"fastapi.APIRouter", "fastapi.routing.APIRouter"})
# Library namespaces never resolved inside the scanned tree, even when vendored there.
_EXTERNAL_ROOTS = frozenset({"fastapi", "starlette"})
_ROUTE_DECORATORS = frozenset(HTTP_METHODS_LOWER)
_HTTP_METHODS = frozenset(method.upper() for method in HTTP_METHODS_LOWER)
_WEBSOCKET_DECORATORS = frozenset({"websocket", "websocket_route"})
_WEBSOCKET_CALLS = frozenset({"add_api_websocket_route", "add_websocket_route"})
_MOUNT_CALLS = frozenset({"mount", "host"})
_MAX_INCLUDE_DEPTH = 8
_MAX_RESOLVE_DEPTH = 32
# Include-graph work per scan: one unit per router entered plus one per registration and
# include it holds. Repeated or diamond-shaped includes re-enter routers, so without this
# a small file could multiply into hours of traversal.
_MAX_GRAPH_WORK = 200_000
# Starlette path parameters: "{name}" or "{name:converter}".
_PATH_PARAMETER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[A-Za-z_][A-Za-z0-9_]*)?\}")
# The only line ends the Python tokenizer counts (form feed, U+2028 and the like are not).
_LINE_END = re.compile(rb"\r\n|\r|\n")
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
# Reported when the include walk itself fails (it is bounded, so this is defensive only).
_RESOLUTION_FAILED = "fastapi:route-resolution-failed"

_UnsupportedKind = Literal[
    "dynamic-prefix",
    "dynamic-path",
    "dynamic-methods",
    "unsupported-method",
    "router-not-mounted",
    "route-after-include",
    "unresolved-router",
    "unresolved-router-include",
    "include-cycle",
    "include-depth-limit",
    "include-graph-limit",
    "late-include",
    "mount",
    "websocket",
    "starlette-route",
]


def scan_fastapi_routes(
    source_root: Path, *, max_files: int = 5000, max_file_bytes: int = 1_000_000
) -> SourceScan:
    """Literal FastAPI routes under ``source_root``; the scanned code is parsed, never run."""
    files = read_source_files(
        source_root,
        is_relevant=suffix_filter((".py",)),
        max_files=max_files,
        max_file_bytes=max_file_bytes,
    )
    notes = list(files.file_notes)
    facts: dict[str, _FileFacts] = {}
    for source in files.files:
        tree = _parse(source, notes)
        if tree is None:
            continue
        try:
            facts[source.path] = _ModuleWalker(source, _package_of(source.path), tree).run()
        except RecursionError:
            notes.append(f"{source.path}: skipped (too deeply nested to analyze)")

    resolver = _Resolver(facts, _module_index(source.path for source in files.files))
    failure: list[str] = []
    try:
        routes = resolver.resolve_routes()
    except RecursionError:  # source content must never make discovery raise
        routes = []
        failure.append(
            "fastapi: route resolution exceeded the recursion limit; no FastAPI route was"
            " emitted (scan truncated)"
        )
    return SourceScan(
        routes=routes,
        files_scanned=len(facts),
        files_skipped=files.files_skipped + len(files.files) - len(facts),
        unsupported_patterns=[
            *resolver.unsupported_patterns(),
            *([_RESOLUTION_FAILED] if failure else []),
        ],
        warnings=[*files.warnings, *bounded_notes(notes), *resolver.summary_warnings(), *failure],
    )


# Phase 1 values: symbolic references recorded while walking one module.


class _Unknown:
    """A value the scanner cannot resolve statically (parameters, call results, ...)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNKNOWN"


_UNKNOWN = _Unknown()


@dataclass(frozen=True)
class _ImportRef:
    module: str  # absolute dotted module bound by ``import a.b`` / ``import a.b as m``


@dataclass(frozen=True)
class _SymbolRef:
    module: str  # ``from module import name``: an attribute or a submodule
    name: str


@dataclass(frozen=True)
class _AttrRef:
    base: _Value
    name: str


@dataclass(frozen=True)
class _CallRef:
    file: str
    key: tuple[int, int]  # (line, column) of a call whose result is bound or included


_Value = _Unknown | _ImportRef | _SymbolRef | _AttrRef | _CallRef


@dataclass(frozen=True)
class _Position:
    file: str
    scope: int
    line: int
    column: int

    def runs_before(self, other: _Position) -> bool:
        """Statement order is only known within one scope; elsewhere assume "before"."""
        if self.file != other.file or self.scope != other.scope:
            return True
        return (self.line, self.column) < (other.line, other.column)


@dataclass(frozen=True)
class _CallSite:
    callee: _Value
    line: int
    prefix: str | None  # literal ``prefix=``; "" when absent; None when not a literal
    include_in_schema: bool | None  # True when absent (FastAPI default); None when not literal
    starlette_routes: bool  # ``routes=[...]`` passed to the constructor


@dataclass(eq=False)
class _Registration:
    receiver: _Value
    methods: tuple[str, ...] | None  # None: ``methods=`` is not a literal list
    path: str | None  # None: the path is not a literal
    position: _Position
    line_end: int
    handler: str | None
    excerpt: str
    include_in_schema: bool | None


@dataclass(frozen=True)
class _IncludeFact:
    receiver: _Value
    router: _Value
    prefix: str | None
    include_in_schema: bool | None
    position: _Position


@dataclass(frozen=True)
class _OperationFact:
    receiver: _Value
    kind: Literal["mount", "websocket", "starlette-route"]
    target: _Value  # what ``mount`` attaches; _UNKNOWN otherwise
    position: _Position


@dataclass
class _FileFacts:
    file: str
    imports_fastapi: bool = False
    env: dict[str, _Value] = field(default_factory=dict)
    call_sites: dict[tuple[int, int], _CallSite] = field(default_factory=dict)
    registrations: list[_Registration] = field(default_factory=list)
    includes: list[_IncludeFact] = field(default_factory=list)
    operations: list[_OperationFact] = field(default_factory=list)


@dataclass(eq=False)
class _Scope:
    kind: Literal["module", "class", "function"]
    ident: int
    parent: _Scope | None = None
    env: dict[str, _Value] = field(default_factory=dict)

    def _visible(self) -> Iterator[_Scope]:
        """Scopes searched for a name; class bodies are invisible to nested scopes."""
        scope: _Scope | None = self
        while scope is not None:
            if scope is self or scope.kind != "class":
                yield scope
            scope = scope.parent

    def lookup(self, name: str) -> _Value:
        for scope in self._visible():
            if name in scope.env:
                return scope.env[name]
        return _UNKNOWN

    def shadows_module_name(self, name: str) -> bool:
        return any(scope.kind != "module" and name in scope.env for scope in self._visible())


# Phase 1: one module, statements visited in execution order.


def _parse(source: SourceFile, notes: list[str]) -> ast.Module | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # e.g. invalid escape sequences in scanned code
        try:
            return ast.parse(source.text, filename=source.path)
        except SyntaxError as exc:
            notes.append(f"{source.path}: syntax error at line {exc.lineno or 1}")
        except (ValueError, RecursionError, MemoryError):
            notes.append(f"{source.path}: skipped (could not be parsed)")
    return None


class _ModuleWalker:
    """Records symbolic facts for one module without importing or executing it."""

    def __init__(self, source: SourceFile, package: str | None, tree: ast.Module) -> None:
        self._source = source
        self._file = source.path
        self._package = package
        self._tree = tree
        self._constants = _module_string_constants(tree)
        self._scope_ids = itertools.count()
        self._deferred: deque[tuple[ast.FunctionDef | ast.AsyncFunctionDef, _Scope]] = deque()
        self._data = b""  # the UTF-8 source that AST column offsets count; built on first use
        self._line_starts: list[int] = []  # byte offset where each line starts
        self.facts = _FileFacts(file=source.path)

    def run(self) -> _FileFacts:
        module_scope = _Scope("module", next(self._scope_ids))
        self._block(self._tree.body, module_scope)
        self.facts.env = module_scope.env
        # Function bodies run after the module has loaded, so they see final module names.
        while self._deferred:
            function, parent = self._deferred.popleft()
            scope = _Scope("function", next(self._scope_ids), parent)
            for name in _parameter_names(function.args):
                scope.env[name] = _UNKNOWN
            self._block(function.body, scope)
        return self.facts

    def _block(self, statements: list[ast.stmt], scope: _Scope) -> None:
        for statement in statements:
            self._statement(statement, scope)

    def _statement(self, statement: ast.stmt, scope: _Scope) -> None:
        match statement:
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                for decorator in statement.decorator_list:
                    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                        self._decorator_call(
                            decorator, decorator.func, statement.name, decorator, "@", scope
                        )
                scope.env[statement.name] = _UNKNOWN
                self._deferred.append((statement, scope))
            case ast.ClassDef():
                self._block(statement.body, _Scope("class", next(self._scope_ids), scope))
                scope.env[statement.name] = _UNKNOWN
            case ast.Assign():
                self._scan(statement.value, scope)
                value = self._bound_value(statement.value, scope)
                for target in statement.targets:
                    self._bind(target, value, scope)
            case ast.AnnAssign(value=ast.expr() as value):
                self._scan(value, scope)
                self._bind(statement.target, self._bound_value(value, scope), scope)
            case ast.AugAssign():
                self._scan(statement.value, scope)
                self._bind(statement.target, _UNKNOWN, scope)
            case ast.Import():
                self._import(statement, scope)
            case ast.ImportFrom():
                self._import_from(statement, scope)
            case ast.If() | ast.While():
                self._scan(statement.test, scope)
                self._block(statement.body, scope)
                self._block(statement.orelse, scope)
            case ast.For() | ast.AsyncFor():
                self._scan(statement.iter, scope)
                self._bind(statement.target, _UNKNOWN, scope)
                self._block(statement.body, scope)
                self._block(statement.orelse, scope)
            case ast.With() | ast.AsyncWith():
                for item in statement.items:
                    self._scan(item.context_expr, scope)
                    if item.optional_vars is not None:
                        self._bind(item.optional_vars, _UNKNOWN, scope)
                self._block(statement.body, scope)
            case ast.Try() | ast.TryStar():
                self._block(statement.body, scope)
                for handler in statement.handlers:
                    if handler.name:
                        scope.env[handler.name] = _UNKNOWN
                    self._block(handler.body, scope)
                self._block(statement.orelse, scope)
                self._block(statement.finalbody, scope)
            case ast.Match():
                self._scan(statement.subject, scope)
                for case in statement.cases:
                    for name in _pattern_names(case.pattern):
                        scope.env[name] = _UNKNOWN
                    self._block(case.body, scope)
            case ast.Delete():
                for target in statement.targets:
                    self._bind(target, _UNKNOWN, scope)
            case ast.TypeAlias(name=ast.Name(id=name)):
                scope.env[name] = _UNKNOWN
            case _:
                self._scan(statement, scope)

    def _scan(self, node: ast.AST, scope: _Scope) -> None:
        """Handle the calls inside one expression or simple statement (not nested scopes)."""
        calls: list[ast.Call] = []
        walrus_targets: list[str] = []
        for current in _expression_nodes(node):
            if isinstance(current, ast.Call):
                calls.append(current)
            elif isinstance(current, ast.NamedExpr):
                walrus_targets.append(current.target.id)
        for call in sorted(calls, key=lambda call: (call.lineno, call.col_offset)):
            self._call(call, scope)
        for name in walrus_targets:
            scope.env[name] = _UNKNOWN

    def _call(self, call: ast.Call, scope: _Scope) -> None:
        func = call.func
        if isinstance(func, ast.Call) and isinstance(func.func, ast.Attribute):
            # ``router.get("/p")(handler)``: a route decorator applied by hand.
            handler = _dotted_name(call.args[0]) if call.args else None
            self._decorator_call(func, func.func, handler, call, "", scope)
            return
        if not isinstance(func, ast.Attribute):
            return
        method = func.attr
        if method == "include_router":
            self._include(call, func, scope)
        elif method == "add_api_route":
            self._registration(call, func, _endpoint_name(call), call, "", scope)
        elif method in _WEBSOCKET_CALLS:
            self._operation(func, "websocket", call, scope)
        elif method in _MOUNT_CALLS:
            self._operation(func, "mount", call, scope, target=_argument(call, 1, "app"))
        elif method == "add_route":
            self._operation(func, "starlette-route", call, scope)

    def _decorator_call(
        self,
        call: ast.Call,
        func: ast.Attribute,
        handler: str | None,
        evidence: ast.expr,
        excerpt_prefix: str,
        scope: _Scope,
    ) -> None:
        method = func.attr
        if method in _ROUTE_DECORATORS or method == "api_route":
            self._registration(call, func, handler, evidence, excerpt_prefix, scope)
        elif method in _WEBSOCKET_DECORATORS:
            self._operation(func, "websocket", evidence, scope)
        elif method == "route":
            self._operation(func, "starlette-route", evidence, scope)

    def _registration(
        self,
        call: ast.Call,
        func: ast.Attribute,
        handler: str | None,
        evidence: ast.expr,
        excerpt_prefix: str,
        scope: _Scope,
    ) -> None:
        if func.attr in ("api_route", "add_api_route"):
            methods = self._methods(call, scope)
        else:
            methods = (func.attr.upper(),)
        self.facts.registrations.append(
            _Registration(
                receiver=self._symbol(func.value, scope),
                methods=methods,
                path=self._route_path(call, scope),
                position=self._position(evidence, scope),
                line_end=evidence.end_lineno or evidence.lineno,
                handler=handler,
                excerpt=self._excerpt(evidence, excerpt_prefix),
                include_in_schema=_keyword_bool(call, "include_in_schema"),
            )
        )

    def _include(self, call: ast.Call, func: ast.Attribute, scope: _Scope) -> None:
        router = _argument(call, 0, "router")
        self.facts.includes.append(
            _IncludeFact(
                receiver=self._symbol(func.value, scope),
                router=_UNKNOWN if router is None else self._bound_value(router, scope),
                prefix=self._prefix(call, scope),
                include_in_schema=_keyword_bool(call, "include_in_schema"),
                position=self._position(call, scope),
            )
        )

    def _operation(
        self,
        func: ast.Attribute,
        kind: Literal["mount", "websocket", "starlette-route"],
        evidence: ast.expr,
        scope: _Scope,
        target: ast.expr | None = None,
    ) -> None:
        self.facts.operations.append(
            _OperationFact(
                receiver=self._symbol(func.value, scope),
                kind=kind,
                target=_UNKNOWN if target is None else self._symbol(target, scope),
                position=self._position(evidence, scope),
            )
        )

    def _bound_value(self, expression: ast.expr, scope: _Scope) -> _Value:
        if isinstance(expression, ast.Call):
            return self._call_site(expression, scope)
        return self._symbol(expression, scope)

    def _call_site(self, call: ast.Call, scope: _Scope) -> _Value:
        callee = self._symbol(call.func, scope)
        if callee is _UNKNOWN:
            return _UNKNOWN
        key = (call.lineno, call.col_offset)
        self.facts.call_sites[key] = _CallSite(
            callee=callee,
            line=call.lineno,
            prefix=self._prefix(call, scope),
            include_in_schema=_keyword_bool(call, "include_in_schema"),
            starlette_routes=_keyword(call, "routes") is not None,
        )
        return _CallRef(self._file, key)

    def _symbol(self, expression: ast.expr, scope: _Scope) -> _Value:
        if isinstance(expression, ast.Name):
            return scope.lookup(expression.id)
        if isinstance(expression, ast.Attribute):
            base = self._symbol(expression.value, scope)
            return _UNKNOWN if base is _UNKNOWN else _AttrRef(base, expression.attr)
        return _UNKNOWN

    def _bind(self, target: ast.expr, value: _Value, scope: _Scope) -> None:
        if isinstance(target, ast.Name):
            scope.env[target.id] = value
            return
        for name in _target_names(target):
            scope.env[name] = _UNKNOWN

    def _literal(self, expression: ast.expr, scope: _Scope) -> str | None:
        """A str constant, a placeholder-free f-string, a concatenation of literals, or a
        module-level name assigned a str constant exactly once. Anything else is dynamic."""
        if isinstance(expression, ast.Name):
            if scope.shadows_module_name(expression.id):
                return None
            return self._constants.get(expression.id)
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
            left = self._literal(expression.left, scope)
            right = self._literal(expression.right, scope)
            return None if left is None or right is None else left + right
        return _constant_string(expression)

    def _route_path(self, call: ast.Call, scope: _Scope) -> str | None:
        path = _argument(call, 0, "path")
        return None if path is None else self._literal(path, scope)

    def _prefix(self, call: ast.Call, scope: _Scope) -> str | None:
        prefix = _keyword(call, "prefix")
        if prefix is None:
            return None if _has_double_star(call) else ""
        return self._literal(prefix, scope)

    def _methods(self, call: ast.Call, scope: _Scope) -> tuple[str, ...] | None:
        value = _keyword(call, "methods")
        if value is None:
            return None if _has_double_star(call) else ("GET",)
        if isinstance(value, ast.Constant) and value.value is None:
            return ("GET",)  # FastAPI's default when methods is None
        if not isinstance(value, ast.List | ast.Tuple | ast.Set):
            return None
        methods: set[str] = set()
        for element in value.elts:
            literal = self._literal(element, scope)
            if literal is None:
                return None
            methods.add(literal.upper())
        return tuple(sorted(methods))

    def _import(self, statement: ast.Import, scope: _Scope) -> None:
        for alias in statement.names:
            top = alias.name.split(".")[0]
            if top == "fastapi":
                self.facts.imports_fastapi = True
            if alias.asname:
                scope.env[alias.asname] = _ImportRef(alias.name)
            else:
                scope.env[top] = _ImportRef(top)

    def _import_from(self, statement: ast.ImportFrom, scope: _Scope) -> None:
        module = self._absolute_module(statement.module, statement.level)
        if module is not None and module.split(".")[0] == "fastapi":
            self.facts.imports_fastapi = True
        for alias in statement.names:
            if alias.name == "*":
                continue  # star imports bind unknown names; nothing is guessed from them
            local = alias.asname or alias.name
            scope.env[local] = _UNKNOWN if module is None else _SymbolRef(module, alias.name)

    def _absolute_module(self, module: str | None, level: int) -> str | None:
        if level == 0:
            return module
        if not self._package:
            return None  # relative import outside a package: Python would reject it too
        parts = self._package.split(".")
        keep = len(parts) - (level - 1)
        if keep <= 0:
            return None
        base = parts[:keep]
        return ".".join([*base, module] if module else base)

    def _position(self, node: ast.expr, scope: _Scope) -> _Position:
        return _Position(self._file, scope.ident, node.lineno, node.col_offset)

    def _excerpt(self, node: ast.expr, prefix: str) -> str:
        """``prefix`` + the node's source text, as ``ast.get_source_segment`` would cut it, but
        from a line table built once per file (that function re-splits the file per call)."""
        if not self._line_starts:
            self._data = self._source.text.encode()
            self._line_starts = [0, *(match.end() for match in _LINE_END.finditer(self._data))]
        starts = self._line_starts
        first, last = node.lineno - 1, (node.end_lineno or node.lineno) - 1
        if not 0 <= first < len(starts):
            return excerpt_text(prefix)
        if node.end_col_offset is not None and last < len(starts):
            start, end = starts[first] + node.col_offset, starts[last] + node.end_col_offset
        else:  # no end position: the whole first line
            start, end = starts[first], starts[first + 1] if first + 1 < len(starts) else len(self._data)
        stop = min(end, start + EXCERPT_WINDOW)
        segment = self._data[start:stop].decode("utf-8", "ignore")  # a cut may split a character
        return excerpt_text(prefix + segment, truncated=stop < end)


# Phase 2: cross-module resolution and include-graph traversal.


@dataclass(frozen=True)
class _Module:
    name: str  # a module or package inside the source root


@dataclass(frozen=True)
class _External:
    qualname: str  # something defined outside the source root, e.g. "fastapi.APIRouter"


@dataclass(eq=False)
class _Instance:
    kind: Literal["app", "router"]
    file: str
    line: int
    order: tuple[str, int, int]
    prefix: str | None  # routers only; apps have no prefix
    include_in_schema: bool | None
    registrations: list[_Registration] = field(default_factory=list)
    includes: list[_Edge] = field(default_factory=list)
    mounted: bool = False  # attached with ``mount``: served below an unsupported mount path


@dataclass(frozen=True, eq=False)
class _Edge:
    child: _Instance
    prefix: str | None
    include_in_schema: bool | None
    position: _Position


@dataclass(frozen=True)
class _Emission:
    """A literal registration reached from an application along one include chain."""

    registration: _Registration
    raw_path: str
    methods: tuple[str, ...]
    include_in_schema: bool | None


_Resolved = _Module | _External | _Instance | _Unknown


class _Resolver:
    def __init__(self, facts: dict[str, _FileFacts], modules: dict[str, str]) -> None:
        self._facts = facts
        self._modules = modules  # dotted module name -> file path
        self._packages = {
            ".".join(parts[:end])
            for parts in (name.split(".") for name in modules)
            for end in range(1, len(parts))
        }
        self._instances: dict[tuple[str, tuple[int, int]], _Instance] = {}
        self._active: set[tuple[str, str]] = set()
        self._unsupported: set[tuple[str, int, str]] = set()
        self._outcomes: dict[int, set[str]] = {}  # id(registration) -> traversal outcomes
        self._emissions: list[_Emission] = []
        self._unresolved: Counter[str] = Counter()
        self._work = 0
        self._graph_truncated = False
        # Includes whose subtree is not served through them: (kind, include, hidden registrations).
        self._hidden: list[tuple[_UnsupportedKind, _Position, list[_Registration]]] = []

    def resolve_routes(self) -> list[SourceRoute]:
        for file in sorted(self._facts):
            for key in sorted(self._facts[file].call_sites):
                self.resolve(_CallRef(file, key))
        for file in sorted(self._facts):
            facts = self._facts[file]
            for registration in facts.registrations:
                self._attach(registration, facts)
            for include in facts.includes:
                self._connect(include)
            for operation in facts.operations:
                self._apply_operation(operation)
        roots = [
            instance
            for instance in self._instances.values()
            if instance.kind == "app" and not instance.mounted
        ]
        for root in sorted(roots, key=lambda instance: instance.order):
            self._visit(root, (), dynamic=False, flags=(), chain=(root,), edge=None)
        self._classify_unresolved()
        return self._build_routes()

    def unsupported_patterns(self) -> list[str]:
        return [
            format_location(f"fastapi:{kind}", file, line)
            for file, line, kind in sorted(self._unsupported)
        ]

    def summary_warnings(self) -> list[str]:
        messages = unresolved_summary("fastapi", self._unresolved)
        if self._graph_truncated:
            messages.append("fastapi: include graph too large; scan truncated during route resolution")
        return messages

    def resolve(self, value: _Value | _Resolved, depth: int = 0) -> _Resolved:
        if depth > _MAX_RESOLVE_DEPTH:
            return _UNKNOWN
        match value:
            case _ImportRef(module=module):
                return _Module(module) if self._internal(module) else _External(module)
            case _SymbolRef(module=module, name=name):
                return self._symbol(module, name, depth)
            case _AttrRef(base=base, name=name):
                return self._attribute(self.resolve(base, depth + 1), name, depth)
            case _CallRef():
                return self._call_result(value, depth)
            case _Module() | _External() | _Instance():
                return value
        return _UNKNOWN

    def _internal(self, module: str) -> bool:
        if module.split(".")[0] in _EXTERNAL_ROOTS:
            return False
        return module in self._modules or module in self._packages

    def _symbol(self, module: str, name: str, depth: int) -> _Resolved:
        if not self._internal(module):
            return _External(f"{module}.{name}")
        key = (module, name)
        if key in self._active:
            return _UNKNOWN  # circular import chain
        self._active.add(key)
        try:
            file = self._modules.get(module)
            facts = self._facts.get(file) if file is not None else None
            if facts is not None and name in facts.env:
                return self.resolve(facts.env[name], depth + 1)
            submodule = f"{module}.{name}"
            return _Module(submodule) if self._internal(submodule) else _UNKNOWN
        finally:
            self._active.discard(key)

    def _attribute(self, base: _Resolved, name: str, depth: int) -> _Resolved:
        if isinstance(base, _Module):
            return self._symbol(base.name, name, depth + 1)
        if isinstance(base, _External):
            return _External(f"{base.qualname}.{name}")
        if isinstance(base, _Instance) and base.kind == "app" and name == "router":
            return base  # ``app.router`` is the application's own router
        return _UNKNOWN

    def _call_result(self, reference: _CallRef, depth: int) -> _Resolved:
        key = (reference.file, reference.key)
        existing = self._instances.get(key)
        if existing is not None:
            return existing
        site = self._facts[reference.file].call_sites[reference.key]
        callee = self.resolve(site.callee, depth + 1)
        if not isinstance(callee, _External):
            return _UNKNOWN
        if callee.qualname in _APP_CONSTRUCTORS:
            kind: Literal["app", "router"] = "app"
        elif callee.qualname in _ROUTER_CONSTRUCTORS:
            kind = "router"
        else:
            return _External(f"{callee.qualname}()")  # an object from some other library
        instance = _Instance(
            kind=kind,
            file=reference.file,
            line=site.line,
            order=(reference.file, *reference.key),
            prefix=site.prefix if kind == "router" else "",
            include_in_schema=site.include_in_schema if kind == "router" else True,
        )
        self._instances[key] = instance
        if instance.prefix is None:
            self._report("dynamic-prefix", reference.file, site.line)
        if site.starlette_routes:
            self._report("starlette-route", reference.file, site.line)
        return instance

    def _attach(self, registration: _Registration, facts: _FileFacts) -> None:
        target = self.resolve(registration.receiver)
        if isinstance(target, _Instance):
            target.registrations.append(registration)
            self._outcomes[id(registration)] = set()
            return
        # Not an app or router of this tree: unknown, defined outside the source root (an
        # import the tree does not hold, another library's app or router) or a module. A
        # route path ("" or "/...", the only ones FastAPI accepts) makes it a coverage gap;
        # resolved non-routers need one, so ``@mock.patch("pkg.fn")`` stays quiet.
        path = registration.path
        route_path = path is not None and (path == "" or path.startswith("/"))
        if route_path or (target is _UNKNOWN and facts.imports_fastapi):
            self._report("unresolved-router", facts.file, registration.position.line)
            self._unresolved["unresolved router"] += 1

    def _connect(self, include: _IncludeFact) -> None:
        parent = self.resolve(include.receiver)
        if not isinstance(parent, _Instance):
            return
        position = include.position
        child = self.resolve(include.router)
        if child is parent:
            self._report("include-cycle", position.file, position.line)
        elif not isinstance(child, _Instance) or child.kind != "router":
            self._report("unresolved-router-include", position.file, position.line)
        else:
            if include.prefix is None:
                self._report("dynamic-prefix", position.file, position.line)
            parent.includes.append(_Edge(child, include.prefix, include.include_in_schema, position))

    def _apply_operation(self, operation: _OperationFact) -> None:
        if not isinstance(self.resolve(operation.receiver), _Instance):
            return
        self._report(operation.kind, operation.position.file, operation.position.line)
        if operation.kind == "mount":
            mounted = self.resolve(operation.target)
            if isinstance(mounted, _Instance):
                mounted.mounted = True

    def _visit(
        self,
        instance: _Instance,
        prefixes: tuple[str, ...],
        *,
        dynamic: bool,
        flags: tuple[bool | None, ...],
        chain: tuple[_Instance, ...],
        edge: _Position | None,
    ) -> None:
        if not self._charge(instance, edge):
            return
        for registration in instance.registrations:
            outcomes = self._outcomes[id(registration)]
            if edge is not None and not registration.position.runs_before(edge):
                outcomes.add("late")
            elif dynamic:
                outcomes.add("dynamic")
            else:
                outcomes.add("emitted")
                if registration.path is not None and registration.methods is not None:
                    self._emissions.append(
                        _Emission(
                            registration=registration,
                            raw_path="".join(prefixes) + registration.path,
                            methods=registration.methods,
                            include_in_schema=_effective_flag(
                                (*flags, registration.include_in_schema)
                            ),
                        )
                    )
        for include in instance.includes:
            child = include.child
            if edge is not None and not include.position.runs_before(edge):
                # Ran after the parent's routes were copied: nothing below is served this way.
                hidden = self._hide_subtree(include, "late-include")
                self._hidden.append(("late-include", include.position, hidden))
                continue
            if child in chain:
                self._report("include-cycle", include.position.file, include.position.line)
                continue
            if len(chain) > _MAX_INCLUDE_DEPTH:
                hidden = self._hide_subtree(include, "depth-limit")
                self._hidden.append(("include-depth-limit", include.position, hidden))
                continue
            self._visit(
                child,
                (*prefixes, include.prefix or "", child.prefix or ""),
                dynamic=dynamic or include.prefix is None or child.prefix is None,
                flags=(*flags, include.include_in_schema, child.include_in_schema),
                chain=(*chain, child),
                edge=include.position,
            )

    def _hide_subtree(self, include: _Edge, outcome: str) -> list[_Registration]:
        """Mark the registrations below a non-traversed include; returns them.

        Iterative (an include chain can be thousands of routers long) and cycle-safe.
        """
        hidden: list[_Registration] = []
        seen: set[int] = set()
        pending = [(include.child, include.position)]
        while pending:
            instance, position = pending.pop()
            if id(instance) in seen:
                continue
            seen.add(id(instance))
            if not self._charge(instance, position):
                break
            hidden.extend(instance.registrations)
            for registration in instance.registrations:
                self._outcomes[id(registration)].add(outcome)
            pending.extend((edge.child, edge.position) for edge in instance.includes)
        return hidden

    def _charge(self, instance: _Instance, edge: _Position | None) -> bool:
        """Spend include-walk budget on entering ``instance``; False once it is exhausted.

        The first refusal is reported where the walk stopped (the include, or the
        application itself), so the truncation shows in coverage, not only in a warning.
        """
        self._work += 1 + len(instance.registrations) + len(instance.includes)
        if self._work <= _MAX_GRAPH_WORK:
            return True
        if not self._graph_truncated:
            self._graph_truncated = True
            file, line = (edge.file, edge.line) if edge is not None else (instance.file, instance.line)
            self._report("include-graph-limit", file, line)
        return False

    def _classify_unresolved(self) -> None:
        for kind, position, hidden in self._hidden:
            if any("emitted" not in self._outcomes[id(item)] for item in hidden):
                self._report(kind, position.file, position.line)
        for instance in sorted(self._instances.values(), key=lambda item: item.order):
            unreachable = False
            for registration in instance.registrations:
                reason = self._unresolved_reason(instance, registration)
                if reason is None:
                    continue
                self._unresolved[reason] += 1
                position = registration.position
                if reason == "dynamic path":
                    self._report("dynamic-path", position.file, position.line)
                elif reason == "dynamic methods":
                    self._report("dynamic-methods", position.file, position.line)
                elif reason == "registered after include_router":
                    self._report("route-after-include", position.file, position.line)
                elif reason == "router not mounted":
                    unreachable = True
            if unreachable:
                self._report("router-not-mounted", instance.file, instance.line)

    def _unresolved_reason(self, instance: _Instance, registration: _Registration) -> str | None:
        if registration.path is None:
            return "dynamic path"
        if registration.methods is None:
            return "dynamic methods"
        outcomes = self._outcomes[id(registration)]
        if "emitted" in outcomes:
            return None
        if "dynamic" in outcomes:
            return "dynamic prefix"
        if "late" in outcomes:
            return "registered after include_router"
        if "late-include" in outcomes:
            return "included after its parent was included"
        if "depth-limit" in outcomes:
            return "include depth limit"
        if instance.mounted:
            return "mounted application"
        if self._graph_truncated:
            return "include graph limit"  # the walk may have stopped before reaching it
        return "router not mounted"

    def _build_routes(self) -> list[SourceRoute]:
        return unique_routes(self._emitted_routes())

    def _emitted_routes(self) -> Iterator[SourceRoute]:
        for emission in self._emissions:
            registration = emission.registration
            raw_path = emission.raw_path
            path, mapping = _normalized_path(raw_path)
            position = registration.position
            for method in emission.methods:
                if method not in _HTTP_METHODS:
                    self._report("unsupported-method", position.file, position.line)
                    continue
                yield SourceRoute(
                    framework="fastapi",
                    method=method,
                    path=path,
                    raw_path=raw_path,
                    param_mapping=mapping,
                    file=position.file,
                    line_start=position.line,
                    line_end=registration.line_end,
                    handler=registration.handler,
                    excerpt=registration.excerpt,
                    confidence="direct",
                    include_in_schema=emission.include_in_schema,
                )

    def _report(self, kind: _UnsupportedKind, file: str, line: int) -> None:
        self._unsupported.add((file, line, kind))


# Helpers.


def _module_index(paths: Iterable[str]) -> dict[str, str]:
    """Importable dotted module names inside the source root -> file path.

    Names are source-root-relative. In the src layout (``src/<package>/...`` where ``src``
    is not itself a package) a module is also importable by its name relative to ``src``,
    as it is once the project is installed. Such a name is used only when no
    source-root-relative module and no other ``src`` directory provides its top-level
    name; an ambiguous import stays unresolved and is reported, never guessed.
    """
    files = sorted(paths)
    present = set(files)
    modules = _importable((path, path) for path in files)
    layouts: dict[str, list[tuple[str, str]]] = {}
    for path in files:
        directory = _src_directory(path, present)
        if directory is not None:
            layouts.setdefault(directory, []).append((path[len(directory) + 1 :], path))
    aliases = [_importable(entries) for entries in layouts.values()]
    providers = Counter({name.split(".")[0] for name in modules})
    providers.update(top for names in aliases for top in {name.split(".")[0] for name in names})
    for names in aliases:
        for name, path in names.items():
            if providers[name.split(".")[0]] == 1:
                modules.setdefault(name, path)
    return modules


def _importable(entries: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Dotted module name -> file path, for (path relative to an import root, file path)."""
    modules: dict[str, str] = {}
    for relative, path in sorted(entries):
        name = _module_name(relative)
        # A package (pkg/__init__.py) wins over a same-named module (pkg.py), as in CPython.
        if name is not None and (name not in modules or relative.endswith("/__init__.py")):
            modules[name] = path
    return modules


def _src_directory(path: str, present: set[str]) -> str | None:
    """The nearest ``src`` directory above ``path`` that is not a package, if any."""
    parts = path.split("/")[:-1]
    for end in range(len(parts), 0, -1):
        directory = "/".join(parts[:end])
        if parts[end - 1] == "src" and f"{directory}/__init__.py" not in present:
            return directory
    return None


def _module_parts(path: str) -> tuple[list[str], bool]:
    parts = path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        return parts[:-1], True
    return parts, False


def _module_name(path: str) -> str | None:
    parts, _ = _module_parts(path)
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _package_of(path: str) -> str | None:
    parts, is_package = _module_parts(path)
    package = parts if is_package else parts[:-1]
    if not all(part.isidentifier() for part in package):
        return None
    return ".".join(package)


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level names bound exactly once, to a string literal, and never rebound."""
    counts: Counter[str] = Counter()
    values: dict[str, str] = {}
    for statement in _module_level_statements(tree.body):
        for name, value in _statement_bindings(statement):
            counts[name] += 1
            if value is not None:
                values[name] = value
    rebound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global | ast.Nonlocal):
            rebound.update(node.names)
        elif isinstance(node, ast.NamedExpr):
            rebound.add(node.target.id)
    return {
        name: value
        for name, value in values.items()
        if counts[name] == 1 and name not in rebound
    }


def _module_level_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements executed at module level, including inside if/try/with/for blocks."""
    stack = list(reversed(body))
    while stack:
        statement = stack.pop()
        yield statement
        if isinstance(statement, _NESTED_SCOPES):
            continue
        children: list[ast.stmt] = []
        for attribute in ("body", "orelse", "finalbody"):
            block = getattr(statement, attribute, None)
            if isinstance(block, list):
                children.extend(block)
        for handler in getattr(statement, "handlers", []):
            children.extend(handler.body)
        for case in getattr(statement, "cases", []):
            children.extend(case.body)
        stack.extend(reversed(children))


def _statement_bindings(statement: ast.stmt) -> Iterator[tuple[str, str | None]]:
    """(name, literal value or None) for every name a statement binds at module level."""
    match statement:
        case ast.Assign():
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    yield target.id, _constant_string(statement.value)
                else:
                    yield from ((name, None) for name in _target_names(target))
        case ast.AnnAssign(target=ast.Name(id=name), value=ast.expr() as value):
            yield name, _constant_string(value)
        case ast.AugAssign() | ast.For() | ast.AsyncFor():
            yield from ((name, None) for name in _target_names(statement.target))
        case ast.With() | ast.AsyncWith():
            for item in statement.items:
                if item.optional_vars is not None:
                    yield from ((name, None) for name in _target_names(item.optional_vars))
        case ast.Import():
            for alias in statement.names:
                yield alias.asname or alias.name.split(".")[0], None
        case ast.ImportFrom():
            for alias in statement.names:
                yield alias.asname or alias.name, None
        case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
            yield statement.name, None
        case ast.Delete():
            for target in statement.targets:
                yield from ((name, None) for name in _target_names(target))
        case ast.Try() | ast.TryStar():
            for handler in statement.handlers:
                if handler.name:
                    yield handler.name, None
        case ast.Match():
            for case in statement.cases:
                yield from ((name, None) for name in _pattern_names(case.pattern))
        case ast.TypeAlias(name=ast.Name(id=name)):
            yield name, None


def _constant_string(expression: ast.expr) -> str | None:
    """A str constant, a placeholder-free f-string, or a "+" concatenation of those."""
    if isinstance(expression, ast.Constant):
        return expression.value if isinstance(expression.value, str) else None
    if isinstance(expression, ast.JoinedStr):
        parts = [
            value.value
            for value in expression.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        ]
        return "".join(parts) if len(parts) == len(expression.values) else None
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        left = _constant_string(expression.left)
        right = _constant_string(expression.right)
        return None if left is None or right is None else left + right
    return None


def _expression_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """``node`` and its descendants, excluding the bodies of nested functions/classes/lambdas."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(
            child for child in ast.iter_child_nodes(current) if not isinstance(child, _NESTED_SCOPES)
        )


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _target_names(element)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []  # attributes and subscripts bind no local name


def _pattern_names(pattern: ast.pattern) -> list[str]:
    names: list[str] = []
    for node in ast.walk(pattern):
        if isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            names.append(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.append(node.rest)
    return names


def _parameter_names(arguments: ast.arguments) -> list[str]:
    names = [arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
    names.extend(arg.arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None)
    return names


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _has_double_star(call: ast.Call) -> bool:
    return any(keyword.arg is None for keyword in call.keywords)


def _argument(call: ast.Call, index: int, name: str) -> ast.expr | None:
    """Positional argument ``index`` or keyword ``name``; None when absent or unpacked."""
    if any(isinstance(argument, ast.Starred) for argument in call.args[: index + 1]):
        return None
    if len(call.args) > index:
        return call.args[index]
    return _keyword(call, name)


def _keyword_bool(call: ast.Call, name: str) -> bool | None:
    """Literal bool keyword; FastAPI's default (True) when absent; None when not literal."""
    value = _keyword(call, name)
    if value is None:
        return None if _has_double_star(call) else True
    if isinstance(value, ast.Constant) and isinstance(value.value, bool):
        return value.value
    return None


def _endpoint_name(call: ast.Call) -> str | None:
    endpoint = _argument(call, 1, "endpoint")
    return None if endpoint is None else _dotted_name(endpoint)


def _dotted_name(expression: ast.expr) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        base = _dotted_name(expression.value)
        return None if base is None else f"{base}.{expression.attr}"
    return None


def _normalized_path(raw_path: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    """"{name:converter}" -> "{name}", then ``normalize_path``; mapping keeps source tokens."""
    mapping = sorted({(match.group(1), match.group(0)) for match in _PATH_PARAMETER.finditer(raw_path)})
    stripped = _PATH_PARAMETER.sub(lambda match: "{" + match.group(1) + "}", raw_path)
    return normalize_path(stripped), tuple(mapping)


def _effective_flag(flags: Iterable[bool | None]) -> bool | None:
    """FastAPI ANDs include_in_schema along the include chain; None if any part is unknown."""
    values = list(flags)
    if any(value is False for value in values):
        return False
    if any(value is None for value in values):
        return None
    return True
