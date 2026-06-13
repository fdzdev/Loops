"""ETL / data-pipeline self-healing loop.

GENERATOR  scan an inputs directory, run the *current* transform on each file,
           and surface every input the transform can't ingest. One Candidate per
           broken input; dedupe_key = input path + content hash, so re-running
           against the same (unchanged) file never re-spends tokens, and editing
           the file produces a fresh candidate.

EXECUTOR   llm.strong reads the messy bytes + the canonical contract and writes a
           replacement `transform(raw_text) -> list[dict]` body.

VERIFIER   deterministic invariants in verifier.py: schema match, row-count
           reconciliation, required-column non-null, and a numeric total that
           must reconcile to the trusted reference within tolerance. The model
           that writes the transform never grades it.

Runs out of the box: `python -m loops.etl` heals the bundled messy CSV with only
ANTHROPIC_API_KEY set (plus the `anthropic`/`pydantic` deps the library needs).
"""
from __future__ import annotations

import glob
import hashlib
import os
from typing import Iterable, Optional

from pydantic import BaseModel

from agentloops import LLM, Budget, JsonlState, Verdict, run_loop

from .demo import spec
from .verifier import verify_transform

_HERE = os.path.dirname(__file__)
_DEFAULT_INPUTS = os.path.join(_HERE, "demo", "inputs")


# --- Candidate ----------------------------------------------------------------


class BrokenInput(BaseModel):
    """One input file the current transform cannot ingest."""

    path: str
    content_hash: str
    raw_text: str
    failure: str  # why the current transform broke on it (cheap pre-check)

    @property
    def dedupe_key(self) -> str:
        # Path + content hash: a given file's exact bytes are healed once. Change
        # the bytes -> new hash -> new candidate. (The brief's required key.)
        return f"{self.path}@{self.content_hash}"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --- Generator ----------------------------------------------------------------


def make_generator(inputs_dir: str):
    """Return a `generate()` that yields one BrokenInput per file the current
    transform fails on. Clean files (transform already works) are skipped — the
    loop only spends intelligence where the pipeline is actually broken."""

    def generate() -> Iterable[BrokenInput]:
        crop: list[BrokenInput] = []
        for path in sorted(glob.glob(os.path.join(inputs_dir, "*.csv"))):
            with open(path, newline="") as f:
                raw = f.read()
            broken, why = spec.transform_fails(spec.current_transform, raw)
            if broken:
                crop.append(
                    BrokenInput(
                        path=os.path.abspath(path),
                        content_hash=_hash(raw),
                        raw_text=raw,
                        failure=why,
                    )
                )
        return crop

    return generate


# --- Executor (llm.strong proposes a repaired transform) ----------------------


class ProposedTransform(BaseModel):
    """The repaired transform the strong model proposes."""

    transform_body: str  # Python defining `def transform(raw_text) -> list[dict]`
    notes: str  # what was messy and how the repair handles it


_EXECUTOR_SYSTEM = f"""You repair broken ETL transforms. You are given the raw \
bytes of a messy input file that the production transform cannot ingest, plus \
the canonical output contract. Write a Python function body that cleans the \
input into the canonical schema.

CONTRACT (do not deviate):
- Define exactly: def transform(raw_text: str) -> list[dict]
- Output columns, in this order: {list(spec.OUTPUT_SCHEMA)}
- These columns must never be empty: {list(spec.REQUIRED_NON_NULL)}
- The input may use a different delimiter, renamed/whitespace-padded headers, \
currency symbols, padded numbers, and rows that are voided/cancelled.
- Voided/cancelled rows (blank order id, blank amounts) must be DROPPED, not \
emitted. There are exactly {spec.DOCUMENTED_DROPS} such row(s) expected.
- Numeric columns (units, unit_price, revenue) must be parsed to clean numeric \
strings ('30.00', not '$30.00'). Do NOT invent, zero out, or duplicate values: \
the sum of '{spec.RECONCILE_COLUMN}' over your output MUST equal the true total \
of the real (non-void) rows. Dropping or zeroing real rows to make it "run" is \
a failure.
- You may import only: csv, io, re. No filesystem, no network.

Return the function body as `transform_body` (importable as-is) and a short \
`notes` string. Do not wrap the body in markdown fences."""


def make_executor(llm: LLM):
    def propose(cand: BrokenInput) -> ProposedTransform:
        user = (
            f"The production transform failed on `{os.path.basename(cand.path)}` "
            f"with: {cand.failure}\n\n"
            f"Canonical reference total of '{spec.RECONCILE_COLUMN}' "
            f"(for the clean equivalent data): {spec.reference_total()}\n\n"
            f"RAW INPUT BYTES:\n-----\n{cand.raw_text}\n-----\n\n"
            "Write the repaired transform."
        )
        return llm.structured(
            model=llm.strong,
            system=_EXECUTOR_SYSTEM,
            user=user,
            schema=ProposedTransform,
            max_tokens=4000,
        )

    return propose


# --- Wiring -------------------------------------------------------------------


def build(
    run_dir: str,
    budget: Budget,
    *,
    inputs_dir: str = _DEFAULT_INPUTS,
    llm: Optional[LLM] = None,
):
    """Wire generate + verify + state. Returns kwargs for `run_loop`.

    The verifier is deterministic and never calls the model. The executor is the
    only LLM call per candidate: strong-model in (open-ended repair), invariants
    out (mechanical proof)."""
    llm = llm or LLM(budget)
    generate = make_generator(inputs_dir)
    propose = make_executor(llm)

    def verify(cand: BrokenInput) -> Verdict:
        proposal = propose(cand)
        verdict = verify_transform(
            raw_text=cand.raw_text, transform_body=proposal.transform_body
        )
        # Carry the proposal into the evidence so a human can read the exact body
        # that produced these reconciled totals.
        verdict.evidence = {
            **verdict.evidence,
            "input": os.path.basename(cand.path),
            "executor_notes": proposal.notes,
            "transform_body": proposal.transform_body,
        }
        return verdict

    return {
        "generate": generate,
        "verify": verify,
        "state": JsonlState(run_dir),
        "budget": budget,
    }


def main() -> None:
    run_dir = os.getenv("ETL_RUN_DIR", os.path.join(_HERE, ".run"))
    budget = Budget(max_usd=float(os.getenv("ETL_MAX_USD", "1.0")))

    kwargs = build(run_dir, budget)
    result = run_loop(max_rounds=5, dry_rounds_to_stop=2, **kwargs)

    print("\n=== ETL self-healing loop ===")
    print(f"stopped: {result.stopped}  rounds: {result.rounds}")
    print(f"confirmed: {len(result.confirmed)}  rejected: {len(result.rejected)}")
    print(f"budget: {budget.summary()}")
    for cand, verdict in result.confirmed:
        inv = verdict.evidence.get("invariants", {})
        print(f"\nHEALED {cand.dedupe_key}")
        print(f"  reason: {verdict.reason}")
        for name, r in inv.items():
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"  [{mark}] {name}: {r['detail']}")
        print(
            f"  reconciled total: output={verdict.evidence.get('output_total')} "
            f"reference={verdict.evidence.get('reference_total')}"
        )
    for cand, verdict in result.rejected:
        print(f"\nREJECTED {cand.dedupe_key}: {verdict.reason}")

    # --- Human handoff: what you got, and where the proof lives ---------------
    print("\n=== HUMAN HANDOFF ===")
    if result.confirmed:
        print(
            f"{len(result.confirmed)} broken feed(s) were repaired and proven "
            "safe to load. For each one, the cleaned output reconciles to the "
            "trusted reference total, so this is a correct feed, not just a "
            "non-crashing one."
        )
        for cand, verdict in result.confirmed:
            out_total = verdict.evidence.get("output_total")
            ref_total = verdict.evidence.get("reference_total")
            print(
                f"  - {os.path.basename(cand.path)}: revenue reconciled at "
                f"{out_total} against reference {ref_total}."
            )
        print(
            "Evidence (per-candidate invariant results, row counts, output "
            f"schema, sample rows, and the exact repaired transform body) is in {run_dir} "
            "as JSONL — review it before promoting the transform to production."
        )
    else:
        print(
            "No feed was confirmed this run. Either every input already ingests "
            "cleanly, or no proposed repair reconciled to the reference total. "
            f"See the evidence JSONL in {run_dir} for the per-candidate detail."
        )


if __name__ == "__main__":
    main()
