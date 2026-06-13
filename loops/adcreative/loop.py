"""adcreative — ROAS-graded ad-creative generation.

GENERATOR (llm.strong): given a product brief, propose ad-creative variants
(headline + body). One Candidate per variant; the dedupe_key is a hash of the
creative text, so the loop never re-grades the same creative and the generator
can't farm a better grade by re-wording around it.

VERIFIER (deterministic, see verifier.py): read each variant's performance from a
`MetricsProvider` and confirm only when it beats a CTR/ROAS threshold AFTER a
minimum number of impressions.

PROVIDER: in sim mode (default) a seeded multi-armed-bandit simulator serves
impressions and reports noisy metrics, so the loop RUNS end-to-end with just an
API key. In real mode you inject a Meta/Google Ads provider (see provider.py).
"""
from __future__ import annotations

import argparse
import hashlib
import os
from typing import Iterable, Optional

from pydantic import BaseModel, Field

from agentloops import LLM, Budget, JsonlState, Verdict, run_loop

from .provider import MetricsProvider, SimulatedMetricsProvider
from .verifier import verify_creative

# --------------------------------------------------------------------------- #
# Candidate
# --------------------------------------------------------------------------- #


class AdCreative(BaseModel):
    """One ad-creative variant. `dedupe_key` is a hash of the creative text, so
    two prompts that yield the same headline+body collapse to one candidate."""

    headline: str = Field(..., description="Punchy ad headline, <= ~60 chars.")
    body: str = Field(..., description="1-2 sentence ad body / supporting copy.")
    angle: str = Field("", description="The marketing angle this variant tests.")

    @property
    def text(self) -> str:
        return f"{self.headline}\n{self.body}".strip()

    @property
    def dedupe_key(self) -> str:
        digest = hashlib.sha256(self.text.lower().encode("utf-8")).hexdigest()
        return f"ad-{digest[:16]}"


class _CreativeBatch(BaseModel):
    """Structured envelope for one generation round."""

    variants: list[AdCreative] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #

_SYSTEM = (
    "You are a senior direct-response copywriter. You write distinct, high-CTR ad "
    "creatives that each test a DIFFERENT angle (benefit, fear, social proof, "
    "curiosity, price). Avoid near-duplicates. Keep headlines tight and bodies to "
    "one or two sentences."
)


def make_generator(llm: LLM, *, brief: str, variants_per_round: int):
    """Return a `generate()` that asks llm.strong for fresh creative variants.

    The driver dedupes by key, so returning the full crop each round is fine; we
    nudge the model toward novelty by telling it how many distinct angles to test.
    """

    def generate() -> Iterable[AdCreative]:
        user = (
            f"PRODUCT BRIEF:\n{brief}\n\n"
            f"Write {variants_per_round} DISTINCT ad-creative variants. Each must "
            f"test a different angle. Return headline, body, and the angle for each."
        )
        batch = llm.structured(
            model=llm.strong,
            system=_SYSTEM,
            user=user,
            schema=_CreativeBatch,
        )
        return batch.variants

    return generate


# --------------------------------------------------------------------------- #
# build()
# --------------------------------------------------------------------------- #


def build(
    run_dir: str,
    budget: Budget,
    *,
    brief: str,
    provider: Optional[MetricsProvider] = None,
    llm: Optional[LLM] = None,
    variants_per_round: int = 6,
    impressions_per_round: int = 4000,
    min_impressions: int = 10000,
    ctr_threshold: float = 0.030,
    roas_threshold: Optional[float] = 2.0,
    sim_seed: int = 1234,
):
    """Wire generate + verify + state into args for `run_loop`.

    `provider` defaults to the seeded simulator so the loop runs out-of-the-box.
    Inject a real `MetricsProvider` (e.g. `MetaAdsProvider`) for production.

    Each round we (1) ask the generator for variants and (2) serve every known
    creative another `impressions_per_round` impressions (real platforms serve
    continuously; the sim needs the nudge). The driver verifies each creative
    EXACTLY ONCE — a seen key is filtered out of later rounds — so `verify` itself
    tops the creative up to at least `min_impressions` before reading metrics, and
    the verdict is decided on that full sample, never on a thin first window.
    """
    llm = llm or LLM(budget)
    provider = provider or SimulatedMetricsProvider(seed=sim_seed)
    state = JsonlState(run_dir)

    generate = make_generator(llm, brief=brief, variants_per_round=variants_per_round)

    # Track every key we've generated so we can keep serving impressions to
    # creatives that haven't yet reached the floor.
    served_keys: set[str] = set()

    def generate_and_serve() -> Iterable[AdCreative]:
        variants = list(generate())
        for v in variants:
            served_keys.add(v.dedupe_key)
        # Real platforms serve continuously; the sim needs an explicit nudge. Top
        # up impressions for ALL known creatives, not just this round's, so the
        # floor is reachable over multiple rounds.
        for key in served_keys:
            provider.serve(key, impressions_per_round)
        return variants

    def verify(candidate: AdCreative) -> Verdict:
        # The driver marks a candidate seen and verifies it EXACTLY ONCE (it is
        # filtered out of every later round), so the verdict has to be reachable on
        # this single call. Earlier rounds' top-ups for already-seen keys are never
        # re-read. Therefore: before reading metrics, serve this creative the
        # deficit needed to reach `min_impressions`, plus one full window of fresh
        # evidence on top. This is NOT gaming the floor — the floor is a minimum
        # sample size for a trustworthy CTR, and we are accumulating that many REAL
        # served impressions before ruling; the Wilson lower-bound gate still
        # decides win/lose on the evidence. A real (auto-serving) provider can make
        # `serve` a no-op; we never spin because we issue a single bounded serve.
        key = candidate.dedupe_key
        have = provider.metrics(key).impressions
        deficit = max(0, min_impressions - have)
        provider.serve(key, deficit + impressions_per_round)
        return verify_creative(
            key=candidate.dedupe_key,
            provider=provider,
            min_impressions=min_impressions,
            ctr_threshold=ctr_threshold,
            roas_threshold=roas_threshold,
        )

    return {
        "generate": generate_and_serve,
        "verify": verify,
        "state": state,
        "budget": budget,
        "_provider": provider,  # returned for the report; run_loop ignores extras
    }


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #

_DEMO_BRIEF = (
    "Product: 'Lumen', a $39/mo sleep-tracking smart ring. Audience: busy "
    "professionals 28-45 who feel chronically under-rested. Promise: actionable, "
    "no-nonsense sleep insights without a wrist gadget. Tone: confident, calm, "
    "data-driven. Goal: drive free-trial signups."
)


def main() -> None:
    parser = argparse.ArgumentParser(description="ROAS-graded ad-creative generation loop.")
    parser.add_argument("--run-dir", default=".runs/adcreative")
    parser.add_argument("--brief", default=_DEMO_BRIEF, help="Product brief to write creatives for.")
    parser.add_argument("--max-usd", type=float, default=2.0)
    parser.add_argument("--max-rounds", type=int, default=8)
    parser.add_argument("--variants-per-round", type=int, default=6)
    parser.add_argument("--impressions-per-round", type=int, default=4000)
    parser.add_argument("--min-impressions", type=int, default=10000)
    parser.add_argument("--ctr-threshold", type=float, default=0.030)
    parser.add_argument("--roas-threshold", type=float, default=2.0)
    parser.add_argument("--sim-seed", type=int, default=1234)
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. The verifier and provider run offline, "
            "but the generator needs the API. Set the key and re-run."
        )

    budget = Budget(max_usd=args.max_usd)
    wiring = build(
        args.run_dir,
        budget,
        brief=args.brief,
        variants_per_round=args.variants_per_round,
        impressions_per_round=args.impressions_per_round,
        min_impressions=args.min_impressions,
        ctr_threshold=args.ctr_threshold,
        roas_threshold=args.roas_threshold,
        sim_seed=args.sim_seed,
    )
    provider = wiring.pop("_provider")

    result = run_loop(
        generate=wiring["generate"],
        verify=wiring["verify"],
        state=wiring["state"],
        budget=wiring["budget"],
        max_rounds=args.max_rounds,
        dry_rounds_to_stop=2,
    )

    print("\n=== adcreative report ===")
    print(f"rounds={result.rounds} stopped={result.stopped} {budget.summary()}")
    print(f"confirmed winners: {len(result.confirmed)} | rejected: {len(result.rejected)}")
    for cand, verdict in result.confirmed:
        ev = verdict.evidence
        # If running the sim, surface the hidden truth so a human can confirm the
        # verifier graded the right arm as a winner.
        truth = ""
        if hasattr(provider, "true_ctr"):
            truth = f" (sim true CTR {provider.true_ctr(cand.dedupe_key):.4f})"
        print(f"  WIN  {cand.dedupe_key}: {cand.headline!r}")
        print(
            f"       CTR={ev.get('observed_ctr')} lb={ev.get('ctr_wilson_lower_bound')} "
            f"ROAS={ev.get('observed_roas')} impr={ev.get('impressions')}{truth}"
        )

    # --- HUMAN HANDOFF -----------------------------------------------------
    # Plain-English summary of what the run produced and where the proof lives,
    # so a marketer can act without reading the loop internals.
    n_win = len(result.confirmed)
    print("\n--- here's what you got ---")
    if n_win:
        winners = ", ".join(repr(cand.headline) for cand, _ in result.confirmed)
        print(
            f"{n_win} ad creative(s) cleared the bar on real served impressions "
            f"(CTR Wilson lower bound >= {args.ctr_threshold}, "
            f"ROAS >= {args.roas_threshold} on >= {args.min_impressions} impressions). "
            f"Ship these, kill the rest: {winners}."
        )
    else:
        print(
            "No creative cleared the evidence bar this run — nothing earned the spend. "
            "That is a real result: don't ship any of these as-is. Loosen the brief or "
            "raise the round budget and re-run."
        )
    print(
        f"Proof: per-creative verdicts with the exact counts behind each call "
        f"(impressions / clicks / CTR Wilson lower bound / ROAS) are written to the "
        f"evidence JSONL in {args.run_dir!r}. Every confirm is re-checkable by hand "
        f"from those numbers."
    )


if __name__ == "__main__":
    main()
