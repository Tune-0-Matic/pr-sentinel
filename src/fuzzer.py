"""Deterministic input generation from a function's annotations and AST, plus input shrinking."""
from __future__ import annotations

import itertools
import math
import random
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from .diff_parser import Function, Param

MAX_CASES = 300
SHRINK_BUDGET = 200
EVAL_NAMES = {"Decimal": Decimal, "float": float, "list": list, "range": range, "__builtins__": {}}

# Normal value first: it is the baseline the one-at-a-time variations start from.
TYPED = {
    "int": ["1", "0", "-1", "2", "10**6", "-10**6"],
    "float": ["1.0", "0.0", "-1.0", "0.5", "1e9", "float('nan')", "float('inf')"],
    "Decimal": ["Decimal('19.99')", "Decimal('0')", "Decimal('-1')", "Decimal('1')", "Decimal('0.005')",
                "Decimal('Infinity')"],
    "str": ["'abc'", "''", "' '", "'A'", "'1.5'", "'x' * 10000"],
    "bool": ["True", "False"],
    "list": ["[1, 2]", "[]", "[0]", "[-1]", "[None]", "list(range(500))"],
    "tuple": ["(1, 2)", "()", "(0,)"],
    "dict": ["{'a': 1}", "{}", "{'a': {'b': {'c': {}}}}"],
}
UNTYPED = ["1", "0", "-1", "''", "[]", "float('nan')"]
NUMERIC = {"int", "float", "Decimal"}
SIZED = {"list": "[0] * {}", "tuple": "(0,) * {}", "str": "'x' * {}"}


@dataclass
class Case:
    id: str
    file_index: int
    function: str
    args: list[str]
    kwargs: dict[str, str] = field(default_factory=dict)
    source: str = "mock"

    @property
    def call(self) -> str:
        parts = self.args + [f"{k}={v}" for k, v in self.kwargs.items()]
        return f"{self.function}({', '.join(parts)})"


def kind_of(annotation: str | None) -> str | None:
    for word in re.findall(r"\w+", annotation or ""):
        if word in TYPED:
            return word
    return None


def _as_kind(kind: str, value: Decimal) -> str | None:
    if kind == "int":
        return str(int(value)) if value == value.to_integral_value() else None
    if kind == "float":
        return repr(float(value))
    return f"Decimal('{value}')"


def candidates(p: Param, fn: Function) -> list[str]:
    kind = kind_of(p.annotation)
    values = list(TYPED.get(kind, UNTYPED))
    if kind in NUMERIC:  # n-1, n, n+1 around every number the code compares against
        for raw in fn.numbers[:6]:
            values += [v for d in (-1, 0, 1) if (v := _as_kind(kind, Decimal(raw) + d))]
    if kind in SIZED:  # sizes either side of every len() comparison
        values += [SIZED[kind].format(n + d) for n in fn.lengths[:3] for d in (-1, 0, 1) if n + d >= 0]
    if kind in ("str", None):
        values += [repr(v) for s in fn.strings[:6] for v in (s, s.lower())]
    if kind is None:
        values += fn.numbers[:6]
    if p.default:
        values.append(p.default)
    values += ["None", "0" if kind == "str" else "'x'"]  # null input + type mismatch
    return list(dict.fromkeys(values))


def combos(cands: list[list[str]], cap: int = MAX_CASES) -> list[tuple[str, ...]]:
    if math.prod(len(c) for c in cands) <= cap:
        return list(itertools.product(*cands))
    # Vary one parameter at a time from the baseline, then fill with seeded random combinations.
    seen = dict.fromkeys(tuple(v if j == i else c[0] for j, c in enumerate(cands))
                         for i, cs in enumerate(cands) for v in cs)
    rng = random.Random(0)
    while len(seen) < cap:
        seen[tuple(rng.choice(c) for c in cands)] = None
    return list(seen)[:cap]


def mock_cases(file_index: int, fn: Function) -> list[Case]:
    cases = []
    for k, combo in enumerate(combos([candidates(p, fn) for p in fn.params])):
        args = [v for p, v in zip(fn.params, combo) if not p.keyword_only]
        kwargs = {p.name: v for p, v in zip(fn.params, combo) if p.keyword_only}
        cases.append(Case(f"{file_index}-{fn.name}-{k}", file_index, fn.name, args, kwargs))
    return cases


def evaluate(src: str):
    return eval(src, dict(EVAL_NAMES))  # only our own expressions or ones that passed evaluator.safe_expr


def to_src(v) -> str:
    if isinstance(v, float) and not math.isfinite(v):
        return f"float('{v}')"
    if isinstance(v, list):
        return "[" + ", ".join(map(to_src, v)) + "]"
    if isinstance(v, tuple):
        return "(" + ", ".join(map(to_src, v)) + ("," if len(v) == 1 else "") + ")"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{to_src(k)}: {to_src(x)}" for k, x in v.items()) + "}"
    return repr(v)


def size(v) -> float:
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, (int, float, Decimal)):
        try:
            return float(min(abs(v), 10**9)) if v == v else 1.0
        except ArithmeticError:  # Decimal NaN refuses comparisons
            return 1.0
    if isinstance(v, str):
        return len(v)
    if isinstance(v, (list, tuple)):
        return 1 + len(v) + sum(size(x) for x in v)
    if isinstance(v, dict):
        return 1 + len(v) + sum(size(k) + size(x) for k, x in v.items())
    return 1


def simpler(v):
    """Candidate replacements for v, roughly from most to least aggressive."""
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        kind = type(v)
        for make in (lambda: kind(0), lambda: kind(1), lambda: kind(int(v)), lambda: kind(int(v) // 2),
                     lambda: v - 1 if v > 0 else v + 1):
            try:
                yield make()
            except (ValueError, OverflowError, ArithmeticError):
                pass
    elif isinstance(v, str):
        yield from ("", v[:len(v) // 2], v[len(v) // 2:])
        if len(v) <= 8:
            yield from (v[:i] + v[i + 1:] for i in range(len(v)))
    elif isinstance(v, (list, tuple)):
        kind, half = type(v), len(v) // 2
        yield from (kind(), v[:half], v[half:])
        if len(v) <= 16:
            yield from (v[:i] + v[i + 1:] for i in range(len(v)))
            for i, x in enumerate(v):
                yield from (v[:i] + kind([c]) + v[i + 1:] for c in simpler(x))
    elif isinstance(v, dict):
        yield {}
        for k in v:
            yield {a: b for a, b in v.items() if a != k}
        for k, x in v.items():
            yield from ({**v, k: c} for c in simpler(x))


def shrink(values: list, still_fails: Callable[[list], bool], budget: int = SHRINK_BUDGET) -> list:
    """Greedy delta debugging: keep any strictly smaller argument that still reproduces the failure."""
    calls, improved = 0, True
    while improved and calls < budget:
        improved = False
        for i, v in enumerate(values):
            for c in simpler(v):
                if size(c) >= size(v):
                    continue
                calls += 1
                trial = values[:i] + [c] + values[i + 1:]
                if still_fails(trial):
                    values, improved = trial, True
                    break
                if calls >= budget:
                    return values
            if improved:
                break
    return values
