# etl — data-pipeline self-healing loop

**Who it's for / what it saves you:** a data engineer gets a broken nightly feed fixed before standup, with the revenue total reconciled to a trusted reference so the feed never "heals" into silent corruption.

A messy input lands; the production transform chokes on it. Instead of paging a
human, the loop detects the broken input, has a strong model propose a repaired
transform, and **proves the repair is correct with deterministic invariants** —
including a reconciling total, so "it didn't crash" can never be mistaken for
"it's right".

## The verifier (the star)

`verifier.py` runs the proposed transform on the messy bytes and confirms the
candidate **iff all four invariants pass**. No model grades the result — only
this code does.

1. **Schema** — output columns equal the canonical `OUTPUT_SCHEMA`, exactly and
   in order: `order_id, region, units, unit_price, revenue`.
2. **Row reconciliation** — `input_rows == output_rows + documented_drops`. The
   demo input has exactly one voided order, so an output of 10 rows from 11 input
   rows reconciles; 9 rows means a real row was silently dropped and verification
   **fails**.
3. **Required non-null** — every row has a non-empty `order_id, region, units,
   revenue`.
4. **Total reconciliation** — `sum(revenue)` over the output must equal the
   trusted reference total (`336.49`) within `RECONCILE_TOLERANCE` (`0.01`).

The verdict comes only from running the transform. **The model that writes the
transform never gets to say whether it worked.** Evidence in the `Verdict`
records each invariant's pass/fail with detail, plus the reconciled
`output_total` vs `reference_total`, the row counts, the output schema, a sample
of rows, and the exact `transform_body` the model produced — a human confirms it
in under a minute.

```
[PASS] schema: got [...], want [...]
[PASS] row_reconcile: input_rows(11) - documented_drops(1) = 10, output_rows = 10
[PASS] required_non_null: all required columns populated
[PASS] total_reconcile: output_total=336.49 vs reference_total=336.49 (|delta|=0.0 <= tol=0.01)
```

## Why it pays

- **Tirelessness** — a pipeline that heals its own malformed inputs at 3am
  instead of failing a nightly job and waiting for an on-call engineer.
- **Latency** — minutes from "bad file arrived" to "ingested and reconciled,"
  not a next-business-day ticket.
- **Direct revenue / trust** — the reconciling total means healed data is *safe*
  to load, not just non-crashing. A pipeline you can trust unattended is the
  difference between automation and a future incident.
- **Parallelism** — one candidate per broken input; many bad files heal at once,
  each independently proven.

## The gaming trap

Loose invariants let a model "heal" an input into **silent corruption**: drop the
rows it can't parse, zero out a stubborn field, coerce a bad number to `0`, and
declare victory because the job no longer throws. Every one of those moves the
**reconciling total** off the trusted reference, so invariant 4 fails. Row-count
reconciliation (invariant 2) closes the other escape hatch: you can't quietly
drop rows, because dropped rows must be *documented* drops, not silent ones.
The bar is never "didn't crash" — it's "the money still adds up."

The transform is handed only stdlib parsing tools (`csv`, `io`, `re`) in its
namespace, and — more to the point — the verdict depends solely on a *local*
reconciliation against the bundled reference. There is nothing to "phone home"
for: even a transform that reached the network couldn't fetch a number that
makes a wrong output reconcile to `reference_total`. It has to actually clean
the bytes it was given. (The exec namespace is a demo convenience, not a
security sandbox: builtins are available, so don't run untrusted bodies here as
if they were jailed — the reconciling total, not the namespace, is the guard.)

## Runnable status

**Runnable — out of the box.** `python -m loops.etl` heals the bundled messy CSV
with only `ANTHROPIC_API_KEY` set (plus the library's `anthropic` / `pydantic`
deps). The demo target is pure standard library:

- `demo/reference.csv` — the clean, trusted table (defines the reconciling total).
- `demo/inputs/sales_messy.csv` — a messy variant: `;`-delimited, renamed and
  whitespace-padded headers, `$`-prefixed amounts, mixed casing, and one `VOID`
  row that must be dropped.
- `demo/spec.py` — the canonical schema, invariants, and the naive
  `current_transform` that works on the reference and breaks on the messy file.

Config via env: `ETL_MAX_USD` (budget ceiling, default `1.0`), `ETL_RUN_DIR`
(state dir, default `loops/etl/.run`), and you can point the generator at your
own `inputs_dir` through `build(...)`. Drop a real malformed export into
`demo/inputs/` and it becomes the next candidate.
```
python -m loops.etl
```
