"""The `flaky` loop: retire intermittently-failing tests.

Wiring:
  GENERATOR  parse a CI flaky-test report fixture -> one Candidate per flaky
             test. dedupe_key = the pytest node id.
  EXECUTOR   llm.strong rewrites the target test file to make the flaky test
             deterministic (freeze the clock, seed the RNG, remove the race),
             keeping every assertion intact.
  VERIFIER   (verifier.py) applies the fix to a sandbox copy, runs the target
             test N times, and confirms iff ALL N pass AND the test was not
             weakened (AST check). The verdict is the compiler + the test
             runner — never the model that wrote the fix.

Run it:  python -m loops.flaky   (needs ANTHROPIC_API_KEY + pytest)
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Callable, Iterable

from pydantic import BaseModel, Field

from agentloops import LLM, Budget, JsonlState, Verdict, run_loop

from .verifier import verify_fix

# Resolve demo paths relative to this file, so the loop runs from any cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
DEFAULT_REPORT = os.path.join(_HERE, "demo", "flaky_report.json")


# ---------------------------------------------------------------------------
# Candidate: one flaky test pulled from the CI report.
# ---------------------------------------------------------------------------
class FlakyTest(BaseModel):
    """One intermittently-failing test parsed from the CI flaky-test report."""

    node_id: str  # e.g. "loops/flaky/demo/test_flaky_demo.py::test_recent_event_timestamp"
    target_file: str  # repo-relative path of the file holding the test
    test_name: str  # the function name pytest runs
    pass_rate: float = 1.0
    symptom: str = ""
    suspected_cause: str = ""  # "time" | "random" | ...

    @property
    def dedupe_key(self) -> str:
        # The pytest node id is the stable identity of a test — exactly what a
        # CI flake tracker keys on, and what we must not re-fix once handled.
        return self.node_id


# ---------------------------------------------------------------------------
# Executor output: the LLM returns a full rewritten file.
# ---------------------------------------------------------------------------
class ProposedFix(BaseModel):
    """The executor's proposed repair: the entire rewritten test file."""

    fixed_source: str = Field(
        description="The complete, runnable rewritten contents of the target test file."
    )
    strategy: str = Field(
        default="",
        description="One line: how the flake was removed (freeze clock / seed RNG / remove race).",
    )


# ---------------------------------------------------------------------------
# GENERATOR
# ---------------------------------------------------------------------------
def make_generator(report_path: str) -> Callable[[], Iterable[FlakyTest]]:
    """Build a generator that parses the CI flaky-test report into Candidates.

    Parsing happens each round (cheap, deterministic). The driver dedupes by
    node id, so a test we already fixed/rejected is never re-proposed.
    """

    def generate() -> Iterable[FlakyTest]:
        with open(report_path) as f:
            report = json.load(f)
        return [FlakyTest(**entry) for entry in report.get("flaky_tests", [])]

    return generate


# ---------------------------------------------------------------------------
# EXECUTOR
# ---------------------------------------------------------------------------
_EXECUTOR_SYSTEM = (
    "You repair flaky Python tests. You are given the full source of a test "
    "file and the name of one intermittently-failing test in it. Make THAT test "
    "deterministic by removing its nondeterministic input — typically by "
    "freezing the clock (inject or monkeypatch the time source), seeding the "
    "RNG (random.seed / pass a seeded Random), or removing a race. "
    "HARD RULES: keep every assertion exactly as strong as it was. Do NOT add "
    "@pytest.mark.skip/skipif/xfail, do NOT call pytest.skip()/xfail(), do NOT "
    "delete the test, and do NOT remove or weaken any assert. Return the COMPLETE "
    "rewritten file so it can be run as-is; do not abbreviate or elide code."
)


def propose_fix(llm: LLM, *, candidate: FlakyTest, source: str) -> ProposedFix:
    """Ask the strong model for a deterministic rewrite of the target file.

    Open-ended repair work -> spend intelligence here (llm.strong). The result is
    only a *proposal*; the deterministic verifier decides whether it counts.
    """
    user = (
        f"Target file: {candidate.target_file}\n"
        f"Flaky test to fix: {candidate.test_name}\n"
        f"Symptom from CI: {candidate.symptom}\n"
        f"Suspected cause: {candidate.suspected_cause}\n\n"
        f"--- current file source ---\n{source}\n--- end source ---\n\n"
        "Return the complete rewritten file that makes the flaky test "
        "deterministic while preserving all assertions."
    )
    return llm.structured(
        model=llm.strong,
        system=_EXECUTOR_SYSTEM,
        user=user,
        schema=ProposedFix,
        max_tokens=4000,
    )


# ---------------------------------------------------------------------------
# VERIFY (delegates to the deterministic verifier)
# ---------------------------------------------------------------------------
def make_verify(
    llm: LLM, *, repo_root: str, runs: int
) -> Callable[[FlakyTest], Verdict]:
    """Build the verify callable: propose a fix, then deterministically check it."""

    def verify(candidate: FlakyTest) -> Verdict:
        abs_target = os.path.join(repo_root, candidate.target_file)
        if not os.path.exists(abs_target):
            return Verdict(
                confirmed=False,
                evidence={"target_file": candidate.target_file},
                reason=f"target file not found: {candidate.target_file}",
            )
        with open(abs_target) as f:
            original_source = f.read()

        fix = propose_fix(llm, candidate=candidate, source=original_source)

        verdict = verify_fix(
            original_source=original_source,
            fixed_source=fix.fixed_source,
            target_file=candidate.target_file,
            test_name=candidate.test_name,
            runs=runs,
        )
        # Stamp the executor's stated strategy into the evidence for review.
        verdict.evidence.setdefault("strategy", fix.strategy)
        return verdict

    return verify


# ---------------------------------------------------------------------------
# build() + main()
# ---------------------------------------------------------------------------
def build(
    run_dir: str,
    budget: Budget,
    *,
    report_path: str = DEFAULT_REPORT,
    repo_root: str = _REPO_ROOT,
    runs: int = 30,
    llm: LLM | None = None,
    max_rounds: int = 10,
):
    """Wire generate + verify + state + budget into the arguments for run_loop.

    Returns a dict of kwargs for `agentloops.run_loop`, so a caller (or a test)
    can inject a fake `llm` / smaller `runs` and drive the loop directly.
    """
    llm = llm or LLM(budget)
    return {
        "generate": make_generator(report_path),
        "verify": make_verify(llm, repo_root=repo_root, runs=runs),
        "state": JsonlState(run_dir),
        "budget": budget,
        "max_rounds": max_rounds,
        "dry_rounds_to_stop": 1,  # the report is finite; one dry round means done
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="flaky — flaky-test retirement loop")
    parser.add_argument("--report", default=DEFAULT_REPORT, help="CI flaky-test report JSON")
    parser.add_argument("--repo-root", default=_REPO_ROOT, help="root for resolving target files")
    parser.add_argument("--runs", type=int, default=30, help="how many times to run each fixed test")
    parser.add_argument("--run-dir", default=os.path.join(_HERE, ".runs", "flaky"), help="state dir")
    parser.add_argument("--max-usd", type=float, default=2.0, help="hard budget ceiling in USD")
    parser.add_argument("--max-rounds", type=int, default=10)
    args = parser.parse_args()

    budget = Budget(max_usd=args.max_usd)
    kwargs = build(
        args.run_dir,
        budget,
        report_path=args.report,
        repo_root=args.repo_root,
        runs=args.runs,
        max_rounds=args.max_rounds,
    )
    result = run_loop(**kwargs)

    print("\n=== flaky loop report ===")
    print(f"stopped: {result.stopped} after {result.rounds} round(s)")
    print(f"confirmed fixes: {len(result.confirmed)} | rejected: {len(result.rejected)}")
    print(f"budget: {budget.summary()}")
    for cand, verdict in result.confirmed:
        ev = verdict.evidence
        print(
            f"  CONFIRMED {cand.dedupe_key}\n"
            f"    {verdict.reason}\n"
            f"    strategy: {ev.get('strategy', '?')} | "
            f"asserts {ev.get('assertions_before')}->{ev.get('assertions_after')}"
        )
    for cand, verdict in result.rejected:
        print(f"  rejected  {cand.dedupe_key}: {verdict.reason}")

    # --- HUMAN HANDOFF: what you got, and where the proof is ---------------
    n_confirmed = len(result.confirmed)
    confirmed_path = os.path.join(args.run_dir, "confirmed.jsonl")
    print("\n=== HUMAN HANDOFF ===")
    if n_confirmed:
        runs_each = args.runs
        print(
            f"{n_confirmed} flaky test(s) are now deterministic — each one ran "
            f"{runs_each}x in fresh pytest processes with zero flaps, so they're "
            f"safe to merge without a re-run habit."
        )
        print("Fixed tests:")
        for cand, verdict in result.confirmed:
            ev = verdict.evidence
            passes = ev.get("passes", "?")
            requested = ev.get("runs_requested", runs_each)
            print(f"  - {cand.dedupe_key}  ({passes}/{requested} green)")
        print(
            "\nProof to review before merging:\n"
            f"  - evidence (one JSON line per fix, incl. the unified diff): {confirmed_path}\n"
            f"  - full run state (seen / confirmed / rejected JSONL): {args.run_dir}\n"
            "Each confirmed line carries the before/after assertion counts, markers, "
            "and a unified diff — enough to confirm a fix in under a minute."
        )
    else:
        print(
            "No flaky test was confirmed deterministic this run. Nothing is safe "
            "to merge yet."
        )
        print(
            f"  - rejections and reasons: {os.path.join(args.run_dir, 'rejected.jsonl')}\n"
            f"  - full run state: {args.run_dir}"
        )


if __name__ == "__main__":
    main()
