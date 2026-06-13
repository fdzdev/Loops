# bugfix — repro-test-gated bug fixing from an issue tracker

> **Who it's for / what it saves you:** an engineering team drowning in a
> backlog. The watcher picks up stale, unowned GitHub issues — ones a human
> left untouched for hours — turns each into a small **draft PR** with a
> **red→green repro** attached as proof, and stops there. You review in two
> minutes; nothing is ever auto-merged, and it never touches a ticket someone
> is actively on.

Two modes, one verifier:

- **Offline demo (default):** reads a bundled issue tracker, proposes a patch
  for each open bug with a strong model, and **confirms a fix only when pytest
  says so**. Runs out-of-the-box.
- **GitHub watcher:** polls a real repo for stale, unassigned issues; the model
  writes a repro test *first*, the verifier requires it to go red→green, and a
  confirmed fix becomes a draft PR linking the issue.

One issue in, one verified patch out — or a logged rejection, never a false
"done".

## The verifier (the product)

`verifier.py :: verify_patch` is deterministic. Given a proposed replacement
for a buggy module, against a **fresh temp copy** of the demo project it:

1. **Snapshots every test file** (sha256) before touching anything.
2. **Applies the patch to exactly one file** — the issue's buggy module.
3. **Re-snapshots the test files and demands they are byte-identical.** If any
   test file was added, deleted, or edited, the patch is **rejected** before a
   single test runs (fast static rejection).
4. Runs the **repro test** — which fails on the unpatched code — and requires
   it to **PASS**.
5. Runs the **full pytest suite** and requires it to **stay green** — not just
   the repro.
6. **Re-snapshots the test files one more time, after every pytest run, and
   again demands byte-for-byte identity.** A malicious module can delete or
   rewrite test files at *import time* — which fires during pytest collection,
   after step 3 but during steps 4–5. This post-run check is the authoritative
   anti-gaming gate: it rejects a patch that, e.g., hardcodes the repro's answer
   and then deletes the regression test that would expose it, even though both
   pytest runs exited green.

Confirmed iff all six hold. The verdict comes from pytest's exit codes on real
source, never from asking the model "did that work?". Both pytest runs (the red
baseline and the green patched run) plus the test-file hashes go into
`Verdict.evidence`, so a human confirms the red→green transition in under a
minute.

## Why it pays

- **Tirelessness.** A backlog of repro-bearing bugs gets worked through
  unattended; each fix is gated on the same hard signal a human reviewer would
  demand.
- **Parallelism.** One candidate per issue, deduped by issue id — the loop
  scales across an arbitrarily large tracker without re-attempting issues.
- **Trust.** The output is not "the model thinks it fixed it"; it is "the repro
  went red→green, the suite is still green, and no test was weakened to get
  there." That is mergeable evidence.

## The gaming trap

"All tests pass" is trivially gamed by **deleting or weakening the tests**, or
by smuggling the answer into a test file. This loop forbids it structurally:

- The patch may change **only the buggy module**; the entire `tests/` directory
  must be **byte-for-byte identical** both before the tests run (step 3) **and
  after** (step 6). The post-run gate is what makes this airtight: a patch that
  tampers with test files at import time would slip past a before-only snapshot.
- The **full suite** must pass, so hardcoding the one repro's expected value
  breaks the regression tests (they exercise several distinct inputs and edges).
- The repro is checked **red→green**: a no-op patch that leaves the bug in place
  fails step 4.

## The GitHub watcher

`python -m loops.bugfix --repo <local_clone> --remote <owner/repo>` turns the
loop on a live tracker. The verifier is the same idea, hardened for the fact
that **a real issue ships no repro test**:

```bash
# dry-run (default): print branch + diff + intended PR body, push nothing
python -m loops.bugfix --repo ./myrepo --remote acme/widgets

# narrow to one label, longer idle window
python -m loops.bugfix --repo ./myrepo --remote acme/widgets \
    --label bug --min-idle-hours 8

# actually hand off: push the branch and open a DRAFT PR
python -m loops.bugfix --repo ./myrepo --remote acme/widgets --open-pr

# watch: re-poll every 30 minutes (cron-style), deduping across cycles
python -m loops.bugfix --repo ./myrepo --remote acme/widgets --watch 1800 --open-pr
```

**Which issues it touches — the 5h last-activity rule.** `github.py
:: list_candidate_issues` keeps an open issue only if it is **stale**: idle
since last activity ≥ `--min-idle-hours` (default **5h**) **and** has **no
assignee**. `updatedAt` already advances on the last comment, edit, or linked
commit, so "idle since last activity" is the staleness signal. This is the whole
point — the loop deliberately works only what a human walked away from, so it
**complements the team instead of racing a person who is on the ticket.** It
reads the tracker via the `gh` CLI (JSON output), with the GitHub REST API over
the standard library as a documented fallback if `gh` is absent. No new pip deps.

**Repro-first verification (`verifier.py :: verify_repo_patch`).** Because no
repro ships with the issue, the strong model must **earn** the fix:

1. The model writes a repro test it believes reproduces the bug. The verifier
   writes it into an isolated git worktree and runs it on HEAD — and **requires
   it to FAIL (red)**. If it passes, or errors on collection, the issue is
   **skipped** with reason "could not reproduce". The loop never attempts a fix
   it cannot verify; this red gate is what stops a trivially-passing test.
2. The model proposes the fix as **full replacement contents** for the non-test
   files it changes. The verifier applies them and confirms **only if**: the
   repro now **passes** (red→green), the **full suite stays green**, every
   **pre-existing test file is byte-identical** before and after (the repro is
   the only new test file allowed), and the fix did **not** modify the repro
   after it went red. The byte-identity check is re-run *after* pytest, so a
   module that rewrites tests at import time is still caught. Same hashing as the
   demo verifier — the two share `_run_pytest`, `_hash_tree`, `_changed_files`.

`Verdict.evidence` carries the **red** repro output, the **green** repro output,
the full-suite output, and the changed-files list.

**Handoff + safety.** On a confirmed fix the watcher opens a **DRAFT PR** whose
body links the issue (`Fixes #N`) and embeds the evidence, so a reviewer
confirms red→green in under a minute. **It never merges.** The default is a
**dry-run**: it prints the branch, the diff, and the intended PR body and pushes
**nothing** — you only get a real branch + PR when you pass `--open-pr`. An issue
is deduped by `remote#number` and the state persists on disk, so `--watch` (or a
cron job calling the command on a schedule) never re-attempts an issue it has
already tried.

## Runs out-of-the-box

`python -m loops.bugfix` with **no flags** runs the offline demo. It works with
an `ANTHROPIC_API_KEY` and **pytest installed** (`pip install pytest`, also
available via the project's `dev` extra: `pip install -e ".[dev]"`). It ships
its own demo target — `demo_project/` (the `buggy_lib` package + repro and
regression tests + `issues.json`) — so there is nothing external to wire up.
Evidence is written under `loops/bugfix/.run/`. Adding `--repo/--remote` switches
to the watcher (above), which additionally needs the `gh` CLI authenticated (or
`GH_TOKEN`/`GITHUB_TOKEN` for the REST fallback) and a local clone to work in.

### The bundled demo target

`demo_project/` contains:

- `buggy_lib/pricing.py` — **PRICE-1**: `line_total` discounts only the first
  unit, so multi-quantity discounted lines are overcharged.
- `buggy_lib/text.py` — **TEXT-1**: `slugify` emits double hyphens and a
  trailing hyphen.
- `tests/` — one **repro test per bug** (red until fixed) plus **regression
  tests** that already pass and must stay green.
- `issues.json` — the tracker. Two open issues (PRICE-1, TEXT-1) and one closed
  issue the generator must skip.

Confirm the premise yourself without any API key:

```bash
cd loops/bugfix/demo_project && python -m pytest -q
# 2 failed (the two repro tests), 11 passed
```
