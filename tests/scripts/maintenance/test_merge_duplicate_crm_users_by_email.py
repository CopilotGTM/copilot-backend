from __future__ import annotations

from uuid import UUID

from scripts.maintenance.merge_duplicate_crm_users_by_email import (
    CRMUserRow,
    build_merge_plan,
    normalize_email,
)

APP_USER_ID = UUID("11111111-1111-1111-1111-111111111111")
PROVIDER_USER_ID = UUID("22222222-2222-2222-2222-222222222222")
INTERNAL_USER_ID = UUID("33333333-3333-3333-3333-333333333333")
OTHER_APP_USER_ID = UUID("44444444-4444-4444-4444-444444444444")


def _row(
    user_id: UUID,
    *,
    email: str = "Owner@Example.com",
    active: bool = True,
    deleted: bool = False,
    synced_at: str | None = "2026-01-01 00:00:00+00",
) -> CRMUserRow:
    return CRMUserRow(
        user_id=user_id,
        email=email,
        normalized_email="owner@example.com",
        integration_id=None,
        is_active=active,
        is_deleted=deleted,
        full_name=None,
        synced_at=synced_at,
        updated_at=None,
    )


def test_normalize_email_trims_and_lowercases() -> None:
    assert normalize_email(" Owner@Example.com ") == "owner@example.com"
    assert normalize_email(" ") is None


def test_build_merge_plan_prefers_existing_app_user_id() -> None:
    rows = [_row(PROVIDER_USER_ID), _row(APP_USER_ID, synced_at="2026-02-01 00:00:00+00")]

    plan = build_merge_plan(
        "owner@example.com",
        rows,
        [APP_USER_ID],
        {APP_USER_ID, PROVIDER_USER_ID},
    )

    assert plan.can_execute
    assert plan.survivor_user_id == APP_USER_ID
    assert plan.loser_user_ids == [PROVIDER_USER_ID]
    assert plan.insert_survivor_from_user_id is None


def test_build_merge_plan_inserts_app_id_survivor_when_missing_from_crm_group() -> None:
    rows = [_row(PROVIDER_USER_ID), _row(INTERNAL_USER_ID)]

    plan = build_merge_plan(
        "owner@example.com",
        rows,
        [APP_USER_ID],
        {PROVIDER_USER_ID, INTERNAL_USER_ID},
    )

    assert plan.can_execute
    assert plan.survivor_user_id == APP_USER_ID
    assert set(plan.loser_user_ids) == {PROVIDER_USER_ID, INTERNAL_USER_ID}
    assert plan.insert_survivor_from_user_id in {PROVIDER_USER_ID, INTERNAL_USER_ID}


def test_build_merge_plan_skips_ambiguous_public_user_matches() -> None:
    rows = [_row(PROVIDER_USER_ID), _row(INTERNAL_USER_ID)]

    plan = build_merge_plan(
        "owner@example.com",
        rows,
        [APP_USER_ID, OTHER_APP_USER_ID],
        {PROVIDER_USER_ID, INTERNAL_USER_ID},
    )

    assert not plan.can_execute
    assert plan.skipped_reason == "multiple public.users rows match this email in the organisation"
