"""vuln_scan — exploit-verified vulnerability scanner. THE FLAGSHIP.

Wiring:
  - GENERATOR: a STRONG model (claude-opus-4-8) reads the demo app's SOURCE and
    returns a list of suspected injection sinks (sink, file, line, param,
    taint_path). Each becomes a Candidate keyed by f"{file}:{line}:{param}".
  - DEMO TARGET: demo_app.py, started here as a subprocess on 127.0.0.1 before
    scanning and torn down after.
  - VERIFIER (verifier.py): a CHEAP model proposes one HTTP exploit; we FIRE it
    and confirm iff a per-finding canary echoes back. The model proposes; the
    canary decides.

Run it:  python -m loops.vuln_scan   (needs ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from typing import Iterable, Optional

import requests
from pydantic import BaseModel, Field

from agentloops import LLM, Budget, JsonlState, Verdict, run_loop

from .verifier import make_verifier

# The source file the scanner reads. The line numbers it reports point here.
_DEMO_SOURCE = os.path.join(os.path.dirname(__file__), "demo_app.py")


class Finding(BaseModel):
    """One suspected injection sink the scanner surfaced. This is the Candidate."""

    sink: str = Field(description="Sink class, e.g. 'os command injection', 'eval', 'sql injection'.")
    file: str = Field(description="Source file the sink lives in.")
    line: int = Field(description="1-based line number of the sink.")
    param: str = Field(description="The HTTP parameter that carries the taint, e.g. 'host'.")
    taint_path: str = Field(
        description="Short source->sink trace, e.g. 'GET /ping ?host -> os.popen(\"ping -c1 \"+host)'."
    )

    @property
    def dedupe_key(self) -> str:
        # Stable across rounds: same sink at same location => same candidate.
        return f"{self.file}:{self.line}:{self.param}"


class _Findings(BaseModel):
    findings: list[Finding] = Field(default_factory=list)


_SCANNER_SYSTEM = (
    "You are a static application security scanner. You are given the full SOURCE "
    "of a small Python web app. Find every place where attacker-controlled HTTP "
    "input reaches a dangerous sink (OS command execution, eval/exec, SQL string "
    "interpolation, unescaped reflection). For each, report the sink class, the "
    "file, the 1-based line number of the sink call, the HTTP parameter that "
    "carries the taint, and a one-line source->sink taint path. Report only real "
    "data-flow findings; do not invent endpoints that aren't in the source."
)


def _scan_source(llm: LLM, source_path: str) -> list[Finding]:
    """GENERATOR: strong model reads the source and lists injection sinks."""
    with open(source_path) as f:
        source = f.read()
    # Number the lines so the model can cite accurate `line` values.
    numbered = "\n".join(f"{i+1:4}: {ln}" for i, ln in enumerate(source.splitlines()))
    result = llm.structured(
        model=llm.strong,
        system=_SCANNER_SYSTEM,
        user=(
            f"FILE: {os.path.basename(source_path)}\n"
            f"SOURCE (line-numbered):\n{numbered}\n\n"
            "Return every injection sink you can justify from this source."
        ),
        schema=_Findings,
        max_tokens=3000,
    )
    # Normalize the file field to the basename so dedupe keys are stable
    # regardless of how the model spelled the path.
    base = os.path.basename(source_path)
    for fnd in result.findings:
        fnd.file = base
    return result.findings


# --------------------------------------------------------------------------- #
# Demo target lifecycle: start the vulnerable app as a subprocess, wait for it
# to accept connections, hand back the base URL, and guarantee teardown.
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(base_url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last: Optional[Exception] = None
    while time.time() < deadline:
        try:
            requests.get(base_url + "/", timeout=1.0)
            return
        except requests.RequestException as exc:  # not up yet
            last = exc
            time.sleep(0.1)
    raise RuntimeError(f"demo app did not come up at {base_url}: {last}")


class DemoTarget:
    """Context manager: vulnerable demo app on 127.0.0.1 for the loop's lifetime."""

    def __init__(self, host: str = "127.0.0.1", port: Optional[int] = None):
        self.host = host
        self.port = port or _free_port()
        self.base_url = f"http://{self.host}:{self.port}"
        self._proc: Optional[subprocess.Popen] = None

    def __enter__(self) -> "DemoTarget":
        # Launch as a module so imports resolve from the repo root.
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "loops.vuln_scan.demo_app",
                "--host",
                self.host,
                "--port",
                str(self.port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_ready(self.base_url)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


def build(
    run_dir: str,
    budget: Budget,
    *,
    base_url: str,
    llm: Optional[LLM] = None,
    source_path: str = _DEMO_SOURCE,
):
    """Wire generate + verify + state for run_loop, bound to a running demo app.

    Returns a dict of kwargs ready to splat into `run_loop`. The caller owns the
    demo app lifecycle (see main()), so `base_url` must already be live.
    """
    llm = llm or LLM(budget)

    def generate() -> Iterable[Finding]:
        return _scan_source(llm, source_path)

    verify = make_verifier(llm, base_url)

    return {
        "generate": generate,
        "verify": verify,
        "state": JsonlState(run_dir),
        "budget": budget,
    }


def main() -> None:
    run_dir = os.environ.get("VULN_SCAN_RUN_DIR", os.path.join(os.getcwd(), ".vuln_scan_run"))
    budget = Budget(max_usd=float(os.environ.get("VULN_SCAN_MAX_USD", "2.0")))

    # Start the deliberately-vulnerable target, scan + exploit-verify, tear down.
    with DemoTarget() as target:
        print(f"[vuln_scan] demo target live at {target.base_url}")
        budget_for_llm = budget
        llm = LLM(budget_for_llm)
        kwargs = build(run_dir, budget, base_url=target.base_url, llm=llm)
        result = run_loop(
            **kwargs,
            max_rounds=int(os.environ.get("VULN_SCAN_MAX_ROUNDS", "8")),
            dry_rounds_to_stop=2,
        )

    # Short report. The proof for each lives in confirmed.jsonl / Verdict.evidence.
    print("\n=== vuln_scan report ===")
    print(f"rounds={result.rounds} stopped={result.stopped} {budget.summary()}")
    print(f"CONFIRMED (exploited) {len(result.confirmed)} | rejected {len(result.rejected)}")
    for cand, verdict in result.confirmed:
        req = verdict.evidence.get("request", {})
        print(f"  [EXPLOITED] {cand.dedupe_key}  {verdict.evidence.get('sink')}")
        print(f"     request : {req.get('method', 'GET')} {req.get('url')}")
        print(f"     canary  : {verdict.evidence.get('canary')}")
        print(f"     echoed  : {verdict.evidence.get('response_snippet')}")
    if not result.confirmed:
        print("  (none confirmed — every finding is a guess until the canary lands)")
    print(f"\nfull evidence: {os.path.join(run_dir, 'confirmed.jsonl')}")

    # HUMAN HANDOFF: plain-English summary of what you got and where the proof is.
    evidence_path = os.path.join(run_dir, "confirmed.jsonl")
    n = len(result.confirmed)
    print("\n=== HUMAN HANDOFF ===")
    if n:
        print(
            f"You have {n} confirmed exploit{'s' if n != 1 else ''} — each one was actually "
            "fired at the running app and forced its own per-finding canary back into the "
            "response, so none of these are guesses you need to triage."
        )
        print(
            f"The proof is in {evidence_path}: one JSON line per confirmed finding with the "
            "exact request fired, the canary, the HTTP status, and the response snippet. "
            "Re-fire any line against a live target to reproduce it in under a minute."
        )
    else:
        print(
            "No exploits confirmed this run: every candidate sink the scanner proposed failed "
            "its live canary test, so there is nothing for you to triage."
        )
        print(
            f"The run log is at {run_dir} (evidence file {evidence_path} is present but empty). "
            "Nothing here is a finding you need to chase down."
        )


if __name__ == "__main__":
    main()
