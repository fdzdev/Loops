"""The verifier — the star of the loop, isolated so it's the first thing you read.

A flaky-test "fix" is only trustworthy if BOTH hold:

  1. DETERMINISM. Apply the proposed fix to the target file and run the one
     target test N times (default 30) in fresh subprocess pytest invocations.
     Confirm only if ALL N runs pass. One flap, even at run 30, rejects.

  2. NO WEAKENING. A fix that makes the test pass by gutting it is the obvious
     way to game this loop, so we forbid it with a static (AST) comparison of
     the target test before and after the fix:
       - no `@pytest.mark.skip` / `skipif` / `xfail` may be added to the test,
       - the number of `assert` statements may not drop,
       - no `pytest.skip(...)` / `pytest.xfail(...)` call may be introduced,
       - the test function must still exist.
     Deleting the test, skipping it, or removing an assertion all fail here
     regardless of how green the runs look.

     Scope note: this gate is structural. It catches a *dropped* assertion (the
     count falls) but does NOT semantically diff what survives — rewriting
     `assert x < 700` to a trivially-true `assert x >= 0` keeps the count at 1
     and would pass. Detecting that needs to know each assertion's intent, which
     is out of scope here; the human reviewer reads the `diff` in the evidence to
     catch a same-count rewrite before merging.

The verdict comes from running code and from the compiler's own parser, never
from asking the model that wrote the fix whether it worked. The evidence dict
carries the pass count and the before/after assertion summary so a human can
confirm the result in under a minute.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from agentloops import Verdict


# ---------------------------------------------------------------------------
# Static "did the fix weaken the test?" analysis.
# ---------------------------------------------------------------------------

# Decorator markers that disable or soften a test. Any of these appearing on the
# target test in the FIXED source (but not the original) is gaming.
_WEAKENING_MARKERS = ("skip", "skipif", "xfail")
# Call-form escapes: pytest.skip(...) / pytest.xfail(...) inside the body.
_WEAKENING_CALLS = ("skip", "xfail")


@dataclass
class TestShape:
    """The auditable shape of one test function, extracted via AST."""

    exists: bool
    assert_count: int  # number of `assert` statements
    markers: tuple[str, ...]  # pytest.mark.<name> decorators present
    has_skip_xfail_call: bool  # pytest.skip()/xfail() in the body


def _decorator_marker_names(func: ast.FunctionDef) -> tuple[str, ...]:
    """Weakening-marker names among `func`'s decorators (e.g. 'skip', 'xfail').

    Conservative on purpose: we flag the marker by its *trailing* name no matter
    how the `mark` namespace is reached. `@pytest.mark.skip`, `@mark.skip` (after
    `from pytest import mark`), and a bare `@skip`/`@xfail` import all resolve to
    the same name and are all caught. An adversary aliasing the import is exactly
    the bypass a `.mark.`-anchored check would miss, so we don't anchor on it; we
    only report names in `_WEAKENING_MARKERS`, so an unrelated decorator (e.g.
    `@pytest.mark.parametrize`) is ignored.
    """
    names: list[str] = []
    for dec in func.decorator_list:
        # Strip a trailing call: @pytest.mark.skipif(...) -> the attribute chain.
        node = dec.func if isinstance(dec, ast.Call) else dec
        # The decorator's final name: the attr of an Attribute, or a bare Name id.
        if isinstance(node, ast.Attribute):
            final = node.attr
        elif isinstance(node, ast.Name):
            final = node.id
        else:
            continue
        if final in _WEAKENING_MARKERS:
            names.append(final)
    return tuple(names)


def _has_skip_xfail_call(func: ast.FunctionDef) -> bool:
    """True if the body calls skip(...)/xfail() in any reachable form.

    Catches both `pytest.skip(...)` (Attribute) and a bare `skip(...)` reached
    via `from pytest import skip` (Name) — the same alias bypass we close on the
    decorator side.
    """
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in _WEAKENING_CALLS:
            return True
        if isinstance(fn, ast.Name) and fn.id in _WEAKENING_CALLS:
            return True
    return False


def _find_test(tree: ast.AST, test_name: str) -> ast.FunctionDef | None:
    """Find the top-level (or class-nested) test function by name."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == test_name:
                return node  # type: ignore[return-value]
    return None


def shape_of(source: str, test_name: str) -> TestShape:
    """Extract the auditable shape of `test_name` from `source`.

    A syntax error (or a missing function) yields `exists=False`, which the
    verifier treats as a failed fix — a fix that doesn't even parse is not a fix.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return TestShape(exists=False, assert_count=0, markers=(), has_skip_xfail_call=False)
    func = _find_test(tree, test_name)
    if func is None:
        return TestShape(exists=False, assert_count=0, markers=(), has_skip_xfail_call=False)
    assert_count = sum(1 for n in ast.walk(func) if isinstance(n, ast.Assert))
    return TestShape(
        exists=True,
        assert_count=assert_count,
        markers=_decorator_marker_names(func),
        has_skip_xfail_call=_has_skip_xfail_call(func),
    )


def weakening_reason(before: TestShape, after: TestShape) -> str | None:
    """Return a human reason if `after` weakens `before`, else None.

    This is the anti-gaming gate. Written so an adversary who reads it cannot
    satisfy the loop by deleting, skipping, or loosening the test.
    """
    if not after.exists:
        return "fixed source does not parse or the test function was removed"
    added_markers = tuple(
        m for m in after.markers if m in _WEAKENING_MARKERS and m not in before.markers
    )
    if added_markers:
        return f"fix added disabling marker(s): {', '.join(added_markers)}"
    if after.has_skip_xfail_call and not before.has_skip_xfail_call:
        return "fix introduced a pytest.skip()/xfail() call in the test body"
    if after.assert_count < before.assert_count:
        return (
            f"fix reduced assertions from {before.assert_count} to "
            f"{after.assert_count} (assertion removed/loosened)"
        )
    return None


# ---------------------------------------------------------------------------
# Deterministic execution: apply the fix and run the test N times.
# ---------------------------------------------------------------------------


def _diff_summary(before: str, after: str) -> str:
    """A compact unified diff for the evidence dict (capped so it stays skimmable)."""
    import difflib

    diff = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="original",
            tofile="fixed",
            lineterm="",
            n=2,
        )
    )
    if len(diff) > 120:
        diff = diff[:120] + [f"... (+{len(diff) - 120} more diff lines)"]
    return "\n".join(diff)


# A sandbox conftest that turns "soft green" outcomes into hard failures. pytest
# exits 0 for a skipped or xfailed test, so a `returncode == 0` check alone counts
# a skip as a pass — which is exactly how a gamed fix sneaks through the run gate.
# This hook fails the session unless EXACTLY ONE test ran and it genuinely passed:
# skipped, xfailed, errored, or no-tests-collected all force a nonzero exit.
_GUARD_CONFTEST = '''\
import pytest

_outcomes = []

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    if report.when == "call":
        _outcomes.append(report.outcome)  # "passed" | "failed" | "skipped"
    elif report.when == "setup" and report.outcome == "skipped":
        # skip via marker/fixture short-circuits at setup, never reaching "call"
        _outcomes.append("skipped")

def pytest_sessionfinish(session, exitstatus):
    # Demand one genuine pass; anything else (skip/xfail/error/empty) is not a pass.
    if _outcomes != ["passed"]:
        session.exitstatus = 1
'''


def _run_test_once(test_file: str, test_name: str, cwd: str, timeout: float) -> tuple[bool, str]:
    """Run the single target test once in a fresh pytest subprocess.

    Fresh process per run is deliberate: it resets module-level RNG/clock state,
    so a "fix" that merely seeded the RNG in one import can't leak determinism
    across runs. A guard conftest (written by the caller into `cwd`) makes a
    skip/xfail/error/empty session exit nonzero, so only a real pass returns True.
    Returns (passed, last_output_tail).
    """
    proc = subprocess.run(
        # -rN reports skip/xfail reasons into output so the tail explains a reject.
        [sys.executable, "-m", "pytest", f"{test_file}::{test_name}", "-q", "-rN",
         "-p", "no:randomly"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    passed = proc.returncode == 0
    tail = (proc.stdout or "")[-600:] + (proc.stderr or "")[-200:]
    return passed, tail


def verify_fix(
    *,
    original_source: str,
    fixed_source: str,
    target_file: str,
    test_name: str,
    runs: int = 30,
    per_run_timeout: float = 30.0,
) -> Verdict:
    """Deterministically verify a proposed flaky-test fix.

    Strategy: write `fixed_source` to a temp copy of the file, then run only the
    target test `runs` times in fresh subprocesses. Confirm iff (a) the static
    no-weakening gate passes AND (b) every single run passes.

    We never touch the real target file — the fix is applied to a temp copy in a
    sandbox dir so a rejected fix leaves the repo untouched.
    """
    # --- Gate 1: static no-weakening check (cheap, runs first). ---
    before = shape_of(original_source, test_name)
    after = shape_of(fixed_source, test_name)
    reason = weakening_reason(before, after)
    diff = _diff_summary(original_source, fixed_source)
    base_evidence = {
        "test": test_name,
        "runs_requested": runs,
        "assertions_before": before.assert_count,
        "assertions_after": after.assert_count,
        "markers_before": list(before.markers),
        "markers_after": list(after.markers),
        "diff": diff,
    }
    if reason is not None:
        return Verdict(
            confirmed=False,
            evidence={**base_evidence, "gaming_trap": reason, "passes": 0},
            reason=f"rejected by anti-gaming gate: {reason}",
        )

    # --- Gate 2: run the fixed test N times in a sandbox. ---
    with tempfile.TemporaryDirectory(prefix="flaky_verify_") as sandbox:
        # Recreate the file at its same relative path inside the sandbox so any
        # intra-file imports / module name still resolve.
        rel = os.path.basename(target_file)
        sandbox_file = os.path.join(sandbox, rel)
        with open(sandbox_file, "w") as f:
            f.write(fixed_source)
        # Defense in depth behind the AST gate: a guard conftest fails the run
        # unless exactly one test genuinely passed, so a skip/xfail (which pytest
        # exits 0 for) can never be counted as a green run.
        with open(os.path.join(sandbox, "conftest.py"), "w") as f:
            f.write(_GUARD_CONFTEST)

        passes = 0
        executed = 0
        last_fail_tail = ""
        for i in range(runs):
            executed += 1
            try:
                ok, tail = _run_test_once(rel, test_name, cwd=sandbox, timeout=per_run_timeout)
            except subprocess.TimeoutExpired:
                last_fail_tail = f"run {i + 1} timed out after {per_run_timeout}s"
                break
            if ok:
                passes += 1
            else:
                last_fail_tail = tail
                break  # one flap is enough to reject; no point burning the rest

    confirmed = passes == runs
    evidence = {**base_evidence, "passes": passes, "runs_executed": executed}
    if confirmed:
        return Verdict(
            confirmed=True,
            evidence=evidence,
            reason=f"all {runs}/{runs} runs passed; assertions preserved ({after.assert_count})",
        )
    return Verdict(
        confirmed=False,
        evidence={**evidence, "last_failure": last_fail_tail},
        reason=f"only {passes}/{runs} runs passed before a flap — fix is not deterministic",
    )
