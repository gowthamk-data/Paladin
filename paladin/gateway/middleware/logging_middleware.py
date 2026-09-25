import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("paladin.requests")


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.time()
        try:
            response = await call_next(request)
        except Exception:
            # The exception itself is logged (with traceback) by gateway/errors.py; this line
            # keeps the request-id trail complete for the failed request.
            correlation_id = getattr(request.state, "correlation_id", "unknown")
            logger.exception(
                "%s %s %s -> 500 %.3fs",
                correlation_id, request.method, request.url.path, time.time() - start,
            )
            raise
        duration = time.time() - start
        # The server-generated id is the only correlation value ever logged: a client-supplied
        # X-Request-ID header is discarded by CorrelationMiddleware before this runs.
        correlation_id = getattr(request.state, "correlation_id", "unknown")
        log = logger.warning if response.status_code >= 400 else logger.info
        log(
            "%s %s %s -> %s %.3fs",
            correlation_id,
            request.method,
            request.url.path,
            response.status_code,
            duration,
        )
        return response
