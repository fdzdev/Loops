# Loop authoring spec

Read this before adding a loop. Every loop in `loops/` follows the same shape so
they're interchangeable and reviewable. The core library (`agentloops/`) gives
you the plumbing; your job is the **verifier**.

## The contract

A loop supplies three things to `agentloops.run_loop`:

- `generate() -> Iterable[Candidate]` — the work generator. Called once per
  round; return the full current crop (the driver filters out anything seen).
- `verify(candidate) -> Verdict` — the star. Prefer a **deterministic** check
  (a test passes, an exploit lands, totals reconcile). Use
  `agentloops.adversarial_vote` only when no deterministic check exists.
- a `state` (`agentloops.JsonlState`) and a `budget` (`agentloops.Budget`).

A `Candidate` is any object with a `.dedupe_key` property (a stable string). A
Pydantic model with a `dedupe_key` property is the easy choice.

## Core API you build against (exact signatures)

```python
from agentloops import run_loop, Verdict, LoopResult, LLM, Budget, JsonlState, adversarial_vote

llm = LLM(budget)                       # tiers: llm.strong, llm.mid, llm.cheap (model id strings)
obj  = llm.structured(model=llm.strong, system=..., user=..., schema=MyPydanticModel)  # -> MyPydanticModel
txt  = llm.text(model=llm.cheap, system=..., user=..., effort="medium")                 # -> str

Verdict(confirmed=bool, evidence=dict, reason=str)

run_loop(generate=..., verify=..., state=JsonlState(run_dir), budget=Budget(max_usd=2.0),
         max_rounds=20, dry_rounds_to_stop=2, on_confirm=None, on_reject=None) -> LoopResult
```

## Anthropic API rules (do NOT get these wrong)

- Model ids only: `claude-opus-4-8` (strong default), `claude-sonnet-4-6` (mid),
  `claude-haiku-4-5` (cheap), `claude-fable-5` (max capability). Never invent ids,
  never add date suffixes.
- **Always go through the `LLM` wrapper.** Do not call `anthropic` directly — the
  wrapper handles thinking/effort gating and budget tracking for you. If you call
  the SDK directly you will get the gating wrong and the budget guard won't see it.
- The wrapper already knows: adaptive thinking and `effort` only go to
  opus/sonnet/fable, never to haiku; structured calls don't set effort. You don't
  need to think about this — just use `llm.structured` / `llm.text`.
- Make a structured call cheaper by passing a cheaper `model`, never by fiddling
  with effort.

## Verifier rules

- A model that proposes a thing must NOT be the one that confirms it. The verdict
  comes from running code (tests/exploits/reconciliation) or from
  `adversarial_vote` in a fresh context — never from asking the generator "did
  that work?".
- Write the goal like an adversary will satisfy it. State the anti-gaming clause
  in code and in the README (e.g. "deleting a test fails verification", "the diff
  may not weaken an assertion", "the canary must appear in the response").
- Put the proof in `Verdict.evidence` so a human can confirm the result in under a
  minute (the failing-then-passing test output, the exploit request + response,
  the reconciliation numbers).

## Files every loop directory must contain

`loops/<name>/`
- `__init__.py` (can be empty) — makes it a package.
- `verifier.py` — the verifier, isolated so it's the first thing a reader sees.
- `loop.py` — defines `build(run_dir, budget, ...)` wiring generate+verify+state,
  and a `main()` that runs it and prints a short report.
- `__main__.py` — `from .loop import main; main()` so `python -m loops.<name>` runs it.
- `README.md` — what it does, **the verifier** (front and center), why it pays
  (latency / parallelism / tirelessness / direct revenue), the gaming trap, and
  whether it runs out-of-the-box or needs your infrastructure.
- demo target (if runnable) — a bundled thing to run against: a vulnerable demo
  app, a sample repo with a known bug, a flaky test, a messy CSV, etc. Keep demo
  dependencies in the standard library where possible.

## Runnable vs needs-infrastructure

Mark each loop honestly in its README:
- **Runnable** — ships a demo target; `python -m loops.<name>` works with only an
  `ANTHROPIC_API_KEY` (plus pip extras if noted). Prefer this.
- **Needs infrastructure** — the verifier reads a real external signal (live ad
  metrics, a CRM, prod telemetry). Ship a `Protocol` for that signal plus a
  simulated provider so the loop still RUNS as a demo, and document the real hook.

## Style

- Python 3.10+, type hints, small functions, comments that explain *why*.
- Keep each loop self-contained in its directory. Shared logic belongs in
  `agentloops/`, not copied between loops.
- No live network calls at import time. Network only inside `verify`/`generate`
  when the loop actually runs.
