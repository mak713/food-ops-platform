# Food Operations Platform

Work in progress. This is a multi-tenant operations platform for small food businesses —
see [`docs/ARCHITECTURE_SPEC.md`](docs/ARCHITECTURE_SPEC.md) for the authoritative product,
architecture, and data-model specification.

A full setup/usage README will be written once product behavior is stable (Spec §18.10). For now:

- `backend/` — FastAPI + SQLAlchemy + PostgreSQL, managed with `uv`.
- `frontend/` — React + TypeScript + Vite.
- `docker-compose.yml` — local PostgreSQL for development.

See `docs/ARCHITECTURE_SPEC.md` §18 for the full technical stack and local setup requirements.
