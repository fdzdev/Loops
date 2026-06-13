"""perf — performance-regression hunting / optimization loop.

Wiring:
  GENERATOR  one Candidate per registered target. dedupe_key = target name +
             a hash of the current baseline implementation, so a given baseline
             is optimized once and never re-tried after a verdict, but changing
             the baseline reopens the problem.
  EXECUTOR   llm.strong proposes a faster, drop-in replacement for the target.
             The model writes ONLY the function source; it cannot see or touch
             the tests, the timer, or the baseline.
  VERIFIER   loops/perf/verifier.py — runs correctness + a noise-guarded
             benchmark in a fresh subprocess and confirms iff correct AND the
             measured median runtime improves by more than the threshold.

Run it:  python -m loops.perf            (needs ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import os
from typing import Callable, Iterable, Optional

from pydantic import BaseModel, Field

from agentloops import LLM, Budget, JsonlState, Verdict, run_loop

from .targets import Target, registry
from .verifier import (
    IMPROVEMENT_THRESHOLD,
    MIN_RUNS,
    verify_candidate,
)


class OptCandidate(BaseModel):
    """One optimization attempt: a target to speed up plus the proposed source.

    `dedupe_key` = target name + baseline hash, per the brief. The proposed
    source is filled in by the executor before verification; it is NOT part of
    the dedupe key (we attempt each baseline once regardless of what the model
    writes, so the loop goes dry instead of re-prompting forever)."""

    target_name: str
    baseline_hash: str
    signature_hint: str
    proposed_src: str = ""

    @property
    def dedupe_key(self) -> str:
        return f"{self.target_name}@{self.baseline_hash}"


class _Proposal(BaseModel):
    """What the executor returns: replacement source and a one-line rationale."""

    source_code: str = Field(
        description="Complete Python source defining the optimized function. "
        "Must define a function with the exact same name and signature. "
        "Standard library only. No prints, no top-level side effects."
    )
    approach: str = Field(description="One sentence: how it is faster.")


_EXECUTOR_SYSTEM = (
    "You are a performance engineer. You are given a slow Python function and "
    "asked for a faster, behavior-identical drop-in replacement.\n"
    "Rules:\n"
    "- Keep the EXACT same function name and signature.\n"
    "- Identical observable behavior for all inputs (same return values, same "
    "ordering, same handling of duplicates/empties/negatives).\n"
    "- Standard library only. No external packages, no I/O, no prints, no "
    "global side effects at import time.\n"
    "- Aim to beat the asymptotic complexity, not micro-tune.\n"
    "Return only the function source plus a one-line rationale. Your claimed "
    "speedup will be IGNORED — the loop measures it independently."
)


def _make_generate(targets: list[Target]) -> Callable[[], Iterable[OptCandidate]]:
    """Generator: emit one candidate per target every round. The driver filters
    out anything already seen, so returning the full crop each round is fine."""

    def generate() -> Iterable[OptCandidate]:
        return [
            OptCandidate(
                target_name=t.name,
                baseline_hash=t.baseline_hash(),
                signature_hint=t.signature_hint,
            )
            for t in targets
        ]

    return generate


def _make_verify(
    llm: LLM, targets: list[Target], model: Optional[str] = None
) -> Callable[[OptCandidate], Verdict]:
    """Verify = ask llm.strong for a faster implementation (the executor), then
    hand it to the deterministic verifier. The proposing model never gets to
    judge its own work — the subprocess benchmark does."""
    by_name = {t.name: t for t in targets}
    proposer_model = model or llm.strong

    def verify(c: OptCandidate) -> Verdict:
        target = by_name[c.target_name]

        # EXECUTOR: llm.strong writes the replacement. It sees the baseline and
        # the signature, never the tests or the timing harness.
        proposal = llm.structured(
            model=proposer_model,
            system=_EXECUTOR_SYSTEM,
            user=(
                f"Function to optimize (signature: {c.signature_hint}):\n\n"
                f"```python\n{target.baseline_src.strip()}\n```\n\n"
                "Write a faster drop-in replacement."
            ),
            schema=_Proposal,
            max_tokens=2000,
        )
        c.proposed_src = proposal.source_code

        # VERIFIER (deterministic): correctness + noise-guarded benchmark.
        verdict = verify_candidate(target, proposal.source_code)
        verdict.evidence["approach_claimed"] = proposal.approach
        return verdict

    return verify


def build(
    run_dir: str,
    budget: Budget,
    *,
    llm: Optional[LLM] = None,
    model: Optional[str] = None,
    targets: Optional[list[Target]] = None,
):
    """Wire generate + verify + state for run_loop.

    Returns (generate, verify, state, llm) so a caller can drive run_loop or
    inspect the pieces. `model` overrides the executor model (defaults to
    llm.strong, per the brief)."""
    llm = llm or LLM(budget)
    targets = targets if targets is not None else registry()
    state = JsonlState(run_dir)
    generate = _make_generate(targets)
    verify = _make_verify(llm, targets, model=model)
    return generate, verify, state, llm


def main() -> None:
    run_dir = os.getenv("PERF_RUN_DIR", os.path.join(os.getcwd(), ".perf_run"))
    budget = Budget(max_usd=float(os.getenv("PERF_MAX_USD", "2.0")))

    generate, verify, state, llm = build(run_dir, budget)

    print(f"perf loop — targets: {[t.name for t in registry()]}")
    print(f"verifier: correct AND >{IMPROVEMENT_THRESHOLD * 100:.0f}% faster "
          f"(median of {MIN_RUNS} warmed-up runs, measured in a subprocess)")
    print(f"run dir: {run_dir}\n")

    result = run_loop(
        generate=generate,
        verify=verify,
        state=state,
        budget=budget,
        max_rounds=int(os.getenv("PERF_MAX_ROUNDS", "10")),
        dry_rounds_to_stop=1,
    )

    print("\n--- perf loop report ---")
    print(f"stopped: {result.stopped}  rounds: {result.rounds}")
    print(f"confirmed optimizations: {len(result.confirmed)}")
    for cand, verdict in result.confirmed:
        print(f"  + {cand.dedupe_key}: {verdict.reason}")
    print(f"rejected: {len(result.rejected)}")
    for cand, verdict in result.rejected:
        print(f"  - {cand.dedupe_key}: {verdict.reason}")
    print(f"\nbudget: {budget.summary()}")
    print(f"evidence written under: {run_dir}/confirmed.jsonl")

    # HUMAN HANDOFF: plain-English summary of what you got and where the proof is.
    print("\n=== HUMAN HANDOFF ===")
    n = len(result.confirmed)
    if n:
        names = ", ".join(cand.target_name for cand, _ in result.confirmed)
        print(f"The loop produced {n} confirmed optimization(s) for you: {names}.")
        print("Each one is faster than the original baseline by a measured margin "
              f"(>{IMPROVEMENT_THRESHOLD * 100:.0f}%, median of {MIN_RUNS} timed runs) "
              "AND verified to return identical results — proven, not claimed.")
        print(f"Proof for every win is in {os.path.join(run_dir, 'confirmed.jsonl')}: "
              "one JSON line per optimization with baseline vs. candidate timings, "
              "speedup, and the correctness verdict.")
        print("Next step: open that file, eyeball the timings, and merge the "
              "replacement source for the wins you want.")
    else:
        print("The loop confirmed no optimizations this run — every candidate was "
              "either not measurably faster or failed the correctness check, so "
              "nothing is recommended for merge.")
        print(f"The full record (including rejections and why) is under {run_dir}.")


__all__ = ["build", "main", "OptCandidate"]
