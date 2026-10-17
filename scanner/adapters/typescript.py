"""JavaScript / TypeScript adapter (tree-sitter-javascript, tree-sitter-typescript).

Supported origins:
  * stripe-node: ``import Stripe from "stripe"``, ``import { Stripe } from "stripe"``,
    ``import * as S from "stripe"`` (``S.Stripe``/``S.default``),
    ``const Stripe = require("stripe")``, ``require("stripe")(key)``;
    client via ``new Stripe(key)`` or ``Stripe(key)``; mapped client methods
    such as ``stripe.paymentIntents.create(...)``.
  * global ``fetch(URL, {method})`` followed by ``.json()`` (awaited), where
    URL is a literal, template literal, ``+`` concatenation or string const.
    ``.then(...)`` chains are reported as unsupported.
Field reads: ``x.f``, ``x?.f``, ``x["f"]``, destructuring ``const {f, g: h} = x``.
"""
from __future__ import annotations

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser

from ..mappings import JS_SDK_CLIENT_METHODS, JS_SDK_PACKAGES, STORE_CALL_SUFFIXES
from ..origins import (
    UNKNOWN, Const, FieldVal, FileContext, Handle, OriginVal, Sym, number_literal, resolve_url,
)
from .base import ScopeBase

LANGS = {
    "javascript": Language(tsjs.language()),
    "typescript": Language(tsts.language_typescript()),
    "tsx": Language(tsts.language_tsx()),
}
UNWRAP = {
    "parenthesized_expression", "await_expression", "as_expression", "satisfies_expression",
    "non_null_expression", "type_assertion",
}
FUNC_TYPES = {
    "function_declaration", "generator_function_declaration", "function_expression",
    "function", "arrow_function", "method_definition", "generator_function",
}
SKIP_TYPES = {
    "type_alias_declaration", "interface_declaration", "type_annotation", "enum_declaration",
    "abstract_class_declaration", "comment",
}
COMPARE_OPS = {"<", ">", "<=", ">=", "==", "===", "!=", "!=="}


def _text(n) -> str:
    return n.text.decode("utf-8", "replace")


def unwrap(n):
    while n is not None and n.type in UNWRAP:
        inner = [c for c in n.named_children if c.type not in ("comment", "type_annotation") and not c.type.endswith("_type") and c.type != "type_identifier"]
        if not inner:
            break
        n = inner[0]
    return n


def parent_skipping(n):
    cur, p = n, n.parent
    while p is not None and p.type in UNWRAP and unwrap(p) is not None:
        cur, p = p, p.parent
    return cur, p


def string_literal(n):
    n = unwrap(n)
    if n is None:
        return None
    if n.type == "string":
        return "".join(_text(c) for c in n.named_children if c.type == "string_fragment")
    if n.type == "template_string" and not any(c.type == "template_substitution" for c in n.named_children):
        return "".join(_text(c) for c in n.named_children if c.type == "string_fragment")
    return None


def _prop_name(n):
    if n is None:
        return None
    if n.type in ("property_identifier", "identifier", "private_property_identifier"):
        return _text(n)
    s = string_literal(n)
    return s


class JsScope(ScopeBase):
    LANGUAGE = "javascript"

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
            return Const(("".join(_text(c) for c in n.named_children if c.type in ("string_fragment", "escape_sequence")),))
        if t == "template_string":
            parts: list = []
            for c in n.named_children:
                if c.type in ("string_fragment", "escape_sequence"):
                    parts.append(_text(c))
                elif c.type == "template_substitution":
                    inner = c.named_children[0] if c.named_children else None
                    v = self.eval(inner) if inner is not None else None
                    parts.extend(v.parts if isinstance(v, Const) else (UNKNOWN,))
            merged: list = []
            for p in parts:
                if merged and isinstance(p, str) and isinstance(merged[-1], str):
                    merged[-1] += p
                else:
                    merged.append(p)
            return Const(tuple(merged) if merged else ("",))
        if t == "binary_expression" and _text(n.child_by_field_name("operator")) == "+":
            l, r = self.eval(n.child_by_field_name("left")), self.eval(n.child_by_field_name("right"))
            if isinstance(l, Const) or isinstance(r, Const):
                lp = l.parts if isinstance(l, Const) else (UNKNOWN,)
                rp = r.parts if isinstance(r, Const) else (UNKNOWN,)
                return Const(lp + rp)
            return None
        if t == "member_expression":
            obj = self.eval(n.child_by_field_name("object"))
            prop = _prop_name(n.child_by_field_name("property"))
            if prop is None:
                return None
            if isinstance(obj, Sym):
                if obj.name == "stripe.ns" and prop in ("Stripe", "default"):
                    return Sym("stripe.ctor")
                return Sym(f"{obj.name}.{prop}")
            if isinstance(obj, (OriginVal, FieldVal)):
                return self._extend(obj, prop)
            return None
        if t == "subscript_expression":
            obj = self.eval(n.child_by_field_name("object"))
            if isinstance(obj, (OriginVal, FieldVal)):
                key = string_literal(n.child_by_field_name("index"))
                if key is not None:
                    return self._extend(obj, key)
            return None
        if t == "new_expression":
            ctor = self.eval(n.child_by_field_name("constructor"))
            if isinstance(ctor, Sym) and ctor.name == "stripe.ctor":
                return Sym("stripe.client")
            return None
        if t == "call_expression":
            return self.eval_call(n)
        return None

    @staticmethod
    def _extend(obj, key):
        if isinstance(obj, OriginVal):
            return FieldVal(obj, (key,), obj.evidence, obj.confidence)
        return FieldVal(obj.origin, obj.path + (key,), obj.evidence, obj.confidence)

    def _args(self, call):
        args = call.child_by_field_name("arguments")
        if args is None or args.type != "arguments":
            return []
        return [c if c.type != "spread_element" else None for c in args.named_children if c.type != "comment"]

    def eval_call(self, call):
        fn = unwrap(call.child_by_field_name("function"))
        line = call.start_point.row + 1
        if fn is None:
            return None
        if fn.type == "identifier" and _text(fn) == "require" and "require" not in self.env:
            args = self._args(call)
            mod = string_literal(args[0]) if args and args[0] is not None else None
            if mod in JS_SDK_PACKAGES:
                return Sym("stripe.ctor")
            return None
        if fn.type == "member_expression":
            prop = _prop_name(fn.child_by_field_name("property"))
            obj = self.eval(fn.child_by_field_name("object"))
            if isinstance(obj, Handle):
                if prop == "json":
                    conf = "inferred" if any(l.label in ("alias", "call_site") for l in obj.evidence) else "direct"
                    return OriginVal(obj.endpoint, "http_response", obj.library, obj.call,
                                     obj.evidence + (self.ctx.loc(call, "json"),), conf)
                if prop == "then":
                    self.ctx.gap("promise_then_chain", line, "use await to make response origin visible")
                return None
        callee = self.eval(fn)
        if not isinstance(callee, Sym):
            return None
        if callee.name == "stripe.ctor":
            return Sym("stripe.client")
        if callee.name.startswith("stripe.client."):
            method = callee.name[len("stripe.client."):]
            op = JS_SDK_CLIENT_METHODS.get(method)
            if op is None:
                if self.ctx.service_id == "stripe":
                    self.ctx.gap("unmapped_sdk_method", line, f"stripe.{method}")
                return None
            if op.service_id != self.ctx.service_id:
                return None
            ep = self.ctx.catalog.by_template(op.service_id, op.method, op.path)
            if ep is None:
                self.ctx.warn("endpoint_not_in_catalog", line, f"{op.call} -> {op.method} {op.path}")
                return None
            return OriginVal(ep, "sdk_response", op.library, op.call, (self.ctx.loc(call, "sdk_call"),))
        if callee.name == "fetch":
            args = self._args(call)
            if not args or args[0] is None:
                self.ctx.gap("dynamic_url", line, "fetch without literal URL argument")
                return None
            method = "GET"
            if len(args) > 1 and args[1] is not None:
                opts = unwrap(args[1])
                if opts.type == "object":
                    for pair in opts.named_children:
                        if pair.type == "pair" and _prop_name(pair.child_by_field_name("key")) == "method":
                            m = string_literal(pair.child_by_field_name("value"))
                            if m is None:
                                self.ctx.gap("dynamic_http_method", line, "fetch")
                                return None
                            method = m.upper()
                else:
                    self.ctx.gap("dynamic_http_method", line, "fetch options not an object literal")
                    return None
            v = self.eval(args[0])
            parts = v.parts if isinstance(v, Const) else (UNKNOWN,)
            res = resolve_url(parts, method, self.ctx.service_id, self.ctx.base_urls, self.ctx.catalog)
            if res.status == "matched":
                return Handle(res.endpoint, "fetch", "fetch", (self.ctx.loc(call, "http_call"),))
            if res.status == "dynamic":
                self.ctx.gap("dynamic_url", line, f"fetch: {res.detail}")
            elif res.status in ("unmatched", "ambiguous"):
                self.ctx.warn(f"endpoint_{res.status}", line, res.detail)
            return None
        return None

    # ----- traversal ---------------------------------------------------
    def walk(self, n):
        t = n.type
        if t in SKIP_TYPES:
            return
        if t in FUNC_TYPES:
            name = n.child_by_field_name("name")
            if t in ("function_declaration", "generator_function_declaration") and name is not None and _text(name) in self.env:
                self.clear(_text(name), n)
            return
        if t in ("class_declaration", "class"):
            name = n.child_by_field_name("name")
            if name is not None and _text(name) in self.env:
                self.clear(_text(name), n)
            return
        if t == "import_statement":
            self.handle_import(n)
            return
        if t == "variable_declarator":
            self.handle_declarator(n)
            return
        if t == "assignment_expression":
            self.handle_assignment(n)
            return
        if t == "augmented_assignment_expression":
            self.walk(n.child_by_field_name("right"))
            left = n.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                self.clear(_text(left), n)
            return
        if t in ("for_in_statement",):
            self.walk(n.child_by_field_name("right"))
            self._clear_pattern(n.child_by_field_name("left"), n)
            body = n.child_by_field_name("body")
            if body is not None:
                self.walk(body)
            return
        if t == "catch_clause":
            self._clear_pattern(n.child_by_field_name("parameter"), n)
            body = n.child_by_field_name("body")
            if body is not None:
                self.walk(body)
            return
        if t == "member_expression":
            self.walk(n.child_by_field_name("object"))
            return
        if t == "pair":
            v = n.child_by_field_name("value")
            if v is not None:
                self.walk(v)
            k = n.child_by_field_name("key")
            if k is not None and k.type == "computed_property_name":
                self.walk(k)
            return
        if t in ("identifier", "shorthand_property_identifier"):
            val = self.env.get(_text(n))
            if isinstance(val, (OriginVal, FieldVal, Handle)):
                self.mark_used(_text(n))
                self.on_value_use(n, val)
            return
        if t == "call_expression":
            val = self.eval(n)
            if isinstance(val, (OriginVal, FieldVal, Handle)):
                self.on_value_use(n, val)
                args = n.child_by_field_name("arguments")
                if args is not None:
                    self.walk(args)
                return
        for c in n.children:
            self.walk(c)

    def _clear_pattern(self, pat, node):
        if pat is None:
            return
        if pat.type in ("identifier", "shorthand_property_identifier_pattern"):
            self.clear(_text(pat), node)
            return
        for c in pat.named_children:
            if c.type == "pair_pattern":
                self._clear_pattern(c.child_by_field_name("value"), node)
            elif c.type not in ("property_identifier",):
                self._clear_pattern(c, node)

    def handle_import(self, n):
        src = n.child_by_field_name("source")
        mod = string_literal(src) if src is not None else None
        clause = next((c for c in n.named_children if c.type == "import_clause"), None)
        if clause is None:
            return
        tracked = mod in JS_SDK_PACKAGES
        for c in clause.named_children:
            if c.type == "identifier":  # default import
                if tracked:
                    self.env[_text(c)] = Sym("stripe.ctor")
                elif _text(c) in self.env:
                    self.clear(_text(c), n)
            elif c.type == "namespace_import":
                ident = next((x for x in c.named_children if x.type == "identifier"), None)
                if ident is not None:
                    if tracked:
                        self.env[_text(ident)] = Sym("stripe.ns")
                    elif _text(ident) in self.env:
                        self.clear(_text(ident), n)
            elif c.type == "named_imports":
                for spec in c.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name = _text(spec.child_by_field_name("name"))
                    alias = spec.child_by_field_name("alias")
                    local = _text(alias) if alias is not None else name
                    if tracked and name in ("Stripe", "default"):
                        self.env[local] = Sym("stripe.ctor")
                    elif local in self.env:
                        self.clear(local, n)

    def handle_declarator(self, n):
        name = n.child_by_field_name("name")
        value = n.child_by_field_name("value")
        self._bind_target(name, value, n)

    def handle_assignment(self, n):
        left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
        if left is not None and left.type in ("identifier", "object_pattern", "array_pattern"):
            self._bind_target(left, right, n)
        else:
            if right is not None:
                self.walk(right)
            if left is not None:
                self.walk(left)

    def _bind_target(self, target, value, n):
        if target is None:
            return
        if value is None:
            self._clear_pattern(target, n)
            return
        val = self.eval(value)
        u = unwrap(value)
        tracked = isinstance(val, (OriginVal, FieldVal, Handle))
        if tracked and target.type in ("identifier", "object_pattern"):
            self.skip.add(u.id)
            if u.type == "identifier":
                self.mark_used(_text(u))
        self.walk(value)
        self.skip.discard(u.id)
        if target.type == "identifier":
            if tracked and (u.type == "identifier" or isinstance(val, FieldVal)):
                val = self.alias_of(val, n)
            self.bind(_text(target), val, n)
        elif target.type == "object_pattern":
            if not isinstance(val, (OriginVal, FieldVal)):
                self._clear_pattern(target, n)
                return
            for c in target.named_children:
                if c.type == "shorthand_property_identifier_pattern":
                    self.bind(_text(c), self.alias_of(self._extend(val, _text(c)), n), n)
                elif c.type == "pair_pattern":
                    key = _prop_name(c.child_by_field_name("key"))
                    v = c.child_by_field_name("value")
                    if key is not None and v is not None and v.type == "identifier":
                        self.bind(_text(v), self.alias_of(self._extend(val, key), n), n)
                    else:
                        self.ctx.gap("unsupported_destructuring", c.start_point.row + 1, _text(c)[:60])
                        self._clear_pattern(v, n)
                elif c.type == "object_assignment_pattern":
                    left = c.child_by_field_name("left")
                    if left is not None and left.type == "shorthand_property_identifier_pattern":
                        self.bind(_text(left), self.alias_of(self._extend(val, _text(left)), n), n)
                    else:
                        self.ctx.gap("unsupported_destructuring", c.start_point.row + 1, _text(c)[:60])
                else:
                    self.ctx.gap("unsupported_destructuring", c.start_point.row + 1, _text(c)[:60])
                    self._clear_pattern(c, n)
        else:
            if tracked:
                self.ctx.gap("unsupported_destructuring", n.start_point.row + 1, _text(target)[:60])
            self._clear_pattern(target, n)

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
            if p.type == "member_expression" and p.child_by_field_name("object") == eff:
                prop = _prop_name(p.child_by_field_name("property"))
                gp_eff, gp = parent_skipping(p)
                is_call = gp is not None and gp.type == "call_expression" and gp.child_by_field_name("function") == gp_eff
                if is_call:
                    if isinstance(val, Handle) and prop == "json":
                        nv = self.eval(gp)
                        if nv is None:
                            return
                        val, cur = nv, gp
                        continue
                    if isinstance(val, (Handle, OriginVal)):
                        return  # method on response object, e.g. res.text(); not a field
                    cur = eff
                    break  # method on a field value, e.g. amount.toFixed(2): a read
                if isinstance(val, Handle) or prop is None:
                    return
                val = self._extend(val, prop)
                cur = p
                continue
            if p.type == "subscript_expression" and p.child_by_field_name("object") == eff:
                if isinstance(val, Handle):
                    return
                idx = p.child_by_field_name("index")
                key = string_literal(idx)
                if key is None:
                    ui = unwrap(idx)
                    if ui is not None and ui.type == "number":
                        val = self._extend(val, "[]")
                        cur = p
                        continue
                    origin, path = (val, ()) if isinstance(val, OriginVal) else (val.origin, val.path)
                    self.ctx.gap("dynamic_field_access", p.start_point.row + 1, _text(p)[:60])
                    self.emit(origin, path + ("*",), "unknown", None, None, "unresolved",
                              val.evidence + (self.ctx.loc(p, "use"),))
                    return
                val = self._extend(val, key)
                cur = p
                continue
            cur = eff
            break
        if cur.id in self.skip or isinstance(val, Handle):
            return
        self.classify(cur, val)

    def classify(self, cur, val):
        origin, path = (val, ()) if isinstance(val, OriginVal) else (val.origin, val.path)
        conf, ev = val.confidence, val.evidence
        if cur.type == "shorthand_property_identifier":
            self.emit_use(origin, path, conf, ev, cur, "pass_through", None, _text(cur))
            return
        eff, p = parent_skipping(cur)
        if p is None:
            self.emit_use(origin, path, conf, ev, eff, "read")
            return
        t = p.type
        if t == "expression_statement":
            return  # value discarded
        if t == "binary_expression":
            op = _text(p.child_by_field_name("operator"))
            other = p.child_by_field_name("right") if p.child_by_field_name("left") == eff else p.child_by_field_name("left")
            operand = self._operand(other)
            if op in COMPARE_OPS:
                self.emit_use(origin, path, conf, ev, p, "compare", operand)
                return
            operation = {"/": "divide", "*": "multiply"}.get(op, "read")
            self.emit_use(origin, path, conf, ev, p, operation, operand if operation != "read" else None)
            return
        if t == "assignment_expression" and p.child_by_field_name("right") == eff:
            self.emit_use(origin, path, conf, ev, p, "store", None, _text(p.child_by_field_name("left"))[:80])
            return
        if t == "variable_declarator" and p.child_by_field_name("value") == eff:
            self.emit_use(origin, path, conf, ev, p, "read")
            return
        if t == "arguments":
            call = p.parent
            fn = call.child_by_field_name("function")
            callee = _text(fn) if fn is not None else "?"
            positional = [c for c in p.named_children if c.type != "comment"]
            idx = positional.index(eff) if eff in positional else None
            if call.type == "call_expression" and self.try_follow(callee, idx, None, val, call):
                return
            operation = "store" if callee.split(".")[-1] in STORE_CALL_SUFFIXES else "pass_through"
            self.emit_use(origin, path, conf, ev, call, operation, None, callee[:80])
            return
        if t == "pair" and p.child_by_field_name("value") == eff:
            key = _prop_name(p.child_by_field_name("key"))
            self.emit_use(origin, path, conf, ev, p, "pass_through", None, key or _text(p.child_by_field_name("key"))[:60])
            return
        if t == "return_statement" or (t == "arrow_function" and p.child_by_field_name("body") == eff):
            self.emit_use(origin, path, conf, ev, p if t == "return_statement" else eff, "pass_through", None, "return")
            return
        self.emit_use(origin, path, conf, ev, eff, "read")

    @staticmethod
    def _operand(n):
        n = unwrap(n)
        if n is None:
            return None
        if n.type == "number":
            return number_literal(_text(n))
        if n.type == "unary_expression" and _text(n).startswith("-"):
            return number_literal(_text(n).replace(" ", ""))
        s = string_literal(n)
        return s


class TypeScriptAdapter:
    """Handles .js/.jsx/.mjs/.cjs (javascript grammar), .ts/.mts/.cts and .tsx."""

    def __init__(self, ctx: FileContext):
        self.ctx = ctx
        grammar = ctx.language
        if ctx.file.endswith(".tsx"):
            grammar = "tsx"
        self.tree = Parser(LANGS[grammar]).parse(ctx.source)
        self.functions: dict = {}
        self.module_env: dict = {"fetch": Sym("fetch")}

    def param_names(self, fn):
        params = fn.child_by_field_name("parameters")
        if params is None:
            single = fn.child_by_field_name("parameter")
            return [_text(single)] if single is not None and single.type == "identifier" else None
        names = []
        for c in params.named_children:
            node = c
            if c.type in ("required_parameter", "optional_parameter"):
                node = c.child_by_field_name("pattern")
            elif c.type == "assignment_pattern":
                node = c.child_by_field_name("left")
            if node is not None and node.type == "identifier":
                names.append(_text(node))
            elif c.type == "comment":
                continue
            else:
                names.append(f"\x00pattern{len(names)}")  # destructured param: not bindable
        return names

    def _param_identifiers(self, fn):
        out = []

        def collect(n):
            if n.type in ("identifier", "shorthand_property_identifier_pattern"):
                out.append(_text(n))
            for c in n.named_children:
                if c.type in ("type_annotation",) or c.type.endswith("_type"):
                    continue
                if c.type == "pair_pattern":
                    v = c.child_by_field_name("value")
                    if v is not None:
                        collect(v)
                    continue
                collect(c)

        params = fn.child_by_field_name("parameters") or fn.child_by_field_name("parameter")
        if params is not None:
            collect(params)
        return out

    def _all_functions(self, node, out):
        for c in node.named_children:
            if c.type in FUNC_TYPES:
                out.append(c)
            self._all_functions(c, out)

    def _register_top_level(self, root):
        for c in root.named_children:
            node = c
            if c.type == "export_statement":
                node = c.child_by_field_name("declaration") or next(
                    (x for x in c.named_children if x.type in FUNC_TYPES or x.type in ("lexical_declaration", "variable_declaration")), None)
                if node is None:
                    continue
            if node.type in ("function_declaration", "generator_function_declaration"):
                self.functions[_text(node.child_by_field_name("name"))] = node
            elif node.type in ("lexical_declaration", "variable_declaration"):
                for d in node.named_children:
                    if d.type == "variable_declarator":
                        v = d.child_by_field_name("value")
                        nm = d.child_by_field_name("name")
                        if v is not None and nm is not None and nm.type == "identifier" and unwrap(v).type in ("arrow_function", "function_expression", "function"):
                            self.functions[_text(nm)] = unwrap(v)

    def run(self) -> None:
        root = self.tree.root_node
        if root.has_error:
            from .python import _first_error_line
            self.ctx.warn("parse_error", _first_error_line(root), "tree-sitter recovered; results for this file may be partial")
        self._register_top_level(root)
        module = JsScope(self, self.module_env)
        for c in root.children:
            module.walk(c)
        module.finish()
        fns: list = []
        self._all_functions(root, fns)
        for fn in fns:
            self.analyze_function(fn, {}, 0, frozenset())

    def analyze_function(self, fn, bound: dict, depth: int, followed: frozenset) -> None:
        env = dict(self.module_env)
        scope = JsScope(self, env, depth, followed)
        for p in self._param_identifiers(fn):
            if p in env:
                if isinstance(env[p], Sym):
                    self.ctx.warn("shadowed_symbol", fn.start_point.row + 1, f"parameter '{p}' shadows {env[p].name}")
                env.pop(p)
        env.update(bound)
        body = fn.child_by_field_name("body")
        if body is None:
            return
        if body.type == "statement_block" or body.type == "class_body":
            for c in body.children:
                scope.walk(c)
        else:  # arrow function expression body
            scope.walk(body)
            # an expression body is returned
        scope.finish()
