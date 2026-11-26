## PR-Sentinel report

![PR-Sentinel](https://img.shields.io/badge/PR--Sentinel-regression-red)

**Regression found: changed code crashes or hangs on inputs the old code handled.**

| Risk | Files | Lines changed | Functions exercised | Cases | Failed | Time |
|---|---|---|---|---|---|---|
| **Low** (2.0) | 1 | 9 | 2 | 388 | 72 | 0.33s |

### Generated edge-case tests

| Function | File | Why it was tested | Cases | Passed | Crashes / hangs | Behaviour changes |
|---|---|---|---|---|---|---|
| `line_total` | `examples/invoice_engine.py:8` | changed | 300 | 228 | 29 | 43 |
| `invoice_total` | `examples/invoice_engine.py:25` | calls line_total | 88 | 88 | 0 | 0 |

### Regressions

#### `line_total`: DivisionByZero (24 cases)

- **Where:** `examples/invoice_engine.py:16` (changed in this PR): `if discount / gross > MAX_DISCOUNT_RATE:`
- **Minimal reproducer:** `line_total(Decimal('0'), 0, Decimal('1'))` (shrunk from `line_total(Decimal('0'), 1, Decimal('19.99'))`)
- **Input:** `line_total(Decimal('0'), 1, Decimal('19.99'))`
  - old returned Decimal('0.00'); new raises DivisionByZero: [<class 'decimal.DivisionByZero'>]
- **Input:** `line_total(Decimal('19.99'), 0, Decimal('19.99'))`
  - old returned Decimal('0.00'); new raises DivisionByZero: [<class 'decimal.DivisionByZero'>]
- **Input:** `line_total(Decimal('0.01'), 0, Decimal('0.005'))`
  - old returned Decimal('0.00'); new raises DivisionByZero: [<class 'decimal.DivisionByZero'>]
- ...and 21 more input(s)
- **Suggested fix:** Guard the denominator: handle zero before dividing (the previous version returned a value here).

<details><summary>Failure trace</summary>

```
examples/invoice_engine.py:16 in line_total
    if discount / gross > MAX_DISCOUNT_RATE:
DivisionByZero: [<class 'decimal.DivisionByZero'>]
```

</details>

#### `line_total`: InvalidOperation (4 cases)

- **Where:** `examples/invoice_engine.py:16` (changed in this PR): `if discount / gross > MAX_DISCOUNT_RATE:`
- **Minimal reproducer:** `line_total(Decimal('0'), 0, Decimal('0'))` (shrunk from `line_total(Decimal('0'), 10**6, Decimal('0'))`)
- **Input:** `line_total(Decimal('0'), 10**6, Decimal('0'))`
  - old returned Decimal('0.00'); new raises InvalidOperation: [<class 'decimal.DivisionUndefined'>]
- **Input:** `line_total(Decimal('-0.99'), 0, Decimal('0'))`
  - old returned Decimal('-0.00'); new raises InvalidOperation: [<class 'decimal.DivisionUndefined'>]
- **Input:** `line_total(Decimal('0.5'), 0, Decimal('0'))`
  - old returned Decimal('0.00'); new raises InvalidOperation: [<class 'decimal.DivisionUndefined'>]
- ...and 1 more input(s)
- **Suggested fix:** Guard the denominator: handle zero before dividing (the previous version returned a value here).

<details><summary>Failure trace</summary>

```
examples/invoice_engine.py:16 in line_total
    if discount / gross > MAX_DISCOUNT_RATE:
InvalidOperation: [<class 'decimal.DivisionUndefined'>]
```

</details>

#### `line_total`: InvalidOperation (1 case)

- **Where:** `examples/invoice_engine.py:18` (not changed, but reached): `return (gross - discount).quantize(CENT, rounding=ROUND_HALF_UP)`
- **Minimal reproducer:** `line_total(Decimal('-0.5'), 0, Decimal('Infinity'))` (shrunk from `line_total(Decimal('-0.5'), 10**6, Decimal('Infinity'))`)
- **Input:** `line_total(Decimal('-0.5'), 10**6, Decimal('Infinity'))`
  - old returned Decimal('0.00'); new raises InvalidOperation: [<class 'decimal.InvalidOperation'>]
- **Suggested fix:** A Decimal operation became invalid for this input (often Infinity or NaN reaching quantize or a comparison); check how the new code handles non-finite values.

<details><summary>Failure trace</summary>

```
examples/invoice_engine.py:18 in line_total
    return (gross - discount).quantize(CENT, rounding=ROUND_HALF_UP)
InvalidOperation: [<class 'decimal.InvalidOperation'>]
```

</details>

### Behaviour changes to confirm

#### `line_total`: different result (43 cases)

- **Minimal reproducer:** `line_total(Decimal('1'), 1, Decimal('1'))` (shrunk from `line_total(Decimal('19.99'), 1, Decimal('19.99'))`)
- **Input:** `line_total(Decimal('19.99'), 1, Decimal('19.99'))`
  - old returned Decimal('0.00'); new returns Decimal('10.00')
- **Input:** `line_total(Decimal('-1'), 1, Decimal('19.99'))`
  - old returned Decimal('0.00'); new returns Decimal('-20.99')
- **Input:** `line_total(Decimal('1'), 1, Decimal('19.99'))`
  - old returned Decimal('0.00'); new returns Decimal('0.50')
- ...and 40 more input(s)
- **Suggested fix:** Output changed for this input. If unintended, restore the previous logic; if intended, update callers and tests.

<sub>Differential testing: each changed function (and its callers in the same module) runs old and new on identical inputs. Crashes and hangs block; behaviour changes need a human to confirm.</sub>
