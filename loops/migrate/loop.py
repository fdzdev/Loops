"""migrate — a codemod / API-migration loop.

GENERATOR  grep a bundled sample repo for a deprecated API pattern; emit one
           Candidate per file that uses it (dedupe_key = the file's relative
           path). Re-scanning each round is safe — the driver dedupes, and a
           file drops out of the crop once its rewrite is confirmed and written.

EXECUTOR   llm.strong rewrites the whole file to the new API, preserving
           behavior. The model only *proposes*; it never grades its own work.

VERIFIER   deterministic — see verifier.py. Apply the rewrite to a temp copy and
           confirm iff it compiles, the repo's tests pass, zero deprecated-pattern
           occurrences remain in that file, and only that file changed.

Run it:  python -m loops.migrate            (uses the bundled sample repo)
         ANTHROPIC_API_KEY must be set; needs `pytest` on PATH.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Iterable, Optional

from pydantic import BaseModel, Field

from agentloops import LLM, Budget, JsonlState, Verdict, run_loop

from .verifier import DEPRECATED_PATTERN, count_pattern, verify_rewrite

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_REPO = os.path.join(HERE, "sample_repo")

# Files we never migrate: the deprecated helper's *definition* site (it must keep
# defining old_api so the shim still works during the rollout) and test files
# (rewriting tests is exactly the gaming move the verifier guards against).
EXCLUDE_BASENAMES = frozenset({"helpers.py"})


def find_files_with_pattern(
    repo_dir: str = SAMPLE_REPO,
    pattern: str = DEPRECATED_PATTERN,
) -> list[tuple[str, int]]:
    """Grep the repo for live uses of `pattern`. Returns (relative_path, count),
    skipping test files and the helper definition. Uses the comment/string-aware
    counter so a commented-out call is not mistaken for a real call site.
    """
    hits: list[tuple[str, int]] = []
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".pytest_cache")]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            if name in EXCLUDE_BASENAMES:
                continue
            # Skip test files — migrating them is forbidden, not desired.
            rel = os.path.relpath(os.path.join(dirpath, name), repo_dir)
            if name.startswith("test_") or os.path.sep + "tests" + os.path.sep in (
                os.path.sep + rel + os.path.sep
            ):
                continue
            with open(os.path.join(dirpath, name), "r", encoding="utf-8") as f:
                source = f.read()
            n = count_pattern(source, pattern)
            if n > 0:
                hits.append((rel, n))
    return sorted(hits)


class MigrationCandidate(BaseModel):
    """One file that still uses the deprecated pattern."""

    repo_dir: str
    rel_path: str
    pattern: str = DEPRECATED_PATTERN
    occurrences: int = Field(0, description="live uses found by the grep")

    @property
    def dedupe_key(self) -> str:
        # File path is the stable key: each file is migrated at most once.
        return self.rel_path


_REWRITE_SYSTEM = (
    "You are a careful codemod. You migrate a Python file off a deprecated API "
    "and onto its modern replacement WITHOUT changing observable behavior.\n"
    "Rules:\n"
    "  - Replace every call to the deprecated function with the new one.\n"
    "  - Fix imports accordingly (import the new name; drop the old import only "
    "if it becomes unused).\n"
    "  - Do NOT alter logic, signatures, docstrings semantics, or formatting "
    "beyond what the rename requires.\n"
    "  - Do NOT add, remove, or weaken any test.\n"
    "  - Return the COMPLETE rewritten file, nothing else."
)


class _Rewrite(BaseModel):
    new_source: str = Field(description="the complete rewritten file contents")


def _strip_code_fence(text: str) -> str:
    """If the model wrapped the file in a ``` fence, unwrap it."""
    m = re.match(r"^\s*```(?:python)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    return m.group(1) if m else text


def make_executor(
    llm: LLM,
    *,
    old_name: str = "old_api",
    new_name: str = "new_api",
) -> Callable[[MigrationCandidate], str]:
    """Return a function that asks llm.strong to rewrite a candidate file.

    The migration target (old_name -> new_name) is stated explicitly so the model
    doesn't have to infer intent from the pattern string.
    """

    def rewrite(candidate: MigrationCandidate) -> str:
        abs_path = os.path.join(candidate.repo_dir, candidate.rel_path)
        with open(abs_path, "r", encoding="utf-8") as f:
            original = f.read()
        result = llm.structured(
            model=llm.strong,
            system=_REWRITE_SYSTEM,
            user=(
                f"Migrate this file from the deprecated `{old_name}` to `{new_name}`.\n"
                f"They are behaviorally identical. After your rewrite there must be "
                f"ZERO calls to `{old_name}` left.\n\n"
                f"--- {candidate.rel_path} ---\n{original}"
            ),
            schema=_Rewrite,
        )
        return _strip_code_fence(result.new_source)

    return rewrite


def build(
    run_dir: str,
    budget: Budget,
    *,
    repo_dir: str = SAMPLE_REPO,
    pattern: str = DEPRECATED_PATTERN,
    old_name: str = "old_api",
    new_name: str = "new_api",
    llm: Optional[LLM] = None,
):
    """Wire generate + verify + state for the migrate loop.

    Returns (generate, verify, state, executor). The caller passes
    generate/verify/state to `run_loop`; `executor` is exposed so a caller (like
    main) can cache the exact blessed source for write-back. A successful verify
    writes the rewrite back to `repo_dir` (via on_confirm in main) so the file
    leaves the next round's crop — see main().
    """
    llm = llm or LLM(budget)
    state = JsonlState(run_dir)
    executor = make_executor(llm, old_name=old_name, new_name=new_name)

    def generate() -> Iterable[MigrationCandidate]:
        return [
            MigrationCandidate(
                repo_dir=repo_dir, rel_path=rel, pattern=pattern, occurrences=n
            )
            for rel, n in find_files_with_pattern(repo_dir, pattern)
        ]

    def verify(candidate: MigrationCandidate) -> Verdict:
        new_source = executor(candidate)
        return verify_rewrite(
            repo_dir=candidate.repo_dir,
            rel_path=candidate.rel_path,
            new_source=new_source,
            pattern=candidate.pattern,
        )

    return generate, verify, state, executor


def main() -> None:
    """Run the loop against the bundled sample repo and print a short report.

    A confirmed rewrite is written back to the sample repo so that file no longer
    matches the grep on the next round — that's how the loop converges and goes
    dry. (Re-running after a full migration prints "nothing to migrate".)
    """
    import argparse

    parser = argparse.ArgumentParser(description="API-migration codemod loop")
    parser.add_argument("--repo", default=SAMPLE_REPO, help="repo to migrate")
    parser.add_argument("--run-dir", default=os.path.join(HERE, ".run"))
    parser.add_argument("--max-usd", type=float, default=2.0)
    parser.add_argument("--max-rounds", type=int, default=10)
    parser.add_argument(
        "--write",
        action="store_true",
        help="write confirmed rewrites back to the repo (default: dry, no writes)",
    )
    args = parser.parse_args()

    pending = find_files_with_pattern(args.repo)
    if not pending:
        print(f"nothing to migrate: no live '{DEPRECATED_PATTERN}' under {args.repo}")
        return
    print(f"files still using '{DEPRECATED_PATTERN}':")
    for rel, n in pending:
        print(f"  {rel}: {n} occurrence(s)")
    print()

    budget = Budget(max_usd=args.max_usd)
    # main() uses its own caching verify (verify_and_cache) so it can write back
    # the exact source the verifier blessed; build()'s plain verify is the reusable
    # contract for other callers.
    generate, _verify, state, executor = build(args.run_dir, budget, repo_dir=args.repo)

    # Cache the verified rewrite per file so on_confirm can write the exact source
    # the verifier blessed, instead of re-querying the model.
    blessed: dict[str, str] = {}

    def verify_and_cache(candidate: MigrationCandidate) -> Verdict:
        new_source = executor(candidate)
        verdict = verify_rewrite(
            repo_dir=candidate.repo_dir,
            rel_path=candidate.rel_path,
            new_source=new_source,
            pattern=candidate.pattern,
        )
        if verdict.confirmed:
            blessed[candidate.rel_path] = new_source
        return verdict

    def on_confirm(candidate: MigrationCandidate, verdict: Verdict) -> None:
        if not args.write:
            return
        src = blessed.get(candidate.rel_path)
        if src is None:
            return
        abs_path = os.path.join(candidate.repo_dir, candidate.rel_path)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(src)
        print(f"  wrote migrated {candidate.rel_path}")

    result = run_loop(
        generate=generate,
        verify=verify_and_cache,
        state=state,
        budget=budget,
        max_rounds=args.max_rounds,
        dry_rounds_to_stop=2,
        on_confirm=on_confirm,
    )

    print()
    print(f"stopped: {result.stopped}  rounds: {result.rounds}")
    print(f"confirmed: {len(result.confirmed)}  rejected: {len(result.rejected)}")
    print(budget.summary())
    for cand, verdict in result.confirmed:
        ev = verdict.evidence
        print(
            f"  OK  {cand.dedupe_key}: "
            f"{ev.get('pattern')} {ev.get('pattern_count_before')} -> "
            f"{ev.get('pattern_count_after')}, "
            f"pytest exit {ev.get('pytest_returncode')}"
        )
    for cand, verdict in result.rejected:
        print(f"  XX  {cand.dedupe_key}: {verdict.reason}")
    if not args.write and result.confirmed:
        print("\n(dry run — rewrites were verified but not written. Pass --write to apply.)")

    # --- HUMAN HANDOFF -------------------------------------------------------
    # Plain-English summary of what the loop produced and where the proof lives,
    # so a reviewer knows exactly what they got and can check it without re-running.
    print()
    print("=== HUMAN HANDOFF ===")
    n_ok = len(result.confirmed)
    if n_ok:
        files = ", ".join(cand.dedupe_key for cand, _ in result.confirmed)
        verb = "migrated and written back to" if args.write else "verified (dry run, not written) against"
        print(
            f"You got {n_ok} file(s) {verb} {args.repo}, each confirmed off "
            f"'{DEPRECATED_PATTERN}' with the test suite still green: {files}."
        )
        if not args.write:
            print("Re-run with --write to apply these confirmed rewrites to the repo.")
    else:
        print(f"No files were confirmed migrated off '{DEPRECATED_PATTERN}'.")
    print(
        "Proof / evidence: per-file verdicts (before/after pattern count, "
        "py_compile, changed_files, pytest result) are in the evidence JSONL "
        f"under the run dir: {args.run_dir}"
    )
    print(
        "Concrete artifact: the rewritten file(s) "
        + ("now in the repo above." if (args.write and n_ok) else "above, ready to apply with --write." if n_ok else "(none this run).")
    )


if __name__ == "__main__":
    main()
