from collections.abc import Generator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

engine: Engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session]:
    """FastAPI dependency yielding a synchronous SQLAlchemy session.

    Declared as a plain generator (not async) — DB-touching endpoints use `def`,
    not `async def`, so FastAPI runs them in its threadpool rather than blocking
    the event loop with synchronous database calls.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
