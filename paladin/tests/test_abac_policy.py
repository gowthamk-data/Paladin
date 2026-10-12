import pytest

from gateway.authz.policy import (
    DocumentAttributes,
    UserAttributes,
    authorize,
    build_chroma_filter,
)


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
# ---------------------------------------------------------------------------
SEEDED_USERS = {
    "emp1": UserAttributes("Engineering", 1, "employee"),
    "mgr1": UserAttributes("Engineering", 2, "manager"),
    "admin1": UserAttributes("Executive", 3, "admin"),
    "fin1": UserAttributes("Finance", 2, "employee"),
}

MANIFEST_DOCUMENTS = {
    "engineering_policy.txt": DocumentAttributes(1, "Engineering", "department"),
    "finance_guidelines.txt": DocumentAttributes(2, "Finance", "department"),
    "executive_briefing.md": DocumentAttributes(3, "Executive", "department"),
    "hr_handbook.md": DocumentAttributes(2, "HR", "company-wide"),
}

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


@pytest.mark.parametrize("user_key", list(SEEDED_USERS))
def test_seeded_user_access_matrix(user_key):
    user = SEEDED_USERS[user_key]
    for doc_key, doc in MANIFEST_DOCUMENTS.items():
        decision = authorize("read", user, doc)
        assert decision.allowed is EXPECTED_ACCESS[user_key][doc_key], (
            f"{user_key} -> {doc_key}: expected allowed="
            f"{EXPECTED_ACCESS[user_key][doc_key]}, got {decision.allowed} "
            f"({decision.reason_code})"
        )
