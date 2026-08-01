"""Contracts for canonical organisation CRM-user lookup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.service.crm.access.crm_user_directory_service import CRMUserDirectoryService
from src.service.crm.utils.identity import normalize_spoken_email_query


def _crm_user(*, organisation_id, full_name: str, email: str | None) -> CRMUser:  # noqa: ANN001
    return CRMUser(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        full_name=full_name,
        email=email,
        is_active=True,
        is_deleted=False,
    )


def _result(*rows: CRMUser) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(rows)
    return result


@pytest.mark.asyncio
async def test_exact_name_resolves_one_active_crm_user() -> None:
    """Exact identity wins before fuzzy matching so callers receive one stable owner UUID."""
    organisation_id = uuid4()
    user = _crm_user(
        organisation_id=organisation_id,
        full_name="Manish Kumar Tiwari",
        email="manish@example.com",
    )
    session = AsyncMock()
    session.execute.return_value = _result(user)

    resolution = await CRMUserDirectoryService(session, organisation_id).search(
        "  MANISH   KUMAR TIWARI "
    )

    assert resolution.status == "resolved"
    assert resolution.candidates == [user]
    session.execute.assert_awaited_once()
    statement = session.execute.await_args.args[0]
    compiled = statement.compile()
    assert organisation_id in compiled.params.values()
    assert "crm_users.organisation_id" in str(statement)
    assert "crm_users.is_active" in str(statement)
    assert "crm_users.is_deleted" in str(statement)


@pytest.mark.asyncio
async def test_duplicate_exact_name_stays_ambiguous() -> None:
    """Names are not identities; two exact names must require user clarification."""
    organisation_id = uuid4()
    first = _crm_user(
        organisation_id=organisation_id,
        full_name="Alex Shah",
        email="alex.one@example.com",
    )
    second = _crm_user(
        organisation_id=organisation_id,
        full_name="Alex Shah",
        email="alex.two@example.com",
    )
    session = AsyncMock()
    session.execute.return_value = _result(first, second)

    resolution = await CRMUserDirectoryService(session, organisation_id).search("Alex Shah")

    assert resolution.status == "ambiguous"
    assert resolution.candidates == [first, second]


@pytest.mark.asyncio
async def test_unique_partial_match_resolves_after_exact_lookup_misses() -> None:
    """A bounded partial lookup supports first-name prompts without guessing an email."""
    organisation_id = uuid4()
    user = _crm_user(
        organisation_id=organisation_id,
        full_name="Bhawesh Tagariya",
        email="bhawesh@example.com",
    )
    session = AsyncMock()
    session.execute.side_effect = [_result(), _result(user)]

    resolution = await CRMUserDirectoryService(session, organisation_id).search("Bhawesh")

    assert resolution.status == "resolved"
    assert resolution.candidates == [user]
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_blank_query_does_not_touch_database() -> None:
    """Invalid input stops at the directory boundary instead of scanning the org."""
    session = AsyncMock()

    resolution = await CRMUserDirectoryService(session, uuid4()).search(" ")

    assert resolution.status == "not_found"
    assert resolution.candidates == []
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_misspelled_name_uses_fuzzy_fallback_only_after_direct_miss() -> None:
    """Speech-like name errors get one bounded fallback after exact and partial miss."""
    organisation_id = uuid4()
    user = _crm_user(
        organisation_id=organisation_id,
        full_name="Jonathan Mehta",
        email="jonathan@example.com",
    )
    session = AsyncMock()
    session.execute.side_effect = [_result(), _result(), _result(user)]

    resolution = await CRMUserDirectoryService(session, organisation_id).search("Jonatan Mehta")

    assert resolution.status == "resolved"
    assert resolution.match_type == "fuzzy_name"
    assert resolution.candidates == [user]
    assert "similarity" in str(session.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_fuzzy_email_is_returned_as_a_suggestion_even_when_unique() -> None:
    """A close address must require confirmation before any recipient or write use."""
    organisation_id = uuid4()
    user = _crm_user(
        organisation_id=organisation_id,
        full_name="Jonathan Mehta",
        email="north.star@company.com",
    )
    session = AsyncMock()
    session.execute.side_effect = [_result(), _result(), _result(user)]

    resolution = await CRMUserDirectoryService(session, organisation_id).search(
        "northstar@company.com"
    )

    assert resolution.status == "ambiguous"
    assert resolution.match_type == "fuzzy_email"
    assert resolution.candidates == [user]


def test_spoken_email_normalization_is_conservative() -> None:
    """Only an explicit at-and-dot address phrase is rewritten from voice text."""
    assert normalize_spoken_email_query("north star at company dot com") == "northstar@company.com"
    assert normalize_spoken_email_query("Meet Matt at Dot Labs") == "meet matt at dot labs"
