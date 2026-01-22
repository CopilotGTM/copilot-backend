"""Service for creating and updating internal CRM deals."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
    CRMPipeline,
)
from src.infra.utils.logger import get_logger
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.service.crm.internal.internal_crm_user_service import InternalCRMUserService

logger = get_logger(__name__)


class DealWriteServiceError(Exception):
    """Custom exception for deal write errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealWriteService:
    """Write operations for internal CRM deals."""

    def __init__(
        self,
        org_session: AsyncSession,
        master_session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
    ):
        """Initialize the service with both org and master sessions.

        Args:
            org_session: Org-scoped session for CRM data writes.
            master_session: Master session for reading public schema data (users).
            organisation_id: Organisation UUID.
            actor_user_id: User UUID performing the action.
        """
        self.session = org_session  # Primary session for org schema operations
        self.master_session = master_session  # For public schema reads
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            logger.debug("DealWriteService close exception", exc_info=True)

    async def create_deal(self, payload: dict[str, Any]) -> CRMDeal:
        """Create a new internal CRM deal.

        Args:
            payload: Deal creation data including name, amount, stage_id, etc.

        Returns:
            Created CRMDeal record

        Raises:
            DealWriteServiceError: If validation fails or internal CRM not configured
        """
        # Get internal CRM integration
        setup_service = InternalCRMSetupService(
            self.session, self.organisation_id, self.actor_user_id
        )
        integration = await setup_service.get_internal_integration()
        if not integration:
            raise DealWriteServiceError("Internal CRM is not configured", status_code=409)

        integration_id = integration.integration_id

        # Validate and get pipeline + stage
        pipeline_id = payload.get("pipeline_id")
        stage_id = payload.get("stage_id")

        if pipeline_id:
            pipeline = await self._load_pipeline(pipeline_id, integration_id)
        else:
            # Use default pipeline
            pipeline = await self._get_default_pipeline(integration_id)
            if not pipeline:
                raise DealWriteServiceError("No default pipeline found", status_code=500)
            pipeline_id = pipeline.pipeline_id

        if stage_id:
            stage = await self._load_stage(stage_id, pipeline_id, integration_id)
        else:
            # Use first stage of pipeline
            stage = await self._get_first_stage(pipeline_id)
            if not stage:
                raise DealWriteServiceError("No stages found in pipeline", status_code=500)
            stage_id = stage.stage_id

        # Handle owner assignment
        owner_user_id = payload.get("owner_user_id")
        owner_id = None
        owner_external_id = None
        owner_name = None
        owner_email = None

        if owner_user_id:
            user_service = InternalCRMUserService(
                org_session=self.session,
                master_session=self.master_session,
                organisation_id=self.organisation_id,
                actor_user_id=self.actor_user_id,
            )
            crm_user = await user_service.ensure_crm_user(owner_user_id)
            owner_id = crm_user.user_id
            owner_external_id = crm_user.external_id
            owner_name = crm_user.full_name
            owner_email = crm_user.email

        # Create deal
        now = datetime.now(UTC)
        deal_id = uuid4()

        deal = CRMDeal(
            deal_id=deal_id,
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            external_id=str(deal_id),  # Use UUID as external_id for internal deals
            name=payload.get("name"),
            amount=payload.get("amount"),
            currency=payload.get("currency"),
            pipeline_id=pipeline_id,
            pipeline_external_id=pipeline.external_id,
            pipeline_name=pipeline.name,
            stage_id=stage_id,
            stage_external_id=stage.external_id,
            stage_name=stage.name,
            deal_status=CRMDealStatus.OPEN,
            owner_id=owner_id,
            owner_external_id=owner_external_id,
            owner_name=owner_name,
            owner_email=owner_email,
            close_date=payload.get("close_date"),
            expected_close_date=payload.get("expected_close_date"),
            description=payload.get("description"),
            deal_type=payload.get("deal_type"),
            priority=payload.get("priority"),
            lead_source=payload.get("lead_source"),
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={"is_internal_crm": True, "created_via": "api"},
        )

        self.session.add(deal)
        await self.session.flush()

        # Create initial stage history record
        history_id = uuid4()
        history = CRMDealStageHistory(
            history_id=history_id,
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            external_id=str(history_id),  # Use UUID as external_id for internal CRM
            deal_id=deal.deal_id,
            stage_id=stage.stage_id,
            stage_external_id=stage.external_id,
            entered_at=now,
            synced_at=now,
            last_seen_at=now,
        )
        self.session.add(history)
        await self.session.flush()

        logger.info("Created internal CRM deal %s: %s", deal.deal_id, deal.name)
        return deal

    async def update_deal(self, deal_id: UUID, payload: dict[str, Any]) -> CRMDeal:
        """Update an internal CRM deal.

        Args:
            deal_id: Deal UUID
            payload: Fields to update (only provided fields are updated)

        Returns:
            Updated CRMDeal record

        Raises:
            DealWriteServiceError: If deal not found, read-only, or validation fails
        """
        if not payload:
            raise DealWriteServiceError("No fields to update", status_code=400)

        deal = await self._load_internal_deal(deal_id)

        # Update allowed fields
        updated = False
        if "name" in payload and payload["name"]:
            deal.name = payload["name"]
            updated = True
        if "amount" in payload:
            deal.amount = payload["amount"]
            updated = True
        if "currency" in payload:
            deal.currency = payload["currency"]
            updated = True
        if "close_date" in payload:
            deal.close_date = payload["close_date"]
            updated = True
        if "expected_close_date" in payload:
            deal.expected_close_date = payload["expected_close_date"]
            updated = True
        if "description" in payload:
            deal.description = payload["description"]
            updated = True
        if "deal_type" in payload:
            deal.deal_type = payload["deal_type"]
            updated = True
        if "priority" in payload:
            deal.priority = payload["priority"]
            updated = True
        if "lead_source" in payload:
            deal.lead_source = payload["lead_source"]
            updated = True

        if not updated:
            raise DealWriteServiceError("No valid fields to update", status_code=400)

        now = datetime.now(UTC)
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now
        properties = deal.properties or {}
        properties["updated_via"] = "api"
        deal.properties = properties

        await self.session.flush()
        logger.info("Updated internal CRM deal %s", deal.deal_id)
        return deal

    async def delete_deal(self, deal_id: UUID) -> CRMDeal:
        """Soft delete an internal CRM deal.

        Args:
            deal_id: Deal UUID

        Returns:
            Deleted CRMDeal record

        Raises:
            DealWriteServiceError: If deal not found or read-only
        """
        deal = await self._load_internal_deal(deal_id)

        now = datetime.now(UTC)
        deal.is_deleted = True
        deal.deal_status = CRMDealStatus.CLOSED
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now
        properties = deal.properties or {}
        properties["deleted_via"] = "api"
        deal.properties = properties

        await self.session.flush()
        logger.info("Deleted internal CRM deal %s", deal.deal_id)
        return deal

    async def move_stage(self, deal_id: UUID, stage_id: UUID) -> CRMDeal:
        """Move a deal to a new stage and record stage history.

        Args:
            deal_id: Deal UUID
            stage_id: New stage UUID

        Returns:
            Updated CRMDeal record

        Raises:
            DealWriteServiceError: If deal or stage not found, or validation fails
        """
        deal = await self._load_internal_deal(deal_id)

        # Verify stage belongs to same integration and pipeline
        if not deal.pipeline_id:
            raise DealWriteServiceError("Deal has no pipeline", status_code=500)
        stage = await self._load_stage(stage_id, deal.pipeline_id, deal.integration_id)

        # Update deal stage
        now = datetime.now(UTC)
        deal.stage_id = stage.stage_id
        deal.stage_external_id = stage.external_id
        deal.stage_name = stage.name
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now

        # Record stage history
        history_id = uuid4()
        history = CRMDealStageHistory(
            history_id=history_id,
            organisation_id=self.organisation_id,
            integration_id=deal.integration_id,
            external_id=str(history_id),  # Use UUID as external_id for internal CRM
            deal_id=deal.deal_id,
            stage_id=stage.stage_id,
            stage_external_id=stage.external_id,
            entered_at=now,
            synced_at=now,
            last_seen_at=now,
        )
        self.session.add(history)
        await self.session.flush()

        logger.info("Moved deal %s to stage %s", deal.deal_id, stage.name)
        return deal

    async def assign_owner(self, deal_id: UUID, owner_user_id: UUID | None) -> CRMDeal:
        """Assign or clear the owner for an internal CRM deal.

        Args:
            deal_id: Deal UUID
            owner_user_id: User UUID to assign as owner, or None to clear

        Returns:
            Updated CRMDeal record

        Raises:
            DealWriteServiceError: If deal or user not found, or validation fails
        """
        deal = await self._load_internal_deal(deal_id)

        if owner_user_id:
            # Ensure CRM user exists
            user_service = InternalCRMUserService(
                org_session=self.session,
                master_session=self.master_session,
                organisation_id=self.organisation_id,
                actor_user_id=self.actor_user_id,
            )
            crm_user = await user_service.ensure_crm_user(owner_user_id)

            deal.owner_id = crm_user.user_id
            deal.owner_external_id = crm_user.external_id
            deal.owner_name = crm_user.full_name
            deal.owner_email = crm_user.email
        else:
            # Clear owner
            deal.owner_id = None
            deal.owner_external_id = None
            deal.owner_name = None
            deal.owner_email = None

        now = datetime.now(UTC)
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now

        await self.session.flush()
        logger.info(
            "Assigned owner %s to deal %s",
            owner_user_id or "None",
            deal.deal_id,
        )
        return deal

    async def _load_internal_deal(self, deal_id: UUID) -> CRMDeal:
        """Load and verify a deal belongs to this org and is internal CRM."""
        deal = await self.session.get(CRMDeal, deal_id)
        if not deal or deal.organisation_id != self.organisation_id:
            raise DealWriteServiceError("Deal not found", status_code=404)
        if deal.is_deleted:
            raise DealWriteServiceError("Deal not found", status_code=404)

        # Check if deal is from internal CRM
        setup_service = InternalCRMSetupService(
            self.session, self.organisation_id, self.actor_user_id
        )
        integration = await setup_service.get_internal_integration()
        if not integration or deal.integration_id != integration.integration_id:
            raise DealWriteServiceError("External CRM deals are read-only", status_code=409)

        return deal

    async def _load_pipeline(self, pipeline_id: UUID, integration_id: UUID) -> CRMPipeline:
        """Load and verify a pipeline belongs to this org and integration."""
        pipeline = await self.session.get(CRMPipeline, pipeline_id)
        if (
            not pipeline
            or pipeline.organisation_id != self.organisation_id
            or pipeline.integration_id != integration_id
        ):
            raise DealWriteServiceError("Pipeline not found", status_code=404)
        if pipeline.is_deleted:
            raise DealWriteServiceError("Pipeline not found", status_code=404)
        return pipeline

    async def _load_stage(
        self, stage_id: UUID, pipeline_id: UUID, integration_id: UUID
    ) -> CRMDealStage:
        """Load and verify a stage belongs to this org, pipeline, and integration."""
        stage = await self.session.get(CRMDealStage, stage_id)
        if (
            not stage
            or stage.organisation_id != self.organisation_id
            or stage.pipeline_id != pipeline_id
            or stage.integration_id != integration_id
        ):
            raise DealWriteServiceError("Stage not found", status_code=404)
        if stage.is_deleted:
            raise DealWriteServiceError("Stage not found", status_code=404)
        return stage

    async def _get_default_pipeline(self, integration_id: UUID) -> CRMPipeline | None:
        """Get the default pipeline for the internal CRM integration."""
        stmt = (
            select(CRMPipeline)
            .where(
                col(CRMPipeline.organisation_id) == self.organisation_id,
                col(CRMPipeline.integration_id) == integration_id,
                col(CRMPipeline.is_deleted).is_not(True),
            )
            .order_by(col(CRMPipeline.display_order).asc(), col(CRMPipeline.created_at).asc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_first_stage(self, pipeline_id: UUID) -> CRMDealStage | None:
        """Get the first stage of a pipeline."""
        stmt = (
            select(CRMDealStage)
            .where(
                col(CRMDealStage.pipeline_id) == pipeline_id,
                col(CRMDealStage.organisation_id) == self.organisation_id,
                col(CRMDealStage.is_deleted).is_not(True),
            )
            .order_by(col(CRMDealStage.display_order).asc(), col(CRMDealStage.created_at).asc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
