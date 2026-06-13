"""Deterministic verifier for the API-migration loop.

The verdict comes entirely from running code against a throwaway copy of the
sample repo — never from asking the model whether its own rewrite worked. A
rewrite is CONFIRMED only when *all four* checks hold:

  (a) py_compile   — the rewritten file still compiles.
  (b) tests pass   — the repo's own pytest suite passes against the patched copy.
  (c) pattern == 0 — zero occurrences of the forbidden deprecated pattern remain
                     in the rewritten file.
  (d) isolation    — only the intended file differs from the pristine repo.

Why all four, and why each one matters as an anti-gaming clause:

  - Tests alone are not enough: a no-op rewrite leaves every `old_api(` in place
    yet the tests stay green (the deprecated shim still works). Check (c) fails.
  - Pattern==0 alone is not enough: an adversary could `# old_api(` it out, gut
    the function, or delete the tests so "0 occurrences" is trivially true. Check
    (b) (behavior preserved) and check (d) (only this file changed, so the test
    files are untouched) close those doors.
  - Comment-hiding is closed explicitly: we strip comments and string literals
    before counting, so `# old_api(x)` or `"old_api("` does NOT count as a
    migrated call — it still counts as a live use and fails check (c).

Evidence carries the before/after pattern counts and the test result so a human
can confirm the migration in under a minute.
"""
from __future__ import annotations

import io
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import tokenize
from typing import Optional

from agentloops import Verdict

# The deprecated pattern to eradicate. A bare-name call `old_api(`; we count it
# only in *live code*, after stripping comments and string literals so an
# adversary can't "migrate" by commenting the call out.
DEPRECATED_PATTERN = "old_api("


def count_pattern(source: str, pattern: str = DEPRECATED_PATTERN) -> int:
    """Count occurrences of `pattern` in `source`, ignoring comments and string
    literals so commented-out or stringified calls don't count as removed.

    Tokenizing collapses comments/strings to placeholders; we then count the
    pattern in the reconstructed *code-only* text. Falls back to a raw count if
    the source can't be tokenized (e.g. mid-rewrite invalid syntax) so we never
    under-report a live use.
    """
    try:
        code_only_parts: list[str] = []
        readline = io.StringIO(source).readline
        for tok in tokenize.generate_tokens(readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                code_only_parts.append(" ")  # blank it out
            else:
                code_only_parts.append(tok.string)
        code_only = "".join(code_only_parts)
        return code_only.count(pattern)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Could not tokenize — count raw so we err toward "still present".
        return source.count(pattern)


def _list_files(root: str) -> dict[str, bytes]:
    """Map of relative-path -> file bytes for every file under `root`, skipping
    caches and the verifier's own scratch."""
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in ("__pycache__", ".pytest_cache", ".git")
        ]
        for name in filenames:
            if name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as f:
                out[rel] = f.read()
    return out


def _changed_files(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    """Relative paths that were added, removed, or modified."""
    changed = []
    for rel in set(before) | set(after):
        if before.get(rel) != after.get(rel):
            changed.append(rel)
    return sorted(changed)


def verify_rewrite(
    *,
    repo_dir: str,
    rel_path: str,
    new_source: str,
    pattern: str = DEPRECATED_PATTERN,
    pytest_args: Optional[list[str]] = None,
) -> Verdict:
    """Apply `new_source` to `rel_path` in a throwaway copy of `repo_dir` and run
    the four deterministic checks. Pure function of its inputs: it never mutates
    the real repo.
    """
    abs_original = os.path.join(repo_dir, rel_path)
    with open(abs_original, "r", encoding="utf-8") as f:
        original_source = f.read()

    before_count = count_pattern(original_source, pattern)
    after_count = count_pattern(new_source, pattern)

    evidence: dict = {
        "file": rel_path,
        "pattern": pattern,
        "pattern_count_before": before_count,
        "pattern_count_after": after_count,
    }

    work = tempfile.mkdtemp(prefix="migrate_verify_")
    try:
        dst_repo = os.path.join(work, "repo")
        shutil.copytree(
            repo_dir,
            dst_repo,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "*.pyc"),
        )

        pristine = _list_files(dst_repo)

        # Apply the rewrite to the single target file.
        target = os.path.join(dst_repo, rel_path)
        with open(target, "w", encoding="utf-8") as f:
            f.write(new_source)

        # (a) py_compile — does the rewritten file even compile?
        try:
            py_compile.compile(target, doraise=True)
            evidence["py_compile"] = "ok"
        except py_compile.PyCompileError as exc:
            evidence["py_compile"] = f"FAILED: {exc}"
            return Verdict(
                confirmed=False,
                evidence=evidence,
                reason=f"rewrite of {rel_path} does not compile",
            )

        # (c) zero occurrences of the deprecated pattern remain (live code only).
        if after_count != 0:
            return Verdict(
                confirmed=False,
                evidence=evidence,
                reason=(
                    f"{after_count} live occurrence(s) of '{pattern}' remain in "
                    f"{rel_path} (was {before_count})"
                ),
            )

        # (d) only the intended file changed. Run BEFORE pytest so generated
        # caches/.pyc don't pollute the diff (we ignore those anyway).
        after_files = _list_files(dst_repo)
        changed = _changed_files(pristine, after_files)
        evidence["changed_files"] = changed
        if changed != [rel_path]:
            return Verdict(
                confirmed=False,
                evidence=evidence,
                reason=(
                    f"expected only {rel_path} to change, but changed={changed} "
                    "(did the rewrite touch tests or other files?)"
                ),
            )

        # (b) the repo's own test suite passes against the patched copy.
        args = pytest_args if pytest_args is not None else ["-q"]
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *args],
            cwd=dst_repo,
            capture_output=True,
            text=True,
            timeout=300,
        )
        tail = (proc.stdout + proc.stderr).strip().splitlines()
        evidence["pytest_returncode"] = proc.returncode
        evidence["pytest_tail"] = "\n".join(tail[-15:])  # enough to confirm by eye
        if proc.returncode != 0:
            return Verdict(
                confirmed=False,
                evidence=evidence,
                reason=f"tests failed after rewriting {rel_path} (exit {proc.returncode})",
            )

        return Verdict(
            confirmed=True,
            evidence=evidence,
            reason=(
                f"migrated {rel_path}: '{pattern}' {before_count} -> 0, tests pass, "
                "only that file changed"
            ),
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
