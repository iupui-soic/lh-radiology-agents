"""Test-session setup for the worklist-api suite.

`main.py` builds the app at import time, and both of its sqlite stores default under `/var/lib`,
so on any machine where the tests run as a normal user the suite fails at COLLECTION with a
PermissionError, not at a test (#134). The CI lane happened to work because it runs as root.

Both paths are pointed at a per-session temp dir here, before any test module imports `main`.
`setdefault` keeps an operator's own `WORKLIST_STORE_PATH` / `WORKLIST_FINDINGS_STORE_PATH`
(and the CI lane's) in force; the temp dir is only the fallback that makes the documented
`python -m pytest -q` work with no environment at all.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_STORE_DIR = tempfile.TemporaryDirectory(prefix="worklist-api-tests-")
os.environ.setdefault("WORKLIST_STORE_PATH", os.path.join(_STORE_DIR.name, "worklist.sqlite"))
os.environ.setdefault("WORKLIST_FINDINGS_STORE_PATH",
                      os.path.join(_STORE_DIR.name, "findings.sqlite"))
