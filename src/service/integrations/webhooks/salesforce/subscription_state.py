from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.service.integrations.webhooks.salesforce.metadata import (
    channel_metadata_key,
)

_DEFAULT_PUBSUB_ENDPOINT = "api.pubsub.salesforce.com:443"


@dataclass(slots=True, frozen=True)
class SalesforceCdcSubscriptionState:
    organisation_id: UUID
    integration_id: UUID
    channel: str
    salesforce_organization_id: str
    pubsub_endpoint: str
    last_replay_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    status: str = "active"
    last_error_class: str | None = None
    last_error_message: str | None = None
    last_event_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class SalesforceCdcLeaseResult:
    acquired: bool
    subscription: SalesforceCdcSubscriptionState


class SalesforceCdcSubscriptionStateService:
    async def get_subscription_state(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        lock: bool = False,
    ) -> SalesforceCdcSubscriptionState | None:
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=lock,
        )
        if integration is None:
            return None
        return self._state_from_integration(integration, channel)

    async def acquire_lease(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        salesforce_organization_id: str,
        lease_owner: str,
        lease_seconds: int,
        pubsub_endpoint: str = _DEFAULT_PUBSUB_ENDPOINT,
    ) -> SalesforceCdcLeaseResult:
        integration = await self._require_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        state = self._state_from_integration(integration, channel)
        now = datetime.now(UTC)
        if (
            state.lease_owner
            and state.lease_owner != lease_owner
            and state.lease_expires_at
            and _as_aware(state.lease_expires_at) > now
        ):
            return SalesforceCdcLeaseResult(acquired=False, subscription=state)

        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["salesforce_organization_id"] = salesforce_organization_id
        cdc_state["pubsub_endpoint"] = pubsub_endpoint
        cdc_state["lease_owner"] = lease_owner
        cdc_state["lease_expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
        cdc_state["status"] = "active"
        cdc_state["last_error_class"] = None
        cdc_state["last_error_message"] = None
        await self._save_metadata(session, integration)
        return SalesforceCdcLeaseResult(
            acquired=True,
            subscription=self._state_from_integration(integration, channel),
        )

    async def renew_lease(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        lease_owner: str,
        lease_seconds: int,
    ) -> bool:
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return False
        state = self._state_from_integration(integration, channel)
        if state.lease_owner != lease_owner:
            return False
        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["lease_expires_at"] = (
            datetime.now(UTC) + timedelta(seconds=lease_seconds)
        ).isoformat()
        await self._save_metadata(session, integration)
        return True

    async def store_replay_after_commit(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        replay_id: str,
        lease_owner: str,
    ) -> bool:
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return False
        state = self._state_from_integration(integration, channel)
        if state.lease_owner != lease_owner:
            return False
        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["last_replay_id"] = replay_id
        cdc_state["last_event_at"] = datetime.now(UTC).isoformat()
        await self._save_metadata(session, integration)
        return True

    async def store_keepalive_replay(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        replay_id: str,
        lease_owner: str,
    ) -> bool:
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return False
        state = self._state_from_integration(integration, channel)
        if state.lease_owner != lease_owner:
            return False
        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["last_replay_id"] = replay_id
        await self._save_metadata(session, integration)
        return True

    async def clear_replay_for_repair(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
    ) -> None:
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return
        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["last_replay_id"] = None
        await self._save_metadata(session, integration)

    async def release_lease(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        lease_owner: str,
    ) -> bool:
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return False
        state = self._state_from_integration(integration, channel)
        if state.lease_owner != lease_owner:
            return False
        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["lease_owner"] = None
        cdc_state["lease_expires_at"] = None
        await self._save_metadata(session, integration)
        return True

    async def mark_error(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        error: Exception,
    ) -> None:
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return
        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["status"] = "error"
        cdc_state["last_error_class"] = type(error).__name__
        cdc_state["last_error_message"] = str(error)
        await self._save_metadata(session, integration)

    async def _require_integration(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        lock: bool = False,
    ) -> KnowledgeIntegration:
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=lock,
        )
        if integration is None:
            raise ValueError(f"Salesforce integration {integration_id} not found")
        return integration

    async def _load_integration(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        lock: bool = False,
    ) -> KnowledgeIntegration | None:
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == organisation_id,
            col(KnowledgeIntegration.integration_id) == integration_id,
        )
        if lock:
            stmt = stmt.with_for_update()
        return cast(
            KnowledgeIntegration | None,
            (await session.execute(stmt)).scalar_one_or_none(),
        )

    def _state_from_integration(
        self,
        integration: KnowledgeIntegration,
        channel: str,
    ) -> SalesforceCdcSubscriptionState:
        cdc_state = self._read_cdc_state(integration, channel)
        return SalesforceCdcSubscriptionState(
            organisation_id=integration.organisation_id,
            integration_id=integration.integration_id,
            channel=channel,
            salesforce_organization_id=str(cdc_state.get("salesforce_organization_id") or ""),
            pubsub_endpoint=str(cdc_state.get("pubsub_endpoint") or _DEFAULT_PUBSUB_ENDPOINT),
            last_replay_id=_optional_str(cdc_state.get("last_replay_id")),
            lease_owner=_optional_str(cdc_state.get("lease_owner")),
            lease_expires_at=_optional_datetime(cdc_state.get("lease_expires_at")),
            status=str(cdc_state.get("status") or "active"),
            last_error_class=_optional_str(cdc_state.get("last_error_class")),
            last_error_message=_optional_str(cdc_state.get("last_error_message")),
            last_event_at=_optional_datetime(cdc_state.get("last_event_at")),
        )

    def _read_cdc_state(
        self,
        integration: KnowledgeIntegration,
        channel: str,
    ) -> dict[str, Any]:
        metadata = dict(integration.connection_metadata or {})
        cdc_metadata = dict(metadata.get("salesforce_cdc") or {})
        key = channel_metadata_key(channel)
        return dict(cdc_metadata.get(key) or {})

    def _get_mutable_cdc_state(
        self,
        integration: KnowledgeIntegration,
        channel: str,
    ) -> dict[str, Any]:
        metadata = dict(integration.connection_metadata or {})
        cdc_metadata = dict(metadata.get("salesforce_cdc") or {})
        key = channel_metadata_key(channel)
        cdc_state = dict(cdc_metadata.get(key) or {})
        cdc_state.setdefault("enabled", True)
        cdc_state.setdefault("channel", channel)
        cdc_metadata[key] = cdc_state
        metadata["salesforce_cdc"] = cdc_metadata
        integration.connection_metadata = metadata
        return cdc_state

    async def _save_metadata(self, session, integration: KnowledgeIntegration) -> None:
        session.add(integration)
        await session.commit()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_aware(value)
    text = str(value).strip()
    if not text:
        return None
    return _as_aware(datetime.fromisoformat(text.replace("Z", "+00:00")))


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
