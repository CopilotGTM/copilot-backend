from logging.config import fileConfig
import os
import urllib.parse

from sqlalchemy import engine_from_config, pool, text
from alembic import context
from sqlmodel import SQLModel
from dotenv import load_dotenv

load_dotenv()

# ---- import every module that declares SQLModel models ----
# (so that SQLModel.metadata is populated)
from src.domain.models.master_tables import (
    Organisation,
    User,
    SuperAdmin,
    OrganisationActionHistory,
    UserActionHistory,
)

# (and import any new model modules here)

# this is the Alembic Config object
config = context.config

# set the DB URL from env vars
DATABASE_HOST = os.getenv("DATABASE_HOST", "localhost")
DATABASE_PORT = os.getenv("DATABASE_PORT", "5432")
DATABASE_NAME = os.getenv("DATABASE_NAME", "postgres")
DATABASE_USER = os.getenv("DATABASE_USER", "postgres")
DATABASE_PASSWORD = os.getenv("DATABASE_PASSWORD", "")
encoded_password = urllib.parse.quote_plus(DATABASE_PASSWORD)
db_url = (
    f"postgresql://{DATABASE_USER}:{encoded_password}"
    f"@{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_NAME}"
)
# Alembic requires % to be escaped as %%
config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))

# set up Python-side logging per alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Create master-specific metadata by importing only master models
# This ensures we only track master-specific tables
from sqlalchemy import MetaData

# Create a fresh metadata instance
master_metadata = MetaData()

# Manually register only the master-specific tables
Organisation.metadata = master_metadata
User.metadata = master_metadata
SuperAdmin.metadata = master_metadata
OrganisationActionHistory.metadata = master_metadata
UserActionHistory.metadata = master_metadata
# Recreate the tables in the new metadata
Organisation.__table__.tometadata(master_metadata)
User.__table__.tometadata(master_metadata)
SuperAdmin.__table__.tometadata(master_metadata)
OrganisationActionHistory.__table__.tometadata(master_metadata)
UserActionHistory.__table__.tometadata(master_metadata)

target_metadata = master_metadata

# grab the --x schema=org_id argument (if any)
x_args = context.get_x_argument(as_dictionary=True)
target_schema = x_args.get("schema")  # e.g. "org_123"


def include_object(object, name, type_, reflected, compare_to):
    """
    Filter objects for autogenerate.
    For master migrations (target_schema is None), only include tables
    from the 'public' schema that are part of SQLModel.metadata.
    """
    if type_ == "table":
        # 'compare_to' is the schema name when type_ is 'table'
        schema = compare_to
        if target_schema:
            # If a target_schema is specified, only include tables from that schema
            return schema == target_schema
        else:
            # If no target_schema (master migration), only include tables
            # that are defined in our target_metadata (master_db_models).
            # The schema check is implicitly handled by target_metadata containing only public tables.
            return name in target_metadata.tables
    return True  # Include other object types (e.g., sequences, indexes)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no DB connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_schemas=False,  # Explicitly set to False
        include_object=include_object,  # Use the custom include_object function
        version_table_schema=target_schema,
    )

    # emit a CREATE SCHEMA if it doesn’t exist
    if target_schema:
        context.execute(f"CREATE SCHEMA IF NOT EXISTS {target_schema}")

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (with DB connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        if target_schema:
            # ensure unqualified DDL goes into the right schema
            connection.execute(text(f"SET search_path TO {target_schema}, public"))

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_schemas=False,  # Explicitly set to False
            include_object=include_object,  # Use the custom include_object function
            version_table_schema=target_schema,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
