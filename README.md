# PR-Sentinel

PR-Sentinel tests the Python functions a pull request changes by running the old and new versions side by side on the same edge-case inputs. If the new code crashes on an input the old code handled, that's a regression and the check fails. If it returns a different result, it goes in a list for a reviewer to confirm. The output is a Markdown report sized for a PR comment.

## The problem

Most regressions get through review because nobody wrote a test for the input that breaks: a zero-dollar line item, an empty list, a `None` last name. Asking someone to write those tests by hand during review doesn't scale, and generated tests that only check "doesn't crash" can't tell you whether behaviour changed.

PR-Sentinel uses the code before the change as the oracle. Each changed function, and each function in the same module that calls it or reads a module constant the PR touched, runs in both versions with the same arguments. Any difference is reported along with the input that caused it.

## Quickstart

```bash
pip install -e .            # Python 3.11+
python -m src.cli demo      # runs against demo/sample_pr.patch, prints the report
```

Scan a branch the way CI does:

```bash
pr-sentinel scan --base main --head HEAD --output sentinel-report.md
```

Or scan a patch file offline (the patch is applied in memory, nothing on disk changes):

```bash
pr-sentinel scan --diff my-change.patch --repo . --output sentinel-report.md
```

Exit codes: `0` for no differences or behaviour changes only, `1` for a crash regression, `2` if the scan itself failed.

### LLM mode

```bash
pip install -e ".[llm]"
export ANTHROPIC_API_KEY=...          # or OPENAI_API_KEY + OPENAI_MODEL (+ OPENAI_BASE_URL)
pr-sentinel scan --base main --mode llm
```

In LLM mode the model gets each function's before/after source and proposes up to 15 targeted inputs per function. These are added to the deterministic set and go through the same differential harness. The model never writes test logic. Its inputs are parsed with `ast` and rejected unless they are plain literals or `Decimal('...')`, because they get executed.

## Sample report

From `python -m src.cli demo`. The PR caps discounts at 50% of the line amount, which means dividing by the line's gross amount, and that's zero for free items:

> **Regression found: changed code now crashes on inputs the old code handled.**
>
> | Risk | Files | Lines changed | Functions exercised | Cases | Failed | Time |
> |---|---|---|---|---|---|---|
> | **Low** (2.0) | 1 | 9 | 2 | 360 | 71 | 4.9s |
>
> | Function | File | Why it was tested | Cases | Passed | Crashes | Behaviour changes |
> |---|---|---|---|---|---|---|
> | `line_total` | `demo/invoice_engine.py:8` | changed | 300 | 229 | 35 | 36 |
> | `invoice_total` | `demo/invoice_engine.py:25` | calls line_total | 60 | 60 | 0 | 0 |
>
> #### `line_total`: DivisionByZero (33 cases)
>
> - **Where:** `demo/invoice_engine.py:16` (changed in this PR): `if discount / gross > MAX_DISCOUNT_RATE:`
> - **Input:** `line_total(Decimal('0'), 1, Decimal('19.99'))`
>   - old returned Decimal('0.00'); new raises decimal.DivisionByZero
> - **Suggested fix:** Guard the denominator: handle zero before dividing (the previous version returned a value here).

The 36 behaviour changes are the intended cap (for example `line_total(Decimal('19.99'), 1, Decimal('19.99'))` used to return `0.00` and now returns `10.00`). They are listed under "Behaviour changes to confirm" and don't fail the check.

## Benchmarks

The benchmark covers 10 realistic bug patches and 2 behaviour-preserving refactors, which are there to measure false positives. Run `python benchmarks/run_benchmarks.py` to reproduce. Full table: [benchmarks/RESULTS.md](benchmarks/RESULTS.md).

| Metric | Mock mode |
|---|---|
| Bugs detected | 9/10 (90%) |
| False positives on clean refactors | 0/2 |
| Mean time per case | 1.8s |

The table counts these as detected, but they aren't clean wins:

- `bug_08_percent_parsing` is missed. The bug only shows on inputs like `"12.5%"`, and the deterministic string candidates don't produce those. This is the kind of gap LLM mode is meant to fill.
- `bug_10_coupon_normalisation` is flagged because `None` is now accepted where it used to raise. The actual bug, lowercase coupon codes, isn't generated.
- `bug_04_auth_bypass` is caught both by the real issue (an empty role string now grants edit rights) and by a `TypeError` on non-string roles.

## How it works

`diff -> AST context -> case synthesis -> isolated pytest run -> diagnosis -> Markdown`. See [ARCHITECTURE.md](ARCHITECTURE.md).

- **Diff parser** (`src/diff_parser.py`): parses unified diffs and gets the old and new source from git (`merge-base..head`) or by applying a patch in memory. It then uses `ast` to find changed functions, functions that call them, and functions that read changed module constants.
- **Evaluator** (`src/evaluator.py`): builds inputs per parameter from type annotations, including null, zero, negative, empty and wrong-type values. It adds the numbers the code compares against ±1 (including module constants such as `FREE_SHIPPING_MIN`), the string literals it uses, and the defaults. Combinations are capped at 300 per function: one parameter is varied at a time from a normal baseline, and the rest are seeded random combinations.
- **Runner** (`src/runner.py`): writes the old and new modules and a parametrised pytest file to a temp directory, then runs pytest in a subprocess with plugins disabled and a hard timeout. It reads the JUnit XML, maps each crash back to its line in the new file, marks whether that line changed in the PR, and attaches a suggested fix based on the exception type.
- **Reporter** (`src/reporter.py`): writes the status badge, the risk score (lines touched, functions changed, branch count), the per-function table, grouped failures with example inputs and traces, and a list of what was skipped and why.

## GitHub Action

```yaml
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  sentinel:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: Tune-0-Matic/pr-sentinel@main
        # with: { mode: llm }  and set ANTHROPIC_API_KEY in env
```

The report goes to the job summary and is posted as a PR comment, updated on each push. This repository's own CI (`.github/workflows/ci.yml`) runs the unit tests, the demo and the benchmarks on Python 3.11 to 3.13, and runs PR-Sentinel on every pull request.

## Limitations

- Only module-level synchronous functions are exercised. Methods, async functions and newly added functions are listed under "Not exercised".
- Intended behaviour changes show up as "behaviour changes to confirm". That's the cost of using the old code as the oracle.
- The subprocess and timeout guard against hangs and crashes, but this is not a security sandbox. Run it only on code you would run in CI anyway.
- Inputs come from annotations. Parameters with custom classes fall back to generic values and get thin coverage.
