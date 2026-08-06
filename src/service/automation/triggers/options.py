"""Organization-scoped CRM options used by trigger catalog and validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables import AdminUserStatus, SupabaseUserStatus, User
from src.domain.models.organisation_tables import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMDealStage,
    CRMPipeline,
    CRMUser,
)
from src.service.automation.triggers.crm_schema import active_custom_field_options
from src.service.automation.triggers.direct import ManualInvocationAccess, ManualSubject

if TYPE_CHECKING:
    from src.api.schemas.automation_trigger_models import TriggerCatalogPipelineOption


class AutomationTriggerOptionAccess:
    """Read only non-deleted CRM metadata inside one organization."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def list_pipelines(self) -> list[TriggerCatalogPipelineOption]:
        """Return pipelines and stages for trigger-builder option rendering."""

        from src.api.schemas.automation_trigger_models import (
            TriggerCatalogPipelineOption,
            TriggerCatalogStageOption,
        )

        pipeline_columns = cast(Any, CRMPipeline).__table__.c
        stage_columns = cast(Any, CRMDealStage).__table__.c
        pipelines = list(
            (
                await self.session.execute(
                    select(CRMPipeline)
                    .where(
                        pipeline_columns.organisation_id == self.organisation_id,
                        pipeline_columns.is_deleted.is_(False),
                    )
                    .order_by(pipeline_columns.display_order, pipeline_columns.name)
                )
            )
            .scalars()
            .all()
        )
        stages = list(
            (
                await self.session.execute(
                    select(CRMDealStage)
                    .where(
                        stage_columns.organisation_id == self.organisation_id,
                        stage_columns.is_deleted.is_(False),
                    )
                    .order_by(
                        stage_columns.pipeline_id,
                        stage_columns.display_order,
                        stage_columns.name,
                    )
                )
            )
            .scalars()
            .all()
        )
        stages_by_pipeline: dict[UUID, list[TriggerCatalogStageOption]] = {}
        for stage in stages:
            stages_by_pipeline.setdefault(stage.pipeline_id, []).append(
                TriggerCatalogStageOption(
                    stage_id=stage.stage_id,
                    label=stage.label or stage.name,
                    stage_state=getattr(stage.stage_state, "value", stage.stage_state),
                    is_active=stage.is_active,
                )
            )
        return [
            TriggerCatalogPipelineOption(
                pipeline_id=pipeline.pipeline_id,
                label=pipeline.label or pipeline.name,
                is_active=pipeline.is_active,
                stages=stages_by_pipeline.get(pipeline.pipeline_id, []),
            )
            for pipeline in pipelines
        ]

    async def validate_crm_selection(self, pipeline_ids: set[UUID], stage_ids: set[UUID]) -> None:
        """Reject foreign, deleted, or cross-pipeline CRM identifiers."""

        pipeline_columns = cast(Any, CRMPipeline).__table__.c
        stage_columns = cast(Any, CRMDealStage).__table__.c
        if pipeline_ids:
            found_pipelines = set(
                (
                    await self.session.execute(
                        select(pipeline_columns.pipeline_id).where(
                            pipeline_columns.organisation_id == self.organisation_id,
                            pipeline_columns.is_deleted.is_(False),
                            pipeline_columns.pipeline_id.in_(pipeline_ids),
                        )
                    )
                ).scalars()
            )
            if found_pipelines != pipeline_ids:
                raise ValueError("Unknown CRM pipeline")
        if stage_ids:
            rows = (
                await self.session.execute(
                    select(stage_columns.stage_id, stage_columns.pipeline_id).where(
                        stage_columns.organisation_id == self.organisation_id,
                        stage_columns.is_deleted.is_(False),
                        stage_columns.stage_id.in_(stage_ids),
                    )
                )
            ).all()
            if {row.stage_id for row in rows} != stage_ids:
                raise ValueError("Unknown CRM stage")
            if pipeline_ids and any(row.pipeline_id not in pipeline_ids for row in rows):
                raise ValueError("CRM stages must belong to the selected pipeline")

    async def validate_option_selection(self, source: str, values: set[str]) -> None:
        """Validate stable registry option IDs against current tenant-owned rows."""

        if not values:
            return
        if source == "deal_statuses":
            if values - {"open", "won", "lost", "closed"}:
                raise ValueError("Unknown deal status")
            return
        if source.startswith("custom_field:"):
            await self._validate_custom_field_options(source, values)
            return
        try:
            identifiers = {UUID(value) for value in values}
        except ValueError as exc:
            raise ValueError(f"{source} values must be UUIDs") from exc
        if source == "pipelines":
            await self.validate_crm_selection(identifiers, set())
            return
        if source == "pipeline_stages":
            await self.validate_crm_selection(set(), identifiers)
            return
        if source == "crm_users":
            found = set(
                (
                    await self.session.execute(
                        select(col(CRMUser.user_id)).where(
                            col(CRMUser.organisation_id) == self.organisation_id,
                            col(CRMUser.is_deleted).is_(False),
                            col(CRMUser.user_id).in_(identifiers),
                        )
                    )
                ).scalars()
            )
            if found != identifiers:
                raise ValueError("Unknown CRM user")
            return
        raise ValueError(f"Unsupported trigger option source: {source}")

    async def _validate_custom_field_options(self, source: str, values: set[str]) -> None:
        try:
            field_id = UUID(source.removeprefix("custom_field:"))
        except ValueError as exc:
            raise ValueError("Invalid custom field option source") from exc
        columns = cast(Any, CRMCustomFieldDefinition).__table__.c
        field = (
            (
                await self.session.execute(
                    select(CRMCustomFieldDefinition).where(
                        columns.organisation_id == self.organisation_id,
                        columns.field_id == field_id,
                        columns.entity_type == CRMCustomFieldEntity.DEAL.value,
                        columns.is_active.is_(True),
                        columns.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .first()
        )
        if field is None or getattr(field.data_type, "value", field.data_type) not in {
            CRMCustomFieldDataType.SELECT.value,
            CRMCustomFieldDataType.MULTISELECT.value,
        }:
            raise ValueError("Unknown custom field option source")
        allowed = {value for value, _label in active_custom_field_options(field.options)}
        if not values <= allowed:
            raise ValueError("Unknown custom field option")

    async def validate_audience_user_ids(self, user_ids: set[UUID]) -> None:
        """Require every explicit audience member to be active in this organization."""

        if not user_ids:
            return
        found = set(
            (
                await self.session.execute(
                    select(col(User.user_id)).where(
                        col(User.organisation_id) == self.organisation_id,
                        col(User.user_id).in_(user_ids),
                        col(User.status) == SupabaseUserStatus.ACTIVE,
                        col(User.admin_status) == AdminUserStatus.APPROVED,
                    )
                )
            ).scalars()
        )
        if found != user_ids:
            raise ValueError("Unknown or inactive audience user")

    async def validate_schedule_subjects(
        self,
        subject_type: str,
        subject_ids: set[UUID],
        *,
        principal_id: UUID,
        organisation_scope: bool,
    ) -> None:
        """Require every scheduled record to exist inside its execution scope."""

        subjects = tuple(ManualSubject(cast(Any, subject_type), item) for item in subject_ids)
        records = await ManualInvocationAccess(
            self.session,
            self.organisation_id,
        ).load_subject_records(
            subjects,
            principal_id=principal_id,
            organisation_scope=organisation_scope,
        )
        if set(records) != {subject.identity for subject in subjects}:
            raise ValueError("Unknown or unavailable scheduled subject")
