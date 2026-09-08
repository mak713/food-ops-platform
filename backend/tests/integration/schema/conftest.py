from collections.abc import Generator

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from tests.integration.schema.alembic_utils import alembic_config


@pytest.fixture(scope="session", autouse=True)
def _migrate_test_database(
    settings: Settings, _guard_test_database_isolation: None, test_engine
) -> None:
    """Applies the full migration history (bootstrap + Phase 1 domain schema) to
    TEST_DATABASE_URL before any schema test runs, then drops every table
    afterward so the schema tests never leak into a later test session's state.
    """
    cfg = alembic_config(settings.test_database_url)
    command.upgrade(cfg, "head")
    yield
    with test_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture()
def session(test_engine, _migrate_test_database: None) -> Generator[Session]:
    """A Session bound to its own connection/transaction, rolled back at teardown
    so no test's data or schema mutation leaks into another test. Real Postgres
    constraint/FK/cascade behavior still fires on flush() within that transaction —
    only the final rollback is what keeps tests isolated from each other.
    """
    connection = test_engine.connect()
    outer_transaction = connection.begin()
    session_local = sessionmaker(bind=connection)
    db = session_local()
    try:
        yield db
    finally:
        db.close()
        outer_transaction.rollback()
        connection.close()
