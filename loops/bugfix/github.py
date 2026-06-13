"""GitHub plumbing for the bugfix watcher: read stale issues, work in an
isolated git worktree, open a DRAFT PR.

This is the *only* place the loop touches the outside world. It shells out to
the `gh` CLI (already authenticated, JSON output) and falls back to the GitHub
REST API over urllib if `gh` is missing — both stdlib + the tool a human dev
already has, so there are no new pip dependencies.

The verifier (verifier.py) stays pure: it never calls anything here. This module
only (1) decides *which* issues are worth a look (stale + unowned), and (2)
performs the side effects a confirmed fix needs (branch, commit, push, PR). All
side effects are gated behind an explicit `open_pr` flag by the caller; in
dry-run nothing here pushes or creates anything.

Staleness rule (the whole point): an issue is a candidate only if it has been
idle for >= `min_idle_hours` AND has no assignee. `updatedAt` already advances on
the last comment or linked commit, so "idle since last activity" is the signal.
The loop deliberately picks up only what a human left untouched — it complements
the team's queue instead of racing a person who is actively on a ticket.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# --- shelling out ----------------------------------------------------------


class GitHubError(RuntimeError):
    """A `gh`/`git` invocation failed or returned something unparseable."""


def _have_gh() -> bool:
    return shutil.which("gh") is not None


def _run(cmd: list[str], *, cwd: Optional[str] = None, check: bool = True) -> str:
    """Run a subprocess and return stdout. Raises GitHubError on failure so the
    loop can skip an issue rather than crash the whole watch cycle."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitHubError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    return proc.stdout


def _git(args: list[str], *, cwd: str) -> str:
    return _run(["git", *args], cwd=cwd)


# --- issues ----------------------------------------------------------------


@dataclass
class Issue:
    number: int
    title: str
    body: str
    updated_at: str  # ISO-8601, UTC (e.g. "2026-06-01T12:00:00Z")
    assignees: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    def idle_hours(self, now: Optional[datetime] = None) -> float:
        """Hours since the last activity (comment / commit / edit), which is what
        `updatedAt` tracks."""
        now = now or datetime.now(timezone.utc)
        updated = _parse_iso8601_utc(self.updated_at)
        return (now - updated).total_seconds() / 3600.0


def _parse_iso8601_utc(value: str) -> datetime:
    """Parse GitHub's timestamps to an aware UTC datetime. GitHub emits a
    trailing 'Z'; fromisoformat handles '+00:00' across versions, so normalize."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_issue(raw: dict) -> Issue:
    """Coerce one issue dict (gh JSON or REST JSON) into an Issue. gh nests
    assignees/labels as lists of objects with a 'login'/'name' key; REST uses
    the same shape, so one normalizer covers both."""

    def _names(items, key: str) -> list[str]:
        out = []
        for it in items or []:
            if isinstance(it, dict):
                val = it.get(key) or it.get("login") or it.get("name")
                if val:
                    out.append(val)
            elif it:
                out.append(str(it))
        return out

    return Issue(
        number=int(raw["number"]),
        title=raw.get("title") or "",
        body=raw.get("body") or "",
        updated_at=raw.get("updatedAt") or raw.get("updated_at") or "",
        assignees=_names(raw.get("assignees"), "login"),
        labels=_names(raw.get("labels"), "name"),
    )


def _fetch_issues_gh(remote: str, label: Optional[str], limit: int) -> list[dict]:
    cmd = [
        "gh", "issue", "list",
        "--repo", remote,
        "--state", "open",
        "--json", "number,title,body,updatedAt,assignees,labels",
        "--limit", str(limit),
    ]
    if label:
        cmd += ["--label", label]
    out = _run(cmd)
    try:
        return json.loads(out or "[]")
    except json.JSONDecodeError as e:  # pragma: no cover - defensive
        raise GitHubError(f"could not parse gh issue list JSON: {e}") from e


def _fetch_issues_rest(remote: str, label: Optional[str], limit: int) -> list[dict]:
    """Documented fallback when `gh` is not installed. Uses the REST API over
    urllib (stdlib) with a token from GH_TOKEN / GITHUB_TOKEN. Returns dicts in
    the same shape `_normalize_issue` expects (camelCase 'updatedAt' added)."""
    import urllib.parse
    import urllib.request

    owner_repo = remote.strip("/")
    params = {"state": "open", "per_page": str(min(limit, 100))}
    if label:
        params["labels"] = label
    url = f"https://api.github.com/repos/{owner_repo}/issues?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec - fixed api host
        data = json.loads(resp.read().decode("utf-8"))

    issues = []
    for it in data:
        # The REST issues endpoint also returns PRs; drop those.
        if "pull_request" in it:
            continue
        it["updatedAt"] = it.get("updated_at")
        issues.append(it)
    return issues[:limit]


def list_candidate_issues(
    remote: str,
    *,
    min_idle_hours: float = 5.0,
    label: Optional[str] = None,
    limit: int = 20,
    now: Optional[datetime] = None,
) -> list[Issue]:
    """Open issues that are STALE: idle >= `min_idle_hours` AND unassigned.

    `updatedAt` reflects the last comment/commit/edit, so "idle since last
    activity" is the staleness signal — exactly what tells us a human walked
    away from the ticket. We keep an issue only when (a) nobody is assigned (no
    one is on it) and (b) it has not moved in over `min_idle_hours` hours. That
    is what makes the loop a good teammate: it never grabs work a person is
    actively touching.
    """
    raw = _fetch_issues_gh(remote, label, limit) if _have_gh() else _fetch_issues_rest(
        remote, label, limit
    )
    now = now or datetime.now(timezone.utc)
    out: list[Issue] = []
    for item in raw:
        issue = _normalize_issue(item)
        if issue.assignees:
            continue  # somebody owns it; leave it alone
        if not issue.updated_at:
            continue  # can't judge staleness without a timestamp; skip
        if issue.idle_hours(now) < min_idle_hours:
            continue  # touched too recently; a human may still be on it
        out.append(issue)
    return out


# --- worktree lifecycle ----------------------------------------------------


@dataclass
class Worktree:
    """An isolated checkout + branch for one fix attempt. Lives in a temp dir so
    concurrent attempts never collide and the main clone is never dirtied."""

    path: str
    branch: str
    base_clone: str


def create_worktree(clone_dir: str, branch: str, *, base: Optional[str] = None) -> Worktree:
    """Add a fresh git worktree + branch off `base` (default: current HEAD of the
    clone). The worktree is what the verifier runs in, so a failed attempt is
    discarded by removing the directory — the clone stays clean."""
    base = base or _git(["rev-parse", "HEAD"], cwd=clone_dir).strip()
    parent = os.path.join(clone_dir, ".bugfix-worktrees")
    os.makedirs(parent, exist_ok=True)
    wt_path = os.path.join(parent, branch.replace("/", "_"))
    # -B resets the branch if a previous attempt left it around.
    _git(["worktree", "add", "-B", branch, wt_path, base], cwd=clone_dir)
    return Worktree(path=wt_path, branch=branch, base_clone=clone_dir)


def remove_worktree(wt: Worktree) -> None:
    """Tear down a worktree (used for rejected attempts and after a dry run).
    Best-effort: never raise from cleanup."""
    try:
        _git(["worktree", "remove", "--force", wt.path], cwd=wt.base_clone)
    except GitHubError:
        shutil.rmtree(wt.path, ignore_errors=True)


def worktree_diff(wt: Worktree) -> str:
    """The full diff of everything changed/added in the worktree (the fix + the
    repro test). Shown in dry-run and embedded in the PR body for review."""
    _git(["add", "-A"], cwd=wt.path)
    return _git(["diff", "--cached"], cwd=wt.path)


def commit_all(wt: Worktree, message: str) -> str:
    """Stage everything and commit. Returns the new commit sha."""
    _git(["add", "-A"], cwd=wt.path)
    _git(
        ["-c", "user.name=bugfix-loop", "-c", "user.email=bugfix-loop@local", "commit", "-m", message],
        cwd=wt.path,
    )
    return _git(["rev-parse", "HEAD"], cwd=wt.path).strip()


def push_branch(wt: Worktree, *, remote_name: str = "origin") -> None:
    _git(["push", "--set-upstream", remote_name, wt.branch], cwd=wt.path)


# --- pull requests ---------------------------------------------------------


def open_draft_pr(
    *,
    remote: str,
    head_branch: str,
    title: str,
    body: str,
    cwd: Optional[str] = None,
) -> str:
    """Open a DRAFT PR via `gh pr create --draft` and return its URL.

    Always a draft, never merged — the loop's job ends at "here is a fix with
    red->green proof attached"; a human decides whether it ships. The body
    should already contain `Fixes #N` so merging the PR closes the issue.
    """
    cmd = [
        "gh", "pr", "create",
        "--repo", remote,
        "--draft",
        "--head", head_branch,
        "--title", title,
        "--body", body,
    ]
    out = _run(cmd, cwd=cwd)
    return out.strip()


def build_pr_body(issue: Issue, verdict_evidence: dict, *, remote: str) -> str:
    """The PR body doubles as the handoff: it links the issue (`Fixes #N`) and
    pastes the red->green proof so a reviewer confirms it in under a minute."""
    changed = verdict_evidence.get("changed_files", [])
    red = verdict_evidence.get("red_repro_output", "")
    green = verdict_evidence.get("green_repro_output", "")
    full = verdict_evidence.get("full_suite_output", "")
    return (
        f"Fixes #{issue.number}\n\n"
        f"Automated fix proposed by the agentloops **bugfix** watcher for a stale, "
        f"unassigned issue in `{remote}`. **Draft only — not auto-merged.**\n\n"
        f"## Verification (deterministic)\n\n"
        f"A repro test was written first and confirmed RED on the unpatched code, "
        f"then the fix made it GREEN with the full suite still passing. No "
        f"pre-existing test file was modified.\n\n"
        f"**Files changed:** {', '.join(changed) if changed else '(none reported)'}\n\n"
        f"<details><summary>Repro test: RED before the fix</summary>\n\n"
        f"```\n{red}\n```\n\n</details>\n\n"
        f"<details><summary>Repro test: GREEN after the fix</summary>\n\n"
        f"```\n{green}\n```\n\n</details>\n\n"
        f"<details><summary>Full suite: green</summary>\n\n"
        f"```\n{full}\n```\n\n</details>\n"
    )
