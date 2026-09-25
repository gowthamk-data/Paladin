"""Pytest configuration for the PALADIN test suite.

Two kinds of test live in this directory:

* ``tests/test_pii_detection.py`` - pure unit tests. No Docker stack, database or network
  needed; collected normally by pytest and runnable with ``python -m unittest``.
* ``tests/test_integration_live.py`` plus the older blocking-verification harnesses
  (``test_auth_live.py``, ``test_rate_limit_and_tokens_live.py``, ``test_rate_limit_burst_live.py``,
  ``test_ask_e2e_live.py``). These need the docker compose stack up and drive real HTTP requests,
  real LLM generations and container stop/start cycles.

The live suites are built to be *run*, not collected: they are invoked as
``python tests/<harness>.py`` and their printed summary is the evidence for a phase gate. The
older ones execute at import time, so collecting them from pytest would start a live run during
collection. They are therefore excluded here on purpose - see tests/README.md for the exact
command for each one.
"""

collect_ignore = [
    "test_auth_live.py",
    "test_ask_e2e_live.py",
    "test_rate_limit_burst_live.py",
    "test_rate_limit_and_tokens_live.py",
    "test_integration_live.py",
]
