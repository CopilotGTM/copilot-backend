"""Durable, tenant-scoped progress state for initial email and calendar syncs."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    KnowledgeIntegration,
    SyncStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.messaging.redis_channels import channel_for_initial_sync
from src.infra.messaging.redis_streams import xadd_json_frame
from src.infra.utils.logger import get_logger
from src.service.integrations.sync_history_manager import datetime_now_naive

InitialSyncBranch = Literal["email", "calendar"]
INITIAL_SYNC_KIND = "email_calendar_initial_sync"
TERMINAL_BRANCH_STATUSES = {"completed", "failed"}
TERMINAL_PARENT_STATUSES = {SyncStatus.SUCCESS, SyncStatus.PARTIAL, SyncStatus.FAILED}
_BRANCHES: set[str] = {"email", "calendar"}


class InitialSyncErrorCode(StrEnum):
    """Stable frontend-safe failure categories for sync branches."""

    DISPATCH_FAILED = "dispatch_failed"
    PROVIDER_FAILED = "provider_failed"
    PROVIDER_TIMED_OUT = "provider_timed_out"
    HYDRATION_FAILED = "hydration_failed"
    RECOVERY_TIMEOUT = "recovery_timeout"
    UNKNOWN = "unknown"


_ERROR_MESSAGES = {
    InitialSyncErrorCode.DISPATCH_FAILED: "This sync could not be started.",
    InitialSyncErrorCode.PROVIDER_FAILED: "The provider sync could not be completed.",
    InitialSyncErrorCode.PROVIDER_TIMED_OUT: "The provider took too long to respond.",
    InitialSyncErrorCode.HYDRATION_FAILED: "Some captured records could not be organized.",
    InitialSyncErrorCode.RECOVERY_TIMEOUT: "Initial sync exceeded the recovery window.",
    InitialSyncErrorCode.UNKNOWN: "This part of the sync could not be completed.",
}

logger = get_logger(__name__)


def _publish_snapshot(snapshot: dict[str, Any]) -> None:
    entry_id = xadd_json_frame(
        channel_for_initial_sync(snapshot["organisation_id"], snapshot["sync_id"]),
        {"event": "snapshot", "data": snapshot},
        maxlen=100,
    )
    if entry_id is None:
        raise RuntimeError("Redis stream publish failed")


class InitialSyncProgressService:
    """Mutate sync history atomically and publish only committed snapshots."""

    @staticmethod
    def new_metadata(requested_branches: list[InitialSyncBranch]) -> dict[str, Any]:
        """Build the initial durable state for the requested provider branches."""
        branches = InitialSyncProgressService._validated_branches(requested_branches)
        return {
            "kind": INITIAL_SYNC_KIND,
            "revision": 0,
            "requested_capabilities": branches,
            "branches": {
                branch: {
                    "status": "queued",
                    "attempt": 0,
                    "records_synced": 0,
                    "hydration_expected": 0,
                    "hydration_completed": 0,
                    "hydration_failed": 0,
                    "error_code": None,
                    "error": None,
                }
                for branch in branches
            },
            "provider_batches": {branch: {} for branch in branches},
            "hydration_chunks": {},
            "metrics": {
                "emails_synced": 0,
                "calendar_events_synced": 0,
                "contacts_created": 0,
                "accounts_created": 0,
                "emails_linked": 0,
            },
        }

    @staticmethod
    async def start_run(
        session: AsyncSession,
        *,
        integration: KnowledgeIntegration,
        requested_branches: list[InitialSyncBranch],
    ) -> UUID:
        """Create and commit the progress row before provider work is dispatched."""
        if not integration.connection_id:
            raise ValueError("Tracked initial sync requires an OAuth attempt ID")
        sync_id = UUID(integration.connection_id)
        record = IntegrationSyncHistory(
            sync_id=sync_id,
            organisation_id=integration.organisation_id,
            integration_id=integration.integration_id,
            started_at=datetime_now_naive(),
            status=SyncStatus.QUEUED,
            sync_metadata=InitialSyncProgressService.new_metadata(requested_branches),
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        InitialSyncProgressService._publish_after_commit(record)
        return sync_id

    @staticmethod
    async def mark_branch_started(
        organisation_id: UUID,
        sync_id: UUID,
        branch: InitialSyncBranch,
        attempt: int,
    ) -> None:
        """Mark a non-terminal provider branch as actively syncing."""
        InitialSyncProgressService._validated_branch(branch)

        def _update(_record: IntegrationSyncHistory, metadata: dict[str, Any]) -> bool:
            branch_state = InitialSyncProgressService._branch_state(metadata, branch)
            if branch_state["status"] in TERMINAL_BRANCH_STATUSES:
                return False
            branch_state["status"] = "syncing"
            branch_state["attempt"] = attempt
            branch_state["error_code"] = None
            branch_state["error"] = None
            return True

        await InitialSyncProgressService._mutate(organisation_id, sync_id, _update)

    @staticmethod
    async def register_hydration(
        organisation_id: UUID,
        sync_id: UUID,
        branch: InitialSyncBranch,
        *,
        records_synced: int,
        chunk_keys: list[str],
    ) -> None:
        """Register stable hydration chunks after the provider data commit."""
        InitialSyncProgressService._validated_branch(branch)
        if records_synced < 0:
            raise ValueError("records_synced cannot be negative")
        if len(chunk_keys) != len(set(chunk_keys)):
            raise ValueError("hydration chunk keys must be unique")

        def _update(_record: IntegrationSyncHistory, metadata: dict[str, Any]) -> bool:
            branch_state = InitialSyncProgressService._branch_state(metadata, branch)
            if branch_state["status"] in TERMINAL_BRANCH_STATUSES:
                return False
            chunks = metadata["hydration_chunks"]
            for chunk_key in chunk_keys:
                chunks.setdefault(chunk_key, "queued")
            provider_batches = metadata.get("provider_batches", {}).get(branch, {})
            authoritative_count = (
                sum(provider_batches.values()) if provider_batches else records_synced
            )
            branch_state["records_synced"] = authoritative_count
            branch_state["hydration_expected"] = len(chunk_keys)
            branch_state["status"] = "hydrating" if chunk_keys else "completed"
            metric = "emails_synced" if branch == "email" else "calendar_events_synced"
            metadata["metrics"][metric] = authoritative_count
            return True

        await InitialSyncProgressService._mutate(organisation_id, sync_id, _update)

    @staticmethod
    async def complete_hydration_chunk(
        organisation_id: UUID,
        sync_id: UUID,
        branch: InitialSyncBranch,
        chunk_key: str,
        *,
        contacts_created: int,
        accounts_created: int,
        emails_linked: int,
    ) -> None:
        """Commit metrics for one hydration chunk in a service-owned transaction."""
        InitialSyncProgressService._validated_branch(branch)
        if min(contacts_created, accounts_created, emails_linked) < 0:
            raise ValueError("hydration metrics cannot be negative")

        await InitialSyncProgressService._mutate(
            organisation_id,
            sync_id,
            InitialSyncProgressService._complete_hydration_update(
                branch,
                chunk_key,
                contacts_created=contacts_created,
                accounts_created=accounts_created,
                emails_linked=emails_linked,
            ),
        )

    @staticmethod
    async def complete_hydration_chunk_in_session(
        session: AsyncSession,
        organisation_id: UUID,
        sync_id: UUID,
        branch: InitialSyncBranch,
        chunk_key: str,
        *,
        contacts_created: int,
        accounts_created: int,
        emails_linked: int,
    ) -> dict[str, Any] | None:
        """Stage chunk metrics in the caller's transaction and return its snapshot.

        Hydration creation flags are true only on the transaction that inserts the
        CRM rows. Joining that transaction prevents a crash/retry from replacing
        those one-time counts with zeros.
        """
        InitialSyncProgressService._validated_branch(branch)
        if min(contacts_created, accounts_created, emails_linked) < 0:
            raise ValueError("hydration metrics cannot be negative")
        return await InitialSyncProgressService._mutate_in_session(
            session,
            organisation_id,
            sync_id,
            InitialSyncProgressService._complete_hydration_update(
                branch,
                chunk_key,
                contacts_created=contacts_created,
                accounts_created=accounts_created,
                emails_linked=emails_linked,
            ),
        )

    @staticmethod
    async def record_provider_batch_in_session(
        session: AsyncSession,
        organisation_id: UUID,
        sync_id: UUID,
        branch: InitialSyncBranch,
        *,
        batch_key: str,
        records_synced: int,
    ) -> dict[str, Any] | None:
        """Stage one committed provider batch in the provider writer transaction.

        Stable batch keys make task redelivery harmless: the first committed count
        remains authoritative even when a retry sees an already-advanced cursor.
        """
        InitialSyncProgressService._validated_branch(branch)
        if records_synced < 0:
            raise ValueError("records_synced cannot be negative")

        def _update(_record: IntegrationSyncHistory, metadata: dict[str, Any]) -> bool:
            batches = metadata.setdefault("provider_batches", {}).setdefault(branch, {})
            if batch_key in batches:
                return False
            batches[batch_key] = records_synced
            branch_state = InitialSyncProgressService._branch_state(metadata, branch)
            branch_state["records_synced"] += records_synced
            if branch_state["status"] == "queued":
                branch_state["status"] = "syncing"
            metric = "emails_synced" if branch == "email" else "calendar_events_synced"
            metadata["metrics"][metric] += records_synced
            return True

        return await InitialSyncProgressService._mutate_in_session(
            session,
            organisation_id,
            sync_id,
            _update,
        )

    @staticmethod
    async def fail_hydration_chunk(
        organisation_id: UUID,
        sync_id: UUID,
        branch: InitialSyncBranch,
        chunk_key: str,
        *,
        error: str,
    ) -> None:
        """Fail one registered hydration chunk using a frontend-safe error."""
        InitialSyncProgressService._validated_branch(branch)

        def _update(_record: IntegrationSyncHistory, metadata: dict[str, Any]) -> bool:
            chunks = metadata["hydration_chunks"]
            if chunk_key not in chunks:
                raise ValueError(f"Unregistered hydration chunk: {chunk_key}")
            if chunks[chunk_key] in TERMINAL_BRANCH_STATUSES:
                return False
            chunks[chunk_key] = "failed"
            branch_state = InitialSyncProgressService._branch_state(metadata, branch)
            error_code, error_message = InitialSyncProgressService._safe_error(error)
            branch_state["error_code"] = error_code
            branch_state["error"] = error_message
            InitialSyncProgressService._refresh_hydration_branch(metadata, branch)
            return True

        await InitialSyncProgressService._mutate(organisation_id, sync_id, _update)

    @staticmethod
    async def fail_branch(
        organisation_id: UUID,
        sync_id: UUID,
        branch: InitialSyncBranch,
        *,
        error: str,
    ) -> None:
        """Fail a non-terminal provider branch using a frontend-safe error."""
        InitialSyncProgressService._validated_branch(branch)

        def _update(_record: IntegrationSyncHistory, metadata: dict[str, Any]) -> bool:
            branch_state = InitialSyncProgressService._branch_state(metadata, branch)
            if branch_state["status"] in TERMINAL_BRANCH_STATUSES:
                return False
            branch_state["status"] = "failed"
            error_code, error_message = InitialSyncProgressService._safe_error(error)
            branch_state["error_code"] = error_code
            branch_state["error"] = error_message
            return True

        await InitialSyncProgressService._mutate(organisation_id, sync_id, _update)

    @staticmethod
    async def terminalize_stale_run(organisation_id: UUID, sync_id: UUID) -> None:
        """Fail every unfinished branch and chunk after the recovery deadline."""

        def _update(_record: IntegrationSyncHistory, metadata: dict[str, Any]) -> bool:
            changed = False
            for branch in metadata["requested_capabilities"]:
                branch_state = metadata["branches"][branch]
                if branch_state["status"] not in TERMINAL_BRANCH_STATUSES:
                    branch_state["status"] = "failed"
                    branch_state["error_code"] = InitialSyncErrorCode.RECOVERY_TIMEOUT.value
                    branch_state["error"] = _ERROR_MESSAGES[InitialSyncErrorCode.RECOVERY_TIMEOUT]
                    changed = True
            for chunk_key, status in list(metadata["hydration_chunks"].items()):
                if status not in TERMINAL_BRANCH_STATUSES:
                    metadata["hydration_chunks"][chunk_key] = "failed"
                    changed = True
            return changed

        await InitialSyncProgressService._mutate(organisation_id, sync_id, _update)

    @staticmethod
    def snapshot(record: IntegrationSyncHistory) -> dict[str, Any]:
        """Serialize the durable row into the versioned frontend event contract."""
        metadata = record.sync_metadata
        duration_ms = None
        if record.completed_at is not None:
            duration_ms = max(
                0, int((record.completed_at - record.started_at).total_seconds() * 1000)
            )
        return {
            "sync_id": str(record.sync_id),
            "organisation_id": str(record.organisation_id),
            "integration_id": str(record.integration_id),
            "status": record.status.value,
            "revision": metadata.get("revision", 0),
            "started_at": f"{record.started_at.isoformat()}Z",
            "completed_at": (
                f"{record.completed_at.isoformat()}Z" if record.completed_at else None
            ),
            "duration_ms": duration_ms,
            "branches": metadata.get("branches", {}),
            "metrics": metadata.get("metrics", {}),
        }

    @staticmethod
    async def _mutate(
        organisation_id: UUID,
        sync_id: UUID,
        update: Callable[[IntegrationSyncHistory, dict[str, Any]], bool],
    ) -> None:
        snapshot = None
        async for session in acquire_org_async_session(organisation_id):
            snapshot = await InitialSyncProgressService._mutate_in_session(
                session,
                organisation_id,
                sync_id,
                update,
            )
            if snapshot is None:
                return
            await session.commit()
            break
        if snapshot is not None:
            InitialSyncProgressService._publish_snapshot_safely(snapshot)

    @staticmethod
    async def _mutate_in_session(
        session: AsyncSession,
        organisation_id: UUID,
        sync_id: UUID,
        update: Callable[[IntegrationSyncHistory, dict[str, Any]], bool],
    ) -> dict[str, Any] | None:
        # The org predicate is the tenant trust boundary; a caller-supplied sync UUID
        # alone must never select another organisation's progress row.
        statement = (
            select(IntegrationSyncHistory)
            .where(
                col(IntegrationSyncHistory.organisation_id) == organisation_id,
                col(IntegrationSyncHistory.sync_id) == sync_id,
            )
            .with_for_update()
        )
        result = await session.execute(statement)
        record = result.scalar_one_or_none()
        if record is None:
            raise LookupError(f"Initial sync run not found: {sync_id}")
        if record.sync_metadata.get("kind") != INITIAL_SYNC_KIND:
            raise ValueError(f"Sync history is not an initial sync run: {sync_id}")
        if record.status in TERMINAL_PARENT_STATUSES:
            return None

        metadata = deepcopy(record.sync_metadata)
        if not update(record, metadata):
            return None
        metadata["revision"] = int(metadata.get("revision", 0)) + 1
        record.sync_metadata = metadata
        InitialSyncProgressService._recompute_parent(record)
        session.add(record)
        return InitialSyncProgressService.snapshot(record)

    @staticmethod
    def _complete_hydration_update(
        branch: InitialSyncBranch,
        chunk_key: str,
        *,
        contacts_created: int,
        accounts_created: int,
        emails_linked: int,
    ) -> Callable[[IntegrationSyncHistory, dict[str, Any]], bool]:
        def _update(_record: IntegrationSyncHistory, metadata: dict[str, Any]) -> bool:
            chunks = metadata["hydration_chunks"]
            if chunk_key not in chunks:
                raise ValueError(f"Unregistered hydration chunk: {chunk_key}")
            if chunks[chunk_key] == "completed":
                return False
            chunks[chunk_key] = "completed"
            metrics = metadata["metrics"]
            metrics["contacts_created"] += contacts_created
            metrics["accounts_created"] += accounts_created
            metrics["emails_linked"] += emails_linked
            InitialSyncProgressService._refresh_hydration_branch(metadata, branch)
            return True

        return _update

    @staticmethod
    def _recompute_parent(record: IntegrationSyncHistory) -> None:
        metadata = record.sync_metadata
        branches = [metadata["branches"][name] for name in metadata["requested_capabilities"]]
        failed_chunks = sum(state == "failed" for state in metadata["hydration_chunks"].values())
        failed_branches = sum(branch["status"] == "failed" for branch in branches)
        record.records_processed = (
            metadata["metrics"]["emails_synced"] + metadata["metrics"]["calendar_events_synced"]
        )
        record.records_failed = failed_branches + failed_chunks

        if not all(branch["status"] in TERMINAL_BRANCH_STATUSES for branch in branches):
            record.status = SyncStatus.IN_PROGRESS
            record.completed_at = None
            return

        completed = sum(branch["status"] == "completed" for branch in branches)
        record.status = (
            SyncStatus.SUCCESS
            if completed == len(branches)
            else SyncStatus.PARTIAL
            if completed
            else SyncStatus.FAILED
        )
        record.completed_at = record.completed_at or datetime_now_naive()
        errors = {
            name: metadata["branches"][name]["error"]
            for name in metadata["requested_capabilities"]
            if metadata["branches"][name]["error"]
        }
        record.error_details = {"branches": errors} if errors else None

    @staticmethod
    def _refresh_hydration_branch(metadata: dict[str, Any], branch: str) -> None:
        branch_state = InitialSyncProgressService._branch_state(metadata, branch)
        prefix = f"{branch}:"
        states = [
            state for key, state in metadata["hydration_chunks"].items() if key.startswith(prefix)
        ]
        branch_state["hydration_completed"] = sum(state == "completed" for state in states)
        branch_state["hydration_failed"] = sum(state == "failed" for state in states)
        if states and all(state in TERMINAL_BRANCH_STATUSES for state in states):
            branch_state["status"] = "failed" if "failed" in states else "completed"

    @staticmethod
    def _branch_state(metadata: dict[str, Any], branch: str) -> dict[str, Any]:
        try:
            return metadata["branches"][branch]
        except KeyError as error:
            raise ValueError(f"Branch was not requested: {branch}") from error

    @staticmethod
    def _validated_branches(branches: list[InitialSyncBranch]) -> list[InitialSyncBranch]:
        if not branches or len(branches) != len(set(branches)):
            raise ValueError("At least one unique initial sync branch is required")
        for branch in branches:
            InitialSyncProgressService._validated_branch(branch)
        return list(branches)

    @staticmethod
    def _validated_branch(branch: str) -> None:
        if branch not in _BRANCHES:
            raise ValueError(f"Unsupported initial sync branch: {branch}")

    @staticmethod
    def _safe_error(error: str | InitialSyncErrorCode) -> tuple[str, str]:
        try:
            code = InitialSyncErrorCode(error)
        except ValueError:
            code = InitialSyncErrorCode.UNKNOWN
        return code.value, _ERROR_MESSAGES[code]

    @staticmethod
    def _publish_after_commit(record: IntegrationSyncHistory) -> None:
        InitialSyncProgressService._publish_snapshot_safely(
            InitialSyncProgressService.snapshot(record)
        )

    @staticmethod
    def _publish_snapshot_safely(snapshot: dict[str, Any]) -> None:
        try:
            _publish_snapshot(snapshot)
        except Exception:
            # copilotgtm: Redis is live transport only; PostgreSQL remains terminal truth.
            logger.warning(
                "Could not publish initial sync snapshot: sync_id=%s revision=%s",
                snapshot["sync_id"],
                snapshot["revision"],
                exc_info=True,
            )

    @staticmethod
    def publish_committed_snapshot(snapshot: dict[str, Any] | None) -> None:
        """Publish a snapshot only after its caller-owned database commit succeeds."""
        if snapshot is not None:
            InitialSyncProgressService._publish_snapshot_safely(snapshot)


__all__ = [
    "INITIAL_SYNC_KIND",
    "InitialSyncErrorCode",
    "InitialSyncBranch",
    "InitialSyncProgressService",
]
