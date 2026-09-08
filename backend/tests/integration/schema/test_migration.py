"""Migration round-trip: downgrade back to the Phase 0 bootstrap revision, then
upgrade back to head, proving the Phase 1 migration is reversible and
idempotent-on-reapply. Runs against TEST_DATABASE_URL only (via the
ALEMBIC_DATABASE_URL escape hatch), never DATABASE_URL.
"""

from alembic import command
from sqlalchemy import text

from app.core.config import Settings
from tests.integration.schema.alembic_utils import BOOTSTRAP_REVISION, alembic_config


def _table_exists(test_engine, table_name: str) -> bool:
    with test_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :table)"
            ),
            {"table": table_name},
        ).scalar_one()


def test_migration_round_trip(settings: Settings, test_engine, _migrate_test_database: None):
    cfg = alembic_config(settings.test_database_url)

    # Sanity: head state has the Phase 1 domain schema.
    assert _table_exists(test_engine, "businesses")
    assert _table_exists(test_engine, "orders")

    # Downgrade all the way back to the Phase 0 bootstrap: domain tables gone.
    command.downgrade(cfg, BOOTSTRAP_REVISION)
    assert not _table_exists(test_engine, "businesses")
    assert not _table_exists(test_engine, "orders")
    assert _table_exists(test_engine, "alembic_version")

    # Re-upgrade to head: domain schema is back, cleanly reapplied.
    command.upgrade(cfg, "head")
    assert _table_exists(test_engine, "businesses")
    assert _table_exists(test_engine, "orders")
