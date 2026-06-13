"""The verifier: a claim is real only if its cited page EXISTS and SUPPORTS it.

This is a *grounded* mixed verifier. It does two things the generator model
cannot fake:

  1. It FETCHES the cited URL over HTTP (or reads a bundled fixture in offline
     demo mode) and extracts the page text. A claim whose source is unreachable
     (DNS failure, 404, timeout) is rejected before any model is consulted.
  2. It hands the *fetched page text* — not the model's memory — to
     `agentloops.adversarial_vote` in a fresh context, asking a panel to REFUTE
     that this specific text supports the claim. The burden of proof is on the
     claim.

GAMING TRAP: "the generator is confident the claim is true" is never enough.
The page must be reachable and the snippet that supports the claim must come
from the page we actually fetched. A confidently-asserted claim backed by a
dead link, a 404, or an off-topic page is rejected every time. The evidence
dict carries the URL, the HTTP status, and the supporting snippet so a human
can confirm the verdict in under a minute.
"""
from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from typing import Optional

from agentloops import LLM, Verdict, adversarial_vote

# Cap how much page text we extract / feed to the panel. Real pages can be huge;
# the relevant evidence for an atomic claim is almost always in the first few KB,
# and an unbounded body would blow the token budget on a single verify call.
_MAX_PAGE_CHARS = 12_000
_FETCH_TIMEOUT_S = 15
_USER_AGENT = "agentloops-research/0.1 (+https://github.com/; citation verifier)"


@dataclass
class FetchResult:
    """What a fetch (network or fixture) yielded. `ok` gates everything else."""

    ok: bool
    status: int  # HTTP status, or 0 for fixtures / pre-network failures
    text: str  # extracted, tag-stripped page text (possibly truncated)
    error: str = ""  # populated when ok is False


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")


def extract_text(raw: str) -> str:
    """Strip scripts/styles/tags and collapse whitespace.

    Deliberately dependency-free (no bs4): the loop's offline demo must run on
    the standard library alone, and a crude strip is enough to let the panel see
    whether the claim's substance appears on the page.
    """
    no_scripts = _SCRIPT_STYLE_RE.sub(" ", raw)
    no_tags = _TAG_RE.sub(" ", no_scripts)
    unescaped = html.unescape(no_tags)
    collapsed = _WS_RE.sub(" ", unescaped).strip()
    return collapsed[:_MAX_PAGE_CHARS]


def _fixture_path(fixtures_dir: str, url: str) -> Optional[str]:
    """Map a demo URL to a bundled fixture file.

    Offline claims cite `fixture://<name>` URLs; we resolve `<name>.txt` inside
    the fixtures directory. Anything else (a real http URL, or a missing
    fixture) returns None so the verifier reports an honest "unreachable".
    """
    prefix = "fixture://"
    if not url.startswith(prefix):
        return None
    name = url[len(prefix):].strip().strip("/")
    # Refuse path traversal — a fixture name is a bare slug, never a path.
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    candidate = os.path.join(fixtures_dir, f"{name}.txt")
    return candidate if os.path.isfile(candidate) else None


def fetch_offline(url: str, fixtures_dir: str) -> FetchResult:
    """Read a bundled fixture instead of hitting the network.

    Reachable iff a `fixture://<name>` URL resolves to an existing `<name>.txt`.
    A demo claim that cites a fixture we did not ship is treated exactly like a
    dead link in live mode: unreachable -> rejected.
    """
    path = _fixture_path(fixtures_dir, url)
    if path is None:
        return FetchResult(ok=False, status=0, text="", error=f"no fixture for {url!r}")
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    text = extract_text(raw)
    if not text:
        return FetchResult(ok=False, status=0, text="", error="fixture is empty")
    return FetchResult(ok=True, status=200, text=text)


def fetch_live(url: str) -> FetchResult:
    """Fetch a real URL with a timeout and a user agent.

    `requests` is imported lazily so the offline demo never requires it. Any
    network error (DNS, timeout, connection reset) and any non-2xx status is a
    rejection: the source must actually exist and serve content.
    """
    try:
        import requests  # lazy: offline mode must not require this dependency
    except ImportError as e:  # pragma: no cover - environment dependent
        return FetchResult(ok=False, status=0, text="", error=f"requests not installed: {e}")

    try:
        resp = requests.get(
            url,
            timeout=_FETCH_TIMEOUT_S,
            headers={"User-Agent": _USER_AGENT},
            allow_redirects=True,
        )
    except Exception as e:  # network is hostile: any failure is "unreachable"
        return FetchResult(ok=False, status=0, text="", error=f"fetch failed: {e}")

    if not (200 <= resp.status_code < 300):
        return FetchResult(
            ok=False, status=resp.status_code, text="", error=f"HTTP {resp.status_code}"
        )

    text = extract_text(resp.text)
    if not text:
        return FetchResult(ok=False, status=resp.status_code, text="", error="empty page body")
    return FetchResult(ok=True, status=resp.status_code, text=text)


def _find_snippet(page_text: str, claim: str, width: int = 240) -> str:
    """Best-effort supporting snippet for the human reviewer.

    Find the page window with the most token overlap with the claim. This is NOT
    the verification — the adversarial panel decides support — it's just the
    receipt we put in the evidence so a human can eyeball the match fast.
    """
    words = [w for w in re.findall(r"[a-zA-Z0-9]{4,}", claim.lower())]
    if not words or not page_text:
        return page_text[:width]
    lower = page_text.lower()
    best_pos, best_hits = 0, -1
    # Slide a coarse window; cheap and good enough for a receipt.
    step = max(width // 2, 1)
    for pos in range(0, max(len(page_text) - width, 0) + 1, step):
        window = lower[pos: pos + width]
        hits = sum(1 for w in set(words) if w in window)
        if hits > best_hits:
            best_pos, best_hits = pos, hits
    return page_text[best_pos: best_pos + width].strip()


def make_verifier(
    llm: LLM,
    *,
    offline: bool,
    fixtures_dir: str,
    panel_size: int = 3,
):
    """Build the `verify(candidate) -> Verdict` callable.

    `candidate` is a `Claim` (see loop.py): `.text` is the atomic claim and
    `.source_url` is the citation to check.
    """

    def verify(candidate) -> Verdict:
        url = candidate.source_url
        claim = candidate.text

        # 1) GROUND THE CLAIM: fetch the cited page. No fetch, no confirmation.
        fetched = fetch_offline(url, fixtures_dir) if offline else fetch_live(url)
        if not fetched.ok:
            return Verdict(
                confirmed=False,
                evidence={
                    "url": url,
                    "http_status": fetched.status,
                    "supporting_snippet": "",
                    "mode": "offline" if offline else "live",
                },
                reason=f"source unreachable ({fetched.error})",
            )

        # 2) ADVERSARIAL SUPPORT CHECK in a fresh context, against the FETCHED
        #    text — never the generator's memory. The panel is told to refute
        #    that *this page* supports the claim, and to default to refuted when
        #    the page is silent or only tangentially related.
        snippet = _find_snippet(fetched.text, claim)
        vote = adversarial_vote(
            llm,
            claim=(
                "The following SOURCE PAGE TEXT directly supports this claim:\n"
                f"CLAIM: {claim}"
            ),
            evidence=(
                f"SOURCE URL: {url}\n"
                f"HTTP STATUS: {fetched.status}\n"
                "Judge support ONLY from the SOURCE PAGE TEXT below. If the page "
                "does not state or clearly imply the claim, refute it. Do not use "
                "outside knowledge.\n\n"
                f"SOURCE PAGE TEXT:\n{fetched.text}"
            ),
            n=panel_size,
            model=llm.mid,
        )

        evidence = {
            "url": url,
            "http_status": fetched.status,
            "supporting_snippet": snippet,
            "mode": "offline" if offline else "live",
            "panel": vote.evidence,
        }
        if vote.confirmed:
            return Verdict(
                confirmed=True,
                evidence=evidence,
                reason=f"source reachable (HTTP {fetched.status}) and {vote.reason}",
            )
        return Verdict(
            confirmed=False,
            evidence=evidence,
            reason=f"source reachable but does not support claim: {vote.reason}",
        )

    return verify
