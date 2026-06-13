# perf — performance-regression hunting / optimization

**Who it's for / what it saves you:** a backend engineer gets measured speedups on hot functions with before/after timing proof, not vibes — continuous optimization no human sustains by hand.

Pick a slow function, ask a strong model for a faster version, and **confirm the
speedup by measuring it** — never by believing the model.

## The verifier (the product)

`verifier.py :: verify_candidate(target, candidate_src)` confirms an optimization
**iff both** of these hold, decided entirely by code the candidate cannot touch:

1. **Correctness.** The candidate's output is identical to the baseline's on
   every table case, and it passes every seeded property test (e.g. for
   `dedupe`: no duplicates, same value set, first-seen order preserved). One
   mismatch → rejected.
2. **Speed.** The candidate's **median** runtime over `MIN_RUNS = 5` warmed-up
   runs is **more than `IMPROVEMENT_THRESHOLD = 10%`** below the baseline's
   median.

How it runs: the verifier writes the baseline, the loop-owned tests, the
inputs, and the candidate into four separate files in a throwaway temp dir, then
executes a **harness it generates itself** in a **fresh subprocess** (with a
60s wall-clock timeout). The harness owns `time.perf_counter`, discards 2
warm-up runs, times baseline and candidate back-to-back in the same
interpreter, and prints one JSON line. The parent reads timing from that line —
not from anything the model said.

**Evidence** (in `confirmed.jsonl`, reviewable in under a minute):

```json
{
  "target": "dedupe",
  "correctness": "all table cases + property tests passed",
  "baseline_median_ms": 142.31,
  "candidate_median_ms": 0.93,
  "speedup_x": 153.0,
  "improvement_pct": 99.35,
  "threshold_pct": 10.0,
  "runs": 5,
  "warmup_discarded": 2,
  "baseline_samples_s": [0.142, 0.143, ...],
  "candidate_samples_s": [0.0009, 0.0010, ...]
}
```

## Why it pays

- **Tirelessness.** Optimization is grind: profile, rewrite, re-measure, prove
  you didn't break anything. The loop does that for every registered target
  every round, unattended, and only surfaces wins that are *proven* faster and
  *proven* correct.
- **Latency / cost.** An O(n²) → O(n) win on a hot path is real money. The loop
  finds those and hands you a diff plus the timing proof to justify the merge.
- **Parallelism.** Add functions to the registry in `targets.py`; the loop works
  the whole crop. The dedupe key (`name@baseline_hash`) means each baseline is
  attempted once and a verdict is never re-litigated — but change the baseline
  and the problem reopens automatically.

## The gaming trap (and how it's shut)

An optimizer under loop pressure will happily "win" by cheating. Each escape is
closed in code:

- *"Just claim it's faster."* The model's claimed speedup is recorded as
  `approach_claimed` and **ignored**. The verdict comes only from measured
  `time.perf_counter` deltas in the subprocess.
- *"Weaken or delete a test."* Impossible. The candidate supplies only the
  function source; the tests, the baseline, the inputs, and the timer live in
  loop-owned files written fresh per run. The candidate is imported as data and
  never sees them.
- *"Return the wrong answer fast."* Correctness is checked against the
  baseline's own output **before** timing; any mismatch rejects.
- *"Stub the timer / short-circuit the inputs."* The candidate runs in a
  separate process and cannot reach back into the harness's `time` or
  `make_inputs`.
- *"Win on noise."* Warm-ups are discarded, we take the **median** of ≥5 runs
  (not the min/mean), baseline and candidate are timed in the same process
  under the same interpreter, and the gain must clear a **>10% threshold** — a
  1–2% jitter never confirms.
- *"Hang to dodge the comparison."* A 60s subprocess timeout rejects anything
  that doesn't finish (which also catches "optimizations" that are slower).

## Runnable status: **out-of-the-box**

Ships two demo targets (O(n²) order-preserving `dedupe`, O(n²) `count_pairs`)
with correctness tests and benchmark inputs. Standard library only.

```bash
export ANTHROPIC_API_KEY=sk-...
python -m loops.perf
```

Env knobs: `PERF_RUN_DIR`, `PERF_MAX_USD` (default 2.0), `PERF_MAX_ROUNDS`
(default 10). Tune `MIN_RUNS`, `WARMUP_RUNS`, `IMPROVEMENT_THRESHOLD`,
`SUBPROCESS_TIMEOUT_S` in `verifier.py`. Add targets in `targets.py`.
```
