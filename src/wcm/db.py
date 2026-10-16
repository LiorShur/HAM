from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wcm.config import get_settings


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    kwargs = (
        # timeout: wait up to 30s for another process's write to finish instead of
        # failing with "database is locked".
        {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if url.startswith("sqlite")
        else {}
    )
    return create_engine(url, future=True, **kwargs)


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    session = session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# The first migration. Databases created before migrations were used (with the old
# `wcm init-db`, which created tables directly) match it exactly.
INITIAL_REVISION = "d11350e4f612"


def migrate(url: str | None = None) -> str:
    """Bring the database up to date, creating it if needed. Returns what happened."""
    from alembic.config import Config
    from sqlalchemy import inspect

    from alembic import command

    url = url or get_settings().database_url
    cfg = Config()
    cfg.set_main_option("script_location", get_settings().wcm_alembic_dir)
    cfg.set_main_option("sqlalchemy.url", url)

    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    if "articles" in tables and "alembic_version" not in tables:
        command.stamp(cfg, INITIAL_REVISION)
        note = "Existing database recorded at the first schema version; "
    elif "articles" in tables:
        note = "Existing database; "
    else:
        note = "New database; "
    command.upgrade(cfg, "head")
    return note + "schema is up to date."
