import os
from pathlib import Path

from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[3]

# The Phase 0 bootstrap revision — downgrading here removes all Phase 1 domain
# schema while keeping the migration chain itself intact.
BOOTSTRAP_REVISION = "64dd66ad2889"


def alembic_config(database_url: str) -> Config:
    """Builds an Alembic Config targeting an arbitrary database via the
    ALEMBIC_DATABASE_URL escape hatch in migrations/env.py, so this never touches
    DATABASE_URL — only the isolation-guarded TEST_DATABASE_URL.
    """
    os.environ["ALEMBIC_DATABASE_URL"] = database_url
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return cfg
