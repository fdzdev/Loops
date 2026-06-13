# finmodel — rubric-graded financial-model builder

**Who it's for / what it saves you:** an analyst or founder gets a multi-year financial model as a real `.xlsx` whose every number reconciles against an independent Python recompute, so it's sendable instead of a draft to re-check by hand.

A loop that builds multi-year financial models as real `.xlsx` files and only
keeps the ones whose numbers **reconcile against an independent recompute** and
that **pass every rubric criterion as a concrete boolean**.

## The verifier (the product)

`verifier.py` is deterministic. It contains no LLM call and touches no network —
it must not depend on the thing it judges. For each candidate it:

1. **Recomputes every figure in plain Python** straight from the assumptions
   (revenue, COGS, gross profit, opex, operating income, tax, net income, per-year
   margins, and the cross-year revenue total). This is the ground truth.
2. **Reads the `.xlsx`** the executor built and **reconciles it cell by cell**
   against the recompute. Any cell that disagrees by more than a cent is a
   reconciliation failure; the offending `{row, year_index, expected, got}` lands
   in `Verdict.evidence["reconcile_diffs"]`.
3. **Checks accounting identities** on the recompute itself — `net = (revenue -
   COGS - opex) - tax` and `0 <= gross margin <= 1`, **always**, independent of the
   rubric — so a self-consistent but *wrong* recompute, or a task whose rubric
   simply forgets to ask, still can't pass. (Further sheet-internal identities such
   as `Total Revenue == sum of yearly revenue` are available as rubric criteria.)
4. **Grades each rubric criterion** by mapping its name to a pure boolean in
   `RUBRIC_CHECKS` (e.g. `has_total_revenue_row`, `years_contiguous`,
   `gross_margin_in_unit_interval`, `net_income_identity`). Per-criterion results
   go into `Verdict.evidence["rubric_results"]`.

**Confirmed iff** the spreadsheet reconciles clean AND every requested rubric
criterion passes. The evidence dict carries the reconciliation diffs, the
per-criterion booleans, and the recompute's headline figures, so a human can
confirm the verdict in under a minute.

The model that *proposes* the assumptions never gets a say in whether the build
passed — the verdict is arithmetic.

## Why it pays

- **Tirelessness / direct revenue.** Building an operating model and tying out
  every total by hand is slow, error-prone analyst work. This loop proposes,
  builds, and reconciles models unattended, keeping only the ones that tie out.
- **Trust.** The output isn't "the model says it's right" — it's a spreadsheet
  that provably reconciles against an independent Python recompute, with the diffs
  attached. That's the difference between a draft and something you can send.
- **Parallelism.** Each candidate is independent; the loop dedupes on the hash of
  the assumptions so the same numbers are never re-built or re-graded.

## The gaming trap

A vague rubric makes a noisy loop: "has good margins", "looks reasonable", "totals
are correct" can all be argued into a pass. Two guards close that off:

- **Every rubric criterion is a concrete, code-checkable boolean.** A criterion
  the task names but `RUBRIC_CHECKS` can't map to code is reported as
  *un-gradeable* and **fails** — you cannot pass a vague rubric, and you cannot
  invent a flattering criterion the verifier doesn't know how to refute.
- **The numbers must reconcile against an independent recompute.** The executor
  never writes the figures; the loop's own builder code (`loop._derive_rows`)
  derives them. The verifier re-derives every figure from scratch with its *own*
  separate code (`verifier.recompute`) — the builder does **not** import or share
  the verifier's math — and compares cell by cell. So a bug in the builder's
  arithmetic, or a spreadsheet that merely *looks* right but whose totals don't add
  up, loses on the reconciliation diff regardless of the rubric. (If the two shared
  one math function, reconciliation would be the verifier checking a serialized
  copy of itself and could never catch a builder bug — they are kept separate on
  purpose.)

So the only way to pass is to produce a spreadsheet whose every cell equals the
independent recompute and whose structure satisfies every boolean — which is
exactly a correct financial model.

## Runnable

**Out-of-the-box**, with the `finmodel` extra (it needs `openpyxl`):

```bash
pip install -e '.[finmodel]'
export ANTHROPIC_API_KEY=...        # the generator/executor uses llm.strong
python -m loops.finmodel
```

It builds `.xlsx` files under `.runs/finmodel/models/` and prints which builds
reconciled and passed the rubric. Override the run directory with
`FINMODEL_RUN_DIR`. No network at import time; the only network is the generator's
`llm.strong` call inside `generate()`. The verifier itself is pure arithmetic plus
an `openpyxl` read.
