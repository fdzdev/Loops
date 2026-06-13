"""bugfix — repro-test-gated bug fixing, in two modes.

OFFLINE DEMO (the default for `python -m loops.bugfix`):
  GENERATOR: read a bundled issues.json. One Candidate per *open* issue;
             dedupe_key = the issue id.
  EXECUTOR:  llm.structured(model=llm.strong) reads the issue + buggy source +
             the bundled failing repro test, and proposes the full replacement
             source for the buggy module.
  VERIFIER:  verify_patch — apply to a fresh temp copy, run pytest. Confirmed
             iff the repro goes red->green, the suite stays green, no test
             file changed. Runs out-of-the-box (no network).

GITHUB WATCHER (`--repo <clone> --remote <owner/repo>`):
  GENERATOR: list_candidate_issues — open issues that are STALE (idle since last
             activity >= --min-idle-hours, default 5h) AND unassigned, so the
             loop only picks up what a human left untouched.
  EXECUTOR:  the strong model FIRST writes a repro test it believes reproduces
             the bug, THEN proposes the fix (full replacement of non-test files).
  VERIFIER:  verify_repo_patch — runs the repro on HEAD and REQUIRES it red, then
             requires red->green + green suite + pre-existing tests untouched.
             An issue whose repro won't go red is skipped ("could not reproduce").
  HANDOFF:   on a confirmed fix, open a DRAFT PR linking the issue. DRY-RUN by
             default; pass --open-pr to actually push + create. Never merges.

Run the demo:  ANTHROPIC_API_KEY=... python -m loops.bugfix   (needs pytest)
Watch a repo:  python -m loops.bugfix --repo ./clone --remote owner/repo
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Iterable, Optional

from pydantic import BaseModel, Field

from agentloops import LLM, Budget, JsonlState, Verdict, run_loop

from . import github
from .verifier import verify_patch, verify_repo_patch

# The demo project bundled with this loop.
_HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_PROJECT_DIR = os.path.join(_HERE, "demo_project")
ISSUES_PATH = os.path.join(DEMO_PROJECT_DIR, "issues.json")


# --- The candidate ---------------------------------------------------------


class FixCandidate(BaseModel):
    """One proposed bug fix. The dedupe key is the issue id, so the loop never
    attempts the same issue twice — a fix the verifier rejects is not retried
    in a tight loop (it would just regenerate the same way)."""

    issue_id: str
    title: str
    buggy_module_rel: str
    repro_test_rel: str
    issue_body: str
    current_source: str
    patched_source: str = ""  # filled by the executor

    @property
    def dedupe_key(self) -> str:
        return self.issue_id


# --- The executor (proposes the patch) -------------------------------------


class _ProposedPatch(BaseModel):
    """Schema the strong model fills. We ask for the WHOLE module back so the
    verifier can drop it in atomically — no fragile diff application."""

    patched_module_source: str = Field(
        description=(
            "The complete, corrected source of the buggy module. Return the "
            "entire file, ready to write to disk and import. Fix only the bug; "
            "do not change the public function signatures or touch any test."
        )
    )
    explanation: str = Field(description="One or two sentences on the root cause and the fix.")


def propose_patch(llm: LLM, cand: FixCandidate) -> str:
    """Ask the strong model for a corrected version of the buggy module.

    The model NEVER decides whether its own fix is correct — that is the
    verifier's job, against real pytest runs. The model only writes code.
    """
    system = (
        "You are a senior engineer fixing a single reported bug. You are given "
        "the issue, the current (buggy) source of one module, and the failing "
        "repro test. Return the COMPLETE corrected source for that module. "
        "Rules: fix the root cause described in the issue; keep all public "
        "function signatures; do not weaken, edit, or reference the tests; do "
        "not add new dependencies. Output only via the structured schema."
    )
    user = (
        f"ISSUE {cand.issue_id}: {cand.title}\n\n"
        f"{cand.issue_body}\n\n"
        f"--- BUGGY MODULE ({cand.buggy_module_rel}) ---\n"
        f"{cand.current_source}\n\n"
        f"--- FAILING REPRO TEST ({cand.repro_test_rel}) ---\n"
        f"{_read_rel(cand.repro_test_rel)}\n"
    )
    proposed = llm.structured(
        model=llm.strong,
        system=system,
        user=user,
        schema=_ProposedPatch,
        max_tokens=4000,
    )
    return proposed.patched_module_source


# --- The generator ---------------------------------------------------------


def _read_rel(rel_path: str) -> str:
    with open(os.path.join(DEMO_PROJECT_DIR, rel_path)) as f:
        return f.read()


def load_open_issues() -> list[dict]:
    """Read the bundled tracker and return only OPEN issues."""
    with open(ISSUES_PATH) as f:
        issues = json.load(f)
    return [i for i in issues if i.get("status") == "open"]


def make_generator(llm: LLM):
    """Build the generate() callable. Each round it returns one candidate per
    open issue, with the patch already proposed by the executor. The driver
    filters out issues already seen (by id), so the executor only runs for
    issues that haven't been attempted yet."""

    def generate() -> Iterable[FixCandidate]:
        crop: list[FixCandidate] = []
        for issue in load_open_issues():
            cand = FixCandidate(
                issue_id=issue["id"],
                title=issue["title"],
                buggy_module_rel=issue["buggy_module"],
                repro_test_rel=issue["repro_test"],
                issue_body=issue.get("body", ""),
                current_source=_read_rel(issue["buggy_module"]),
            )
            # The executor (a strong-model call) runs here, lazily per issue.
            cand.patched_source = propose_patch(llm, cand)
            crop.append(cand)
        return crop

    return generate


# --- The verify adapter ----------------------------------------------------


def make_verify():
    """Wrap the deterministic verifier into the (candidate)->Verdict signature.

    The verifier excludes the *other* open issues' repro tests from the
    full-suite gate (a one-module patch can't fix a different bug). That list
    comes from the tracker here — never from the candidate — so it can only ever
    name known other-issue repros, not hide a genuine regression.
    """

    def verify(cand: FixCandidate) -> Verdict:
        others = tuple(
            issue["repro_test"]
            for issue in load_open_issues()
            if issue["id"] != cand.issue_id
        )
        return verify_patch(
            demo_project_dir=DEMO_PROJECT_DIR,
            buggy_module_rel=cand.buggy_module_rel,
            repro_test_rel=cand.repro_test_rel,
            patched_source=cand.patched_source,
            other_open_repro_rels=others,
        )

    return verify


# --- Wiring ----------------------------------------------------------------


def build(
    run_dir: str,
    budget: Optional[Budget] = None,
    *,
    llm: Optional[LLM] = None,
    max_rounds: int = 5,
    dry_rounds_to_stop: int = 1,
):
    """Wire generate + verify + state + budget into a runnable closure.

    Returns a zero-arg `run()` that executes the loop and returns a LoopResult.
    """
    budget = budget or Budget(max_usd=2.0)
    llm = llm or LLM(budget)
    state = JsonlState(run_dir)

    def run():
        return run_loop(
            generate=make_generator(llm),
            verify=make_verify(),
            state=state,
            budget=budget,
            max_rounds=max_rounds,
            dry_rounds_to_stop=dry_rounds_to_stop,
        )

    return run, budget


# ===========================================================================
# GitHub watcher mode
# ===========================================================================
#
# Same loop shape (generate -> verify), but the generator reads a live tracker
# and the executor must EARN a fix by first writing a repro test that goes red.


class GithubFixCandidate(BaseModel):
    """One stale, unassigned GitHub issue plus the strong model's proposed repro
    test and fix. dedupe_key = "remote#number" so an issue attempted once is
    never retried across watch cycles (the JsonlState on disk remembers it)."""

    remote: str
    number: int
    title: str
    body: str
    repro_test_rel: str = ""       # filled by the executor
    repro_test_source: str = ""    # filled by the executor
    patched_files: dict[str, str] = Field(default_factory=dict)  # filled by the executor

    @property
    def dedupe_key(self) -> str:
        return f"{self.remote}#{self.number}"


class _ProposedRepro(BaseModel):
    """The repro test the strong model writes FIRST, before any fix. The verifier
    runs it on HEAD and requires it to FAIL — that red gate is what stops a
    trivially-passing test from waving a non-fix through."""

    test_rel_path: str = Field(
        description=(
            "Path for the new repro test, relative to the repo root, UNDER the "
            "tests directory (e.g. 'tests/test_issue_123_repro.py'). Must be a "
            "new file that does not already exist."
        )
    )
    test_source: str = Field(
        description=(
            "Complete source of a pytest test that FAILS on the current code "
            "because of the bug, and will PASS once the bug is fixed. Import the "
            "real module under test; assert the correct behavior."
        )
    )


class _ProposedRepoFix(BaseModel):
    """The fix: full replacement contents for each NON-TEST file it changes.
    Whole files (not diffs) so the verifier applies them atomically."""

    changed_files: dict[str, str] = Field(
        description=(
            "Map of repo-relative path -> COMPLETE new file contents, for the "
            "non-test source files this fix changes. Do NOT include any test "
            "file; do NOT modify the repro test. Fix the root cause only."
        )
    )
    explanation: str = Field(description="One or two sentences: root cause and fix.")


def propose_repro(llm: LLM, issue: github.Issue, *, tests_dir_rel: str) -> _ProposedRepro:
    """Strong model writes the repro test FIRST, from the issue prose alone."""
    system = (
        "You are a senior engineer triaging a bug report. Before fixing anything, "
        "write a single pytest test that REPRODUCES the bug: it must FAIL on the "
        "current code and PASS once the bug is fixed. Import the real modules; do "
        "not stub them. Output only via the structured schema."
    )
    user = (
        f"ISSUE #{issue.number}: {issue.title}\n\n{issue.body}\n\n"
        f"Place the test under the '{tests_dir_rel}/' directory. Choose a unique "
        f"file name that does not collide with existing tests."
    )
    return llm.structured(
        model=llm.strong, system=system, user=user, schema=_ProposedRepro, max_tokens=4000
    )


def propose_repo_fix(
    llm: LLM, issue: github.Issue, repro: _ProposedRepro
) -> _ProposedRepoFix:
    """Strong model proposes the fix, given the issue and its own repro test. It
    never decides whether the fix is correct — verify_repo_patch does, via real
    pytest runs."""
    system = (
        "You are a senior engineer fixing a single reported bug. You are given the "
        "issue and a failing repro test. Return the COMPLETE corrected contents of "
        "each NON-TEST source file you change. Rules: fix the root cause; keep "
        "public signatures; do NOT add, edit, delete, or reference any test file "
        "(the repro is fixed); do not add new dependencies. Output only via the "
        "structured schema."
    )
    user = (
        f"ISSUE #{issue.number}: {issue.title}\n\n{issue.body}\n\n"
        f"--- FAILING REPRO TEST ({repro.test_rel_path}) ---\n{repro.test_source}\n\n"
        f"The repo root is the working directory. Reference files by their path "
        f"relative to that root."
    )
    return llm.structured(
        model=llm.strong, system=system, user=user, schema=_ProposedRepoFix, max_tokens=8000
    )


def make_github_generator(
    llm: LLM,
    *,
    remote: str,
    min_idle_hours: float,
    label: Optional[str],
    limit: int,
    tests_dir_rel: str,
    log=print,
):
    """generate() for the watcher: each round, fetch stale+unassigned issues and,
    for each, have the strong model draft a repro test and a fix. The driver
    filters out issues already seen (by remote#number), so the executor only
    runs for issues not yet attempted."""

    def generate() -> Iterable[GithubFixCandidate]:
        issues = github.list_candidate_issues(
            remote, min_idle_hours=min_idle_hours, label=label, limit=limit
        )
        log(f"  {len(issues)} stale, unassigned issue(s): {[i.number for i in issues]}")
        crop: list[GithubFixCandidate] = []
        for issue in issues:
            repro = propose_repro(llm, issue, tests_dir_rel=tests_dir_rel)
            fix = propose_repo_fix(llm, issue, repro)
            crop.append(
                GithubFixCandidate(
                    remote=remote,
                    number=issue.number,
                    title=issue.title,
                    body=issue.body,
                    repro_test_rel=repro.test_rel_path,
                    repro_test_source=repro.test_source,
                    patched_files=dict(fix.changed_files),
                )
            )
        return crop

    return generate


def make_github_verify(clone_dir: str, *, tests_dir_rel: str, log=print):
    """Wrap verify_repo_patch into (candidate)->Verdict. Each candidate is
    verified in its OWN throwaway worktree so a failed attempt never dirties the
    clone and attempts can't bleed into each other. The worktree of a CONFIRMED
    fix is kept (its branch is what a PR would push); rejected worktrees are
    removed immediately."""

    def verify(cand: GithubFixCandidate) -> Verdict:
        branch = f"bugfix/{cand.remote.replace('/', '-')}-issue-{cand.number}"
        wt = github.create_worktree(clone_dir, branch)
        try:
            verdict = verify_repo_patch(
                repo_dir=wt.path,
                repro_test_rel=cand.repro_test_rel,
                repro_test_source=cand.repro_test_source,
                patched_files=cand.patched_files,
                tests_dir_rel=tests_dir_rel,
            )
        except Exception as e:  # a broken candidate must not crash the cycle
            github.remove_worktree(wt)
            return Verdict(confirmed=False, reason=f"verification errored: {e}", evidence={})

        # Stash the worktree handle on the verdict's evidence so on_confirm can
        # push it. The driver records evidence as-is; we keep only JSON-friendly
        # bits there and pass the live handle out-of-band via the closure below.
        if verdict.confirmed:
            _confirmed_worktrees[cand.dedupe_key] = wt
        else:
            github.remove_worktree(wt)
        return verdict

    return verify


# Live worktree handles for confirmed fixes, keyed by dedupe_key. Populated by
# make_github_verify, consumed by the handoff in build_github.run().
_confirmed_worktrees: dict[str, github.Worktree] = {}


def build_github(
    *,
    repo: str,
    remote: str,
    run_dir: str,
    budget: Optional[Budget] = None,
    llm: Optional[LLM] = None,
    min_idle_hours: float = 5.0,
    label: Optional[str] = None,
    limit: int = 20,
    tests_dir_rel: str = "tests",
    open_pr: bool = False,
    max_rounds: int = 1,
    dry_rounds_to_stop: int = 1,
    log=print,
):
    """Wire the GitHub watcher. Returns (run, budget). `run()` executes one poll
    of the loop and performs the handoff (dry-run preview, or push + draft PR if
    open_pr) for each confirmed fix. The caller drives --watch by calling run()
    repeatedly; the on-disk JsonlState dedupes across cycles."""
    budget = budget or Budget(max_usd=2.0)
    llm = llm or LLM(budget)
    state = JsonlState(run_dir)
    clone_dir = os.path.abspath(repo)

    handoffs: list[dict] = []

    def on_confirm(cand: GithubFixCandidate, verdict: Verdict) -> None:
        wt = _confirmed_worktrees.pop(cand.dedupe_key, None)
        if wt is None:  # pragma: no cover - defensive
            return
        title = f"Fix #{cand.number}: {cand.title}"
        pr_body = github.build_pr_body(
            github.Issue(number=cand.number, title=cand.title, body=cand.body, updated_at=""),
            verdict.evidence,
            remote=remote,
        )
        diff = github.worktree_diff(wt)
        record = {
            "issue": cand.number,
            "branch": wt.branch,
            "pr_title": title,
            "pr_body": pr_body,
            "diff": diff,
            "opened": False,
            "pr_url": "",
        }
        if open_pr:
            github.commit_all(wt, title)
            github.push_branch(wt)
            record["pr_url"] = github.open_draft_pr(
                remote=remote, head_branch=wt.branch, title=title, body=pr_body, cwd=wt.path
            )
            record["opened"] = True
            log(f"  opened DRAFT PR for #{cand.number}: {record['pr_url']}")
        else:
            log(f"  DRY-RUN: would open DRAFT PR for #{cand.number} from {wt.branch}")
            github.remove_worktree(wt)  # nothing pushed; reclaim the disk
        handoffs.append(record)

    def run():
        result = run_loop(
            generate=make_github_generator(
                llm,
                remote=remote,
                min_idle_hours=min_idle_hours,
                label=label,
                limit=limit,
                tests_dir_rel=tests_dir_rel,
                log=log,
            ),
            verify=make_github_verify(clone_dir, tests_dir_rel=tests_dir_rel, log=log),
            state=state,
            budget=budget,
            max_rounds=max_rounds,
            dry_rounds_to_stop=dry_rounds_to_stop,
            on_confirm=on_confirm,
        )
        return result, handoffs

    return run, budget


# ===========================================================================
# CLI
# ===========================================================================


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m loops.bugfix",
        description=(
            "Repro-test-gated bug fixing. With no flags it runs the bundled "
            "offline demo. Pass --repo/--remote to watch a real GitHub tracker."
        ),
    )
    p.add_argument("--repo", help="path to a local clone of the repo to fix in")
    p.add_argument("--remote", help="GitHub remote as owner/repo (e.g. octocat/hello)")
    p.add_argument(
        "--min-idle-hours", type=float, default=5.0,
        help="only touch issues idle (since last activity) at least this long (default 5)",
    )
    p.add_argument("--label", default=None, help="only consider issues with this label")
    p.add_argument(
        "--tests-dir", default="tests",
        help="the repo's tests directory, relative to its root (default 'tests')",
    )
    p.add_argument(
        "--watch", type=float, default=None, metavar="SECONDS",
        help="poll forever, sleeping this many seconds between cycles",
    )
    p.add_argument(
        "--open-pr", action="store_true",
        help="actually push the branch and open a DRAFT PR (default: dry-run only)",
    )
    p.add_argument("--max-usd", type=float, default=2.0, help="hard budget ceiling (default 2.0)")
    return p


def _run_demo() -> None:
    run_dir = os.path.join(_HERE, ".run")
    run, budget = build(run_dir)
    print(f"bugfix loop (offline demo) — demo project at {DEMO_PROJECT_DIR}")
    print(f"open issues: {[i['id'] for i in load_open_issues()]}\n")

    result = run()

    print("\n=== bugfix report ===")
    print(f"rounds={result.rounds} stopped={result.stopped}")
    print(f"confirmed fixes: {len(result.confirmed)}  rejected: {len(result.rejected)}")
    for cand, verdict in result.confirmed:
        print(f"  FIXED {cand.dedupe_key}: {verdict.reason}")
    for cand, verdict in result.rejected:
        print(f"  REJECTED {cand.dedupe_key}: {verdict.reason}")
    print(budget.summary())
    print(f"evidence written under {run_dir}/")


def _print_handoff(result, handoffs: list[dict], *, open_pr: bool, run_dir: str) -> None:
    """The human handoff: what the loop did, the PR link (or dry-run preview),
    and where the full evidence lives."""
    print("\n=== bugfix handoff ===")
    print(f"confirmed fixes: {len(result.confirmed)}  rejected/skipped: {len(result.rejected)}")
    for cand, verdict in result.rejected:
        print(f"  skipped {cand.dedupe_key}: {verdict.reason}")
    for record in handoffs:
        print(f"\n--- issue #{record['issue']} -> branch {record['branch']} ---")
        if record["opened"]:
            print(f"  DRAFT PR opened (NOT merged): {record['pr_url']}")
        else:
            print("  DRY-RUN (no push, no PR). Re-run with --open-pr to hand it off.")
            print("  Intended PR title: " + record["pr_title"])
            print("  Intended PR body:\n" + _indent(record["pr_body"]))
            print("  Diff:\n" + _indent(record["diff"]))
    if not handoffs:
        print("  no confirmed fixes this run; nothing to hand off.")
    print(f"\nfull evidence (red/green/suite output) under {run_dir}/confirmed.jsonl")


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def main(argv: Optional[list[str]] = None) -> None:
    args = _build_arg_parser().parse_args(argv)

    # Default path: the offline demo, so `python -m loops.bugfix` still works
    # out-of-the-box with only an API key + pytest.
    if not args.repo and not args.remote:
        _run_demo()
        return

    if not (args.repo and args.remote):
        raise SystemExit("--repo and --remote must be given together for GitHub mode")

    run_dir = os.path.join(_HERE, ".run-github")
    budget = Budget(max_usd=args.max_usd)
    run, budget = build_github(
        repo=args.repo,
        remote=args.remote,
        run_dir=run_dir,
        budget=budget,
        min_idle_hours=args.min_idle_hours,
        label=args.label,
        tests_dir_rel=args.tests_dir,
        open_pr=args.open_pr,
    )

    mode = "OPEN-PR" if args.open_pr else "DRY-RUN"
    print(f"bugfix watcher [{mode}] — remote={args.remote} clone={args.repo}")
    print(f"staleness: idle >= {args.min_idle_hours}h since last activity AND unassigned")
    if args.label:
        print(f"label filter: {args.label}")

    cycle = 0
    while True:
        cycle += 1
        print(f"\n[cycle {cycle}] polling {args.remote} ...")
        result, handoffs = run()
        _print_handoff(result, handoffs, open_pr=args.open_pr, run_dir=run_dir)
        print(budget.summary())
        if args.watch is None:
            break
        if budget.exhausted():
            print("budget exhausted; stopping watch.")
            break
        print(f"sleeping {args.watch}s before the next poll ...")
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
