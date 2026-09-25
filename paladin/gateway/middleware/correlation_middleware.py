import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


def get_correlation_id(request: Request) -> str:
    """Return the server-generated correlation id for this request.

    The id is created by CorrelationMiddleware and never comes from the client. The fallback
    keeps the endpoint working if the middleware is not installed (e.g. in unit tests).
    """
    correlation_id = getattr(request.state, "correlation_id", None)
    if not correlation_id:
        correlation_id = str(uuid.uuid4())
        request.state.correlation_id = correlation_id
    return correlation_id



class CorrelationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.correlation_id = request_id
        # A client-supplied X-Request-ID is never trusted: overwrite it in the request scope so
        # every downstream reader (endpoint, logging, audit rows) only sees the server value.
        headers = [(name, value) for name, value in request.scope.get("headers", [])
                   if name.lower() != b"x-request-id"]
        headers.append((b"x-request-id", request_id.encode("latin-1")))
        request.scope["headers"] = headers
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
