# PR-Sentinel

[![CI](https://github.com/Tune-0-Matic/pr-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/Tune-0-Matic/pr-sentinel/actions/workflows/ci.yml)

**PR-Sentinel runs every Python function a pull request changes in both its old and new versions, on hundreds of boundary and negative inputs, and flags any input where the new code crashes, hangs or returns something different.** It shrinks each failing input to the smallest one that still reproduces the failure, points to the line in the diff that caused it, and posts the result as a PR comment, with no API key and no runtime dependencies.

## Quickstart

```bash
pip install git+https://github.com/Tune-0-Matic/pr-sentinel
pr-sentinel scan --base origin/main --output sentinel-report.md
```

```yaml
# .github/workflows/sentinel.yml
on: pull_request
permissions: { contents: read, pull-requests: write }
jobs:
  sentinel:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: Tune-0-Matic/pr-sentinel@main
```

Try it without a repo of your own: clone this one and run `python -m src.cli demo`.

## What it posts on a pull request

This is the comment for the bundled example PR ([full report](examples/SAMPLE_REPORT.md)). The PR caps invoice discounts at 50% of the line amount, which means dividing by the line's gross amount, and that's zero for free items:

> ![PR-Sentinel](https://img.shields.io/badge/PR--Sentinel-regression-red)
>
> **Regression found: changed code crashes or hangs on inputs the old code handled.**
>
> | Risk | Files | Lines changed | Functions exercised | Cases | Failed | Time |
> |---|---|---|---|---|---|---|
> | **Low** (2.0) | 1 | 9 | 2 | 388 | 72 | 0.33s |
>
> | Function | File | Why it was tested | Cases | Passed | Crashes / hangs | Behaviour changes |
> |---|---|---|---|---|---|---|
> | `line_total` | `examples/invoice_engine.py:8` | changed | 300 | 228 | 29 | 43 |
> | `invoice_total` | `examples/invoice_engine.py:25` | calls line_total | 88 | 88 | 0 | 0 |
>
> #### `line_total`: DivisionByZero (24 cases)
>
> - **Where:** `examples/invoice_engine.py:16` (changed in this PR): `if discount / gross > MAX_DISCOUNT_RATE:`
> - **Minimal reproducer:** `line_total(Decimal('0'), 0, Decimal('1'))` (shrunk from `line_total(Decimal('0'), 1, Decimal('19.99'))`)
> - **Input:** `line_total(Decimal('0'), 1, Decimal('19.99'))`
>   - old returned Decimal('0.00'); new raises DivisionByZero
> - **Suggested fix:** Guard the denominator: handle zero before dividing (the previous version returned a value here).
>
> ```
> examples/invoice_engine.py:16 in line_total
>     if discount / gross > MAX_DISCOUNT_RATE:
> DivisionByZero: [<class 'decimal.DivisionByZero'>]
> ```

The intended change, where a big discount is now capped, also shows up: 43 inputs are listed under "Behaviour changes to confirm", for example `line_total(Decimal('1'), 1, Decimal('1'))`, which used to return `0.00` and now returns `0.50`. Behaviour changes don't fail the check; crashes and hangs do.

## Why differential testing

Most regressions get through review because nobody wrote a test for the input that breaks: a zero-dollar line, an empty list, a `None` last name. Generated tests that only check "doesn't crash" can't tell you whether behaviour changed, and writing expected values by hand doesn't scale. PR-Sentinel uses the code before the change as the oracle, so every generated input becomes a regression test with a known expected result.

| Old version | New version | Verdict |
|---|---|---|
| returns `a` | returns an equal value of the same type | pass |
| returns `a` | returns something else, or a different type | behaviour change (review) |
| returns `a` | raises | **regression** |
| returns `a` | runs past the per-input timeout, or kills the process | **regression** |
| raises | raises (any type) | pass: both reject the input |
| raises | returns | accepts input that used to be rejected (review) |

## How inputs are chosen

`src/fuzzer.py` builds inputs per parameter from the type annotation and the function's AST:

- **Boundary constants:** every number the old or new code uses, including module constants it reads (such as `FREE_SHIPPING_MIN = Decimal("50")`), becomes `n - 1`, `n`, `n + 1` in the parameter's own type.
- **Size boundaries:** a comparison like `len(items) > 100` produces collections of length 99, 100 and 101.
- **String and dict keys:** string literals in the code are used as-is and lowercased, which catches lost case normalisation.
- **Negative and pathological values:** `None`, zero, negatives, empty collections, `float('nan')`, `float('inf')`, `Decimal('Infinity')`, deeply nested dicts, 500-item lists, 10,000-character strings, numeric strings such as `'1.5'`, and a wrong-type value for every parameter.
- **Combination:** at most 300 inputs per function. Each parameter is varied one at a time from a normal baseline, then the rest are seeded random combinations, so runs are reproducible.

**Shrinking:** for every distinct failure (same function, outcome, exception and line), a greedy delta-debugging pass tries smaller versions of each argument: halves of lists and strings, removed elements and keys, numbers stepped towards zero. It keeps any version that still fails the same way. A 500-item list that triggers a crash comes back as `[0]`.

**LLM mode** (`--mode llm`, with `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` plus `OPENAI_MODEL`) sends the model each function's before and after source and adds up to 15 proposed inputs per function. The model never writes test logic. Its inputs are checked with `ast` against an allow-list (literals, `Decimal('...')`, `float('nan'/'inf')`) before anything runs.

## Isolation and CI security

- **Per-input timeout:** inputs run in a separate worker process, one at a time. The parent applies a time limit (1.0s by default) separately to the old call and the new call. A hang or a hard crash (a segfault or `os._exit`) kills only that worker. It's recorded as a regression for that input, and a fresh worker carries on with the next one. If a function hangs 3 times it's stopped early, so a broken PR can't keep CI busy.
- **Protocol hygiene:** the code under test gets a dummy stdin, and its stdout goes to stderr, so printing or calling `input()` can't corrupt the worker protocol.
- **Fair blame:** if the old version also hangs or crashes on an input, the PR isn't blamed for it.
- **Workflow hardening:** trigger on `pull_request`, never `pull_request_target`, as in the snippet above. PR code then runs without repository secrets and with a read-only token for forks; grant only `contents: read`, plus `pull-requests: write` for the comment. Inside the Action, third-party actions are pinned to commit SHAs and inputs reach shell steps only through environment variables, which prevents script injection.
- **Not a sandbox:** the code under test runs with the runner's own permissions. That's normal for CI on `pull_request`, but don't point this at code you wouldn't run anyway.

## Benchmarks

`python benchmarks/run_benchmarks.py` runs 10 realistic bug patches and 2 behaviour-preserving refactors, and writes [benchmarks/RESULTS.md](benchmarks/RESULTS.md).

| Metric | Mock mode |
|---|---|
| Bugs detected | 10/10 |
| False positives on clean refactors | 0/2 |
| Mean time per case | 0.22s |

The cases cover an off-by-one in pagination, an unhandled `None`, currency rounding drift, an auth bypass through a substring check, division by zero, a removed negative-quantity check, an empty collection, stricter percent parsing, a `>=` that became `>` at a boundary, and lost coupon-code normalisation. The benchmark was written alongside the generator, and two of its heuristics (numeric strings and lowercased literals) were added after the first run missed two cases. Treat it as the tool's regression suite, not an independent accuracy measure.

## Project layout

```
src/diff_parser.py   unified diff parsing, in-memory patch apply, git merge-base mode, AST context
src/fuzzer.py        input generation from annotations and AST constants, delta-debugging shrinker
src/evaluator.py     mock / LLM input selection, allow-list for model-proposed inputs
src/runner.py        worker pool, per-input timeout, crash containment, failure diagnosis
src/worker.py        stdlib-only subprocess that runs old and new code on one input at a time
src/reporter.py      Markdown PR comment
examples/            invoice module, the PR that breaks it, and the report it produces
benchmarks/          10 bug patches, 2 clean refactors, runner and results
tests/               the project's own pytest suite (30 tests), run in CI on Linux and Windows, Python 3.11 to 3.13
```

Developing: `pip install -e ".[dev]"`, then `pytest`. Architecture diagrams are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Limitations

- Only module-level synchronous functions are exercised. Methods, async functions and newly added functions are listed under "Not exercised" in the report.
- Inputs come from annotations. Parameters typed as custom classes fall back to generic values and get thin coverage.
- Intended behaviour changes are listed for confirmation. That's the cost of using the old code as the oracle, and it's why they don't fail the check.
