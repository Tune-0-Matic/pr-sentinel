"""pr-sentinel command line: `scan` a diff or branch, or run the bundled `demo`."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import evaluator, reporter, runner
from .diff_parser import FileContext, PatchError, load_git, load_patch

ROOT = Path(__file__).resolve().parent.parent


def scan(contexts: list[FileContext], repo: Path, mode: str = "mock", timeout: float = 1.0):
    cases = evaluator.generate(contexts, mode)
    result = runner.run(contexts, cases, repo, timeout)
    return reporter.render(contexts, result), result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pr-sentinel", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan", help="evaluate a diff file or a branch comparison")
    src = s.add_mutually_exclusive_group(required=True)
    src.add_argument("--diff", type=Path, help="unified diff / .patch file, applied in memory to --repo")
    src.add_argument("--base", help="git ref to compare against, e.g. main or origin/main")
    s.add_argument("--head", default="HEAD", help="git ref with the changes (default: HEAD)")
    s.add_argument("--repo", type=Path, default=Path("."), help="repository root (default: .)")
    s.add_argument("--mode", choices=["mock", "llm"], default="mock")
    s.add_argument("--output", type=Path, default=Path("sentinel-report.md"))
    s.add_argument("--timeout", type=float, default=1.0, help="per-input limit in seconds (default: 1.0)")
    sub.add_parser("demo", help="run against examples/sample_pr.patch and print the report")
    args = parser.parse_args(argv)

    try:
        if args.command == "demo":
            report, result = scan(load_patch(ROOT / "examples" / "sample_pr.patch", ROOT), ROOT)
            print(report)
            return 0 if reporter.status(result) == "fail" else 1
        contexts = load_patch(args.diff, args.repo) if args.diff else load_git(args.repo, args.base, args.head)
        report, result = scan(contexts, args.repo, args.mode, args.timeout)
    except (PatchError, OSError, evaluator.LLMError) as e:
        print(f"pr-sentinel: {e}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as e:
        print(f"pr-sentinel: git {' '.join(e.cmd[1:])} failed: {e.stderr.strip()}", file=sys.stderr)
        return 2
    args.output.write_text(report, encoding="utf-8")
    st = reporter.status(result)
    print(f"pr-sentinel: {st} - {len(result.results)} cases, "
          f"{sum(not r.passed for r in result.results)} flagged. Report: {args.output}")
    return {"fail": 1, "error": 2}.get(st, 0)


if __name__ == "__main__":
    sys.exit(main())
