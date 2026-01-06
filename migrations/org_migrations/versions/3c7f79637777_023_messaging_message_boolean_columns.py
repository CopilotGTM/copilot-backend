"""023_messaging_message_boolean_columns

Convert is_deleted, is_bot_message, is_important from VARCHAR to BOOLEAN in messaging_messages table.

Revision ID: 3c7f79637777
Revises: 3c7f79637778
Create Date: 2026-01-06

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "3c7f79637777"
down_revision = "3c7f79637778"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Convert VARCHAR boolean columns to proper BOOLEAN type."""
    # Step 1: Add temporary boolean columns
    op.add_column(
        "messaging_messages",
        sa.Column("is_deleted_bool", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "messaging_messages",
        sa.Column("is_bot_message_bool", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "messaging_messages",
        sa.Column("is_important_bool", sa.Boolean(), nullable=True),
    )

    # Step 2: Migrate data - convert string 'true'/'false' to boolean
    op.execute(
        """
        UPDATE messaging_messages
        SET is_deleted_bool = CASE WHEN LOWER(is_deleted) = 'true' THEN true ELSE false END,
            is_bot_message_bool = CASE WHEN LOWER(is_bot_message) = 'true' THEN true ELSE false END,
            is_important_bool = CASE WHEN LOWER(is_important) = 'true' THEN true ELSE false END
        """
    )

    # Step 3: Drop old VARCHAR columns
    op.drop_index("ix_messaging_messages_is_important", table_name="messaging_messages")
    op.drop_column("messaging_messages", "is_deleted")
    op.drop_column("messaging_messages", "is_bot_message")
    op.drop_column("messaging_messages", "is_important")

    # Step 4: Rename temp columns to original names
    op.alter_column(
        "messaging_messages",
        "is_deleted_bool",
        new_column_name="is_deleted",
        nullable=False,
        server_default=sa.text("false"),
    )
    op.alter_column(
        "messaging_messages",
        "is_bot_message_bool",
        new_column_name="is_bot_message",
        nullable=False,
        server_default=sa.text("false"),
    )
    op.alter_column(
        "messaging_messages",
        "is_important_bool",
        new_column_name="is_important",
        nullable=False,
        server_default=sa.text("false"),
    )

    # Step 5: Recreate index on is_important
    op.create_index(
        "ix_messaging_messages_is_important",
        "messaging_messages",
        ["is_important"],
        unique=False,
    )


def downgrade() -> None:
    """Revert BOOLEAN columns back to VARCHAR."""
    # Step 1: Add temporary string columns
    op.add_column(
        "messaging_messages",
        sa.Column("is_deleted_str", sa.String(), nullable=True),
    )
    op.add_column(
        "messaging_messages",
        sa.Column("is_bot_message_str", sa.String(), nullable=True),
    )
    op.add_column(
        "messaging_messages",
        sa.Column("is_important_str", sa.String(), nullable=True),
    )

    # Step 2: Migrate data - convert boolean to string
    op.execute(
        """
        UPDATE messaging_messages
        SET is_deleted_str = CASE WHEN is_deleted THEN 'true' ELSE 'false' END,
            is_bot_message_str = CASE WHEN is_bot_message THEN 'true' ELSE 'false' END,
            is_important_str = CASE WHEN is_important THEN 'true' ELSE 'false' END
        """
    )

    # Step 3: Drop boolean columns
    op.drop_index("ix_messaging_messages_is_important", table_name="messaging_messages")
    op.drop_column("messaging_messages", "is_deleted")
    op.drop_column("messaging_messages", "is_bot_message")
    op.drop_column("messaging_messages", "is_important")

    # Step 4: Rename string columns to original names
    op.alter_column(
        "messaging_messages",
        "is_deleted_str",
        new_column_name="is_deleted",
        nullable=False,
        server_default=sa.text("'false'"),
    )
    op.alter_column(
        "messaging_messages",
        "is_bot_message_str",
        new_column_name="is_bot_message",
        nullable=False,
        server_default=sa.text("'false'"),
    )
    op.alter_column(
        "messaging_messages",
        "is_important_str",
        new_column_name="is_important",
        nullable=False,
        server_default=sa.text("'false'"),
    )

    # Step 5: Recreate index on is_important
    op.create_index(
        "ix_messaging_messages_is_important",
        "messaging_messages",
        ["is_important"],
        unique=False,
    )
