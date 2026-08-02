"""Add external authentication and durable organisation signup.

Revision ID: f1a2b3c4d5ee
Revises: f1a2b3c4d5ed
Create Date: 2026-08-01

This unreleased revision owns the complete authentication/signup schema change:
provider profile fields, domain-based organisation identity, and durable signup
milestones. Existing users remain password users through the server default.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f1a2b3c4d5ee"
down_revision = "f1a2b3c4d5ed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add authentication fields, organisation domains, and signup state."""
    op.add_column(
        "users",
        sa.Column("profile_image_url", sa.String(length=1000), nullable=True),
        schema="public",
    )
    op.add_column(
        "users",
        sa.Column(
            "authentication_method",
            sa.String(length=32),
            nullable=False,
            server_default="password",
        ),
        schema="public",
    )
    op.create_check_constraint(
        "ck_users_authentication_method",
        "users",
        "authentication_method IN ('password', 'google_workspace', 'microsoft_entra')",
        schema="public",
    )

    op.add_column(
        "organisations",
        sa.Column("primary_domain", sa.String(length=253), nullable=True),
        schema="public",
    )
    op.add_column(
        "organisations",
        sa.Column("domain_verification_method", sa.String(length=32), nullable=True),
        schema="public",
    )
    op.add_column(
        "organisations",
        sa.Column("domain_verified_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )

    # Existing organisations predate provider verification. The classified first
    # domain becomes canonical, but its verification fields intentionally stay null.
    op.execute(
        """
        UPDATE public.organisations
        SET primary_domain = lower(trim(trailing '.' FROM domain ->> 0))
        WHERE json_typeof(COALESCE(domain, '[]'::json)) = 'array'
          AND json_array_length(COALESCE(domain, '[]'::json)) > 0
        """
    )
    op.create_unique_constraint(
        "uq_organisations_primary_domain",
        "organisations",
        ["primary_domain"],
        schema="public",
    )
    op.drop_constraint(
        "organisations_organisation_name_key",
        "organisations",
        schema="public",
        type_="unique",
    )

    op.create_table(
        "signup_requests",
        sa.Column("signup_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("supabase_user_id", sa.String(length=255), nullable=False),
        sa.Column("provider_user_id", sa.String(length=512), nullable=False),
        sa.Column("provider_tenant_id", sa.String(length=512), nullable=False),
        sa.Column("normalized_email", sa.String(length=255), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("job_title", sa.String(length=100), nullable=True),
        sa.Column("organisation_name", sa.String(length=255), nullable=False),
        sa.Column("primary_domain", sa.String(length=253), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("sales_team_size", sa.Integer(), nullable=True),
        sa.Column("website_url", sa.String(length=2048), nullable=True),
        sa.Column("linkedin_url", sa.String(length=2048), nullable=True),
        sa.Column("terms_version", sa.String(length=32), nullable=False),
        sa.Column("privacy_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=40), server_default="received", nullable=False),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["public.organisations.organisation_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["public.users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("signup_request_id"),
        sa.UniqueConstraint("idempotency_key_hash", name="uq_signup_idempotency_key_hash"),
        sa.UniqueConstraint(
            "provider",
            "provider_user_id",
            "provider_tenant_id",
            name="uq_signup_provider_subject_tenant",
        ),
        schema="public",
    )
    op.create_index(
        "ix_signup_requests_primary_domain",
        "signup_requests",
        ["primary_domain"],
        schema="public",
    )
    op.create_index(
        "ix_signup_requests_status",
        "signup_requests",
        ["status"],
        schema="public",
    )
    op.create_index(
        "ix_signup_requests_supabase_user_id",
        "signup_requests",
        ["supabase_user_id"],
        schema="public",
    )


def downgrade() -> None:
    """Remove signup/domain state before authentication profile fields."""
    op.drop_index(
        "ix_signup_requests_supabase_user_id",
        table_name="signup_requests",
        schema="public",
    )
    op.drop_index(
        "ix_signup_requests_status",
        table_name="signup_requests",
        schema="public",
    )
    op.drop_index(
        "ix_signup_requests_primary_domain",
        table_name="signup_requests",
        schema="public",
    )
    op.drop_table("signup_requests", schema="public")
    op.create_unique_constraint(
        "organisations_organisation_name_key",
        "organisations",
        ["organisation_name"],
        schema="public",
    )
    op.drop_constraint(
        "uq_organisations_primary_domain",
        "organisations",
        schema="public",
        type_="unique",
    )
    op.drop_column("organisations", "domain_verified_at", schema="public")
    op.drop_column("organisations", "domain_verification_method", schema="public")
    op.drop_column("organisations", "primary_domain", schema="public")

    op.drop_constraint(
        "ck_users_authentication_method",
        "users",
        schema="public",
        type_="check",
        if_exists=True,
    )
    op.drop_column("users", "authentication_method", schema="public", if_exists=True)
    op.drop_column("users", "profile_image_url", schema="public", if_exists=True)
