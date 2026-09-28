"""Database engine and session management."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def configure(database_url: str) -> None:
    global _engine, _session_factory
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    _engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)


def init_db() -> None:
    from app import models  # noqa: F401  (registers tables)

    Base.metadata.create_all(get_engine())


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not configured; call app.db.configure() first")
    return _engine


def new_session() -> Session:
    if _session_factory is None:
        raise RuntimeError("Database not configured; call app.db.configure() first")
    return _session_factory()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session per request."""
    session = new_session()
    try:
        yield session
    finally:
        session.close()
