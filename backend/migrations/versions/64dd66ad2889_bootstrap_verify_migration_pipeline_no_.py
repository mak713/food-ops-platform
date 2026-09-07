"""bootstrap: verify migration pipeline (no domain schema)

Revision ID: 64dd66ad2889
Revises:
Create Date: 2026-09-07 17:11:20.835376

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "64dd66ad2889"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
