"""Wire the citation-verified research loop: generate claims -> verify sources.

GENERATOR (`llm.strong`): given a research question, produce atomic claims, each
with one candidate source URL. One `Candidate` per claim; `dedupe_key` is a hash
of the claim text so the same claim is never re-verified.

VERIFIER (see verifier.py): fetch the cited URL (or a bundled fixture offline),
extract its text, then run `adversarial_vote` in a fresh context to REFUTE that
the page supports the claim. Confirmed iff reachable AND supported.

OFFLINE DEMO MODE: pass `offline=True` (or set RESEARCH_OFFLINE=1) to read the
bundled `fixtures/*.txt` instead of the network, and to use the bundled demo
claims as the generator output. This makes the demo out-of-the-box runnable
with no internet and no API key for the generator — only the verifier's panel
needs the API.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from typing import Iterable, Optional

from pydantic import BaseModel, Field

from agentloops import LLM, Budget, JsonlState, run_loop

from .verifier import make_verifier

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES_DIR = os.path.join(_HERE, "fixtures")


class Claim(BaseModel):
    """One atomic, checkable assertion plus the single source that backs it."""

    text: str = Field(description="A single atomic factual claim, self-contained.")
    source_url: str = Field(description="One URL whose page content supports the claim.")

    @property
    def dedupe_key(self) -> str:
        # Hash of the claim text: the same assertion is verified once, even if a
        # later round cites a different URL for it.
        digest = hashlib.sha256(self.text.strip().lower().encode("utf-8")).hexdigest()
        return f"claim:{digest[:16]}"


class _ClaimBatch(BaseModel):
    claims: list[Claim]


# ---------------------------------------------------------------------------
# Demo claims for OFFLINE mode. Each cites a `fixture://<name>` URL resolved to
# fixtures/<name>.txt. The set is deliberately mixed so the loop demonstrates
# both confirmation and the gaming trap on a single run, no network required.
# ---------------------------------------------------------------------------
DEMO_CLAIMS: list[Claim] = [
    # SUPPORTED — the pricing page states $12/user/month for the Team plan.
    Claim(
        text="Acme Cloud's Team plan costs $12 per user per month.",
        source_url="fixture://acme_pricing",
    ),
    # SUPPORTED — the market report states a 9% CAGR through 2030.
    Claim(
        text="Globex Research projects the global widget market to grow at a 9% CAGR through 2030.",
        source_url="fixture://widget_market_report",
    ),
    # SUPPORTED — the about page states Initech was founded in 2011.
    Claim(
        text="Initech was founded in 2011.",
        source_url="fixture://initech_about",
    ),
    # TRAP 1 (non-supporting source): the claim is plausible-sounding, but the
    # cited about page explicitly says Initech has NOT announced plans to go
    # public. Page exists, but does not support the claim -> must be rejected.
    Claim(
        text="Initech has announced plans for an IPO in 2026.",
        source_url="fixture://initech_about",
    ),
    # TRAP 2 (off-topic source): true-sounding pricing claim, but pointed at the
    # market report, which says nothing about Acme's free tier seat count.
    Claim(
        text="Acme Cloud's free Starter plan allows up to 3 seats.",
        source_url="fixture://widget_market_report",
    ),
    # TRAP 3 (unreachable source): no fixture by this name ships, so the source
    # is "unreachable" — the verifier rejects before ever consulting the panel.
    Claim(
        text="Hooli leads the global widget market with a 35% share.",
        source_url="fixture://hooli_press_release",
    ),
]


def _generate_offline() -> Iterable[Claim]:
    """Offline generator: return the bundled demo claims (no API call)."""
    return list(DEMO_CLAIMS)


def _make_live_generator(llm: LLM, question: str):
    """Live generator: `llm.strong` proposes atomic claims with source URLs.

    Returns the full crop each round; `run_loop` dedupes by claim hash. The
    generator NEVER decides truth — it only proposes; verifier.py grounds each
    claim against its cited page.
    """

    def generate() -> Iterable[Claim]:
        batch = llm.structured(
            model=llm.strong,
            system=(
                "You are a research analyst building a citation-checked brief. "
                "Produce ATOMIC, individually-checkable factual claims about the "
                "question. For EACH claim, give exactly ONE real source URL whose "
                "page content directly states the claim. Prefer primary sources "
                "(official pages, filings, reports). Do not fabricate URLs; if you "
                "are unsure a page supports a claim, omit the claim."
            ),
            user=f"RESEARCH QUESTION:\n{question}\n\nReturn 5-8 atomic claims with sources.",
            schema=_ClaimBatch,
            max_tokens=4000,
        )
        return batch.claims

    return generate


def build(
    run_dir: str,
    budget: Budget,
    *,
    question: Optional[str] = None,
    offline: bool = False,
    fixtures_dir: str = _FIXTURES_DIR,
    panel_size: int = 3,
    llm: Optional[LLM] = None,
):
    """Wire generate + verify + state into the args `run_loop` expects.

    Returns a dict of kwargs to splat into `run_loop`, plus the `llm`/`budget`
    so `main()` can print a cost summary.
    """
    llm = llm or LLM(budget)

    if offline:
        generate = _generate_offline
    else:
        if not question:
            raise ValueError("live mode needs a research question (use --question)")
        generate = _make_live_generator(llm, question)

    verify = make_verifier(
        llm, offline=offline, fixtures_dir=fixtures_dir, panel_size=panel_size
    )

    return {
        "generate": generate,
        "verify": verify,
        "state": JsonlState(run_dir),
        "budget": budget,
        "_llm": llm,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Citation-verified research loop.")
    parser.add_argument(
        "--question",
        default=None,
        help="Research question for live mode (ignored when --offline).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        default=os.getenv("RESEARCH_OFFLINE", "") not in ("", "0", "false", "False"),
        help="Use bundled fixtures + demo claims instead of the network "
        "(default if RESEARCH_OFFLINE is set). Out-of-the-box runnable.",
    )
    parser.add_argument("--run-dir", default=os.path.join(_HERE, "_run"))
    parser.add_argument("--max-usd", type=float, default=2.0)
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--panel-size", type=int, default=3)
    args = parser.parse_args()

    budget = Budget(max_usd=args.max_usd)
    kwargs = build(
        args.run_dir,
        budget,
        question=args.question,
        offline=args.offline,
        panel_size=args.panel_size,
    )
    llm = kwargs.pop("_llm")

    mode = "OFFLINE (fixtures)" if args.offline else f"LIVE: {args.question!r}"
    print(f"=== research loop — {mode} ===")

    result = run_loop(max_rounds=args.max_rounds, dry_rounds_to_stop=1, **kwargs)

    print("\n--- confirmed claims (source reachable AND supports the claim) ---")
    for cand, verdict in result.confirmed:
        ev = verdict.evidence
        print(f"  [OK] {cand.text}")
        print(f"       source: {ev.get('url')}  (HTTP {ev.get('http_status')})")
        snippet = (ev.get("supporting_snippet") or "").strip()
        if snippet:
            print(f"       snippet: {snippet[:160]}")

    print("\n--- rejected claims (gaming trap: not enough that the model believes it) ---")
    for cand, verdict in result.rejected:
        ev = verdict.evidence
        print(f"  [NO] {cand.text}")
        print(f"       source: {ev.get('url')}  (HTTP {ev.get('http_status')})")
        print(f"       why: {verdict.reason}")

    print(
        f"\nrounds={result.rounds} stopped={result.stopped} "
        f"confirmed={len(result.confirmed)} rejected={len(result.rejected)}"
    )
    print(f"budget: {budget.summary()}")

    # --- HUMAN HANDOFF: what you got, and where the proof is ---
    confirmed_jsonl = os.path.join(args.run_dir, "confirmed.jsonl")
    rejected_jsonl = os.path.join(args.run_dir, "rejected.jsonl")
    topic = "the demo claims" if args.offline else f"{args.question!r}"
    print("\n=== HUMAN HANDOFF ===")
    print(
        f"You have a research brief on {topic} with "
        f"{len(result.confirmed)} claim(s) confirmed and "
        f"{len(result.rejected)} rejected. Every confirmed claim cites a source "
        f"that was fetched and checked to actually support the sentence it's "
        f"attached to — so you can ship it without re-verifying each citation."
    )
    print(
        f"Proof: each confirmed claim above carries its source URL, HTTP status, "
        f"and a supporting snippet pulled from the fetched page."
    )
    print(f"Confirmed claims + evidence (JSONL): {confirmed_jsonl}")
    print(f"Rejected claims + reasons (JSONL):  {rejected_jsonl}")
    print(f"Full run directory: {args.run_dir}")


if __name__ == "__main__":
    main()
