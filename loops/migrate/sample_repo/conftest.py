"""Make ``sample_pkg`` importable when pytest runs from the repo root.

The verifier invokes ``pytest`` with this directory as CWD; adding it to
``sys.path`` here means the tests import the *copy under test*, not any
installed version.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
