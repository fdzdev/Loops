# agentloops

A loop is a verification problem, not an automation problem. The hard part of
running an agent unattended is never generating work — models do that cheaply and
endlessly. The hard part is knowing which of that work is real. So in every loop
here, **the verifier is the product**: a deterministic check (a test passes, an
exploit lands, totals reconcile) decides what counts, and the model that proposed
the work never gets to grade it. The core library wires up the five parts of a
loop correctly — durable state, a generator, an executor, a verifier, a stop
condition with a hard budget — so each loop only has to supply its verifier, and
every confirmation ships with machine-checkable evidence a human can re-check in
under a minute.

## Install

```bash
pip install -e .                 # core: anthropic, pydantic, requests
pip install -e '.[finmodel]'     # adds openpyxl, required only by the finmodel loop
pip install -e '.[dev]'          # adds pytest, used by bugfix / flaky / migrate
export ANTHROPIC_API_KEY=sk-ant-...
```

Python 3.10+. Some loops also need `pytest` on your `PATH` (bugfix, flaky,
migrate); the `[dev]` extra covers that.

## Quickstart

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m loops.vuln_scan
```

That runs the flagship loop: it spins up a deliberately vulnerable demo app on
`127.0.0.1`, scans it, fires a real exploit per finding, and confirms only the
ones that bounce a fresh canary back in the response. Every loop runs the same
way — `python -m loops.<name>` — and ships its own demo target.

## The loops

| Loop | What it does | Verifier | Pays via | Runs |
| --- | --- | --- | --- | --- |
| [vuln_scan](loops/vuln_scan/README.md) | Upgrades static-scan guesses to exploit-verified facts against a live target | Fires a per-finding HTTP exploit carrying a fresh unguessable canary; confirmed iff the canary appears in the response (and the endpoint didn't merely echo it) | direct revenue / tirelessness | out-of-the-box |
| [bugfix](loops/bugfix/README.md) | Reads an issue tracker, patches each open bug, confirms only when tests agree | Applies the patch to a fresh copy, demands the repro test goes red→green and the full suite stays green, with the `tests/` dir byte-identical before *and* after each run | tirelessness / parallelism | out-of-the-box (needs pytest) |
| [flaky](loops/flaky/README.md) | Retires flaky tests by making them deterministic | Runs the target test 30× in fresh subprocesses (all must pass) plus an AST check that no assertion was dropped and no skip/xfail was added | tirelessness / parallelism | out-of-the-box (needs pytest) |
| [migrate](loops/migrate/README.md) | Moves a codebase off a deprecated API, one file at a time | Per-file gate on a throwaway copy: compiles, suite passes, zero live uses of the old pattern remain, and only the intended file changed | tirelessness / parallelism | out-of-the-box (needs pytest) |
| [perf](loops/perf/README.md) | Hunts speedups for registered hot functions | Subprocess harness checks output equals baseline on all cases + property tests, then confirms only a measured median speedup over a >10% threshold | latency / tirelessness | out-of-the-box |
| [etl](loops/etl/README.md) | Self-heals a malformed data file the production transform chokes on | Runs the repaired transform and checks four invariants — exact schema, row-count reconciliation, required non-nulls, and `sum(revenue)` matching a trusted reference total | tirelessness / direct revenue | out-of-the-box |
| [finmodel](loops/finmodel/README.md) | Builds multi-year financial models as real `.xlsx` files | Independently recomputes every figure in plain Python, reconciles the spreadsheet cell-by-cell, and grades each rubric criterion as a concrete boolean | direct revenue / tirelessness | out-of-the-box (needs `[finmodel]`) |
| [research](loops/research/README.md) | Turns a question into atomic claims, each backed by a fetched, checked source | Fetches the cited URL (dead link → reject), then runs an adversarial refutation panel against the fetched page text in a fresh context | tirelessness / direct revenue | needs-net (offline fixture mode out-of-the-box) |
| [leads](loops/leads/README.md) | Enriches and ICP-qualifies a messy lead list | Deterministic checks: valid email, resolving domain (fails closed), headcount/geo thresholds, industry in allowlist — the model's industry guess is treated as an untrusted claim | direct revenue / tirelessness | out-of-the-box (`--live-dns` needs net) |
| [adcreative](loops/adcreative/README.md) | Generates ad-creative variants and keeps only market-confirmed winners | Reads metrics from a provider; confirms iff impressions clear a floor and the Wilson lower bound of CTR (plus optional ROAS) clears threshold | direct revenue / parallelism | sim (ships `MetricsProvider` Protocol + seeded simulator; real-ads hook documented) |

"Pays via" is the economic argument for running the loop unattended: it removes a
**latency** bottleneck, exploits **parallelism** across independent candidates,
does **tireless** grind a human won't sustain, or produces **direct revenue**.
"Runs" is honest about infrastructure — *out-of-the-box* needs only an API key
(and noted extras); *needs-net* hits the network in its default mode; *sim* reads
a real external signal in production but ships a simulated provider so the demo
still runs end-to-end.

## How a loop is built

Every loop is the same five parts, and the core library (`agentloops/`) owns four
of them so your loop can focus on the fifth:

1. **State** — `JsonlState(run_dir)`: append-only `seen` / `confirmed` /
   `rejected` JSONL. The driver dedupes against *everything ever seen* (including
   rejections), marks a candidate seen *before* verifying it (a crash can't
   resurrect it), and resumes a run by re-pointing at the same directory.
2. **Generator** — `generate() -> Iterable[Candidate]`, called once per round.
   Return the full current crop; the driver filters out anything already seen. A
   `Candidate` is any object with a stable `.dedupe_key` (a Pydantic model is the
   easy choice).
3. **Executor** — the open-ended work, usually an LLM call, that turns a candidate
   into a concrete proposal (a patch, a rewrite, a request, a spreadsheet).
4. **Verifier** — `verify(candidate) -> Verdict`. The star. Prefer a
   **deterministic** check; fall back to `adversarial_vote` only when no
   deterministic check exists. Put the proof in `Verdict.evidence`.
5. **Stop condition** — `run_loop` stops on whichever comes first: the work goes
   dry, a round cap, or a hard `Budget` ceiling, so an adversarially large input
   can't run the bill to the moon.

The core API you build against:

```python
from agentloops import run_loop, Verdict, LoopResult, LLM, Budget, JsonlState, adversarial_vote

llm = LLM(budget)                       # tiers: llm.strong, llm.mid, llm.cheap (model id strings)
obj = llm.structured(model=llm.strong, system=..., user=..., schema=MyPydanticModel)  # -> MyPydanticModel
txt = llm.text(model=llm.cheap, system=..., user=..., effort="medium")                # -> str

Verdict(confirmed=bool, evidence=dict, reason=str)

run_loop(generate=..., verify=..., state=JsonlState(run_dir), budget=Budget(max_usd=2.0),
         max_rounds=20, dry_rounds_to_stop=2, on_confirm=None, on_reject=None) -> LoopResult
```

**Model tiering.** Spend intelligence where the task is open-ended (`llm.strong`,
`claude-opus-4-8`) and a cheap model where it's mechanical (`llm.cheap`,
`claude-haiku-4-5`); `llm.mid` (`claude-sonnet-4-6`) sits between them. vuln_scan
is the clearest example — the strong model reasons over source, the cheap model
just writes one request. Always go through the `LLM` wrapper rather than calling
`anthropic` directly: it handles thinking/effort gating per model and tracks the
budget for you. Make a structured call cheaper by passing a cheaper `model`, never
by fiddling with `effort`. Override the tiers with `AGENTLOOPS_MODEL_STRONG` /
`_MID` / `_CHEAP` (see `.env.example`).

## You don't need a frontier model for every part

The reflex to reach for a frontier model on every call is what makes loops
needlessly expensive. In a well-built loop most of the cost is in slots that
don't need it:

- **The verifier is usually deterministic.** A test passing, a canary appearing
  in an HTTP response, totals reconciling — these cost *zero* model tokens. The
  trust comes from running code, not from spending on a smart grader.
- **Where grading is model-based, it's a narrow task.** "Does this fetched page
  support this claim?" or "which of these ad variants reads best?" is the kind of
  judgment a small or free model handles fine. Diversity and independence matter
  more than raw capability here.
- **Mechanical sub-tasks** — normalizing a lead, writing one exploit request,
  classifying — are cheap-model work by definition.

So the only slot that often *earns* a frontier model is the open-ended
**executor** (reason over a whole codebase, propose a real fix). The rest can run
on something cheap, and the easiest "cheap" is **free**: NVIDIA publishes hosted,
OpenAI-compatible models on [build.nvidia.com](https://build.nvidia.com) with a
complimentary API endpoint — for example
[`nvidia/nemotron-3-ultra-550b-a55b`](https://build.nvidia.com/nvidia/nemotron-3-ultra-550b-a55b),
a 550B reasoning model. Any model id that doesn't start with `claude-` routes to
the OpenAI-compatible endpoint (`AGENTLOOPS_OPENAI_BASE_URL`, default NVIDIA),
authenticated with `NVIDIA_API_KEY`. So you can run a strong Claude executor and a
free Nemotron verifier in the same loop:

```bash
pip install -e '.[nvidia]'                          # adds the openai client
export ANTHROPIC_API_KEY=sk-ant-...                 # strong executor
export NVIDIA_API_KEY=nvapi-...                     # free cheap/mid tiers
export AGENTLOOPS_MODEL_CHEAP=nvidia/nemotron-3-ultra-550b-a55b
export AGENTLOOPS_MODEL_MID=nvidia/nemotron-3-ultra-550b-a55b
python -m loops.research
```

`llm.structured` and `llm.text` behave identically across providers — loops never
have to know which one answered — and the budget guard prices free models at zero,
so your ceiling reflects only what you actually pay for. The lesson the article
makes in prose, this repo makes in config: match the model to the slot, and most
slots are not frontier work.

## More

- [LOOP_AUTHORING_SPEC.md](LOOP_AUTHORING_SPEC.md) — the loop contract, exact core
  signatures, the Anthropic API rules, and the verifier-honesty rules in full.
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to add a new loop, the file checklist,
  and the one rule that matters most.
- Each loop's own `README.md` has the verifier front and center, its gaming trap,
  and exactly how to run it.
