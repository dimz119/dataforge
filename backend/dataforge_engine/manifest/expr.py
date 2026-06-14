"""A validator for the closed ``derived.expr`` grammar (§4.5).

The grammar (no variables, conditionals, loops, strings, or composition beyond
depth 4)::

    expr   := term (('+'|'-') term)*
    term   := factor (('*'|'/') factor)*
    factor := NUMBER | path | func | '(' expr ')'
    func   := ('round'|'min'|'max'|'sum'|'count') '(' args ')'
    path   := contextPath    # actor.x, subject.y, session.cart_items[].unit_price

Bounds (B-10): ≤ 256 chars, ≤ 32 tokens, depth ≤ 4. ``sum``/``count`` accept only
``session.<key>[].<field>`` list paths. :func:`validate_expr` returns ``None`` when
valid, else a short, content-light reason string (the MAN-V406 message).

This is a *static* validator — it checks well-formedness, not realised division by
zero (that is a Phase-4 dry-run concern, MAN-D603). Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import re

MAX_EXPR_CHARS = 256
MAX_EXPR_TOKENS = 32
MAX_EXPR_DEPTH = 4

_FUNCS = frozenset({"round", "min", "max", "sum", "count"})
_LIST_FUNCS = frozenset({"sum", "count"})

# Tokeniser: numbers, identifiers/paths (with optional [] and dots), operators,
# parens, comma.
_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<number>\d+(?:\.\d+)?)
      | (?P<path>[a-z][a-z0-9_]*(?:\[\])?(?:\.[a-z][a-z0-9_]*(?:\[\])?)*)
      | (?P<op>[+\-*/])
      | (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<comma>,)
    )\s*
    """,
    re.VERBOSE,
)


class _ExprError(Exception):
    pass


def validate_expr(expr: str) -> str | None:
    """Return ``None`` if ``expr`` is valid per §4.5, else a short reason string."""
    if len(expr) > MAX_EXPR_CHARS:
        return f"expression exceeds {MAX_EXPR_CHARS} characters"
    try:
        tokens = _tokenize(expr)
    except _ExprError as exc:
        return str(exc)
    if len(tokens) > MAX_EXPR_TOKENS:
        return f"expression exceeds {MAX_EXPR_TOKENS} tokens"
    parser = _Parser(tokens)
    try:
        parser.parse_expr(depth=1)
        parser.expect_end()
    except _ExprError as exc:
        return str(exc)
    return None


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expr):
        if expr[pos].isspace():
            pos += 1
            continue
        match = _TOKEN_RE.match(expr, pos)
        if match is None or match.start() == match.end():
            raise _ExprError("expression contains an illegal token")
        pos = match.end()
        for kind in ("number", "path", "op", "lparen", "rparen", "comma"):
            value = match.group(kind)
            if value is not None:
                tokens.append((kind, value))
                break
    return tokens


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

    def expect_end(self) -> None:
        if self.i != len(self.tokens):
            raise _ExprError("trailing tokens after a complete expression")

    def parse_expr(self, depth: int) -> None:
        if depth > MAX_EXPR_DEPTH:
            raise _ExprError(f"expression nesting exceeds depth {MAX_EXPR_DEPTH}")
        self.parse_term(depth)
        while True:
            token = self._peek()
            if token is not None and token[0] == "op" and token[1] in "+-":
                self._advance()
                self.parse_term(depth)
            else:
                break

    def parse_term(self, depth: int) -> None:
        self.parse_factor(depth)
        while True:
            token = self._peek()
            if token is not None and token[0] == "op" and token[1] in "*/":
                self._advance()
                self.parse_factor(depth)
            else:
                break

    def parse_factor(self, depth: int) -> None:
        token = self._peek()
        if token is None:
            raise _ExprError("expression ended unexpectedly")
        kind, value = token
        if kind == "number":
            self._advance()
            return
        if kind == "lparen":
            self._advance()
            self.parse_expr(depth + 1)
            self._expect("rparen", ")")
            return
        if kind == "path":
            if value in _FUNCS:
                self.parse_func(value, depth)
            else:
                self._advance()
            return
        raise _ExprError("unexpected token in expression factor")

    def parse_func(self, name: str, depth: int) -> None:
        self._advance()  # function name
        self._expect("lparen", "(")
        self._parse_args(name, depth + 1)
        self._expect("rparen", ")")

    def _parse_args(self, func_name: str, depth: int) -> None:
        # sum/count take exactly one list path argument; others take 1+ exprs.
        if func_name in _LIST_FUNCS:
            token = self._peek()
            if token is None or token[0] != "path" or "[]" not in token[1]:
                raise _ExprError(f"{func_name}() requires a session list path argument")
            self._advance()
            return
        self.parse_expr(depth)
        while True:
            token = self._peek()
            if token is not None and token[0] == "comma":
                self._advance()
                self.parse_expr(depth)
            else:
                break

    def _expect(self, kind: str, literal: str) -> None:
        token = self._peek()
        if token is None or token[0] != kind:
            raise _ExprError(f"expected '{literal}' in expression")
        self._advance()
