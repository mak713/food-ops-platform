"""Sets/clears the `fo_session`/`fo_csrf` cookie pair on a response.

`fo_session` is `HttpOnly` always; `fo_csrf` deliberately is not, since the SPA must read it
and echo it back as the `X-CSRF-Token` header (Phase 2 plan §3). Both are `Secure` only in
production so local HTTP dev keeps working.

Cookie `Max-Age` is fixed to the 30-day absolute session lifetime (`SESSION_ABSOLUTE_TTL`),
not the session's current 7-day sliding idle deadline: the two are deliberately decoupled
(Checkpoint 2 review, issue 2). The idle deadline is what the *server* enforces on every
authenticated request (`get_current_session` rejects a session whose `expires_at` has
passed); the cookie itself only needs to still be present in the browser for the request to
reach the server at all. Tying `Max-Age` to the idle deadline would make the browser stop
sending the cookie after ~7 days regardless of activity, capping every session at 7 days in
practice and never letting an active session reach the approved 30-day absolute cap.
"""

from __future__ import annotations

from fastapi import Response

from app.core import security
from app.core.config import get_settings


def set_session_cookies(response: Response, *, raw_session_token: str, raw_csrf_token: str) -> None:
    secure = get_settings().environment == "production"
    max_age = int(security.SESSION_ABSOLUTE_TTL.total_seconds())
    response.set_cookie(
        security.SESSION_COOKIE_NAME,
        raw_session_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )
    response.set_cookie(
        security.CSRF_COOKIE_NAME,
        raw_csrf_token,
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(security.SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(security.CSRF_COOKIE_NAME, path="/")
