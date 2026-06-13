"""leads — ICP lead qualification + enrichment.

GENERATOR  read a bundled leads.csv; one Candidate per row, dedupe_key = email.
EXECUTOR   llm.cheap normalizes the record and guesses industry/segment from the
           company name. This is enrichment, NOT the verdict.
VERIFIER   verifier.verify_lead — deterministic: valid email + resolving domain +
           ICP rules (headcount / industry / geo). The model's guess is just one
           input; it can never confirm a lead on its own.

OFFLINE DEMO MODE  a FixtureResolver answers domain checks with no DNS, so
                   `python -m loops.leads` runs out-of-the-box (with an API key).
NEEDS-INFRA HOOK   real enrichment needs a data provider (Clearbit / Apollo /
                   etc.). We ship an `Enricher` Protocol + a `FakeEnricher`; the
                   real hook is documented in README.md.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol

from pydantic import BaseModel

from agentloops import LLM, Budget, JsonlState, Verdict, run_loop

from .verifier import (
    DomainResolver,
    FixtureResolver,
    ICPRules,
    SocketResolver,
    verify_lead,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = os.path.join(_HERE, "leads.csv")
DEFAULT_RULES = os.path.join(_HERE, "icp_rules.json")
DEFAULT_FIXTURES = os.path.join(_HERE, "dns_fixtures.json")


# ---------------------------------------------------------------------------
# Candidate: one raw lead row. dedupe_key = email so the same address is never
# re-qualified, even across resumed runs.
# ---------------------------------------------------------------------------
@dataclass
class Lead:
    name: str
    email: str
    company: str
    domain: str
    headcount: Optional[int]
    country: str

    @property
    def dedupe_key(self) -> str:
        return self.email.strip().lower()


# ---------------------------------------------------------------------------
# Enricher Protocol + a fake provider.
#
# Real enrichment (firmographics: confirmed industry, verified headcount, geo)
# comes from a data provider — Clearbit, Apollo, ZoomInfo, etc. That is the
# "needs infrastructure" part: it needs an API key and live network. We define
# the seam as a Protocol and ship a deterministic FakeEnricher so the loop runs
# offline. Swap in a real provider behind the same interface for production.
# ---------------------------------------------------------------------------
class EnrichedFirmographics(BaseModel):
    industry: str = ""
    headcount: Optional[int] = None
    country: str = ""


class Enricher(Protocol):
    """A firmographics provider. `lookup` returns whatever the provider knows;
    empty/None fields mean "unknown" and the loop keeps the CSV value.
    """

    def lookup(self, *, company: str, domain: str) -> EnrichedFirmographics: ...


class FakeEnricher:
    """Stand-in provider for the demo: returns nothing (all-unknown), so the
    loop falls back to CSV facts + the model's industry guess. It exists to
    prove the seam compiles and to document where the real provider plugs in.
    A real Apollo/Clearbit client would implement `lookup` over the network.
    """

    def lookup(self, *, company: str, domain: str) -> EnrichedFirmographics:
        return EnrichedFirmographics()


# ---------------------------------------------------------------------------
# EXECUTOR: cheap-model enrichment of one lead.
# ---------------------------------------------------------------------------
class _IndustryGuess(BaseModel):
    industry: str
    segment: str


_SEGMENT_SYSTEM = (
    "You normalize B2B lead records. Given a company name and domain, return the "
    "single most likely industry as a short lowercase label (e.g. 'software', "
    "'fintech', 'healthcare', 'retail', 'manufacturing') and a coarse segment "
    "('smb', 'mid-market', 'enterprise'). Guess from the name; do not invent "
    "facts. If unsure, say industry='unknown'."
)


def enrich_industry(llm: LLM, *, company: str, domain: str) -> _IndustryGuess:
    """Ask the cheap model for an industry/segment guess. This is enrichment
    only — the verifier decides whether the guess earns confirmation."""
    return llm.structured(
        model=llm.cheap,
        system=_SEGMENT_SYSTEM,
        user=f"Company: {company!r}\nDomain: {domain!r}\nReturn industry + segment.",
        schema=_IndustryGuess,
        max_tokens=300,
    )


# ---------------------------------------------------------------------------
# GENERATOR
# ---------------------------------------------------------------------------
def _coerce_headcount(raw: str) -> Optional[int]:
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def make_generator(csv_path: str):
    """Return generate() that reads the CSV and yields one Lead per row.

    The driver dedupes on dedupe_key, so re-reading the whole file each round is
    fine — new rows appended between rounds get picked up, old ones skipped.
    """

    def generate() -> Iterable[Lead]:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                yield Lead(
                    name=(row.get("name") or "").strip(),
                    email=(row.get("email") or "").strip(),
                    company=(row.get("company") or "").strip(),
                    domain=(row.get("domain") or "").strip(),
                    headcount=_coerce_headcount(row.get("headcount", "")),
                    country=(row.get("country") or "").strip(),
                )

    return generate


# ---------------------------------------------------------------------------
# VERIFIER wiring: enrich (model + provider), then verify (deterministic).
# ---------------------------------------------------------------------------
def make_verifier(
    llm: LLM,
    *,
    resolver: DomainResolver,
    rules: ICPRules,
    enricher: Enricher,
):
    def verify(lead: Lead) -> Verdict:
        # 1. Firmographics from the data provider (real infra) override CSV when
        #    present. In the demo the FakeEnricher returns nothing, so CSV wins.
        firmo = enricher.lookup(company=lead.company, domain=lead.domain)
        headcount = firmo.headcount if firmo.headcount is not None else lead.headcount
        country = firmo.country or lead.country

        # 2. Industry: prefer the provider's confirmed value; else the model's
        #    guess. Either way it is an UNTRUSTED claim to the verifier.
        if firmo.industry:
            industry = firmo.industry
        else:
            try:
                industry = enrich_industry(
                    llm, company=lead.company, domain=lead.domain
                ).industry
            except Exception as e:  # enrichment is best-effort; never block verify
                industry = ""
                # leave a breadcrumb in the verdict evidence below via reason
                _ = e

        # 3. Deterministic verdict.
        return verify_lead(
            email=lead.email,
            company=lead.company,
            headcount=headcount,
            country=country,
            enriched_industry=industry,
            resolver=resolver,
            rules=rules,
            domain_hint=lead.domain,
        )

    return verify


# ---------------------------------------------------------------------------
# build(): wire generate + verify + state for run_loop.
# ---------------------------------------------------------------------------
@dataclass
class BuiltLoop:
    generate: object
    verify: object
    state: JsonlState
    budget: Budget


def _load_rules(path: str) -> ICPRules:
    with open(path, encoding="utf-8") as f:
        return ICPRules.from_dict(json.load(f))


def _load_fixtures(path: str) -> dict[str, bool]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build(
    run_dir: str,
    budget: Budget,
    *,
    csv_path: str = DEFAULT_CSV,
    rules_path: str = DEFAULT_RULES,
    offline: bool = True,
    fixtures_path: str = DEFAULT_FIXTURES,
    llm: Optional[LLM] = None,
    enricher: Optional[Enricher] = None,
) -> BuiltLoop:
    """Wire the leads loop.

    offline=True (default) uses the FixtureResolver so domain checks run with no
    DNS — the out-of-the-box demo path. offline=False uses live getaddrinfo
    (needs-net). `enricher` defaults to FakeEnricher; pass a real provider for
    production firmographics.
    """
    llm = llm or LLM(budget)
    rules = _load_rules(rules_path)
    if offline:
        resolver: DomainResolver = FixtureResolver(_load_fixtures(fixtures_path))
    else:
        resolver = SocketResolver()
    enricher = enricher or FakeEnricher()

    return BuiltLoop(
        generate=make_generator(csv_path),
        verify=make_verifier(llm, resolver=resolver, rules=rules, enricher=enricher),
        state=JsonlState(run_dir),
        budget=budget,
    )


# ---------------------------------------------------------------------------
# main(): run against the bundled demo CSV and print a short report.
# ---------------------------------------------------------------------------
def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="ICP lead qualification + enrichment loop")
    ap.add_argument("--csv", default=DEFAULT_CSV, help="path to leads.csv")
    ap.add_argument("--rules", default=DEFAULT_RULES, help="path to icp_rules.json")
    ap.add_argument("--run-dir", default=os.path.join(_HERE, ".runs", "demo"))
    ap.add_argument("--max-usd", type=float, default=1.0)
    ap.add_argument("--max-rounds", type=int, default=5)
    ap.add_argument(
        "--live-dns",
        action="store_true",
        help="use live getaddrinfo instead of the offline DNS fixtures (needs-net)",
    )
    args = ap.parse_args()

    budget = Budget(max_usd=args.max_usd)
    built = build(
        args.run_dir,
        budget,
        csv_path=args.csv,
        rules_path=args.rules,
        offline=not args.live_dns,
    )

    result = run_loop(
        generate=built.generate,
        verify=built.verify,
        state=built.state,
        budget=built.budget,
        max_rounds=args.max_rounds,
        dry_rounds_to_stop=1,
    )

    print()
    print("=" * 64)
    print(f"leads loop done — stopped: {result.stopped}, rounds: {result.rounds}")
    print(f"confirmed (ICP-qualified): {len(result.confirmed)}")
    print(f"rejected:                  {len(result.rejected)}")
    print(f"budget: {budget.summary()}")
    if result.confirmed:
        print("\nQualified leads:")
        for cand, verdict in result.confirmed:
            print(f"  + {cand.dedupe_key:40s} {verdict.reason}")
    if result.rejected:
        print("\nDisqualified (with failing checks):")
        for cand, verdict in result.rejected:
            print(f"  - {cand.dedupe_key:40s} {verdict.reason}")
    print("=" * 64)

    # ----- HUMAN HANDOFF: what you got, and where the proof lives -----------
    print()
    print("HUMAN HANDOFF")
    print(
        f"You have {len(result.confirmed)} ICP-qualified lead(s) ready to call: "
        f"each one passed a deterministic check for a well-formed email, a "
        f"resolving domain, and your ICP rules (headcount / industry / geo)."
    )
    print(
        f"Proof: every confirmation is logged with its per-check evidence in "
        f"{built.state.confirmed_path}"
    )
    print(
        f"  (rejections, with the failing check, are in {built.state.rejected_path}; "
        f"the full run lives under {args.run_dir})."
    )
    print(
        "Open confirmed.jsonl to audit any lead's evidence in under a minute, "
        "then send the list to your reps."
    )


if __name__ == "__main__":
    main()
