"""Unit test for the non-disclosing tenant-lookup helper (app.core.tenant.NotFoundError).

Phase 2 has no client-facing tenant-owned resource reachable by ID yet, so — per the Phase
2 plan §13 — this exercises the helper's behavior directly against a throwaway, test-only
FastAPI app rather than adding a real endpoint to the application just to prove the 404
shape. The end-to-end "Business A hits Business B's resource by ID" proof is Phase 3's
responsibility, once a real tenant-owned resource exists.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.api_errors import register_api_error_handlers
from app.core.tenant import NotFoundError


def _build_test_app() -> FastAPI:
    app = FastAPI()
    register_api_error_handlers(app)

    @app.get("/_raises-not-found")
    def _raise() -> None:
        raise NotFoundError()

    return app


def test_not_found_error_returns_non_revealing_404_envelope():
    client = TestClient(_build_test_app())
    response = client.get("/_raises-not-found")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "The requested resource was not found."
    assert body["error"]["issues"] == []
