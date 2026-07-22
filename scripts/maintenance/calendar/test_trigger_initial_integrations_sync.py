"""Safety and eligibility checks for controlled calendar recovery."""

from argparse import Namespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from scripts.maintenance.calendar.trigger_initial_integrations_sync import (
    RunStats,
    _prepare_org,
    _validate_args,
)
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations.scope_validation import get_required_scopes


class _Scalars:
    def __init__(self, *, rows=None, one=None):
        self.rows = rows or []
        self.one = one

    def all(self):
        """Return the rows supplied by the test's query double."""
        return self.rows

    def first(self):
        """Return the optional first row supplied by the query double."""
        return self.one


class _Result:
    def __init__(self, *, rows=None, one=None):
        self._scalars = _Scalars(rows=rows, one=one)

    def scalars(self):
        """Mirror SQLAlchemy's scalar-result adapter for the script tests."""
        return self._scalars

    def scalar_one_or_none(self):
        """Return the optional single row supplied by the query double."""
        return self._scalars.one


def _args(**overrides):
    values = {
        "all_orgs": False,
        "org_id": None,
        "integration_id": None,
        "enable_calendar_capability": False,
        "execute": False,
        "yes": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _parent_integration(*, scopes: list[str]) -> KnowledgeIntegration:
    return KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_scope=IntegrationScope.ORGANIZATION,
        integration_type=IntegrationType.GOOGLE,
        name="Google",
        connection_id="connected",
        credentials={"access_token": "access", "refresh_token": "refresh"},
        scopes=scopes,
        is_active=True,
    )


def _session_generator(session):
    async def _gen(_organisation_id):
        yield session

    return _gen


def test_global_execute_requires_explicit_confirmation() -> None:
    """One --execute flag must not reset every approved production organisation."""
    with pytest.raises(ValueError, match="--all-orgs --yes"):
        _validate_args(_args(execute=True))

    _validate_args(_args(execute=True, all_orgs=True, yes=True))


def test_capability_enable_requires_explicit_integration_ids() -> None:
    """Broad recovery must never reverse a possibly intentional disconnect."""
    with pytest.raises(ValueError, match="requires --integration-id"):
        _validate_args(_args(enable_calendar_capability=True))


@pytest.mark.asyncio
async def test_allowlisted_dry_run_can_create_valid_calendar_state() -> None:
    """Stored scopes/token fields make an allowlisted missing state eligible in dry-run."""
    integration = _parent_integration(
        scopes=list(get_required_scopes(IntegrationType.GOOGLE_CALENDAR))
    )
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[_Result(rows=[integration]), _Result(one=None)]
    )
    session.add = MagicMock()

    with patch(
        "scripts.maintenance.calendar.trigger_initial_integrations_sync.acquire_org_async_session",
        side_effect=_session_generator(session),
    ):
        plans = await _prepare_org(
            integration.organisation_id,
            families={"calendar"},
            execute=False,
            stats=RunStats(),
            integration_ids={integration.integration_id},
            enable_calendar_capability=True,
        )

    assert plans[0].blockers == []
    assert "created enabled calendar capability state" in plans[0].resets
    assert plans[0].dispatches


@pytest.mark.asyncio
async def test_allowlisted_recovery_blocks_missing_scopes() -> None:
    """An allowlist does not bypass provider consent requirements."""
    integration = _parent_integration(scopes=[])
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[_Result(rows=[integration]), _Result(one=None)]
    )
    session.add = MagicMock()

    with patch(
        "scripts.maintenance.calendar.trigger_initial_integrations_sync.acquire_org_async_session",
        side_effect=_session_generator(session),
    ):
        plans = await _prepare_org(
            integration.organisation_id,
            families={"calendar"},
            execute=False,
            stats=RunStats(),
            integration_ids={integration.integration_id},
            enable_calendar_capability=True,
        )

    assert any(blocker.startswith("missing scopes:") for blocker in plans[0].blockers)
    assert plans[0].dispatches == []
