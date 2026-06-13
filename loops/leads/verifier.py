"""The verifier — the star of this loop.

A lead is CONFIRMED iff EVERY check below passes. Every check is deterministic:
it reads a fact (a regex match, a DNS lookup, a number, a config membership
test), never the model's opinion. The cheap model in loop.py *enriches* a lead
(guessing its industry/segment from the company name) but its guess is just one
input to these rules — the verdict is computed here, in code.

The four checks:
  1. email_valid     — the address is syntactically a single addr-spec.
  2. domain_resolves  — the domain has at least one A/AAAA record (or, in demo
                        mode, a fixture says it resolves). Catches typo'd and
                        parked domains that a model would happily "enrich".
  3. headcount_ok     — company headcount >= rules.min_headcount.
  4. icp_match        — enriched industry is in rules.allowed_industries AND the
                        country is in rules.allowed_countries.

Anti-gaming: the enriched `industry` is supplied by the executor model, so it is
the one field an adversary (or a confused model) could fabricate to force a
match. We treat it as an untrusted claim: it must land in the allowlist, AND the
two facts the model cannot invent — a *resolving* domain and a real headcount —
must also hold. A lead can never be confirmed on the industry guess alone.
"""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from typing import Optional, Protocol

from agentloops import Verdict


# ---------------------------------------------------------------------------
# Domain resolution: a Protocol so the deterministic check is identical online
# and offline. Live DNS in real runs; a fixture map in the demo.
# ---------------------------------------------------------------------------
class DomainResolver(Protocol):
    """Returns True iff `domain` has at least one resolvable address record."""

    def resolves(self, domain: str) -> bool: ...


class SocketResolver:
    """Live DNS via socket.getaddrinfo. Used in real (needs-net) runs.

    Fails *closed*: any lookup error (NXDOMAIN, timeout, no network) means
    "does not resolve", so we never confirm a lead on a domain we couldn't
    actually reach. A friendly grader would fail open; we do the opposite.
    """

    def __init__(self, timeout: float = 3.0):
        self._timeout = timeout

    def resolves(self, domain: str) -> bool:
        if not domain:
            return False
        prev = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self._timeout)
        try:
            socket.getaddrinfo(domain, None)
            return True
        except (socket.gaierror, socket.timeout, OSError):
            return False
        finally:
            socket.setdefaulttimeout(prev)


class FixtureResolver:
    """Offline resolver for demo mode: a domain resolves iff the fixture map
    says so. Lets the deterministic domain check run with no DNS at all, and
    lets the demo include a known-bad domain that the verifier must reject.
    """

    def __init__(self, fixtures: dict[str, bool]):
        # ignore JSON comment keys like "_comment"
        self._fixtures = {k: v for k, v in fixtures.items() if not k.startswith("_")}

    def resolves(self, domain: str) -> bool:
        return bool(self._fixtures.get(domain, False))


# ---------------------------------------------------------------------------
# ICP rules: a small, reviewable config. The verifier reads these; it does not
# hardcode them, so qualification policy is a config change, not a code change.
# ---------------------------------------------------------------------------
@dataclass
class ICPRules:
    min_headcount: int = 0
    allowed_industries: frozenset[str] = field(default_factory=frozenset)
    allowed_countries: frozenset[str] = field(default_factory=frozenset)
    require_domain_resolves: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "ICPRules":
        return cls(
            min_headcount=int(d.get("min_headcount", 0)),
            allowed_industries=frozenset(
                s.strip().lower() for s in d.get("allowed_industries", [])
            ),
            allowed_countries=frozenset(
                s.strip().upper() for s in d.get("allowed_countries", [])
            ),
            require_domain_resolves=bool(d.get("require_domain_resolves", True)),
        )


# A pragmatic single-addr-spec check. We are deliberately conservative: it must
# be exactly one local@domain token, the local part non-empty, the domain a
# dotted name with a 2+ char TLD. Too-clever RFC 5322 regexes accept addresses
# no real MTA would; rejecting those is the safer default for outbound.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@([A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,})$"
)


def email_is_valid(email: str) -> tuple[bool, str]:
    """Returns (ok, domain). domain is "" when the address is invalid."""
    email = (email or "").strip()
    m = _EMAIL_RE.match(email)
    if not m:
        return False, ""
    return True, m.group(1).lower()


def verify_lead(
    *,
    email: str,
    company: str,
    headcount: Optional[int],
    country: str,
    enriched_industry: str,
    resolver: DomainResolver,
    rules: ICPRules,
    domain_hint: str = "",
) -> Verdict:
    """Deterministically decide whether a lead is ICP-qualified.

    `enriched_industry` is the model's guess; it is treated as an untrusted
    claim and only matters if it lands in the allowlist. The hard facts
    (resolving domain, headcount) gate confirmation regardless of the guess.
    """
    checks: dict[str, object] = {}

    # 1. Email syntactically valid. The domain we verify is the email's domain
    #    (the address we'd actually send to), not a separately-supplied hint.
    ok_email, email_domain = email_is_valid(email)
    checks["email_valid"] = ok_email
    domain = email_domain or (domain_hint or "").strip().lower()
    checks["domain"] = domain

    # 2. Domain resolves. Skipped only if the rules explicitly opt out.
    if rules.require_domain_resolves:
        ok_domain = bool(domain) and resolver.resolves(domain)
    else:
        ok_domain = True
    checks["domain_resolves"] = ok_domain

    # 3. Headcount threshold. A missing/garbage headcount fails closed.
    try:
        hc = int(headcount) if headcount is not None else None
    except (TypeError, ValueError):
        hc = None
    ok_headcount = hc is not None and hc >= rules.min_headcount
    checks["headcount"] = hc
    checks["headcount_ok"] = ok_headcount
    checks["min_headcount"] = rules.min_headcount

    # 4. ICP match: industry (model claim) in allowlist AND country in allowlist.
    industry_norm = (enriched_industry or "").strip().lower()
    ok_industry = industry_norm in rules.allowed_industries
    country_norm = (country or "").strip().upper()
    ok_country = (not rules.allowed_countries) or (country_norm in rules.allowed_countries)
    checks["industry"] = industry_norm
    checks["industry_in_icp"] = ok_industry
    checks["country"] = country_norm
    checks["country_in_icp"] = ok_country

    confirmed = ok_email and ok_domain and ok_headcount and ok_industry and ok_country

    if confirmed:
        reason = (
            f"ICP match: {company or domain} — industry={industry_norm}, "
            f"headcount={hc}>={rules.min_headcount}, {country_norm}, domain resolves"
        )
    else:
        failed = [
            name
            for name, ok in (
                ("email_valid", ok_email),
                ("domain_resolves", ok_domain),
                ("headcount_ok", ok_headcount),
                ("industry_in_icp", ok_industry),
                ("country_in_icp", ok_country),
            )
            if not ok
        ]
        reason = f"failed: {', '.join(failed)}"

    return Verdict(confirmed=confirmed, evidence=checks, reason=reason)
