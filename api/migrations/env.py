"""Alembic environment: URL from IFLOW_API_DATABASE_URL / _SECRET (or the -x url=... override used by tests)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from iflow_api.db import Base
from iflow_api.settings import Settings

config = context.config
url = context.get_x_argument(as_dictionary=True).get("url") or Settings.from_env().database_url
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))   # configparser: URL-escaped passwords
target_metadata = Base.metadata


def run_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.",
                                poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
