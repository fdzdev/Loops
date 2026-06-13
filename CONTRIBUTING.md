# Contributing a loop

A loop in `loops/` is a self-contained, self-verifying agent loop. They all share
one shape so they're interchangeable and reviewable. Read
[LOOP_AUTHORING_SPEC.md](LOOP_AUTHORING_SPEC.md) first — it has the exact
signatures and the Anthropic API rules. This file is the short version: the
contract, the one rule that matters, and the checklist.

## The contract

Your loop supplies three things to `agentloops.run_loop`:

- `generate() -> Iterable[Candidate]` — the work generator, called once per round.
  Return the full current crop; the driver filters out anything already seen, so
  it's fine to re-scan or re-query the whole set every round. A `Candidate` is any
  object with a stable `.dedupe_key` property (a Pydantic model is the easy
  choice).
- `verify(candidate) -> Verdict` — the star. See the rule below.
- a `state` (`agentloops.JsonlState`) and a `budget` (`agentloops.Budget`).

Everything else — dedupe against everything ever seen, mark-seen-before-verify,
stop on dry/round-cap/budget, durable resumable state, model tiering, budget
tracking — is already handled by the core library. Don't reimplement it in a
loop; if you need shared logic, it belongs in `agentloops/`, not copied between
loops.

## The verifier-honesty rule (the one that matters)

**The model that proposes a thing must never be the one that confirms it.** The
verdict comes from running code — a test passes, an exploit lands, totals
reconcile — or, only when no deterministic check exists, from
`agentloops.adversarial_vote` running fresh-context skeptics whose default is to
refute. Never from asking the generator "did that work?".

Two corollaries, and you must satisfy both in code *and* state them in your
README:

- **Write the goal like an adversary will satisfy it.** "All tests pass" is gamed
  by deleting the tests; "no crash" is gamed by dropping the rows that don't
  parse. Close every such cheat with an explicit gate (the `tests/` dir must be
  byte-identical before and after; the totals must reconcile to a trusted
  reference; the canary must appear in the response). Document the gaming trap and
  the gate that shuts it.
- **Put the proof in `Verdict.evidence`.** A human should be able to confirm any
  result in under a minute — the red→green test output, the exploit request and
  response, the reconciliation numbers.

If your verifier has a known limit (e.g. an AST check that counts assertions but
can't tell a weakened one from an intact one), say so plainly in the README and
carry whatever a reviewer needs to catch it by eye in the evidence.

## Runnable vs needs-infrastructure

Mark your loop honestly in its README:

- **Runnable** — ships a demo target; `python -m loops.<name>` works with only
  `ANTHROPIC_API_KEY` (plus any noted pip extras). Prefer this. Keep demo deps in
  the standard library where you can.
- **Needs infrastructure** — the verifier reads a real external signal (live ad
  metrics, a CRM, prod telemetry). Ship a `Protocol` for that signal plus a
  simulated provider so the loop still *runs* as a demo, and document the real
  hook. See `adcreative` (sim metrics provider) and `leads` (Protocol enricher +
  fixture DNS resolver) for the pattern.

No live network calls at import time — network only inside `verify`/`generate`
when the loop actually runs.

## File checklist

Create `loops/<name>/` with:

- [ ] `__init__.py` — can be empty; makes it a package.
- [ ] `verifier.py` — the verifier, isolated so it's the first thing a reader
      sees.
- [ ] `loop.py` — a `build(run_dir, budget, ...)` that wires generate + verify +
      state, and a `main()` that runs it and prints a short report.
- [ ] `__main__.py` — `from .loop import main; main()` so `python -m loops.<name>`
      runs it.
- [ ] `README.md` — what it does, **the verifier front and center**, why it pays
      (latency / parallelism / tirelessness / direct revenue), the gaming trap,
      and whether it runs out-of-the-box or needs infrastructure.
- [ ] a demo target if runnable — a vulnerable app, a repo with a known bug, a
      flaky test, a messy CSV, etc.

Then add a row to the loop table in [README.md](README.md).

## Before you open a PR

```bash
python3 -m compileall agentloops loops      # whole repo compiles
python -m loops.<name>                      # your loop runs end-to-end on its demo
```

Style: Python 3.10+, type hints, small functions, comments that explain *why*.
Keep each loop self-contained in its directory.
