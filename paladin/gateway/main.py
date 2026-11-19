import json
import logging

from fastapi import FastAPI, Request, Response
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from gateway.config import LOG_LEVEL, MAX_REQUEST_BYTES
from gateway.errors import register_exception_handlers
from gateway.middleware.body_limit_middleware import MaxBodySizeMiddleware
from gateway.middleware.correlation_middleware import CorrelationMiddleware
from gateway.middleware.logging_middleware import LoggingMiddleware
from gateway.routes.auth_routes import router as auth_router
from gateway.routes.metrics_routes import router as metrics_router
from gateway.routes.query_routes import router as ask_router
from gateway.security.rate_limiter import limiter

# Configure root logger with the specified LOG_LEVEL.
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="Paladin - Secure Enterprise AI Gateway",
    description="Local RAG-based LLM gateway with clearance-based access control, PII scrubbing, and audit logging.",
    version="0.1.0",
)

app.state.limiter = limiter

# Middleware is applied outermost-last, so the first call added here is the innermost layer.
# MaxBodySizeMiddleware is deliberately innermost: a rejected oversized request still travels
# back out through the logging and correlation layers, so the 413 is logged and carries the
# server-generated X-Request-ID like every other response.
app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_REQUEST_BYTES)
app.add_middleware(CorrelationMiddleware)
app.add_middleware(LoggingMiddleware)
app.add_middleware(SlowAPIMiddleware)

# Leak-free handlers: dependency outages become 503s, oversized input 413s, malformed input
# 422s and anything unexpected a 500 with a server-side traceback (never a client-visible one).
register_exception_handlers(app)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    response = Response(
        status_code=429,
        content=json.dumps({"detail": "Rate limit exceeded"}),
        headers={"Content-Type": "application/json"},
    )
    # Inject dynamically calculated rate-limit headers (Retry-After, X-RateLimit-Reset, etc.)
    # based on the remaining window duration in the backend storage.
    view_limit = getattr(request.state, "view_rate_limit", None)
    response = request.app.state.limiter._inject_headers(response, view_limit)
    if "Retry-After" not in response.headers:
        response.headers["Retry-After"] = str(getattr(exc, "retry_after", "60"))
    return response


app.include_router(auth_router)
app.include_router(ask_router)
app.include_router(metrics_router)


@app.get("/health")
def health_check():
    return {"status": "healthy"}