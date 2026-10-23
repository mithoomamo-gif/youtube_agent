"""Alembic environment.

The schema's single source of truth is ``app.schema_ddl`` — migrations
execute those statements verbatim instead of duplicating DDL.

The database URL is resolved at runtime, in priority order:
1. ``YTA_DB_URL`` env var (full SQLAlchemy URL),
2. ``YTA_DATA_DIR`` env var -> ``<dir>/app.db``,
3. ``app.db.default_db_path()`` (app.config settings / platform app-data).
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the backend package importable regardless of the caller's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Imported so migrations share the one DDL definition; env itself does not
# autogenerate (target_metadata stays None — this project uses raw DDL).
from app import schema_ddl  # noqa: E402,F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    url = os.environ.get("YTA_DB_URL")
    if url:
        return url
    data_dir = os.environ.get("YTA_DATA_DIR")
    if data_dir:
        return f"sqlite:///{Path(data_dir) / 'app.db'}"
    try:
        from app.db import default_db_path

        return f"sqlite:///{default_db_path()}"
    except Exception:
        return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
