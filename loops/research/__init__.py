"""Citation-verified research / competitive intel loop.

The generator proposes atomic claims, each with a candidate source URL. The
verifier FETCHES the cited URL, extracts its text, and asks an adversarial
panel (fresh context) to REFUTE that the fetched page actually supports the
claim. A claim is confirmed only if the page is reachable AND supports it.
"""
