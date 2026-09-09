"""Schema-level tenant-scoping smoke test (Phase 1 plan §1.7).

Not the full Spec §19.14 tenant-isolation suite — Phase 2 added the real one at
tests/integration/security/test_tenant_isolation.py, covering what actually exists at
this phase (User/Business session/context derivation); the full foreign-resource-by-ID
404 proof is Phase 3's responsibility once a tenant-owned resource is reachable by ID.
This test only proves every tenant-owned table's business_id column is NOT NULL,
introspected via information_schema so a future table added without one fails this test
automatically rather than silently slipping through.
"""

from sqlalchemy import text

# Framework/non-tenant tables that legitimately have no business_id, plus
# `businesses` itself — the tenant root, not a tenant-owned table.
#
# `sessions`/`password_reset_tokens` (Phase 2) are the framework-infrastructure exception
# (Spec §8.1, ADR-072) — they're scoped by user_id, not business_id; a session belongs to
# a User, not directly to a Business (the 1:1 owner relationship makes that derivable, not
# stored twice).
_NON_TENANT_TABLES = {
    "users",
    "businesses",
    "alembic_version",
    "sessions",
    "password_reset_tokens",
}


def test_every_domain_table_has_a_not_null_business_id(session):
    tables = (
        session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        .scalars()
        .all()
    )

    domain_tables = [t for t in tables if t not in _NON_TENANT_TABLES]
    assert domain_tables, "expected at least one domain table to exist"

    missing_business_id = []
    nullable_business_id = []
    for table in domain_tables:
        row = session.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table "
                "AND column_name = 'business_id'"
            ),
            {"table": table},
        ).fetchone()
        if row is None:
            missing_business_id.append(table)
        elif row[0] != "NO":
            nullable_business_id.append(table)

    assert not missing_business_id, f"tables with no business_id column: {missing_business_id}"
    assert not nullable_business_id, f"tables with nullable business_id: {nullable_business_id}"
