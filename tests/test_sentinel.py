import difflib
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from src import cli, evaluator, fuzzer, reporter, runner
from src.diff_parser import PatchError, analyze, apply_patch, load_git, load_patch, parse_diff

ROOT = Path(__file__).resolve().parent.parent

BEFORE = '''\
LIMIT = 10


def clamp(x: int) -> int:
    return min(x, LIMIT)


def double(x: int) -> int:
    """Twice x."""
    return clamp(x) * 2


def untouched(x):
    return x
'''
AFTER = BEFORE.replace("LIMIT = 10", "LIMIT = 10\nFLOOR = 0").replace(
    "    return min(x, LIMIT)", "    return max(FLOOR, min(x, LIMIT))")


def make_patch(before: str, after: str, rel: str = "mod.py") -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{rel}", f"b/{rel}"))


def scan_change(tmp_path: Path, before: str, after: str, **kw):
    (tmp_path / "mod.py").write_text(before, encoding="utf-8")
    (tmp_path / "p.patch").write_text(make_patch(before, after), encoding="utf-8")
    return cli.scan(load_patch(tmp_path / "p.patch", tmp_path), tmp_path, **kw)


def test_parse_and_apply_roundtrip():
    [fd] = parse_diff(make_patch(BEFORE, AFTER))
    assert fd.path == "mod.py"
    assert apply_patch(BEFORE, fd) == AFTER
    assert fd.added == {2, 6}


def test_apply_rejects_mismatched_context():
    [fd] = parse_diff(make_patch(BEFORE, AFTER))
    with pytest.raises(PatchError):
        apply_patch(BEFORE.replace("LIMIT = 10", "LIMIT = 11"), fd)


def test_changed_function_and_caller_detected():
    [fd] = parse_diff(make_patch(BEFORE, AFTER))
    ctx = analyze(fd, BEFORE, AFTER)
    assert {f.name: f.reason for f in ctx.functions} == {"clamp": "changed", "double": "calls clamp"}
    assert ctx.functions[0].changed_lines == [6]


def test_new_and_non_python_files_are_skipped():
    [fd] = parse_diff(make_patch("", "def f(x):\n    return x\n"))
    assert analyze(fd, "", "def f(x):\n    return x\n").skipped[0].startswith("`f`: new function")
    [txt] = parse_diff(make_patch("a\n", "b\n", "notes.txt"))
    assert analyze(txt, "a\n", "b\n").skipped == ["`notes.txt`: not a Python file"]


def test_constants_from_body_module_and_len_comparisons():
    before = 'MAX = Decimal("50")\n\n\ndef f(items: list, code: str, n: int) -> int:\n    return len(items)\n'
    after = before.replace("    return len(items)",
                           '    if len(items) > 100 or code == "VIP" or n >= MAX:\n        return 0\n    return 1')
    [fd] = parse_diff(make_patch(before, after))
    fn = analyze(fd, before, after).functions[0]
    assert fn.lengths == [100]
    assert "50" in fn.numbers and "VIP" in fn.strings
    items, code, n = (fuzzer.candidates(p, fn) for p in fn.params)
    assert {"[0] * 99", "[0] * 100", "[0] * 101", "[]", "list(range(500))", "None"} <= set(items)
    assert {"'VIP'", "'vip'", "''", "'x' * 10000", "0"} <= set(code)
    assert {"49", "50", "51", "0", "-1", "'x'"} <= set(n)


def test_float_candidates_include_nan_and_inf():
    [fd] = parse_diff(make_patch("def f(x: float):\n    return x\n", "def f(x: float):\n    return -x\n"))
    fn = analyze(fd, "def f(x: float):\n    return x\n", "def f(x: float):\n    return -x\n").functions[0]
    assert {"float('nan')", "float('inf')"} <= set(fuzzer.candidates(fn.params[0], fn))


def test_combos_are_capped_and_deterministic():
    cands = [[str(i) for i in range(20)]] * 3
    first = fuzzer.combos(cands, cap=50)
    assert len(first) == 50 and first == fuzzer.combos(cands, cap=50)


@pytest.mark.parametrize("expr, ok", [
    ("Decimal('0.005')", True), ("[1, -2, (3,)]", True), ("{'a': None}", True), ("float('nan')", True),
    ("__import__('os').system('x')", False), ("open('f')", False), ("Decimal(x)", False), ("a.b", False),
    ("10**10**10", False),
])
def test_safe_expr(expr, ok):
    assert evaluator.safe_expr(expr) is ok


def test_shrink_500_item_list_to_single_element():
    assert fuzzer.shrink([list(range(500))], lambda v: 0 in v[0]) == [[0]]


def test_shrink_string_and_number_together():
    fails = lambda v: "%" in v[0] and v[1] > 3  # noqa: E731
    assert fuzzer.shrink(["12.5%" * 40, 10**6], fails) == ["%", 4]


def test_shrink_nested_dict_and_respects_budget():
    calls = []
    fails = lambda v: calls.append(1) or "k" in v[0].get("a", {})  # noqa: E731
    assert fuzzer.shrink([{"a": {"k": [1, 2, 3]}, "b": "zzz"}], fails) == [{"a": {"k": []}}]
    calls.clear()
    fuzzer.shrink([list(range(1, 17))], lambda v: calls.append(1) or (len(v[0]) == 16 and min(v[0]) > 0), budget=7)
    assert len(calls) == 7


def test_to_src_round_trips_special_values():
    for v in [float("nan"), float("-inf"), Decimal("1.50"), (1,), {"a": [None, True]}, "it's"]:
        back = fuzzer.evaluate(fuzzer.to_src(v))
        assert repr(back) == repr(v)


HANG_BEFORE = "def f(x: int) -> int:\n    return x\n"
HANG_AFTER = '''\
import os


def f(x: int) -> int:
    print("noise on stdout must not break the protocol")
    if x == 0:
        while True:
            pass
    if x == 2:
        os._exit(3)
    return x
'''


def test_per_input_timeout_and_process_crash_are_contained(tmp_path):
    _, result = scan_change(tmp_path, HANG_BEFORE, HANG_AFTER, timeout=0.5)
    failed = {r.case.call: r for r in result.results if not r.passed}
    assert failed["f(0)"].kind == "timeout"
    assert failed["f(2)"].error == "ProcessCrash"
    assert any(r.passed and r.case.call == "f(1)" for r in result.results)  # the run carried on
    assert reporter.status(result) == "fail"


def test_function_that_always_hangs_is_abandoned(tmp_path):
    _, result = scan_change(tmp_path, HANG_BEFORE, "def f(x: int) -> int:\n    while True:\n        pass\n",
                            timeout=0.3)
    assert len(result.results) == runner.MAX_TIMEOUTS_PER_FUNCTION and result.stopped == ["f"]


def test_old_version_hanging_is_not_blamed_on_the_pr(tmp_path):
    hangs = "def f(x: int) -> int:\n    while x == 0:\n        pass\n    return x\n"
    _, result = scan_change(tmp_path, hangs, hangs.replace("return x", "return x  # tidy"), timeout=0.3)
    assert reporter.status(result) == "pass"


def test_crash_reports_minimized_input_and_line(tmp_path):
    before = "def mean(items: list) -> float:\n    return sum(items) / len(items) if items else 0.0\n"
    after = "def mean(items: list) -> float:\n    total = sum(items)\n    return total / len([i for i in items if i])\n"
    report, result = scan_change(tmp_path, before, after)
    crash = next(r for r in result.results if r.error == "ZeroDivisionError" and r.minimized)
    assert crash.line == 3 and crash.minimized.call == "mean([])"
    assert "**Minimal reproducer:** `mean([])`" in report
    assert "Guard the denominator" in report and "`mod.py:3` (changed in this PR)" in report


def test_demo_catches_zero_dollar_regression():
    report, result = cli.scan(load_patch(ROOT / "examples" / "sample_pr.patch", ROOT), ROOT)
    assert reporter.status(result) == "fail"
    zero_div = [r for r in result.results if r.error == "DivisionByZero"]
    assert zero_div and all(r.case.function == "line_total" and r.line == 16 for r in zero_div)
    assert "Minimal reproducer:** `line_total(Decimal('0'), 0, Decimal('1'))`" in report


def test_report_sections_for_review_only_change(tmp_path):
    report, result = scan_change(tmp_path, BEFORE, AFTER)
    assert reporter.status(result) == "review"
    assert "PR--Sentinel-review-yellow" in report
    assert "### Behaviour changes to confirm" in report and "### Regressions" not in report
    assert "| `double` | `mod.py:" in report and "calls clamp" in report


def test_clean_refactor_passes(tmp_path):
    (tmp_path / "mod.py").write_text(BEFORE, encoding="utf-8")
    after = BEFORE.replace("    return min(x, LIMIT)", "    return x if x < LIMIT else LIMIT")
    (tmp_path / "p.patch").write_text(make_patch(BEFORE, after), encoding="utf-8")
    assert cli.main(["scan", "--diff", str(tmp_path / "p.patch"), "--repo", str(tmp_path),
                     "--output", str(tmp_path / "r.md")]) == 0
    assert "No behaviour differences" in (tmp_path / "r.md").read_text(encoding="utf-8")


def test_llm_reply_parsing_drops_unsafe_and_unknown(monkeypatch):
    [fd] = parse_diff(make_patch(BEFORE, AFTER))
    ctx = analyze(fd, BEFORE, AFTER)
    reply = ('Here you go: {"cases": ['
             '{"function": "clamp", "args": ["-5"]},'
             '{"function": "clamp", "args": ["__import__(\'os\')"]},'
             '{"function": "nope", "args": ["1"]}]}')
    monkeypatch.setattr(evaluator, "_ask_llm", lambda prompt: reply)
    llm = [c for c in evaluator.generate([ctx], "llm") if c.source == "llm"]
    assert [c.call for c in llm] == ["clamp(-5)"]


def test_llm_mode_without_keys_errors(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(evaluator.LLMError):
        evaluator._ask_llm("x")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_branch_mode(tmp_path):
    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "mod.py").write_text(BEFORE, encoding="utf-8")
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "feature")
    (tmp_path / "mod.py").write_text(BEFORE.replace("return min(x, LIMIT)", "return 100 // (x - 1)"),
                                     encoding="utf-8")
    git("commit", "-qam", "change")
    _, result = cli.scan(load_git(tmp_path, "main"), tmp_path)
    assert reporter.status(result) == "fail"
    assert any(r.error == "ZeroDivisionError" for r in result.results if not r.passed)
