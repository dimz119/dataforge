"""Evaluator for the closed ``derived.expr`` grammar (plugin-architecture §4.5).

The static validator (``dataforge_engine.manifest.expr``) proves well-formedness
at publish; this module compiles a valid expression to an AST once (at IR compile)
and evaluates it against a resolved context per generation. The grammar::

    expr   := term (('+'|'-') term)*
    term   := factor (('*'|'/') factor)*
    factor := NUMBER | path | func | '(' expr ')'
    func   := ('round'|'min'|'max'|'sum'|'count') '(' args ')'
    path   := contextPath    # actor.x, subject.y, session.cart_items[].unit_price

``sum``/``count`` take exactly one ``session.<key>[].<field>`` list path. Division
by a value that realizes to zero is a :class:`GenerationError` (MAN-D603 analogue:
the event is a bug, never emitted). All arithmetic uses :class:`~decimal.Decimal`
so ``output: decimal`` is exact and money stays string-stable (S-6).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from .errors import GenerationError

if TYPE_CHECKING:
    from dataforge_engine.envelope.types import JSONValue

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<number>\d+(?:\.\d+)?)"
    r"|(?P<path>[a-z][a-z0-9_]*(?:\[\])?(?:\.[a-z][a-z0-9_]*(?:\[\])?)*)"
    r"|(?P<op>[+\-*/])|(?P<lparen>\()|(?P<rparen>\))|(?P<comma>,))\s*"
)

_FUNCS = frozenset({"round", "min", "max", "sum", "count"})
_LIST_FUNCS = frozenset({"sum", "count"})


class _Node:
    __slots__ = ()

    def eval(self, resolve: Callable[[str], JSONValue]) -> Decimal:  # pragma: no cover
        raise NotImplementedError


class _Num(_Node):
    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = Decimal(value)

    def eval(self, resolve: Callable[[str], JSONValue]) -> Decimal:
        return self.value


class _Path(_Node):
    __slots__ = ("path",)

    def __init__(self, path: str) -> None:
        self.path = path

    def eval(self, resolve: Callable[[str], JSONValue]) -> Decimal:
        return _to_decimal(resolve(self.path))


class _BinOp(_Node):
    __slots__ = ("left", "op", "right")

    def __init__(self, left: _Node, op: str, right: _Node) -> None:
        self.left = left
        self.op = op
        self.right = right

    def eval(self, resolve: Callable[[str], JSONValue]) -> Decimal:
        lhs = self.left.eval(resolve)
        rhs = self.right.eval(resolve)
        if self.op == "+":
            return lhs + rhs
        if self.op == "-":
            return lhs - rhs
        if self.op == "*":
            return lhs * rhs
        if rhs == 0:
            raise GenerationError("derived.expr division by zero (MAN-D603)")
        return lhs / rhs


class _Func(_Node):
    __slots__ = ("args", "list_path", "name")

    def __init__(self, name: str, args: list[_Node], list_path: str | None) -> None:
        self.name = name
        self.args = args
        self.list_path = list_path

    def eval(self, resolve: Callable[[str], JSONValue]) -> Decimal:
        if self.name in _LIST_FUNCS:
            assert self.list_path is not None
            raw = resolve(self.list_path)
            items = list(raw) if isinstance(raw, list) else []
            if self.name == "count":
                return Decimal(len(items))
            return sum((_to_decimal(item) for item in items), Decimal(0))
        values = [arg.eval(resolve) for arg in self.args]
        if self.name == "round":
            quant = Decimal(1) if len(values) < 2 else Decimal(10) ** -int(values[1])
            return values[0].quantize(quant)
        if self.name == "min":
            return min(values)
        return max(values)


def _to_decimal(value: JSONValue) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool ⊂ int — exclude before int
        return Decimal(1) if value else Decimal(0)
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except (ArithmeticError, ValueError) as exc:
            raise GenerationError(f"derived.expr non-numeric operand {value!r}") from exc
    raise GenerationError(f"derived.expr operand not numeric: {value!r}")


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.i = 0

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _advance(self) -> tuple[str, str]:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def parse(self) -> _Node:
        node = self.parse_expr()
        if self.i != len(self.tokens):
            raise GenerationError("derived.expr: trailing tokens")
        return node

    def parse_expr(self) -> _Node:
        node = self.parse_term()
        while (tok := self._peek()) is not None and tok[0] == "op" and tok[1] in "+-":
            self._advance()
            node = _BinOp(node, tok[1], self.parse_term())
        return node

    def parse_term(self) -> _Node:
        node = self.parse_factor()
        while (tok := self._peek()) is not None and tok[0] == "op" and tok[1] in "*/":
            self._advance()
            node = _BinOp(node, tok[1], self.parse_factor())
        return node

    def parse_factor(self) -> _Node:
        tok = self._peek()
        if tok is None:
            raise GenerationError("derived.expr ended unexpectedly")
        kind, value = tok
        if kind == "number":
            self._advance()
            return _Num(value)
        if kind == "lparen":
            self._advance()
            node = self.parse_expr()
            self._expect("rparen")
            return node
        if kind == "path":
            if value in _FUNCS:
                return self.parse_func(value)
            self._advance()
            return _Path(value)
        raise GenerationError("derived.expr: unexpected token")

    def parse_func(self, name: str) -> _Func:
        self._advance()
        self._expect("lparen")
        if name in _LIST_FUNCS:
            tok = self._peek()
            if tok is None or tok[0] != "path" or "[]" not in tok[1]:
                raise GenerationError(f"derived.expr: {name}() needs a list path")
            self._advance()
            self._expect("rparen")
            return _Func(name, [], tok[1])
        args = [self.parse_expr()]
        while (tok := self._peek()) is not None and tok[0] == "comma":
            self._advance()
            args.append(self.parse_expr())
        self._expect("rparen")
        return _Func(name, args, None)

    def _expect(self, kind: str) -> None:
        tok = self._peek()
        if tok is None or tok[0] != kind:
            raise GenerationError(f"derived.expr: expected {kind}")
        self._advance()


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expr):
        if expr[pos].isspace():
            pos += 1
            continue
        match = _TOKEN_RE.match(expr, pos)
        if match is None or match.start() == match.end():
            raise GenerationError("derived.expr: illegal token")
        pos = match.end()
        for kind in ("number", "path", "op", "lparen", "rparen", "comma"):
            v = match.group(kind)
            if v is not None:
                tokens.append((kind, v))
                break
    return tokens


class CompiledExpr:
    """A compiled ``derived.expr`` ready to evaluate against a resolved context."""

    __slots__ = ("_root",)

    def __init__(self, expr: str) -> None:
        self._root = _Parser(_tokenize(expr)).parse()

    def evaluate(self, resolve: Callable[[str], JSONValue]) -> Decimal:
        return self._root.eval(resolve)


def compile_expr(expr: str) -> CompiledExpr:
    return CompiledExpr(expr)


def evaluate(expr: str, context: Mapping[str, JSONValue]) -> Decimal:
    """Convenience: compile + evaluate against a flat ``{path: value}`` mapping."""
    return CompiledExpr(expr).evaluate(lambda p: context[p])
