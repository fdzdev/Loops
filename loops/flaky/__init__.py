"""flaky — flaky-test retirement loop.

Generator parses a CI-log fixture for intermittently-failing tests, an executor
proposes a deterministic fix (freeze clock / seed RNG / remove race), and a
DETERMINISTIC verifier runs the target test N times and only confirms when ALL
N pass AND the test was not weakened (checked via AST).
"""
