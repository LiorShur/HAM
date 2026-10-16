from logging.config import fileConfig

from sqlalchemy import create_engine

from alembic import context
from wcm.config import get_settings
from wcm.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
# `wcm init-db` passes the URL in; the alembic CLI falls back to DATABASE_URL / .env.
url = config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, render_as_batch=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url)
    with engine.connect() as connection:
        # Batch mode lets SQLite apply ALTER COLUMN changes (by copying the table).
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
