# migrate — codemod / API-migration loop

**Who it's for / what it saves you:** an eng lead gets a deprecation/upgrade applied across the codebase as small, per-file changes that each compile, keep the test suite green, and touch nothing else — landable diffs instead of a hand-audited mega-rewrite.

Move a codebase off a deprecated API and onto its replacement, one file at a
time, with a machine confirming every rewrite before it counts.

## The verifier (the product)

`verifier.py :: verify_rewrite(...)` is deterministic. It applies the model's
rewrite to a **throwaway copy** of the sample repo and confirms the migration of
one file **iff all four checks hold**:

1. **Compiles** — `py_compile` succeeds on the rewritten file.
2. **Behavior preserved** — the repo's own `pytest` suite passes against the
   patched copy.
3. **Pattern gone** — **zero** occurrences of the forbidden deprecated pattern
   (`old_api(`) remain in that file, counting *live code only* (comments and
   string literals are stripped before counting, so `# old_api(...)` or
   `"old_api("` does NOT count as migrated — it's still a live use).
4. **Isolation** — **only the intended file** differs from the pristine repo.

The verdict comes entirely from running code, never from asking the generator
model "did that work?". The proof lands in `Verdict.evidence`:

```
file, pattern, pattern_count_before, pattern_count_after,
py_compile, changed_files, pytest_returncode, pytest_tail
```

— before/after counts plus the test result, enough to confirm by eye in under a
minute.

## How it works

- **Generator** — greps the bundled sample repo for the deprecated pattern and
  emits one `MigrationCandidate` per file that uses it. `dedupe_key` is the file
  path, so each file is migrated at most once. The helper's *definition* site
  (`helpers.py`) and the test files are excluded from the scan — migrating them
  is forbidden, not desired.
- **Executor** — `llm.strong` (`claude-opus-4-8`) rewrites the whole file to the
  new API, preserving behavior, and returns the complete file.
- **Verifier** — the deterministic four-check gate above, in a fresh temp copy.

## Why it pays

- **Tirelessness & parallelism** — a large-repo migration is hundreds of
  identical, boring edits. The loop grinds through every call site without
  fatigue, and each file is an independent candidate that can be verified on its
  own.
- **Latency** — no human in the rewrite-then-eyeball-the-diff cycle; the suite
  is the reviewer, and only confirmed files get written back (`--write`).
- **Trust** — a green migration here means *compiles + tests pass + pattern
  eradicated + nothing else touched*. That's a diff a human can land without
  re-deriving correctness.

## The gaming trap

A migration loop has two obvious cheats, and **each check alone is not enough**:

- Make the tests pass by doing **nothing** — leave every `old_api(` in place.
  The deprecated shim still works, so the suite stays green. **Check 3 (pattern
  count must hit 0) fails.**
- Make the pattern count hit 0 by **gutting the function, commenting the call
  out, or deleting the tests**. **Check 2 (behavior preserved) and Check 4 (only
  this file changed, so test files are untouched) fail**, and the
  comment/string-stripping counter refuses to treat `# old_api(` as removed.

You only pass by doing the real migration: tests stay green **and** the live
pattern count is zero **and** the only file you changed is the one you were
asked to migrate.

## Runnable status

**Out-of-the-box.** Ships a bundled sample repo under `sample_repo/` (a package
using the deprecated `old_api()` and a pinned `pytest` suite). Needs only
`ANTHROPIC_API_KEY` and `pytest` on `PATH`.

```bash
# from the agentloops repo root
python -m loops.migrate            # dry run: verify rewrites, write nothing
python -m loops.migrate --write    # apply confirmed rewrites back to the repo
```

The sample repo ships **un-migrated** (`currency.py` and `reporting.py` both call
`old_api`), so a fresh run has real work to do. After `--write`, re-running
prints `nothing to migrate`.
