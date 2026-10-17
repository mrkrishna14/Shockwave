"""Shared scope machinery for the Tree-sitter adapters.

Analysis is intentionally narrow: a scope is one function (or the module
body). Statements are visited in source order; assignments rebind names, so a
later local ``stripe = ...`` or a parameter named ``stripe`` shadows the SDK.
Branches are not merged (last write wins) and closures are not followed. One
bounded form of interprocedural flow is supported: passing a tracked value as
a positional/keyword argument to a function defined at module top level in
the same file. Those findings are ``inferred`` and carry the call-site
evidence.
"""
from __future__ import annotations

from typing import Optional

from ..origins import FieldVal, FileContext, Finding, Handle, Loc, OriginVal, Sym, weaker

MAX_FOLLOW_DEPTH = 3


def field_path_str(path: tuple) -> str:
    return "response" + "".join(f".{p}" for p in path)


class ScopeBase:
    LANGUAGE = ""
    TRACKED_SYMBOL_NAMES: frozenset = frozenset()

    def __init__(self, adapter, env: dict, depth: int = 0, followed: frozenset = frozenset()):
        self.ad = adapter
        self.ctx: FileContext = adapter.ctx
        self.env = env
        self.depth = depth
        self.followed = followed
        self.skip: set[int] = set()
        self.memo: dict[int, object] = {}
        # name -> [FieldVal, Loc of alias assignment, used?]
        self.pending_alias: dict[str, list] = {}

    # ----- binding -----------------------------------------------------
    def bind(self, name: str, val, node) -> None:
        self._flush_alias(name)
        old = self.env.get(name)
        if isinstance(old, Sym) and not isinstance(val, Sym):
            self.ctx.warn("shadowed_symbol", node.start_point.row + 1, f"'{name}' no longer refers to {old.name}")
        if isinstance(val, OriginVal):
            val = OriginVal(
                val.endpoint, val.kind, val.library, val.call, val.evidence, val.confidence
            )
        if val is None:
            self.env.pop(name, None)
        else:
            self.env[name] = val
        if isinstance(val, FieldVal):
            self.pending_alias[name] = [val, self.ctx.loc(node, "alias"), False]

    def alias_of(self, val, node):
        """Value bound through a plain local alias (``b = a``) becomes inferred."""
        loc = self.ctx.loc(node, "alias")
        if isinstance(val, OriginVal):
            return OriginVal(val.endpoint, val.kind, val.library, val.call, val.evidence + (loc,), "inferred")
        if isinstance(val, FieldVal):
            return FieldVal(val.origin, val.path, val.evidence + (loc,), "inferred")
        return val

    def mark_used(self, name: str) -> None:
        if name in self.pending_alias:
            self.pending_alias[name][2] = True

    def _flush_alias(self, name: str) -> None:
        entry = self.pending_alias.pop(name, None)
        if entry and not entry[2]:
            fv, loc, _ = entry
            self.emit(fv.origin, fv.path, "read", None, None, fv.confidence, fv.evidence + (loc,))

    def finish(self) -> None:
        for name in list(self.pending_alias):
            self._flush_alias(name)

    def clear(self, name: str, node) -> None:
        self.bind(name, None, node)

    # ----- emission ----------------------------------------------------
    def emit(self, origin: OriginVal, path, operation, operand, destination, confidence, evidence) -> None:
        self.ctx.findings.append(
            Finding(
                origin=origin,
                path=tuple(path),
                operation=operation,
                operand=operand,
                destination=destination,
                confidence=confidence,
                evidence=tuple(evidence),
                language=self.ctx.language,
            )
        )

    def emit_use(self, origin, path, conf, ev, use_node, operation, operand=None, destination=None):
        use = self.ctx.loc(use_node, "use")
        if not path:
            # The whole response escapes somewhere we do not follow.
            self.emit(origin, (), operation, operand, destination, "unresolved", ev + (use,))
            self.ctx.gap("origin_escape", use.line, f"whole response used as {operation}" + (f" -> {destination}" if destination else ""))
            return
        self.emit(origin, path, operation, operand, destination, conf, ev + (use,))

    # ----- bounded call following -------------------------------------
    def try_follow(self, callee_name: str, arg_index: Optional[int], kw_name: Optional[str], val, call_node) -> bool:
        fn = self.ad.functions.get(callee_name)
        if fn is None or self.depth >= MAX_FOLLOW_DEPTH:
            return False
        params = self.ad.param_names(fn)
        if params is None:
            return False
        if kw_name is not None:
            if kw_name not in params:
                return False
            pname = kw_name
        else:
            if arg_index is None or arg_index >= len(params):
                return False
            pname = params[arg_index]
        key = (fn.id, pname, id(val))
        if key in self.followed:
            return True
        site = self.ctx.loc(call_node, "call_site")
        if isinstance(val, OriginVal):
            bound = OriginVal(val.endpoint, val.kind, val.library, val.call, val.evidence + (site,), "inferred")
        elif isinstance(val, FieldVal):
            bound = FieldVal(val.origin, val.path, val.evidence + (site,), "inferred")
        elif isinstance(val, Handle):
            bound = Handle(val.endpoint, val.library, val.call, val.evidence + (site,))
        else:
            return False
        self.ad.analyze_function(fn, {pname: bound}, self.depth + 1, self.followed | {key})
        return True
