from logging.config import fileConfig
import os
import urllib.parse
from typing import Any

from sqlalchemy import engine_from_config, pool, text, MetaData
from alembic import context
from dotenv import load_dotenv

from src.domain.models.organisation_tables import (
    ChatSession,
    ChatMessage,
    OrganisationDocument,
    Attachment,
    OrganisationSource,
    KnowledgeIntegration,
    IntegrationSyncHistory,
    ChatMessageJob,
    KnowledgeProcessingJob,
    ChatMessageAttachmentLink,
    CalendarEvent,
    CalendarAttendee,
    CRMPipeline,
    CRMDealStage,
    CRMDeal,
    CRMDealParticipant,
    CRMDealStageHistory,
    CRMDealTask,
    CRMContact,
    CRMUser,
    CRMCompany,
)
from src.domain.models.organisation_tables.sync.email_models import (
    EmailFolder,
    Email,
    EmailThreadSummary,
)
from src.domain.models.organisation_tables.sync.messaging_models import (
    MessagingChannel,
    MessagingChannelMember,
    MessagingDealLink,
    MessagingMessage,
    MessagingThreadSummary,
)
from src.domain.models.organisation_tables.sync.participant_models import (
    IntegrationParticipant,
    IntegrationParticipantIdentity,
)

load_dotenv()

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

# Create org-specific metadata by importing only org models
# This ensures we only track org-specific tables

# Create a fresh metadata instance
org_metadata = MetaData()


# Manually register only the org-specific tables
# Manually register all the org-specific tables
all_org_models: list[type[Any]] = [
    ChatSession,
    ChatMessage,
    OrganisationDocument,
    Attachment,
    OrganisationSource,
    KnowledgeIntegration,
    IntegrationSyncHistory,
    ChatMessageJob,
    KnowledgeProcessingJob,
    ChatMessageAttachmentLink,
    CalendarEvent,
    CalendarAttendee,
    CRMPipeline,
    CRMDealStage,
    CRMDeal,
    CRMDealParticipant,
    CRMDealStageHistory,
    CRMDealTask,
    CRMContact,
    CRMUser,
    CRMCompany,
    # Email tables
    EmailFolder,
    Email,
    EmailThreadSummary,
    # Messaging tables
    MessagingChannel,
    MessagingChannelMember,
    MessagingDealLink,
    MessagingMessage,
    MessagingThreadSummary,
    # Participant tables (shared across integrations)
    IntegrationParticipant,
    IntegrationParticipantIdentity,
]

for model in all_org_models:
    # At runtime, SQLModel classes expose a SQLAlchemy "metadata" attribute.
    # We avoid static type complaints by not asserting the attribute at type level.
    try:
        setattr(model, "metadata", org_metadata)
    except Exception:
        # Best-effort only; we'll still copy tables into target metadata below.
        pass

# Recreate the tables in the new metadata
# Recreate the tables in the new metadata
for model in all_org_models:
    # Mypy/Pylance may not see __table__ on SQLModel metaclass, so use getattr safely.
    table = getattr(model, "__table__", None)
    if table is not None:
        table.tometadata(org_metadata)

target_metadata = org_metadata

# grab the --x schema=org_id argument (if any)
x_args = context.get_x_argument(as_dictionary=True)
target_schema = x_args.get("schema")  # e.g. "org_123"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no DB connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_schemas=False,  # Only focus on target schema
        version_table_schema=target_schema,
        # Only compare tables that exist in our target metadata
        include_object=lambda obj, name, type_, reflected, compare_to: (
            type_ != "table" or name in target_metadata.tables
        ),
        transactional_ddl=False,  # Disable transactions for extension creation
    )

    # emit a CREATE SCHEMA if it doesn't exist
    if target_schema:
        context.execute(f"CREATE SCHEMA IF NOT EXISTS {target_schema}")

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (with DB connection)."""
    configuration = config.get_section(config.config_ini_section)
    if configuration is None:
        configuration = {}
    
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Use the connection's transaction context manager.
        # This will automatically commit on success or rollback on error.
        with connection.begin():
            if target_schema:
                # Set search_path inside the transaction.
                connection.execute(text(f"SET search_path TO {target_schema}, public"))

            # Configure context within the transaction block.
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                include_schemas=False,
                version_table_schema=target_schema,
                include_object=lambda obj, name, type_, reflected, compare_to: (
                    type_ != "table" or name in target_metadata.tables
                ),
            )
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
