"""Service for initializing internal CRM integration and default pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import IntegrationScope
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealStage,
    CRMPipeline,
    CRMStageState,
)
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.utils.logger import get_logger
from src.service.crm.utils.pipeline_shape import mark_pipeline_usability

logger = get_logger(__name__)


DEFAULT_PIPELINE_NAME = "Default Pipeline"
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

# The built-in pipeline is intentionally complete, so a newly provisioned org
# can create, close, and reopen deals before any custom pipeline setup exists.
DEFAULT_STAGES: list[dict[str, Any]] = [
    {
        "name": "Qualification",
        "label": "Qualification",
        "stage_key": "qualification",
        "stage_state": CRMStageState.OPEN,
        "probability": 10.0,
        "is_closed": False,
        "display_order": 1,
    },
    {
        "name": "Discovery",
        "label": "Discovery",
        "stage_key": "discovery",
        "stage_state": CRMStageState.OPEN,
        "probability": 25.0,
        "is_closed": False,
        "display_order": 2,
    },
    {
        "name": "Proposal",
        "label": "Proposal",
        "stage_key": "proposal",
        "stage_state": CRMStageState.OPEN,
        "probability": 50.0,
        "is_closed": False,
        "display_order": 3,
    },
    {
        "name": "Negotiation",
        "label": "Negotiation",
        "stage_key": "negotiation",
        "stage_state": CRMStageState.OPEN,
        "probability": 75.0,
        "is_closed": False,
        "display_order": 4,
    },
    {
        "name": "Closed Won",
        "label": "Closed Won",
        "stage_key": "closed_won",
        "stage_state": CRMStageState.WON,
        "probability": 100.0,
        "is_closed": True,
        "display_order": 5,
    },
    {
        "name": "Closed Lost",
        "label": "Closed Lost",
        "stage_key": "closed_lost",
        "stage_state": CRMStageState.LOST,
        "probability": 0.0,
        "is_closed": True,
        "display_order": 6,
    },
]


@dataclass
class InternalCRMSetupResult:
    """Provisioning snapshot returned after ensuring internal CRM records exist."""

    integration: KnowledgeIntegration
    pipeline: CRMPipeline
    stages: list[CRMDealStage]


class InternalCRMSetupService:
    """Provision internal CRM integration and default pipeline/stages."""

    def __init__(self, session: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def setup_internal_crm(self, *, commit: bool = True) -> InternalCRMSetupResult:
        """Ensure internal CRM integration and default pipeline exist.

        Called during org creation (organization_management) and by the
        ensure_internal_crm_setup maintenance script. Creates the integration
        and seeds a default pipeline when one does not exist. Existing pipeline
        stages are user-owned configuration and are never repaired by name.
        """
        integration = await self._ensure_internal_integration()
        pipeline, stages = await self._ensure_default_pipeline(integration.integration_id)
        if commit:
            await self.session.commit()
        return InternalCRMSetupResult(integration=integration, pipeline=pipeline, stages=stages)

    async def get_status(self) -> InternalCRMSetupResult | None:
        """Return the existing default pipeline + its stages, or None if not configured.

        Read-only: does NOT create missing records (create_missing=False).
        Used by CRM setup callers to load the default pipeline context.
        """
        integration = await self._get_internal_integration()
        if not integration:
            return None

        try:
            pipeline, stages = await self._ensure_default_pipeline(
                integration.integration_id, create_missing=False
            )
        except ValueError:
            return None

        return InternalCRMSetupResult(integration=integration, pipeline=pipeline, stages=stages)

    async def get_internal_integration(self) -> KnowledgeIntegration | None:
        """Return the internal CRM integration record, or None if not set up."""
        return await self._get_internal_integration()

    async def get_or_create_internal_integration(self) -> KnowledgeIntegration:
        """Return the internal CRM integration, creating it when absent."""
        return await self._ensure_internal_integration()

    async def _get_internal_integration(self) -> KnowledgeIntegration | None:
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == self.organisation_id,
            col(KnowledgeIntegration.integration_type) == IntegrationType.INTERNAL_CRM.value,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _ensure_internal_integration(self) -> KnowledgeIntegration:
        existing = await self._get_internal_integration()
        if existing:
            return existing

        integration = KnowledgeIntegration(
            organisation_id=self.organisation_id,
            created_by_user_id=self.user_id,
            created_by_admin=False,
            integration_type=IntegrationType.INTERNAL_CRM,
            integration_scope=IntegrationScope.ORGANIZATION,
            name="Copilot CRM",
            credentials={},
            auto_sync_enabled=False,
            sync_frequency="manual",
            connection_metadata={"source": "internal_crm", "internal": True},
            is_active=True,
        )

        self.session.add(integration)
        await self.session.flush()
        await self.session.refresh(integration)
        logger.info(
            "Created internal CRM integration for org=%s integration=%s",
            self.organisation_id,
            integration.integration_id,
        )
        return integration

    async def _ensure_default_pipeline(
        self, integration_id: UUID, *, create_missing: bool = True
    ) -> tuple[CRMPipeline, list[CRMDealStage]]:
        stmt = select(CRMPipeline).where(
            col(CRMPipeline.organisation_id) == self.organisation_id,
            col(CRMPipeline.integration_id) == integration_id,
            col(CRMPipeline.is_default).is_(True),
            col(CRMPipeline.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        pipeline = result.scalars().first()

        now = utcnow()
        pipeline_was_created = False
        if not pipeline and create_missing:
            pipeline_id = uuid4()
            pipeline = CRMPipeline(
                pipeline_id=pipeline_id,
                organisation_id=self.organisation_id,
                integration_id=integration_id,
                name=DEFAULT_PIPELINE_NAME,
                label=DEFAULT_PIPELINE_NAME,
                object_type="deals",
                is_default=True,
                is_active=True,
                display_order=0,
                synced_at=now,
                last_seen_at=now,
                # Default stages are created from DEFAULT_STAGES, which includes
                # open/won/lost states; deal writes may use this pipeline immediately.
                properties={"source": "internal_crm", "usable_for_internal_deals": True},
            )
            self.session.add(pipeline)
            await self.session.flush()
            await self.session.refresh(pipeline)
            pipeline_was_created = True

        if not pipeline:
            raise ValueError("Internal CRM pipeline not found")

        stages: list[CRMDealStage] = []
        if not create_missing:
            stages = await self._load_pipeline_stages(pipeline.pipeline_id)
            return pipeline, stages

        if pipeline_was_created:
            for stage_def in DEFAULT_STAGES:
                stage = CRMDealStage(
                    stage_id=uuid4(),
                    organisation_id=self.organisation_id,
                    integration_id=integration_id,
                    name=stage_def["name"],
                    label=stage_def["label"],
                    stage_key=stage_def["stage_key"],
                    stage_state=stage_def["stage_state"],
                    probability=stage_def["probability"],
                    forecast_category=None,
                    is_closed=stage_def["is_closed"],
                    display_order=stage_def["display_order"],
                    pipeline_id=pipeline.pipeline_id,
                    synced_at=now,
                    last_seen_at=now,
                )
                self.session.add(stage)
                stages.append(stage)
        else:
            # A default flag selects where implicit deal writes go; it does not
            # transfer ownership of that pipeline's stage configuration to setup.
            stages = await self._load_pipeline_stages(pipeline.pipeline_id)

        if stages:
            await self.session.flush()

        # Existing orgs may have the default rows but not the newer usability
        # metadata; setup/repair is the one place that should normalize them.
        mark_pipeline_usability(pipeline, stages)
        await self.session.flush()

        return pipeline, stages

    async def get_all_pipelines(self, integration_id: UUID) -> list[CRMPipeline]:
        """Return all active pipelines for the given integration."""
        stmt = (
            select(CRMPipeline)
            .where(
                col(CRMPipeline.organisation_id) == self.organisation_id,
                col(CRMPipeline.integration_id) == integration_id,
                col(CRMPipeline.is_deleted).is_(False),
            )
            .order_by(col(CRMPipeline.display_order).asc().nulls_last())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_all_stages(self, integration_id: UUID) -> list[CRMDealStage]:
        """Return all active stages for the given integration."""
        stmt = select(CRMDealStage).where(
            col(CRMDealStage.organisation_id) == self.organisation_id,
            col(CRMDealStage.integration_id) == integration_id,
            col(CRMDealStage.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def _load_pipeline_stages(self, pipeline_id: UUID) -> list[CRMDealStage]:
        stmt = select(CRMDealStage).where(
            col(CRMDealStage.organisation_id) == self.organisation_id,
            col(CRMDealStage.pipeline_id) == pipeline_id,
            col(CRMDealStage.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
