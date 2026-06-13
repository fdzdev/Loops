# flaky — flaky-test retirement

**Who it's for / what it saves you:** a dev team reclaims trust in CI — flaky tests made deterministic and proven green 30 times in a row, so nobody says "just re-run it" again.

Retire intermittently-failing tests by making them deterministic — without
letting the fix cheat by deleting or weakening the test.

## The verifier (the star)

A proposed fix is **confirmed only when both checks pass**, and the verdict
comes from running code and the Python parser — never from asking the model that
wrote the fix whether it worked.

1. **Determinism (runs the code).** Apply the fix to a *sandbox copy* of the
   target file, then run the one target test **N times (default 30)** in fresh
   pytest subprocesses. Confirm iff **all N pass**. A single flap — even on run
   30 — rejects. Fresh process per run resets module-level clock/RNG state, so a
   fix can't fake determinism by leaking state across runs.

2. **No weakening (AST check).** Compare the target test before vs. after the
   fix. The fix is rejected if it:
   - adds `@pytest.mark.skip` / `skipif` / `xfail`,
   - introduces a `pytest.skip(...)` / `pytest.xfail(...)` call,
   - reduces the number of `assert` statements, or
   - removes the test function (or produces source that doesn't parse).

Evidence in `Verdict.evidence`: the pass count (`passes`/`runs_requested`), the
before/after assertion counts, the markers before/after, the executor's stated
strategy, and a compact unified `diff` — enough to confirm in under a minute.
The verifier lives in [`verifier.py`](verifier.py) (`verify_fix`), isolated so
it's the first thing you read.

## How it works

- **Generator** parses a CI flaky-test report fixture
  ([`demo/flaky_report.json`](demo/flaky_report.json)) into one `Candidate` per
  flaky test. `dedupe_key` = the pytest **node id**, so a test that's already
  been fixed or rejected is never re-proposed.
- **Executor** (`llm.strong`) rewrites the whole target file to remove the
  nondeterministic input — freeze the clock, seed the RNG, or remove the race —
  keeping every assertion intact. Open-ended repair, so intelligence is spent
  here; the result is only a *proposal*.
- **Verifier** decides, deterministically (above).

## Why it pays

Flaky tests are a tax that compounds: they erode trust in CI, trigger pointless
reruns, and mask real regressions. Triaging them is tedious, parallelizable, and
exactly the kind of tireless work a loop is good at — propose a fix, prove it
green 30×, move to the next node id. The deterministic gate means a confirmed
fix is one a human can merge with a glance at the evidence, not re-audit.

## Gaming trap

The obvious cheat is to make a flaky test "pass" by gutting it. Every such move
fails verification automatically:

- **Delete the test** → `exists=False` → rejected.
- **`@pytest.mark.skip`/`skipif`/`xfail`** or **`pytest.skip()`/`xfail()`** →
  rejected by the marker/call gate. The gate matches the marker by its name, so
  aliasing the import (`from pytest import mark` → `@mark.skip`, or a bare
  `@skip`) does **not** evade it.
- **Remove an assertion** → assertion count drops → rejected.
- **A fix that's merely *usually* green** → one flap in the 30 runs → rejected.

Because the no-weakening gate is static (AST), it fires before a single test run
— a gutted test never even gets the benefit of the doubt. As defense in depth,
the run gate also drops a **guard `conftest.py`** into the sandbox that fails the
session unless exactly one test *genuinely passes*: pytest exits 0 for a skipped
or xfailed test, so without this guard a skip the AST somehow missed would count
as a green run. Both layers must agree before a fix is confirmed.

**Known limit (be honest about it).** The AST gate counts assertions; it does
*not* semantically diff the ones that survive. A fix that keeps the count but
rewrites `assert ms < 700` into a trivially-true `assert ms >= 0` would pass both
gates. Closing that needs to know each assertion's intent, which is out of scope
for a static check — so the unified `diff` is carried in `Verdict.evidence`
precisely so the human reviewer can spot a same-count rewrite at a glance before
merging.

## Runnable

**Out-of-the-box.** Ships a genuinely flaky demo target
([`demo/test_flaky_demo.py`](demo/test_flaky_demo.py)) with two tests that pass
~70% of the time (one clock-dependent, one RNG-dependent), plus the CI report
fixture. Needs only `ANTHROPIC_API_KEY` and `pytest`.

```bash
# see the flakiness for yourself (run a few times; it flips):
pytest loops/flaky/demo/test_flaky_demo.py

# run the loop:
python -m loops.flaky                 # default 30 runs per test, $2 budget ceiling
python -m loops.flaky --runs 50       # stricter determinism bar
```

State (seen / confirmed / rejected JSONL) lands under `loops/flaky/.runs/flaky`
by default; re-running resumes from there.
