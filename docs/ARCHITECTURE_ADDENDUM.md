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
