"""SQLAlchemy engine and session factory. Application reads and writes go through the ORM."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import settings


def _sqlalchemy_url(url: str) -> str:
    """SQLAlchemy must be told which driver to use; this project uses psycopg 3."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


engine = create_engine(_sqlalchemy_url(settings.database_url()), pool_pre_ping=True, pool_size=5,
                       connect_args={"options": "-c timezone=UTC"})
SessionLocal = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any error."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
