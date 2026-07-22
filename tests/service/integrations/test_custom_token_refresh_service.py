from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations.custom_token_refresh_service import CustomTokenRefreshService


class FakeResult:
    def __init__(
        self,
        *,
        rows: list[tuple[object, ...]] | None = None,
        scalar: object | None = None,
    ) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def all(self) -> list[tuple[object, ...]]:
        return self._rows

    def scalar_one_or_none(self) -> object | None:
        return self._scalar


def _session_generator(sessions: list[AsyncMock]):
    async def _gen(_organisation_id):
        yield sessions.pop(0)

    return _gen


def _make_session(*results: FakeResult) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=list(results))
    session.commit = AsyncMock()
    session.add = MagicMock()
    return session


def _make_integration(**overrides: object) -> KnowledgeIntegration:
    now = datetime.utcnow()
    defaults: dict[str, object] = {
        "organisation_id": uuid4(),
        "created_by_user_id": uuid4(),
        "integration_scope": IntegrationScope.ORGANIZATION,
        "integration_type": IntegrationType.GMAIL,
        "name": "Gmail Custom Integration",
        "credentials": {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
        },
        "connection_id": str(uuid4()),
        "is_active": True,
        "connection_metadata": {},
        "scopes": ["old.scope"],
        "access_token_expires_at": now + timedelta(minutes=5),
        "refresh_token_expires_at": None,
    }
    defaults.update(overrides)
    return KnowledgeIntegration(**defaults)


@pytest.mark.asyncio
async def test_refresh_single_token_updates_credentials_and_preserves_refresh_token() -> None:
    organisation_id = uuid4()
    integration = _make_integration(
        connection_metadata={
            "token_refresh_failures": 2,
            "last_refresh_failure_at": datetime.utcnow().isoformat(),
        }
    )
    session = _make_session(FakeResult(scalar=integration))
    provider = AsyncMock()
    provider.provider_key = "google"
    provider.refresh_token.return_value = {
        "access_token": "new-access",
        "expires_in": 3600,
        "scope": "gmail.readonly profile",
    }

    service = CustomTokenRefreshService(organisation_id=organisation_id)

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([session]),
        ),
        patch(
            "src.service.integrations.custom_token_refresh_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
    ):
        result = await service.refresh_single_token_async(integration.integration_id)

    assert result["success"] is True
    assert integration.credentials["access_token"] == "new-access"
    assert integration.credentials["refresh_token"] == "old-refresh"
    assert integration.scopes == ["gmail.readonly", "profile"]
    assert integration.access_token_expires_at is not None
    assert "token_refresh_failures" not in integration.connection_metadata
    provider.refresh_token.assert_awaited_once_with("old-refresh")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_single_token_skips_reauth_integration_until_user_reconnects() -> None:
    organisation_id = uuid4()
    integration = _make_integration(
        is_active=False,
        requires_reauth=True,
        last_sync_status="auth_failed",
        last_sync_error="Token refresh failed: stale client credentials",
        consecutive_sync_failures=1,
        connection_metadata={
            "token_refresh_failures": 2,
            "last_refresh_failure_at": datetime.utcnow().isoformat(),
        },
    )
    session = _make_session(FakeResult(scalar=integration))
    provider = AsyncMock()
    provider.provider_key = "google"
    provider.refresh_token.return_value = {
        "access_token": "new-access",
        "expires_in": 3600,
        "scope": "gmail.readonly profile",
    }

    service = CustomTokenRefreshService(organisation_id=organisation_id)

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([session]),
        ),
        patch(
            "src.service.integrations.custom_token_refresh_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
    ):
        result = await service.refresh_single_token_async(integration.integration_id)

    assert result["success"] is False
    assert result["handled"] is True
    assert result["reason"] == "requires_reauth"
    assert integration.is_active is False
    assert integration.requires_reauth is True
    provider.refresh_token.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_single_token_fails_when_refresh_token_missing() -> None:
    organisation_id = uuid4()
    integration = _make_integration(credentials={"access_token": "old-access"})
    session = _make_session(FakeResult(scalar=integration))

    service = CustomTokenRefreshService(organisation_id=organisation_id)

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([session]),
        ),
        patch(
            "src.service.integrations.custom_token_refresh_service.ProviderFactory.get_provider",
        ) as provider_factory,
    ):
        result = await service.refresh_single_token_async(integration.integration_id)

    assert result == {
        "success": False,
        "integration_id": str(integration.integration_id),
        "error": "Integration has no refresh_token",
        "deactivated": False,
    }
    provider_factory.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_single_token_keeps_transient_failures_active() -> None:
    """Repeated internal/provider failures remain eligible for scheduled recovery."""
    organisation_id = uuid4()
    integration = _make_integration(connection_metadata={"token_refresh_failures": 2})
    session = _make_session(FakeResult(scalar=integration))
    provider = AsyncMock()
    provider.provider_key = "google"
    provider.refresh_token.side_effect = Exception("refresh failed")

    service = CustomTokenRefreshService(organisation_id=organisation_id)

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([session]),
        ),
        patch(
            "src.service.integrations.custom_token_refresh_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
    ):
        result = await service.refresh_single_token_async(integration.integration_id)

    assert result["success"] is False
    assert result["failure_count"] == 3
    assert result["deactivated"] is False
    assert integration.is_active is True
    assert integration.last_sync_status == "internal_error"
    assert integration.connection_metadata["token_refresh_failures"] == 3
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload", "expected_type"),
    [
        (429, {"error": "rate_limited"}, "rate_limited"),
        (500, {"error": "provider_down"}, "provider_error"),
        (401, {"error": "unauthorized"}, "provider_error"),
        (400, {"error": "invalid_client"}, "internal_error"),
    ],
)
async def test_refresh_http_failures_without_revoked_grant_stay_active(
    status_code: int,
    payload: dict[str, str],
    expected_type: str,
) -> None:
    """Provider/app failures must not turn into terminal user reconnect state."""
    organisation_id = uuid4()
    integration = _make_integration(connection_metadata={"token_refresh_failures": 8})
    session = _make_session(FakeResult(scalar=integration))
    response = httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "https://provider.example/token"),
    )
    provider = AsyncMock()
    provider.provider_key = "provider"
    provider.refresh_token.side_effect = httpx.HTTPStatusError(
        str(status_code),
        request=response.request,
        response=response,
    )

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([session]),
        ),
        patch(
            "src.service.integrations.custom_token_refresh_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
    ):
        result = await CustomTokenRefreshService(organisation_id).refresh_single_token_async(
            integration.integration_id
        )

    assert result["failure_type"] == expected_type
    assert result["deactivated"] is False
    assert integration.is_active is True
    assert integration.requires_reauth is False


@pytest.mark.asyncio
async def test_refresh_timeout_stays_active() -> None:
    """A provider timeout remains retryable even after many earlier attempts."""
    organisation_id = uuid4()
    integration = _make_integration(connection_metadata={"token_refresh_failures": 8})
    session = _make_session(FakeResult(scalar=integration))
    provider = AsyncMock()
    provider.provider_key = "google"
    provider.refresh_token.side_effect = httpx.ReadTimeout("provider timeout")

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([session]),
        ),
        patch(
            "src.service.integrations.custom_token_refresh_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
    ):
        result = await CustomTokenRefreshService(organisation_id).refresh_single_token_async(
            integration.integration_id
        )

    assert result["failure_type"] == "provider_error"
    assert result["deactivated"] is False
    assert integration.is_active is True


@pytest.mark.asyncio
async def test_refresh_single_token_marks_invalid_grant_as_reauth_required() -> None:
    organisation_id = uuid4()
    integration = _make_integration(connection_metadata={"token_refresh_failures": 2})
    session = _make_session(FakeResult(scalar=integration))
    response = httpx.Response(
        400,
        json={
            "error": "invalid_grant",
            "error_description": "Token has been expired or revoked.",
        },
        request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
    )
    error = httpx.HTTPStatusError("400 Bad Request", request=response.request, response=response)
    provider = AsyncMock()
    provider.provider_key = "google"
    provider.refresh_token.side_effect = error

    service = CustomTokenRefreshService(organisation_id=organisation_id)

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([session]),
        ),
        patch(
            "src.service.integrations.custom_token_refresh_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
    ):
        result = await service.refresh_single_token_async(integration.integration_id)

    assert result["success"] is False
    assert result["handled"] is True
    assert result["action_required"] == "reauth_expired"
    assert result["failure_type"] == "auth_failed"
    assert integration.requires_reauth is True
    assert integration.is_active is False
    assert integration.last_sync_status == "auth_failed"
    assert integration.last_sync_error.startswith("invalid_grant:")
    assert "token_refresh_failures" not in integration.connection_metadata
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_org_tokens_summarises_results() -> None:
    organisation_id = uuid4()
    integration_id_1 = uuid4()
    integration_id_2 = uuid4()
    query_session = _make_session(FakeResult(rows=[(integration_id_1,), (integration_id_2,)]))

    service = CustomTokenRefreshService(organisation_id=organisation_id)

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([query_session]),
        ),
        patch.object(
            service,
            "_refresh_single_integration",
            AsyncMock(
                side_effect=[
                    {
                        "success": True,
                        "integration_id": str(integration_id_1),
                        "deactivated": False,
                    },
                    {
                        "success": False,
                        "integration_id": str(integration_id_2),
                        "deactivated": True,
                    },
                ]
            ),
        ) as refresh_single,
    ):
        result = await service.refresh_org_tokens_async(buffer_hours=2)

    assert result["status"] == "partial"
    assert result["summary"] == {
        "total_integrations": 2,
        "successful": 1,
        "failed": 1,
        "deactivated": 1,
        "action_required": 0,
    }
    assert refresh_single.await_count == 2


@pytest.mark.asyncio
async def test_refresh_org_tokens_counts_reauth_as_handled_action_required() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    query_session = _make_session(FakeResult(rows=[(integration_id,)]))

    service = CustomTokenRefreshService(organisation_id=organisation_id)

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([query_session]),
        ),
        patch.object(
            service,
            "_refresh_single_integration",
            AsyncMock(
                return_value={
                    "success": False,
                    "handled": True,
                    "action_required": "reauth_expired",
                    "failure_type": "auth_failed",
                    "integration_id": str(integration_id),
                    "deactivated": True,
                }
            ),
        ),
    ):
        result = await service.refresh_org_tokens_async(buffer_hours=2)

    assert result["status"] == "completed"
    assert result["summary"] == {
        "total_integrations": 1,
        "successful": 0,
        "failed": 0,
        "deactivated": 1,
        "action_required": 1,
    }


@pytest.mark.asyncio
async def test_refresh_org_tokens_processes_integrations_returned_by_active_query() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    query_session = _make_session(FakeResult(rows=[(integration_id,)]))

    service = CustomTokenRefreshService(organisation_id=organisation_id)

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([query_session]),
        ),
        patch.object(
            service,
            "_refresh_single_integration",
            AsyncMock(
                return_value={
                    "success": True,
                    "integration_id": str(integration_id),
                    "deactivated": False,
                }
            ),
        ),
    ):
        result = await service.refresh_org_tokens_async(buffer_hours=2)

    executed_stmt = query_session.execute.await_args.args[0]
    assert "is_active" in str(executed_stmt)
    assert "requires_reauth" in str(executed_stmt)
    assert result["summary"]["successful"] == 1
