"""Python adapter (tree-sitter-python).

Supported origins:
  * ``import stripe`` / ``import stripe as s`` / ``from stripe import PaymentIntent``
    then ``stripe.PaymentIntent.create(...)`` / ``.retrieve(...)``
  * ``import requests`` / ``from requests import get`` then
    ``requests.get(URL)`` (and post/put/patch/delete/request) followed by
    ``.json()``, where URL is a literal, f-string, ``+`` concatenation or a
    module/function string constant starting with the provider base URL.
Field reads: ``x["f"]``, ``x.f``, ``x.get("f")`` (nested chains allowed).
"""
from __future__ import annotations

import tree_sitter_python as tsp
from tree_sitter import Language, Parser

from ..mappings import PYTHON_HTTP_FUNCS, PYTHON_SDK_METHODS, PYTHON_SDK_MODULES, STORE_CALL_SUFFIXES
from ..origins import (
    UNKNOWN, Catalog, Const, FieldVal, FileContext, Handle, OriginVal, Sym, number_literal, resolve_url,
)
from .base import ScopeBase

_LANG = Language(tsp.language())
TRACKED_MODULES = set(PYTHON_SDK_MODULES) | {"requests"}
UNWRAP = {"parenthesized_expression", "await"}
FUNC_TYPES = {"function_definition"}
COMPARE_OPS = {"<", ">", "<=", ">=", "==", "!=", "is", "is not", "in", "not in"}


def _text(n) -> str:
    return n.text.decode("utf-8", "replace")


def unwrap(n):
    while n is not None and n.type in UNWRAP:
        inner = [c for c in n.named_children if c.type != "comment"]
        if not inner:
            break
        n = inner[0]
    return n


def parent_skipping(n):
    """Return (effective_child, parent), climbing through parens/await."""
    cur, p = n, n.parent
    while p is not None and p.type in UNWRAP:
        cur, p = p, p.parent
    return cur, p


def string_literal(n):
    """Plain (non-f) string literal value, else None."""
    n = unwrap(n)
    if n is None or n.type != "string":
        return None
    start = n.child(0)
    if start is not None and "f" in _text(start).lower():
        return None
    return "".join(_text(c) for c in n.named_children if c.type == "string_content")


class PyScope(ScopeBase):
    LANGUAGE = "python"

    # ----- evaluation --------------------------------------------------
    def eval(self, node):
        node = unwrap(node)
        if node is None:
            return None
        if node.id in self.memo:
            return self.memo[node.id]
        val = self._eval(node)
        self.memo[node.id] = val
        return val

    def _eval(self, n):
        t = n.type
        if t == "identifier":
            return self.env.get(_text(n))
        if t == "string":
            return Const(self.string_parts(n))
        if t == "concatenated_string":
            parts: tuple = ()
            for c in n.named_children:
                v = self.eval(c)
                parts += v.parts if isinstance(v, Const) else (UNKNOWN,)
            return Const(parts)
        if t == "binary_operator" and _text(n.child_by_field_name("operator")) == "+":
            l, r = self.eval(n.child_by_field_name("left")), self.eval(n.child_by_field_name("right"))
            if isinstance(l, Const) or isinstance(r, Const):
                lp = l.parts if isinstance(l, Const) else (UNKNOWN,)
                rp = r.parts if isinstance(r, Const) else (UNKNOWN,)
                return Const(lp + rp)
            return None
        if t == "attribute":
            obj = self.eval(n.child_by_field_name("object"))
            attr = _text(n.child_by_field_name("attribute"))
            if isinstance(obj, Sym):
                return Sym(f"{obj.name}.{attr}")
            if isinstance(obj, (OriginVal, FieldVal)):
                return self._extend(obj, attr)
            return None
        if t == "subscript":
            obj = self.eval(n.child_by_field_name("value"))
            if isinstance(obj, (OriginVal, FieldVal)):
                key = string_literal(n.child_by_field_name("subscript"))
                if key is not None:
                    return self._extend(obj, key)
            return None
        if t == "call":
            return self.eval_call(n)
        return None

    @staticmethod
    def _extend(obj, key):
        if isinstance(obj, OriginVal):
            return FieldVal(obj, (key,), obj.evidence, obj.confidence)
        return FieldVal(obj.origin, obj.path + (key,), obj.evidence, obj.confidence)

    def string_parts(self, n) -> tuple:
        start = n.child(0)
        is_f = start is not None and "f" in _text(start).lower()
        parts: list = []
        for c in n.children:
            if c.type == "string_content":
                parts.append(_text(c))
            elif c.type == "escape_sequence":
                parts.append(_text(c).encode().decode("unicode_escape", "ignore"))
            elif c.type == "interpolation" and is_f:
                expr = c.child_by_field_name("expression")
                v = self.eval(expr) if expr is not None else None
                parts.extend(v.parts if isinstance(v, Const) else (UNKNOWN,))
        merged: list = []
        for p in parts:
            if merged and isinstance(p, str) and isinstance(merged[-1], str):
                merged[-1] += p
            else:
                merged.append(p)
        return tuple(merged) if merged else ("",)

    def _args(self, call):
        args = call.child_by_field_name("arguments")
        pos, kw = [], {}
        if args is None:
            return pos, kw
        for c in args.named_children:
            if c.type == "keyword_argument":
                kw[_text(c.child_by_field_name("name"))] = c.child_by_field_name("value")
            elif c.type in ("list_splat", "dictionary_splat"):
                pos.append(None)
            elif c.type != "comment":
                pos.append(c)
        return pos, kw

    def eval_call(self, call):
        fn = unwrap(call.child_by_field_name("function"))
        line = call.start_point.row + 1
        if fn is not None and fn.type == "attribute":
            attr = _text(fn.child_by_field_name("attribute"))
            obj = self.eval(fn.child_by_field_name("object"))
            if attr == "json" and isinstance(obj, Handle):
                conf = "inferred" if any(l.label in ("alias", "call_site") for l in obj.evidence) else "direct"
                return OriginVal(obj.endpoint, "http_response", obj.library, obj.call,
                                 obj.evidence + (self.ctx.loc(call, "json"),), conf)
            if attr == "get" and isinstance(obj, (OriginVal, FieldVal)):
                pos, _ = self._args(call)
                key = string_literal(pos[0]) if pos and pos[0] is not None else None
                if key is not None:
                    return self._extend(obj, key)
                return None
        callee = self.eval(fn) if fn is not None else None
        if not isinstance(callee, Sym):
            return None
        name = callee.name
        op = PYTHON_SDK_METHODS.get(name)
        if op is not None:
            if op.service_id != self.ctx.service_id:
                return None
            ep = self.ctx.catalog.by_template(op.service_id, op.method, op.path)
            if ep is None:
                self.ctx.warn("endpoint_not_in_catalog", line, f"{op.call} -> {op.method} {op.path}")
                return None
            return OriginVal(ep, "sdk_response", op.library, op.call, (self.ctx.loc(call, "sdk_call"),))
        if name.startswith("stripe.") and name.count(".") >= 2 and "stripe" in self.ctx.service_id:
            self.ctx.gap("unmapped_sdk_method", line, name)
            return None
        if name in PYTHON_HTTP_FUNCS:
            pos, kw = self._args(call)
            method = PYTHON_HTTP_FUNCS[name]
            url_node = None
            if method is None:  # requests.request(method, url)
                m_node = pos[0] if pos else kw.get("method")
                m = string_literal(m_node) if m_node is not None else None
                if m is None:
                    self.ctx.gap("dynamic_http_method", line, name)
                    return None
                method = m.upper()
                url_node = pos[1] if len(pos) > 1 else kw.get("url")
            else:
                url_node = pos[0] if pos else kw.get("url")
            if url_node is None:
                self.ctx.gap("dynamic_url", line, f"{name} without URL argument")
                return None
            v = self.eval(url_node)
            parts = v.parts if isinstance(v, Const) else (UNKNOWN,)
            res = resolve_url(parts, method, self.ctx.service_id, self.ctx.base_urls, self.ctx.catalog)
            if res.status == "matched":
                return Handle(res.endpoint, "requests", name, (self.ctx.loc(call, "http_call"),))
            if res.status == "dynamic":
                self.ctx.gap("dynamic_url", line, f"{name}: {res.detail}")
            elif res.status in ("unmatched", "ambiguous"):
                self.ctx.warn(f"endpoint_{res.status}", line, res.detail)
            return None
        return None

    # ----- traversal ---------------------------------------------------
    def walk(self, n):
        t = n.type
        if t in FUNC_TYPES or t == "class_definition":
            name = n.child_by_field_name("name")
            if name is not None and _text(name) in self.env:
                self.clear(_text(name), n)
            return
        if t == "decorated_definition":
            for c in n.children:
                self.walk(c)
            return
        if t == "lambda":
            return
        if t in ("import_statement", "import_from_statement"):
            self.handle_import(n)
            return
        if t == "assignment":
            self.handle_assignment(n)
            return
        if t == "augmented_assignment":
            self.walk(n.child_by_field_name("right"))
            left = n.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                self.clear(_text(left), n)
            else:
                self.walk(left)
            return
        if t == "for_statement":
            self.walk(n.child_by_field_name("right"))
            self._clear_targets(n.child_by_field_name("left"), n)
            for f in ("body", "alternative"):
                c = n.child_by_field_name(f)
                if c is not None:
                    self.walk(c)
            return
        if t == "as_pattern":
            for c in n.named_children[:-1]:
                self.walk(c)
            tgt = n.child_by_field_name("alias") or n.named_children[-1]
            self._clear_targets(tgt, n)
            return
        if t == "attribute":
            self.walk(n.child_by_field_name("object"))
            return
        if t == "keyword_argument":
            self.walk(n.child_by_field_name("value"))
            return
        if t == "identifier":
            val = self.env.get(_text(n))
            if isinstance(val, (OriginVal, FieldVal, Handle)):
                self.mark_used(_text(n))
                self.on_value_use(n, val)
            return
        if t == "call":
            val = self.eval(n)
            if isinstance(val, (OriginVal, FieldVal, Handle)):
                self.on_value_use(n, val)
                args = n.child_by_field_name("arguments")
                if args is not None:
                    self.walk(args)
                return
        for c in n.children:
            self.walk(c)

    def _clear_targets(self, tgt, node):
        if tgt is None:
            return
        if tgt.type == "identifier":
            self.clear(_text(tgt), node)
            return
        for c in tgt.named_children:
            self._clear_targets(c, node)

    def handle_import(self, n):
        if n.type == "import_statement":
            for c in n.named_children:
                if c.type == "dotted_name":
                    mod, local = _text(c), _text(c).split(".")[0]
                elif c.type == "aliased_import":
                    mod, local = _text(c.child_by_field_name("name")), _text(c.child_by_field_name("alias"))
                else:
                    continue
                root = mod.split(".")[0]
                if root in TRACKED_MODULES:
                    self.env[local] = Sym(mod if c.type == "aliased_import" else root)
                elif local in self.env:
                    self.clear(local, n)
            return
        mod_node = n.child_by_field_name("module_name")
        mod = _text(mod_node) if mod_node is not None else ""
        for c in n.children_by_field_name("name"):
            if c.type == "aliased_import":
                name, local = _text(c.child_by_field_name("name")), _text(c.child_by_field_name("alias"))
            else:
                name = local = _text(c)
            if mod.split(".")[0] in TRACKED_MODULES:
                self.env[local] = Sym(f"{mod}.{name}")
            elif local in self.env:
                self.clear(local, n)
        if any(c.type == "wildcard_import" for c in n.children) and mod.split(".")[0] in TRACKED_MODULES:
            self.ctx.gap("wildcard_import", n.start_point.row + 1, mod)

    def handle_assignment(self, n):
        left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
        if right is None:
            if left is not None and left.type == "identifier":
                self.clear(_text(left), n)
            return
        if right.type == "assignment":  # chained a = b = expr
            self.handle_assignment(right)
            inner = right.child_by_field_name("left")
            val = self.env.get(_text(inner)) if inner is not None and inner.type == "identifier" else None
        else:
            val = self.eval(right)
            u = unwrap(right)
            simple_alias = unwrap(right).type == "identifier"
            if left is not None and left.type == "identifier" and isinstance(val, (OriginVal, FieldVal, Handle)):
                self.skip.add(u.id)
                if simple_alias:
                    self.mark_used(_text(u))
                    val = self.alias_of(val, n)
            self.walk(right)
            self.skip.discard(u.id)
        if left is None:
            return
        if left.type == "identifier":
            self.bind(_text(left), val, n)
        elif left.type in ("pattern_list", "tuple_pattern", "list_pattern"):
            if isinstance(val, (OriginVal, FieldVal)):
                self.ctx.gap("unpacking_unsupported", n.start_point.row + 1)
            self._clear_targets(left, n)
        else:
            self.walk(left)

    # ----- uses --------------------------------------------------------
    def on_value_use(self, node, val):
        if node.id in self.skip:
            return
        cur = node
        while True:
            eff, p = parent_skipping(cur)
            if p is None:
                cur = eff
                break
            if p.type == "attribute" and p.child_by_field_name("object") == eff:
                attr = _text(p.child_by_field_name("attribute"))
                gp_eff, gp = parent_skipping(p)
                is_call = gp is not None and gp.type == "call" and gp.child_by_field_name("function") == gp_eff
                if is_call:
                    if isinstance(val, Handle) and attr == "json":
                        val = self.eval(gp)
                        cur = gp
                        continue
                    if isinstance(val, (OriginVal, FieldVal)) and attr == "get":
                        nv = self.eval(gp)
                        if isinstance(nv, FieldVal):
                            val, cur = nv, gp
                            continue
                    if isinstance(val, Handle):
                        return
                    cur = eff  # method call on the value: a read
                    if isinstance(val, OriginVal):
                        return  # e.g. intent.to_dict(); not a field dependency
                    break
                if isinstance(val, Handle):
                    return  # resp.status_code etc. are not response-body fields
                val = self._extend(val, attr)
                cur = p
                continue
            if p.type == "subscript" and p.child_by_field_name("value") == eff:
                if isinstance(val, Handle):
                    return
                key = string_literal(p.child_by_field_name("subscript"))
                if key is None:
                    sub = unwrap(p.child_by_field_name("subscript"))
                    if sub is not None and sub.type == "integer":
                        val = self._extend(val, "[]")
                        cur = p
                        continue
                    o, path = self._op(val)
                    self.ctx.gap("dynamic_field_access", p.start_point.row + 1, _text(p)[:60])
                    self.emit(o, path + ("*",), "unknown", None, None, "unresolved",
                              self._ev(val) + (self.ctx.loc(p, "use"),))
                    return
                val = self._extend(val, key)
                cur = p
                continue
            cur = eff
            break
        if cur.id in self.skip or isinstance(val, Handle):
            return
        self.classify(cur, val)

    @staticmethod
    def _op(val):
        if isinstance(val, OriginVal):
            return val, ()
        return val.origin, val.path

    @staticmethod
    def _ev(val):
        return val.evidence

    def classify(self, cur, val):
        origin, path = self._op(val)
        conf, ev = val.confidence, val.evidence
        eff, p = parent_skipping(cur)
        if p is None:
            self.emit_use(origin, path, conf, ev, eff, "read")
            return
        t = p.type
        if t == "expression_statement":
            return  # value discarded
        if t == "binary_operator":
            op = _text(p.child_by_field_name("operator"))
            other = p.child_by_field_name("right") if p.child_by_field_name("left") == eff else p.child_by_field_name("left")
            operand = self._operand(other)
            operation = {"/": "divide", "//": "divide", "*": "multiply"}.get(op, "read")
            self.emit_use(origin, path, conf, ev, p, operation, operand if operation != "read" else None)
            return
        if t == "comparison_operator":
            others = [c for c in p.named_children if c != eff]
            operand = self._operand(others[0]) if len(others) == 1 else None
            self.emit_use(origin, path, conf, ev, p, "compare", operand)
            return
        if t == "assignment" and p.child_by_field_name("right") == eff:
            left = p.child_by_field_name("left")
            self.emit_use(origin, path, conf, ev, p, "store", None, _text(left)[:80])
            return
        if t == "keyword_argument":
            call = p.parent.parent if p.parent is not None else None
            kw = _text(p.child_by_field_name("name"))
            if call is not None and call.type == "call":
                callee = _text(call.child_by_field_name("function"))
                if self.try_follow(callee, None, kw, val, call):
                    return
                operation = "store" if callee.split(".")[-1] in STORE_CALL_SUFFIXES else "pass_through"
                self.emit_use(origin, path, conf, ev, p, operation, None, f"{callee}({kw}=)")
                return
            self.emit_use(origin, path, conf, ev, p, "pass_through", None, kw)
            return
        if t == "argument_list":
            call = p.parent
            callee = _text(call.child_by_field_name("function"))
            idx = [c for c in p.named_children if c.type not in ("comment", "keyword_argument")].index(eff) \
                if eff in p.named_children else None
            if self.try_follow(callee, idx, None, val, call):
                return
            operation = "store" if callee.split(".")[-1] in STORE_CALL_SUFFIXES else "pass_through"
            self.emit_use(origin, path, conf, ev, call, operation, None, callee[:80])
            return
        if t == "pair" and p.child_by_field_name("value") == eff:
            key = string_literal(p.child_by_field_name("key"))
            self.emit_use(origin, path, conf, ev, p, "pass_through", None, key if key is not None else _text(p.child_by_field_name("key"))[:60])
            return
        if t == "return_statement":
            self.emit_use(origin, path, conf, ev, p, "pass_through", None, "return")
            return
        self.emit_use(origin, path, conf, ev, eff, "read")

    @staticmethod
    def _operand(n):
        n = unwrap(n)
        if n is None:
            return None
        if n.type in ("integer", "float"):
            return number_literal(_text(n))
        if n.type == "unary_operator" and _text(n).startswith("-"):
            v = number_literal(_text(n).replace(" ", ""))
            return v
        if n.type == "string":
            return string_literal(n)
        return None


class PythonAdapter:
    def __init__(self, ctx: FileContext):
        self.ctx = ctx
        self.tree = Parser(_LANG).parse(ctx.source)
        self.functions: dict = {}
        self.module_env: dict = {}

    def param_names(self, fn):
        params = fn.child_by_field_name("parameters")
        names = []
        for c in params.named_children if params is not None else []:
            if c.type == "identifier":
                names.append(_text(c))
            elif c.type in ("typed_parameter",):
                ident = next((x for x in c.named_children if x.type == "identifier"), None)
                if ident is None:
                    return names
                names.append(_text(ident))
            elif c.type in ("default_parameter", "typed_default_parameter"):
                names.append(_text(c.child_by_field_name("name")))
            elif c.type in ("list_splat_pattern", "dictionary_splat_pattern", "keyword_separator", "positional_separator"):
                continue
        return names

    def _all_functions(self, node, out):
        for c in node.named_children:
            if c.type in FUNC_TYPES:
                out.append(c)
            self._all_functions(c, out)

    def run(self) -> None:
        root = self.tree.root_node
        if root.has_error:
            self.ctx.warn("parse_error", _first_error_line(root), "tree-sitter recovered; results for this file may be partial")
        for c in root.named_children:
            fn = c
            if c.type == "decorated_definition":
                fn = c.child_by_field_name("definition")
            if fn is not None and fn.type in FUNC_TYPES:
                self.functions[_text(fn.child_by_field_name("name"))] = fn
        module = PyScope(self, self.module_env)
        for c in root.children:
            module.walk(c)
        module.finish()
        fns: list = []
        self._all_functions(root, fns)
        for fn in fns:
            self.analyze_function(fn, {}, 0, frozenset())

    def analyze_function(self, fn, bound: dict, depth: int, followed: frozenset) -> None:
        env = dict(self.module_env)
        scope = PyScope(self, env, depth, followed)
        for p in self.param_names(fn):
            if p in env:
                if isinstance(env[p], Sym):
                    self.ctx.warn("shadowed_symbol", fn.start_point.row + 1, f"parameter '{p}' shadows {env[p].name}")
                env.pop(p)
        env.update(bound)
        body = fn.child_by_field_name("body")
        if body is not None:
            for c in body.children:
                scope.walk(c)
        scope.finish()


def _first_error_line(node) -> int:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.is_error or n.is_missing:
            return n.start_point.row + 1
        stack.extend(reversed(n.children))
    return 1
