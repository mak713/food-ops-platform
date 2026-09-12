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
