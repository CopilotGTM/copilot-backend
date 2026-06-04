"""055 provider capability sync states.

Revision ID: 3c7f79637744
Revises: 3c7f79637745
Create Date: 2026-06-03 08:03:00.000000
"""

from __future__ import annotations

import json
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "3c7f79637744"
down_revision = "3c7f79637745"
branch_labels = None
depends_on = None


CAPABILITY_PROVIDER = {
    "gmail": "google",
    "google_calendar": "google",
    "outlook_email": "microsoft",
    "outlook_calendar": "microsoft",
    "teams": "microsoft",
}

CURSOR_KEYS = {
    "gmail_history_id",
    "email_folder_delta_links",
    "calendar_delta_link",
    "outlook_delta_link",
}

WEBHOOK_KEYS = {
    "google_webhook_subscriptions",
    "microsoft_graph_subscriptions",
    "microsoft_graph_subscription",
}


def upgrade() -> None:
    op.create_table(
        "integration_sync_states",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sync_state_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("capability", sa.String(length=50), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_sync_status", sa.String(length=20), nullable=True),
        sa.Column("last_sync_error", sa.String(), nullable=True),
        sa.Column(
            "consecutive_sync_failures",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "cursor_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "webhook_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "sync_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["knowledge_integrations.integration_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("sync_state_id"),
    )
    op.create_index(
        op.f("ix_integration_sync_states_created_at"),
        "integration_sync_states",
        ["created_at"],
    )
    op.create_index(
        op.f("ix_integration_sync_states_updated_at"),
        "integration_sync_states",
        ["updated_at"],
    )
    op.create_index(
        "ux_integration_sync_states_integration_capability",
        "integration_sync_states",
        ["integration_id", "capability"],
        unique=True,
    )
    op.create_index(
        "ix_integration_sync_states_org_capability",
        "integration_sync_states",
        ["organisation_id", "capability"],
    )
    op.create_index(
        "ix_integration_sync_states_enabled",
        "integration_sync_states",
        ["enabled"],
    )
    op.create_index(
        op.f("ix_integration_sync_states_organisation_id"),
        "integration_sync_states",
        ["organisation_id"],
    )
    op.create_index(
        op.f("ix_integration_sync_states_integration_id"),
        "integration_sync_states",
        ["integration_id"],
    )

    _migrate_provider_capabilities()


def downgrade() -> None:
    op.drop_index(
        op.f("ix_integration_sync_states_updated_at"),
        table_name="integration_sync_states",
    )
    op.drop_index(
        op.f("ix_integration_sync_states_created_at"),
        table_name="integration_sync_states",
    )
    op.drop_index(
        op.f("ix_integration_sync_states_integration_id"),
        table_name="integration_sync_states",
    )
    op.drop_index(
        op.f("ix_integration_sync_states_organisation_id"),
        table_name="integration_sync_states",
    )
    op.drop_index("ix_integration_sync_states_enabled", table_name="integration_sync_states")
    op.drop_index(
        "ix_integration_sync_states_org_capability",
        table_name="integration_sync_states",
    )
    op.drop_index(
        "ux_integration_sync_states_integration_capability",
        table_name="integration_sync_states",
    )
    op.drop_table("integration_sync_states")


def _migrate_provider_capabilities() -> None:
    bind = op.get_bind()
    rows = (
        bind.execute(
            sa.text(
                """
                SELECT *
                FROM knowledge_integrations
                WHERE integration_type IN :capabilities
                ORDER BY created_by_user_id, integration_email, integration_type, updated_at DESC
                """
            ).bindparams(sa.bindparam("capabilities", expanding=True)),
            {"capabilities": list(CAPABILITY_PROVIDER.keys())},
        )
        .mappings()
        .all()
    )

    parent_by_key: dict[tuple[str, str, str], str] = {}
    for row in rows:
        capability = str(row["integration_type"])
        provider = CAPABILITY_PROVIDER[capability]
        key = (
            str(row["created_by_user_id"]),
            provider,
            str(row["integration_email"] or ""),
        )
        parent_id = parent_by_key.get(key)
        metadata = dict(row["connection_metadata"] or {})
        if parent_id is None:
            parent_id = str(row["integration_id"])
            parent_by_key[key] = parent_id
            parent_metadata = {
                key: value
                for key, value in metadata.items()
                if key not in CURSOR_KEYS and key not in WEBHOOK_KEYS
            }
            bind.execute(
                sa.text(
                    """
                    UPDATE knowledge_integrations
                    SET integration_type = :provider,
                        name = :name,
                        connection_metadata = CAST(:connection_metadata AS jsonb),
                        updated_at = NOW()
                    WHERE integration_id = :integration_id
                    """
                ),
                {
                    "provider": provider,
                    "name": "Google" if provider == "google" else "Microsoft",
                    "integration_id": parent_id,
                    "connection_metadata": json.dumps(parent_metadata),
                },
            )
        else:
            bind.execute(
                sa.text(
                    """
                    UPDATE knowledge_integrations
                    SET is_active = false,
                        auto_sync_enabled = false,
                        last_sync_error = 'Retired after provider capability migration',
                        updated_at = NOW()
                    WHERE integration_id = :integration_id
                    """
                ),
                {"integration_id": str(row["integration_id"])},
            )

        cursor_metadata = {key: metadata[key] for key in CURSOR_KEYS if key in metadata}
        webhook_metadata = {key: metadata[key] for key in WEBHOOK_KEYS if key in metadata}
        bind.execute(
            sa.text(
                """
                INSERT INTO integration_sync_states (
                    sync_state_id,
                    organisation_id,
                    integration_id,
                    capability,
                    enabled,
                    last_synced_at,
                    last_sync_status,
                    last_sync_error,
                    consecutive_sync_failures,
                    cursor_metadata,
                    webhook_metadata,
                    sync_metadata,
                    created_at,
                    updated_at
                )
                VALUES (
                    :sync_state_id,
                    :organisation_id,
                    :integration_id,
                    :capability,
                    :enabled,
                    :last_synced_at,
                    :last_sync_status,
                    :last_sync_error,
                    :consecutive_sync_failures,
                    CAST(:cursor_metadata AS jsonb),
                    CAST(:webhook_metadata AS jsonb),
                    '{}'::jsonb,
                    NOW(),
                    NOW()
                )
                ON CONFLICT (integration_id, capability) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    last_synced_at = EXCLUDED.last_synced_at,
                    last_sync_status = EXCLUDED.last_sync_status,
                    last_sync_error = EXCLUDED.last_sync_error,
                    consecutive_sync_failures = EXCLUDED.consecutive_sync_failures,
                    cursor_metadata = EXCLUDED.cursor_metadata,
                    webhook_metadata = EXCLUDED.webhook_metadata,
                    updated_at = NOW()
                """
            ),
            {
                "organisation_id": str(row["organisation_id"]),
                "sync_state_id": str(uuid4()),
                "integration_id": parent_id,
                "capability": capability,
                "enabled": bool(row["is_active"]),
                "last_synced_at": row["last_synced_at"],
                "last_sync_status": row["last_sync_status"],
                "last_sync_error": row["last_sync_error"],
                "consecutive_sync_failures": row["consecutive_sync_failures"] or 0,
                "cursor_metadata": json.dumps(cursor_metadata),
                "webhook_metadata": json.dumps(webhook_metadata),
            },
        )
