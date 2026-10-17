"""Static extraction of literal Express routes from JavaScript/TypeScript (never executed).

A deliberately small, conservative extension of FastAPI discovery. Each JS/TS file is
tokenized by a minimal lexer (comments dropped, string/template/regex literals kept as
single tokens, line numbers preserved) and then matched against a few literal
registration shapes. Text inside comments or string literals therefore can never look
like a route, and nothing in the scanned repository is evaluated.

Supported, per file: apps created with ``express()``; routers created with
``express.Router()`` or ``Router()`` when ``Router`` comes from "express";
``x.get|post|put|patch|delete|options|head(<literal path>, handler...)``, including calls
chained onto one another (``app.get(...).post(...)``: Express returns the receiver);
``x.route(<literal>).get(...).post(...)`` chains; and ``x.use('<literal prefix>', router)``
mounts of routers declared in the same file (nested in-file mounts compose, depth <= 8,
cycle-guarded). Routes are emitted only when reachable from an app that is not itself
mounted. Other recognized shapes become ``unsupported_patterns`` entries instead of
guesses: ``all()`` registrations, non-literal paths or prefixes, optional/regex/wildcard/
array paths, routers imported from other files (cross-file resolution is out of scope),
in-file routers that are never mounted, and route registrations on receivers that are
not an app or router of the file (``unresolved-router``, see ``_unresolved_calls``). A
path is normalized (":id" -> "{id}") only when every segment is plain text or a
whole-segment named parameter, the only Express form with an unambiguous OpenAPI
equivalent.

Work stays linear in the file size: an excerpt reads a bounded window of its span, the
mount walk is charged to a per-file budget (running out is reported as
``mount-graph-limit``), and a file whose "/" characters would need rescanning beyond a
budget to tell regular expressions from division is skipped and counted.

Not reported (outside the scanner's view): registrations on computed members
(``this['app'].get``) or call results (``makeRouter().get``), and ``x.get('/path',
handler)`` whose receiver has no router-like name and whose path has no ":param"
segment, unless the receiver is declared in the file and passed a function: such a call
cannot be told apart from an HTTP-client request.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from discovery._source_files import (
    EXCERPT_WINDOW,
    bounded_notes,
    excerpt_text,
    format_location,
    read_source_files,
    suffix_filter,
    unique_routes,
    unresolved_summary,
)
from discovery.records import SourceRoute, SourceScan, normalize_path

__all__ = ["scan_express_routes"]

_EXTENSIONS = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")
_DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")
_ROUTE_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head"})
# Methods that register routes or return the receiver for chaining (app.use(...).get(...)).
_CHAIN_METHODS = _ROUTE_METHODS | {"all", "route", "use"}
_MAX_MOUNT_DEPTH = 8
# Mount-graph work per file: one unit per instance entered plus one per route and mount it
# holds. A router mounted many times is re-entered each time, so this bounds the walk.
_MAX_GRAPH_WORK = 200_000
_MAX_TEMPLATE_DEPTH = 32
_MAX_LOOKAHEAD_TOKENS = 64
# Characters examined while looking for the end of possible regular expressions, per
# character of the file: a long line of "/" that never close (``(/[(/[...``) would
# otherwise be rescanned from every "/". Beyond it the file is skipped, never guessed.
_REGEX_SCAN_FACTOR = 4
# Characters with special meaning in Express 4/5 paths (optional, regex, wildcard, ...).
_SPECIAL_PATH_CHARACTERS = frozenset("*?+()[]{}!\\:$^|")
_PARAMETER_NAME = re.compile(r"[A-Za-z0-9_]+")
# A whole-segment Express parameter ("/:id"): route syntax that an HTTP-client URL never holds.
_ROUTE_PARAMETER_SEGMENT = re.compile(r"/:[A-Za-z0-9_]")
# Receivers whose name suggests a router even when the scanner cannot resolve them.
_ROUTER_LIKE_NAMES = frozenset({"app", "router", "server"})
_ROUTER_LIKE_SUFFIXES = ("Router", "router", "App", "_app")
_KEYWORDS_BEFORE_EXPRESSION = frozenset(
    {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw",
     "case", "do", "else", "yield", "await"}
)
_MULTI_CHARACTER_PUNCTUATORS = (
    ">>>=", "...", "===", "!==", "**=", "<<=", ">>=", ">>>", "&&=", "||=", "??=",
    "=>", "==", "!=", "<=", ">=", "&&", "||", "??", "?.", "++", "--", "+=", "-=",
    "*=", "/=", "%=", "&=", "|=", "^=", "**", "<<", ">>",
)
# Inside ``${...}``: characters after which "/" begins a regex literal rather than a division.
_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%<>~^")
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_BYTE_ORDER_MARK = chr(0xFEFF)
_OPENERS = frozenset({"(", "[", "{"})
_CLOSER_FOR = {")": "(", "]": "[", "}": "{"}

_TokenKind = Literal["identifier", "string", "template", "regex", "number", "punctuator"]
_BindingKind = Literal[
    "express", "router-factory", "instance", "relative", "package", "function", "string", "other"
]
_Problem = Literal[
    "dynamic-path",
    "dynamic-prefix",
    "unsupported-path-syntax",
    "all-methods",
    "router-not-mounted",
    "cross-file-router",
    "unresolved-router",
    "mount-cycle",
    "mount-depth-limit",
    "mount-graph-limit",
]


def scan_express_routes(
    source_root: Path, *, max_files: int = 5000, max_file_bytes: int = 1_000_000
) -> SourceScan:
    """Literal Express routes under ``source_root``; scanned code is tokenized, never run."""
    files = read_source_files(
        source_root,
        is_relevant=suffix_filter(_EXTENSIONS, _DECLARATION_SUFFIXES),
        max_files=max_files,
        max_file_bytes=max_file_bytes,
    )
    notes = list(files.file_notes)
    results: list[_FileScanner] = []
    for source in files.files:
        lexer = _Lexer(source.text)
        try:
            tokens = lexer.tokenize()
        except _LexError as error:
            notes.append(
                f"{source.path}: skipped (could not be tokenized: {error.reason} at line {error.line})"
            )
            continue
        scanner = _FileScanner(source.path, lexer.text, tokens)
        scanner.run()
        results.append(scanner)

    return SourceScan(
        routes=unique_routes(route for scanner in results for route in scanner.routes),
        files_scanned=len(results),
        files_skipped=files.files_skipped + len(files.files) - len(results),
        unsupported_patterns=[
            format_location(f"express:{kind}", file, line)
            for file, line, kind in sorted(
                (scanner.path, line, kind) for scanner in results for line, kind in scanner.unsupported
            )
        ],
        warnings=[*files.warnings, *bounded_notes(notes), *_summary_warnings(results)],
    )


def _summary_warnings(results: list[_FileScanner]) -> list[str]:
    unresolved: Counter[str] = Counter()
    for scanner in results:
        unresolved.update({kind.replace("-", " "): count for kind, count in scanner.unresolved.items()})
    messages = unresolved_summary("express", unresolved)
    cross_file = sum(scanner.cross_file_mounts for scanner in results)
    if cross_file:
        messages.append(
            f"express: {cross_file} mount(s) use routers from other files; cross-file routers "
            "are not resolved, so their routes are not in this scan"
        )
    if any(scanner.graph_truncated for scanner in results):
        messages.append("express: mount graph too large; scan truncated during route resolution")
    return messages


# Lexer.


@dataclass(frozen=True)
class _Token:
    kind: _TokenKind
    text: str  # source text; the decoded value for string and template literals
    start: int
    end: int
    line: int
    end_line: int
    dynamic: bool = False  # a template literal containing a ${...} substitution


class _LexError(Exception):
    def __init__(self, line: int, reason: str) -> None:
        super().__init__(f"{reason} at line {line}")
        self.line = line
        self.reason = reason


def _punctuator_table(candidates: Iterable[str]) -> dict[str, tuple[str, ...]]:
    table: dict[str, list[str]] = {}
    for candidate in sorted(candidates, key=len, reverse=True):
        table.setdefault(candidate[0], []).append(candidate)
    return {first: tuple(options) for first, options in table.items()}


_PUNCTUATORS_BY_FIRST_CHARACTER = _punctuator_table(_MULTI_CHARACTER_PUNCTUATORS)


class _Lexer:
    """Minimal JS/TS tokenizer: enough structure to find calls, never fooled by literals."""

    def __init__(self, text: str) -> None:
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.tokens: list[_Token] = []
        self._position = 0
        self._line = 1
        self._regex_scanned = 0
        self._regex_budget = _REGEX_SCAN_FACTOR * (len(self.text) + 1024)

    def tokenize(self) -> list[_Token]:
        if self.text.startswith("#!"):
            self._position = self._line_end(0)
        while self._position < len(self.text):
            self._next_token()
        return self.tokens

    def _next_token(self) -> None:
        text, position = self.text, self._position
        char = text[position]
        if char == "\n":
            self._line += 1
            self._position += 1
        elif char.isspace() or char == _BYTE_ORDER_MARK:
            self._position += 1
        elif text.startswith("//", position):
            self._position = self._line_end(position)
        elif text.startswith("/*", position):
            self._position = self._block_comment_end(position)
        elif char in "'\"`":
            self._literal(char)
        elif char == "/" and self._regex_allowed() and (end := self._regex_end(position)):
            self._emit("regex", end)
        elif char.isalpha() or char in "_$":
            self._emit("identifier", self._word_end(position + 1))
        elif char.isdigit() or (char == "." and text[position + 1 : position + 2].isdigit()):
            self._emit("number", self._word_end(position + 1, extra="."))
        else:
            self._emit("punctuator", position + self._punctuator_length(position))

    def _emit(self, kind: _TokenKind, end: int) -> None:
        text = self.text[self._position : end]
        self.tokens.append(_Token(kind, text, self._position, end, self._line, self._line))
        self._position = end

    def _literal(self, quote: str) -> None:
        start, line = self._position, self._line
        if quote == "`":
            value, end, dynamic = self._template(start, depth=0)
            kind: _TokenKind = "template"
        else:
            value, end = self._string(start)
            dynamic, kind = False, "string"
        self.tokens.append(_Token(kind, value, start, end, line, self._line, dynamic))
        self._position = end

    def _string(self, start: int) -> tuple[str, int]:
        text = self.text
        quote = text[start]
        index = start + 1
        chunks: list[str] = []
        while index < len(text):
            char = text[index]
            if char == quote:
                return "".join(chunks), index + 1
            if char == "\n":
                break
            if char == "\\":
                decoded, index = self._escape(index)
                chunks.append(decoded)
            else:
                chunks.append(char)
                index += 1
        raise _LexError(self._line, "unterminated string literal")

    def _template(self, start: int, depth: int) -> tuple[str, int, bool]:
        text = self.text
        line = self._line
        index = start + 1
        chunks: list[str] = []
        dynamic = False
        while index < len(text):
            char = text[index]
            if char == "`":
                return "".join(chunks), index + 1, dynamic
            if char == "\\":
                decoded, index = self._escape(index)
                chunks.append(decoded)
            elif text.startswith("${", index):
                dynamic = True
                index = self._substitution_end(index + 2, depth + 1)
            else:
                if char == "\n":
                    self._line += 1
                chunks.append(char)
                index += 1
        raise _LexError(line, "unterminated template literal")

    def _substitution_end(self, index: int, depth: int) -> int:
        """Skip a template ``${...}`` expression, including nested literals and comments."""
        if depth > _MAX_TEMPLATE_DEPTH:
            raise _LexError(self._line, "template literal nesting too deep")
        text = self.text
        braces = 0
        previous = "{"  # last significant character; "/" after an operator starts a regex
        while index < len(text):
            char = text[index]
            if char in "'\"":
                index = self._string(index)[1]
            elif char == "`":
                index = self._template(index, depth)[1]
            elif text.startswith("//", index):
                index = self._line_end(index)
                continue
            elif text.startswith("/*", index):
                index = self._block_comment_end(index)
                continue
            elif char == "/" and previous in _REGEX_PRECEDERS and (end := self._regex_end(index)):
                index = end  # e.g. `${name.replace(/'/g, "")}`: the quote is not a string
            elif char == "}" and braces == 0:
                return index + 1
            else:
                if char == "{":
                    braces += 1
                elif char == "}":
                    braces -= 1
                elif char == "\n":
                    self._line += 1
                index += 1
                if char.isspace():
                    continue
            previous = char
        raise _LexError(self._line, "unterminated template literal")

    def _escape(self, index: int) -> tuple[str, int]:
        """Decode the escape sequence starting at the backslash at ``index``."""
        text = self.text
        if index + 1 >= len(text):
            raise _LexError(self._line, "unterminated string literal")
        char = text[index + 1]
        if char == "\n":  # line continuation
            self._line += 1
            return "", index + 2
        if char == "x" and (code := _hex_value(text[index + 2 : index + 4], 2)) is not None:
            return chr(code), index + 4
        if char == "u":
            if text.startswith("{", index + 2):
                close = text.find("}", index + 3)
                code = _hex_value(text[index + 3 : close]) if close != -1 else None
                if code is not None and code <= 0x10FFFF:
                    return chr(code), close + 1
            elif (code := _hex_value(text[index + 2 : index + 6], 4)) is not None:
                return chr(code), index + 6
        return _SIMPLE_ESCAPES.get(char, char), index + 2

    def _line_end(self, index: int) -> int:
        newline = self.text.find("\n", index)
        return len(self.text) if newline == -1 else newline

    def _block_comment_end(self, index: int) -> int:
        close = self.text.find("*/", index + 2)
        if close == -1:
            raise _LexError(self._line, "unterminated comment")
        self._line += self.text.count("\n", index, close)
        return close + 2

    def _regex_allowed(self) -> bool:
        """Whether "/" starts a regular expression literal rather than a division."""
        if not self.tokens:
            return True
        previous = self.tokens[-1]
        if previous.kind == "identifier":
            return previous.text in _KEYWORDS_BEFORE_EXPRESSION
        if previous.kind == "punctuator":
            return previous.text not in (")", "]")
        return False

    def _regex_end(self, start: int) -> int | None:
        text = self.text
        index = start + 1
        in_class = False
        end: int | None = None  # None: not a regular expression, "/" is an operator
        while index < len(text):
            char = text[index]
            if char == "\n":
                break
            if char == "\\":
                index += 2
                continue
            if in_class:
                in_class = char != "]"
            elif char == "[":
                in_class = True
            elif char == "/":
                end = self._word_end(index + 1)  # include flags
                break
            index += 1
        self._regex_scanned += index - start
        if self._regex_scanned > self._regex_budget:
            raise _LexError(self._line, "regular expression scan limit exceeded")
        return end

    def _word_end(self, index: int, extra: str = "") -> int:
        text = self.text
        while index < len(text) and (text[index].isalnum() or text[index] in "_$" or text[index] in extra):
            index += 1
        return index

    def _punctuator_length(self, position: int) -> int:
        text = self.text
        for candidate in _PUNCTUATORS_BY_FIRST_CHARACTER.get(text[position], ()):
            if text.startswith(candidate, position):
                if candidate == "?." and text[position + 2 : position + 3].isdigit():
                    continue  # "a?.5:b" is a conditional, not optional chaining
                return len(candidate)
        return 1


def _hex_value(digits: str, length: int | None = None) -> int | None:
    if not digits or (length is not None and len(digits) != length):
        return None
    if any(char not in "0123456789abcdefABCDEF" for char in digits):
        return None
    return int(digits, 16)


# Pattern scanner.


@dataclass(eq=False)
class _Instance:
    kind: Literal["app", "router"]
    line: int
    routes: list[_Route] = field(default_factory=list)
    mounts: list[_Mount] = field(default_factory=list)
    mounted: bool = False


@dataclass(frozen=True, eq=False)
class _Route:
    method: str
    path: str  # a validated literal path
    line_start: int
    line_end: int
    handler: str | None
    excerpt: str


@dataclass(frozen=True, eq=False)
class _Mount:
    child: _Instance
    prefix: str  # validated literal prefix; "" when mounted without one
    problem: _Problem | None  # set when the prefix is not a usable literal
    line: int


@dataclass(frozen=True)
class _Binding:
    kind: _BindingKind
    instance: _Instance | None = None


class _FileScanner:
    """Literal Express registrations in one tokenized file (no cross-file resolution)."""

    def __init__(self, path: str, text: str, tokens: list[_Token]) -> None:
        self.path = path
        self.routes: list[SourceRoute] = []
        self.unsupported: set[tuple[int, _Problem]] = set()
        self.unresolved: Counter[str] = Counter()
        self.cross_file_mounts = 0
        self.graph_truncated = False
        self._text = text
        self._tokens = tokens
        self._closing, self._opening = _matching_brackets(tokens)
        self._functions = _declared_functions(tokens)
        # Sorted indices of "=>" tokens, so "is there an arrow in this argument" is a bisect.
        self._arrows = [
            index for index, token in enumerate(tokens) if token.kind == "punctuator" and token.text == "=>"
        ]
        self._bindings: dict[str, _Binding] = {}
        self._instances: list[_Instance] = []
        self._emitted: set[int] = set()
        self._blocked: dict[int, _Problem] = {}
        self._work = 0

    def run(self) -> None:
        for index, token in enumerate(self._tokens):
            if token.kind != "identifier":
                continue
            if self._after_dot(index):
                # ``this.app.get(...)``: a member receiver is never a known app or router.
                if self._is(index + 1, ".") and self._word(index + 2) in _CHAIN_METHODS:
                    self._unresolved_calls(index, bound=False)
            elif token.text == "import":
                self._import(index)
            elif token.text in ("const", "let", "var"):
                self._declaration(index)
            elif self._is(index + 1, ".") and self._word(index + 2) is not None:
                self._member_call(index)
        self._resolve_mounts()

    # Calls on known instances.

    def _member_call(self, index: int) -> None:
        binding = self._bindings.get(self._tokens[index].text)
        if binding is not None and binding.instance is not None:
            self._instance_calls(binding.instance, index)
        elif self._word(index + 2) in _CHAIN_METHODS:
            self._unresolved_calls(index, bound=binding is not None)
        # Anything else (res.json(...)) is not a receiver this scanner reasons about.

    def _chained_calls(
        self, start: int, method_index: int
    ) -> Iterator[tuple[str, int, int, list[tuple[int, int]]]]:
        """``x.a(...).b(...)``: (method, evidence start, closing parenthesis, arguments) per
        call. The first call's evidence starts at ``start``, each chained one's at its "."."""
        while (method := self._word(method_index)) is not None:
            open_index = self._call_open(method_index + 1)
            close_index = None if open_index is None else self._closing.get(open_index)
            if open_index is None or close_index is None:
                return
            yield method, start, close_index, self._arguments(open_index, close_index)
            start = close_index + 1
            if not self._is(start, "."):
                return
            method_index = start + 1

    def _instance_calls(self, instance: _Instance, index: int) -> None:
        """Calls on a known app or router. ``get``/``post``/..., ``all`` and ``use`` return
        the receiver, so ``app.get(...).post(...)`` registers both routes on ``app``."""
        for method, start, close_index, arguments in self._chained_calls(index, index + 2):
            line = self._tokens[start].line
            if method in _ROUTE_METHODS:
                if len(arguments) < 2:
                    return  # a single argument is a settings read: app.get("env")
                self._register(instance, method, start, close_index, arguments)
            elif method == "all":
                if arguments:
                    self._problem("all-methods", line)
            elif method == "use":
                self._use(instance, line, arguments)
            else:
                if method == "route":
                    self._route_chain(instance, start, close_index, arguments)
                return

    def _register(
        self,
        instance: _Instance,
        method: str,
        index: int,
        close_index: int,
        arguments: list[tuple[int, int]],
    ) -> None:
        line = self._tokens[index].line
        path, problem = self._path_argument(arguments[0])
        if problem is not None:
            self._problem(problem, line)
            return
        instance.routes.append(
            _Route(
                method=method.upper(),
                path=path,
                line_start=line,
                line_end=self._tokens[close_index].end_line,
                handler=self._handler(arguments[-1]),
                excerpt=self._excerpt(index, close_index),
            )
        )

    def _route_chain(
        self, instance: _Instance, index: int, close_index: int, arguments: list[tuple[int, int]]
    ) -> None:
        """``x.route(path).get(h).post(h)``: one route per chained method call."""
        line = self._tokens[index].line
        path, problem = self._path_argument(arguments[0]) if arguments else ("", "dynamic-path")
        if problem is not None:
            self.unsupported.add((line, problem))
        cursor = close_index + 1
        while self._is(cursor, ".") and (method := self._word(cursor + 1)) is not None:
            open_index = self._call_open(cursor + 2)
            method_close = None if open_index is None else self._closing.get(open_index)
            if open_index is None or method_close is None:
                break
            if method == "all":
                self._problem("all-methods", self._tokens[cursor + 1].line)
            elif method not in _ROUTE_METHODS:
                break
            elif problem is not None:
                self.unresolved[problem] += 1
            else:
                handlers = self._arguments(open_index, method_close)
                instance.routes.append(
                    _Route(
                        method=method.upper(),
                        path=path,
                        line_start=line,
                        line_end=self._tokens[method_close].end_line,
                        handler=self._handler(handlers[-1]) if handlers else None,
                        excerpt=self._excerpt(index, method_close),
                    )
                )
            cursor = method_close + 1

    def _use(self, instance: _Instance, line: int, arguments: list[tuple[int, int]]) -> None:
        if not arguments:
            return
        kind, prefix = self._mount_prefix(arguments[0], len(arguments))
        handlers = arguments if kind == "handler" else arguments[1:]
        problem: _Problem | None = kind if kind in ("dynamic-prefix", "unsupported-path-syntax") else None
        for start, end in handlers:
            target = self._mount_target(start, end)
            if target == "cross-file":
                self.unsupported.add((line, "cross-file-router"))
                self.cross_file_mounts += 1
            elif isinstance(target, _Instance) and target is not instance:
                if problem is not None:
                    self.unsupported.add((line, problem))
                target.mounted = True
                instance.mounts.append(_Mount(target, prefix, problem, line))

    def _unresolved_calls(self, index: int, *, bound: bool) -> None:
        """Route registrations on a receiver that is not an app or router of this file.

        Nothing is emitted: the receiver's mount point is unknown. A call with two or more
        arguments and a literal "/" path is reported as ``unresolved-router`` when the
        receiver is named like a router (``app``, ``this.router``, ``adminRouter``), when
        the path holds a ":param" segment, or when a receiver declared in this file is
        passed a function. Once one call in a chain is reported, the calls chained onto it
        (``app.get(...).post(...)``) are reported too, and ``x.route('/p').get(h)`` is
        reported whatever the receiver (no HTTP client has that shape). Other calls, such
        as HTTP-client requests (``axios.get('/v1/x', config)``), are left alone.
        """
        name = self._tokens[index].text
        router_like = name in _ROUTER_LIKE_NAMES or name.endswith(_ROUTER_LIKE_SUFFIXES)
        registering = False
        for method, start, close_index, arguments in self._chained_calls(index, index + 2):
            line = self._tokens[start].line
            if method == "use":
                continue
            if method == "route":  # x.route('/p').get(h).post(h): one per chained method
                literal = bool(arguments) and self._literal_path(arguments[0]) is not None
                if literal and self._is(close_index + 1, "."):
                    for chained, *_ in self._chained_calls(close_index + 1, close_index + 2):
                        if chained not in _ROUTE_METHODS and chained != "all":
                            break
                        self._problem("unresolved-router", line)
                return
            if (method not in _ROUTE_METHODS and method != "all") or len(arguments) < 2:
                return
            if not registering:
                path = self._literal_path(arguments[0])
                registering = path is not None and (
                    router_like
                    or _ROUTE_PARAMETER_SEGMENT.search(path) is not None
                    or (bound and self._passes_function(arguments[1:]))
                )
                if not registering:
                    return
            self._problem("unresolved-router", line)

    def _literal_path(self, argument: tuple[int, int]) -> str | None:
        """The argument's text when it is a literal path starting with "/"."""
        start, end = argument
        token = self._tokens[start]
        literal = end - start == 1 and token.kind in ("string", "template") and not token.dynamic
        return token.text if literal and token.text.startswith("/") else None

    def _passes_function(self, arguments: list[tuple[int, int]]) -> bool:
        """An inline function (``function``, ``async``, ``x =>``, ``(a, b) =>``) or a function
        declared or bound in this file among the arguments."""
        for start, end in arguments:
            token = self._tokens[start]
            if token.kind == "identifier":
                if token.text in ("function", "async") or self._is(start + 1, "=>"):
                    return True
                binding = self._bindings.get(token.text)
                if end - start == 1 and (
                    token.text in self._functions or (binding is not None and binding.kind == "function")
                ):
                    return True
            elif self._is(start, "("):
                close = self._closing.get(start)
                if close is not None and self._is(close + 1, "=>"):
                    return True
        return False

    # Path and argument classification.

    def _path_argument(self, argument: tuple[int, int]) -> tuple[str, _Problem | None]:
        start, end = argument
        token = self._tokens[start]
        single = end - start == 1
        if single and token.kind in ("string", "template") and not token.dynamic:
            if _path_segments(token.text) is None:
                return "", "unsupported-path-syntax"
            return token.text, None
        if (single and token.kind == "regex") or self._is(start, "["):
            return "", "unsupported-path-syntax"
        return "", "dynamic-path"

    def _mount_prefix(
        self, argument: tuple[int, int], count: int
    ) -> tuple[Literal["prefix", "handler", "dynamic-prefix", "unsupported-path-syntax"], str]:
        start, end = argument
        token = self._tokens[start]
        single = end - start == 1
        if single and token.kind in ("string", "template"):
            if token.dynamic:
                return "dynamic-prefix", ""
            if _path_segments(token.text) is None:
                return "unsupported-path-syntax", ""
            return "prefix", token.text
        if (single and token.kind == "regex") or self._is(start, "["):
            return "unsupported-path-syntax", ""
        if count == 1 or self._handler_expression(start, end):
            return "handler", ""
        # An identifier or member expression may hold a prefix string: do not guess.
        return "dynamic-prefix", ""

    def _handler_expression(self, start: int, end: int) -> bool:
        """Arguments that cannot be a path: functions, calls, routers, imported packages."""
        token = self._tokens[start]
        if end - start == 1 and token.kind == "identifier":
            binding = self._bindings.get(token.text)
            if binding is not None:
                return binding.kind in ("instance", "express", "router-factory", "package", "function")
            return token.text in self._functions
        if token.kind == "identifier" and token.text in ("function", "async"):
            return True
        arrow = bisect.bisect_left(self._arrows, start)  # an "=>" anywhere in the argument
        if arrow < len(self._arrows) and self._arrows[arrow] < end:
            return True
        opening = self._opening.get(end - 1) if self._is(end - 1, ")") else None
        return opening is not None and opening > start  # a call such as cors() or express.json()

    def _mount_target(self, start: int, end: int) -> _Instance | Literal["cross-file"] | None:
        token = self._tokens[start]
        if end - start == 1 and token.kind == "identifier":
            binding = self._bindings.get(token.text)
            if binding is None:
                return None
            if binding.instance is not None:
                return binding.instance
            return "cross-file" if binding.kind == "relative" else None
        module = self._require(start)
        if module is not None and _is_relative(module[0]):
            return "cross-file"
        return None

    # Declarations and imports.

    def _declaration(self, index: int) -> None:
        cursor = index + 1
        names: list[tuple[str, str]] | None = None
        local = self._word(cursor)
        if self._is(cursor, "{"):
            close = self._closing.get(cursor)
            if close is None:
                return
            names = self._destructured(cursor, close)
            cursor = close + 1
        elif local is not None:
            cursor += 1
        else:
            return
        cursor = self._skip_type_annotation(cursor)
        if not self._is(cursor, "="):
            return
        if names is None and local is not None:
            self._bindings[local] = self._initializer(cursor + 1, self._tokens[index].line)
            return
        source = self._destructuring_source(cursor + 1)
        for imported, name in names or []:
            if source == "express":
                kind: _BindingKind = "router-factory" if imported == "Router" else "other"
            else:
                kind = source or "other"
            self._bindings[name] = _Binding(kind)

    def _initializer(self, cursor: int, line: int) -> _Binding:
        while self._word(cursor) in ("await", "new"):
            cursor += 1
        module = self._require(cursor)
        if module is not None:
            name, after = module
            if name == "express":
                return self._express_value(after, line)
            return _Binding("relative" if _is_relative(name) else "package")
        token = self._token(cursor)
        if token is None:
            return _Binding("other")
        if token.kind == "identifier":
            binding = self._bindings.get(token.text)
            called = self._call_open(cursor + 1) is not None
            if binding is not None and binding.kind == "express":
                return self._express_value(cursor + 1, line)
            if binding is not None and binding.kind == "router-factory" and called:
                return self._new_instance("router", line)
            if binding is not None and binding.kind == "package" and called:
                return _Binding("package")  # e.g. rateLimit({...}): middleware from a package
            if token.text in ("function", "async") or self._is(cursor + 1, "=>"):
                return _Binding("function")
            return _Binding("other")
        if token.kind in ("string", "template"):
            return _Binding("string")
        close = self._closing.get(cursor) if self._is(cursor, "(") else None
        if close is not None and self._is(close + 1, "=>"):
            return _Binding("function")
        return _Binding("other")

    def _express_value(self, cursor: int, line: int) -> _Binding:
        """What an expression starting with the express module evaluates to."""
        if self._is(cursor, "("):
            return self._new_instance("app", line)
        if self._is(cursor, "."):
            called = self._call_open(cursor + 2) is not None
            if self._word(cursor + 1) == "Router":
                return self._new_instance("router", line) if called else _Binding("router-factory")
            return _Binding("function" if called else "other")  # express.json() is middleware
        return _Binding("express")

    def _new_instance(self, kind: Literal["app", "router"], line: int) -> _Binding:
        instance = _Instance(kind, line)
        self._instances.append(instance)
        return _Binding("instance", instance)

    def _destructured(self, open_index: int, close_index: int) -> list[tuple[str, str]]:
        """``{ Router, json: parseJson }`` -> [("Router", "Router"), ("json", "parseJson")]."""
        names: list[tuple[str, str]] = []
        for start, end in self._arguments(open_index, close_index):
            imported = self._word(start)
            if imported is None:
                continue  # "...rest" or a computed key
            alias = self._word(start + 2) if self._is(start + 1, ":") else None
            if end - start == 3 and alias is not None:
                names.append((imported, alias))
            elif end - start == 1 or self._is(start + 1, "="):
                names.append((imported, imported))
        return names

    def _destructuring_source(self, cursor: int) -> _BindingKind | Literal["express"] | None:
        module = self._require(cursor)
        if module is not None:
            if module[0] == "express":
                return "express"
            return "relative" if _is_relative(module[0]) else "package"
        binding = self._bindings.get(self._word(cursor) or "")
        if binding is not None and binding.kind == "express" and not self._is(cursor + 1, "."):
            return "express"
        return None

    def _import(self, index: int) -> None:
        cursor = index + 1
        if self._is(cursor, "(") or self._is(cursor, "."):
            return  # import("m") or import.meta
        if self._word(cursor) == "type" and (
            self._word(cursor + 1) not in (None, "from") or self._is(cursor + 1, "{") or self._is(cursor + 1, "*")
        ):
            return  # TypeScript type-only import: nothing exists at runtime
        whole: list[str] = []
        named: list[tuple[str, str]] = []
        default = self._word(cursor)
        if default is not None and default != "from":
            whole.append(default)
            cursor += 1
            if self._is(cursor, "="):  # TypeScript: import x = require("m")
                module = self._require(cursor + 1)
                if module is not None:
                    self._bind_import(module[0], whole, named)
                return
            if self._is(cursor, ","):
                cursor += 1
        namespace = self._word(cursor + 2)
        if self._is(cursor, "*") and self._word(cursor + 1) == "as" and namespace is not None:
            whole.append(namespace)
            cursor += 3
        elif self._is(cursor, "{"):
            close = self._closing.get(cursor)
            if close is None:
                return
            named = self._import_specifiers(cursor, close)
            cursor = close + 1
        module_token = self._token(cursor + 1)
        if self._word(cursor) == "from" and module_token is not None and module_token.kind == "string":
            self._bind_import(module_token.text, whole, named)

    def _import_specifiers(self, open_index: int, close_index: int) -> list[tuple[str, str]]:
        names: list[tuple[str, str]] = []
        for start, end in self._arguments(open_index, close_index):
            words = [self._word(index) for index in range(start, end)]
            if any(word is None for word in words):
                continue
            if words[0] == "type" and len(words) > 1 and words[1] != "as":
                continue  # inline type-only specifier
            if len(words) == 1:
                names.append((str(words[0]), str(words[0])))
            elif len(words) == 3 and words[1] == "as":
                names.append((str(words[0]), str(words[2])))
        return names

    def _bind_import(self, module: str, whole: list[str], named: list[tuple[str, str]]) -> None:
        if module == "express":
            for local in whole:
                self._bindings[local] = _Binding("express")
            for imported, local in named:
                self._bindings[local] = _Binding("router-factory" if imported == "Router" else "other")
            return
        kind: _BindingKind = "relative" if _is_relative(module) else "package"
        for local in [*whole, *(local for _, local in named)]:
            self._bindings[local] = _Binding(kind)

    def _require(self, cursor: int) -> tuple[str, int] | None:
        """``require("m")`` at ``cursor`` -> (module, index after the closing parenthesis)."""
        argument = self._token(cursor + 2)
        if (
            self._word(cursor) == "require"
            and self._is(cursor + 1, "(")
            and argument is not None
            and argument.kind in ("string", "template")
            and not argument.dynamic
            and self._is(cursor + 3, ")")
        ):
            return argument.text, cursor + 4
        return None

    # Mount graph.

    def _resolve_mounts(self) -> None:
        roots = [item for item in self._instances if item.kind == "app" and not item.mounted]
        for root in roots:
            self._visit(root, (), None, (root,), root.line)
        for instance in self._instances:
            unreachable = 0
            for route in instance.routes:
                if id(route) in self._emitted:
                    continue
                blocked = self._blocked.get(id(route))
                if blocked is not None:
                    self.unresolved[blocked] += 1
                elif self.graph_truncated:
                    self.unresolved["mount-graph-limit"] += 1  # the walk may have stopped first
                else:
                    unreachable += 1
            if unreachable:
                self.unsupported.add((instance.line, "router-not-mounted"))
                self.unresolved["router-not-mounted"] += unreachable

    def _visit(
        self,
        instance: _Instance,
        prefixes: tuple[str, ...],
        blocked: _Problem | None,
        chain: tuple[_Instance, ...],
        line: int,
    ) -> None:
        """Emit ``instance``'s routes below ``prefixes``; ``line`` is the mount that led here."""
        self._work += 1 + len(instance.routes) + len(instance.mounts)
        if self._work > _MAX_GRAPH_WORK:
            if not self.graph_truncated:  # reported once, where the walk stopped
                self.graph_truncated = True
                self.unsupported.add((line, "mount-graph-limit"))
            return
        for route in instance.routes:
            if blocked is not None:
                self._blocked.setdefault(id(route), blocked)
            else:
                self._emit(route, prefixes)
        for mount in instance.mounts:
            if mount.child in chain:
                self.unsupported.add((mount.line, "mount-cycle"))
            elif len(chain) > _MAX_MOUNT_DEPTH:
                self.unsupported.add((mount.line, "mount-depth-limit"))
            else:
                self._visit(
                    mount.child,
                    (*prefixes, mount.prefix),
                    blocked or mount.problem,
                    (*chain, mount.child),
                    mount.line,
                )

    def _emit(self, route: _Route, prefixes: tuple[str, ...]) -> None:
        raw_path = "".join(prefixes) + route.path
        normalized = _normalized_path(raw_path)
        if normalized is None:  # every part was validated; kept as a guard against regressions
            self.unsupported.add((route.line_start, "unsupported-path-syntax"))
            return
        path, mapping = normalized
        self._emitted.add(id(route))
        self.routes.append(
            SourceRoute(
                framework="express",
                method=route.method,
                path=path,
                raw_path=raw_path,
                param_mapping=mapping,
                file=self.path,
                line_start=route.line_start,
                line_end=route.line_end,
                handler=route.handler,
                excerpt=route.excerpt,
                confidence="direct",
            )
        )

    # Token helpers.

    def _problem(self, kind: _Problem, line: int) -> None:
        self.unsupported.add((line, kind))
        self.unresolved[kind] += 1

    def _token(self, index: int) -> _Token | None:
        return self._tokens[index] if 0 <= index < len(self._tokens) else None

    def _is(self, index: int, punctuator: str) -> bool:
        token = self._token(index)
        return token is not None and token.kind == "punctuator" and token.text == punctuator

    def _word(self, index: int) -> str | None:
        token = self._token(index)
        return token.text if token is not None and token.kind == "identifier" else None

    def _after_dot(self, index: int) -> bool:
        return self._is(index - 1, ".") or self._is(index - 1, "?.")

    def _call_open(self, index: int) -> int | None:
        """Index of the "(" that calls a member, skipping TypeScript type arguments."""
        if self._is(index, "("):
            return index
        if not self._is(index, "<"):
            return None
        depth = 0
        cursor = index
        limit = min(len(self._tokens), index + _MAX_LOOKAHEAD_TOKENS)
        while cursor < limit:
            token = self._tokens[cursor]
            if token.kind == "punctuator":
                if token.text in _OPENERS and cursor in self._closing:
                    cursor = self._closing[cursor] + 1
                    continue
                if set(token.text) == {"<"}:
                    depth += len(token.text)
                elif set(token.text) == {">"}:
                    depth -= len(token.text)
                elif token.text in (";", ")"):
                    return None
                if depth <= 0:
                    return cursor + 1 if self._is(cursor + 1, "(") else None
            cursor += 1
        return None

    def _arguments(self, open_index: int, close_index: int) -> list[tuple[int, int]]:
        """Token ranges of the comma-separated items between two matching brackets."""
        items: list[tuple[int, int]] = []
        start = cursor = open_index + 1
        while cursor < close_index:
            if self._tokens[cursor].kind == "punctuator":
                text = self._tokens[cursor].text
                if text in _OPENERS and cursor in self._closing:
                    cursor = self._closing[cursor] + 1
                    continue
                if text == ",":
                    if cursor > start:
                        items.append((start, cursor))
                    start = cursor + 1
            cursor += 1
        if close_index > start:
            items.append((start, close_index))
        return items

    def _skip_type_annotation(self, cursor: int) -> int:
        """Skip ``: Type`` after a declared name (TypeScript); returns the "=" index if any."""
        if not self._is(cursor, ":"):
            return cursor
        index = cursor + 1
        limit = min(len(self._tokens), cursor + _MAX_LOOKAHEAD_TOKENS)
        while index < limit:
            token = self._tokens[index]
            if token.kind == "punctuator":
                if token.text in ("=", ";", ","):
                    return index
                if token.text in _OPENERS and index in self._closing:
                    index = self._closing[index] + 1
                    continue
            index += 1
        return index

    def _handler(self, argument: tuple[int, int]) -> str | None:
        """The handler's name when the argument is a plain identifier or member chain."""
        start, end = argument
        if (end - start) % 2 == 0:
            return None
        parts: list[str] = []
        for offset, index in enumerate(range(start, end)):
            if offset % 2 == 1:
                if not self._is(index, "."):
                    return None
                continue
            word = self._word(index)
            if word is None:
                return None
            parts.append(word)
        return ".".join(parts)

    def _excerpt(self, start_index: int, close_index: int) -> str:
        """The registration's source text; at most a window of it is read (calls nest)."""
        start, end = self._tokens[start_index].start, self._tokens[close_index].end
        stop = min(end, start + EXCERPT_WINDOW)
        return excerpt_text(self._text[start:stop], truncated=stop < end)


def _matching_brackets(tokens: list[_Token]) -> tuple[dict[int, int], dict[int, int]]:
    """(open index -> close index, close index -> open index); stray closers are ignored."""
    closing: dict[int, int] = {}
    opening: dict[int, int] = {}
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.kind != "punctuator":
            continue
        if token.text in _OPENERS:
            stack.append(index)
        elif token.text in _CLOSER_FOR and stack and tokens[stack[-1]].text == _CLOSER_FOR[token.text]:
            open_index = stack.pop()
            closing[open_index] = index
            opening[index] = open_index
    return closing, opening


def _declared_functions(tokens: list[_Token]) -> set[str]:
    names: set[str] = set()
    for index, token in enumerate(tokens[:-1]):
        if token.kind == "identifier" and token.text == "function":
            following = tokens[index + 2] if tokens[index + 1].text == "*" and index + 2 < len(tokens) else tokens[index + 1]
            if following.kind == "identifier":
                names.add(following.text)
    return names


def _is_relative(module: str) -> bool:
    return module in (".", "..") or module.startswith(("./", "../", "/"))


def _path_segments(path: str) -> list[str] | None:
    """Segments of a supported literal path, or None when Express path syntax is involved."""
    if path and not path.startswith("/"):
        return None
    segments = [segment for segment in path.split("/") if segment]
    for segment in segments:
        if segment.startswith(":"):
            if not _PARAMETER_NAME.fullmatch(segment[1:]):
                return None  # optional (:id?), regex (:id(\\d+)) or multi-parameter segment
        elif any(char in _SPECIAL_PATH_CHARACTERS for char in segment):
            return None
    return segments


def _normalized_path(raw_path: str) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    """":id" -> "{id}" and ``normalize_path``; mapping keeps the source token per parameter."""
    segments = _path_segments(raw_path)
    if segments is None:
        return None
    normalized = ["{" + segment[1:] + "}" if segment.startswith(":") else segment for segment in segments]
    mapping = sorted({(segment[1:], segment) for segment in segments if segment.startswith(":")})
    return normalize_path("/" + "/".join(normalized)), tuple(mapping)
