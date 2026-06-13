"""The verifier — the product. The model proposes; the canary decides.

A scanner that just *reports* sinks is a list of guesses. This verifier upgrades
every finding to a fact (or discards it) by firing a real HTTP exploit at the
live demo app and checking for a per-finding random canary in the response.

How a finding gets confirmed (the ONLY way):

  1. We mint a fresh canary with secrets.token_hex — unguessable, unique per
     finding, so a stale or echoed-from-elsewhere string can't satisfy it.
  2. A CHEAP model (claude-haiku-4-5) proposes ONE concrete HTTP request whose
     payload, IF the sink is exploitable, forces the canary into the response
     body. For command injection that's `; echo <canary>`; for eval it's a
     string literal; etc. The model only writes the request — it renders no
     verdict.
  3. We FIRE that request at 127.0.0.1 with the `requests` library and read the
     response body.
  4. Confirmed iff the exact canary substring appears in the body. Full stop.

Anti-gaming clause (enforced in code, not just prose):
  - The canary is generated HERE, by the verifier, after the model has spoken —
    it is never shown to the proposing model and never to the scanner.
  - We confirm on the canary substring ONLY. A model claiming "exploited!" or a
    500 error both FAIL — only the live response body counts.
  - The proposed payload must literally carry the canary, else the proposer is
    guessing and any match is coincidence (payload_carried_canary).
  - The payload reflecting verbatim WITHOUT execution FAILS: if a whole
    attacker-controlled payload value (canary + injection scaffolding) comes back
    unchanged, the endpoint merely mirrored input, so the canary proves echoing,
    not a live sink. Under genuine execution the scaffolding is consumed and only
    the canary surfaces (see _is_pure_reflection). This is checked against the
    very request we fired, so it cannot miss the actual vulnerable param.

The proving model and the proposing model are different processes/calls, and
neither is ever asked "is this real?" — the running app answers that.
"""
from __future__ import annotations

import secrets
from urllib.parse import urlencode

import requests
from pydantic import BaseModel, Field

from agentloops import LLM, Verdict


class ExploitRequest(BaseModel):
    """One concrete HTTP request the proposer believes will echo the canary."""

    method: str = Field(default="GET", description="HTTP method, usually GET.")
    path: str = Field(description="Path WITHOUT query string, e.g. /ping")
    query: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Query parameters as key->value. Put the exploit payload (which MUST "
            "embed the literal CANARY token you were given) in the vulnerable param."
        ),
    )
    rationale: str = Field(
        default="", description="One sentence: why this payload forces the canary out."
    )


_PROPOSER_SYSTEM = (
    "You are an exploit author for an AUTHORIZED security test against a local "
    "demo server you control. You are given one suspected injection sink and a "
    "secret CANARY token. Propose exactly ONE HTTP request whose payload, if the "
    "sink is genuinely exploitable, causes the server's response body to contain "
    "the CANARY token verbatim.\n"
    "Rules:\n"
    "- The CANARY must appear literally inside your payload (you are forcing the "
    "server to emit it, not guessing it).\n"
    "- For OS command injection, chain a command that prints the canary, e.g. "
    "value `127.0.0.1; echo CANARY` so the shell runs `echo CANARY`.\n"
    "- For eval/code injection, make the expression evaluate to a string "
    "containing the canary, e.g. `'CANARY'`.\n"
    "- For SQL injection, use a UNION/literal that surfaces the canary in output.\n"
    "- For reflected sinks, put the canary where it will be echoed.\n"
    "- Output only the request fields. Do NOT claim anything about the result; a "
    "separate process fires your request and checks the response."
)


def _build_url(base_url: str, path: str, query: dict[str, str]) -> str:
    base = base_url.rstrip("/")
    p = path if path.startswith("/") else "/" + path
    if query:
        return f"{base}{p}?{urlencode(query)}"
    return f"{base}{p}"


def _payload_values(proposal: "ExploitRequest") -> list[str]:
    """Every attacker-controlled string the proposer is sending (path + each
    query value). These are what an endpoint would mirror if it just echoes
    input."""
    return [proposal.path, *proposal.query.values()]


def _is_pure_reflection(proposal: "ExploitRequest", body: str, canary: str) -> bool:
    """Did the endpoint just MIRROR the payload back, with no execution?

    The distinction between a real injection and an endpoint that blindly echoes
    input is structural: under genuine execution the injection scaffolding (the
    `; echo `, the `UNION SELECT`, the quotes around an eval literal) is CONSUMED
    by the sink, so only the canary surfaces. Under pure reflection — or an error
    message that quotes the input verbatim — a whole attacker-controlled payload
    value comes back unchanged, scaffolding and all.

    So: if any payload value that *contains the canary* appears verbatim in the
    response body, the canary match proves mirroring, not execution → reject.
    Unlike a separate benign probe (the old approach), this inspects the very
    request we fired, so it cannot miss the actual vulnerable param.
    """
    for value in _payload_values(proposal):
        # Only payload values that carry the canary matter: those are the ones a
        # mirroring endpoint would echo to fake a match. A short or canary-free
        # fragment reflecting is irrelevant.
        if canary in value and value in body:
            return True
    return False


def make_verifier(llm: LLM, base_url: str, *, timeout: float = 5.0):
    """Return a `verify(finding) -> Verdict` bound to the running demo app.

    `finding` is a Finding from loop.py (anything with sink/file/line/param/
    taint_path attributes). We keep the import light to avoid a cycle.
    """

    def verify(finding) -> Verdict:  # type: ignore[no-untyped-def]
        # 1. Mint the canary HERE. The proposer never sees it until we hand it a
        #    copy to embed; the SCANNER never sees it at all.
        canary = "CANARY_" + secrets.token_hex(16)

        # 2. CHEAP model proposes ONE request. It renders no verdict.
        try:
            proposal = llm.structured(
                model=llm.cheap,
                system=_PROPOSER_SYSTEM,
                user=(
                    f"SINK TYPE: {finding.sink}\n"
                    f"ENDPOINT PATH: guess from taint_path / param below\n"
                    f"VULNERABLE PARAM: {finding.param}\n"
                    f"TAINT PATH: {finding.taint_path}\n"
                    f"SOURCE LOCATION: {finding.file}:{finding.line}\n\n"
                    f"CANARY (must appear verbatim in your payload): {canary}\n\n"
                    "Propose the single HTTP request that forces this canary into "
                    "the response body."
                ),
                schema=ExploitRequest,
                max_tokens=1200,
            )
        except Exception as exc:  # noqa: BLE001 — a proposer failure is a rejection, not a crash
            return Verdict(
                confirmed=False,
                reason=f"proposer failed: {exc}",
                evidence={"finding": finding.dedupe_key, "canary": canary},
            )

        # Defense in depth: the payload must actually carry the canary, else the
        # proposer is guessing and any match would be a coincidence.
        carries_canary = canary in "".join(_payload_values(proposal))

        url = _build_url(base_url, proposal.path, proposal.query)

        # 3. FIRE the request at the live app and read the body. THIS is the
        #    verdict source — never the model.
        try:
            resp = requests.request(proposal.method or "GET", url, timeout=timeout)
            body = resp.text
            status = resp.status_code
        except requests.RequestException as exc:
            return Verdict(
                confirmed=False,
                reason=f"request failed: {exc}",
                evidence={"finding": finding.dedupe_key, "request": url, "canary": canary},
            )

        # 4. The canary, in the body, decides. Plus the two anti-gaming gates.
        canary_in_body = canary in body
        # Reflection gate: if the endpoint mirrored the payload verbatim, a canary
        # match proves echoing, not execution. Checked against the request we
        # actually fired (no separate probe that could miss the vulnerable param).
        reflected = _is_pure_reflection(proposal, body, canary)

        confirmed = canary_in_body and carries_canary and not reflected

        snippet = _canary_snippet(body, canary) if canary_in_body else body[:240]
        if confirmed:
            reason = f"EXPLOITED {finding.sink}: canary echoed in response body"
        elif not carries_canary:
            reason = "rejected: proposed payload never embedded the canary"
        elif not canary_in_body:
            reason = f"rejected: canary not in response (HTTP {status})"
        else:  # reflected is the only remaining way to be unconfirmed here
            reason = "rejected: endpoint reflects payload verbatim without execution (no proof of sink)"

        return Verdict(
            confirmed=confirmed,
            reason=reason,
            # Proof a human can confirm in under a minute: the exact request,
            # the canary, the HTTP status, and the response snippet around it.
            evidence={
                "finding": finding.dedupe_key,
                "sink": finding.sink,
                "request": {"method": proposal.method or "GET", "url": url},
                "rationale": proposal.rationale,
                "canary": canary,
                "http_status": status,
                "response_snippet": snippet,
                "checks": {
                    "canary_in_body": canary_in_body,
                    "payload_carried_canary": carries_canary,
                    "payload_reflected_verbatim_no_execution": reflected,
                },
            },
        )

    return verify


def _canary_snippet(body: str, canary: str, pad: int = 80) -> str:
    """A window of the response body centered on the canary, for the evidence."""
    idx = body.find(canary)
    if idx < 0:
        return body[:240]
    start = max(0, idx - pad)
    end = min(len(body), idx + len(canary) + pad)
    return ("..." if start else "") + body[start:end] + ("..." if end < len(body) else "")
