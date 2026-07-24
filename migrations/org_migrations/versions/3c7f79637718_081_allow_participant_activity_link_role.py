"""Allow hydration-owned participant activity links.

Revision ID: 3c7f79637718
Revises: 3c7f79637719
Create Date: 2026-07-24

Communication hydration uses PARTICIPANT links to maintain contact and company
visibility without replacing sender, attendee, or deal-mapper-owned roles.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3c7f79637718"
down_revision: str | None = "3c7f79637719"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASE_ROLES = (
    "'PRIMARY', 'SENDER', 'RECIPIENT', 'ATTENDEE', 'OWNER', 'MENTIONED', "
    "'RELATED', 'CREATED', 'RESOLVED', 'ASSIGNEE'"
)


def upgrade() -> None:
    """Permit the hydration-owned participant role."""
    op.drop_constraint(
        "ck_crm_activity_link_role",
        "crm_activity_links",
        type_="check",
    )
    op.create_check_constraint(
        "ck_crm_activity_link_role",
        "crm_activity_links",
        f"link_role IN ({_BASE_ROLES}, 'PARTICIPANT')",
    )


def downgrade() -> None:
    """Fold participant links into RELATED before restoring the old role set."""
    op.drop_constraint(
        "ck_crm_activity_link_role",
        "crm_activity_links",
        type_="check",
    )
    # A target may already have a mapper-owned RELATED link. Remove only the
    # duplicate participant row before converting the remaining hydration links.
    op.execute(
        """
        DELETE FROM crm_activity_links AS participant_link
        USING crm_activity_links AS related_link
        WHERE participant_link.organisation_id = related_link.organisation_id
          AND participant_link.activity_id = related_link.activity_id
          AND participant_link.object_type = related_link.object_type
          AND participant_link.object_id = related_link.object_id
          AND participant_link.link_role = 'PARTICIPANT'
          AND related_link.link_role = 'RELATED'
        """
    )
    op.execute(
        """
        UPDATE crm_activity_links
        SET link_role = 'RELATED'
        WHERE link_role = 'PARTICIPANT'
        """
    )
    op.create_check_constraint(
        "ck_crm_activity_link_role",
        "crm_activity_links",
        f"link_role IN ({_BASE_ROLES})",
    )
