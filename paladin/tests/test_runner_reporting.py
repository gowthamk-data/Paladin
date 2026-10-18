"""Decision-logic tests for the isolated ABAC test runner.

Covers the summary parser (``parse_test_counts``) and the verdict / exit-
status decision functions, including the exit-status contract:

* **exit 0** — every test executed and passed, zero skips.
* **exit 1** — a failure/error, a zero-collection run, OR an unparseable
  result (no recognized summary: missing ``OK``/``FAILED`` line, truncated
  or malformed output). An unparseable result is never counted as a pass,
  so the process exit status is non-zero (1 for failed, 3 for
  incomplete-verification).
* **exit 3** — INCOMPLETE VERIFICATION: tests executed but some were
  skipped, so the run *cannot* claim complete verification (this includes
  the all-skipped case, where zero tests were executed).

No database is required; the runner is imported as a plain module.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_isolated_abac_tests import (
    RUN_FAILED,
    RUN_INCOMPLETE,
    RUN_PASS,
    classify_pytest,
    classify_unittest,
    parse_test_counts,
    runner_exit_status,
)
# pytest terminal summaries
PYTEST_FULL_PASS = "48 passed in 3.5s"
PYTEST_PASS_WITH_SKIPS = "2 failed, 3 skipped, 45 passed in 4.1s"
PYTEST_ERROR_OUTPUT = "1 error in 0.1s"
PYTEST_ALL_SKIPPED = "3 skipped in 0.01s"  # summary recognised, no outcomes
PYTEST_MISSING_SUMMARY = "no summary here"  # no summary at all

# unittest summaries
PYTEST_OK_SUMMARY = "Ran 3 tests in 0.001s\n\nOK"
PYTEST_OK_SKIPPED_SUMMARY = "Ran 4 tests in 0.001s\n\nOK (skipped=2)"
PYTEST_FAILED_SUMMARY = "Ran 6 tests in 0.002s\n\nFAILED (failures=1, errors=2, skipped=1)"
PYTEST_TRUNCATED_SUMMARY = "Ran 3 tests in 0.001s\n\nOK (skipped=1"
PYTEST_MISSING_SUMMARY_UNIT = "Ran 3 tests"


# ---- pytest parser cases (Task 2) ------------------------------------------

def test_parse_test_counts_full_pass():
    assert parse_test_counts(PYTEST_FULL_PASS, "pytest") == {
        "passed": 48, "skipped": 0, "failed": 0, "unparseable": 0
    }


def test_parse_test_counts_pass_with_skips_and_failures():
    counts = parse_test_counts(PYTEST_PASS_WITH_SKIPS, "pytest")
    assert counts == {"passed": 45, "skipped": 3, "failed": 2, "unparseable": 0}


def test_parse_test_counts_errors_fold_into_failed():
    counts = parse_test_counts(PYTEST_ERROR_OUTPUT, "pytest")
    assert counts == {"passed": 0, "skipped": 0, "failed": 1, "unparseable": 0}


def test_parse_test_counts_skipped_without_outcome_is_recognised():
    # "3 skipped in 0.01s" -> 3 skipped recognised, but no pass/fail outcome,
    # so the run is not a pass (0 executed tests). The unparseable flag is
    # not set; the verdict layer decides the outcome.
    counts = parse_test_counts(PYTEST_ALL_SKIPPED, "pytest")
    assert counts == {"passed": 0, "skipped": 3, "failed": 0, "unparseable": 0}


def test_parse_test_counts_missing_summary_fail_closed():
    # No summary at all: unparseable, never a pass, non-zero exit.
    counts = parse_test_counts(PYTEST_MISSING_SUMMARY, "pytest")
    assert counts == {"passed": 0, "skipped": 0, "failed": 0, "unparseable": 1}


# ---- unittest parser cases (Task 2) ------------------------------------------

def test_parse_unittest_counts_ok():
    assert parse_test_counts(PYTEST_OK_SUMMARY, "unittest") == {
        "passed": 3, "skipped": 0, "failed": 0, "unparseable": 0
    }


def test_parse_unittest_counts_ok_with_skipped():
    assert parse_test_counts(PYTEST_OK_SKIPPED_SUMMARY, "unittest") == {
        "passed": 2, "skipped": 2, "failed": 0, "unparseable": 0
    }


def test_parse_unittest_counts_failed():
    assert parse_test_counts(PYTEST_FAILED_SUMMARY, "unittest") == {
        "passed": 2, "skipped": 1, "failed": 3, "unparseable": 0
    }


def test_parse_unittest_counts_truncated_summary_fail_closed():
    # "OK (skipped=1" - summary opened but never closed: unclassifiable.
    assert parse_test_counts(PYTEST_TRUNCATED_SUMMARY, "unittest") == {
        "passed": 0, "skipped": 0, "failed": 3, "unparseable": 1
    }


def test_parse_unittest_counts_missing_summary_fail_closed():
    # "Ran 3 tests" with no OK/FAILED line: unparseable, never a pass.
    assert parse_test_counts(PYTEST_MISSING_SUMMARY_UNIT, "unittest") == {
        "passed": 0, "skipped": 0, "failed": 3, "unparseable": 1
    }


def test_parse_unittest_counts_zero_tests_no_summary():
    # "Ran 0 tests" with no result line: fail-closed, unparseable flag set.
    assert parse_test_counts("Ran 0 tests", "unittest") == {
        "passed": 0, "skipped": 0, "failed": 0, "unparseable": 1
    }


# ---- classifier cases: all-skipped, mixed, failures, full pass --------


def test_classify_all_skipped_is_incomplete_not_pass():
    counts = {"passed": 0, "skipped": 3, "failed": 0, "unparseable": 0}
    status, _ = classify_pytest(counts, 0, "collected 3 items")
    assert status == RUN_INCOMPLETE
    counts = {"passed": 0, "skipped": 2, "failed": 0, "unparseable": 0}
    status, _ = classify_unittest(counts, 0, "Ran 2 tests")
    assert status == RUN_INCOMPLETE


def test_classify_mixed_passed_and_skipped_is_incomplete():
    counts = {"passed": 45, "skipped": 3, "failed": 0, "unparseable": 0}
    status, _ = classify_pytest(counts, 0, "collected 48 items")
    assert status == RUN_INCOMPLETE
    counts = {"passed": 2, "skipped": 2, "failed": 0, "unparseable": 0}
    status, _ = classify_unittest(counts, 0, "Ran 4 tests")
    assert status == RUN_INCOMPLETE


def test_classify_failures_are_failed():
    counts = {"passed": 45, "skipped": 0, "failed": 2, "unparseable": 0}
    status, _ = classify_pytest(counts, 1, "collected 47 items")
    assert status == RUN_FAILED
    counts = {"passed": 2, "skipped": 1, "failed": 3, "unparseable": 0}
    status, _ = classify_unittest(counts, 1, "Ran 6 tests")
    assert status == RUN_FAILED


def test_classify_complete_pass_and_exit_status():
    counts = {"passed": 48, "skipped": 0, "failed": 0, "unparseable": 0}
    assert classify_pytest(counts, 0, "collected 48 items")[0] == RUN_PASS
    counts = {"passed": 3, "skipped": 0, "failed": 0, "unparseable": 0}
    assert classify_unittest(counts, 0, "Ran 3 tests")[0] == RUN_PASS
    assert runner_exit_status([RUN_PASS, RUN_PASS]) == 0
    assert runner_exit_status([RUN_PASS, RUN_INCOMPLETE]) == 3
    assert runner_exit_status([RUN_PASS, RUN_FAILED]) == 1
