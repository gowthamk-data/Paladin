"""Internal attribute-based authorization."""

from gateway.authz.policy import (
    AuthorizationDecision,
    DocumentAttributes,
    UserAttributes,
    authorize,
    build_chroma_filter,
)

__all__ = [
    "AuthorizationDecision",
    "DocumentAttributes",
    "UserAttributes",
    "authorize",
    "build_chroma_filter",
]
