from dataclasses import dataclass
from typing import Any, Mapping

VALID_CLEARANCE_LEVELS = frozenset({1, 2, 3})
VALID_VISIBILITIES = frozenset({"company-wide", "department"})


@dataclass(frozen=True)
class UserAttributes:
    department: str
    clearance_level: int
    role: str


@dataclass(frozen=True)
class DocumentAttributes:
    classification_level: int
    department: str
    visibility: str


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason_code: str


def _valid_clearance(value: Any) -> bool:
    return type(value) is int and value in VALID_CLEARANCE_LEVELS


def _valid_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _parse_user(user: Any) -> UserAttributes | None:
    if isinstance(user, UserAttributes):
        candidate = user
    else:
        values = user if isinstance(user, Mapping) else None
        candidate = UserAttributes(
            department=(
                values.get("department")
                if values is not None
                else getattr(user, "department", None)
            ),
            clearance_level=(
                values.get("clearance_level")
                if values is not None
                else getattr(user, "clearance_level", None)
            ),
            role=values.get("role") if values is not None else getattr(user, "role", None),
        )
    if (
        not _valid_text(candidate.department)
        or not _valid_clearance(candidate.clearance_level)
        or not _valid_text(candidate.role)
    ):
        return None
    return candidate


def _parse_document(document: Any) -> DocumentAttributes | None:
    if isinstance(document, DocumentAttributes):
        candidate = document
    else:
        try:
            values = document if isinstance(document, Mapping) else vars(document)
        except TypeError:
            return None
        candidate = DocumentAttributes(
            classification_level=values.get("classification_level"),
            department=values.get("department"),
            visibility=values.get("visibility"),
        )
    if (
        not _valid_clearance(candidate.classification_level)
        or not _valid_text(candidate.department)
        or candidate.visibility not in VALID_VISIBILITIES
    ):
        return None
    return candidate


def authorize(action: str, user: Any, document: Any) -> AuthorizationDecision:
    """Evaluate the fixed V1 read policy and fail closed for every malformed input."""
    if action != "read":
        return AuthorizationDecision(False, "UNKNOWN_ACTION")
    parsed_user = _parse_user(user)
    if parsed_user is None:
        return AuthorizationDecision(False, "INVALID_USER_ATTRIBUTES")
    parsed_document = _parse_document(document)
    if parsed_document is None:
        return AuthorizationDecision(False, "INVALID_DOCUMENT_ATTRIBUTES")
    if parsed_user.clearance_level < parsed_document.classification_level:
        return AuthorizationDecision(False, "INSUFFICIENT_CLEARANCE")
    if (
        parsed_document.visibility != "company-wide"
        and parsed_document.department != parsed_user.department
    ):
        return AuthorizationDecision(False, "DEPARTMENT_MISMATCH")
    return AuthorizationDecision(True, "ALLOWED")


def build_chroma_filter(user: Any) -> dict[str, Any]:
    """Build the complete Chroma predicate, raising on invalid user attributes."""
    parsed_user = _parse_user(user)
    if parsed_user is None:
        raise ValueError("INVALID_USER_ATTRIBUTES")
    return {
        "$and": [
            {"classification_level": {"$lte": parsed_user.clearance_level}},
            {
                "$or": [
                    {"visibility": {"$eq": "company-wide"}},
                    {"department": {"$eq": parsed_user.department}},
                ]
            },
        ]
    }
