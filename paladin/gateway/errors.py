"""Centralised error handling for the gateway.

Two rules drive everything in this module:

1. **Never leak internals to the client.** Upstream failures (Ollama, ChromaDB, PostgreSQL),
   tracebacks, connection strings and exception reprs stay in the server log. Clients only ever
   receive a short, fixed, machine-readable detail string plus the correlation id they can quote
   to an operator.
2. **Fail with an accurate status code.** A dependency being down is a 503 (retryable), an
   oversized request is a 413 (client must shrink it), a malformed request is a 422, and anything
   genuinely unexpected is a 500 that is logged with a full traceback carrying the request id.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

try:  # Optional at import time (e.g. lightweight CI); registered only when present.
    from sqlalchemy.exc import SQLAlchemyError as _SQLAlchemyError
except ImportError:  # pragma: no cover - sqlalchemy is a runtime dependency in Docker

    class _SQLAlchemyError(Exception):  # type: ignore[no-redef]
        """Fallback so the database handler still registers without SQLAlchemy installed."""

logger = logging.getLogger("paladin.errors")


class ServiceUnavailableError(Exception):
    """An upstream dependency (LLM, vector store, database) could not serve the request."""

    def __init__(self, service: str, detail: str = None):
        self.service = service
        self.detail = detail or f"{service} is temporarily unavailable"
        super().__init__(self.detail)


class PayloadTooLargeError(Exception):
    """The request exceeded a configured size limit (prompt length or body size)."""

    def __init__(self, detail: str = "Request payload is too large"):
        self.detail = detail
        super().__init__(detail)


class AuthorizationDeniedError(Exception):
    """A retrieval result or authorization predicate failed closed."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def get_request_id(request: Request) -> str:
    """Return the correlation id for error reporting without depending on middleware order."""
    return getattr(request.state, "correlation_id", None) or "unknown"


def _json(status_code: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=payload)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the gateway's leak-free exception handlers to ``app``."""

    @app.exception_handler(ServiceUnavailableError)
    async def service_unavailable_handler(request: Request, exc: ServiceUnavailableError):
        request_id = get_request_id(request)
        logger.error(
            "request_id=%s dependency=%s unavailable: %s",
            request_id, exc.service, exc.detail, exc_info=True,
        )
        return _json(
            503,
            {
                "detail": exc.detail,
                "dependency": exc.service,
                "request_id": request_id,
            },
        )

    @app.exception_handler(PayloadTooLargeError)
    async def payload_too_large_handler(request: Request, exc: PayloadTooLargeError):
        request_id = get_request_id(request)
        logger.warning("request_id=%s payload rejected: %s", request_id, exc.detail)
        return _json(413, {"detail": exc.detail, "request_id": request_id})

    @app.exception_handler(_SQLAlchemyError)
    async def database_error_handler(request: Request, exc: _SQLAlchemyError):
        request_id = get_request_id(request)
        logger.error(
            "request_id=%s database error: %s", request_id, type(exc).__name__, exc_info=True
        )
        return _json(
            503,
            {
                "detail": "Database is temporarily unavailable",
                "dependency": "database",
                "request_id": request_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # FastAPI's default handler echoes the offending input back to the caller, which would
        # replay user-submitted PII (and any injected payload) into the HTTP response and into
        # reverse-proxy/access logs. Only the *location* of the fault is returned instead.
        request_id = get_request_id(request)
        fields = sorted(
            {".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()}
        )
        logger.warning("request_id=%s validation error on fields=%s", request_id, fields)
        return _json(
            422,
            {
                "detail": "Request validation failed",
                "fields": fields,
                "request_id": request_id,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = get_request_id(request)
        logger.error(
            "request_id=%s unhandled %s", request_id, type(exc).__name__, exc_info=True
        )
        return _json(500, {"detail": "Internal server error", "request_id": request_id})
