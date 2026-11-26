"""Turn changed functions into differential test cases (old vs new on identical inputs)."""
from __future__ import annotations

import ast
import itertools
import json
import math
import os
import random
import re
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal

from .diff_parser import FileContext, Function, Param

MAX_CASES = 300
ANTHROPIC_MODEL = "claude-opus-5-5"

# Normal value first: it is the baseline the one-at-a-time variations start from.
TYPED = {
    "int": ["1", "0", "-1", "2", "10**6"],
    "float": ["1.0", "0.0", "-1.0", "0.5", "1e9"],
    "Decimal": ["Decimal('19.99')", "Decimal('0')", "Decimal('-1')", "Decimal('1')", "Decimal('0.005')"],
    "str": ["'abc'", "''", "' '", "'A'"],
    "bool": ["True", "False"],
    "list": ["[1, 2]", "[]", "[0]", "[-1]"],
    "tuple": ["(1, 2)", "()", "(0,)"],
    "dict": ["{'a': 1}", "{}"],
}
UNTYPED = ["1", "0", "-1", "''", "[]"]
NUMERIC = {"int", "float", "Decimal"}


class LLMError(Exception):
    pass


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
    if kind in NUMERIC:  # boundary values around every number the code compares against
        for raw in fn.numbers[:6]:
            values += [v for d in (-1, 0, 1) if (v := _as_kind(kind, Decimal(raw) + d))]
    if kind in ("str", None):
        values += [repr(s) for s in fn.strings[:6]]
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


SAFE_NODES = (ast.Expression, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set, ast.UnaryOp,
              ast.USub, ast.UAdd, ast.BinOp, ast.Pow, ast.Mult, ast.Add, ast.Sub, ast.Load)


def safe_expr(src: str) -> bool:
    """LLM output gets executed, so only literals and Decimal('...') are allowed through."""
    try:
        tree = ast.parse(src, mode="eval")
    except (SyntaxError, ValueError):
        return False
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            if not (isinstance(n.func, ast.Name) and n.func.id == "Decimal" and not n.keywords
                    and len(n.args) == 1 and isinstance(n.args[0], ast.Constant)):
                return False
        elif isinstance(n, ast.Name):
            if n.id != "Decimal":
                return False
        elif not isinstance(n, SAFE_NODES):
            return False
    return len(src) <= 500


PROMPT = """You are reviewing a Python pull request for regressions. For each function below you get
the code before and after the change. Propose up to 15 inputs per function that are most likely to make
the new version behave differently from the old one (boundaries, zero, negatives, None, empty values,
rounding edges, wrong types). Each argument must be a Python literal expression; the only call allowed
is Decimal('...').

Reply with JSON only, in this shape:
{{"cases": [{{"function": "name", "args": ["expr", ...], "kwargs": {{"param": "expr"}}}}]}}

{functions}"""


def _ask_llm(prompt: str) -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("install the llm extra: pip install 'pr-sentinel[llm]'") from e
        resp = anthropic.Anthropic().beta.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            raise LLMError("the model declined the request")
        return "".join(b.text for b in resp.content if b.type == "text")
    if key := os.environ.get("OPENAI_API_KEY"):
        model = os.environ.get("OPENAI_MODEL")
        if not model:
            raise LLMError("set OPENAI_MODEL to the model name your OpenAI-compatible endpoint serves")
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(f"{base}/chat/completions", body,
                                     {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)["choices"][0]["message"]["content"]
    raise LLMError("--mode llm needs ANTHROPIC_API_KEY or OPENAI_API_KEY")


def parse_llm_cases(text: str, targets: dict[str, tuple[int, Function]]) -> list[Case]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise LLMError("model reply contained no JSON object")
    try:
        items = json.loads(text[start:end + 1]).get("cases", [])
    except (json.JSONDecodeError, AttributeError) as e:
        raise LLMError(f"model reply was not valid JSON: {e}") from e
    cases = []
    for k, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict) or item.get("function") not in targets:
            continue
        args, kwargs = item.get("args") or [], item.get("kwargs") or {}
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            continue
        exprs = [*args, *kwargs.values()]
        if not all(isinstance(e, str) and safe_expr(e) for e in exprs):
            continue
        if not all(isinstance(k2, str) and k2.isidentifier() for k2 in kwargs):
            continue
        file_index, fn = targets[item["function"]]
        cases.append(Case(f"{file_index}-{fn.name}-llm{k}", file_index, fn.name, args, kwargs, "llm"))
    return cases


def generate(contexts: list[FileContext], mode: str = "mock") -> list[Case]:
    cases = [c for i, ctx in enumerate(contexts) for fn in ctx.functions for c in mock_cases(i, fn)]
    if mode == "llm":
        targets = {fn.name: (i, fn) for i, ctx in enumerate(contexts) for fn in ctx.functions}
        if targets:
            listing = "\n\n".join(
                f"### {fn.name}  (file {contexts[i].path})\n--- before\n{fn.old_code}\n--- after\n{fn.new_code}"
                for i, fn in targets.values())
            cases += parse_llm_cases(_ask_llm(PROMPT.format(functions=listing)), targets)
    return cases
