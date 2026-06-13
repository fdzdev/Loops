# adcreative — ROAS-graded ad-creative generation

**Who it's for / what it saves you:** a growth marketer keeps only the ad variants the market actually rewarded — spend follows the metrics signal, not gut.

Generate ad-creative variants, then keep only the ones the **market** confirms.

## The verifier (this is the product)

`verifier.py :: verify_creative` is **deterministic**. It never asks the model
that wrote the ad whether the ad is good. It reads the variant's performance from
a `MetricsProvider` and confirms a creative **iff**:

1. it has accumulated at least `min_impressions` impressions (default `10000`), **and**
2. the **Wilson score lower bound** of its CTR clears `ctr_threshold`
   (default `0.030`), **and**
3. (optionally) its observed **ROAS** clears `roas_threshold` (default `2.0`) on
   at least `min_conversions_for_roas` conversions.

Two of those gates exist purely to resist gaming (see below). The verdict is
arithmetic over counts the provider reports — `clicks / impressions`,
`revenue / spend` — so a human can re-check any call by hand from
`Verdict.evidence` in under a minute. Example evidence on a confirm:

```json
{
  "impressions": 12000, "clicks": 540, "conversions": 71,
  "spend_usd": 432.0, "revenue_usd": 2840.0,
  "observed_ctr": 0.045, "ctr_wilson_lower_bound": 0.0414,
  "observed_roas": 6.57, "ctr_threshold": 0.03, "roas_threshold": 2.0,
  "min_impressions": 10000, "gate": "passed"
}
```

## Why it pays

Direct revenue. Copywriting is cheap to generate and expensive to judge — the
bottleneck is knowing which variant actually earns. This loop runs creative
generation tirelessly and in parallel, but spends real confidence only on what
the metrics signal confirms beats threshold. The output is a shortlist of
creatives backed by impressions, not vibes: you ship the winners and kill the
rest with the numbers attached.

## The gaming trap (and the gate that closes it)

**Short windows overfit.** A creative that scores 1 click on 8 impressions reads
as a 12.5% CTR and would "win" against any sane threshold — it just got lucky.
An adversary (or an over-eager optimizer) loves thin windows because noise looks
like signal.

The verifier closes this two ways, both in code and stated here:

- **Min-impressions gate.** Below `min_impressions` the verdict is **reject**
  with reason `insufficient impressions` — never a confirm, never a silent pass.
  The driver verifies each creative exactly once (a seen key is filtered out of
  later rounds), so before that single verify the loop serves the creative enough
  impressions to reach the floor and reads the verdict on that full sample. It is
  graded on `>= min_impressions` of real served data, never on a thin first
  window.
- **Confidence gate.** Even at the floor, observed CTR is noisy, so we compare
  the **Wilson lower bound** (not the raw rate) against the threshold. A creative
  clears the bar on evidence, not on a lucky run. ROAS is likewise gated on a
  minimum conversion count so one fluke sale can't carry it.

The dedupe key is a hash of the creative text, so the generator can't farm a
better grade by re-wording around the hash — and in sim mode each creative's
hidden true CTR is derived deterministically from that same key.

## Runnable status: needs-infra-demo-sim (runs out-of-the-box in sim mode)

The verifier reads a real external signal (live ad metrics). Per the spec we ship
the **`MetricsProvider` Protocol** plus a **seeded simulator** so the loop runs
end-to-end as a demo, and document the real hook.

- **SIM mode (default).** `SimulatedMetricsProvider` is a seeded multi-armed-bandit
  sim: each creative has a hidden "true" CTR, the provider serves impressions and
  returns noisy observed metrics. Reproducible via `--sim-seed`. No network.
- **REAL mode (the hook).** Inject a `MetaAdsProvider`-style provider that reads
  live insights from Meta / Google / TikTok Ads. The stub in `provider.py`
  documents exactly which two methods to implement (`serve`, `metrics`) and how to
  map a platform insights response into `CreativeMetrics`. The verifier does not
  change — the min-impressions and confidence gates are exactly what you want
  against a cold-start ad that got lucky in its first hour of real traffic.

### Run it

```bash
export ANTHROPIC_API_KEY=...        # generator (llm.strong) needs this
python -m loops.adcreative          # sim provider, demo brief, ~$2 budget cap
python -m loops.adcreative --brief "Product: ..." --ctr-threshold 0.04
```

The generator (`claude-opus-4-8` via `llm.strong`) writes variants; the verifier
and the sim provider run fully offline. The report prints each confirmed winner
with its CTR / Wilson lower bound / ROAS / impressions, plus the sim's hidden
true CTR so you can confirm the verifier graded the right arm as a winner.
