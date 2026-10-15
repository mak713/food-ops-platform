"""phase 8: production run cost basis and surplus provenance

Revision ID: 0e6f9a20fa8a
Revises: d6ab1ed479d7
Create Date: 2026-09-17 02:41:55.717170

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0e6f9a20fa8a"
down_revision: str | None = "d6ab1ed479d7"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # production_run_ingredients: cost_basis_source discriminator (nullable=False
    # requires a default for a migration against a populated table, but Phase 8
    # verified `production_runs`/`production_run_ingredients` are empty in the live
    # dev database, so no backfill value is needed).
    op.add_column(
        "production_run_ingredients",
        sa.Column(
            "cost_basis_source",
            sa.Enum(
                "WAC",
                "FALLBACK",
                name="ck_production_run_ingredients_cost_basis_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
    )

    # production_runs: defense-in-depth #1 (partial unique active-run index) and
    # #2 (status/timestamp coupling CHECK) — Final Architecture Lock §14/§F.
    op.create_index(
        "ix_production_runs_unique_active_source",
        "production_runs",
        ["source_production_requirement_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'IN_PRODUCTION' AND source_production_requirement_id IS NOT NULL"
        ),
    )
    op.create_check_constraint(
        op.f("ck_production_runs_status_timestamp_match"),
        "production_runs",
        "(status = 'IN_PRODUCTION' AND completed_at IS NULL AND canceled_at IS NULL) "
        "OR (status = 'COMPLETED' AND completed_at IS NOT NULL AND canceled_at IS NULL) "
        "OR (status = 'CANCELED' AND canceled_at IS NOT NULL AND completed_at IS NULL)",
    )

    # surplus_inventory: exact immutable origin-cost provenance for a
    # production-created lot (Final Architecture Amendment §2 / Lock Addendum §B).
    op.add_column(
        "surplus_inventory",
        sa.Column("source_total_cost_basis", sa.Numeric(precision=14, scale=2), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_surplus_inventory_source_total_cost_basis_non_negative"),
        "surplus_inventory",
        "source_total_cost_basis IS NULL OR source_total_cost_basis >= 0",
    )
    op.create_check_constraint(
        op.f("ck_surplus_inventory_source_total_cost_basis_provenance_match"),
        "surplus_inventory",
        "(source_production_run_id IS NULL AND source_total_cost_basis IS NULL) "
        "OR (source_production_run_id IS NOT NULL AND source_total_cost_basis IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_surplus_inventory_source_total_cost_basis_provenance_match"),
        "surplus_inventory",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_surplus_inventory_source_total_cost_basis_non_negative"),
        "surplus_inventory",
        type_="check",
    )
    op.drop_column("surplus_inventory", "source_total_cost_basis")

    op.drop_constraint(
        op.f("ck_production_runs_status_timestamp_match"), "production_runs", type_="check"
    )
    op.drop_index(
        "ix_production_runs_unique_active_source",
        table_name="production_runs",
        postgresql_where=sa.text(
            "status = 'IN_PRODUCTION' AND source_production_requirement_id IS NOT NULL"
        ),
    )

    op.drop_column("production_run_ingredients", "cost_basis_source")
