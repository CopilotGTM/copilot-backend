"""073 add meeting recording video artifact columns

Revision ID: 3c7f79637726
Revises: 3c7f79637727
Create Date: 2026-07-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637726"
down_revision: str | None = "3c7f79637727"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    table_name = "meeting_recording_sessions"
    _add_column_if_missing(table_name, sa.Column("video_blob_path", sa.Text(), nullable=True))
    _add_column_if_missing(table_name, sa.Column("video_blob_url", sa.Text(), nullable=True))
    _add_column_if_missing(
        table_name,
        sa.Column("video_content_type", sa.String(length=128), nullable=True),
    )
    _add_column_if_missing(table_name, sa.Column("video_size_bytes", sa.BigInteger(), nullable=True))
    _add_column_if_missing(
        table_name,
        sa.Column("video_recording_id", sa.String(length=255), nullable=True),
    )
    _add_column_if_missing(
        table_name,
        sa.Column("video_artifact_id", sa.String(length=255), nullable=True),
    )
    _add_column_if_missing(
        table_name,
        sa.Column("video_archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    table_name = "meeting_recording_sessions"
    _drop_column_if_exists(table_name, "video_archived_at")
    _drop_column_if_exists(table_name, "video_artifact_id")
    _drop_column_if_exists(table_name, "video_recording_id")
    _drop_column_if_exists(table_name, "video_size_bytes")
    _drop_column_if_exists(table_name, "video_content_type")
    _drop_column_if_exists(table_name, "video_blob_url")
    _drop_column_if_exists(table_name, "video_blob_path")
