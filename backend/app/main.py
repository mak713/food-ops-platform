from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import RequestIdMiddleware, configure_logging
from app.db.session import get_db

configure_logging()
settings = get_settings()

app = FastAPI(title="Food Operations Platform API")

app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)


@app.get("/health")
def health(db: Annotated[Session, Depends(get_db)]) -> dict:
    """Liveness + database connectivity check (Spec §11.12).

    Declared `def`, not `async def`: it performs a blocking DB call, so FastAPI
    runs it in its threadpool instead of on the event loop.
    """
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
