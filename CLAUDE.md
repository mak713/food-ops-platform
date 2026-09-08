# Claude Code Project Instructions

## Project Source of Truth

The authoritative requirements and architecture specification for this project is:

`docs/ARCHITECTURE_SPEC.md`

This specification is authoritative. Before planning or implementing a phase, read the portions of the specification relevant to that work.

Do not silently redesign, simplify, reinterpret, add, or remove requirements, database entities, business rules, lifecycle behavior, security boundaries, tenant behavior, financial semantics, or major technology choices defined by the specification.

If implementation reveals a conflict, ambiguity, missing requirement, or technical constraint that would require changing the specified architecture:

1. Stop work on the affected decision.
2. Identify the issue clearly.
3. Reference the relevant specification requirement or architecture decision.
4. Explain the smallest proposed resolution.
5. Wait for explicit approval before implementing a specification-changing solution.

Framework-specific implementation details may be chosen where the specification intentionally leaves them open.

## Implementation Decision Addendum

Implementation-level decisions made after the architecture freeze — filling gaps `docs/ARCHITECTURE_SPEC.md` explicitly left open, not redefining product/architecture requirements — are recorded in `docs/ARCHITECTURE_ADDENDUM.md`. Treat it as authoritative for the decisions it documents, with the same "do not silently deviate" discipline that applies to the main spec. If the two ever conflict, `docs/ARCHITECTURE_SPEC.md` governs.

## Development Workflow

Development follows the implementation phases defined in the specification.

Work only on the currently authorized phase.

Do not begin later phases simply because their implementation appears convenient or related.

For substantial or architecturally significant phases:

1. Inspect the existing repository.
2. Read the relevant specification sections.
3. Produce an implementation plan before modifying files when requested.
4. Identify assumptions, conflicts, or ambiguities before implementation.
5. Implement only the approved/current phase.
6. Add and run the applicable tests.
7. Run applicable linting, formatting, type-checking, migration, and build checks.
8. Report what changed and the results of verification.
9. Explain the important implementation decisions and code so the project owner can understand the system.
10. Stop at any review checkpoint defined by the specification.

A checkpoint is a hard stop. Do not begin the next phase until explicitly authorized.

## Architecture Rules

Maintain the modular-monolith architecture defined by the specification.

Backend business and financial calculations are authoritative. Do not create competing authoritative calculation logic in the frontend.

Keep deterministic domain calculations separate from database/application orchestration where specified.

Controllers/routes should remain thin. Business workflows belong in services/domain logic rather than route handlers.

Operational projection state and immutable historical/event state must remain conceptually distinct.

Do not introduce microservices, event buses, generic repository frameworks, generalized RBAC, speculative abstractions, or future-feature infrastructure unless explicitly required by the active specification.

Do not implement features listed only under Future Extension Notes.

## Database Rules

The logical/domain database schema in `docs/ARCHITECTURE_SPEC.md` is authoritative.

Do not independently normalize, denormalize, merge, split, rename semantically significant fields, add domain entities, remove entities, or alter relationships without approval.

Before the initial domain migration, identify any conflict between the specified logical schema and PostgreSQL/SQLAlchemy and propose the smallest necessary adjustment.

Tenant isolation, historical integrity, financial precision, inventory integrity, reservation behavior, and lifecycle constraints are correctness requirements, not optional implementation details.

Use PostgreSQL for integration behavior. Do not silently substitute SQLite for behavior whose correctness depends on PostgreSQL.

## Security and Tenant Isolation

Never trust a client-supplied `business_id` as authorization.

Derive tenant context from the authenticated server-side session.

Every tenant-owned read and write must be scoped to the authenticated Business.

Cross-tenant resource references must be rejected.

Foreign-tenant resources should produce the non-disclosing behavior required by the specification.

Tenant-isolation failures are release-blocking defects.

Never weaken security behavior merely to make a test pass.

## Data and Financial Correctness

Use Decimal/fixed-precision arithmetic for money and precision-sensitive quantities as specified. Do not use binary floating-point arithmetic for authoritative monetary calculations.

Preserve historical snapshots and immutable historical cost/revenue behavior.

Do not silently recalculate historical results using current prices, recipes, labor rates, or ingredient costs.

Do not silently guess missing business data.

## Testing

Tests are part of implementation, not optional follow-up work.

Prioritize:

- deterministic domain calculations;
- tenant isolation;
- lifecycle transitions;
- inventory/reservation integrity;
- financial correctness;
- transaction rollback;
- historical immutability;
- concurrency-sensitive operations;
- API contracts;
- critical end-to-end workflows.

A bug fix should receive a regression test when practical.

Do not weaken, delete, or rewrite a valid test merely to make the suite pass. If a test conflicts with the authoritative specification, report the conflict.

## Repository and Scope Discipline

Do not modify unrelated files without a concrete reason.

Do not perform large opportunistic refactors while implementing an unrelated requirement.

Do not add dependencies merely for convenience when the existing stack can reasonably solve the problem.

Do not commit secrets, credentials, `.env` files, generated dependency directories, build artifacts, or local development data.

Keep changes reviewable and explain consequential modifications.

## Communication

The project owner is learning the implementation while building this project.

When completing meaningful work, explain:

- what was built;
- why the implementation is structured that way;
- important framework or architectural concepts involved;
- how data flows through the relevant code;
- how the implementation was tested;
- any assumptions, limitations, or deviations.

Assume the project owner understands programming, Python, SQL, object-oriented programming, and general computer-science concepts, but may be new to FastAPI, SQLAlchemy, Alembic, React architecture, and some production software-engineering practices.

Do not bury important architectural decisions inside implementation output.

When uncertain about a consequential interpretation of the specification, ask rather than guess.