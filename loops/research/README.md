# research — citation-verified research / competitive intel

**Who it's for / what it saves you:** an analyst gets a brief where every claim is backed by a source that was actually fetched and checked to support it — no hallucinated citations to chase down.

A loop that turns a research question into **atomic claims, each backed by a
source that was actually fetched and actually checked**. The generator proposes;
the verifier grounds. A claim survives only if its cited page exists and
supports it.

## The verifier (the star)

`verifier.py` runs a two-stage, grounded check on every claim. The generator
model's confidence counts for nothing here.

1. **Fetch the cited URL.** Over HTTP with a 15s timeout and a real user agent
   (`fetch_live`), or — in offline demo mode — by reading a bundled
   `fixtures/<name>.txt` (`fetch_offline`). The page text is tag-stripped with
   the standard library (no `bs4`). Any DNS failure, timeout, non-2xx status, or
   empty body is an immediate **rejection**: `Verdict(confirmed=False, reason="source unreachable …")`.
   No model is consulted for a dead link.

2. **Adversarially check support, in a fresh context.** The *fetched page text*
   (never the generator's memory) is handed to
   `agentloops.adversarial_vote(model=llm.mid)`. A panel of independent skeptics
   is told to **REFUTE** that this specific page supports the claim, and to
   default to "refuted" when the page is silent or only tangentially related.
   The claim is confirmed only if a majority of the panel fail to refute it.

A claim is **confirmed iff the page is reachable AND the page supports it.** The
verdict's `evidence` carries the **URL, HTTP status, and a supporting snippet**
pulled from the fetched page — enough for a human to confirm in under a minute.
The generator (`llm.strong`) is never asked "is this true?"; the truth signal
comes from fetched bytes plus a fresh-context refutation panel.

## Why it pays

- **Tirelessness + parallelism.** Fact-checking 40 competitive-intel claims by
  hand is an afternoon of opening tabs and skimming. The loop fetches and
  adversarially checks each one unattended, and the dedupe key (a hash of the
  claim text) means a claim is verified exactly once across resumed runs.
- **Direct revenue / risk avoidance.** Competitive briefs, sales battle-cards,
  and analyst memos go out with citations that were *verified to support the
  sentence they're attached to* — not hallucinated links. One fabricated "their
  SLA is only 99%" in a deal review can cost a deal or a lawsuit.
- **Latency.** The answer you can trust arrives with its receipts attached
  (`confirmed.jsonl`), so the human reviews evidence instead of re-researching.

## The gaming trap

The whole point: **"the model thinks it's true" is never enough.** An adversary
(or an over-eager generator) trying to get a false claim confirmed must defeat
two things it cannot fake:

- A claim with an **unreachable source** (dead link, 404, timeout, or — in the
  demo — a `fixture://` URL we never shipped) is rejected before any model runs.
- A claim with a **reachable-but-non-supporting source** (the page is real but
  says something else, or nothing on the topic) is rejected by the refutation
  panel, which judges support **only from the fetched page text**.

The bundled demo claims include both traps on purpose: an "Initech IPO in 2026"
claim cited to a page that explicitly says no IPO is planned, an off-topic
citation, and a claim whose source fixture does not exist. All three are
rejected; the three well-cited claims are confirmed.

## Runnable

**needs-net — with an out-of-the-box offline fixture mode.**

- **Offline demo (out-of-the-box):** reads bundled `fixtures/*.txt` and the
  bundled demo claims — **no internet required.** Only the verifier's
  adversarial panel needs an `ANTHROPIC_API_KEY`.

  ```bash
  python -m loops.research --offline
  # or: RESEARCH_OFFLINE=1 python -m loops.research
  ```

- **Live mode (needs net):** the generator (`llm.strong`) proposes claims for a
  real question and the verifier fetches real URLs. Requires `ANTHROPIC_API_KEY`
  and `pip install requests`.

  ```bash
  python -m loops.research --question "How does Acme Cloud price its Team plan vs competitors?"
  ```

Flags: `--max-usd` (budget ceiling, default 2.0), `--max-rounds`,
`--panel-size` (refutation panel size, default 3), `--run-dir` (where the
`seen/confirmed/rejected.jsonl` evidence lands).

## Files

- `verifier.py` — fetch + extract + adversarial support check (the star).
- `loop.py` — `Claim` candidate, generators (live + offline), `build()`, `main()`.
- `fixtures/` — bundled source pages: `acme_pricing.txt`,
  `widget_market_report.txt`, `initech_about.txt`.
- `__main__.py` — `python -m loops.research`.
