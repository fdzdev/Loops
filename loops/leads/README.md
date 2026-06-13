# leads — ICP lead qualification + enrichment

**Who it's for / what it saves you:** an SDR or founder hands in a messy lead list and gets back only the leads code-confirmed against their ICP — reps call real, deliverable, in-profile fits instead of burning hours on dead domains.

Reads a messy `leads.csv`, enriches each lead with a cheap model, and confirms
only the leads that an **ideal customer profile (ICP)** would actually accept —
where "accept" is decided by code, not by the model.

## The verifier (the star)

`verifier.verify_lead` is **deterministic**. A lead is `confirmed=True` iff
*every* check passes; each check reads a fact, never the model's opinion:

| check | what it proves | source |
| --- | --- | --- |
| `email_valid` | the address is a single, well-formed addr-spec | regex over the email |
| `domain_resolves` | the email domain has a real A/AAAA record | `socket.getaddrinfo` (live) or a fixture (demo) |
| `headcount_ok` | `headcount >= rules.min_headcount` | CSV / data-provider number |
| `industry_in_icp` | enriched industry ∈ `allowed_industries` | model guess, treated as a claim |
| `country_in_icp` | country ∈ `allowed_countries` | CSV / data-provider geo |

The ICP policy lives in [`icp_rules.json`](./icp_rules.json) (min headcount,
allowed industries, allowed geos) — qualification is a config edit, not a code
change. The verdict's `evidence` records every check's value, so a human can
confirm a yes/no in well under a minute.

```python
Verdict(
  confirmed=True,
  evidence={"email_valid": True, "domain": "navsoft.example", "domain_resolves": True,
            "headcount": 1200, "headcount_ok": True, "min_headcount": 50,
            "industry": "software", "industry_in_icp": True, "country": "US", "country_in_icp": True},
  reason="ICP match: NavSoft Systems — industry=software, headcount=1200>=50, US, domain resolves",
)
```

Domain resolution sits behind a `DomainResolver` Protocol with two
implementations: `SocketResolver` (live `getaddrinfo`, **fails closed** on any
error so we never confirm an unreachable domain) and `FixtureResolver` (the
offline demo). The deterministic logic is identical either way.

## The gaming trap

The executor uses `llm.cheap` to guess each lead's industry/segment from its
company name. That guess is the **one field a model could fabricate** to force
an ICP match — so the verifier treats `industry` as an *untrusted claim*:

- It only counts if it lands in the `allowed_industries` allowlist, AND
- the two facts a model **cannot** invent — a **resolving domain** and a real
  **headcount** — must independently hold.

A lead is therefore *never* confirmed on the model's guess alone. The verdict
comes from running code (regex + DNS + numeric/membership tests), not from
asking the generator "is this a good lead?". **In real use, re-verify
freshness**: a domain that resolved last quarter may be parked today and a
headcount may be stale — run the loop again rather than trusting a cached
`confirmed`.

## Why it pays

- **Tirelessness / scale** — qualifies and enriches an unbounded lead list
  unattended, with a hard budget ceiling; no rep burns hours on dead domains.
- **Direct revenue** — the SDR/AE pipeline only ever sees ICP-qualified,
  reachable leads, so selling time goes to leads that can actually convert.
- **Trust** — every confirmation carries machine-checkable evidence, so a
  human can audit the qualified list instead of re-doing it.

## Runnable status

**Offline demo: out-of-the-box** (needs only `ANTHROPIC_API_KEY` for the cheap
enrichment call). DNS checks use the bundled fixtures, so no network is needed
for verification.

```bash
python -m loops.leads               # offline DNS fixtures (default)
python -m loops.leads --live-dns    # NEEDS-NET: real getaddrinfo
```

The bundled [`leads.csv`](./leads.csv) is deliberately messy: a malformed email
(`linus`, no domain), a sub-threshold headcount, a non-resolving domain
(`no-such-domain-xyzqwerty.example`, marked unresolvable in the fixtures), and
several clean ICP fits — so you can see each check fire.

### Needs-infrastructure: real enrichment

Industry/headcount/geo from a real **data provider** (Clearbit, Apollo,
ZoomInfo, …) needs an API key and live network. The seam is a Protocol:

```python
class Enricher(Protocol):
    def lookup(self, *, company: str, domain: str) -> EnrichedFirmographics: ...
```

The demo ships `FakeEnricher` (returns all-unknown, so the loop falls back to
CSV facts + the model's industry guess). Implement `lookup` against your
provider and pass it to `build(..., enricher=YourProvider())`; provider-confirmed
firmographics override both the CSV and the model guess. When `--live-dns` is
set, `SocketResolver` does the real DNS check.
```python
from loops.leads.loop import build
from agentloops import Budget, run_loop

built = build("/tmp/leads-run", Budget(max_usd=2.0),
              offline=False, enricher=MyApolloEnricher())
run_loop(generate=built.generate, verify=built.verify,
         state=built.state, budget=built.budget)
```
