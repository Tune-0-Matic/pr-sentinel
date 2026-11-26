"""Render a scan as Markdown for a pull request comment."""
from __future__ import annotations

from collections import defaultdict

from .diff_parser import FileContext
from .runner import CaseResult, RunResult

BADGES = {
    "fail": "![PR-Sentinel](https://img.shields.io/badge/PR--Sentinel-regression-red)",
    "review": "![PR-Sentinel](https://img.shields.io/badge/PR--Sentinel-review-yellow)",
    "pass": "![PR-Sentinel](https://img.shields.io/badge/PR--Sentinel-pass-brightgreen)",
    "error": "![PR-Sentinel](https://img.shields.io/badge/PR--Sentinel-error-lightgrey)",
}
HEADLINES = {
    "fail": "Regression found: changed code now crashes on inputs the old code handled.",
    "review": "Behaviour changed for some inputs. Confirm the changes below are intended.",
    "pass": "No behaviour differences found between the old and new code.",
    "error": "The generated suite could not run.",
}
EXAMPLES_PER_GROUP = 3


def risk(contexts: list[FileContext]) -> tuple[str, float]:
    """Diff complexity only: lines touched, functions changed and their branchiness."""
    changed = [f for c in contexts for f in c.functions if f.reason == "changed"]
    lines = sum(c.lines_changed for c in contexts)
    score = lines / 20 + len(changed) + sum(f.complexity for f in changed) / 5
    return ("Low" if score < 3 else "Medium" if score < 8 else "High"), round(score, 1)


def status(run: RunResult) -> str:
    if run.error or run.timed_out:
        return "error"
    kinds = {r.kind for r in run.results if not r.passed}
    return "fail" if "crash" in kinds else "review" if kinds else "pass"


def _code(s: str) -> str:
    s = s.replace("\n", " ")
    return f"`` {s} ``" if "`" in s else f"`{s}`"


def render(contexts: list[FileContext], run: RunResult) -> str:
    st = status(run)
    level, score = risk(contexts)
    fns = [(i, f) for i, c in enumerate(contexts) for f in c.functions]
    total = len(run.results)
    failed = [r for r in run.results if not r.passed]
    out = [
        "## PR-Sentinel report",
        "",
        BADGES[st],
        "",
        f"**{HEADLINES[st]}**",
        "",
        f"| Risk | Files | Lines changed | Functions exercised | Cases | Failed | Time |",
        f"|---|---|---|---|---|---|---|",
        f"| **{level}** ({score}) | {len(contexts)} | {sum(c.lines_changed for c in contexts)} | "
        f"{len(fns)} | {total} | {len(failed)} | {run.duration:.2f}s |",
        "",
    ]
    if run.error:
        out += ["### Suite error", "", "```", run.error.strip(), "```", ""]

    by_fn: dict[tuple[int, str], list[CaseResult]] = defaultdict(list)
    for r in run.results:
        by_fn[(r.case.file_index, r.case.function)].append(r)
    if fns:
        out += ["### Generated edge-case tests", "",
                "| Function | File | Why it was tested | Cases | Passed | Crashes | Behaviour changes |",
                "|---|---|---|---|---|---|---|"]
        for i, f in fns:
            rs = by_fn[(i, f.name)]
            crash = sum(r.kind == "crash" for r in rs)
            other = sum(not r.passed and r.kind != "crash" for r in rs)
            out.append(f"| `{f.name}` | `{contexts[i].path}:{f.start}` | {f.reason} | {len(rs)} | "
                       f"{sum(r.passed for r in rs)} | {crash} | {other} |")
        out.append("")

    groups: dict[tuple, list[CaseResult]] = defaultdict(list)
    for r in failed:
        groups[(r.case.file_index, r.case.function, r.kind, r.error, r.line)].append(r)
    for title, kinds in (("Regressions", {"crash"}), ("Behaviour changes to confirm", {"value_change", "now_accepts"})):
        selected = [(k, v) for k, v in groups.items() if k[2] in kinds]
        if not selected:
            continue
        out += [f"### {title}", ""]
        for (fi, func, kind, error, line), rs in selected:
            ctx = contexts[fi]
            label = error or {"value_change": "different result", "now_accepts": "accepts rejected input"}[kind]
            out += [f"#### `{func}`: {label} ({len(rs)} case{'s' * (len(rs) != 1)})", ""]
            if line:
                src = ctx.new_source.splitlines()[line - 1].strip() if line <= len(ctx.new_source.splitlines()) else ""
                fn = next((f for f in ctx.functions if f.name == func), None)
                tag = "changed in this PR" if fn and line in fn.changed_lines else "not changed, but reached"
                out += [f"- **Where:** `{ctx.path}:{line}` ({tag}): {_code(src)}"]
            for r in rs[:EXAMPLES_PER_GROUP]:
                out += [f"- **Input:** {_code(r.case.call)}{' (LLM-proposed)' if r.case.source == 'llm' else ''}",
                        f"  - {r.detail}"]
            if len(rs) > EXAMPLES_PER_GROUP:
                out.append(f"- ...and {len(rs) - EXAMPLES_PER_GROUP} more input(s)")
            out += [f"- **Suggested fix:** {rs[0].fix}", ""]
            if kind == "crash" and rs[0].trace:
                out += ["<details><summary>Failure trace</summary>", "", "```",
                        rs[0].trace.replace(f"new_{fi}.py", ctx.path).strip()[-2500:], "```", "", "</details>", ""]

    skipped = [s for c in contexts for s in c.skipped]
    if skipped:
        out += ["### Not exercised", ""] + [f"- {s}" for s in skipped] + [""]
    out += ["<sub>Differential testing: each changed function (and its callers in the same module) runs "
            "old and new on identical inputs. Crashes block; behaviour changes need a human to confirm.</sub>", ""]
    return "\n".join(out)
