"""The bugfix verifier — deterministic, the product of this loop.

A proposed patch is confirmed ONLY when, against a fresh temp copy of the demo
project:

  1. The patch touches nothing but the issue's buggy module. Every test file is
     byte-identical before the patch, again before the tests run, AND again
     after every pytest run (we hash them at all three points). The post-run
     hash is the authoritative anti-gaming clause: you cannot "pass the suite"
     by deleting or weakening a test — not even by doing it at import time, which
     fires during pytest collection and would slip past a before-only snapshot.
  2. The named repro test, which FAILS on the unpatched code, now PASSES.
  3. The FULL pytest suite stays green — not just the repro. A fix that breaks
     any regression test is rejected.

No model is asked "did that work?". The verdict comes from pytest's exit codes
on real source, and the proof (both pytest runs + the test-file hashes) is put
in `Verdict.evidence` so a human can confirm it in under a minute.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from agentloops import Verdict


@dataclass
class _PytestRun:
    returncode: int
    passed: bool  # True iff pytest exited 0 (all selected tests passed)
    output: str


def _run_pytest(
    project_dir: str,
    target: str | None = None,
    deselect: tuple[str, ...] = (),
) -> _PytestRun:
    """Run pytest inside `project_dir`. If `target` is given (a test node id or
    path), run only that; otherwise run the whole suite. `deselect` drops named
    test files from the run — the verifier uses it (and only the verifier, never
    the candidate) to exclude the still-red repro tests of *other* open issues,
    which a single-issue patch cannot be expected to fix. We invoke pytest as a
    subprocess so a candidate's import-time side effects can't poison our own
    interpreter, and we pin the working directory + PYTHONPATH so the demo
    package imports cleanly.

    Bytecode caching is disabled (`-B` / PYTHONDONTWRITEBYTECODE + pytest's
    cacheprovider off). This is load-bearing for correctness, not a tidiness
    nicety: the red baseline run compiles the BUGGY source into `__pycache__`,
    and we then overwrite that source with the patched version. Python keys its
    "is the .pyc stale?" decision on the source mtime truncated to whole seconds,
    so a patch written within the same second as the red run would be silently
    ignored and the GREEN run would re-import the stale buggy bytecode — making a
    correct fix look like it "still fails". Never writing .pyc removes the cache
    entirely, so every run reflects the source on disk right now."""
    cmd = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if target:
        cmd.append(target)
    for path in deselect:
        cmd += ["--ignore", path]
    env = dict(os.environ)
    # Ensure the project root is importable (buggy_lib) regardless of cwd.
    env["PYTHONPATH"] = project_dir + os.pathsep + env.get("PYTHONPATH", "")
    # Belt-and-suspenders with -B: guarantee no .pyc is written or trusted, so a
    # patch applied in the same wall-clock second as the prior run is never
    # masked by stale cached bytecode.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        cmd,
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return _PytestRun(returncode=proc.returncode, passed=proc.returncode == 0, output=out)


def _hash_tree(root: str, rel_dir: str) -> dict[str, str]:
    """Map every file under `root/rel_dir` to its sha256. Used to prove the test
    directory is untouched across the patch."""
    base = os.path.join(root, rel_dir)
    digests: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in sorted(filenames):
            if name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as f:
                digests[rel] = hashlib.sha256(f.read()).hexdigest()
    return digests


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Files that were added, removed, or whose contents changed between two
    hash maps. Used to report exactly which test files a patch tampered with."""
    changed = set()
    for path, digest in before.items():
        if after.get(path) != digest:  # removed or content changed
            changed.add(path)
    for path in after:
        if path not in before:  # added
            changed.add(path)
    return sorted(changed)


def _tail(text: str, n: int = 4000) -> str:
    """Keep evidence reviewable: the tail of pytest output holds the summary."""
    return text if len(text) <= n else text[-n:]


def verify_patch(
    *,
    demo_project_dir: str,
    buggy_module_rel: str,
    repro_test_rel: str,
    patched_source: str,
    tests_dir_rel: str = "tests",
    other_open_repro_rels: tuple[str, ...] = (),
) -> Verdict:
    """Deterministically verify one proposed patch.

    Parameters
    ----------
    demo_project_dir : absolute path to the pristine demo project.
    buggy_module_rel : the buggy module's path, relative to the project root.
    repro_test_rel   : the repro test's path (or node id), relative to the root.
    patched_source   : the full proposed replacement source for the buggy module.
    tests_dir_rel    : the tests directory whose files must remain untouched.
    other_open_repro_rels : repro tests of *other* still-open issues. They are
        excluded from the full-suite gate because a single-issue patch (which
        may only touch this issue's module) cannot fix another issue's bug. This
        is set by the loop from the tracker, never by the candidate, so it can't
        be used to hide a real regression: only known other-issue repros qualify.
    """
    tmp = tempfile.mkdtemp(prefix="bugfix_")
    try:
        work = os.path.join(tmp, "project")
        shutil.copytree(demo_project_dir, work)

        # --- 0. Baseline: prove the repro test FAILS on the unpatched code.
        # Not a gate (the loop already knows it's an open bug), but it makes the
        # red->green transition auditable from the evidence alone.
        baseline = _run_pytest(work, repro_test_rel)

        # --- 1. Snapshot test files before applying the patch.
        tests_before = _hash_tree(work, tests_dir_rel)

        # --- 2. Apply the patch: overwrite ONLY the buggy module.
        target_path = os.path.join(work, buggy_module_rel)
        if not os.path.isfile(target_path):
            return Verdict(
                confirmed=False,
                reason=f"buggy module {buggy_module_rel} not found in demo project",
                evidence={"buggy_module": buggy_module_rel},
            )
        with open(target_path, "w") as f:
            f.write(patched_source)

        # --- 3. Anti-gaming gate (static): the tests directory must be
        # byte-identical right after the patch is written. Fast rejection for a
        # patch that deletes a test file, edits an assertion, or sneaks the
        # "fix" into a test. The patch is allowed to change exactly one file:
        # the buggy module (which lives outside tests/).
        #
        # This static check is NOT sufficient on its own: a malicious module can
        # delete or rewrite test files at IMPORT time, which fires only when
        # pytest collects them — i.e. AFTER this snapshot. So we re-verify the
        # tests are untouched again in step 6, after every pytest run, and that
        # post-run check is the authoritative anti-gaming gate.
        tests_after_write = _hash_tree(work, tests_dir_rel)
        if tests_before != tests_after_write:
            return Verdict(
                confirmed=False,
                reason="patch altered the test directory (forbidden)",
                evidence={
                    "changed_test_files": _changed_files(tests_before, tests_after_write),
                    "tests_before": tests_before,
                    "tests_after": tests_after_write,
                },
            )

        # --- 4. The repro test must now PASS.
        repro = _run_pytest(work, repro_test_rel)
        if not repro.passed:
            return Verdict(
                confirmed=False,
                reason="repro test still failing after patch",
                evidence={
                    "repro_test": repro_test_rel,
                    "repro_returncode": repro.returncode,
                    "repro_pytest_output": _tail(repro.output),
                    "baseline_pytest_output": _tail(baseline.output),
                },
            )

        # --- 5. The FULL suite must stay green (not just the repro). Other open
        # issues' repro tests are excluded: this patch may only touch one module
        # and cannot be expected to fix a different bug. Everything else — every
        # regression test and this issue's repro — must pass.
        full = _run_pytest(work, deselect=other_open_repro_rels)
        if not full.passed:
            return Verdict(
                confirmed=False,
                reason="patch fixed the repro but broke the full suite",
                evidence={
                    "full_returncode": full.returncode,
                    "excluded_other_repros": list(other_open_repro_rels),
                    "full_pytest_output": _tail(full.output),
                    "repro_pytest_output": _tail(repro.output),
                },
            )

        # --- 6. Authoritative anti-gaming gate: re-hash the test directory AFTER
        # every pytest run. A malicious module can delete or rewrite test files
        # at import time, which fires during collection — after step 3's snapshot
        # but during steps 4/5. Comparing against the pre-patch snapshot here
        # catches that: deleting the regression test that would expose a hardcoded
        # repro answer is rejected even though both pytest runs exited green.
        tests_after_run = _hash_tree(work, tests_dir_rel)
        if tests_before != tests_after_run:
            return Verdict(
                confirmed=False,
                reason="patch altered the test directory while tests ran (forbidden)",
                evidence={
                    "changed_test_files": _changed_files(tests_before, tests_after_run),
                    "tests_before": tests_before,
                    "tests_after": tests_after_run,
                    "full_pytest_output": _tail(full.output),
                },
            )

        # All gates passed: a real fix.
        return Verdict(
            confirmed=True,
            reason="repro went red->green and full suite is green; tests untouched",
            evidence={
                "buggy_module": buggy_module_rel,
                "repro_test": repro_test_rel,
                "tests_unchanged": True,
                "test_file_hashes": tests_after_run,
                "excluded_other_repros": list(other_open_repro_rels),
                "baseline_repro_output": _tail(baseline.output),
                "patched_repro_output": _tail(repro.output),
                "full_suite_output": _tail(full.output),
            },
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _hash_file(path: str) -> str:
    """sha256 of a single file. Used to pin the repro test byte-for-byte: it is
    the only new test file the loop adds, so it gets its own hash rather than
    riding in the tests-dir snapshot (which must exclude it)."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _is_under(rel: str, dir_rel: str) -> bool:
    """True if `rel` is the directory `dir_rel` itself or a path inside it.
    Pure string/path logic (no ValueError on disjoint roots, unlike
    os.path.commonpath) so it's safe to run on attacker-influenced paths."""
    a = os.path.normpath(rel)
    b = os.path.normpath(dir_rel)
    return a == b or a.startswith(b + os.sep)


def _hash_tests_excluding(
    root: str, tests_dir_rel: str, exclude_rel: str
) -> dict[str, str]:
    """Hash the tests directory but drop one file (the repro we add).

    The repro test is the single new test file the loop is allowed to introduce,
    so it must NOT be in the snapshot we demand stays byte-identical — otherwise
    writing it would trip the anti-gaming gate. Everything else under
    `tests_dir_rel` is a pre-existing test file and must survive the fix
    untouched. `exclude_rel` is normalized to the same project-relative form
    `_hash_tree` produces so the comparison is exact.
    """
    digests = _hash_tree(root, tests_dir_rel)
    exclude_norm = os.path.normpath(exclude_rel)
    return {rel: d for rel, d in digests.items() if os.path.normpath(rel) != exclude_norm}


def verify_repo_patch(
    *,
    repo_dir: str,
    repro_test_rel: str,
    repro_test_source: str,
    patched_files: dict[str, str],
    tests_dir_rel: str = "tests",
    pytest_target: str | None = None,
) -> Verdict:
    """Verify a fix for a REAL repo where no repro test shipped with the issue.

    Unlike `verify_patch` (which has a bundled red repro and one buggy module),
    a real issue gives us only prose. So the honesty bar is higher and enforced
    here, in order:

      0. Write the model's proposed repro test into the worktree and run ONLY it
         against the current code. It MUST FAIL (red). If it passes or errors on
         collection, the model did not actually reproduce the bug — we reject
         with "could not reproduce" and never attempt a fix we can't verify.
         This red gate is what stops a trivially-passing test from waving a
         non-fix through.
      1. Snapshot every PRE-EXISTING test file (the whole tests dir minus the
         repro we just added).
      2. Apply the proposed fix: write each file in `patched_files`. A fix that
         names the repro test or any pre-existing test file is rejected before
         running anything — the fix may only touch non-test source.
      3. Demand the pre-existing test files are byte-identical to step 1, AND
         the repro file is byte-identical to what we wrote in step 0 (the fix
         may not edit the repro after it went red).
      4. Run the repro test: it must now PASS (red -> green).
      5. Run the FULL suite: it must be green.
      6. Re-hash after both runs (authoritative anti-gaming gate): a module that
         rewrites test files at import time is caught here.

    This operates IN PLACE on `repo_dir` — pass an isolated git worktree (see
    github.py) so the caller can diff/commit/discard it independently. The same
    `_run_pytest`, `_hash_tree`, and `_changed_files` helpers back both verifiers
    so the guarantees can't drift apart.

    Parameters
    ----------
    repo_dir          : absolute path to the worktree to verify in place.
    repro_test_rel    : path for the repro test, relative to the repo root. Must
                        live under `tests_dir_rel`.
    repro_test_source : full source of the repro test the strong model wrote.
    patched_files     : {rel_path: full_replacement_contents} for the non-test
                        files the fix changes.
    tests_dir_rel     : the tests directory whose pre-existing files must remain
                        byte-identical across the fix.
    pytest_target     : optional explicit target for the full-suite run (a path
                        or node id). Defaults to the whole repo.
    """
    repro_norm = os.path.normpath(repro_test_rel)
    tests_norm = os.path.normpath(tests_dir_rel)

    # Structural guard: the repro must live under the tests dir, and the fix may
    # not masquerade as a test edit. Checked before any code runs.
    if not _is_under(repro_norm, tests_norm):
        return Verdict(
            confirmed=False,
            reason=f"repro test {repro_test_rel} is not under {tests_dir_rel}",
            evidence={"repro_test": repro_test_rel, "tests_dir": tests_dir_rel},
        )
    illegal = [
        rel
        for rel in patched_files
        if os.path.normpath(rel) == repro_norm or _is_under(rel, tests_norm)
    ]
    if illegal:
        return Verdict(
            confirmed=False,
            reason="fix tried to write a test file (forbidden); fixes may only touch non-test source",
            evidence={"illegal_test_writes": sorted(illegal)},
        )

    repro_abs = os.path.join(repo_dir, repro_test_rel)

    # --- 0. Write the repro and prove it FAILS on the current code (red gate).
    os.makedirs(os.path.dirname(repro_abs) or repo_dir, exist_ok=True)
    with open(repro_abs, "w") as f:
        f.write(repro_test_source)
    red = _run_pytest(repo_dir, repro_test_rel)
    if red.passed:
        return Verdict(
            confirmed=False,
            reason="could not reproduce: repro test passes on unpatched code (not red)",
            evidence={"repro_test": repro_test_rel, "red_pytest_output": _tail(red.output)},
        )
    # returncode 0 = all passed (handled above); 1 = tests ran and failed (the
    # red we want); anything else = collection/usage error, i.e. the test never
    # actually exercised the bug. Treat that as "could not reproduce" too.
    if red.returncode != 1:
        return Verdict(
            confirmed=False,
            reason="could not reproduce: repro test errored on collection rather than failing",
            evidence={
                "repro_test": repro_test_rel,
                "repro_returncode": red.returncode,
                "red_pytest_output": _tail(red.output),
            },
        )

    # --- 1. Snapshot the pre-existing test files (tests dir minus the repro).
    tests_before = _hash_tests_excluding(repo_dir, tests_dir_rel, repro_test_rel)
    repro_digest_before = _hash_file(repro_abs)

    # --- 2. Apply the fix: overwrite each named non-test file.
    for rel, contents in patched_files.items():
        dest = os.path.join(repo_dir, rel)
        os.makedirs(os.path.dirname(dest) or repo_dir, exist_ok=True)
        with open(dest, "w") as f:
            f.write(contents)

    # --- 3. Pre-existing tests untouched, and the repro untouched by the fix.
    tests_after_write = _hash_tests_excluding(repo_dir, tests_dir_rel, repro_test_rel)
    repro_digest_after_write = _hash_file(repro_abs)
    if tests_before != tests_after_write:
        return Verdict(
            confirmed=False,
            reason="fix altered a pre-existing test file (forbidden)",
            evidence={"changed_test_files": _changed_files(tests_before, tests_after_write)},
        )
    if repro_digest_before != repro_digest_after_write:
        return Verdict(
            confirmed=False,
            reason="fix modified the repro test after it went red (forbidden)",
            evidence={"repro_test": repro_test_rel},
        )

    # --- 4. The repro must now PASS (red -> green).
    green = _run_pytest(repo_dir, repro_test_rel)
    if not green.passed:
        return Verdict(
            confirmed=False,
            reason="repro test still failing after fix",
            evidence={
                "repro_test": repro_test_rel,
                "repro_returncode": green.returncode,
                "red_pytest_output": _tail(red.output),
                "green_pytest_output": _tail(green.output),
            },
        )

    # --- 5. The FULL suite must be green. No deselect here: a real repo's own
    # tests are all expected to pass, and this is the only candidate in flight.
    full = _run_pytest(repo_dir, pytest_target)
    if not full.passed:
        return Verdict(
            confirmed=False,
            reason="fix made the repro pass but broke the full suite",
            evidence={
                "full_returncode": full.returncode,
                "full_pytest_output": _tail(full.output),
                "green_pytest_output": _tail(green.output),
            },
        )

    # --- 6. Authoritative anti-gaming gate: re-hash after both pytest runs.
    tests_after_run = _hash_tests_excluding(repo_dir, tests_dir_rel, repro_test_rel)
    repro_digest_after_run = _hash_file(repro_abs)
    if tests_before != tests_after_run or repro_digest_before != repro_digest_after_run:
        return Verdict(
            confirmed=False,
            reason="tests were altered while the suite ran (forbidden)",
            evidence={
                "changed_test_files": _changed_files(tests_before, tests_after_run),
                "full_pytest_output": _tail(full.output),
            },
        )

    changed = sorted(patched_files.keys())
    return Verdict(
        confirmed=True,
        reason="repro went red->green, full suite green, pre-existing tests untouched",
        evidence={
            "repro_test": repro_test_rel,
            "changed_files": changed,
            "tests_unchanged": True,
            "test_file_hashes": tests_after_run,
            "red_repro_output": _tail(red.output),
            "green_repro_output": _tail(green.output),
            "full_suite_output": _tail(full.output),
        },
    )
