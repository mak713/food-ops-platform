"""A small typed exception + handler for domain-specific API error codes (e.g.
`AUTH_INVALID_CREDENTIALS`, `AUTH_CSRF_MISMATCH`, `AUTH_EMAIL_ALREADY_REGISTERED`).

`app/core/errors.py` (Phase 0, frozen) already handles `StarletteHTTPException`,
`RequestValidationError`, and any uncaught `Exception` with the shared envelope shape —
but its `HTTPException` handler hard-codes `code="HTTP_ERROR"` for every case, since Phase
0 had no need yet for endpoint-specific machine-readable codes. Phase 2 introduces the
first errors that need a real code distinct from the human-readable message (frontend
logic branches on `AUTH_UNAUTHENTICATED` vs. `AUTH_CSRF_MISMATCH` vs.
`AUTH_INVALID_CREDENTIALS`, for instance), so rather than changing frozen Phase 0
behavior, this module adds a parallel, explicitly-coded exception type that produces the
identical envelope shape (`error.code` / `error.message` / `error.issues` /
`error.request_id`) that `app/core/errors.py` already established.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Raise to produce a specific `{status_code, code, message}` API error response."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        issues: list[dict] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.issues = issues or []
        super().__init__(message)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body = {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "issues": exc.issues,
            "request_id": request_id,
        }
    }
    return JSONResponse(status_code=exc.status_code, content=body)


def register_api_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)
