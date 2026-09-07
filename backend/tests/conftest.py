from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session", autouse=True)
def _guard_test_database_isolation(settings: Settings) -> None:
    """Refuse to run the test suite against the development database.

    Later phases' integration tests (Spec §19.3) will create and mutate rows,
    and eventually schema. This guard fails fast and loudly rather than letting
    a misconfigured TEST_DATABASE_URL silently touch dev data.
    """
    if not settings.test_database_url:
        pytest.exit("TEST_DATABASE_URL is not set — refusing to run tests.", returncode=1)
    if settings.test_database_url == settings.database_url:
        pytest.exit(
            "TEST_DATABASE_URL resolves to the same database as DATABASE_URL — "
            "refusing to run tests against the development database.",
            returncode=1,
        )


@pytest.fixture(scope="session")
def test_engine(settings: Settings):
    engine = create_engine(settings.test_database_url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(test_engine) -> Generator[Session]:
    session_local = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    session = session_local()
    try:
        yield session
    finally:
        session.close()
