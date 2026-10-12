import json
from pathlib import Path

import pytest

from gateway.authz.policy import (
    DocumentAttributes,
    UserAttributes,
    authorize,
    build_chroma_filter,
)
from gateway.db import seed as seed_mod


def user(department="Engineering", clearance=2):
    return UserAttributes(department, clearance, "employee")


def document(level=2, department="Engineering", visibility="department"):
    return DocumentAttributes(level, department, visibility)


@pytest.mark.parametrize(
    ("clearance", "allowed"),
    [(1, False), (2, True), (3, True)],
)
def test_clearance_is_enforced(clearance, allowed):
    assert authorize("read", user(clearance=clearance), document()).allowed is allowed


def test_company_wide_document_ignores_department():
    assert authorize("read", user(department="Engineering"), document(department="HR", visibility="company-wide")).allowed


def test_department_document_requires_match():
    decision = authorize("read", user(department="Engineering"), document(department="Finance"))
    assert (decision.allowed, decision.reason_code) == (False, "DEPARTMENT_MISMATCH")


@pytest.mark.parametrize(
    "bad_user",
    [
        {"department": "", "clearance_level": 2, "role": "employee"},
        {"department": "Engineering", "clearance_level": 99, "role": "employee"},
        {"department": "Engineering", "clearance_level": 2, "role": None},
    ],
)
def test_invalid_user_attributes_fail_closed(bad_user):
    decision = authorize("read", bad_user, document())
    assert (decision.allowed, decision.reason_code) == (False, "INVALID_USER_ATTRIBUTES")


def test_invalid_document_and_unknown_action_fail_closed():
    assert authorize("read", user(), {"classification_level": 2}).reason_code == "INVALID_DOCUMENT_ATTRIBUTES"
    assert authorize("write", user(), document()).reason_code == "UNKNOWN_ACTION"


def test_chroma_filter_contains_clearance_and_scope_constraints():
    assert build_chroma_filter(user("Finance", 3)) == {
        "$and": [
            {"classification_level": {"$lte": 3}},
            {
                "$or": [
                    {"visibility": {"$eq": "company-wide"}},
                    {"department": {"$eq": "Finance"}},
                ]
            },
        ]
    }

# ---------------------------------------------------------------------------
# Full access matrix: all four seeded users x all four manifest documents.
# Expected outcomes were confirmed in Stage 0 (docs/classification-decisions.md)
# and are asserted here at the policy layer.
#
# Fixture-stability guards: SEEDED_USERS is asserted to match gateway/db/seed.py
# and MANIFEST_DOCUMENTS is asserted to match data/manifest.json, so a change
# to either source fails loudly here instead of silently drifting. The manifest
# labels themselves are owner-approved and are NOT altered by this file.
# ---------------------------------------------------------------------------
SEEDED_USERS = {
    "emp1": UserAttributes("Engineering", 1, "employee"),
    "mgr1": UserAttributes("Engineering", 2, "manager"),
    "admin1": UserAttributes("Executive", 3, "admin"),
    "fin1": UserAttributes("Finance", 2, "employee"),
}

MANIFEST_PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DOCUMENTS = {
    name: DocumentAttributes(
        entry["classification_level"], entry["department"], entry["visibility"]
    )
    for name, entry in json.loads(
        (MANIFEST_PROJECT_ROOT / "data" / "manifest.json").read_text(encoding="utf-8")
    ).items()
}


def test_fixtures_match_seed_definitions():
    seed_lookup = {entry["username"]: entry for entry in seed_mod.SEED_USERS}
    assert set(seed_lookup) == set(SEEDED_USERS), (
        f"seed usernames {sorted(seed_lookup)} != fixture {sorted(SEEDED_USERS)}"
    )
    for username, fixture in SEEDED_USERS.items():
        entry = seed_lookup[username]
        assert (fixture.department, fixture.clearance_level, fixture.role) == (
            entry["department"],
            entry["clearance_level"],
            entry["role"],
        ), f"fixture for {username} drifted from gateway/db/seed.py"


def test_fixtures_match_manifest_labels():
    raw = json.loads(
        (MANIFEST_PROJECT_ROOT / "data" / "manifest.json").read_text(encoding="utf-8")
    )
    assert set(raw) == set(MANIFEST_DOCUMENTS), (
        f"manifest files {sorted(raw)} != fixture {sorted(MANIFEST_DOCUMENTS)}"
    )
    for filename, doc in MANIFEST_DOCUMENTS.items():
        entry = raw[filename]
        assert (
            doc.classification_level,
            doc.department,
            doc.visibility,
        ) == (
            entry["classification_level"],
            entry["department"],
            entry["visibility"],
        ), f"fixture for {filename} drifted from data/manifest.json"

EXPECTED_ACCESS = {
    # user: {document: allowed}
    "emp1": {
        "engineering_policy.txt": True,
        "finance_guidelines.txt": False,
        "executive_briefing.md": False,
        "hr_handbook.md": False,
    },
    "mgr1": {
        "engineering_policy.txt": True,
        "finance_guidelines.txt": False,
        "executive_briefing.md": False,
        "hr_handbook.md": True,
    },
    "admin1": {
        "engineering_policy.txt": False,
        "finance_guidelines.txt": False,
        "executive_briefing.md": True,
        "hr_handbook.md": True,
    },
    "fin1": {
        "engineering_policy.txt": False,
        "finance_guidelines.txt": True,
        "executive_briefing.md": False,
        "hr_handbook.md": True,
    },
}


@pytest.mark.parametrize(
    ("user_key", "doc_key"),
    [
        (user_key, doc_key)
        for user_key in ("emp1", "mgr1", "admin1", "fin1")
        for doc_key in (
            "engineering_policy.txt",
            "finance_guidelines.txt",
            "executive_briefing.md",
            "hr_handbook.md",
        )
    ],
)
def test_seeded_user_access_matrix(user_key, doc_key):
    decision = authorize("read", SEEDED_USERS[user_key], MANIFEST_DOCUMENTS[doc_key])
    assert decision.allowed is EXPECTED_ACCESS[user_key][doc_key], (
        f"{user_key} -> {doc_key}: expected allowed="
        f"{EXPECTED_ACCESS[user_key][doc_key]}, got {decision.allowed} "
        f"({decision.reason_code})"
    )


def test_company_wide_still_requires_clearance():
    # visibility="company-wide" waives the department check but NOT the
    # clearance gate: emp1 (clearance 1) must still be denied the Level 2 HR
    # handbook, while mgr1 (clearance 2, same department) is allowed.
    hr_doc = MANIFEST_DOCUMENTS["hr_handbook.md"]
    assert hr_doc.visibility == "company-wide"
    emp1_decision = authorize("read", SEEDED_USERS["emp1"], hr_doc)
    assert (emp1_decision.allowed, emp1_decision.reason_code) == (
        False,
        "INSUFFICIENT_CLEARANCE",
    )
    assert authorize("read", SEEDED_USERS["mgr1"], hr_doc).allowed is True
