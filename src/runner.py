"""Run the generated differential suite in a separate pytest process and diagnose failures."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .diff_parser import FileContext, Function
from .evaluator import Case

HARNESS = '''\
import importlib.util
import sys
from decimal import Decimal

import pytest

sys.path[:0] = {paths!r}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD = {{{old}}}
NEW = {{{new}}}


def _same(a, b):
    if type(a).__name__ != type(b).__name__:
        return False
    if a == b or repr(a) == repr(b):
        return True
    return hasattr(a, "__dict__") and vars(a) == vars(b)


def _call(fn, make):
    args, kwargs = make()  # fresh objects per call so one version cannot mutate the other's input
    return fn(*args, **kwargs)


CASES = [
{cases}
]


@pytest.mark.parametrize("fid, func, make", CASES, ids={ids!r})
def test_case(fid, func, make):
    try:
        old = ("ok", _call(getattr(OLD[fid], func), make))
    except Exception as e:
        old = ("raised", e)
    if old[0] == "ok":
        try:
            new = _call(getattr(NEW[fid], func), make)
        except Exception as e:
            e.add_note(f"OLD_RESULT={{old[1]!r:.300}}")
            raise
        assert _same(old[1], new), f"VALUE_CHANGE old={{old[1]!r:.300}} new={{new!r:.300}}"
    else:
        try:
            new = _call(getattr(NEW[fid], func), make)
        except Exception:
            return
        assert False, f"NOW_ACCEPTS old raised {{type(old[1]).__name__}}: {{old[1]!s:.200}} new={{new!r:.300}}"
'''

NEW_LINE_RE = re.compile(r"new_(\d+)\.py:(\d+)")
FIXES = [
    (r"ZeroDivisionError|DivisionByZero|DivisionUndefined|InvalidOperation",
     "Guard the denominator: handle zero before dividing (the previous version returned a value here)."),
    (r"NoneType", "Handle `None` before using the value; the previous version accepted it."),
    (r"empty|IndexError|StopIteration", "Handle empty collections (an early return or a `default=`)."),
    (r"KeyError", "Use `.get()` or check membership before indexing."),
    (r"RecursionError", "Check the recursion's base case."),
]


@dataclass
class CaseResult:
    case: Case
    passed: bool
    kind: str = ""  # crash | value_change | now_accepts
    error: str = ""  # exception type, for crashes
    detail: str = ""
    trace: str = ""
    line: int | None = None
    fix: str = ""


@dataclass
class RunResult:
    results: list[CaseResult]
    duration: float
    timed_out: bool = False
    error: str = ""


def _case_source(c: Case) -> str:
    kwargs = ", ".join(f"{k!r}: {v}" for k, v in c.kwargs.items())
    return f"    ({c.file_index}, {c.function!r}, lambda: ([{', '.join(c.args)}], {{{kwargs}}})),"


def write_suite(workdir: Path, contexts: list[FileContext], cases: list[Case], repo: Path) -> Path:
    used = sorted({c.file_index for c in cases})
    for i in used:
        (workdir / f"old_{i}.py").write_text(contexts[i].old_source, encoding="utf-8")
        (workdir / f"new_{i}.py").write_text(contexts[i].new_source, encoding="utf-8")
    paths = [str(repo)] + sorted({str((repo / contexts[i].path).parent) for i in used})
    src = HARNESS.format(
        paths=paths,
        old=", ".join(f"{i}: _load('old_{i}', {str(workdir / f'old_{i}.py')!r})" for i in used),
        new=", ".join(f"{i}: _load('new_{i}', {str(workdir / f'new_{i}.py')!r})" for i in used),
        cases="\n".join(_case_source(c) for c in cases),
        ids=[c.id for c in cases],
    )
    (workdir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")  # keeps outer configs out
    test_file = workdir / "test_sentinel_generated.py"
    test_file.write_text(src, encoding="utf-8")
    return test_file


def diagnose(r: CaseResult, fn: Function | None) -> None:
    text = r.detail + "\n" + r.trace
    if "VALUE_CHANGE" in text:
        r.kind = "value_change"
        r.detail = text[text.index("VALUE_CHANGE") + 13:].split("\n")[0]
    elif "NOW_ACCEPTS" in text:
        r.kind = "now_accepts"
        r.detail = text[text.index("NOW_ACCEPTS") + 12:].split("\n")[0]
        r.fix = "The new code accepts input the old code rejected; a validation check was probably removed."
    else:
        r.kind = "crash"
        r.error = r.detail.split(":")[0].split(".")[-1].strip() or "Exception"
        old = re.search(r"OLD_RESULT=(.*)", r.trace)
        r.detail = f"old returned {old[1].strip() if old else '?'}; new raises {r.detail.strip().splitlines()[0][:300]}"
        hits = [int(m[2]) for m in NEW_LINE_RE.finditer(r.trace) if m[1] == r.case.id.split("-")[0]]
        r.line = hits[-1] if hits else None
        r.fix = next((fix for pat, fix in FIXES if re.search(pat, text)),
                     "Compare the changed lines with the previous implementation for this input.")
    if r.line is None and fn and fn.changed_lines:
        r.line = fn.changed_lines[0]
    if r.kind == "value_change":
        r.fix = "Output changed for this input. If unintended, restore the previous logic; if intended, update callers and tests."


def run(contexts: list[FileContext], cases: list[Case], repo: str | Path, timeout: float = 120) -> RunResult:
    if not cases:
        return RunResult([], 0.0)
    fns = {(i, fn.name): fn for i, ctx in enumerate(contexts) for fn in ctx.functions}
    by_id = {c.id: c for c in cases}
    with tempfile.TemporaryDirectory(prefix="pr-sentinel-") as tmp:
        workdir = Path(tmp)
        test_file = write_suite(workdir, contexts, cases, Path(repo).resolve())
        junit = workdir / "junit.xml"
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        cmd = [sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider",
               "--tb=short", f"--junitxml={junit}"]
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return RunResult([], time.perf_counter() - t0, timed_out=True,
                             error=f"suite exceeded {timeout:g}s; a changed function may loop forever")
        duration = time.perf_counter() - t0
        if not junit.exists():
            return RunResult([], duration, error=(proc.stdout + proc.stderr)[-3000:])
        results = []
        for tc in ET.parse(junit).iter("testcase"):
            m = re.search(r"\[(.*)\]$", tc.get("name", ""))
            case = by_id.get(m[1]) if m else None
            if case is None:  # collection/import errors land here
                err = tc.find("error")
                if err is not None:
                    return RunResult([], duration, error=(err.get("message", "") + "\n" + (err.text or ""))[-3000:])
                continue
            bad = tc.find("failure")
            if bad is None:
                bad = tc.find("error")
            if bad is None:
                results.append(CaseResult(case, True))
                continue
            trace = bad.text or ""
            r = CaseResult(case, False, detail=bad.get("message", ""), trace=trace[trace.find("new_"):] if "new_" in trace else trace)
            diagnose(r, fns.get((case.file_index, case.function)))
            results.append(r)
    return RunResult(results, duration)
