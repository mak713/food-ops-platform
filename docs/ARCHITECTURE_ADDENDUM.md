# Architecture Addendum — Post-Freeze Implementation Decisions

**Document status:** Addendum to the V1 architecture freeze (`docs/ARCHITECTURE_SPEC.md`)
**Relationship to the frozen spec:** This document never overrides `docs/ARCHITECTURE_SPEC.md`.
It records implementation-level decisions the spec left open, made during Phase 0/Phase 1 and
approved at their respective checkpoints. Where the two ever appear to conflict, the frozen spec
governs and the conflict must be resolved via the same stop/propose/wait-for-approval discipline
the spec itself requires (§22.2) — not by silently editing either document.

## How to add to this document

Same discipline as the main spec: an entry here reflects a decision already approved at a
checkpoint, not a place to unilaterally record a new one. Do not add an ADR here speculatively or
ahead of the phase/checkpoint that actually established it.

---

### ADR-083 — `uv` as Backend Dependency Manager

`uv` is the authoritative Python dependency manager for the backend, using `backend/pyproject.toml`
and a committed `backend/uv.lock` as the sole source of truth for dependency versions. No parallel
`requirements.txt` or Poetry workflow is used. All backend commands are run through `uv run ...` so
the lockfile stays authoritative.

### ADR-084 — Synchronous SQLAlchemy Sessions

V1 uses synchronous SQLAlchemy 2.x sessions/engine (`Session`, not `AsyncSession`) for all database
access. This prioritizes transactional clarity for the complex multi-step atomic workflows the
spec describes (e.g. Finish Production, §7.13) over async I/O concurrency, which this
portfolio-scale application does not need.

### ADR-085 — Synchronous Path-Operation Convention for DB-Touching Endpoints

FastAPI operations that perform blocking synchronous database work are declared as `def`, not
`async def`, so FastAPI runs them in its threadpool rather than blocking the event loop. This is
the implementation convention for DB-touching endpoints specifically — it is not a blanket
prohibition on `async def` anywhere in the application. A future endpoint doing genuinely
asynchronous, non-blocking work may use `async def` if a concrete need arises.

### ADR-086 — `CreatedAtMixin` Schema Abstraction

`CreatedAtMixin` (`backend/app/db/mixins.py`) provides `created_at` with no `updated_at` column,
for any model whose authoritative Spec §8 column list gives it `created_at` only. It is a
column-shape abstraction, nothing more: using it is not a claim that the row is immutable. Some
created-at-only rows are legitimately updated in place during their lifecycle (e.g.
`RecipeRevision.is_current`, `OrderLine` during allowed order-editing states) without gaining an
`updated_at` column, because the spec's own column list for that table doesn't include one.
Business-critical mutable rows use `TimestampMixin` (`created_at` + `updated_at`) instead, per
their own §8 column list.

### ADR-087 — SQLAlchemy Enum Representation

Domain enums (Spec §8.2: "may be implemented as PostgreSQL enums or constrained strings... an
implementation decision") are represented as `VARCHAR` + `CHECK`, never as native PostgreSQL
`ENUM` types. V1 standardized on constrained `VARCHAR` rather than native `ENUM`. The established
configuration, via the `pg_enum()` helper in `backend/app/db/enums.py`, is:

```python
sa.Enum(
    enum_cls,
    name=name,                  # explicit, deterministic, e.g. "ck_products_product_type"
    native_enum=False,          # render as VARCHAR, not CREATE TYPE ... AS ENUM
    create_constraint=True,     # required — native_enum=False alone does not emit the CHECK
    validate_strings=True,      # Python-side guard against invalid values before hitting the DB
    values_callable=lambda cls: [member.value for member in cls],  # persist .value, not .name
    length=longest_value,       # sized to the longest member of that specific enum
)
```

`create_constraint=True` is not optional — omitting it silently produces a `VARCHAR` column with
no `CHECK` at all. `values_callable` is likewise required: SQLAlchemy's default behavior for a
Python `enum.Enum` persists based on the member's `.name`, not its `.value`, unless told otherwise;
every enum in this codebase is written so `.name` and `.value` are identical strings, so this is a
correctness guarantee against future drift, not a currently-observable bug. Any new enum-like
column must use this same `pg_enum()` helper.

### ADR-088 — Database Constraint/Identifier Naming Convention

All Postgres identifiers in this schema are kept within the 63-character limit through two
established rules, both fixed in Phase 1 after an initial migration attempt hit
`IdentifierError`/silent truncation on several long table+column combinations:

1. **`Base.metadata` naming convention** (`backend/app/db/base.py`):
   ```python
   NAMING_CONVENTION = {
       "ix": "ix_%(table_name)s_%(column_0_name)s",
       "uq": "uq_%(table_name)s_%(column_0_name)s",
       "ck": "%(constraint_name)s",
       "fk": "fk_%(table_name)s_%(column_0_name)s",
       "pk": "pk_%(table_name)s",
   }
   ```
   `"ck"` is deliberately just `%(constraint_name)s`: this convention intentionally preserves the
   complete explicit name supplied to each `CHECK` constraint without adding another
   prefix/template on top of it. `CHECK` constraints are explicitly named at declaration (see rule
   2) — this convention entry is what carries that explicit name through unmodified, not a
   fallback for an unnamed one. `"fk"` deliberately omits `%(referred_table_name)s`: several
   table/column combinations (e.g.
   `surplus_allocations.production_requirement_id` → `production_requirements`) exceed 63
   characters once the referenced table name is appended too; `table_name` + `column_0_name` is
   still unique per FK in this schema, since no table has two FKs sharing one column name.

2. **Every `CHECK`/`UNIQUE`/enum constraint gets an explicit, hand-chosen name** — never left to
   convention-based inference. Naming pattern:
   `ck_<table>_<column>_<positive|non_negative>` for single-column numeric checks,
   `ck_<table>_<column>` for enum checks, `ck_<table>_<short semantic description>` for
   multi-column checks (e.g. `ck_order_lines_line_type_shape`,
   `ck_order_cost_allocations_source_exclusivity`). Where a fully-descriptive name would still
   exceed 63 characters, consistent abbreviations are used rather than ad hoc truncation:
   `production`→`prod`, `requirements`→`reqs`, `ingredients`→`ingr`, `non_negative`→`nonneg`,
   `estimated`→`est` (e.g. `ck_prod_run_ingr_wavg_unit_cost_snapshot_nonneg`).

Any future migration — including non-domain infrastructure tables permitted under the spec's
framework-infrastructure exception (§8.1, ADR-072), such as session or password-reset tables —
must follow both rules, since Postgres silently truncates (and can silently collide) identifiers
over 63 characters rather than always raising an error.
