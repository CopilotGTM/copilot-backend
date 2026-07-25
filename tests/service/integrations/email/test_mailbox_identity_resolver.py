"""Tests for application-user mailbox identity resolution."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import ClauseElement
from src.service.integrations.email.mailbox_identity_resolver import (
    CALENDAR_MATCHER_INTEGRATION_TYPES,
    EMAIL_MATCHER_INTEGRATION_TYPES,
    get_active_integration_emails_for_users,
    get_active_integration_emails_grouped_by_user,
)


class _FakeScalarResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeSession:
    def __init__(self, rows_by_execute: list[list[object]]):
        self._rows_by_execute = rows_by_execute
        self.statements: list[ClauseElement] = []
        self.execute_calls = 0

    async def execute(self, stmt: ClauseElement):
        self.statements.append(stmt)
        idx = min(self.execute_calls, len(self._rows_by_execute) - 1)
        self.execute_calls += 1

        if not self._rows_by_execute:
            return _FakeExecuteResult([])

        return _FakeExecuteResult(self._rows_by_execute[idx])


def _compile_sql(stmt: ClauseElement) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


@pytest.mark.asyncio
async def test_resolves_active_integration_emails_for_integration_user_ids() -> None:
    org_id = uuid4()
    owner_id = uuid4()
    partner_id = uuid4()
    session = _FakeSession(
        [
            [
                (owner_id, "Seller@gmail.com"),
                (owner_id, "  "),
                (partner_id, " null "),
                (partner_id, None),
                (partner_id, "owner@company.com"),
            ],
        ]
    )

    emails = await get_active_integration_emails_for_users(
        session,
        organisation_id=org_id,
        created_by_user_ids={owner_id, partner_id},
        integration_types=EMAIL_MATCHER_INTEGRATION_TYPES,
    )

    assert emails == {"seller@gmail.com", "owner@company.com"}

    stmt = _compile_sql(session.statements[0])
    assert "knowledge_integrations.organisation_id" in stmt
    assert str(org_id) in stmt
    assert "knowledge_integrations.created_by_user_id" in stmt
    assert str(owner_id) in stmt
    assert str(partner_id) in stmt
    assert "knowledge_integrations.integration_type" in stmt
    assert "'GMAIL'" not in stmt
    assert "'google'" in stmt
    assert "'microsoft'" in stmt
    assert "'gmail'" not in stmt
    assert "'outlook_email'" not in stmt
    assert "knowledge_integrations.is_active" in stmt
    assert "IS true" in stmt
    assert "knowledge_integrations.integration_email" in stmt
    assert "trim(" in stmt
    assert "!=" in stmt


@pytest.mark.asyncio
async def test_resolves_active_integration_emails_grouped_by_user_id() -> None:
    org_id = uuid4()
    owner_id = uuid4()
    partner_id = uuid4()
    session = _FakeSession(
        [
            [
                (owner_id, "Seller@gmail.com"),
                (owner_id, "  "),
                (owner_id, " null "),
                (owner_id, None),
                (partner_id, "partner@company.com"),
            ],
        ]
    )

    emails_by_user = await get_active_integration_emails_grouped_by_user(
        session,
        organisation_id=org_id,
        created_by_user_ids={owner_id, partner_id},
        integration_types=EMAIL_MATCHER_INTEGRATION_TYPES,
    )

    assert emails_by_user == {
        owner_id: {"seller@gmail.com"},
        partner_id: {"partner@company.com"},
    }

    stmt = _compile_sql(session.statements[0])
    assert "knowledge_integrations.organisation_id" in stmt
    assert str(org_id) in stmt
    assert "knowledge_integrations.created_by_user_id" in stmt
    assert str(owner_id) in stmt
    assert str(partner_id) in stmt
    assert "knowledge_integrations.integration_type" in stmt
    assert "'google'" in stmt
    assert "'microsoft'" in stmt
    assert "'gmail'" not in stmt
    assert "'outlook_email'" not in stmt
    assert "knowledge_integrations.is_active" in stmt
    assert "IS true" in stmt
    assert "knowledge_integrations.integration_email" in stmt
    assert "trim(" in stmt
    assert "!=" in stmt


@pytest.mark.asyncio
async def test_returns_empty_set_when_no_participant_users_are_provided() -> None:
    session = _FakeSession([])

    emails = await get_active_integration_emails_for_users(
        session,
        organisation_id=uuid4(),
        created_by_user_ids=set(),
        integration_types=EMAIL_MATCHER_INTEGRATION_TYPES,
    )

    assert emails == set()
    assert session.statements == []


@pytest.mark.asyncio
async def test_resolves_calendar_integration_types() -> None:
    user_id = uuid4()
    session = _FakeSession([[(user_id, "calendar@google.com")]])

    emails = await get_active_integration_emails_for_users(
        session,
        organisation_id=uuid4(),
        created_by_user_ids={user_id},
        integration_types=CALENDAR_MATCHER_INTEGRATION_TYPES,
    )

    assert emails == {"calendar@google.com"}

    stmt = _compile_sql(session.statements[0])
    assert "knowledge_integrations.integration_type" in stmt
    assert "'google'" in stmt
    assert "'microsoft'" in stmt
    assert "'google_calendar'" not in stmt
    assert "'outlook_calendar'" not in stmt
