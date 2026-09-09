"""Rate limiting for auth endpoints (Phase 2 plan §12).

`slowapi`, in-memory, single-process — an accepted assumption for the single-instance V1
deployment target. Keyed by `get_remote_address` (the direct socket peer IP) only, never a
client-settable `X-Forwarded-For`/`X-Real-IP` header — this deployment has no
reverse-proxy/trusted-proxy configuration today, so trusting a forwarded-IP header would
let the limit be trivially spoofed. A future trusted-proxy deployment would need to
explicitly allow-list the proxy's own IP before trusting its forwarded header; that's a
deliberate follow-up, not something to half-wire now.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

# Approved V1 tunable defaults (Phase 2 plan §12) — not spec-mandated numbers, just a
# concrete, adjustable starting point for "basic rate limiting" (Spec §10.7).
LOGIN_RATE_LIMIT = "10/minute"
SIGNUP_RATE_LIMIT = "10/minute"
PASSWORD_RESET_REQUEST_RATE_LIMIT = "5/hour"


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body = {
        "error": {
            "code": "RATE_LIMITED",
            "message": "Too many requests. Please try again later.",
            "issues": [],
            "request_id": request_id,
        }
    }
    return JSONResponse(status_code=429, content=body)


def register_rate_limiting(app: FastAPI) -> None:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
