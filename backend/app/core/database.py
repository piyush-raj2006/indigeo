from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _build_engine() -> Engine:
    settings = get_settings()
    url = settings.database_url
    kwargs: dict[str, object] = {"echo": settings.sql_echo, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver callback
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def init_schema() -> None:
    from app import models  # noqa: F401  - ensure mappers are registered

    engine = get_engine()
    Base.metadata.create_all(engine)
    _upgrade_hazard_analyses(engine)


def _upgrade_hazard_analyses(engine: Engine) -> None:
    """Additive upgrade for relational stores created before drawn-area analyses.

    This build has no migration runner, and ``create_all`` does not alter an existing table.
    Rows written before the columns existed are point analyses, so they are labelled as such
    rather than left null; nothing is rewritten or dropped.
    """
    from sqlalchemy import inspect, text

    existing = {column["name"] for column in inspect(engine).get_columns("hazard_analyses")}
    additions = {
        "geometry_kind": "VARCHAR(20) NOT NULL DEFAULT 'point'",
        "wkt": "TEXT",
    }
    missing = {name: ddl for name, ddl in additions.items() if name not in existing}
    if not missing:
        return
    with engine.begin() as connection:
        for name, ddl in missing.items():
            connection.execute(text(f"ALTER TABLE hazard_analyses ADD COLUMN {name} {ddl}"))
