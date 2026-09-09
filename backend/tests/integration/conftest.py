"""Shared fixtures for the Phase 2 API/security integration tests (tests/integration/api/,
tests/integration/security/) — parallel to, but independent of,
tests/integration/schema/conftest.py's own migrate/session fixtures (that file keeps its
own narrower-scoped version; this one is inherited by the other subpackages instead of
duplicating the same fixtures in each).

`session` uses SQLAlchemy 2.0's `join_transaction_mode="create_savepoint"`: the app's
services call `db.commit()` for real (Phase 2 established that as the request-transaction
convention — see app/services/auth_service.py's module docstring), so a plain "bind to a
connection and never commit" pattern won't do. With `create_savepoint`, every ORM-level
transaction (including what `db.commit()` ends) runs as a SAVEPOINT nested inside one outer,
never-committed connection transaction — so application code can commit/rollback freely
and the whole test's effect is still undone by the outer `rollback()` at teardown.
"""

from collections.abc import Generator

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.mail import InMemoryMailer, get_mailer
from app.db.session import get_db
from app.main import app
from tests.integration.schema.alembic_utils import alembic_config


@pytest.fixture(scope="session", autouse=True)
def _migrate_test_database(
    settings: Settings, _guard_test_database_isolation: None, test_engine
) -> Generator[None]:
    cfg = alembic_config(settings.test_database_url)
    command.upgrade(cfg, "head")
    yield
    with test_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture()
def session(test_engine, _migrate_test_database: None) -> Generator[Session]:
    connection = test_engine.connect()
    outer_transaction = connection.begin()
    session_local = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
    db = session_local()
    try:
        yield db
    finally:
        db.close()
        outer_transaction.rollback()
        connection.close()


@pytest.fixture()
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


@pytest.fixture()
def real_session_factory(test_engine) -> sessionmaker[Session]:
    """For the two genuine-concurrency tests (signup race, password-reset-confirm race):
    each call checks out its own real connection from the pool and commits for real
    against TEST_DATABASE_URL, unlike the savepoint-wrapped `session` fixture above — two
    threads sharing one connection/transaction can't demonstrate real row-locking
    behavior. Callers are responsible for cleaning up the rows they create."""
    return sessionmaker(bind=test_engine)


@pytest.fixture()
def client(session: Session, mailer: InMemoryMailer) -> Generator[TestClient]:
    """A TestClient wired to the per-test rolled-back `session` and an `InMemoryMailer`,
    with slowapi's in-memory rate-limit counters reset first — that storage lives on the
    (singleton, module-imported) `app`, so without a reset, requests made by an earlier
    test would count against a later, unrelated test's rate limit."""
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_mailer] = lambda: mailer
    app.state.limiter.reset()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
