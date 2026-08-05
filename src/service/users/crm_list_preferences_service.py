"""Persistence for CRM collection sorting stored in the shared user document."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.crm_list_preference_models import (
    CRMColumnEntity,
    CRMListColumn,
    CRMListConfiguration,
    CRMListEntity,
    CRMListSortingPreferences,
    UpdateCRMListColumnsRequest,
    UpdateCRMListSortingRequest,
)
from src.domain.models.master_tables.user_models import User
from src.service.crm.schema.deal_schema_service import DealSchemaService
from src.service.users.crm_list_column_catalog import (
    DEFAULT_COLUMNS,
    REQUIRED_COLUMNS,
    standard_columns,
)
from src.service.users.user_preferences import (
    CRM_LIST_SORT_FIELDS,
    merge_crm_list_columns,
    merge_crm_list_sorting,
)


class CRMListPreferencesService:
    """Persist one authenticated user's sorting without replacing other settings."""

    def __init__(
        self,
        master_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
        org_session: AsyncSession | None = None,
    ) -> None:
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.org_session = org_session

    async def get_configuration(self, entity: CRMColumnEntity) -> CRMListConfiguration:
        """Return one reconciled catalog with selected columns first."""
        user = await self._get_current_user_row()
        catalog = await self._column_catalog(entity)
        list_preferences = self._list_preferences(user.preferences, entity)
        selected = self._reconcile_columns(
            list_preferences.get("columns"),
            catalog,
            entity,
        )
        return self._configuration(entity, catalog, selected, list_preferences)

    async def update_columns(
        self,
        entity: CRMColumnEntity,
        request: UpdateCRMListColumnsRequest,
    ) -> CRMListConfiguration:
        """Validate and persist one complete ordered visible-column selection."""
        catalog = await self._column_catalog(entity)
        selected = request.columns
        supported = {column.id for column in catalog}
        if len(selected) != len(set(selected)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Column selection contains duplicates",
            )
        if not set(selected) <= supported:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported {entity} column",
            )
        missing_required = [
            column_id for column_id in REQUIRED_COLUMNS[entity] if column_id not in selected
        ]
        if missing_required:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{missing_required[0]} is required",
            )

        user = await self._get_current_user_row(lock=True)
        user.preferences = merge_crm_list_columns(user.preferences, entity, selected)
        await self.master_session.commit()
        await self.master_session.refresh(user)
        return self._configuration(
            entity,
            catalog,
            selected,
            self._list_preferences(user.preferences, entity),
        )

    async def update_sorting(
        self,
        entity: CRMListEntity,
        request: UpdateCRMListSortingRequest,
    ) -> CRMListSortingPreferences:
        """Validate and save the selected sorting for one CRM collection."""
        if request.sort_by not in CRM_LIST_SORT_FIELDS[entity]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported {entity} sort field",
            )

        user = await self._get_current_user_row(lock=True)
        user.preferences = merge_crm_list_sorting(
            user.preferences,
            entity,
            request.sort_by,
            request.sort_order,
        )
        await self.master_session.commit()
        await self.master_session.refresh(user)
        return CRMListSortingPreferences.model_validate(request.model_dump())

    async def _column_catalog(self, entity: CRMColumnEntity) -> list[CRMListColumn]:
        columns = standard_columns(entity)
        if entity != "deals":
            return columns
        if self.org_session is None:
            raise RuntimeError("Organisation session is required for deal columns")

        schema = await DealSchemaService(self.org_session, self.organisation_id).get_schema()
        columns.extend(
            CRMListColumn(
                id=f"custom_columns.{field.key}",
                label=field.label,
                data_type=field.type,
                source="custom",
                value_path=f"custom_columns.{field.key}",
                visible=False,
                default_visible=False,
                position=len(columns) + index,
                sortable=False,
                hideable=True,
            )
            for index, field in enumerate(schema.fields)
            if field.source == "internal"
        )
        return columns

    @staticmethod
    def _list_preferences(preferences: object, entity: CRMColumnEntity) -> dict:
        root = preferences if isinstance(preferences, dict) else {}
        crm = root.get("crm") if isinstance(root.get("crm"), dict) else {}
        entity_preferences = crm.get(entity) if isinstance(crm.get(entity), dict) else {}
        branch = entity_preferences.get("list")
        return branch if isinstance(branch, dict) else {}

    @staticmethod
    def _reconcile_columns(
        saved: object,
        catalog: list[CRMListColumn],
        entity: CRMColumnEntity,
    ) -> list[str]:
        supported = {column.id for column in catalog}
        if not isinstance(saved, list) or not all(isinstance(item, str) for item in saved):
            return [column for column in DEFAULT_COLUMNS[entity] if column in supported]
        selected = list(dict.fromkeys(column for column in saved if column in supported))
        if not selected:
            return [column for column in DEFAULT_COLUMNS[entity] if column in supported]
        positions = {column.id: index for index, column in enumerate(catalog)}
        for required in REQUIRED_COLUMNS[entity]:
            if required not in selected:
                insert_at = next(
                    (
                        index
                        for index, column in enumerate(selected)
                        if positions[column] > positions[required]
                    ),
                    len(selected),
                )
                selected.insert(insert_at, required)
        return selected

    @staticmethod
    def _configuration(
        entity: CRMColumnEntity,
        catalog: list[CRMListColumn],
        selected: list[str],
        preferences: dict,
    ) -> CRMListConfiguration:
        by_id = {column.id: column for column in catalog}
        ordered = [by_id[column_id] for column_id in selected]
        ordered.extend(column for column in catalog if column.id not in selected)
        columns = [
            column.model_copy(update={"visible": column.id in selected, "position": position})
            for position, column in enumerate(ordered)
        ]
        sort_by = preferences.get("sort_by")
        sort_order = preferences.get("sort_order")
        if sort_by not in CRM_LIST_SORT_FIELDS[entity] or sort_order not in {"asc", "desc"}:
            sort_by = None
            sort_order = None
        return CRMListConfiguration(
            entity=entity,
            columns=columns,
            sort_by=sort_by,
            sort_order=sort_order,
        )

    async def _get_current_user_row(self, *, lock: bool = False) -> User:
        """Load the user through both identity and organisation trust boundaries."""
        statement = select(User).where(
            col(User.user_id) == self.user_id,
            col(User.organisation_id) == self.organisation_id,
        )
        if lock:
            statement = statement.with_for_update()
        result = await self.master_session.execute(statement)
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return user
