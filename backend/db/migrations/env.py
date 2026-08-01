from logging.config import fileConfig
import os
from alembic import context
from sqlalchemy import engine_from_config, pool
from backend.config import settings
from backend.db.base import Base
from backend.db.session import prepare_sqlite_parent, validate_database_mode

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
database_url = os.getenv("SMARTAI_DATABASE_URL", settings.database_url)
validate_database_mode(database_url)
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata

def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    prepare_sqlite_parent(database_url)
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
