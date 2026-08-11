"""Store Salesforce CDC cursors, leases, and repair state in integration JSON metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.service.integrations.webhooks.salesforce.metadata import (
    AGGREGATE_CDC_CHANNEL,
    SALESFORCE_CDC_OBJECTS,
    channel_metadata_key,
)

_DEFAULT_PUBSUB_ENDPOINT = "api.pubsub.salesforce.com:443"


@dataclass(slots=True, frozen=True)
class SalesforceCdcSubscriptionState:
    """Immutable view of one aggregate or per-object CDC subscription."""

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
    heartbeat_at: datetime | None = None
    last_connected_at: datetime | None = None
    last_disconnected_at: datetime | None = None
    replay_preset: str | None = None
    repair_attempts: int = 0
    repair_next_retry_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class SalesforceCdcLeaseResult:
    """Report whether a lease was acquired and return the authoritative state."""

    acquired: bool
    subscription: SalesforceCdcSubscriptionState


class SalesforceCdcSubscriptionStateService:
    """Serialize CDC state transitions through the integration row lock."""

    async def get_subscription_state(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        lock: bool = False,
    ) -> SalesforceCdcSubscriptionState | None:
        """Read one subscription, optionally locking its integration row."""
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
        """Acquire or reclaim an expired lease unless aggregate mode fences the channel."""
        integration = await self._require_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        state = self._state_from_integration(integration, channel)
        if channel != AGGREGATE_CDC_CHANNEL and self._is_aggregate_mode(integration):
            return SalesforceCdcLeaseResult(acquired=False, subscription=state)
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
        """Extend a lease only for its current owner and authoritative runtime mode."""
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return False
        if channel != AGGREGATE_CDC_CHANNEL and self._is_aggregate_mode(integration):
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
        """Advance replay only after persistence commits and ownership is verified."""
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
        """Advance the provider keepalive cursor only for the current lease owner."""
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
        """Clear an invalid replay cursor before a bounded repair starts."""
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
        """Release only the caller's lease, preserving a successor owner's lease."""
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
        """Persist the latest subscriber error for operational diagnosis."""
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

    async def mark_heartbeat(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        lease_owner: str,
    ) -> bool:
        """Stamp worker liveness only while the caller owns the row-locked lease."""
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if (
            integration is None
            or self._state_from_integration(integration, channel).lease_owner != lease_owner
        ):
            return False
        self._get_mutable_cdc_state(integration, channel)["heartbeat_at"] = datetime.now(
            UTC
        ).isoformat()
        await self._save_metadata(session, integration)
        return True

    async def mark_connected(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        lease_owner: str,
    ) -> bool:
        """Record a successful Pub/Sub connection for the current lease owner."""
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if (
            integration is None
            or self._state_from_integration(integration, channel).lease_owner != lease_owner
        ):
            return False
        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["last_connected_at"] = datetime.now(UTC).isoformat()
        cdc_state["status"] = "active"
        await self._save_metadata(session, integration)
        return True

    async def mark_disconnected(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        lease_owner: str,
    ) -> bool:
        """Record transport disconnection without allowing a stale owner to overwrite state."""
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if (
            integration is None
            or self._state_from_integration(integration, channel).lease_owner != lease_owner
        ):
            return False
        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["last_disconnected_at"] = datetime.now(UTC).isoformat()
        cdc_state["status"] = "pending"
        await self._save_metadata(session, integration)
        return True

    async def mark_repairing(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
    ) -> bool:
        """Claim the serial repair transition; repeated claims are rejected."""
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return False
        state = self._state_from_integration(integration, channel)
        if state.status == "repairing":
            return False
        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["status"] = "repairing"
        cdc_state["repair_attempts"] = state.repair_attempts + 1
        cdc_state["repair_next_retry_at"] = None
        await self._save_metadata(session, integration)
        return True

    async def mark_repair_failed(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
        error: Exception,
        retry_at: datetime | None,
    ) -> bool:
        """Persist a bounded repair failure and its next supervisor retry time."""
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return False
        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["status"] = "repairing"
        cdc_state["last_error_class"] = type(error).__name__
        cdc_state["last_error_message"] = str(error)
        cdc_state["repair_next_retry_at"] = (
            _as_aware(retry_at).isoformat() if retry_at is not None else None
        )
        await self._save_metadata(session, integration)
        return True

    async def mark_repair_succeeded(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
    ) -> bool:
        """Reset repair state and force the repaired aggregate stream to EARLIEST."""
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return False
        cdc_state = self._get_mutable_cdc_state(integration, channel)
        cdc_state["status"] = "pending"
        cdc_state["last_replay_id"] = None
        cdc_state["replay_preset"] = "EARLIEST"
        cdc_state["repair_attempts"] = 0
        cdc_state["repair_next_retry_at"] = None
        cdc_state["last_error_class"] = None
        cdc_state["last_error_message"] = None
        await self._save_metadata(session, integration)
        return True

    async def mark_capacity_exceeded(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        channel: str,
    ) -> bool:
        """Persist that this integration is desired but outside the worker's fixed capacity."""
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return False
        self._get_mutable_cdc_state(integration, channel)["status"] = "capacity_exceeded"
        await self._save_metadata(session, integration)
        return True

    async def has_active_legacy_leases(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> bool:
        """Return whether any configured per-object subscriber still owns an unexpired lease."""
        integration = await self._load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            lock=True,
        )
        if integration is None:
            return False
        now = datetime.now(UTC)
        for config in SALESFORCE_CDC_OBJECTS.values():
            state = self._state_from_integration(integration, config["channel"])
            if (
                state.lease_owner
                and state.lease_expires_at
                and _as_aware(state.lease_expires_at) > now
            ):
                return True
        return False

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
            heartbeat_at=_optional_datetime(cdc_state.get("heartbeat_at")),
            last_connected_at=_optional_datetime(cdc_state.get("last_connected_at")),
            last_disconnected_at=_optional_datetime(cdc_state.get("last_disconnected_at")),
            replay_preset=_optional_str(cdc_state.get("replay_preset")),
            repair_attempts=int(cdc_state.get("repair_attempts") or 0),
            repair_next_retry_at=_optional_datetime(cdc_state.get("repair_next_retry_at")),
        )

    def _is_aggregate_mode(self, integration: KnowledgeIntegration) -> bool:
        metadata = dict(integration.connection_metadata or {})
        cdc_metadata = dict(metadata.get("salesforce_cdc") or {})
        return cdc_metadata.get("mode") == "aggregate"

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
