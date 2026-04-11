"""Service for internal CRM contact CRUD operations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.service.crm.internal.internal_crm_user_service import InternalCRMUserService

logger = get_logger(__name__)

EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class CRMContactServiceError(Exception):
    """Custom exception for CRM contact service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class CRMContactService:
    """Service for managing internal CRM contacts."""

    def __init__(self, session: AsyncSession, organisation_id: UUID, actor_user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            logger.debug("CRMContactService close exception", exc_info=True)

    async def list_contacts(
        self, *, limit: int = 50, cursor: str | None = None, q: str | None = None
    ) -> tuple[list[CRMContact], str | None]:
        integration_id = await self._get_internal_integration_id(require=False)
        if not integration_id:
            return [], None

        c = cast(Any, CRMContact).__table__.c
        stmt = select(CRMContact).where(
            c.organisation_id == self.organisation_id,
            c.integration_id == integration_id,
            c.is_deleted.is_(False),
        )

        if q:
            search = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    c.first_name.ilike(search),
                    c.last_name.ilike(search),
                    c.full_name.ilike(search),
                    c.email.ilike(search),
                    c.company_name.ilike(search),
                )
            )

        if cursor:
            try:
                ts, contact_id = decode_cursor(cursor)
            except Exception as exc:
                raise CRMContactServiceError("Invalid cursor", status_code=400) from exc

            stmt = stmt.where(
                or_(
                    c.created_at < ts,
                    and_(c.created_at == ts, c.contact_id < contact_id),
                )
            )

        stmt = stmt.order_by(c.created_at.desc(), c.contact_id.desc()).limit(limit + 1)
        result = await self.session.execute(stmt)
        rows = list(result.scalars().all())

        has_more = len(rows) == limit + 1
        items = rows[:limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].contact_id)
            if has_more and items
            else None
        )
        return items, next_cursor

    async def create_contact(self, payload: dict[str, Any]) -> CRMContact:
        integration_id = await self._get_internal_integration_id(require=True)
        now = datetime.now(UTC)

        contact_data = self._normalize_payload(payload)
        email = contact_data.get("email")
        if email and not self._is_valid_email(email):
            raise CRMContactServiceError("Invalid email address", status_code=400)

        additional_emails = self._normalize_email_list(contact_data.get("additional_emails") or [])
        if email:
            additional_emails = [addr for addr in additional_emails if addr != email]
        contact_data["additional_emails"] = additional_emails

        if email:
            await self._ensure_email_unique(integration_id, email)

        owner = None
        owner_user_id = contact_data.get("owner_user_id")
        if owner_user_id:
            owner_service = InternalCRMUserService(
                self.session, self.organisation_id, self.actor_user_id
            )
            owner = await owner_service.ensure_crm_user(owner_user_id)

        contact_id = uuid4()
        properties = dict(contact_data.get("properties") or {})
        properties["source"] = "internal_crm"

        contact = CRMContact(
            contact_id=contact_id,
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            external_id=str(contact_id),
            first_name=contact_data.get("first_name"),
            last_name=contact_data.get("last_name"),
            full_name=self._build_full_name(
                contact_data.get("first_name"),
                contact_data.get("last_name"),
                contact_data.get("full_name"),
            ),
            job_title=contact_data.get("job_title"),
            company_name=contact_data.get("company_name"),
            email=email,
            additional_emails=additional_emails,
            phone=contact_data.get("phone"),
            mobile_phone=contact_data.get("mobile_phone"),
            website=contact_data.get("website"),
            lifecycle_stage=contact_data.get("lifecycle_stage"),
            lead_status=contact_data.get("lead_status"),
            marketing_status=contact_data.get("marketing_status"),
            address=contact_data.get("address"),
            city=contact_data.get("city"),
            state=contact_data.get("state"),
            postal_code=contact_data.get("postal_code"),
            country=contact_data.get("country"),
            timezone=contact_data.get("timezone"),
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties=properties,
        )

        if owner:
            contact.owner_id = owner.user_id
            contact.owner_external_id = owner.external_id
            contact.owner_name = owner.full_name
            contact.owner_email = owner.email

        self.session.add(contact)
        return contact

    async def update_contact(
        self, contact_id: UUID, payload: dict[str, Any]
    ) -> tuple[CRMContact, list[UUID], UUID]:
        integration_id = await self._get_internal_integration_id(require=True)
        contact = await self._get_internal_contact(contact_id, integration_id)

        contact_data = self._normalize_payload(payload)
        new_email = contact_data.get("email")

        email_changed = False
        name_updated = any(
            field in contact_data for field in ("first_name", "last_name", "full_name")
        )
        job_title_updated = "job_title" in contact_data
        if "email" in contact_data:
            if new_email and not self._is_valid_email(new_email):
                raise CRMContactServiceError("Invalid email address", status_code=400)

            current_email = (contact.email or "").lower() if contact.email else None
            incoming_email = new_email.lower() if new_email else None
            email_changed = current_email != incoming_email

            if email_changed and incoming_email:
                await self._ensure_email_unique(integration_id, incoming_email, exclude=contact_id)
                await self._ensure_stakeholder_email_available(contact_id, incoming_email)
            if email_changed and incoming_email is None:
                await self._ensure_email_clearable(contact_id)

        if "additional_emails" in contact_data:
            additional_emails = self._normalize_email_list(
                contact_data.get("additional_emails") or []
            )
            primary_email = new_email if "email" in contact_data else contact.email
            if primary_email:
                additional_emails = [addr for addr in additional_emails if addr != primary_email]
            contact.additional_emails = additional_emails

        if "first_name" in contact_data:
            contact.first_name = contact_data.get("first_name")
        if "last_name" in contact_data:
            contact.last_name = contact_data.get("last_name")
        if "full_name" in contact_data:
            contact.full_name = contact_data.get("full_name")

        if "first_name" in contact_data or "last_name" in contact_data:
            if "full_name" not in contact_data:
                contact.full_name = self._build_full_name(
                    contact.first_name,
                    contact.last_name,
                    None,
                )

        for field in [
            "job_title",
            "company_name",
            "phone",
            "mobile_phone",
            "website",
            "lifecycle_stage",
            "lead_status",
            "marketing_status",
            "address",
            "city",
            "state",
            "postal_code",
            "country",
            "timezone",
        ]:
            if field in contact_data:
                setattr(contact, field, contact_data.get(field))

        if "email" in contact_data:
            contact.email = new_email

        if "owner_user_id" in contact_data:
            owner_user_id = contact_data.get("owner_user_id")
            if owner_user_id:
                owner_service = InternalCRMUserService(
                    self.session, self.organisation_id, self.actor_user_id
                )
                owner = await owner_service.ensure_crm_user(owner_user_id)
                contact.owner_id = owner.user_id
                contact.owner_external_id = owner.external_id
                contact.owner_name = owner.full_name
                contact.owner_email = owner.email
            else:
                contact.owner_id = None
                contact.owner_external_id = None
                contact.owner_name = None
                contact.owner_email = None

        if "properties" in contact_data:
            properties = dict(contact.properties or {})
            properties.update(contact_data.get("properties") or {})
            properties["source"] = "internal_crm"
            contact.properties = properties

        now = datetime.now(UTC)
        contact.source_modified_at = now
        contact.synced_at = now
        contact.last_seen_at = now
        self.session.add(contact)

        deal_ids: list[UUID] = []
        if email_changed or name_updated or job_title_updated:
            deal_ids = await self._sync_contact_references(
                integration_id=integration_id,
                contact=contact,
                new_email=new_email,
                email_changed=email_changed,
                name_updated=name_updated,
                job_title_updated=job_title_updated,
                updated_at=now,
            )

        return contact, deal_ids, integration_id

    async def delete_contact(self, contact_id: UUID) -> tuple[CRMContact, list[UUID], UUID]:
        integration_id = await self._get_internal_integration_id(require=True)
        contact = await self._get_internal_contact(contact_id, integration_id)

        now = datetime.now(UTC)
        contact.is_deleted = True
        contact.source_modified_at = now
        contact.synced_at = now
        contact.last_seen_at = now
        self.session.add(contact)

        deal_ids: list[UUID] = []
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == integration_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.related_local_id) == contact_id,
            col(CRMDealParticipant.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        participants = list(result.scalars().all())

        for participant in participants:
            participant.is_deleted = True
            participant.related_email = None
            participant.source_modified_at = now
            participant.synced_at = now
            participant.last_seen_at = now
            self.session.add(participant)
            deal_ids.append(participant.deal_id)

        return contact, sorted(set(deal_ids)), integration_id

    async def _get_internal_integration_id(self, *, require: bool) -> UUID | None:
        setup_service = InternalCRMSetupService(
            self.session, self.organisation_id, self.actor_user_id
        )
        integration = await setup_service.get_internal_integration()
        if not integration:
            if require:
                raise CRMContactServiceError("Internal CRM is not configured", status_code=409)
            return None
        return integration.integration_id

    async def _get_internal_contact(self, contact_id: UUID, integration_id: UUID) -> CRMContact:
        contact = await self.session.get(CRMContact, contact_id)
        if not contact or contact.organisation_id != self.organisation_id:
            raise CRMContactServiceError("Contact not found", status_code=404)
        if contact.integration_id != integration_id:
            raise CRMContactServiceError("External CRM contacts are read-only", status_code=409)
        if contact.is_deleted:
            raise CRMContactServiceError("Contact is deleted", status_code=404)
        return contact

    async def _ensure_email_unique(
        self,
        integration_id: UUID,
        email: str,
        *,
        exclude: UUID | None = None,
    ) -> None:
        c = cast(Any, CRMContact).__table__.c
        stmt = select(CRMContact).where(
            c.organisation_id == self.organisation_id,
            c.integration_id == integration_id,
            func.lower(c.email) == email.lower(),
            c.is_deleted.is_(False),
        )
        if exclude:
            stmt = stmt.where(c.contact_id != exclude)
        result = await self.session.execute(stmt)
        existing = result.scalars().first()
        if existing:
            raise CRMContactServiceError("Contact with this email already exists", 409)

    async def _ensure_email_clearable(self, contact_id: UUID) -> None:
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.contact_id) == contact_id,
            col(DealStakeholder.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        if result.scalars().first():
            raise CRMContactServiceError(
                "Cannot clear email for a contact linked to stakeholders", status_code=400
            )

    async def _ensure_stakeholder_email_available(self, contact_id: UUID, email: str) -> None:
        stmt = select(col(DealStakeholder.deal_id)).where(
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.contact_id) == contact_id,
            col(DealStakeholder.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        deal_ids = [row[0] for row in result.all()]
        if not deal_ids:
            return

        conflict_stmt = select(DealStakeholder).where(
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.deal_id).in_(deal_ids),
            func.lower(col(DealStakeholder.email)) == email.lower(),
            col(DealStakeholder.contact_id) != contact_id,
            col(DealStakeholder.is_deleted).is_(False),
        )
        conflict_result = await self.session.execute(conflict_stmt)
        if conflict_result.scalars().first():
            raise CRMContactServiceError(
                "Email already exists for another stakeholder on this deal", status_code=409
            )

    async def _sync_contact_references(
        self,
        *,
        integration_id: UUID,
        contact: CRMContact,
        new_email: str | None,
        email_changed: bool,
        name_updated: bool,
        job_title_updated: bool,
        updated_at: datetime,
    ) -> list[UUID]:
        display_name = contact.full_name or self._build_full_name(
            contact.first_name, contact.last_name, None
        )

        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == integration_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.related_local_id) == contact.contact_id,
            col(CRMDealParticipant.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        participants = list(result.scalars().all())

        deal_ids: list[UUID] = []
        for participant in participants:
            if email_changed:
                participant.related_email = new_email
            if name_updated:
                participant.related_name = display_name
            participant.source_modified_at = updated_at
            participant.synced_at = updated_at
            participant.last_seen_at = updated_at
            self.session.add(participant)
            deal_ids.append(participant.deal_id)

        if email_changed or name_updated or job_title_updated:
            stmt = select(DealStakeholder).where(
                col(DealStakeholder.organisation_id) == self.organisation_id,
                col(DealStakeholder.contact_id) == contact.contact_id,
                col(DealStakeholder.is_deleted).is_(False),
            )
            stake_result = await self.session.execute(stmt)
            stakeholders = list(stake_result.scalars().all())

            for stakeholder in stakeholders:
                if email_changed and new_email is not None:
                    stakeholder.email = new_email
                if name_updated:
                    stakeholder.name = display_name
                if job_title_updated:
                    stakeholder.job_title = contact.job_title
                stakeholder.updated_at = updated_at
                self.session.add(stakeholder)

        return sorted(set(deal_ids))

    @staticmethod
    def _clean_string(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, str):
                cleaned[key] = self._clean_string(value)
            else:
                cleaned[key] = value

        email = cleaned.get("email")
        if email:
            cleaned["email"] = self._normalize_email(email)
        return cleaned

    @staticmethod
    def _normalize_email(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        return cleaned or None

    def _normalize_email_list(self, values: Iterable[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = self._normalize_email(raw)
            if not value:
                continue
            if not self._is_valid_email(value):
                raise CRMContactServiceError(
                    f"Invalid email address in additional_emails: {raw}", status_code=400
                )
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    @staticmethod
    def _is_valid_email(value: str) -> bool:
        return bool(EMAIL_PATTERN.match(value))

    @staticmethod
    def _build_full_name(
        first_name: str | None,
        last_name: str | None,
        full_name: str | None,
    ) -> str | None:
        explicit = CRMContactService._clean_string(full_name)
        if explicit:
            return explicit
        parts = [p for p in [first_name, last_name] if p]
        return " ".join(parts) if parts else None
