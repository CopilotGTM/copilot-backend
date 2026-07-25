"""Query read-only email messages for CRM sidebar scopes."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from src.api.schemas.crm_email_models import CRMEmailItem, CRMEmailPage
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.domain.models.organisation_tables.sync.crm.activities import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityObjectType,
    CRMActivityType,
)
from src.domain.models.organisation_tables.sync.email_models import DealEmailAssociation, Email
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)

_EMAIL_INTEGRATION_TYPES = (IntegrationType.GOOGLE, IntegrationType.MICROSOFT)
_BODY_PREVIEW_MAX_LENGTH = 1000


class CRMEmailScope(StrEnum):
    """Supported CRM sidebar email scopes."""

    OVERALL = "overall"
    CONTACT = "contact"
    ACCOUNT = "account"
    DEAL = "deal"


def _encode_cursor(email: Email) -> str:
    payload = {"received_at": email.received_at.isoformat(), "email_id": str(email.email_id)}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(payload["received_at"]), UUID(payload["email_id"])
    except Exception as exc:
        raise ValueError("Invalid cursor") from exc


class CRMEmailReadService:
    """Tenant and mailbox-owner scoped reader for synchronized email messages."""

    def __init__(self, session: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def close(self) -> None:
        """Close the request-owned organization session."""
        await self.session.close()

    async def list_emails(
        self,
        *,
        scope: CRMEmailScope,
        object_id: UUID | None = None,
        integration_id: UUID | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> CRMEmailPage:
        """Return a page of messages visible in one CRM sidebar context."""
        if scope is not CRMEmailScope.OVERALL and object_id is None:
            raise ValueError("object_id is required for CRM object email scopes")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        email_t = Email.__table__.c
        integration_t = KnowledgeIntegration.__table__.c
        where = [
            email_t.organisation_id == self.organisation_id,
            email_t.is_deleted.is_(False),
            integration_t.organisation_id == self.organisation_id,
            integration_t.created_by_user_id == self.user_id,
            integration_t.integration_type.in_(_EMAIL_INTEGRATION_TYPES),
            integration_t.is_active.is_(True),
            integration_t.requires_reauth.is_(False),
        ]
        stmt = select(Email).join(
            KnowledgeIntegration,
            integration_t.integration_id == email_t.integration_id,
        )

        if integration_id is not None:
            where.append(email_t.integration_id == integration_id)

        if scope in (CRMEmailScope.CONTACT, CRMEmailScope.ACCOUNT):
            object_type = (
                CRMActivityObjectType.CONTACT.value
                if scope is CRMEmailScope.CONTACT
                else CRMActivityObjectType.COMPANY.value
            )
            activity_t = CRMActivity.__table__.c
            link_t = CRMActivityLink.__table__.c
            canonical_email = aliased(Email, name="canonical_emails")
            stmt = (
                stmt.join(
                    canonical_email,
                    and_(
                        canonical_email.organisation_id == self.organisation_id,
                        or_(
                            canonical_email.email_id == email_t.email_id,
                            and_(
                                canonical_email.internet_message_id.is_not(None),
                                email_t.internet_message_id.is_not(None),
                                canonical_email.internet_message_id == email_t.internet_message_id,
                            ),
                        ),
                    ),
                )
                .join(
                    CRMActivity,
                    activity_t.source_record_id == cast(canonical_email.email_id, String),
                )
                .join(CRMActivityLink, link_t.activity_id == activity_t.activity_id)
                .distinct()
            )
            where.extend(
                [
                    activity_t.organisation_id == self.organisation_id,
                    activity_t.activity_type == CRMActivityType.EMAIL.value,
                    activity_t.is_deleted.is_(False),
                    link_t.organisation_id == self.organisation_id,
                    link_t.object_type == object_type,
                    link_t.object_id == object_id,
                ]
            )
        elif scope is CRMEmailScope.DEAL:
            association_t = DealEmailAssociation.__table__.c
            stmt = stmt.join(
                DealEmailAssociation,
                and_(
                    association_t.organisation_id == email_t.organisation_id,
                    association_t.internet_message_id == email_t.internet_message_id,
                ),
            ).distinct()
            where.extend(
                [
                    association_t.organisation_id == self.organisation_id,
                    association_t.deal_id == object_id,
                    association_t.status == AssociationStatus.ACTIVE.value,
                ]
            )

        if cursor:
            received_at, email_id = _decode_cursor(cursor)
            where.append(
                or_(
                    email_t.received_at < received_at,
                    and_(email_t.received_at == received_at, email_t.email_id < email_id),
                )
            )

        result = await self.session.execute(
            stmt.where(*where)
            .order_by(email_t.received_at.desc(), email_t.email_id.desc())
            .limit(limit + 1)
        )
        rows = result.scalars().all()
        page_rows = rows[:limit]
        return CRMEmailPage(
            items=[self._present(email) for email in page_rows],
            cursor=_encode_cursor(page_rows[-1]) if len(rows) > limit and page_rows else None,
        )

    @staticmethod
    def _present(email: Email) -> CRMEmailItem:
        """Convert a synced message to the bounded sidebar contract."""
        body = email.body_text or email.snippet
        return CRMEmailItem(
            email_id=email.email_id,
            integration_id=email.integration_id,
            internet_message_id=email.internet_message_id,
            thread_id=email.thread_id,
            subject=email.subject,
            from_name=email.from_name,
            from_email=email.from_email,
            to_emails=email.to_emails or [],
            cc_emails=email.cc_emails or [],
            received_at=email.received_at,
            sent_at=email.sent_at,
            direction=getattr(email.direction, "value", email.direction),
            is_read=email.is_read,
            is_important=email.is_important,
            has_attachments=email.has_attachments,
            labels=email.labels or [],
            snippet=email.snippet,
            body_preview=body[:_BODY_PREVIEW_MAX_LENGTH] if body else None,
        )
