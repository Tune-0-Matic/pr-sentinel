"""Run every input in an isolated worker process with a per-input timeout, then shrink failures."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import fuzzer
from .diff_parser import FileContext
from .fuzzer import Case

WORKER = Path(__file__).with_name("worker.py")
MARK = "@@pr-sentinel@@ "
STARTUP_TIMEOUT = 60  # importing the target modules can be slow; per-input limits start after this
MAX_TIMEOUTS_PER_FUNCTION = 3  # stop feeding a function that keeps hanging

FIXES = [
    (r"ZeroDivisionError|DivisionByZero|DivisionUndefined",
     "Guard the denominator: handle zero before dividing (the previous version returned a value here)."),
    (r"InvalidOperation", "A Decimal operation became invalid for this input (often Infinity or NaN reaching "
                          "quantize or a comparison); check how the new code handles non-finite values."),
    (r"NoneType", "Handle `None` before using the value; the previous version accepted it."),
    (r"empty|IndexError|StopIteration", "Handle empty collections (an early return or a `default=`)."),
    (r"KeyError", "Use `.get()` or check membership before indexing."),
    (r"RecursionError", "Check the recursion's base case."),
    (r"invalid literal|could not convert",
     "The new parsing is stricter than the old one; accept the formats the previous version accepted."),
]


@dataclass
class CaseResult:
    case: Case
    passed: bool
    kind: str = ""  # crash | timeout | value_change | now_accepts
    error: str = ""
    detail: str = ""
    trace: str = ""
    line: int | None = None
    fix: str = ""
    minimized: Case | None = None


@dataclass
class RunResult:
    results: list[CaseResult]
    duration: float
    error: str = ""
    stopped: list[str] = field(default_factory=list)  # functions abandoned after repeated timeouts


class Worker:
    def __init__(self, files: dict, paths: list[str], cwd: Path):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen(
            [sys.executable, str(WORKER), json.dumps(files), json.dumps(paths)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", cwd=cwd, env=env)
        self.replies: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        msg = self._next(STARTUP_TIMEOUT)
        if not (isinstance(msg, dict) and msg.get("ready")):
            self.close()
            raise RuntimeError("test worker failed to start")

    def _pump(self):
        for line in self.proc.stdout:
            if line.startswith(MARK):  # anything else is noise from the code under test
                self.replies.put(json.loads(line[len(MARK):]))
        self.replies.put(None)

    def _next(self, timeout: float):
        try:
            return self.replies.get(timeout=timeout)
        except queue.Empty:
            return "timeout"

    def ask(self, case: Case, timeout: float) -> dict:
        try:
            self.proc.stdin.write(json.dumps({"file": case.file_index, "func": case.function,
                                              "args": case.args, "kwargs": case.kwargs}) + "\n")
            self.proc.stdin.flush()
        except OSError:
            return {"result": "died", "phase": "old"}
        phase = "old"
        while True:
            msg = self._next(timeout)  # the limit applies to the old call and the new call separately
            if msg == "timeout":
                return {"result": "timeout", "phase": phase}
            if msg is None:
                return {"result": "died", "phase": phase, "code": self.proc.wait()}
            if msg.get("phase") == "new":
                phase = "new"
                continue
            return msg

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


class Pool:
    """One live worker, replaced whenever an input hangs or kills it."""

    def __init__(self, *args):
        self.args = args
        self.worker = Worker(*args)

    def ask(self, case: Case, timeout: float) -> dict:
        msg = self.worker.ask(case, timeout)
        if msg["result"] in ("timeout", "died"):
            self.worker.close()
            self.worker = Worker(*self.args)
        return msg

    def close(self):
        self.worker.close()


def to_result(case: Case, msg: dict, timeout: float) -> CaseResult:
    kind = msg["result"]
    if kind == "pass":
        return CaseResult(case, True)
    r = CaseResult(case, False, kind=kind, error=msg.get("error", ""), detail=msg.get("detail", ""),
                   trace=msg.get("trace", ""), line=msg.get("line"))
    if kind == "timeout":
        r.error, r.detail = "Timeout", f"old returned in time; new did not finish within {timeout:g}s"
        r.fix = "Look for a loop whose exit condition can no longer be reached for this input."
    elif kind == "died":
        r.kind, r.error = "crash", "ProcessCrash"
        r.detail = f"old returned; new killed the interpreter (exit code {msg.get('code')})"
        r.fix = "The new code takes the whole process down for this input (native code, os._exit, memory)."
    elif kind == "value_change":
        r.fix = ("Output changed for this input. If unintended, restore the previous logic; "
                 "if intended, update callers and tests.")
    elif kind == "now_accepts":
        r.fix = "The new code accepts input the old code rejected; a validation check was probably removed."
    else:
        r.fix = next((fix for pat, fix in FIXES if re.search(pat, f"{r.error}: {r.detail}")),
                     "Compare the changed lines with the previous implementation for this input.")
    return r


def signature(r: CaseResult) -> tuple:
    return r.case.file_index, r.case.function, r.kind, r.error, r.line


def minimize(pool: Pool, r: CaseResult, timeout: float) -> Case | None:
    try:
        values = [fuzzer.evaluate(a) for a in [*r.case.args, *r.case.kwargs.values()]]
    except Exception:
        return None
    n, keys = len(r.case.args), list(r.case.kwargs)

    def as_case(vals: list) -> Case:
        return Case(r.case.id + "-min", r.case.file_index, r.case.function, [fuzzer.to_src(v) for v in vals[:n]],
                    {k: fuzzer.to_src(v) for k, v in zip(keys, vals[n:])}, r.case.source)

    def still_fails(vals: list) -> bool:
        trial = as_case(vals)
        msg = pool.ask(trial, timeout)
        return msg["result"] not in ("pass", "timeout", "died") and \
            signature(to_result(trial, msg, timeout)) == signature(r)

    return as_case(fuzzer.shrink(values, still_fails))


def run(contexts: list[FileContext], cases: list[Case], repo: str | Path, timeout: float = 1.0) -> RunResult:
    if not cases:
        return RunResult([], 0.0)
    repo = Path(repo).resolve()
    used = sorted({c.file_index for c in cases})
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="pr-sentinel-") as tmp:
        workdir = Path(tmp)
        files = {}
        for i in used:
            (workdir / f"old_{i}.py").write_text(contexts[i].old_source, encoding="utf-8")
            (workdir / f"new_{i}.py").write_text(contexts[i].new_source, encoding="utf-8")
            files[str(i)] = [str(workdir / f"old_{i}.py"), str(workdir / f"new_{i}.py")]
        paths = [str(repo)] + sorted({str((repo / contexts[i].path).parent) for i in used})
        try:
            pool = Pool(files, paths, workdir)
        except RuntimeError as e:
            return RunResult([], time.perf_counter() - t0, error=str(e))
        try:
            results, hangs, stopped = [], {}, []
            for case in cases:
                key = (case.file_index, case.function)
                if hangs.get(key, 0) >= MAX_TIMEOUTS_PER_FUNCTION:
                    if case.function not in stopped:
                        stopped.append(case.function)
                    continue
                msg = pool.ask(case, timeout)
                if msg["result"] in ("timeout", "died"):
                    if msg["phase"] == "old":
                        continue  # the old version hangs or dies too: not a regression
                    hangs[key] = hangs.get(key, 0) + 1
                results.append(to_result(case, msg, timeout))
            seen = set()
            for r in results:  # shrink one representative per distinct failure (hangs cost a full timeout each)
                if not r.passed and r.kind != "timeout" and r.error != "ProcessCrash" and signature(r) not in seen:
                    seen.add(signature(r))
                    r.minimized = minimize(pool, r, timeout)
        finally:
            pool.close()
    return RunResult(results, time.perf_counter() - t0, stopped=stopped)
