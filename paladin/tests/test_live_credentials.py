import sys
from pathlib import Path

import pytest

TEST_DIR = str(Path(__file__).resolve().parent)
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

from live_credentials import password_for


def test_reads_user_specific_environment_value():
    assert password_for("admin1", {"PALADIN_TEST_ADMIN1_PASSWORD": "opaque"}) == "opaque"


def test_missing_credential_names_only_variable():
    secret = "must-not-appear-in-output"
    with pytest.raises(RuntimeError) as exc_info:
        password_for("emp1", {})
    assert "PALADIN_TEST_EMP1_PASSWORD" in str(exc_info.value)
    assert secret not in str(exc_info.value)


def test_rejects_unknown_user_without_echoing_input():
    with pytest.raises(ValueError, match="Unknown demo user"):
        password_for("secret-user-name", {})
