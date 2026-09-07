from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app


def test_health_returns_ok_and_checks_database(db_session: Session) -> None:
    """GET /health must verify real database connectivity, using the dedicated
    TEST_DATABASE_URL — never the development database (see conftest.py)."""

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.get("/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
