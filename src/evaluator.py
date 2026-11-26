"""Choose test inputs for the changed functions: deterministic fuzzing, optionally plus LLM proposals."""
from __future__ import annotations

import ast
import json
import os
import urllib.request

from .diff_parser import FileContext, Function
from .fuzzer import Case, mock_cases

ANTHROPIC_MODEL = "claude-opus-5-5"
SAFE_NODES = (ast.Expression, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set, ast.UnaryOp,
              ast.USub, ast.UAdd, ast.BinOp, ast.Mult, ast.Add, ast.Sub, ast.Load)
SAFE_CALLS = {"Decimal", "float"}


class LLMError(Exception):
    pass


def safe_expr(src: str) -> bool:
    """LLM output gets executed, so only literals, Decimal('...') and float('nan'/'inf') pass."""
    try:
        tree = ast.parse(src, mode="eval")
    except (SyntaxError, ValueError):
        return False
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            if not (isinstance(n.func, ast.Name) and n.func.id in SAFE_CALLS and not n.keywords
                    and len(n.args) == 1 and isinstance(n.args[0], ast.Constant)):
                return False
        elif isinstance(n, ast.Name):
            if n.id not in SAFE_CALLS:
                return False
        elif not isinstance(n, SAFE_NODES):
            return False
    return len(src) <= 500


PROMPT = """You are reviewing a Python pull request for regressions. For each function below you get
the code before and after the change. Propose up to 15 inputs per function that are most likely to make
the new version behave differently from the old one (boundaries, zero, negatives, None, empty values,
rounding edges, wrong types). Each argument must be a Python literal expression; the only calls allowed
are Decimal('...') and float('nan') / float('inf').

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
        if not all(isinstance(e, str) and safe_expr(e) for e in [*args, *kwargs.values()]):
            continue
        if not all(isinstance(name, str) and name.isidentifier() for name in kwargs):
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
