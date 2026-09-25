from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request
from jose import jwt
from gateway.auth.jwt_utils import PUBLIC_KEY, AUDIENCE, ISSUER
from gateway.config import RATE_LIMIT_PER_MINUTE, RATE_LIMIT_BURST


def get_user_key(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            payload = jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER)
            user_id = payload.get("sub")
            if user_id:
                return f"rate_limit:{user_id}"
        except Exception:
            pass
    return f"rate_limit:anon:{get_remote_address(request)}"


# Rate-limiting policy: a short burst allowance on top of a sustained per-minute rate.
#
# slowapi evaluates every limit configured for a route independently, so the /ask policy is
# expressed as two stacked limits (see gateway/routes/query_routes.py):
#   * burst:     RATE_LIMIT_BURST requests inside one minute            (default 20/minute)
#   * sustained: RATE_LIMIT_PER_MINUTE per minute over a longer window  (default 60/6minutes)
# A burst allowance can only exist on a *longer* window than the burst window: if the
# sustained cap used the same 60s window it would always bind first and the burst would be
# meaningless. A burst smaller than the sustained rate would make the policy stricter than
# configured, hence the floor at RATE_LIMIT_PER_MINUTE.
SUSTAINED_WINDOW_MINUTES = 6
BURST_LIMIT = max(RATE_LIMIT_BURST, RATE_LIMIT_PER_MINUTE)
BURST_LIMIT_STRING = f"{BURST_LIMIT}/minute"
SUSTAINED_LIMIT_STRING = (
    f"{RATE_LIMIT_PER_MINUTE * SUSTAINED_WINDOW_MINUTES}/{SUSTAINED_WINDOW_MINUTES}minutes"
)
# Applied to routes without an explicit @limiter.limit decorator (/auth/login, /auth/me, ...).
DEFAULT_LIMIT_STRING = f"{RATE_LIMIT_PER_MINUTE}/minute"



limiter = Limiter(
    key_func=get_user_key,
    default_limits=[DEFAULT_LIMIT_STRING],
    storage_uri="memory://",
    headers_enabled=True,
)