"""Backfill canonical task and note rows from legacy activity columns.

Revision ID: 3c7f79637734
Revises: 3c7f79637735
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "3c7f79637734"
down_revision: str | None = "3c7f79637735"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Move legacy task/note payloads into canonical source tables."""
    op.execute(
        """
        INSERT INTO crm_tasks (
            organisation_id,
            integration_id,
            is_deleted,
            created_at,
            updated_at,
            task_id,
            deal_id,
            title,
            description,
            status,
            priority,
            due_date,
            due_at,
            is_all_day,
            reminder_at,
            completed_at,
            assigned_to_user_id,
            assigned_to_email,
            assigned_to_name,
            created_by_user_id,
            owner_user_id,
            owner_email,
            completed_by_user_id,
            source_created_at,
            source_modified_at,
            task_metadata
        )
        SELECT
            organisation_id,
            integration_id,
            is_deleted,
            created_at,
            updated_at,
            activity_id,
            deal_id,
            COALESCE(NULLIF(title, ''), NULLIF(subject, ''), 'Untitled task'),
            body,
            CASE
                WHEN status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')
                    THEN status
                WHEN status IN ('WAITING', 'DEFERRED')
                    THEN 'IN_PROGRESS'
                ELSE 'NOT_STARTED'
            END,
            CASE
                WHEN priority IN ('NONE', 'LOW', 'MEDIUM', 'HIGH', 'URGENT')
                    THEN priority
                ELSE 'NONE'
            END,
            due_date::date,
            CASE WHEN is_all_day IS TRUE THEN NULL ELSE due_date END,
            COALESCE(is_all_day, false),
            reminder_date,
            completed_date,
            owner_id,
            external_assignee_email,
            external_assignee_name,
            created_by_user_id,
            owner_id,
            owner_email,
            CASE WHEN status = 'COMPLETED' THEN owner_id ELSE NULL END,
            source_created_at,
            source_modified_at,
            jsonb_strip_nulls(
                jsonb_build_object(
                    'legacy_activity_id', activity_id::text,
                    'legacy_activity_type', activity_type,
                    'legacy_activity_subtype', activity_subtype,
                    'legacy_source', source,
                    'owner_name', owner_name,
                    'properties', properties,
                    'metrics', metrics
                )
            )
        FROM crm_activities
        WHERE activity_type IN ('TASK', 'TODO')
        ON CONFLICT (task_id) DO NOTHING
        """
    )

    op.execute(
        """
        INSERT INTO crm_notes (
            organisation_id,
            integration_id,
            is_deleted,
            created_at,
            updated_at,
            note_id,
            deal_id,
            title,
            body,
            body_format,
            author_user_id,
            author_name,
            author_email,
            visibility,
            source_created_at,
            source_modified_at,
            note_metadata
        )
        SELECT
            organisation_id,
            integration_id,
            is_deleted,
            created_at,
            updated_at,
            activity_id,
            deal_id,
            COALESCE(
                NULLIF(title, ''),
                NULLIF(subject, ''),
                NULLIF(left(btrim(COALESCE(body, summary, preview, '')), 80), '')
            ),
            COALESCE(NULLIF(body, ''), NULLIF(summary, ''), NULLIF(preview, ''), ''),
            'plain_text',
            owner_id,
            owner_name,
            owner_email,
            'TEAM',
            source_created_at,
            source_modified_at,
            jsonb_strip_nulls(
                jsonb_build_object(
                    'legacy_activity_id', activity_id::text,
                    'legacy_activity_type', activity_type,
                    'legacy_activity_subtype', activity_subtype,
                    'legacy_source', source,
                    'properties', properties,
                    'metrics', metrics
                )
            )
        FROM crm_activities
        WHERE activity_type = 'NOTE'
        ON CONFLICT (note_id) DO NOTHING
        """
    )

    op.execute(
        """
        UPDATE crm_source_object_mappings m
        SET
            local_object_type = CASE WHEN a.activity_type = 'NOTE' THEN 'note' ELSE 'task' END,
            local_object_id = a.activity_id,
            mapping_metadata = COALESCE(m.mapping_metadata, '{}'::jsonb)
                || jsonb_build_object(
                    'migration', '065_backfill_legacy_activity_tasks_notes',
                    'legacy_local_object_type', 'activity',
                    'legacy_activity_id', a.activity_id::text,
                    'legacy_activity_type', a.activity_type
                ),
            updated_at = now()
        FROM crm_activities a
        WHERE m.organisation_id = a.organisation_id
            AND m.local_object_type = 'activity'
            AND m.local_object_id = a.activity_id
            AND a.activity_type IN ('TASK', 'TODO', 'NOTE')
        """
    )

    op.execute(
        """
        UPDATE crm_activities
        SET
            source_type = 'TASK',
            source_record_id = activity_id::text,
            title = COALESCE(NULLIF(title, ''), NULLIF(subject, ''), 'Untitled task'),
            summary = COALESCE(summary, body),
            preview = COALESCE(preview, left(COALESCE(body, subject, title, ''), 1000)),
            occurred_at = COALESCE(occurred_at, due_date, source_created_at, created_at)
        WHERE activity_type IN ('TASK', 'TODO')
            AND source_record_id IS NULL
        """
    )

    op.execute(
        """
        UPDATE crm_activities
        SET
            source_type = 'NOTE',
            source_record_id = activity_id::text,
            title = COALESCE(NULLIF(title, ''), NULLIF(subject, '')),
            summary = COALESCE(summary, body),
            preview = COALESCE(preview, left(COALESCE(body, subject, title, ''), 1000)),
            occurred_at = COALESCE(occurred_at, source_created_at, created_at)
        WHERE activity_type = 'NOTE'
            AND source_record_id IS NULL
        """
    )

    op.execute(
        """
        INSERT INTO crm_activity_links (
            activity_link_id,
            organisation_id,
            activity_id,
            object_type,
            object_id,
            link_role,
            link_source,
            confidence_score,
            occurred_at,
            created_at
        )
        SELECT
            gen_random_uuid(),
            organisation_id,
            activity_id,
            'DEAL',
            deal_id,
            'PRIMARY',
            'SYSTEM',
            NULL,
            COALESCE(occurred_at, due_date, source_created_at, created_at),
            now()
        FROM crm_activities
        WHERE activity_type IN ('TASK', 'TODO', 'NOTE')
            AND deal_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )

    op.execute(
        """
        INSERT INTO crm_activity_links (
            activity_link_id,
            organisation_id,
            activity_id,
            object_type,
            object_id,
            link_role,
            link_source,
            confidence_score,
            occurred_at,
            created_at
        )
        SELECT
            gen_random_uuid(),
            organisation_id,
            activity_id,
            'CONTACT',
            contact_id,
            'RELATED',
            'SYSTEM',
            NULL,
            COALESCE(occurred_at, due_date, source_created_at, created_at),
            now()
        FROM crm_activities
        WHERE activity_type IN ('TASK', 'TODO', 'NOTE')
            AND contact_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )

    op.execute(
        """
        INSERT INTO crm_activity_links (
            activity_link_id,
            organisation_id,
            activity_id,
            object_type,
            object_id,
            link_role,
            link_source,
            confidence_score,
            occurred_at,
            created_at
        )
        SELECT
            gen_random_uuid(),
            organisation_id,
            activity_id,
            'COMPANY',
            company_id,
            'RELATED',
            'SYSTEM',
            NULL,
            COALESCE(occurred_at, due_date, source_created_at, created_at),
            now()
        FROM crm_activities
        WHERE activity_type IN ('TASK', 'TODO', 'NOTE')
            AND company_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    """Remove source rows created from legacy activity rows."""
    op.execute(
        """
        DELETE FROM crm_activity_links l
        USING crm_activities a
        WHERE l.organisation_id = a.organisation_id
            AND l.activity_id = a.activity_id
            AND a.activity_type IN ('TASK', 'TODO', 'NOTE')
            AND l.link_source = 'SYSTEM'
        """
    )
    op.execute(
        """
        UPDATE crm_source_object_mappings
        SET
            local_object_type = 'activity',
            local_object_id = (mapping_metadata->>'legacy_activity_id')::uuid,
            mapping_metadata = mapping_metadata
                - 'migration'
                - 'legacy_local_object_type'
                - 'legacy_activity_id'
                - 'legacy_activity_type',
            updated_at = now()
        WHERE mapping_metadata->>'migration' = '065_backfill_legacy_activity_tasks_notes'
            AND mapping_metadata->>'legacy_local_object_type' = 'activity'
        """
    )
    op.execute(
        """
        DELETE FROM crm_tasks t
        USING crm_activities a
        WHERE t.task_id = a.activity_id
            AND a.activity_type IN ('TASK', 'TODO')
        """
    )
    op.execute(
        """
        DELETE FROM crm_notes n
        USING crm_activities a
        WHERE n.note_id = a.activity_id
            AND a.activity_type = 'NOTE'
        """
    )
    op.execute(
        """
        UPDATE crm_activities
        SET source_type = NULL,
            source_record_id = NULL
        WHERE activity_type IN ('TASK', 'TODO', 'NOTE')
            AND source_record_id = activity_id::text
        """
    )
