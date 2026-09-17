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

### ADR-089 — Session Architecture: DB-Backed Opaque Sessions, No Application Secret

Sessions use a DB-backed opaque-token design under the framework-infrastructure exception
(§8.1, ADR-072): the `sessions` table (named `AuthSession` in Python to avoid colliding with
`sqlalchemy.orm.Session`) stores only a SHA-256 hash of a `secrets.token_urlsafe(32)` session
token and a SHA-256 hash of a separately-generated CSRF token — never the raw values. This was
chosen over a stateless signed cookie specifically so logout, password change, and password reset
can truly revoke a session server-side (Spec 10.1's "session invalidation on logout"), which a
signed cookie cannot provide before its own expiry. Because every token is opaque/random and
compared only via hash, no application signing/entropy secret (e.g. `SESSION_SECRET_KEY`) is used
anywhere — there is nothing to sign. `sessions.expires_at` is a single sliding-idle-deadline
column, recomputed on each authenticated request as `min(now + 7d, created_at + 30d)`; the 30-day
absolute cap is computed from `created_at` rather than stored in a second column.
`password_reset_tokens` follows the same opaque/hashed pattern, with a 30-minute, single-use
token. Both tables FK to `users.id` with `ON DELETE CASCADE` (account deletion needs no separate
session-revocation step) and index `user_id` explicitly (not auto-indexed by Postgres); neither
adds a redundant separate index on `token_hash`, since its `UNIQUE` constraint already supplies one.

### ADR-090 — CSRF: Double-Submit, Session-Bound, Plus Origin/Referer Allow-List

Authenticated mutating requests must pass five checks, not merely a double-submit comparison: the
`fo_csrf` cookie and `X-CSRF-Token` header must both be present, be equal via constant-time
comparison (the double-submit half), and the header's SHA-256 hash must match *that specific
session's* stored `csrf_token_hash` (the session-bound half — a syntactically valid pair issued
for a different session fails here even though double-submit alone would pass it); an
Origin-preferred, Referer-fallback allow-list check (normalized `scheme://host:port`, never a raw
string/prefix match against `Referer`'s path-bearing value) must also pass independently, as
defense-in-depth. Pre-session endpoints (signup, login) get the Origin/Referer check only, via a
distinct `require_origin_check` dependency — a deliberate, narrower control, not an absence of one.

### ADR-091 — Password Hashing: Argon2id With Explicit Parameters and a Rehash Path

Passwords are hashed with `argon2-cffi`'s `PasswordHasher`, configured explicitly
(`time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID`) rather
than left on library defaults, so the choice is documented and stable across dependency upgrades.
After a successful verify, `check_needs_rehash` is consulted and the password is transparently
re-hashed with the now-in-hand plaintext if parameters were later tuned upward — allowing future
parameter upgrades to roll out lazily without forcing a password reset.

### ADR-092 — Login Enumeration Resistance via a Dummy-Hash Argon2id Verify

Login returns an identical `AUTH_INVALID_CREDENTIALS` response for "no such email" and "correct
email, wrong password." To close the Argon2id timing gap between those two cases (a real verify
takes measurably longer than an immediate "not found" return), the service maintains one fixed,
precomputed dummy Argon2id hash (not any real user's) and runs a real `verify()` against it when
the submitted email doesn't match any user, before returning the generic 401.

### ADR-093 — V1 Password Policy: Length-Only, No Composition Rules

V1 requires a minimum of 15 and maximum of 128 characters, with no composition-class rules (no
forced upper/lower/digit/symbol mix), no periodic forced rotation, and no external
breached-password blocklist/API dependency. Passphrases, spaces, and normal printable characters
are allowed; the value is never trimmed or silently truncated — a request outside the length
bounds is rejected with a clear `422`. Enforced once, via `Field` constraints on the Pydantic
request schemas, sourced from constants in `app/core/security.py`.

### ADR-094 — Signup Atomicity Includes the Initial Session

Because signup auto-authenticates the new account, the `User`, `Business`, and initial `sessions`
row are created within one transaction; session/CSRF cookies are set on the HTTP response only
after that transaction commits. A `User`/`Business` flush precedes constructing the session row
(rather than adding all three and flushing once) because `sessions.user_id` has no ORM-level
`relationship()` back to `User` (deliberately omitted to avoid modifying the frozen Phase 1
`User` model) — without that first flush, SQLAlchemy's automatic insert-ordering has no way to
know `users` must be written before `sessions` in the same flush, and the insert fails with a
foreign-key violation. Duplicate-email signups return `409 AUTH_EMAIL_ALREADY_REGISTERED` (not
422 — an existing-resource conflict, not a request-shape validation failure), handled at both the
pre-check-query layer and the DB unique-constraint layer, so a genuine two-request race resolves
to exactly one success rather than a duplicate row or an unhandled 500.

### ADR-095 — Password Change Rotates Sessions; Password Reset Requires Re-Login

An authenticated password change verifies `current_password` before anything else changes, then
revokes every session for that user — including the one making the request — and immediately
issues a fresh session+CSRF pair to the same response, so the current browser stays logged in
under rotated credentials while every other session/device is cut off immediately. An
unauthenticated password-reset confirmation instead revokes all sessions with no replacement,
since there is no already-authenticated browser to hand new credentials back to. Reset
confirmation runs inside one `SELECT ... FOR UPDATE`-guarded transaction so two concurrent
confirmations of the same token cannot both succeed.

### ADR-096 — Account Deletion: Fully Implemented in Phase 2

AUTH-008's account/business deletion is fully implemented now, not deferred as groundwork — only
`users`/`businesses` exist as of Phase 2, and Phase 1's own `test_full_tenant_graph_deletion_succeeds`
already proves `DELETE FROM businesses` cascades cleanly, so the real flow is safe today and stays
correct as later phases add cascading tables. It requires an authenticated session, the full
CSRF check, current-password verification, and the caller typing the business's exact current
name; execution deletes the Business row first, then the User row, matching the existing
`NO ACTION` FK direction Phase 1 established.

### ADR-097 — Rate Limiting: `slowapi`, IP-Keyed, No Forwarded-Header Trust

Login, signup, and password-reset-request are rate-limited with `slowapi`'s in-memory store
(10/minute, 10/minute, 5/hour respectively — tunable V1 defaults, not spec-mandated numbers),
keyed by the direct socket peer address (`get_remote_address`) only. `X-Forwarded-For`/`X-Real-IP`
are not trusted, since this deployment has no reverse-proxy/trusted-proxy configuration; a future
trusted-proxy deployment would need to explicitly allow-list the proxy's own IP before trusting
its forwarded header.

### ADR-098 — Tenant-Isolation Test Scope Deferred to Phase 3 for By-ID Resources

Phase 2's tenant-isolation suite (`tests/integration/security/test_tenant_isolation.py`) proves
what Phase 2 actually has: session/context derivation can't be influenced by a client-supplied
`business_id` anywhere, and forged/tampered/expired/revoked sessions never authenticate as any
business, using at least two independently created Businesses. The full "foreign-tenant
resource-by-ID → non-revealing 404" proof (Spec §10.5/17.12) is deferred to Phase 3, once a real
tenant-owned resource reachable by ID exists (`Customer`) — Phase 2 instead establishes the
reusable pattern (`app/core/tenant.py`'s `NotFoundError`) that Phase 3 applies. A throwaway
by-ID endpoint was deliberately not added to Phase 2 solely to exercise that proof early.

### ADR-099 — Tenant-Scoped Resource Lookup: Query-Level `(id, business_id)` Scoping

`Customer` and `Product` are fetched through `app/core/tenant.py::get_owned_or_404` via their
per-resource named wrappers (`get_customer_for_business`, `get_product_for_business`), matching
the call-shape Spec §10.4 illustrates. Its `SELECT` itself is scoped by both `id` and
`business_id` in one query (`select(model).where(model.id == id_, model.business_id ==
business.id)`), never a bare primary-key fetch followed by a post-hoc ownership check. A row
belonging to another tenant and a genuinely nonexistent row are structurally indistinguishable
to this function — both produce zero rows from the same query and raise the identical
`NotFoundError` (Spec §10.5) — so no downstream code path can ever hold a foreign-tenant row in
memory, even transiently. `SellingOption` is not fetched through this generic helper — it uses
the specialized `get_selling_option_for_product`, which scopes its own query by `(id,
product_id, business_id)` together, so a selling option belonging to a different product *or* a
different tenant is equally unreachable in a single query, given an already tenant-verified
parent `Product`. List endpoints scope their own `WHERE business_id = ...` clause directly,
never relying on post-fetch filtering. This is the reusable pattern every later by-ID resource
router is expected to follow — a generic scoped-lookup helper for directly tenant-owned
resources, and a specialized parent-scoped query for resources owned through a parent.

### ADR-100 — Optimistic Concurrency: `version_id_col` Plus an Application-Level Pre-Check

`Customer`, `Product`, and `SellingOption` (the three aggregates Spec §8.33 names as in scope
for Phase 3) each declare `__mapper_args__ = {"version_id_col": cls.version}` via a
per-class `@declared_attr.directive` — not on the shared `VersionMixin` itself, so `User`/
`Business` are unaffected. This is SQLAlchemy's built-in optimistic-concurrency mechanism: every
`UPDATE` is emitted as `... WHERE id = ? AND version = ?`, auto-incrementing `version` on
success and raising `sqlalchemy.orm.exc.StaleDataError` if a concurrent write already moved the
row. Every mutating request (`PATCH`, `POST .../archive`, `POST .../reactivate`, and `DELETE`'s
query parameter) additionally carries a client-submitted `version`, which each service function
compares against the freshly-loaded row's current value *before* mutating — this is the primary,
common-case defense against a stale cross-request edit. The rarer within-transaction race that
only the ORM-level mechanism can catch is translated by `commit_or_raise_stale` into the same
`409 STALE_VERSION` `ApiError`, with the exact user-facing message Spec §17.8 specifies ("This
record changed since you opened it. Refresh the latest version and review your changes before
saving again."), so a caller cannot distinguish which of the two layers caught the conflict.
Archiving/reactivating a resource that is already in the target state is treated as an idempotent
no-op (still version-checked first, but leaves `version` and `updated_at` untouched if nothing
would actually change) rather than a real state transition.

### ADR-101 — List Pagination Envelope: `PageResponse[T]`

Every list endpoint returns a generic `PageResponse[T]` envelope — `{items: T[], total: int,
limit: int, offset: int}` — built with PEP 695 generic syntax (`class PageResponse[T](BaseModel)`
in `app/schemas/common.py`), offset/limit-based with a default `limit=50`. This is the first use
of this shape in the codebase (Phase 2 had no paginated list endpoint) and is the pattern every
later phase's list endpoints are expected to reuse rather than inventing a per-resource shape.

### ADR-102 — Duplicate-Warning API Mechanism (Customer-Only in Phase 3)

A likely-duplicate `Customer` on create produces a `422` with a `WARNING`-severity issue
(`POSSIBLE_DUPLICATE_CUSTOMER`, code `CUSTOMER_CREATE_WARNING` at the envelope level) rather than
silently succeeding or hard-blocking; the client resubmits with `confirm_duplicate: true` to
create unconditionally. Matching is case-insensitive exact comparison on `name` (trimmed,
case-folded), OR exact match on a trimmed+case-folded `email`, OR exact match on a
trimmed-only `phone` (deliberately no digit-stripping or country-code normalization, to avoid
an incorrect equivalence assumption without a phone-parsing dependency) — no fuzzy/similarity
matching. The check does not re-run on `PATCH`, so editing an unrelated field on an existing
Customer never surprises the user with a duplicate warning triggered by their own unchanged
name. Phase 3 implements this for `Customer` only — the phase-plan's own bullet list names
"duplicate warning" solely under Customers. Spec §17.6's broader mention of Products/Ingredients
is a general V1-wide capability statement, not a phase-scoping instruction; extending this
mechanism to other resources remains deferred to whichever future phase explicitly scopes that
work.

### ADR-103 — Safe-Delete Pattern: Attempt-Delete With Driver-Specific FK-Violation Narrowing

A resource's `DELETE` attempts the delete directly and translates a failure into a `409` only
when the failure is specifically a foreign-key violation — narrowed to
`isinstance(exc.orig, psycopg.errors.ForeignKeyViolation)`, not a blanket
`except IntegrityError`, so an unrelated constraint failure is never misreported as "this record
has references." This relies on the frozen schema's own `ondelete="NO ACTION"` FKs (Postgres
itself refuses the delete and raises the violation) for every reference into `customers`/
`products`/`selling_options` except one: `recipes.product_id`, the sole `CASCADE` edge, which the
database would otherwise silently remove protected historical data through. `delete_product` is
the only delete path with an explicit pre-check (`Recipe` existence, not "active recipe" —
`Recipe` has no such concept), raising `409 PRODUCT_HAS_RECIPE` before the delete is even
attempted; every other reference (order lines, purchased-product inventory and reservations,
production requirements/runs, surplus inventory) is caught automatically by the attempt/catch
path with zero per-table application code, and stays correctly protected as later phases
populate those tables.

### ADR-104 — Ingredient Joins the Optimistic-Concurrency `version_id_col` Cohort

`Ingredient` now declares the identical `__mapper_args__ = {"version_id_col": cls.version}`
`@declared_attr.directive` wiring ADR-100 established for `Customer`/`Product`/`SellingOption` —
Spec §8.33 already named `Ingredient` in its optimistic-concurrency list; Phase 3 simply hadn't
reached it yet. Every mutating Ingredient operation (`PATCH`, archive, reactivate, delete) performs
the identical client-submitted-`version` pre-check via `check_version` before writing anything.
From there, the two write shapes translate the rarer within-transaction race the same way but
through different code paths: `PATCH`/archive/reactivate route their commit through the shared
`commit_or_raise_stale` helper, while hard-delete performs its own direct `db.delete(...)` +
`db.commit()` and explicitly catches the ORM's `StaleDataError` itself (since a delete has no
"changed columns" to route through the update-oriented helper). Both forms translate that race
into the identical `409 STALE_VERSION` envelope — no variant *contract* was introduced for it, even
though the delete path's commit is not literally a call to `commit_or_raise_stale`. `Recipe` and
`RecipeRevision` deliberately do not follow this pattern at all; see ADR-105.

### ADR-105 — Concurrency for Version-less, Append-Only Aggregates: Row Lock Plus Expected-Parent-State-ID Equality Check

`Recipe` and `RecipeRevision` carry no `version` column — Spec §8.33 does not name them, and
`RecipeRevision` content is immutable by design (§8.9/§15.10), so a version column on it would be
meaningless. Editing a Recipe means creating a new immutable `RecipeRevision` and switching which
one is current, never mutating existing content in place — an append-only-revision shape
ADR-100's `version_id_col` mechanism doesn't fit.

The established concurrency idiom for this shape instead is a real PostgreSQL row lock
(`SELECT ... FOR UPDATE`), applied differently depending on whether a current revision already
exists:

- **Replacement revision** (a Recipe already exists): lock the `Recipe` row itself
  (`app/core/tenant.py::lock_recipe_for_product`), load exactly one current revision under that
  lock, and compare its id against the client-supplied `expected_current_revision_id` — captured
  by the client when its edit session began, never silently re-derived. A mismatch is
  `409 RECIPE_REVISION_CONFLICT` with no write: the same "reject a stale write, never silently
  overwrite" principle Spec §17.8 states for version-column resources, enforced here through a
  lock-and-compare rather than a version column. Only once that comparison passes does the
  replacement (flip old `is_current`, insert the new revision) proceed under the same lock.
- **First-Recipe creation** (no Recipe, and therefore no current revision, exists yet): there is
  nothing to compare an `expected_current_revision_id` against, so none is sent or checked. Instead
  the `Product` row itself is locked first (ADR-106), and Recipe-nonexistence is checked under that
  lock — serializing first-Recipe creation against both a concurrent Product deletion and a
  concurrent competing first-Recipe creation for the same Product.

Both shapes share the same durable principle — contested/stale parent state is checked under a
real row lock before any write, never silently overwritten — but only the replacement-revision
shape has an existing revision to compare against; first-Recipe creation has no analogous
"expected" value to check at all. Any future version-less, append-only-revision-style aggregate
should follow whichever of these two shapes actually fits (an existing-state comparison under a
lock when prior state exists, or a plain existence-check under a lock when it doesn't) rather than
inventing a version column that would misrepresent immutable content as though it had
independently-mutable state of its own.

A detected violation of the "exactly one current revision" invariant (zero or more than one
current row found under the lock) is never silently repaired or arbitrarily picked — it raises an
unhandled `RuntimeError`, rolling back and surfacing through the application's existing
generic-500 path. This is the standing convention for any future phase that detects a broken data
invariant at read time: crash loudly through the existing error path, never self-heal a corrupted
invariant transparently.

### ADR-106 — Lock Ordering Discipline: Deterministic Multi-Row Order and Shared-Ancestor Locking Across Racing Workflows

Two related lock-ordering conventions were established, both aimed at bounding deadlock risk
without claiming to eliminate it entirely:

1. **Deterministic multi-row locking.** Whenever one operation must lock more than one row of the
   same table — `ingredient_service.lock_ingredients_for_business` locking every Ingredient a
   Recipe/Revision references — the rows are locked in a fixed, deterministic order (sorted by
   `id`) rather than in submission order. Any two concurrent operations locking overlapping rows
   of that table therefore always request their locks in the same relative order, so the classic
   circular-wait deadlock precondition cannot arise between them. This is the standard pattern for
   any future phase that must lock multiple sibling rows together (e.g. multiple Order lines,
   multiple inventory rows).
2. **Shared-ancestor locking for cross-workflow races.** Two independently-evolving workflows that
   can race on a common parent resource — first-Recipe creation and Product deletion, both of
   which depend on whether a Product currently has a Recipe — both lock that same shared ancestor
   row (`product_service.get_product_for_business_locked`), in the same relative position within
   each workflow (first, before anything that reads/depends on the contested state), so the two
   workflows always serialize on it instead of racing past each other. This is the durable
   convention for any future pair of independently-evolving workflows that can race on a shared
   parent (e.g. a future Order-cancellation vs. Production-start race).

Across both first-Recipe creation and replacement-revision creation, the fixed order is always
parent-row-first, then Ingredient rows in sorted order — never reversed.

### ADR-107 — Non-Disclosing Validation for a Body-Embedded Relationship Reference

ADR-099 covers a URL-path resource lookup: a foreign-tenant or nonexistent resource id in the URL
produces the shared `NotFoundError` → `404`. A different shape of reference — a resource id
embedded inside a request body, referencing a sibling resource rather than identifying the
resource the URL itself addresses (a Recipe/Revision request's `ingredients[].ingredient_id`) —
instead produces a structured `422` with a per-line issue (code `INGREDIENT_NOT_FOUND`, `field`
pointing at the specific array index), deliberately identical in shape whether the id doesn't
exist at all or belongs to another tenant. This is a distinct, second non-disclosure pattern
future phases should reach for whenever a request body references a sibling resource by id (as
opposed to the URL identifying the resource itself): a body-embedded foreign/missing reference is
a validation-shaped `422` issue, not a `404`, but the foreign-vs-missing indistinguishability
requirement (Spec §10.5) still applies identically.

### ADR-108 — Archived-Master-Data Carry-Forward Rule

An archived (no longer active) master-data record — first established for `Ingredient` — may not
be newly introduced into a fresh reference, but a reference already present before it was archived
remains valid and is preserved rather than force-removed or force-rejected. Concretely, for a
replacement Recipe Revision: a line's Ingredient *identity* may be carried forward from the base
revision only when that same Ingredient was already present on that immediately preceding
revision — it can never be newly (re-)introduced once archived, even if it was referenced on some
earlier revision further back. Once carried forward, that line's Ingredient identity stays fixed
to the archived Ingredient, but the line is not otherwise frozen: its quantity and unit remain
editable in the new revision like any other line (subject to the usual unit-family compatibility
check against that Ingredient), and the line may be removed from the new revision entirely if it's
no longer wanted. If it is removed, that same archived Ingredient cannot later be newly re-added to
a still-later revision while it remains archived — removal is not a temporary edit that can be
undone by re-adding the identity once archived, only by the Ingredient being reactivated first. None
of this touches the base revision itself: as always, it is immutable history and is never rewritten
by a later edit or by an unrelated archive action. This is a general master-data rule, not an
Ingredient-specific one — any future phase introducing another archivable master-data resource
referenced by an otherwise-immutable historical record (e.g. a future archived Customer discount
tier still validly referenced by a past Order) should default to this same "active-only for a new
reference, carried-forward-only for an already-present one, and not restorable by re-adding once
removed" shape rather than reinventing the decision per resource.

### ADR-109 — `app/domain/` as the Home for Deterministic Pure Calculators, With Mandatory Self-Contained Decimal-Context Isolation

Spec §11.3 calls for deterministic, side-effect-free business calculators (`RecipeScaling`,
`UnitConversion`, and others named for later phases). Phase 4 establishes `app/domain/` as the
package these live in (`app/domain/unit_conversion.py`, `app/domain/recipe_scaling.py`) and
demonstrates the concrete implementation bar the pattern requires: no HTTP or database access,
and — since these are Decimal-heavy calculations — every public function runs its arithmetic
inside its own explicit `decimal.localcontext()` with a fixed precision, so a result can never
depend on, or leak into, whatever ambient `decimal.getcontext()` precision the caller happens to
have set. Later phases' own domain calculators (demand aggregation, surplus allocation, planned
costing, etc.) are expected to live under this same package and meet this same
self-contained-context bar, not just the "pure function, no I/O" half of it.

**Phase 7 addition — domain/application layering for representability failures:** the pure
`app/domain/` layer never imports or raises `ApiError` or any other application/HTTP exception
type, even for a representability/overflow condition (e.g. a derived quantity that would not
fit `NUMERIC(18,6)`, or an `INTEGER` batch/minute count out of int32 range). A pure calculator
only returns a value or a plain, non-raising validation result — detecting that a value doesn't
fit is itself just a boolean/value-returning function, not an error-raising one. Translating a
representability failure into a structured API error, and owning any resulting rollback, is
exclusively a service-layer responsibility (`operational_recalculation_service.py`'s
`_persist_numeric_18_6`/`_persist_int32`/`_persist_suggested_start` helpers), matching the
established pattern `order_service._snapshot_to_columns` already uses. This keeps the "no HTTP
or database access" boundary this ADR already establishes from being quietly narrowed to "no
HTTP access, but domain code may still raise HTTP-shaped errors."

### ADR-110 — Initial Balance: One-Time Physical/Cost-Basis Seeding, Not a Purchase Event

"Initial Balance" (Ingredient and Purchased Product Inventory) represents inventory that
already physically existed at the moment inventory tracking began for that resource — it is
a one-time seeding operation, not an ordinary purchase. Once any inventory transaction of
any type exists for a resource, Initial Balance may never be used again for that resource;
the request is rejected outright rather than falling back to a different behavior. It
requires a strictly positive starting quantity and a non-negative cost basis, and it sets
exactly two fields: physical quantity and weighted-average unit cost. Because it is not
evidence that an actual purchase occurred, Initial Balance never sets Latest Purchase Cost
and never creates a Replacement Cost override — both remain NULL after Initial Balance,
distinguishing "we know a cost basis" from "we know a market/purchase price."

For Ingredient, the existing versioned Ingredient aggregate (ADR-104) is mutated in place, so
an Initial Balance request carries and checks the caller's expected `version` like any other
Ingredient mutation. The one-time-only check ("has any transaction ever been recorded") is a
check-then-act race against a second, concurrent Initial Balance attempt on the same
never-yet-initialized Ingredient — a version check alone does not serialize this, since both
concurrent requests can legitimately observe the same pre-initialization version before
either writes. The Ingredient row is therefore locked (`SELECT ... FOR UPDATE`) narrowly,
only for the duration of this one call, so the check and the write happen atomically with
respect to a competing Initial Balance call; every other Ingredient inventory mutation
(Restock, Manual Adjustment, Replacement Cost) uses the existing unlocked version-checked
path, since none of them has this same check-then-act existence race.

For Purchased Product Inventory, no inventory row — and therefore no inventory `version` —
exists before Initial Balance (or a legitimate first Restock, ADR-113) creates it.
Serialization for that creation race is via the shared parent-Product lock, per ADR-106's
shared-ancestor-locking pattern, not via any inventory-row version, since none exists yet to
lock or check.

### ADR-111 — Non-Positive Pre-Restock Ingredient Balances Do Not Participate in the Weighted-Average Blend

For an Ingredient Restock where the physical quantity on hand immediately before the restock
is strictly positive, the standard weighted-average formula (Spec §15.3) applies without
modification. Where that pre-restock quantity is zero or negative, the new weighted-average
unit cost is instead set directly to the restock's own normalized incoming purchase unit
cost — the prior quantity and prior weighted-average cost are both discarded from the blend
entirely, not merely down-weighted. A zero-or-negative balance is treated as a
deficit/reconciliation state rather than positively-valued inventory available to participate
in the blend, and therefore receives no quantity/cost weight in it — blending a deficit's
stale cost into a real purchase's cost would produce a mathematically misleading weighted
average. This rule applies uniformly regardless of which legitimate inventory event produced
the zero/negative state: Phase 5 can produce one through Manual Adjustment (ADR-114), and a
later Production phase may produce one through real Production Consumption; neither source
changes this approved weighted-average behavior, since the rule depends only on the sign of
the pre-restock balance, never on how that balance came to be. This rule is scoped to the
weighted-average calculation only — it does not change how the resulting physical quantity
itself is computed (still `old_quantity + purchased_quantity`, whatever sign that lands on).

### ADR-112 — Replacement Cost Is a Fallback-Chained, Nullable-Column Override With No Separate Provenance Column

Both Ingredient and Purchased Product Inventory expose a `replacement_unit_cost` column that
is itself the complete override-state signal — no separate boolean/provenance column exists
or is needed to distinguish "no override" from "override present." `replacement_unit_cost IS
NULL` means no explicit override exists, and the effective replacement cost falls back to
`latest_purchase_unit_cost` (itself NULL, and therefore an unknown/unset effective value,
until at least one Restock has occurred). `replacement_unit_cost IS NOT NULL` means the
seller has explicitly taken over maintenance of that value, and it is used as-is. A Restock
always updates `latest_purchase_unit_cost`, and never writes to `replacement_unit_cost` under
any circumstance — an explicit override, once set, survives every subsequent restock until
the seller explicitly changes or clears it. The seller sets or clears the override through the
dedicated Replacement Cost maintenance action; clearing means writing an explicit `NULL`,
restoring automatic fallback to Latest Purchase Cost. The authoritative fallback rule —
`replacement_unit_cost ?? latest_purchase_unit_cost` — is implemented once, as a shared pure
domain helper (`resolve_effective_replacement_cost`); any future consumer of a planned/expected
cost (e.g. a later Shopping List or planned-costing phase) must call this same helper rather
than re-deriving the fallback logic independently.

### ADR-113 — Purchased Product Inventory Joins the Optimistic-Concurrency Cohort; First-Row Creation Is Serialized Through the Parent Product Lock

`PurchasedProductInventory`'s existing `version` column is wired through SQLAlchemy's
`version_id_col` mechanism (`__mapper_args__`, the identical `@declared_attr.directive`
pattern ADR-100 established and ADR-104 extended to Ingredient) — Spec §8.33 already scopes
Purchased Product Inventory into the optimistic-concurrency cohort. For a Purchased Product
Inventory row that already exists, every mutation (Restock, Manual Adjustment, Replacement
Cost) requires the caller's expected inventory `version` and follows the same unlocked
check-version-then-write shape already established for Ingredient — no Product-parent lock is
taken merely to update an already-existing row.

The row's creation is a distinct problem: before it exists, there is no `version` to check, so
an absent inventory version on a request is not a validation gap — it is the caller's explicit
assertion "I believe no row exists yet for this Product." That assertion is verified, not
trusted: the shared parent `Product` row is locked first (ADR-106's shared-ancestor pattern),
and only under that lock is the inventory row's absence re-checked. If another request has
already created the row by the time the lock is acquired, the caller is rejected (`409`) and
must refresh and retry against the now-existing row, rather than the system silently applying
an unversioned write on top of a row it never actually observed. Both Initial Balance and a
legitimate first-ever Restock (a first purchase may itself be the very first inventory event
for a Purchased Product) are permitted to create the row this way, and each retains its own
distinct transaction-type semantics (`INITIAL_BALANCE` vs. `RESTOCK`) — Restock's creation
path does not become an Initial Balance merely because it happens to be first. The schema's
`UNIQUE(business_id, product_id)` constraint remains in place as a database-level backstop
against this exact scenario, but is not the primary mechanism relied on to prevent a duplicate
row — the Product lock is.

### ADR-114 — Inventory Availability Under Archived/Inactive Master Data; Negative Ingredient Balances Represent Reality, Not a Production Workflow

When an Ingredient is archived or a Purchased Product is inactive, its inventory remains
fully readable — current balance, cost fields, and transaction history are never hidden or
altered by the master-data state change — and Manual Adjustment remains available, since
reconciling already-recorded stock is a legitimate need regardless of whether the resource is
currently active for new use. Three actions are blocked until the resource is reactivated:
Initial Balance, Restock, and explicit Replacement Cost set/clear — all three represent
bringing new stock or new forward-looking cost data onto a resource the seller has
deliberately taken out of active use, which reactivation is the intended gate for. This policy
never deletes or hides any historical inventory truth.

Ingredient physical quantity may become negative as a direct result of Manual Adjustment, and
Phase 5 represents that resulting value exactly as computed — it is never clamped to zero and
never blocked from going negative. The UI surfaces a generic reconciliation-attention state
whenever an Ingredient's balance is negative. This is a display treatment of whatever value
Manual Adjustment (or, in a later phase, real Production Consumption) has produced — it is not
itself a production or consumption workflow, and Phase 5 implements no such workflow.
Purchased Product Inventory has no equivalent negative-balance concept: its physical quantity
remains non-negative, enforced by both an application-level pre-check and the schema's
existing `CHECK` constraint as a backstop.

Phase 5 implements Initial Balance, Restock, Manual Adjustment, Replacement Cost maintenance,
and inventory history for Ingredient and Purchased Product Inventory only. It does not
implement Ingredient or Purchased Product reservations, Orders, operational recalculation,
Shopping List derivation, Production Requirements/Runs, Start/Finish Production, the
`PRODUCTION_CONSUMPTION` or `ORDER_FULFILLMENT` transaction workflows, Surplus, readiness, or
analytics. The `InventoryTransaction`/`PurchasedProductInventoryTransaction` schemas'
pre-existing enum values for those later transaction types (established at the Phase 1
migration) are schema readiness for future phases, not evidence that those workflows exist
yet.

### ADR-115 — Phase 5 Decimal Persistence Convention: Unrounded Calculators, One Quantization Point, `ROUND_HALF_UP`, Application-Level Representability Validation

Phase 5 is the first phase whose domain calculators produce a derived (converted, blended, or
multiplied) Decimal result that is itself persisted, rather than a raw already-bounded request
value passing straight through. The following convention governs every Phase 5 inventory value
persisted into the applicable `NUMERIC(18,6)` quantity/unit-cost/transaction fields.

The durable, cross-phase architectural principles this convention establishes — and that later
phases introducing their own derived, persisted Decimal results must also follow — are: Decimal-
only arithmetic; explicit, fixed local Decimal contexts; deterministic calculations with no
dependence on ambient Decimal context; avoiding premature rounding; and explicitly quantizing
only when a value crosses into a persisted fixed-precision representation, at the precision and
scale appropriate to that value's own authoritative destination column. The specific six-decimal
quantization and `ROUND_HALF_UP` rounding mode below are the Phase 5 inventory rule for its own
`NUMERIC(18,6)` columns — they are not a claim that every future persisted Decimal value must
also be stored at six decimal places. A future phase persisting a derived value into a
differently-scaled authoritative column (e.g. a `NUMERIC(14,2)` money field) follows this same
calculation/persistence *pattern* while honoring that column's own precision/scale and whatever
domain-specific rounding rule is explicitly approved for it, rather than reusing Phase 5's
six-decimal figure by default.

Domain calculators (`app/domain/inventory_costing.py`, and `unit_conversion.py`'s
cost-conversion addition) remain pure per ADR-109 — Decimal-only arithmetic inside their own
explicit `decimal.localcontext()` — and return full, unrounded precision; they never round
internally. Exactly one quantization point exists for each derived value, at the service
layer, immediately before that value is assigned to a `NUMERIC(18,6)`-backed column:
`Decimal.quantize(Decimal("0.000001"), rounding=decimal.ROUND_HALF_UP)`. `ROUND_HALF_UP`
(round-half-away-from-zero) is the chosen mode everywhere this quantization occurs, rather
than Python's own `Decimal` default of round-half-to-even, since a seller reading a cost
ledger expects ordinary rounding behavior.

For a restock's ledger entry specifically, the normalized quantity and normalized unit cost
are each quantized to their persisted six-decimal value first, and `total_cost` is computed
from those two already-quantized, already-persisted values and quantized once more — never
computed from unrounded intermediates. This trades a theoretically tiny amount of compounded
rounding for a stronger auditability guarantee: a transaction row's stored `total_cost` is
always exactly the arithmetic product of that same row's own stored quantity and unit cost,
verifiable by inspection without re-deriving intermediate precision no longer on the row.

Every derived value is validated for `NUMERIC(18,6)` representability at the application
layer before it is persisted, using a shared deterministic helper
(`is_representable_in_numeric_18_6`); a value that would not fit is rejected with a clean
`4xx` before any write is attempted. The database column's own type/constraints remain a
backstop against a bug in that validation, never the primary, user-facing failure mode. A raw
client-submitted value can never violate this bound (request-schema validation already
constrains it), but a converted quantity, a converted unit cost, a weighted-average result, or
a computed `total_cost` can, through unit-conversion multiplication or through combining two
independently-valid stored values. Distinctly, a strictly positive converted Ingredient
quantity that would quantize to `0.000000` at six-decimal storage precision is rejected
outright rather than silently recording a zero-quantity inventory event — a positive physical
action must never appear in the ledger or the balance as if nothing happened.

**Phase 7 addition:** Phase 7's own `NUMERIC(18,6)` operational columns (`production_requirements`,
`production_ingredient_requirements`, etc.) follow this identical convention unchanged —
calculators remain unrounded; the service layer quantizes exactly once, `ROUND_HALF_UP`;
representability is validated before the value is ever assigned to an ORM column. The
positive-value-collapsing-to-zero rejection above is a **field-scoped** rule, not a blanket one:
it applies only to destination fields whose invariant requires a strictly positive persisted
value (`> 0`) — e.g. `required_quantity_canonical`, which can never legitimately be zero once a
batch is actually being produced. A `NUMERIC` field where zero is itself a legitimate, meaningful
persisted value (e.g. a computed shortage of exactly `0`) is unaffected and continues to allow
it. Phase 7 also introduces one new sibling primitive alongside this Decimal convention: an
analogous `INTEGER` representability guard for its new integer batch/minute columns
(`recommended_batches`, `estimated_active_minutes`, `estimated_elapsed_minutes`) — an
out-of-int32-range value is rejected the same way, before any database write, never left to a
raw column-level overflow. `suggested_start_at` gets a related but distinct guard: a genuine
`datetime` arithmetic `OverflowError`, with every timing input present and individually valid, is
treated as a data-integrity anomaly and rejected with the same structured error family — this is
different from, and must never be confused with, the separate non-error case where a timing
input (fulfillment time or elapsed duration) is simply missing, which yields
`suggested_start_at = NULL` with no rejection at all.

### ADR-116 — Inventory Current State and Historical Ledger Commit Atomically; the Ledger Is Immutable and Never Summed to Reconstruct Current State

For both Ingredient and Purchased Product Inventory, the current physical balance and cost
fields (on the `Ingredient`/`PurchasedProductInventory` row itself) are stored,
directly-read current state — not a value recomputed by summing the transaction ledger on
every read. The `InventoryTransaction`/`PurchasedProductInventoryTransaction` rows are
immutable historical/event truth: Phase 5 provides no edit or delete endpoint for either
table, and none is anticipated for any future phase — a correction to inventory is itself
recorded as a new Manual Adjustment transaction, never as a rewrite of a prior one. Every
inventory-affecting operation (Initial Balance, Restock, Manual Adjustment) writes its
balance/cost-field mutation and its corresponding transaction row within one atomic database
transaction, committed together — there is no code path that can persist one without the
other, whether the operation succeeds or is rejected partway through.

### ADR-117 — Order Aggregate Concurrency: `Order.version` as the Aggregate Boundary, Stable Version-less `OrderLine`s, and `touch_order()`

`Order` joins the `version_id_col` optimistic-concurrency cohort ADR-100 established and
ADR-104/ADR-113 extended to `Ingredient`/`PurchasedProductInventory` — the identical
`__mapper_args__ = {"version_id_col": cls.version}` `@declared_attr.directive` wiring, the
identical client-submitted-`version` pre-check via `check_version` before any write, and the
identical `commit_or_raise_stale` translation of a within-transaction `StaleDataError` race into
`409 STALE_VERSION`. `OrderLine` deliberately receives no version column of its own: `Order.version`
is the single optimistic-concurrency boundary for the *entire mutable Draft aggregate* — header
fields, every line, and their derived totals together — not a per-line concern. A retained line's
`id`/`created_at` never change under any edit (stable-ID reconciliation: a submitted line with a
populated `id` is matched and updated in place; an absent `id` is a new line; a persisted line
whose `id` is not resubmitted is deleted); only the parent `Order.version` need ever be checked or
compared, never an individual line's own state.

This creates one implementation gap SQLAlchemy's default behavior doesn't close on its own: a
Draft edit that changes only a child `OrderLine` row, where the recomputed `subtotal`/`final_total`
happen to net to the same value as before, leaves every column on the `orders` row itself
unchanged — so SQLAlchemy's unit-of-work never considers the `Order` instance dirty, never emits
an `UPDATE orders ...`, and `version` silently fails to advance despite a real mutation having
occurred. `app/core/tenant.py::touch_order(order)` closes this: `order.updated_at = func.now()`,
called unconditionally at the end of every Order-aggregate-mutating service function, immediately
before `commit_or_raise_stale`. Assigning an actual new value (a SQL expression, not the
already-loaded Python `datetime`) unambiguously dirties the attribute through the ORM's ordinary
change-tracking path, which is what places `Order` in `Session.dirty` and causes `version_id_col`'s
unconditional-once-dirty `UPDATE ... SET version = version + 1 WHERE version = :current` to fire —
this is the same path an ordinarily-changed column already takes for a header-only or
total-changing edit; `touch_order()` only guarantees the *child-only, net-zero-total* edit takes it
too. `add_payment` never calls `touch_order()` — inserting a `Payment` is a separate
historical-child operation, not a mutation of the `Order` aggregate's own state (ADR-120).

Every code path that acquires the parent Order row lock (`get_order_for_business_locked`) and
then reaches an expected rejection — a stale-version pre-check failure, a non-`DRAFT` mutation
attempt, an invalid/inactive newly-introduced reference, a Payment-overage warning, a
Draft-total-reduction-vs-Payments warning, a Draft-delete-has-Payments warning, or a
reconciliation/representability failure discovered after the lock is held (ADR-119) — rolls back
explicitly (`except ApiError: db.rollback(); raise`) before raising, so the lock is never left held
by a transaction the caller didn't explicitly end. Reference locking across the Order's three
reference types (Customer, Product, Selling Option) follows ADR-106's deterministic
multi-row/shared-ancestor discipline in one fixed group order — Customer, then Product, then
Selling Option, IDs sorted within each group — every time a request needs more than one, never
reversed.

### ADR-118 — Snapshot-Preserving Draft Edits: Independent Per-Dimension Checks and Line-Type Semantics

ADR-029 (spec) establishes that existing Order Lines preserve their seller-agreed price/packaging
snapshots despite later catalog changes, and ADR-108 establishes the general archived-master-data
carry-forward rule. Phase 6's implementation of that principle for a *retained* `OrderLine` is four
independent per-dimension checks, evaluated in a fixed order against only that line's own
resubmitted fields — never a blanket "refresh from catalog" and never coupled to one another:

1. **Source-identity** — triggers only when the submitted `product_id`/`selling_option_id` (for
   `STANDARD_OPTION`; `CUSTOM_QUANTITY` has only `product_id`; `CUSTOM_ITEM` has no source-identity
   dimension at all) genuinely differs from what's stored. Resubmitting the *same* id, even with
   every other field on the line also resubmitted, is never treated as a source change — this is
   the "same-source reselection is not a catalog refresh" rule. Only this check ever touches
   `display_name_snapshot`, the underlying-quantity basis, and the catalog-derived packaging
   snapshot; if it doesn't fire, nothing else on the line touches those fields either.
2. **Price-only** — triggers on an explicit submitted price differing from the stored snapshot;
   touches only the price snapshot (+ `price_override_reason` if supplied) and `line_subtotal`.
3. **Packaging-only** — meaningful only for `CUSTOM_QUANTITY` (its packaging basis is the one
   seller-editable, non-catalog-derived dimension); triggers on an explicit submitted packaging
   override differing from stored.
4. **Quantity-only** — triggers on a submitted `package_quantity`/`underlying_quantity` differing
   from stored; recomputes only the quantity-derived fields, using whatever price/packaging basis
   is already in effect after checks 2–3 in the same pass, never a live catalog re-fetch.

If none of the four fire, the row is not written at all — an unrelated edit (a different line, or
only Order-header fields) leaves a retained line's snapshot byte-for-byte untouched regardless of
any live catalog change elsewhere. A retained line's reference may carry forward even if it has
since been archived (ADR-108), but a *newly introduced* reference — a new line, or an actual source
change on a retained line — may never target an inactive Customer/Product/Selling Option.

**Line-type interpretation** (ADR-028's spec-level three-type shape, as implemented): `STANDARD_OPTION`
is Product + Selling Option package semantics — `package_quantity` is the seller-editable
dimension, `underlying_quantity` is always derived from it. `CUSTOM_QUANTITY` retains Product-level
automation (a real `product_id`) but `package_quantity` is fixed at `1` always, never seller-edited,
and `charged_unit_price` is the seller-agreed *whole-line* price rather than a per-package rate.
`CUSTOM_ITEM` has no Product/Recipe relationship at all — no fabricated automation, `product_id`
always `NULL`.

`CUSTOM_ITEM.manual_fulfillment_required` defaults to `true` at the application/service layer (a
Custom Item has no Product/Recipe automation to prove fulfillment against, so the safe default
requires manual confirmation); the seller may explicitly turn it off. The frozen column itself
still defaults `false` at the database level — this `true`-for-`CUSTOM_ITEM` default is enforced by
the service whenever the request field is omitted, not by the schema. `STANDARD_OPTION`/
`CUSTOM_QUANTITY` unconditionally force this field `false`, ignoring whatever a client submits.
`manual_fulfillment_satisfied` is always `false` in Phase 6 for every line type and is not accepted
as request input anywhere — it becomes operationally meaningful only once a later phase's
Ready-determination logic reads it.

### ADR-119 — Phase 6 Decimal Persistence Convention: `NUMERIC(14,2)` Money and `NUMERIC(18,6)` Quantities

Following the calculation/persistence *pattern* ADR-115 established for Phase 5 — Decimal-only
arithmetic inside self-contained `decimal.localcontext()`s (ADR-109), unrounded domain calculators,
exactly one quantization point at the service layer, application-level representability validation
before persistence — Phase 6 makes its own, independently-chosen quantum/rounding decisions for its
own two authoritative column shapes, not a reuse of Phase 5's inventory figures:

- Derived customer-facing money persisted to `NUMERIC(14,2)`: `Decimal("0.01")` quantum,
  `ROUND_HALF_UP`.
- Derived Order quantities persisted to `NUMERIC(18,6)`: `Decimal("0.000001")` quantum,
  `ROUND_HALF_UP`. Despite the coincidentally-matching numeric shape, this quantum/rounding pair is
  declared locally in `app/domain/order_pricing.py`, not imported from
  `app/domain/inventory_costing.py` — an independent Phase 6 domain decision, not an accidental
  inheritance from Phase 5's inventory module.

Raw seller-entered values (a typed price, an entered quantity) are validated to their destination
precision via Pydantic `Field(max_digits=..., decimal_places=...)` and rejected with a clean `422`
above that precision — never silently rounded. Every *derived* value (a line subtotal, an Order
subtotal, a `final_total`) is checked for representability at its own destination precision before
assignment, using the same `is_representable_in_numeric_14_2`/`_18_6` shared-helper pattern ADR-115
established; a value that would not fit is rejected before any write. `subtotal` and `final_total`
are checked *independently* of one another — a large negative `order_adjustment` can bring an
out-of-range `subtotal` back into a representable `final_total`, so checking only the final figure
would let an unrepresentable subtotal slip through unnoticed; both are validated on their own terms.
A strictly positive derived quantity that would quantize to `0.000000` at six-decimal storage
precision is rejected outright, mirroring ADR-115's identical Ingredient-quantity rule.

### ADR-120 — Payment Model: Append-Only Events, Derived Status With Zero-Dollar Precedence, and Lock-Serialized Concurrency

ADR-033 (spec) establishes that Payments are internal positive records with derived status and
permitted overpayment. Phase 6's implementation: a Payment is an append-only, individually-positive
record with no edit endpoint and no individual-delete endpoint anywhere — the sole exception is the
full Draft-deletion cascade, which removes every Payment on that Draft only as part of removing the
Draft itself, behind its own explicit has-Payments warning/acknowledgment. Payment status is always
derived at read time from `payments_total` vs. `final_total`, never persisted as its own column.
`derive_payment_status` checks `payments_total == 0` unconditionally *first*, before any `>=`
comparison — so a `$0.00` Order with zero recorded Payments is `UNPAID`, never spuriously `PAID`
merely because `0 >= 0`; a deliberately recorded positive Payment against a `$0.00` Order is
therefore always an overpayment, subject to the same acknowledgment flow as any other. New Payments
are rejected (`409`) against a `CANCELED` Order; Payments remain explicitly permitted after
`COMPLETED` (ADR-034's completion-independent-of-payment rule extends naturally to "and payment can
continue after completion too"). Inserting a Payment never calls `touch_order()` (ADR-117) — it is
a separate historical-child write, not a mutation of the Order aggregate's own version-tracked state.

Concurrency: the parent Order row lock (`get_order_for_business_locked`) is the single serialization
point between a new Payment insertion and a concurrent Draft-total-reducing edit. Both `add_payment`
and `update_draft_order` query the authoritative `SUM(payments.amount)` directly from the database,
*after* acquiring that lock — never from a possibly-stale, possibly-pre-loaded `order.payments`
relationship collection that could have been populated earlier in the same request before the lock
was taken — so the two operations can never disagree about "the current recorded Payments total"
they're each protecting against, and a second concurrent Payment always sees the first's already-
committed sum rather than a stale pre-lock read.

### ADR-121 — Phase 6/7 Confirmation Boundary: Structural Readiness Only, No Persisted `CONFIRMED`

Phase 6 provides no route that ever persists any Order status other than `DRAFT` — `CONFIRMED`,
`READY`, `COMPLETED`, and `CANCELED` (ADR-026, spec) remain reachable in this phase only through
direct test-fixture construction for boundary testing, never through any Phase 6 endpoint, and a
Draft's cascade-delete removes its rows entirely rather than transitioning them. In place of real
confirmation, `OrderResponse` exposes a typed, pure structural-readiness check — `is_confirmable:
bool` and `confirmation_issues: list[{severity, code, message, field}]` — covering only status is
`DRAFT`, at least one `OrderLine` exists, and `fulfillment_date` is present. This check performs no
database access and never re-validates reference active-state, so a validly carried-forward archived
reference (ADR-108) can never cause a false rejection. No `confirmed_at` column is written, no
`DRAFT → CONFIRMED` `OrderStatusHistory` row is ever inserted, and no operational
projection/reservation/allocation table receives any write from any Phase 6 code path.

This is an especially important cross-phase contract: a persisted `CONFIRMED` Order represents real
operational demand — it is the trigger for Phase 7's recalculation, reservation, and downstream
production/shopping-list behavior. Phase 7 must therefore implement the real confirmation action
(persisting `CONFIRMED`, the status-history row, and its operational recalculation/reservation
writes) as a single atomic operation, reusing this same structural-readiness check as its first
validation gate — no code path may ever leave an Order persisted as `CONFIRMED` without its
corresponding operational projection already committed alongside it, since a partially-confirmed
Order (confirmed in name but operationally unaccounted-for) would violate the guarantee every later
phase's reservation/production logic depends on.

Phase 6 implements: Draft create/read/list/edit/delete; Guest/Customer selection, including
switching between them on an existing Draft; inline Customer creation from Order Entry; all three
line types (`STANDARD_OPTION`, `CUSTOM_QUANTITY`, `CUSTOM_ITEM`) with their snapshot semantics
(ADR-118); fulfillment details/notes; a generic Order adjustment and manual tax (ADR-031/ADR-032,
spec); server-authoritative totals; an optional initial Payment at Draft creation and unlimited
subsequent Payments; derived Payment status and overpayment acknowledgment (ADR-120);
Draft-delete-with-Payments acknowledgment; optimistic concurrency and stable `OrderLine` identity
(ADR-117); archived-reference carry-forward (ADR-108); the typed structural-readiness indicator
above; Order Details; a single-page Order Entry UI; and a disabled, clearly-labeled Operational
Impact Preview placeholder. It does not implement: actual confirmation; Cancel/Ready/Complete
workflows; Ingredient/Purchased-Product reservations; operational recalculation or projections;
Production Requirements/Runs; Shopping List derivation; operational cost allocation; or any other
Phase 7+ lifecycle behavior.

### ADR-122 — Order Number Generation: UUID-Derived Token, No Sequence

The seller-facing `order_number` is `f"ORD-{order_id.hex[:12].upper()}"` — derived entirely from the
Order's own already-globally-unique `id` (assigned client-side via `uuid.uuid4()` before insert).
There is no `MAX()+1` query, no separate sequence/counter table, and therefore no schema migration
and no reuse-after-deletion problem. `UNIQUE(business_id, order_number)` remains the database-level
backstop against the astronomically unlikely case of two Orders in the same Business sharing a
12-hex-character id prefix. On that constraint violation, the bounded retry (three attempts)
restarts the *entire* transactional create attempt from scratch — fresh reference locks, fresh
reads, a fresh `id` — rather than only regenerating the `id`/`order_number` pair while continuing to
use ORM objects or locks obtained before the failed attempt's rollback: a rollback releases every
lock the transaction was holding, so continuing to operate on state read under those now-released
locks would be unsound.

### ADR-123 — Phase 7 Operational Recalculation Closure and Atomic Lifecycle Transaction Boundary

`recalculate_product_closure` (Spec §11.4) re-derives a Product's entire operational projection —
`ProductionRequirement`/`-Order`/`ProductionIngredientRequirement`/`IngredientReservation`/
`SurplusAllocation` — from every applicable *active* `CONFIRMED` `OrderLine` referencing that
Product, at any `demand_date`, past or future; there is no "today forward" filter, so an overdue
Order's demand is recalculated exactly like any other. The closure is deliberately Product-scoped:
recalculating one Product never rewrites another Product's own requirement rows. Cross-Product
interaction exists only through shared state a Recipe's ingredients happen to draw from — two
Products' confirmed demand can compete for the same Ingredient's physical stock through their
independently-owned `IngredientReservation` rows (ADR-126 below) — never through any direct
Product-to-Product read or write. Reconciliation is a full replace/rebuild of every rebuildable row
for that Product on each pass, never an incremental patch or an additively-duplicated row, matching
PLAN-010's "replace/reconcile ... rather than duplicate."

This closure is never computed standalone: Confirm, Cancel, a demand-affecting `CONFIRMED` edit, and
Recipe/first-Recipe migration each compose it into their own single atomic transaction. A lifecycle
mutation the recalculation must see (a just-set `CONFIRMED` status, a just-inserted `RecipeRevision`)
is made visible via an explicit `db.flush()` — never an intermediate `db.commit()` — before the
closure reads it; the transaction's real commit happens exactly once, after both the mutation and
its recalculation have succeeded. A rejected warning-acknowledgment set or any recalculation failure
rolls back the *entire* triggering operation, including the lifecycle mutation that was already
flushed — there is no code path that leaves a persisted `CONFIRMED`/`CANCELED` status, or a written
`OrderStatusHistory` row, without its corresponding operational projection change committed
alongside it in the same transaction, fulfilling the guarantee ADR-121 anticipated.

A persisted `CONFIRMED` Order must always remain structurally confirmable: a proposed `CONFIRMED`
edit's candidate state must retain at least one valid Order line and a non-null fulfillment date, and
this structural check runs *before* the production-lock/protected-demand check (ADR-126) — a
fundamentally invalid candidate is reported as non-confirmable, never masked behind an operational
lock conflict it never actually reached. Draft editing remains intentionally looser, unaffected by
this rule. Preview shares the same lifecycle boundary at read time: it is available for a `DRAFT` or
`CONFIRMED` Order only; a `CANCELED`, `READY`, or `COMPLETED` Order is rejected outright as
non-previewable, since Preview is an editable-order simulation, not a general-purpose read — this is
solely a Phase 7 Preview guard and does not imply `READY`/`COMPLETED` behavior itself is implemented
(both remain out of scope, ADR-121).

Two line types participate in this closure on their own terms, never through a borrowed or fabricated
representation of the other. A Custom Item line contributes only transient, clearly-labeled
manual/custom workload guidance computed from its own `custom_active_time_minutes` — no `Product`, no
`Recipe`, no `ProductionRequirement`, and no `IngredientReservation` is ever fabricated for one
(ORD-009/010); the resulting minutes figure is operational guidance for the seller, never disguised
as a produced-goods projection. A Purchased Product's demand is tracked through its own
`PurchasedProductReservation`/shortage path rather than any Recipe-driven production math; a
reservation may legitimately exist for a Product with no corresponding `purchased_product_inventory`
row at all, in which case physical availability is simply treated as zero for the shortage
calculation — Phase 7 never creates a fake inventory row merely to have something to reserve against
or lock, and neither reservation nor confirmation ever physically consumes Purchased inventory
(actual consumption remains a Phase 8 Start/Finish-Production concern).

### ADR-124 — Phase 7 Composed Lock Hierarchy

Order-side workflows (confirmation, a demand-affecting `CONFIRMED` edit, cancellation) acquire locks
in one composed, deterministic sequence: `Order` → the changed/new `Customer` if applicable → the
unified, sorted set of every Product either newly referenced or operationally affected by the
recalculation → newly-referenced Selling Options, sorted → the affected Recipes, in that same
Product order → the globally sorted union of every participating Ingredient → Surplus/Purchased
inventory state, sorted. Recipe (and first-Recipe) creation/revision use a separate, shorter
sequence — `Product` → `Recipe` → one deterministic, globally-sorted union of every Ingredient
referenced by every RecipeRevision participating in the Product's rebuildable closure (including a
retained *historical* pin from an earlier `future_only` migration, not merely "the immediately-prior
revision") plus the submitted/new RecipeRevision → Surplus state — and never acquire an Order,
Customer, or Selling Option lock at any point, since migration only ever mutates Product-scoped
projection/reservation state, never an Order aggregate column. This is not a simple
`old_revision ∪ new_revision` union: because a later `apply_existing` migration can rebuild demand
still pinned to a revision older than the immediately-prior one (ADR-125), the Ingredient set locked
is the union across *every* revision the rebuildable closure could actually touch, computed by the
same `revisions_participating_in_closure` helper both this lock acquisition and the Ingredient/
Recipe lock pass in `order_lifecycle_service` share, rather than two independently-derived answers to
the same question. Demand protected by an active `IN_PRODUCTION` run is never part of the rebuildable
closure in the first place (ADR-126), so a revision pinned only by protected demand contributes
nothing to this lock set.

The concurrency rationale for sharing an Ingredient lock across unrelated Products is precise, not
merely "Ingredient is acquired last" (it isn't — Surplus/Purchased state follows it on the Order-side
path). Every workflow that touches a given Ingredient acquires that Ingredient's lock in exactly one
deterministic, globally-sorted Ingredient phase, and that phase always occurs after every Product and
Recipe lock the same workflow takes, and always before any Surplus/Purchased-inventory lock it takes.
Because every competing recalculation — whichever Product, whichever workflow — follows this same
relative ordering, and no workflow ever acquires Ingredient (or any other shared resource) in a
reversed relative order against another, two Products' recalculations contending for one shared
Ingredient serialize safely on that Ingredient's lock rather than creating a lock-order reversal; a
deadlock cycle requires two transactions each waiting on a resource the other already holds, in
opposing acquisition order, which this fixed relative ordering structurally forecloses.

### ADR-125 — Recipe Revision Pinning and Impact-Choice Protocol

The Recipe Revision an `OrderLine`'s demand is calculated against is never stored on the line itself
— `OrderLine` has no `recipe_revision_id` column. The pin exists only through
`ProductionRequirementOrder.order_line_id → ProductionRequirement.recipe_revision_id`: a line kept
across a recalculation reuses whatever revision its existing link already points to; a line whose
Product identity just changed, or that has no existing link at all, resolves fresh to whatever
revision is currently `is_current` at that moment. A new Recipe Revision (or a Product's very first
Recipe, when confirmed `INCOMPLETE_RECIPE` demand already exists for it) therefore never silently
migrates or silently preserves existing confirmed demand — the caller must submit an explicit
`apply_scope`. `future_only` leaves every existing confirmed link exactly as it was, on its prior
pin; `apply_existing` migrates every eligible, *unstarted* confirmed link for that Product to the new
revision atomically, including demand that was previously pinned to an older historical revision and
demand that was previously `INCOMPLETE_RECIPE`. Demand covered by an active `IN_PRODUCTION` run
(ADR-126) is never eligible for migration under either scope — it stays frozen on whatever it already
had. When eligible affected demand exists and no `apply_scope` was submitted, the request is rejected
with a structured `RECIPE_REVISION_IMPACT_REQUIRED` response and zero mutation (not even the new
revision row is inserted) — there is no silently-applied default in either direction.

### ADR-126 — Active-Run Protection and the Fixed/Rebuildable Split Across Surplus Allocation and Cross-Product Ingredient Shortage

Phase 7 only ever *reads* whether a `ProductionRequirement` is currently referenced by an
`IN_PRODUCTION` `ProductionRun` (via the existing `ProductionRun.source_production_requirement_id`
FK) — it never creates or mutates a `ProductionRun` row itself (that remains Phase 8's Start/Finish
behavior). A `ProductionRequirement` found to be so referenced is entirely protected: it is never
recomputed, re-linked, or deleted by a recalculation pass, and no code path may alter, cancel, or
migrate the operational demand it represents. This is conservative and whole-line, not
partial-quantity: since the frozen schema assigns no per-Order output at Start, every `OrderLine`
linked to a protected requirement is treated as production-locked in full. Cancel is blocked for the
entire Order if *any* of its demand is protected (cancellation is all-or-nothing per Order); a
demand-affecting edit or Recipe migration is instead rejected at the level of the specific protected
line/requirement it would touch, leaving an unrelated, unprotected line on the same Order fully
editable; and non-production metadata (pricing, notes) remains editable regardless, since it never
touches protected operational demand at all. The frontend's read-time `production_locked` flag is
purely advisory UI gating — every write path independently and authoritatively re-checks this rule
under its own lock, regardless of what that flag reported.

This same fixed/rebuildable principle governs two further resources a recalculation pass touches.
For Surplus, lot priority is FEFO-style — earliest `usable_through_date` first, undated lots last,
then oldest `produced_at`, then a stable id tie-break — and a lot's eligibility for a given demand
item additionally requires `usable_through_date IS NULL OR usable_through_date >= max(demand_date,
business_today)`, so a lot that has already expired as of the Business's local today is never
resurrected merely because an overdue Order's own demand date once fell before that expiry.
Allocations belonging to protected, `IN_PRODUCTION`-covered demand are fixed on both sides of the
match — excluded from what the pure allocator may reassign, on the lot's remaining-quantity side and
on the demand line's remaining-need side alike — while every other (rebuildable) allocation is
deleted and freshly recomputed each pass; a `ProductionRequirement`'s persisted
`surplus_allocated_quantity` is always the sum of its fixed and freshly-rebuilt portions together. For
a shared Ingredient, the authoritative shortage read is `external/fixed reserved` (every active
`IngredientReservation` for that Ingredient *not* belonging to this pass's own about-to-be-rebuilt
rows) `+ fresh candidate reservations across every affected Product's own closure in this same pass`,
compared against physical stock; a stale, about-to-be-superseded reservation from the closure
currently being rebuilt is excluded from the "external" side specifically so it is never counted
twice — once as stale state, once as fresh candidate state. Preview and a real Confirm/Edit read this
identical formula, never two independently-invented ones.

A direct consequence of treating a protected requirement as untouchable: the database's own partial
unique index on `(business_id, product_id, recipe_revision_id, demand_date)` allows only one
`ProductionRequirement` row per key, and a protected row occupying that key can never be deleted,
recreated, or rewritten to make room for a competing mutable one — not even temporarily lifting its
active-run protection to satisfy the constraint is permitted. When new or rebuildable demand would
require a conflicting requirement at a key an `IN_PRODUCTION`-protected row already occupies, the
operation is rejected outright with the structured `PRODUCTION_LOCKED_DEMAND_DATE_CONFLICT` (409)
conflict, with zero mutation. The durable principle this encodes, independent of that specific error
name, is that a protected operational row's identity must never be destroyed or rewritten merely to
work around the schema's own uniqueness projection key.

### ADR-127 — Draft/Confirmed Operational Preview Semantics

`POST /orders/preview` and `POST /orders/{id}/preview` are zero-write end to end — every lock either
route acquires is released by an unconditional rollback, success or failure alike, and reference
resolution during Preview uses the identical Phase 6 line-resolution rules a real save uses but
without `FOR UPDATE`, since a computation that is guaranteed to be discarded has no reason to hold a
write lock. Two distinct composition rules apply depending on what's being previewed: a brand-new or
`DRAFT` Order's preview is the current confirmed world *plus* the hypothetical proposed payload as
additional demand; a `CONFIRMED` Order's preview is the current confirmed world *minus that Order's
own already-persisted contribution* plus the proposed replacement — the exclusion is what prevents
the Order being edited from being counted twice. A retained protected line's operational contribution
is always left as fixed state in both cases, never re-added as hypothetical demand on top of its own
already-protected requirement. A `DRAFT` with no fulfillment date yet never fabricates "today" as a
stand-in demand date — no dated operational result (production requirements, reservations, dated
Custom Item workload) is computed at all until a real date is supplied; only the financial subtotal
remains available. Every operational number Preview returns comes from the exact same backend
calculators and the exact same `recalculate_product_closure` core a real Confirm/Edit uses — never a
second, independently-invented calculation in React — and the candidate Ingredient quantities it
exposes are the identical, already-quantized six-decimal values a real commit would actually persist,
which is what makes Preview and Commit numerically identical for the same input rather than merely
similar.

### ADR-128 — Warning Acknowledgment Protocol

Every operational warning (`INGREDIENT_SHORTAGE`, `PURCHASED_PRODUCT_SHORTAGE`,
`PRODUCT_MISSING_RECIPE`, and `MISSING_FULFILLMENT_TIME`) carries a stable, server-constructed
fingerprint built entirely from authoritative identifiers and quantities (e.g.
`f"INGREDIENT_SHORTAGE:{ingredient_id}:{shortage_quantity}"`) — never from, or coupled to, that
warning's own human-readable `message` text, which two structurally separate functions
(`_collect_warning_fingerprints`/`_warning_issues`) each derive independently from the same
underlying result data. A client echoes back `acknowledged_warning_fingerprints` on
Confirm/confirmed-edit; the server always recomputes the authoritative warning set itself, fresh,
under its own locks, at the moment of the real write, and accepts the request only if every
freshly-computed fingerprint is already a member of the acknowledged set. A brand-new or materially
worsened warning (a fingerprint the caller couldn't have known about) always forces re-review; a
warning set that has gotten strictly smaller or improved since the client last saw it is permitted to
proceed without demanding a redundant re-acknowledgment of warnings that no longer apply. Because
identity lives entirely in the fingerprint, the wording of a warning's `message` may be revised at any
time (as the Manual Acceptance UX pass did, to name the specific Ingredient/unit) without touching
acknowledgment semantics or invalidating any previously-issued fingerprint.

### ADR-129 — Business-Local Time and DST-Safe Fulfillment Validation

`fulfillment_date`/`fulfillment_time` are interpreted as wall-clock values in the Business's own
timezone (`business.timezone`), never UTC or server-local time; `business_today` is derived once,
uniformly, as `datetime.now(UTC).astimezone(ZoneInfo(business.timezone)).date()`. A supplied
date+time combination is validated deterministically for the two ways a local wall-clock moment can
fail to correspond to a real instant: a spring-forward gap (detected via a UTC round-trip that
doesn't return the original wall-clock value) or a fall-back overlap (detected via comparing the
`fold=0`/`fold=1` interpretations), and either is rejected with a structured
`FULFILLMENT_LOCAL_TIME_INVALID` error rather than silently resolved by picking one interpretation.
This wall-clock validity check is deliberately independent of whether a Recipe's elapsed production
duration is known at all: a valid, unambiguous fulfillment time combined with an unknown elapsed
duration is not an error — it simply yields `suggested_start_at = NULL`, since no exact start moment
can be computed without a duration to subtract. When several demand lines on the same date each
contribute their own fulfillment time to one `ProductionRequirement`'s suggested-start computation,
every supplied (non-`None`) contributing time is individually validated for DST nonexistence/ambiguity
*before* the earliest one is selected — a missing time on one contributing line must never mask a
genuinely invalid supplied time on another, and an invalid non-earliest contributor must never hide
silently behind the `min()` selection either.

### ADR-130 — Shopping List Phase 7 Boundary: Cumulative-Horizon Ingredient Derivation

The Phase 7 Shopping List is a read-only, backend-only derivation — no route or UI exists for it yet
— and Ingredient-only: Purchased Product shortages are deliberately not folded into it, remaining
surfaced only as their own operational shortage warnings, since the spec's Shopping List language is
phrased entirely around ingredient shopping. It is computed at read time directly from
`Ingredient.physical_quantity` and the authoritative, currently-active `IngredientReservation` rows —
never from a persisted "shortage" value, since neither table stores one. For each Ingredient and each
display date `D` within the horizon (defaulting to `Business.shopping_horizon_days`), the shortage is
`max(0, cumulative_reserved_through_D - physical_quantity)`, where `cumulative_reserved_through_D`
sums every active reservation with `demand_date <= D` — across every Product sharing that Ingredient,
not only the one the horizon's own display is scoped to. This means an overdue active reservation
(`demand_date` before today) always participates in every in-horizon date's cumulative figure, since
it is `<= D` for every `D` in the horizon, while a reservation dated *beyond* the requested horizon's
own end is never summed into an earlier date's result — later demand cannot inflate what an earlier
display date reports.

### ADR-131 — Planned Production Cost Basis and Snapshot Semantics

An Ingredient's planned unit cost for a `ProductionRequirement` resolves through
`resolve_planned_ingredient_unit_cost`: when that Ingredient's `physical_quantity > 0`, its own
Weighted Average Unit Cost is used and is authoritative in that branch even when the Weighted
Average Unit Cost itself is legitimately `0` — a zero physical quantity does *not* belong to this
branch at all and instead falls to the fallback below. When `physical_quantity <= 0`, the basis
falls back to ADR-112's existing `resolve_effective_replacement_cost` chain (`replacement_unit_cost` override, else
`latest_purchase_unit_cost`) — the exact shared helper ADR-112 already designated for any future
planned-cost consumer, reused rather than re-derived. If that fallback is also unknown (`None`), the
basis for that specific Ingredient is unknown: its own
`ProductionIngredientRequirement.estimated_unit_cost`/`estimated_total_cost` stay `NULL`, and — since
a partial sum that silently dropped an unknown component would understate the true total — the
aggregate `ProductionRequirement.estimated_ingredient_cost`/`estimated_direct_production_cost` are
both left `NULL` as a whole rather than summing only the ingredients with a known cost.
`estimated_direct_production_cost` is deliberately narrow: Ingredient cost plus labor cost only, never
folding in Packaging, Purchased Product cost, Custom Item direct cost, or a Surplus lot's own
unit-cost basis, each of which is either already snapshotted elsewhere (Phase 6) or explicitly
out of scope for this field. `Order.estimated_direct_cost`/`estimated_contribution`/
`estimated_contribution_margin` remain `NULL` throughout Phase 7 — the spec gives no authoritative
Order-level rollup formula, so none is invented.

Every planned-cost field on `ProductionRequirement` is a snapshot as of its most recent operational
recalculation *event* (a confirmation, edit, cancellation, or migration touching that Product) — it is
never eagerly refreshed merely because an Ingredient's own WAC or replacement cost changed
independently. Concretely, no Ingredient-inventory mutation (Restock, Manual Adjustment, Replacement
Cost) ever calls into Product-scoped recalculation from inside its own Ingredient-locked transaction;
doing so would require acquiring a Product lock from inside an already-held Ingredient lock, inverting
the fixed `Product → ... → Ingredient` relative order ADR-124 depends on, and risking a genuine
deadlock against every other Phase 7 workflow that locks Product before Ingredient. A later real
operational recalculation naturally picks up whatever cost inputs are current at that moment; between
events, the planned-cost figures are expected to go stale, consistent with the spec's own three-tier
cost terminology distinguishing "Planned" from "Actual Historical" cost.

## Phase 7 Completion Status

Phase 7 Operational Recalculation is implemented: real Confirm/Cancel persistence, demand-affecting
`CONFIRMED` edits, Recipe Revision/first-Recipe impact-choice migration, Draft/Confirmed Operational
Preview, Ingredient/Purchased-Product reservation reconciliation, active-`IN_PRODUCTION`-run
protection, planned costing, and backend Shopping List derivation, per ADR-123 through ADR-131 above.
Independent implementation review passed the final bundle, and manual acceptance — including the
follow-up UX correction pass (Ingredient name/unit identification, corrected shortage-warning copy,
human-readable quantity formatting, and corrected Custom-Item/Purchased-only empty-state display) —
is complete.

Final verified automated baseline: backend **656 passing**, frontend **211 passing**; `ruff check`
and `ruff format --check` clean; `alembic check` reports no schema drift; TypeScript (`tsc --noEmit`)
clean; ESLint reports zero errors; the frontend production build succeeds. No database migration was
required or performed at any point in Phase 7 — the pre-existing Phase 1 schema was already sufficient
for every Phase 7 table (§3 of the Phase 7 planning record). No dependency was added or changed.

Phase 7 deliberately does not: create, start, or finish a `ProductionRun`; physically consume
Ingredient inventory; physically consume Purchased Product inventory; create physical Surplus
inventory; write a `SurplusTransaction`; write a historical `OrderCostAllocation`; implement
`READY`/`COMPLETED` order lifecycle behavior; implement a Shopping List UI; or implement any
Dashboard/Analytics feature. All of the above remain Phase 8+ scope.

Frozen Phase 6 parent baseline: `210318d57d77554ccdde0f1fe682a10841fbca70`. Phase 7 has not yet been
committed — no Phase 7 commit SHA is recorded here; it will be added once Phase 7 is committed and its
own CI/freeze checkpoint is reached.
