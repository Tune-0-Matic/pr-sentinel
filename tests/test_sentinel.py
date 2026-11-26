import difflib
import shutil
import subprocess
from pathlib import Path

import pytest

from src import cli, evaluator, reporter
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
    reasons = {f.name: f.reason for f in ctx.functions}
    assert reasons == {"clamp": "changed", "double": "calls clamp"}
    clamp = ctx.functions[0]
    assert clamp.changed_lines == [6]
    assert "10" in clamp.numbers  # module constant LIMIT is harvested as a boundary


def test_new_and_non_python_files_are_skipped():
    [fd] = parse_diff(make_patch("", "def f(x):\n    return x\n"))
    assert analyze(fd, "", "def f(x):\n    return x\n").skipped[0].startswith("`f`: new function")
    [txt] = parse_diff(make_patch("a\n", "b\n", "notes.txt"))
    assert analyze(txt, "a\n", "b\n").skipped == ["`notes.txt`: not a Python file"]


def test_candidates_include_boundaries_null_and_type_mismatch():
    [fd] = parse_diff(make_patch(BEFORE, AFTER))
    clamp = analyze(fd, BEFORE, AFTER).functions[0]
    values = evaluator.candidates(clamp.params[0], clamp)
    assert {"9", "10", "11", "0", "-1", "None", "'x'"} <= set(values)


def test_combos_are_capped_and_deterministic():
    cands = [[str(i) for i in range(20)]] * 3
    first = evaluator.combos(cands, cap=50)
    assert len(first) == 50 and first == evaluator.combos(cands, cap=50)


@pytest.mark.parametrize("expr, ok", [
    ("Decimal('0.005')", True), ("[1, -2, (3,)]", True), ("{'a': None}", True),
    ("__import__('os').system('x')", False), ("open('f')", False), ("Decimal(x)", False), ("a.b", False),
])
def test_safe_expr(expr, ok):
    assert evaluator.safe_expr(expr) is ok


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


def test_demo_catches_zero_dollar_regression():
    contexts = load_patch(ROOT / "demo" / "sample_pr.patch", ROOT)
    report, result = cli.scan(contexts, ROOT)
    assert reporter.status(result) == "fail"
    crashes = [r for r in result.results if r.kind == "crash"]
    assert crashes and all(r.case.function == "line_total" and r.line == 16 for r in crashes)
    assert "Guard the denominator" in report


def test_clean_refactor_passes(tmp_path):
    (tmp_path / "mod.py").write_text(BEFORE, encoding="utf-8")
    after = BEFORE.replace("    return min(x, LIMIT)", "    return x if x < LIMIT else LIMIT")
    (tmp_path / "p.patch").write_text(make_patch(BEFORE, after), encoding="utf-8")
    assert cli.main(["scan", "--diff", str(tmp_path / "p.patch"), "--repo", str(tmp_path),
                     "--output", str(tmp_path / "r.md")]) == 0
    assert "No behaviour differences" in (tmp_path / "r.md").read_text(encoding="utf-8")


def test_suite_timeout_is_reported(tmp_path):
    (tmp_path / "mod.py").write_text("def f(x: int) -> int:\n    return x\n", encoding="utf-8")
    after = "def f(x: int) -> int:\n    while True:\n        pass\n"
    (tmp_path / "p.patch").write_text(make_patch("def f(x: int) -> int:\n    return x\n", after), encoding="utf-8")
    _, result = cli.scan(load_patch(tmp_path / "p.patch", tmp_path), tmp_path, timeout=5)
    assert result.timed_out and reporter.status(result) == "error"


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
