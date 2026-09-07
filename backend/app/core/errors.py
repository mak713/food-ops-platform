from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# Shared error envelope shape (Spec §11.9). Severity/code/message/field/resource/details
# concepts must remain machine-readable across all backend error responses.


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _envelope(code: str, message: str, issues: list[dict]) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "issues": issues,
        }
    }


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    body = _envelope(
        code="HTTP_ERROR",
        message=str(exc.detail),
        issues=[
            {
                "severity": "ERROR",
                "code": "HTTP_ERROR",
                "message": str(exc.detail),
                "field": None,
                "resource": None,
                "details": {},
            }
        ],
    )
    body["error"]["request_id"] = _request_id(request)
    return JSONResponse(status_code=exc.status_code, content=body)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    issues = [
        {
            "severity": "ERROR",
            "code": "VALIDATION_ERROR",
            "message": error["msg"],
            "field": ".".join(str(loc) for loc in error["loc"] if loc != "body"),
            "resource": None,
            "details": {},
        }
        for error in exc.errors()
    ]
    body = _envelope(
        code="VALIDATION_ERROR",
        message="One or more fields failed validation.",
        issues=issues,
    )
    body["error"]["request_id"] = _request_id(request)
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=body)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    body = _envelope(
        code="INTERNAL_SERVER_ERROR",
        message="An unexpected error occurred.",
        issues=[],
    )
    body["error"]["request_id"] = _request_id(request)
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
