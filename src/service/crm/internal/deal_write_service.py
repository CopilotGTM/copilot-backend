"""Service for creating and updating internal CRM deals."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
)
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
    CRMParticipantType,
    CRMPipeline,
)
from src.infra.utils.logger import get_logger
from src.service.crm.internal.deal_participant_service import DealParticipantServiceError
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.service.crm.internal.internal_crm_user_service import InternalCRMUserService

logger = get_logger(__name__)

_DOMAIN_CLEAN_RE = re.compile(r"^https?://", re.IGNORECASE)
_COMPANY_NAME_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_CONTACT_NAME_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_CUSTOM_KEY_INVALID_RE = re.compile(r"[^a-z0-9_]+")


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

        # Resolve deal_status: use provided value or default to OPEN
        deal_status_raw = payload.get("deal_status")
        deal_status = CRMDealStatus(deal_status_raw) if deal_status_raw else CRMDealStatus.OPEN

        # Build properties dict: merge base properties with any custom properties
        created_via = payload.get("created_via", "api")
        properties: dict[str, Any] = {"is_internal_crm": True, "created_via": created_via}
        custom_properties = payload.get("custom_properties")
        if custom_properties:
            properties.update(custom_properties)

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
            deal_status=deal_status,
            probability=payload.get("probability"),
            forecast_category=payload.get("forecast_category"),
            owner_id=owner_id,
            owner_external_id=owner_external_id,
            owner_name=owner_name,
            owner_email=owner_email,
            close_date=payload.get("close_date"),
            expected_close_date=payload.get("expected_close_date"),
            closed_won_at=payload.get("closed_won_at"),
            closed_lost_at=payload.get("closed_lost_at"),
            closed_reason=payload.get("closed_reason"),
            crm_url=payload.get("crm_url"),
            description=payload.get("description"),
            deal_type=payload.get("deal_type"),
            priority=payload.get("priority"),
            lead_source=payload.get("lead_source"),
            primary_contact_name=payload.get("primary_contact_name"),
            primary_contact_email=payload.get("primary_contact_email"),
            source_created_at=payload.get("source_created_at") or now,
            last_activity_at=payload.get("last_activity_at"),
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties=properties,
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

        # Optional: account + team members supplied during deal creation
        account_payload = payload.get("account")
        team_members = payload.get("team_members")
        primary_contact_name = payload.get("primary_contact_name")
        primary_contact_email = payload.get("primary_contact_email")
        if account_payload or team_members:
            await self._apply_account_and_team(
                deal=deal,
                integration_id=integration_id,
                account_payload=account_payload,
                team_members=team_members,
                primary_contact_name=primary_contact_name,
                primary_contact_email=primary_contact_email,
                now=now,
            )
        elif primary_contact_name or primary_contact_email:
            contact, _fallback_name, _fallback_email = await self._upsert_contact_from_primary(
                primary_contact_name=primary_contact_name,
                primary_contact_email=primary_contact_email,
                company_name=deal.company_name,
                integration_id=integration_id,
                now=now,
            )
            if contact:
                await self._replace_primary_contact_participant(deal, contact, now)

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
        if "probability" in payload:
            deal.probability = payload["probability"]
            updated = True
        if "forecast_category" in payload:
            deal.forecast_category = payload["forecast_category"]
            updated = True
        if "custom_properties" in payload and payload["custom_properties"] is not None:
            validated_custom = await self._validate_custom_properties(payload["custom_properties"])
            properties = deal.properties or {}
            properties.update(validated_custom)
            deal.properties = properties
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

    async def _validate_custom_properties(
        self, custom_properties: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate custom field keys/types before writing to deal properties."""
        if not isinstance(custom_properties, dict):
            raise DealWriteServiceError("custom_properties must be an object", status_code=400)

        normalized_map: dict[str, Any] = {}
        normalized_keys: list[str] = []
        for key, value in custom_properties.items():
            normalized_key = self._normalize_custom_key(key)
            normalized_map[normalized_key] = value
            normalized_keys.append(normalized_key)

        if not normalized_keys:
            return {}

        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
            col(CRMCustomFieldDefinition.is_active).is_(True),
            col(CRMCustomFieldDefinition.key).in_(normalized_keys),
        )
        result = await self.session.execute(stmt)
        fields = list(result.scalars().all())
        field_by_key = {field.key: field for field in fields}

        unknown_keys = sorted(set(normalized_keys) - set(field_by_key))
        if unknown_keys:
            raise DealWriteServiceError(
                f"Unknown custom fields: {', '.join(unknown_keys)}",
                status_code=400,
            )

        coerced: dict[str, Any] = {}
        for key, value in normalized_map.items():
            field = field_by_key[key]
            raw_data_type = (
                field.data_type.value
                if isinstance(field.data_type, CRMCustomFieldDataType)
                else str(field.data_type)
            )
            data_type = CRMCustomFieldDataType(raw_data_type)
            if data_type == CRMCustomFieldDataType.FORMULA:
                raise DealWriteServiceError(
                    f"Formula custom field '{key}' is read-only",
                    status_code=400,
                )
            coerced[key] = self._coerce_custom_value(key=key, value=value, data_type=data_type)

        return coerced

    def _coerce_custom_value(
        self,
        *,
        key: str,
        value: Any,
        data_type: CRMCustomFieldDataType,
    ) -> Any:
        if value is None:
            return None

        try:
            if data_type == CRMCustomFieldDataType.TEXT:
                return str(value).strip()
            if data_type == CRMCustomFieldDataType.INTEGER:
                if isinstance(value, bool):
                    raise ValueError
                return int(value)
            if data_type == CRMCustomFieldDataType.NUMBER:
                if isinstance(value, bool):
                    raise ValueError
                return float(value)
            if data_type == CRMCustomFieldDataType.BOOLEAN:
                return self._parse_boolean(value)
            if data_type == CRMCustomFieldDataType.DATE:
                return self._parse_date(value)
            if data_type == CRMCustomFieldDataType.DATETIME:
                return self._parse_datetime(value)
            if data_type == CRMCustomFieldDataType.CURRENCY:
                return self._parse_currency(value)
            if data_type == CRMCustomFieldDataType.SELECT:
                return str(value).strip()
            if data_type == CRMCustomFieldDataType.MULTISELECT:
                if isinstance(value, list):
                    return [str(item).strip() for item in value if str(item).strip()]
                return [part.strip() for part in str(value).split(",") if part.strip()]
        except (TypeError, ValueError) as exc:
            raise DealWriteServiceError(
                f"Invalid value for custom field '{key}' ({data_type.value})",
                status_code=400,
            ) from exc

        return value

    @staticmethod
    def _parse_boolean(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
        raise ValueError("Invalid boolean value")

    @staticmethod
    def _parse_date(value: Any) -> str:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()

        raw = str(value).strip()
        if not raw:
            raise ValueError("Empty date")

        normalized = raw.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).date().isoformat()
        except ValueError:
            return date.fromisoformat(raw).isoformat()

    @staticmethod
    def _parse_datetime(value: Any) -> str:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
        else:
            raw = str(value).strip()
            if not raw:
                raise ValueError("Empty datetime")

            normalized = raw.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                from dateutil import parser as date_parser  # type: ignore[import-untyped]

                parsed = date_parser.parse(raw)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.isoformat()

    @staticmethod
    def _parse_currency(value: Any) -> float:
        raw = str(value).strip()
        if not raw:
            raise ValueError("Empty currency")
        normalized = re.sub(r"[$€£¥₹]", "", raw)
        normalized = re.sub(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])", "", normalized.upper())
        normalized = normalized.replace(",", "").replace(" ", "")
        if not normalized:
            raise ValueError("Invalid currency")
        return float(normalized)

    @staticmethod
    def _normalize_custom_key(value: Any) -> str:
        raw = str(value).strip().lower()
        if raw.startswith("custom_"):
            raw = raw[7:]
        normalized = _CUSTOM_KEY_INVALID_RE.sub("_", raw)
        normalized = re.sub(r"_+", "_", normalized).strip("_")
        if not normalized or not re.match(r"^[a-z][a-z0-9_]{1,62}$", normalized):
            raise DealWriteServiceError(
                f"Invalid custom field key '{value}'",
                status_code=400,
            )
        return normalized

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

        now = datetime.now(UTC)
        self._apply_stage_update(deal=deal, stage=stage, now=now)
        self._record_stage_history(deal=deal, stage=stage, now=now)
        await self.session.flush()

        logger.info("Moved deal %s to stage %s", deal.deal_id, stage.name)
        return deal

    async def close_deal(
        self,
        deal_id: UUID,
        *,
        outcome: str,
        closed_reason: str | None,
        close_date: datetime | None = None,
        stage_id: UUID | None = None,
    ) -> CRMDeal:
        """Close an internal deal as won/lost and record closure metadata."""
        normalized_outcome = outcome.strip().lower()
        if normalized_outcome not in {CRMDealStatus.WON.value, CRMDealStatus.LOST.value}:
            raise DealWriteServiceError("outcome must be 'won' or 'lost'", status_code=400)
        if normalized_outcome == CRMDealStatus.LOST.value and not closed_reason:
            raise DealWriteServiceError(
                "closed_reason is required when outcome is 'lost'",
                status_code=400,
            )

        deal = await self._load_internal_deal(deal_id)
        if not deal.pipeline_id:
            raise DealWriteServiceError("Deal has no pipeline", status_code=500)

        stage = await self._resolve_stage_for_state(
            deal=deal,
            target_state=normalized_outcome,
            stage_id=stage_id,
        )
        now = datetime.now(UTC)
        final_close_at = close_date or now
        if final_close_at.tzinfo is None:
            final_close_at = final_close_at.replace(tzinfo=UTC)

        self._apply_stage_update(deal=deal, stage=stage, now=now)
        self._record_stage_history(deal=deal, stage=stage, now=now)

        deal.deal_status = CRMDealStatus(normalized_outcome)
        deal.close_date = final_close_at
        if normalized_outcome == CRMDealStatus.WON.value:
            deal.closed_won_at = final_close_at
            deal.closed_lost_at = None
            deal.closed_reason = closed_reason
        else:
            deal.closed_lost_at = final_close_at
            deal.closed_won_at = None
            deal.closed_reason = closed_reason

        properties = deal.properties or {}
        properties["closed_via"] = "api"
        properties["closed_by_user_id"] = str(self.actor_user_id)
        deal.properties = properties

        await self.session.flush()
        logger.info("Closed deal %s with outcome=%s", deal.deal_id, normalized_outcome)
        return deal

    async def reopen_deal(
        self,
        deal_id: UUID,
        *,
        stage_id: UUID | None = None,
        clear_closed_reason: bool = False,
    ) -> CRMDeal:
        """Reopen a closed internal deal and move it to an open stage."""
        deal = await self._load_internal_deal(deal_id)
        if not deal.pipeline_id:
            raise DealWriteServiceError("Deal has no pipeline", status_code=500)

        stage = await self._resolve_stage_for_state(
            deal=deal,
            target_state=CRMDealStatus.OPEN.value,
            stage_id=stage_id,
        )
        now = datetime.now(UTC)
        self._apply_stage_update(deal=deal, stage=stage, now=now)
        self._record_stage_history(deal=deal, stage=stage, now=now)

        deal.deal_status = CRMDealStatus.OPEN
        deal.closed_won_at = None
        deal.closed_lost_at = None
        if clear_closed_reason:
            deal.closed_reason = None

        properties = deal.properties or {}
        properties["reopened_via"] = "api"
        properties["reopened_by_user_id"] = str(self.actor_user_id)
        deal.properties = properties

        await self.session.flush()
        logger.info("Reopened deal %s", deal.deal_id)
        return deal

    async def _resolve_stage_for_state(
        self,
        *,
        deal: CRMDeal,
        target_state: str,
        stage_id: UUID | None,
    ) -> CRMDealStage:
        if stage_id:
            stage = await self._load_stage(stage_id, deal.pipeline_id, deal.integration_id)
            actual_state = self._normalize_stage_state(stage.stage_state)
            if actual_state != target_state:
                raise DealWriteServiceError(
                    f"Stage state '{actual_state}' does not match required '{target_state}'",
                    status_code=400,
                )
            return stage

        stmt = (
            select(CRMDealStage)
            .where(
                col(CRMDealStage.organisation_id) == self.organisation_id,
                col(CRMDealStage.integration_id) == deal.integration_id,
                col(CRMDealStage.pipeline_id) == deal.pipeline_id,
                col(CRMDealStage.is_deleted).is_(False),
                col(CRMDealStage.stage_state) == target_state,
            )
            .order_by(col(CRMDealStage.display_order).asc(), col(CRMDealStage.created_at).asc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        stage = result.scalar_one_or_none()
        if not stage:
            raise DealWriteServiceError(
                f"No stage found with state '{target_state}' for this deal pipeline",
                status_code=404,
            )
        return stage

    def _apply_stage_update(self, *, deal: CRMDeal, stage: CRMDealStage, now: datetime) -> None:
        deal.stage_id = stage.stage_id
        deal.stage_external_id = stage.external_id
        deal.stage_name = stage.name
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now
        self.session.add(deal)

    def _record_stage_history(self, *, deal: CRMDeal, stage: CRMDealStage, now: datetime) -> None:
        history_id = uuid4()
        history = CRMDealStageHistory(
            history_id=history_id,
            organisation_id=self.organisation_id,
            integration_id=deal.integration_id,
            external_id=str(history_id),
            deal_id=deal.deal_id,
            stage_id=stage.stage_id,
            stage_external_id=stage.external_id,
            entered_at=now,
            synced_at=now,
            last_seen_at=now,
        )
        self.session.add(history)

    @staticmethod
    def _normalize_stage_state(value: Any) -> str:
        return value.value if hasattr(value, "value") else str(value).strip().lower()

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

    async def update_account(self, deal_id: UUID, account_payload: dict[str, Any]) -> CRMDeal:
        """Update account details for an internal CRM deal."""
        if not account_payload or not any(account_payload.values()):
            raise DealWriteServiceError("No account fields to update", status_code=400)

        deal = await self._load_internal_deal(deal_id)
        now = datetime.now(UTC)

        try:
            company, fallback_name = await self._upsert_company_from_account(
                account_payload, deal.integration_id, now
            )
            if company:
                await self._replace_company_participant(deal, company, now)
            elif fallback_name:
                await self._apply_fallback_company_name(deal, fallback_name, now)
            else:
                raise DealWriteServiceError(
                    "Account payload missing domain or name", status_code=400
                )
        except DealParticipantServiceError as exc:
            raise DealWriteServiceError(exc.message, status_code=exc.status_code) from exc

        logger.info("Updated account for internal CRM deal %s", deal.deal_id)
        return deal

    async def update_primary_contact(
        self,
        deal_id: UUID,
        *,
        primary_contact_name: str | None,
        primary_contact_email: str | None,
        company_name: str | None = None,
    ) -> CRMDeal:
        """Update primary contact for an internal CRM deal."""
        if not primary_contact_name and not primary_contact_email:
            raise DealWriteServiceError("No primary contact fields to update", status_code=400)

        deal = await self._load_internal_deal(deal_id)
        now = datetime.now(UTC)
        contact, fallback_name, fallback_email = await self._upsert_contact_from_primary(
            primary_contact_name=primary_contact_name,
            primary_contact_email=primary_contact_email,
            company_name=company_name or deal.company_name,
            integration_id=deal.integration_id,
            now=now,
        )
        if contact:
            await self._replace_primary_contact_participant(deal, contact, now)
        else:
            if fallback_name:
                deal.primary_contact_name = fallback_name
            if fallback_email:
                deal.primary_contact_email = fallback_email
            deal.source_modified_at = now
            deal.synced_at = now
            deal.last_seen_at = now
            self.session.add(deal)

        logger.info("Updated primary contact for internal CRM deal %s", deal.deal_id)
        return deal

    async def _apply_account_and_team(
        self,
        *,
        deal: CRMDeal,
        integration_id: UUID,
        account_payload: dict[str, Any] | None,
        team_members: list[dict[str, Any]] | None,
        primary_contact_name: str | None,
        primary_contact_email: str | None,
        now: datetime,
    ) -> None:
        try:
            company = None
            fallback_name = None

            if account_payload:
                company, fallback_name = await self._upsert_company_from_account(
                    account_payload, integration_id, now
                )

            if company:
                await self._add_company_participant(deal, company, now)
            elif fallback_name:
                deal.company_name = fallback_name
                deal.source_modified_at = now
                deal.synced_at = now
                deal.last_seen_at = now
                self.session.add(deal)

            (
                contact,
                _contact_fallback_name,
                _contact_fallback_email,
            ) = await self._upsert_contact_from_primary(
                primary_contact_name=primary_contact_name,
                primary_contact_email=primary_contact_email,
                company_name=(company.name if company else fallback_name or deal.company_name),
                integration_id=integration_id,
                now=now,
            )
            if contact:
                await self._replace_primary_contact_participant(deal, contact, now)

            if team_members:
                await self._add_team_participants(deal, team_members)
        except DealParticipantServiceError as exc:
            raise DealWriteServiceError(exc.message, status_code=exc.status_code) from exc

    async def _upsert_contact_from_primary(
        self,
        *,
        primary_contact_name: str | None,
        primary_contact_email: str | None,
        company_name: str | None,
        integration_id: UUID,
        now: datetime,
    ) -> tuple[CRMContact | None, str | None, str | None]:
        contact_name = self._clean_string(primary_contact_name)
        contact_email = self._normalize_email(primary_contact_email)
        company_name_clean = self._clean_string(company_name)

        if not contact_name and not contact_email:
            return None, None, None

        if contact_email:
            stmt = select(CRMContact).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.integration_id) == integration_id,
                col(CRMContact.is_deleted).is_(False),
                func.lower(CRMContact.email) == contact_email,
            )
            result = await self.session.execute(stmt)
            email_matches = list(result.scalars().all())

            if len(email_matches) == 1:
                contact = email_matches[0]
                updated = False
                if contact_name and not contact.full_name:
                    contact.full_name = contact_name
                    updated = True
                if company_name_clean and not contact.company_name:
                    contact.company_name = company_name_clean
                    updated = True
                if updated:
                    contact.source_modified_at = now
                    contact.synced_at = now
                    contact.last_seen_at = now
                    self.session.add(contact)
                return contact, None, None

            if len(email_matches) > 1:
                return None, contact_name, contact_email

            contact_id = uuid4()
            contact = CRMContact(
                contact_id=contact_id,
                organisation_id=self.organisation_id,
                integration_id=integration_id,
                external_id=str(contact_id),
                full_name=contact_name,
                email=contact_email,
                company_name=company_name_clean,
                source_created_at=now,
                source_modified_at=now,
                synced_at=now,
                last_seen_at=now,
                properties={
                    "source": "internal_crm",
                    "created_via": "deal_create",
                    "is_provisional": False,
                    "match_source": "email_import",
                },
            )
            self.session.add(contact)
            await self.session.flush()
            return contact, None, None

        if not contact_name:
            return None, None, None

        normalized_name = self._normalize_contact_name(contact_name)
        if normalized_name:
            stmt = select(CRMContact).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.integration_id) == integration_id,
                col(CRMContact.is_deleted).is_(False),
                func.lower(func.trim(CRMContact.full_name)) == normalized_name,
            )
            result = await self.session.execute(stmt)
            name_matches = list(result.scalars().all())

            if len(name_matches) == 1:
                contact = name_matches[0]
                if company_name_clean and not contact.company_name:
                    contact.company_name = company_name_clean
                    contact.source_modified_at = now
                    contact.synced_at = now
                    contact.last_seen_at = now
                    self.session.add(contact)
                return contact, None, None

            if len(name_matches) > 1:
                return None, contact_name, None

        contact_id = uuid4()
        contact = CRMContact(
            contact_id=contact_id,
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            external_id=str(contact_id),
            full_name=contact_name,
            email=None,
            company_name=company_name_clean,
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={
                "source": "internal_crm",
                "created_via": "deal_create",
                "is_provisional": True,
                "match_source": "name_only_import",
                "normalized_name": normalized_name or contact_name.lower(),
            },
        )
        self.session.add(contact)
        await self.session.flush()
        return contact, None, None

    async def _upsert_company_from_account(
        self,
        account_payload: dict[str, Any],
        integration_id: UUID,
        now: datetime,
    ) -> tuple[CRMCompany | None, str | None]:
        name = self._clean_string(account_payload.get("name"))
        domain = self._normalize_domain(
            account_payload.get("domain") or account_payload.get("website")
        )
        website = self._clean_string(account_payload.get("website"))
        linkedin_url = self._clean_string(account_payload.get("linkedin_url"))
        industry = self._clean_string(account_payload.get("industry"))
        country = self._clean_string(account_payload.get("country"))
        normalized_name = self._normalize_company_name(name)

        if not domain:
            if not name:
                return None, None

            # Name-only fallback: attempt a safe exact normalized-name match.
            if normalized_name:
                stmt = select(CRMCompany).where(
                    col(CRMCompany.organisation_id) == self.organisation_id,
                    col(CRMCompany.integration_id) == integration_id,
                    col(CRMCompany.is_deleted).is_(False),
                    func.lower(func.trim(CRMCompany.name)) == normalized_name,
                )
                result = await self.session.execute(stmt)
                name_matches = list(result.scalars().all())

                if len(name_matches) == 1:
                    company = name_matches[0]
                    updated = False
                    if website and not company.website:
                        company.website = website
                        updated = True
                    if linkedin_url and not company.linkedin_url:
                        company.linkedin_url = linkedin_url
                        updated = True
                    if industry and not company.industry:
                        company.industry = industry
                        updated = True
                    if country and not company.country:
                        company.country = country
                        updated = True
                    if updated:
                        company.source_modified_at = now
                        company.synced_at = now
                        company.last_seen_at = now
                        self.session.add(company)
                    return company, None

                if len(name_matches) > 1:
                    return None, name

            # No safe match found: create a provisional company with name only.
            company_id = uuid4()
            company = CRMCompany(
                company_id=company_id,
                organisation_id=self.organisation_id,
                integration_id=integration_id,
                external_id=str(company_id),
                name=name,
                domain=None,
                website=website,
                linkedin_url=linkedin_url,
                industry=industry,
                country=country,
                source_created_at=now,
                source_modified_at=now,
                synced_at=now,
                last_seen_at=now,
                properties={
                    "source": "internal_crm",
                    "created_via": "deal_create",
                    "is_provisional": True,
                    "match_source": "name_only_import",
                    "normalized_name": normalized_name or name.lower(),
                },
            )
            self.session.add(company)
            await self.session.flush()
            return company, None

        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted).is_(False),
            func.lower(CRMCompany.domain) == domain.lower(),
        )
        result = await self.session.execute(stmt)
        company = result.scalars().first()

        if company:
            updated = False
            if name and not company.name:
                company.name = name
                updated = True
            if website and not company.website:
                company.website = website
                updated = True
            if linkedin_url and not company.linkedin_url:
                company.linkedin_url = linkedin_url
                updated = True
            if industry and not company.industry:
                company.industry = industry
                updated = True
            if country and not company.country:
                company.country = country
                updated = True

            if updated:
                company.source_modified_at = now
                company.synced_at = now
                company.last_seen_at = now
                self.session.add(company)
            return company, None

        company_id = uuid4()
        company = CRMCompany(
            company_id=company_id,
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            external_id=str(company_id),
            name=name or domain,
            domain=domain,
            website=website,
            linkedin_url=linkedin_url,
            industry=industry,
            country=country,
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={"source": "internal_crm", "created_via": "deal_create"},
        )
        self.session.add(company)
        await self.session.flush()
        return company, None

    async def _add_company_participant(
        self,
        deal: CRMDeal,
        company: CRMCompany,
        now: datetime,
    ) -> None:
        from src.service.crm.internal.deal_participant_service import DealParticipantService

        participant_service = DealParticipantService(
            self.session, self.organisation_id, self.actor_user_id, self.master_session
        )
        payloads = [
            {
                "participant_type": "company",
                "related_id": company.company_id,
                "relationship_role": None,
            }
        ]
        await participant_service.add_participants(deal.deal_id, payloads)

        deal.company_external_id = company.external_id
        deal.company_name = company.name
        deal.company_domain = company.domain
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now
        self.session.add(deal)

    async def _replace_company_participant(
        self,
        deal: CRMDeal,
        company: CRMCompany,
        now: datetime,
    ) -> None:
        await self._add_company_participant(deal, company, now)

        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == deal.integration_id,
            col(CRMDealParticipant.deal_id) == deal.deal_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.COMPANY.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.related_external_id) != company.external_id,
        )
        result = await self.session.execute(stmt)
        for participant in result.scalars().all():
            participant.is_deleted = True
            participant.source_modified_at = now
            participant.synced_at = now
            participant.last_seen_at = now
            self.session.add(participant)

    async def _replace_primary_contact_participant(
        self,
        deal: CRMDeal,
        contact: CRMContact,
        now: datetime,
    ) -> None:
        await self._add_contact_participant(deal, contact, now)

        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == deal.integration_id,
            col(CRMDealParticipant.deal_id) == deal.deal_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.relationship_role) == "primary_contact",
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.related_external_id) != contact.external_id,
        )
        result = await self.session.execute(stmt)
        for participant in result.scalars().all():
            participant.is_deleted = True
            participant.source_modified_at = now
            participant.synced_at = now
            participant.last_seen_at = now
            self.session.add(participant)

    async def _apply_fallback_company_name(
        self,
        deal: CRMDeal,
        fallback_name: str,
        now: datetime,
    ) -> None:
        company = None
        if deal.company_external_id:
            stmt = select(CRMCompany).where(
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.integration_id) == deal.integration_id,
                col(CRMCompany.external_id) == deal.company_external_id,
                col(CRMCompany.is_deleted).is_(False),
            )
            result = await self.session.execute(stmt)
            company = result.scalars().first()

        if company:
            if company.name != fallback_name:
                company.name = fallback_name
                company.source_modified_at = now
                company.synced_at = now
                company.last_seen_at = now
                self.session.add(company)
            deal.company_name = fallback_name
        else:
            deal.company_name = fallback_name

        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now
        self.session.add(deal)

    async def _add_team_participants(
        self,
        deal: CRMDeal,
        team_members: list[dict[str, Any]],
    ) -> None:
        from src.service.crm.internal.deal_participant_service import DealParticipantService

        if not team_members:
            return

        payloads: list[dict[str, Any]] = []
        for member in team_members:
            user_id = member.get("user_id")
            if not isinstance(user_id, UUID):
                raise DealWriteServiceError("Invalid team member user_id", status_code=400)
            payloads.append(
                {
                    "participant_type": "user",
                    "related_id": user_id,
                    "relationship_role": self._clean_string(member.get("role")),
                }
            )

        participant_service = DealParticipantService(
            self.session, self.organisation_id, self.actor_user_id, self.master_session
        )
        await participant_service.add_participants(deal.deal_id, payloads)

    async def _add_contact_participant(
        self,
        deal: CRMDeal,
        contact: CRMContact,
        now: datetime,
    ) -> None:
        from src.service.crm.internal.deal_participant_service import DealParticipantService

        participant_service = DealParticipantService(
            self.session, self.organisation_id, self.actor_user_id, self.master_session
        )
        payloads = [
            {
                "participant_type": "contact",
                "related_id": contact.contact_id,
                "relationship_role": "primary_contact",
            }
        ]
        await participant_service.add_participants(deal.deal_id, payloads)

        deal.primary_contact_external_id = contact.external_id
        deal.primary_contact_name = contact.full_name or deal.primary_contact_name
        deal.primary_contact_email = contact.email or deal.primary_contact_email
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now
        self.session.add(deal)

    @staticmethod
    def _clean_string(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @staticmethod
    def _normalize_domain(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        cleaned = _DOMAIN_CLEAN_RE.sub("", cleaned)
        cleaned = cleaned.split("/")[0]
        cleaned = cleaned.lower()
        if cleaned.startswith("www."):
            cleaned = cleaned[4:]
        return cleaned or None

    @staticmethod
    def _normalize_company_name(value: str | None) -> str | None:
        cleaned = DealWriteService._clean_string(value)
        if not cleaned:
            return None
        cleaned = cleaned.lower()
        cleaned = _COMPANY_NAME_NON_ALNUM_RE.sub(" ", cleaned)
        cleaned = " ".join(cleaned.split())
        return cleaned or None

    @staticmethod
    def _normalize_contact_name(value: str | None) -> str | None:
        cleaned = DealWriteService._clean_string(value)
        if not cleaned:
            return None
        cleaned = cleaned.lower()
        cleaned = _CONTACT_NAME_NON_ALNUM_RE.sub(" ", cleaned)
        cleaned = " ".join(cleaned.split())
        return cleaned or None

    @staticmethod
    def _normalize_email(value: str | None) -> str | None:
        cleaned = DealWriteService._clean_string(value)
        if not cleaned:
            return None
        return cleaned.lower()

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
