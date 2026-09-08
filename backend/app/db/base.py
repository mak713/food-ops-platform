from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Every CHECK constraint in this schema is given an explicit, complete, deterministic
# name (e.g. "ck_products_product_type") at the point it's declared. The "ck" entry
# here is intentionally just the constraint's own name, not a template that re-prefixes
# it (e.g. "ck_%(table_name)s_%(constraint_name)s" would double-prefix an already
# complete name) — it exists only as a safety net for the rare constraint created
# without an explicit name, not as the primary naming mechanism.
# "fk" intentionally omits %(referred_table_name)s: several table/column name
# combinations in this schema (e.g. surplus_allocations.production_requirement_id ->
# production_requirements) exceed Postgres's 63-character identifier limit once the
# referenced table name is appended too. table_name + column_0_name is still unique
# per FK in this schema (no table has two FKs sharing one column name).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base. Domain models are added starting in Phase 1."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
