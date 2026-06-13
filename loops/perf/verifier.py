"""The verifier: the product of this loop.

A proposed optimization is CONFIRMED iff, measured by code the candidate cannot
touch:

  1. CORRECTNESS — the candidate produces identical results to the baseline on
     every table case AND passes every property test. One mismatch => rejected.
  2. SPEED — the candidate's *median* runtime over >= MIN_RUNS warmed-up runs is
     more than IMPROVEMENT_THRESHOLD (default 10%) faster than the baseline's
     median, measured in the same process, back to back.

How the anti-gaming guarantee is enforced (see README "Gaming trap"):

  * The candidate only supplies a function body. The harness that imports it,
    runs the loop-owned tests, and times it is written fresh by THIS file into a
    temp directory the candidate never sees. The candidate cannot weaken a test,
    swap the baseline, stub `time`, or short-circuit `make_inputs`.
  * Correctness is checked against the baseline's own output, not against numbers
    the model claimed.
  * The speedup is MEASURED here, never read from anything the model said.
  * Noise guard: warm-up runs are discarded, we take the MEDIAN (not the min or
    mean) of an odd number of runs, baseline and candidate are timed in the same
    subprocess invocation under the same interpreter, and we require the gain to
    clear a threshold rather than just be positive.
  * The candidate runs in a separate process with a wall-clock timeout, so an
    accidental infinite loop or a fork bomb can't wedge the parent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING

from agentloops import Verdict

if TYPE_CHECKING:  # avoid import cycles / keep import-time cheap
    from .targets import Target

# Tunables. Conservative defaults: median of >= 5 runs, 2 warm-ups discarded,
# require a real >10% win, kill anything that runs longer than the timeout.
MIN_RUNS = 5
WARMUP_RUNS = 2
IMPROVEMENT_THRESHOLD = 0.10  # candidate median must be >10% below baseline median
SUBPROCESS_TIMEOUT_S = 60.0


# The benchmark harness. Written verbatim into a temp file and executed by a
# fresh interpreter. It owns the timer, the baseline, the tests and the inputs;
# the candidate is imported as data. It prints a single JSON line to stdout.
#
# Note the deliberate separation: baseline and candidate live in their own
# modules, both timed by THIS harness using time.perf_counter and statistics
# .median. The candidate file is pure function source — it is never trusted to
# report its own timing.
_HARNESS = r'''
import json, statistics, sys, time, importlib.util

MIN_RUNS = {min_runs}
WARMUP_RUNS = {warmup_runs}
FN_NAME = {fn_name!r}

def _load(path, modname):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def _bench(fn, inputs):
    # Warm up (JIT-free CPython still benefits: caches, branch prediction, and
    # it lets a first-call import cost fall outside the measured window).
    for _ in range(WARMUP_RUNS):
        for args in inputs:
            fn(*args)
    samples = []
    for _ in range(MIN_RUNS):
        t0 = time.perf_counter()
        for args in inputs:
            fn(*args)
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples), samples

def main():
    baseline_mod = _load({baseline_path!r}, "baseline_mod")
    tests_mod    = _load({tests_path!r}, "tests_mod")
    inputs_mod   = _load({inputs_path!r}, "inputs_mod")
    candidate_mod = _load({candidate_path!r}, "candidate_mod")

    base_fn = getattr(baseline_mod, FN_NAME)
    cand_fn = getattr(candidate_mod, FN_NAME, None)
    if cand_fn is None:
        print(json.dumps({{"ok": False, "stage": "load",
            "error": "candidate did not define a function named " + FN_NAME}}))
        return

    # ---- Correctness: identical to baseline on every table case. ----
    for i, (args, expected) in enumerate(tests_mod.TEST_CASES):
        try:
            base_out = base_fn(*args)
        except Exception as e:
            print(json.dumps({{"ok": False, "stage": "baseline_case",
                "error": "baseline raised on case %d: %r" % (i, e)}}))
            return
        # Compare baseline to the declared expectation (sanity on the oracle).
        # Equality is exact and type-aware: a drop-in replacement must reproduce
        # the baseline's exact return value (list==list, int==int). Works for
        # scalar AND sequence returns; no list() coercion that would explode on
        # an int.
        if base_out != expected:
            print(json.dumps({{"ok": False, "stage": "oracle",
                "error": "baseline disagrees with expected on case %d" % i}}))
            return
        try:
            cand_out = cand_fn(*args)
        except Exception as e:
            print(json.dumps({{"ok": False, "stage": "correctness",
                "error": "candidate raised on case %d (%r): %r" % (i, args, e)}}))
            return
        # Compare the candidate to the BASELINE's own output, not just to the
        # declared `expected`. The oracle gate above already proved
        # base_out == expected, so this is at least as strict, and it makes the
        # README's guarantee literally true: a drop-in replacement must
        # reproduce the baseline's exact return value.
        if cand_out != base_out:
            print(json.dumps({{"ok": False, "stage": "correctness",
                "error": "case %d: candidate=%r baseline=%r" % (i, cand_out, base_out)}}))
            return

    # ---- Property tests (loop-owned, random, seeded). ----
    if hasattr(tests_mod, "check"):
        try:
            tests_mod.check(cand_fn)
        except AssertionError as e:
            print(json.dumps({{"ok": False, "stage": "property",
                "error": "property test failed: %s" % e}}))
            return
        except Exception as e:
            print(json.dumps({{"ok": False, "stage": "property",
                "error": "property test errored: %r" % e}}))
            return

    # ---- Speed: median of warmed-up runs, both timed here. ----
    inputs = inputs_mod.make_inputs()
    base_median, base_samples = _bench(base_fn, inputs)
    cand_median, cand_samples = _bench(cand_fn, inputs)

    speedup = (base_median / cand_median) if cand_median > 0 else float("inf")
    improvement = (base_median - cand_median) / base_median if base_median > 0 else 0.0

    print(json.dumps({{
        "ok": True,
        "baseline_median_s": base_median,
        "candidate_median_s": cand_median,
        "baseline_samples_s": base_samples,
        "candidate_samples_s": cand_samples,
        "speedup_x": speedup,
        "improvement_frac": improvement,
        "runs": MIN_RUNS,
        "warmup": WARMUP_RUNS,
    }}))

main()
'''


def verify_candidate(target: "Target", candidate_src: str) -> Verdict:
    """Run `candidate_src` (which must define a function named target.name) in a
    fresh subprocess against the loop-owned tests and benchmark.

    Returns a Verdict whose evidence is enough to confirm the result by eye:
    baseline vs candidate median timing, the raw samples, the measured speedup,
    and the correctness outcome.
    """
    with tempfile.TemporaryDirectory(prefix="perf_verify_") as tmp:
        baseline_path = os.path.join(tmp, "baseline_mod.py")
        tests_path = os.path.join(tmp, "tests_mod.py")
        inputs_path = os.path.join(tmp, "inputs_mod.py")
        candidate_path = os.path.join(tmp, "candidate_mod.py")
        harness_path = os.path.join(tmp, "harness.py")

        with open(baseline_path, "w") as f:
            f.write(target.baseline_src)
        with open(tests_path, "w") as f:
            f.write(target.tests_src)
        with open(inputs_path, "w") as f:
            f.write(target.inputs_src)
        with open(candidate_path, "w") as f:
            f.write(candidate_src)
        with open(harness_path, "w") as f:
            f.write(
                _HARNESS.format(
                    min_runs=MIN_RUNS,
                    warmup_runs=WARMUP_RUNS,
                    fn_name=target.name,
                    baseline_path=baseline_path,
                    tests_path=tests_path,
                    inputs_path=inputs_path,
                    candidate_path=candidate_path,
                )
            )

        try:
            proc = subprocess.run(
                [sys.executable, harness_path],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_S,
                cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            return Verdict(
                confirmed=False,
                evidence={"target": target.name, "timeout_s": SUBPROCESS_TIMEOUT_S},
                reason=f"candidate exceeded {SUBPROCESS_TIMEOUT_S:.0f}s wall-clock timeout "
                "(likely slower than baseline or stuck in a loop)",
            )

        if proc.returncode != 0:
            return Verdict(
                confirmed=False,
                evidence={
                    "target": target.name,
                    "returncode": proc.returncode,
                    "stderr": proc.stderr.strip()[-2000:],
                },
                reason="candidate subprocess crashed (syntax error or runtime exception)",
            )

        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        try:
            result = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return Verdict(
                confirmed=False,
                evidence={
                    "target": target.name,
                    "stdout": proc.stdout.strip()[-2000:],
                    "stderr": proc.stderr.strip()[-2000:],
                },
                reason="harness produced no parseable result",
            )

        if not result.get("ok"):
            return Verdict(
                confirmed=False,
                evidence={"target": target.name, **result},
                reason=f"{result.get('stage', 'unknown')}: {result.get('error', 'failed')}",
            )

        improvement = float(result["improvement_frac"])
        speedup = float(result["speedup_x"])
        base_ms = result["baseline_median_s"] * 1000.0
        cand_ms = result["candidate_median_s"] * 1000.0

        evidence = {
            "target": target.name,
            "correctness": "all table cases + property tests passed",
            "baseline_median_ms": round(base_ms, 4),
            "candidate_median_ms": round(cand_ms, 4),
            "speedup_x": round(speedup, 3),
            "improvement_pct": round(improvement * 100, 2),
            "threshold_pct": round(IMPROVEMENT_THRESHOLD * 100, 2),
            "runs": result["runs"],
            "warmup_discarded": result["warmup"],
            "baseline_samples_s": result["baseline_samples_s"],
            "candidate_samples_s": result["candidate_samples_s"],
        }

        if improvement > IMPROVEMENT_THRESHOLD:
            return Verdict(
                confirmed=True,
                evidence=evidence,
                reason=(
                    f"correct AND {improvement * 100:.1f}% faster "
                    f"({speedup:.2f}x): baseline {base_ms:.2f}ms -> candidate {cand_ms:.2f}ms "
                    f"(median of {result['runs']} runs, >{IMPROVEMENT_THRESHOLD * 100:.0f}% threshold)"
                ),
            )

        return Verdict(
            confirmed=False,
            evidence=evidence,
            reason=(
                f"correct but only {improvement * 100:.1f}% faster "
                f"(needs >{IMPROVEMENT_THRESHOLD * 100:.0f}%): "
                f"baseline {base_ms:.2f}ms vs candidate {cand_ms:.2f}ms"
            ),
        )


__all__ = ["verify_candidate", "IMPROVEMENT_THRESHOLD", "MIN_RUNS", "WARMUP_RUNS"]
