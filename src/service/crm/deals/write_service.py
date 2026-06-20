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
from src.domain.models.organisation_tables.sync.crm.custom_fields import CRMCustomFieldDataType
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
from src.service.crm.accounts.contracts import AccountWriteServiceError
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.deals.contracts import DealWriteContext, DealWriteServiceError
from src.service.crm.deals.custom_field_service import DealCustomFieldWriteService
from src.service.crm.deals.mutation_event_service import DealMutationEventService, changed_field
from src.service.crm.deals.owner_service import DealOwnerService
from src.service.crm.deals.stage_service import DealStageService
from src.service.crm.internal.deal_participant_service import DealParticipantServiceError
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.service.crm.internal.internal_crm_user_service import InternalCRMUserService
from src.service.crm.mutations.policies import DealMutationPolicy

logger = get_logger(__name__)

_CONTACT_NAME_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_CUSTOM_KEY_INVALID_RE = re.compile(r"[^a-z0-9_]+")


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
        # Account materialization is an account-domain rule. DealWriteService
        # keeps orchestration and participant side effects only.
        self._account_writer = AccountWriteService(self.session, self.organisation_id)
        self._custom_fields = DealCustomFieldWriteService(self.session, self.organisation_id)
        self._mutation_events = DealMutationEventService(self.session, self.organisation_id)
        self._owners = DealOwnerService(
            session=self.session,
            organisation_id=self.organisation_id,
            actor_user_id=self.actor_user_id,
            master_session=self.master_session,
        )
        self._stages = DealStageService(self.session, self.organisation_id)

    async def close(self) -> None:
        """Close the org-scoped session when this service owns dependency cleanup."""
        try:
            await self.session.close()
        except Exception:
            logger.debug("DealWriteService close exception", exc_info=True)

    def _write_context(self) -> DealWriteContext:
        """Build default audit context for legacy callers that only pass payloads."""
        return DealWriteContext(source="api", actor_user_id=self.actor_user_id)

    async def _record_mutation_event(
        self,
        *,
        deal: CRMDeal,
        event_type: str,
        operation: str,
        changed_fields: dict[str, dict[str, Any]],
        summary: str | None,
        context: DealWriteContext | None = None,
    ) -> None:
        semantic_event_types = DealMutationPolicy.semantic_for_event(
            event_type,
            changed_fields,
        )
        await self._mutation_events.record_event(
            deal=deal,
            event_type=event_type,
            operation=operation,
            changed_fields=changed_fields,
            semantic_event_types=semantic_event_types,
            context=context or self._write_context(),
            summary=summary,
        )

    @staticmethod
    def _collect_changed_fields(
        *,
        old_values: dict[str, Any],
        obj: Any,
    ) -> dict[str, dict[str, Any]]:
        changed_fields: dict[str, dict[str, Any]] = {}
        for field_name, old_value in old_values.items():
            if change := changed_field(old_value, getattr(obj, field_name)):
                changed_fields[field_name] = change
        return changed_fields

    async def create_deal(
        self,
        payload: dict[str, Any],
        *,
        context: DealWriteContext | None = None,
    ) -> CRMDeal:
        """Create a new internal CRM deal.

        Args:
            payload: Deal creation data including name, amount, stage_id, etc.

        Returns:
            Created CRMDeal record

        Raises:
            DealWriteServiceError: If validation fails or internal CRM not configured
        """
        write_context = context or self._write_context()

        # Deals created through this writer must belong to the internal CRM
        # integration; external provider deals stay read-only elsewhere.
        setup_service = InternalCRMSetupService(
            self.session, self.organisation_id, self.actor_user_id
        )
        integration = await setup_service.get_internal_integration()
        if not integration:
            raise DealWriteServiceError("Internal CRM is not configured", status_code=409)

        integration_id = integration.integration_id

        # Validate and get pipeline + stage
        raw_pipeline_id = payload.get("pipeline_id")
        raw_stage_id = payload.get("stage_id")

        if raw_pipeline_id is not None and not isinstance(raw_pipeline_id, UUID):
            raise DealWriteServiceError("Invalid pipeline_id", status_code=400)
        if raw_stage_id is not None and not isinstance(raw_stage_id, UUID):
            raise DealWriteServiceError("Invalid stage_id", status_code=400)

        pipeline_id = raw_pipeline_id
        stage_id = raw_stage_id

        if pipeline_id:
            pipeline = await self._load_pipeline(pipeline_id, integration_id)
        else:
            # Use default pipeline
            default_pipeline = await self._get_default_pipeline(integration_id)
            if not default_pipeline:
                raise DealWriteServiceError("No default pipeline found", status_code=500)
            pipeline = default_pipeline
            pipeline_id = pipeline.pipeline_id

        if stage_id:
            stage = await self._load_stage(stage_id, pipeline_id, integration_id)
        else:
            # Use first stage of pipeline
            first_stage = await self._get_first_stage(pipeline_id)
            if not first_stage:
                raise DealWriteServiceError("No stages found in pipeline", status_code=500)
            stage = first_stage
            stage_id = stage.stage_id

        # Handle owner assignment
        owner_user_id = payload.get("owner_user_id")
        crm_user = None

        if owner_user_id:
            user_service = InternalCRMUserService(
                org_session=self.session,
                master_session=self.master_session,
                organisation_id=self.organisation_id,
                actor_user_id=self.actor_user_id,
            )
            crm_user = await user_service.ensure_crm_user(owner_user_id)

        # The deal row is written before participants so downstream participant
        # services can validate a real deal_id in the same transaction.
        now = datetime.now(UTC)
        deal_id = uuid4()

        # Resolve deal_status: use provided value or default to OPEN
        deal_status_raw = payload.get("deal_status")
        deal_status = CRMDealStatus(deal_status_raw) if deal_status_raw else CRMDealStatus.OPEN

        # Build properties dict: merge base properties with any custom properties
        created_via = payload.get("created_via", write_context.source)
        properties: dict[str, Any] = {"is_internal_crm": True, "created_via": created_via}
        custom_properties = payload.get("custom_properties")
        if custom_properties:
            properties.update(custom_properties)

        deal = CRMDeal(
            deal_id=deal_id,
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            name=payload.get("name"),
            amount=payload.get("amount"),
            currency=payload.get("currency"),
            pipeline_id=pipeline_id,
            pipeline_name=pipeline.name,
            stage_id=stage_id,
            stage_name=stage.name,
            deal_status=deal_status,
            probability=payload.get("probability"),
            forecast_category=payload.get("forecast_category"),
            owner_id=None,
            owner_name=None,
            owner_email=None,
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
            stage_entered_at=now,
            stage_updated_at=now,
            last_activity_at=payload.get("last_activity_at"),
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties=properties,
        )

        self.session.add(deal)
        await self.session.flush()
        if crm_user is not None:
            self._owners.apply_owner(deal=deal, crm_user=crm_user)
            await self._owners.sync_owner_participant(deal=deal, crm_user=crm_user)

        # Stage history starts at creation time so later analytics can compute
        # stage dwell time without special-casing the first stage.
        history_id = uuid4()
        history = CRMDealStageHistory(
            history_id=history_id,
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            deal_id=deal.deal_id,
            stage_id=stage.stage_id,
            entered_at=now,
            source=created_via,
            synced_at=now,
            last_seen_at=now,
        )
        self.session.add(history)
        await self.session.flush()

        # Optional: account + team members supplied during deal creation
        account_payload = payload.get("account")
        team_members = payload.get("team_members")
        contact_participants = payload.get("contact_participants")
        primary_contact_name = payload.get("primary_contact_name")
        primary_contact_email = payload.get("primary_contact_email")
        if account_payload or team_members or contact_participants:
            await self._apply_account_and_team(
                deal=deal,
                integration_id=integration_id,
                account_payload=account_payload,
                team_members=team_members,
                contact_participants=contact_participants,
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

        created_changed_fields = DealMutationPolicy.build_create_changed_fields(deal)

        await self._record_mutation_event(
            deal=deal,
            event_type="crm.deal.created",
            operation="create_deal",
            changed_fields=created_changed_fields,
            summary=f"Created deal {deal.name}" if deal.name else "Created deal",
            context=write_context,
        )
        logger.info("Created internal CRM deal %s: %s", deal.deal_id, deal.name)
        return deal

    async def update_deal(
        self,
        deal_id: UUID,
        payload: dict[str, Any],
        *,
        skip_custom_validation: bool = False,
        context: DealWriteContext | None = None,
    ) -> CRMDeal:
        """Update an internal CRM deal.

        Args:
            deal_id: Deal UUID
            payload: Fields to update (only provided fields are updated)
            skip_custom_validation: When True the caller already validated custom
                properties (e.g. the import service pre-validates against cached
                definitions), so the extra DB round-trip is skipped.

        Returns:
            Updated CRMDeal record

        Raises:
            DealWriteServiceError: If deal not found, read-only, or validation fails
        """
        if not payload:
            raise DealWriteServiceError("No fields to update", status_code=400)

        deal = await self._load_internal_deal(deal_id)
        tracked_fields = [
            "name",
            "amount",
            "currency",
            "deal_status",
            "close_date",
            "expected_close_date",
            "closed_won_at",
            "closed_lost_at",
            "closed_reason",
            "crm_url",
            "description",
            "deal_type",
            "priority",
            "lead_source",
            "probability",
            "forecast_category",
        ]
        old_values = {field_name: getattr(deal, field_name) for field_name in tracked_fields}
        old_properties = dict(deal.properties or {})

        # Only explicit payload keys are applied; omitted nullable fields must
        # preserve their existing value while provided nulls clear the field.
        updated = False
        if "name" in payload:
            deal.name = payload["name"]
            updated = True
        if "amount" in payload:
            deal.amount = payload["amount"]
            updated = True
        if "currency" in payload:
            deal.currency = payload["currency"]
            updated = True
        if "deal_status" in payload:
            deal_status_raw = payload["deal_status"]
            deal.deal_status = (
                CRMDealStatus(deal_status_raw) if deal_status_raw else CRMDealStatus.OPEN
            )
            updated = True
        if "close_date" in payload:
            deal.close_date = payload["close_date"]
            updated = True
        if "expected_close_date" in payload:
            deal.expected_close_date = payload["expected_close_date"]
            updated = True
        if "closed_won_at" in payload:
            deal.closed_won_at = payload["closed_won_at"]
            updated = True
        if "closed_lost_at" in payload:
            deal.closed_lost_at = payload["closed_lost_at"]
            updated = True
        if "closed_reason" in payload:
            deal.closed_reason = payload["closed_reason"]
            updated = True
        if "crm_url" in payload:
            deal.crm_url = payload["crm_url"]
            updated = True
        if "source_created_at" in payload:
            deal.source_created_at = payload["source_created_at"]
            updated = True
        if "last_activity_at" in payload:
            deal.last_activity_at = payload["last_activity_at"]
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
            custom_props = payload["custom_properties"]
            validated_custom = (
                custom_props
                if skip_custom_validation
                else await self._validate_custom_properties(custom_props)
            )
            # New dict so SQLAlchemy detects the JSONB column change.
            deal.properties = {**(deal.properties or {}), **validated_custom}
            updated = True

        if not updated:
            raise DealWriteServiceError("No valid fields to update", status_code=400)

        now = datetime.now(UTC)
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now
        # New dict so SQLAlchemy detects the JSONB column change.
        write_context = context or self._write_context()
        deal.properties = {**(deal.properties or {}), "updated_via": write_context.source}

        changed_fields = self._collect_changed_fields(old_values=old_values, obj=deal)
        if "custom_properties" in payload and payload["custom_properties"] is not None:
            for key, new_value in (deal.properties or {}).items():
                if key == "updated_via":
                    continue
                if (key in old_properties or key in payload["custom_properties"]) and (
                    change := changed_field(old_properties.get(key), new_value)
                ):
                    changed_fields[f"custom_properties.{key}"] = change

        await self.session.flush()
        await self._record_mutation_event(
            deal=deal,
            event_type="crm.deal.updated",
            operation="update_deal",
            changed_fields=changed_fields,
            summary=f"Updated deal {deal.name}" if deal.name else "Updated deal",
            context=write_context,
        )
        logger.info("Updated internal CRM deal %s", deal.deal_id)
        return deal

    async def _validate_custom_properties(
        self, custom_properties: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate custom field keys/types before writing to deal properties."""
        return await self._custom_fields.validate_custom_properties(custom_properties)

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

    async def delete_deal(
        self,
        deal_id: UUID,
        *,
        context: DealWriteContext | None = None,
    ) -> CRMDeal:
        """Soft delete an internal CRM deal.

        Args:
            deal_id: Deal UUID

        Returns:
            Deleted CRMDeal record

        Raises:
            DealWriteServiceError: If deal not found or read-only
        """
        deal = await self._load_internal_deal(deal_id)
        old_values = {
            "is_deleted": deal.is_deleted,
            "deal_status": deal.deal_status,
        }

        now = datetime.now(UTC)
        deal.is_deleted = True
        deal.deal_status = CRMDealStatus.CLOSED
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now
        write_context = context or self._write_context()
        deal.properties = {**(deal.properties or {}), "deleted_via": write_context.source}

        await self.session.flush()
        await self._record_mutation_event(
            deal=deal,
            event_type="crm.deal.deleted",
            operation="delete_deal",
            changed_fields=self._collect_changed_fields(old_values=old_values, obj=deal),
            summary=f"Deleted deal {deal.name}" if deal.name else "Deleted deal",
            context=write_context,
        )
        logger.info("Deleted internal CRM deal %s", deal.deal_id)
        return deal

    async def move_stage(
        self,
        deal_id: UUID,
        stage_id: UUID,
        *,
        context: DealWriteContext | None = None,
    ) -> CRMDeal:
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
        if deal.stage_id == stage.stage_id:
            return deal

        old_values = {
            "stage_id": deal.stage_id,
            "stage_name": deal.stage_name,
        }
        write_context = context or self._write_context()
        await self._stages.apply_stage(deal=deal, stage=stage, source=write_context.source)
        await self._record_mutation_event(
            deal=deal,
            event_type="crm.deal.stage_changed",
            operation="move_stage",
            changed_fields=self._collect_changed_fields(old_values=old_values, obj=deal),
            summary=f"Moved deal to {stage.name}",
            context=write_context,
        )

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
        context: DealWriteContext | None = None,
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
        old_values = {
            "stage_id": deal.stage_id,
            "stage_name": deal.stage_name,
            "deal_status": deal.deal_status,
            "close_date": deal.close_date,
            "closed_won_at": deal.closed_won_at,
            "closed_lost_at": deal.closed_lost_at,
            "closed_reason": deal.closed_reason,
        }

        stage = await self._resolve_stage_for_state(
            deal=deal,
            target_state=normalized_outcome,
            stage_id=stage_id,
        )
        now = datetime.now(UTC)
        final_close_at = close_date or now
        if final_close_at.tzinfo is None:
            final_close_at = final_close_at.replace(tzinfo=UTC)

        write_context = context or self._write_context()
        await self._stages.apply_stage(deal=deal, stage=stage, source=write_context.source)

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

        deal.properties = {
            **(deal.properties or {}),
            "closed_via": write_context.source,
            "closed_by_user_id": str(self.actor_user_id),
        }

        await self.session.flush()
        await self._record_mutation_event(
            deal=deal,
            event_type="crm.deal.closed",
            operation="close_deal",
            changed_fields=self._collect_changed_fields(old_values=old_values, obj=deal),
            summary=f"Closed deal as {normalized_outcome}",
            context=write_context,
        )
        logger.info("Closed deal %s with outcome=%s", deal.deal_id, normalized_outcome)
        return deal

    async def reopen_deal(
        self,
        deal_id: UUID,
        *,
        stage_id: UUID | None = None,
        clear_closed_reason: bool = False,
        context: DealWriteContext | None = None,
    ) -> CRMDeal:
        """Reopen a closed internal deal and move it to an open stage."""
        deal = await self._load_internal_deal(deal_id)
        if not deal.pipeline_id:
            raise DealWriteServiceError("Deal has no pipeline", status_code=500)
        old_values = {
            "stage_id": deal.stage_id,
            "stage_name": deal.stage_name,
            "deal_status": deal.deal_status,
            "closed_won_at": deal.closed_won_at,
            "closed_lost_at": deal.closed_lost_at,
            "closed_reason": deal.closed_reason,
        }

        stage = await self._resolve_stage_for_state(
            deal=deal,
            target_state=CRMDealStatus.OPEN.value,
            stage_id=stage_id,
        )
        write_context = context or self._write_context()
        await self._stages.apply_stage(deal=deal, stage=stage, source=write_context.source)

        deal.deal_status = CRMDealStatus.OPEN
        deal.closed_won_at = None
        deal.closed_lost_at = None
        if clear_closed_reason:
            deal.closed_reason = None

        deal.properties = {
            **(deal.properties or {}),
            "reopened_via": write_context.source,
            "reopened_by_user_id": str(self.actor_user_id),
        }

        await self.session.flush()
        await self._record_mutation_event(
            deal=deal,
            event_type="crm.deal.reopened",
            operation="reopen_deal",
            changed_fields=self._collect_changed_fields(old_values=old_values, obj=deal),
            summary="Reopened deal",
            context=write_context,
        )
        logger.info("Reopened deal %s", deal.deal_id)
        return deal

    async def _resolve_stage_for_state(
        self,
        *,
        deal: CRMDeal,
        target_state: str,
        stage_id: UUID | None,
    ) -> CRMDealStage:
        pipeline_id = deal.pipeline_id
        if pipeline_id is None:
            raise DealWriteServiceError("Deal has no pipeline", status_code=500)

        if stage_id:
            stage = await self._load_stage(stage_id, pipeline_id, deal.integration_id)
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
                col(CRMDealStage.pipeline_id) == pipeline_id,
                col(CRMDealStage.is_deleted).is_(False),
                col(CRMDealStage.stage_state) == target_state,
            )
            .order_by(col(CRMDealStage.display_order).asc(), col(CRMDealStage.created_at).asc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        matching_stage = result.scalar_one_or_none()
        if not matching_stage:
            raise DealWriteServiceError(
                f"No stage found with state '{target_state}' for this deal pipeline",
                status_code=404,
            )
        return matching_stage

    @staticmethod
    def _normalize_stage_state(value: Any) -> str:
        return value.value if hasattr(value, "value") else str(value).strip().lower()

    async def assign_owner(
        self,
        deal_id: UUID,
        owner_user_id: UUID | None,
        *,
        context: DealWriteContext | None = None,
    ) -> CRMDeal:
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

        crm_user = None
        if owner_user_id:
            # Ensure CRM user exists
            user_service = InternalCRMUserService(
                org_session=self.session,
                master_session=self.master_session,
                organisation_id=self.organisation_id,
                actor_user_id=self.actor_user_id,
            )
            crm_user = await user_service.ensure_crm_user(owner_user_id)

        owner_result = self._owners.apply_owner(deal=deal, crm_user=crm_user)
        if not owner_result.changed:
            return deal

        if crm_user is not None:
            await self._owners.sync_owner_participant(deal=deal, crm_user=crm_user)
        else:
            await self._owners.clear_owner_participants(deal=deal)

        now = datetime.now(UTC)
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now

        await self.session.flush()
        await self._record_mutation_event(
            deal=deal,
            event_type="crm.deal.owner_changed",
            operation="assign_owner",
            changed_fields=owner_result.changed_fields,
            summary="Changed deal owner",
            context=context,
        )
        logger.info(
            "Assigned owner %s to deal %s",
            owner_user_id or "None",
            deal.deal_id,
        )
        return deal

    async def update_account(
        self,
        deal_id: UUID,
        account_payload: dict[str, Any],
        *,
        context: DealWriteContext | None = None,
    ) -> CRMDeal:
        """Update the account snapshot/link for one internal CRM deal."""
        if not account_payload or not any(account_payload.values()):
            raise DealWriteServiceError("No account fields to update", status_code=400)

        deal = await self._load_internal_deal(deal_id)
        now = datetime.now(UTC)
        old_values = {
            "company_id": deal.company_id,
            "company_name": deal.company_name,
            "company_domain": deal.company_domain,
        }

        # Auto-derive domain from website — domain is never sent by the frontend.
        if "website" in account_payload and "domain" not in account_payload:
            derived = self._normalize_domain(account_payload["website"])
            if derived:
                account_payload["domain"] = derived

        # Preserve existing company name when only domain/other fields are updated
        if "name" not in account_payload and deal.company_name:
            account_payload["name"] = deal.company_name

        # Carry over existing company fields so they aren't lost when
        # the domain change creates or links to a different company record.
        needs_company_field_backfill = any(
            field not in account_payload
            for field in ("website", "linkedin_url", "industry", "country")
        )
        if deal.company_id and needs_company_field_backfill:
            existing_company = await self._get_current_company(deal.company_id, deal.integration_id)
            if existing_company:
                for field in ("website", "linkedin_url", "industry", "country"):
                    if field not in account_payload and getattr(existing_company, field, None):
                        account_payload[field] = getattr(existing_company, field)

        try:
            company, fallback_name = await self._account_writer.upsert_company_from_account(
                account_payload, deal.integration_id, now
            )
            if company:
                same_company = (
                    deal.company_id == company.company_id if deal.company_id is not None else False
                )
                if same_company and await self._has_active_company_participant(
                    deal=deal,
                    company_id=company.company_id,
                ):
                    # Fast path for import replays: avoid participant-service churn when
                    # the company link is already present and only company fields changed.
                    self._sync_deal_company_snapshot(deal=deal, company=company, now=now)
                else:
                    await self._replace_company_participant(deal, company, now)
            elif fallback_name:
                # Ambiguous name-only account payloads are kept as a deal-level
                # snapshot instead of linking to an arbitrary company record.
                await self._apply_fallback_company_name(deal, fallback_name, now)
            else:
                raise DealWriteServiceError(
                    "Account payload missing domain or name", status_code=400
                )
        except DealParticipantServiceError as exc:
            raise DealWriteServiceError(exc.message, status_code=exc.status_code) from exc
        except AccountWriteServiceError as exc:
            # Preserve the DealWriteService error contract for API callers while
            # letting AccountWriteService own account-domain validation.
            raise DealWriteServiceError(exc.message, status_code=exc.status_code) from exc

        logger.info("Updated account for internal CRM deal %s", deal.deal_id)
        await self._record_mutation_event(
            deal=deal,
            event_type="crm.deal.account_changed",
            operation="update_account",
            changed_fields=self._collect_changed_fields(old_values=old_values, obj=deal),
            summary="Updated deal account",
            context=context,
        )
        return deal

    async def update_primary_contact(
        self,
        deal_id: UUID,
        *,
        primary_contact_name: str | None,
        primary_contact_email: str | None,
        company_name: str | None = None,
        context: DealWriteContext | None = None,
    ) -> CRMDeal:
        """Update primary contact for an internal CRM deal."""
        if not primary_contact_name and not primary_contact_email:
            raise DealWriteServiceError("No primary contact fields to update", status_code=400)

        deal = await self._load_internal_deal(deal_id)
        now = datetime.now(UTC)
        old_values = {
            "primary_contact_id": deal.primary_contact_id,
            "primary_contact_name": deal.primary_contact_name,
            "primary_contact_email": deal.primary_contact_email,
        }
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
            deal.primary_contact_id = None
            if fallback_name:
                deal.primary_contact_name = fallback_name
            if fallback_email:
                deal.primary_contact_email = fallback_email
            deal.source_modified_at = now
            deal.synced_at = now
            deal.last_seen_at = now
            self.session.add(deal)

        logger.info("Updated primary contact for internal CRM deal %s", deal.deal_id)
        await self._record_mutation_event(
            deal=deal,
            event_type="crm.deal.primary_contact_changed",
            operation="update_primary_contact",
            changed_fields=self._collect_changed_fields(old_values=old_values, obj=deal),
            summary="Updated deal primary contact",
            context=context,
        )
        return deal

    async def _apply_account_and_team(
        self,
        *,
        deal: CRMDeal,
        integration_id: UUID,
        account_payload: dict[str, Any] | None,
        team_members: list[dict[str, Any]] | None,
        contact_participants: list[dict[str, Any]] | None,
        primary_contact_name: str | None,
        primary_contact_email: str | None,
        now: datetime,
    ) -> None:
        """Apply optional account, primary contact, team, and contact participants."""
        try:
            company = None
            fallback_name = None

            if account_payload:
                company, fallback_name = await self._account_writer.upsert_company_from_account(
                    account_payload, integration_id, now
                )

            if company:
                await self._add_company_participant(deal, company, now)
            elif fallback_name:
                # During deal creation an ambiguous account name is still useful
                # as display text, but it must not create a participant link.
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

            if contact_participants:
                await self._add_contact_participants(deal, contact_participants)
        except DealParticipantServiceError as exc:
            raise DealWriteServiceError(exc.message, status_code=exc.status_code) from exc
        except AccountWriteServiceError as exc:
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

        deal.company_id = company.company_id
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
        )
        result = await self.session.execute(stmt)
        for participant in result.scalars().all():
            if participant.related_local_id == company.company_id:
                continue
            participant.is_deleted = True
            participant.source_modified_at = now
            participant.synced_at = now
            participant.last_seen_at = now
            self.session.add(participant)

    async def _has_active_company_participant(
        self,
        *,
        deal: CRMDeal,
        company_id: UUID | None,
    ) -> bool:
        filters = [
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == deal.integration_id,
            col(CRMDealParticipant.deal_id) == deal.deal_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.COMPANY.value,
            col(CRMDealParticipant.is_deleted).is_(False),
        ]
        if company_id is not None:
            filters.append(col(CRMDealParticipant.related_local_id) == company_id)
        else:
            return False

        stmt = select(col(CRMDealParticipant.participant_id)).where(*filters).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    def _sync_deal_company_snapshot(
        self, *, deal: CRMDeal, company: CRMCompany, now: datetime
    ) -> None:
        changed = False
        if deal.company_id != company.company_id:
            deal.company_id = company.company_id
            changed = True
        if deal.company_name != company.name:
            deal.company_name = company.name
            changed = True
        if deal.company_domain != company.domain:
            deal.company_domain = company.domain
            changed = True
        if changed:
            deal.source_modified_at = now
            deal.synced_at = now
            deal.last_seen_at = now
            self.session.add(deal)

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
            col(CRMDealParticipant.is_primary_contact).is_(True),
            col(CRMDealParticipant.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        for participant in result.scalars().all():
            if participant.related_local_id == contact.contact_id:
                continue
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
        if deal.company_id:
            stmt = select(CRMCompany).where(
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.integration_id) == deal.integration_id,
                col(CRMCompany.company_id) == deal.company_id,
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

    async def _add_contact_participants(
        self,
        deal: CRMDeal,
        contact_participants: list[dict[str, Any]],
    ) -> None:
        from src.service.crm.internal.deal_participant_service import DealParticipantService

        if not contact_participants:
            return

        payloads: list[dict[str, Any]] = []
        current_company: CRMCompany | None = None
        current_company_loaded = False
        now = datetime.now(UTC)
        for participant in contact_participants:
            contact_id = participant.get("contact_id")
            if not isinstance(contact_id, UUID):
                raise DealWriteServiceError(
                    "Invalid contact participant contact_id", status_code=400
                )
            source_contact = await self._load_org_contact(contact_id)
            if source_contact.integration_id != deal.integration_id and not current_company_loaded:
                current_company = await self._get_current_company(
                    deal.company_id,
                    deal.integration_id,
                )
                current_company_loaded = True
            contact = await self._ensure_internal_contact_from_source(
                source_contact,
                deal.integration_id,
                current_company,
                now,
            )
            payloads.append(
                {
                    "participant_type": "contact",
                    "related_id": contact.contact_id,
                    "relationship_role": None,
                }
            )

        participant_service = DealParticipantService(
            self.session, self.organisation_id, self.actor_user_id, self.master_session
        )
        await participant_service.add_participants(deal.deal_id, payloads)

    async def _load_org_contact(self, contact_id: UUID) -> CRMContact:
        contact = await self.session.get(CRMContact, contact_id)
        if contact is None or contact.organisation_id != self.organisation_id or contact.is_deleted:
            raise DealWriteServiceError("Contact not found", status_code=404)
        return contact

    async def _ensure_internal_contact_from_source(
        self,
        source_contact: CRMContact,
        integration_id: UUID,
        company: CRMCompany | None,
        now: datetime,
    ) -> CRMContact:
        if source_contact.integration_id == integration_id:
            return source_contact

        email = self._normalize_email(source_contact.email)
        if email:
            stmt = select(CRMContact).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.integration_id) == integration_id,
                col(CRMContact.is_deleted).is_(False),
                func.lower(CRMContact.email) == email,
            )
            result = await self.session.execute(stmt)
            matched_contact = result.scalars().first()
            if matched_contact:
                return matched_contact

        contact_id = uuid4()
        company_name = company.name if company else source_contact.company_name
        contact = CRMContact(
            contact_id=contact_id,
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            first_name=source_contact.first_name,
            last_name=source_contact.last_name,
            full_name=self._build_contact_display_name(source_contact),
            job_title=source_contact.job_title,
            company_name=company_name,
            company_id=company.company_id if company else None,
            email=email,
            additional_emails=list(source_contact.additional_emails or []),
            phone=source_contact.phone,
            mobile_phone=source_contact.mobile_phone,
            website=source_contact.website,
            city=source_contact.city,
            state=source_contact.state,
            postal_code=source_contact.postal_code,
            country=source_contact.country,
            timezone=source_contact.timezone,
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={
                "source": "internal_crm",
                "created_via": "deal_create",
                "copied_from_contact_id": str(source_contact.contact_id),
                "copied_from_integration_id": str(source_contact.integration_id),
            },
        )
        self.session.add(contact)
        await self.session.flush()
        return contact

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

        deal.primary_contact_id = contact.contact_id
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
        return AccountWriteService._normalize_domain(value)

    @staticmethod
    def _normalize_company_name(value: str | None) -> str | None:
        return AccountWriteService.normalize_company_name(value)

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
    def _build_contact_display_name(contact: CRMContact) -> str | None:
        if contact.full_name:
            return contact.full_name
        parts = [part for part in (contact.first_name, contact.last_name) if part]
        return " ".join(parts) or None

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

        # Internal CRM ownership is enforced here so write endpoints cannot
        # mutate provider-synced deals that should remain read-only.
        setup_service = InternalCRMSetupService(
            self.session, self.organisation_id, self.actor_user_id
        )
        integration = await setup_service.get_internal_integration()
        if not integration or deal.integration_id != integration.integration_id:
            raise DealWriteServiceError("External CRM deals are read-only", status_code=409)

        return deal

    async def _get_current_company(
        self,
        company_id: UUID | None,
        integration_id: UUID,
    ) -> CRMCompany | None:
        """Fetch the company currently associated with a deal."""
        filters = [
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted).is_(False),
        ]
        if company_id is not None:
            filters.append(col(CRMCompany.company_id) == company_id)
        else:
            return None
        stmt = select(CRMCompany).where(*filters)
        result = await self.session.execute(stmt)
        return result.scalars().first()

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
