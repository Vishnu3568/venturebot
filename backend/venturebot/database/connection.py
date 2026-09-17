"""Database engine and session management for VentureBot."""

from __future__ import annotations

from typing import Generator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from venturebot.database.models import Base

DEFAULT_DB_URL = "sqlite:///venturebot.db"


def get_engine(db_url: str = DEFAULT_DB_URL, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine.
    
    If SQLite in-memory database is specified, uses StaticPool so that
    multiple connections/sessions share the same in-memory database.
    """
    connect_args = {}
    engine_kwargs = {"echo": echo}

    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if ":memory:" in db_url:
            engine_kwargs["poolclass"] = StaticPool

    engine_kwargs["connect_args"] = connect_args
    return create_engine(db_url, **engine_kwargs)


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a configured sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Initialize the database schema by creating all tables."""
    Base.metadata.create_all(bind=engine)
