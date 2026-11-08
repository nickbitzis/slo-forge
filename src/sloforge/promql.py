"""A small PromQL parser for the subset sloforge generates, plus a rule file checker.

It is not a full PromQL implementation. It tokenizes, parses with operator precedence, and
checks types where it matters (rate() needs a range vector, a range vector cannot be compared).
CI also runs the real promtool on the same files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

AGGREGATIONS = {"sum", "avg", "min", "max", "count", "stddev", "stdvar", "group"}
RANGE_FUNCS = {"rate", "increase", "irate", "avg_over_time", "max_over_time", "min_over_time",
               "sum_over_time", "count_over_time"}
INSTANT_FUNCS = {"abs", "clamp_min", "clamp_max", "vector", "scalar", "absent",
                 "histogram_quantile", "round", "time"}
SET_OPS = {"and", "or", "unless"}
COMPARISONS = {"==", "!=", ">", "<", ">=", "<="}

TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<number>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
  | (?P<duration>\[\d+[smhdwy]\])
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<op>==|!=|>=|<=|=~|!~|[-+*/%><=(){},])
  | (?P<ident>[a-zA-Z_:][a-zA-Z0-9_:]*)
""", re.VERBOSE)


class PromQLError(ValueError):
    pass


@dataclass
class Tok:
    kind: str
    text: str
    pos: int


def tokenize(src: str) -> list[Tok]:
    out, pos = [], 0
    while pos < len(src):
        m = TOKEN_RE.match(src, pos)
        if not m:
            raise PromQLError(f"unexpected character {src[pos]!r} at {pos}")
        if m.lastgroup != "ws":
            out.append(Tok(m.lastgroup or "", m.group(), pos))
        pos = m.end()
    out.append(Tok("eof", "", pos))
    return out


class Parser:
    """Returns the expression type: 'scalar', 'instant', or 'range'."""

    def __init__(self, src: str) -> None:
        self.toks = tokenize(src)
        self.i = 0

    def peek(self) -> Tok:
        return self.toks[self.i]

    def until(self, close: str) -> bool:
        """True while the next token is not `close`; an early end is an unclosed bracket."""
        t = self.peek()
        if t.kind == "eof":
            raise PromQLError(f"expected {close!r} at {t.pos}, got end")
        return t.text != close

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, text: str) -> Tok:
        t = self.next()
        if t.text != text:
            raise PromQLError(f"expected {text!r} at {t.pos}, got {t.text or 'end'!r}")
        return t

    def parse(self) -> str:
        typ = self.or_expr()
        if self.peek().kind != "eof":
            t = self.peek()
            raise PromQLError(f"unexpected {t.text!r} at {t.pos}")
        return typ

    def _binary(self, sub, ops: set[str]) -> str:
        left = sub()
        while self.peek().text in ops:
            op = self.next().text
            if op in COMPARISONS and self.peek().text == "bool":
                self.next()
            right = sub()
            for side in (left, right):
                if side == "range":
                    raise PromQLError(f"range vector used with operator {op!r}")
            if op in SET_OPS and "scalar" in (left, right):
                raise PromQLError(f"set operator {op!r} needs vectors on both sides")
            left = "scalar" if left == right == "scalar" else "instant"
        return left

    def or_expr(self) -> str:
        return self._binary(self.and_expr, {"or", "unless"})

    def and_expr(self) -> str:
        return self._binary(self.cmp_expr, {"and"})

    def cmp_expr(self) -> str:
        return self._binary(self.add_expr, COMPARISONS)

    def add_expr(self) -> str:
        return self._binary(self.mul_expr, {"+", "-"})

    def mul_expr(self) -> str:
        return self._binary(self.unary, {"*", "/", "%"})

    def unary(self) -> str:
        if self.peek().text in ("-", "+"):
            self.next()
            return self.unary()
        return self.primary()

    def grouping(self) -> None:
        self.next()  # by / without
        self.expect("(")
        while self.until(")"):
            t = self.next()
            if t.kind != "ident":
                raise PromQLError(f"expected a label name at {t.pos}")
            if self.peek().text == ",":
                self.next()
        self.expect(")")

    def primary(self) -> str:
        t = self.peek()
        if t.kind == "number":
            self.next()
            return "scalar"
        if t.text == "(":
            self.next()
            typ = self.or_expr()
            self.expect(")")
            return typ
        if t.text == "{":
            return self.selector(None)
        if t.kind != "ident":
            raise PromQLError(f"unexpected {t.text or 'end'!r} at {t.pos}")
        self.next()
        name = t.text
        if name in AGGREGATIONS:
            if self.peek().text in ("by", "without"):
                self.grouping()
            self.expect("(")
            if self.or_expr() != "instant":
                raise PromQLError(f"{name}() needs an instant vector")
            self.expect(")")
            if self.peek().text in ("by", "without"):
                self.grouping()
            return "instant"
        if self.peek().text == "(" and (name in RANGE_FUNCS or name in INSTANT_FUNCS):
            self.next()
            args = []
            while self.until(")"):
                args.append(self.or_expr())
                if self.peek().text == ",":
                    self.next()
            self.expect(")")
            if name in RANGE_FUNCS and (len(args) != 1 or args[0] != "range"):
                raise PromQLError(f"{name}() needs one range vector argument")
            if name == "vector":
                return "instant"
            return "scalar" if name in ("scalar", "time") else "instant"
        if self.peek().text == "(":
            raise PromQLError(f"unknown function {name!r}")
        return self.selector(name)

    def selector(self, name: str | None) -> str:
        matchers = 0
        if self.peek().text == "{":
            self.next()
            while self.until("}"):
                label = self.next()
                if label.kind != "ident" or ":" in label.text:
                    raise PromQLError(f"expected a label name at {label.pos}")
                op = self.next()
                if op.text not in ("=", "!=", "=~", "!~"):
                    raise PromQLError(f"expected a matcher operator at {op.pos}")
                val = self.next()
                if val.kind != "string":
                    raise PromQLError(f"expected a quoted value at {val.pos}")
                if op.text in ("=~", "!~"):
                    try:
                        re.compile(val.text[1:-1])
                    except re.error as e:
                        raise PromQLError(f"bad regex {val.text}: {e}") from None
                matchers += 1
                if self.peek().text == ",":
                    self.next()
            self.expect("}")
        if name is None and matchers == 0:
            raise PromQLError("a selector needs a metric name or at least one matcher")
        if self.peek().kind == "duration":
            self.next()
            return "range"
        return "instant"


def parse(expr: str) -> str:
    return Parser(expr).parse()


RECORD_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
ALERT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
FOR_RE = re.compile(r"^\d+[smhdwy]$")


def check_rules(data: object) -> list[str]:
    """Structural checks in the spirit of `promtool check rules`. Returns error strings."""
    errs: list[str] = []
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        return ["top level must be a mapping with a 'groups' list"]
    names: set[str] = set()
    for gi, g in enumerate(data["groups"]):
        where = f"groups[{gi}]"
        if not isinstance(g, dict) or not g.get("name"):
            errs.append(f"{where}: group needs a name")
            continue
        if g["name"] in names:
            errs.append(f"{where}: duplicate group name {g['name']!r}")
        names.add(g["name"])
        for ri, r in enumerate(g.get("rules") or []):
            w = f"{where}.rules[{ri}]"
            if ("record" in r) == ("alert" in r):
                errs.append(f"{w}: exactly one of record or alert is required")
                continue
            if "record" in r and not RECORD_RE.match(str(r["record"])):
                errs.append(f"{w}: invalid recording rule name {r['record']!r}")
            if "alert" in r and not ALERT_RE.match(str(r["alert"])):
                errs.append(f"{w}: invalid alert name {r['alert']!r}")
            if "record" in r and ("for" in r or "annotations" in r):
                errs.append(f"{w}: recording rules cannot have for or annotations")
            if "for" in r and not FOR_RE.match(str(r["for"])):
                errs.append(f"{w}: invalid for duration {r['for']!r}")
            for k in (r.get("labels") or {}):
                if not LABEL_RE.match(k):
                    errs.append(f"{w}: invalid label name {k!r}")
            try:
                typ = parse(str(r.get("expr", "")))
                if typ == "range":
                    errs.append(f"{w}: expression returns a range vector")
            except PromQLError as e:
                errs.append(f"{w}: {e}")
    return errs
