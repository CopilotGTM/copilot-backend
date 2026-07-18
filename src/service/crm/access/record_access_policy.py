"""Relationship-based access policy for CRM deals and leads.

API services pass an authenticated actor into this module before querying or
mutating CRM records. Trusted workers pass an explicit system actor; omission
always denies access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables.user_models import UserAppRole
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealMutationEvent,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLead,
    CRMLeadParticipant,
    CRMLeadParticipantType,
)

DealAccessLevel = Literal["view", "collaborate", "control", "admin"]
LeadAccessLevel = Literal["view", "collaborate", "control", "admin"]


@dataclass(frozen=True)
class CRMAccessActor:
    """Authenticated app-user identity used by CRM authorization checks."""

    user_id: UUID
    user_role: UserAppRole | str = UserAppRole.MEMBER
    email: str | None = None

    @property
    def is_admin(self) -> bool:
        """Return whether the actor has organisation-admin authority."""
        value = getattr(self.user_role, "value", self.user_role)
        return str(value).lower() == UserAppRole.ADMIN.value

    @property
    def normalized_email(self) -> str | None:
        """Return the verified actor email in the form stored by CRM projections."""
        normalized = self.email.strip().lower() if self.email else None
        return normalized or None


@dataclass(frozen=True)
class CRMSystemActor:
    """Explicit authority for trusted imports, syncs, and background workers.

    Human-facing callers must use ``CRMAccessActor``. Requiring a named system
    principal prevents an omitted actor from silently becoming org-wide access.
    """

    source: str


CRMActor = CRMAccessActor | CRMSystemActor


@dataclass(frozen=True)
class RecordAccessDecision:
    """Server-authoritative capabilities for one CRM record."""

    can_view: bool
    can_collaborate: bool
    can_control: bool
    can_administer: bool
    is_owner: bool = False
    is_creator: bool = False
    is_participant: bool = False


class CRMRecordAccessError(Exception):
    """Raised when a CRM record is missing or the actor lacks an action."""

    def __init__(self, message: str, status_code: int) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealAccessPolicy:
    """Build deal visibility predicates and enforce relationship-based actions."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        actor: CRMActor | None,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.actor = actor

    def visible_deal_predicate(self, deal_columns: Any | None = None) -> Any:
        """Return the mandatory SQL predicate for deals visible to this actor.

        A missing actor denies access. Trusted workers must pass CRMSystemActor
        explicitly so a forgotten API or agent actor cannot fail open.
        """
        c = deal_columns if deal_columns is not None else cast(Any, CRMDeal).__table__.c
        if self.actor is None:
            return false()
        if isinstance(self.actor, CRMSystemActor) or self.actor.is_admin:
            return c.organisation_id == self.organisation_id

        # Named aliases keep these authorization subqueries independent when a
        # caller's outer query already joins participants or mutation events.
        participant = cast(Any, CRMDealParticipant).__table__.alias("access_deal_participant").c
        event = cast(Any, CRMDealMutationEvent).__table__.alias("access_deal_event").c
        actor = self.actor
        participant_identity = [participant.related_local_id == actor.user_id]
        owner_identity = [c.owner_id == actor.user_id]
        if actor.normalized_email:
            participant_identity.append(
                func.lower(func.trim(participant.related_email)) == actor.normalized_email
            )
            owner_identity.append(func.lower(func.trim(c.owner_email)) == actor.normalized_email)

        # Creation events cover existing internal deals whose creator column was
        # not populated by the older writer; new deals use the direct column.
        legacy_creator = exists(
            select(1).where(
                event.organisation_id == self.organisation_id,
                event.entity_type == "deal",
                event.entity_id == c.deal_id,
                event.operation == "create_deal",
                event.actor_user_id == actor.user_id,
            )
        )
        active_participant = exists(
            select(1).where(
                participant.organisation_id == self.organisation_id,
                participant.deal_id == c.deal_id,
                participant.participant_type == CRMParticipantType.USER.value,
                participant.is_deleted.is_(False),
                or_(*participant_identity),
            )
        )
        return or_(
            *owner_identity,
            c.created_by_user_id == actor.user_id,
            legacy_creator,
            active_participant,
        )

    async def decision_for_deal(self, deal: CRMDeal) -> RecordAccessDecision:
        """Resolve view, collaboration, and control rights for a loaded deal."""
        if self.actor is None:
            return RecordAccessDecision(False, False, False, False)
        if isinstance(self.actor, CRMSystemActor) or self.actor.is_admin:
            return RecordAccessDecision(True, True, True, True)

        actor = self.actor
        actor_email = actor.normalized_email
        is_owner = deal.owner_id == actor.user_id or bool(
            actor_email and deal.owner_email and deal.owner_email.strip().lower() == actor_email
        )
        is_creator = deal.created_by_user_id == actor.user_id
        if not is_creator:
            event = cast(Any, CRMDealMutationEvent).__table__.c
            is_creator = bool(
                await self.session.scalar(
                    select(
                        exists().where(
                            event.organisation_id == self.organisation_id,
                            event.entity_type == "deal",
                            event.entity_id == deal.deal_id,
                            event.operation == "create_deal",
                            event.actor_user_id == actor.user_id,
                        )
                    )
                )
            )

        participant = cast(Any, CRMDealParticipant).__table__.c
        participant_identity = [participant.related_local_id == actor.user_id]
        if actor_email:
            participant_identity.append(
                func.lower(func.trim(participant.related_email)) == actor_email
            )
        is_participant = bool(
            await self.session.scalar(
                select(
                    exists().where(
                        participant.organisation_id == self.organisation_id,
                        participant.deal_id == deal.deal_id,
                        participant.participant_type == CRMParticipantType.USER.value,
                        participant.is_deleted.is_(False),
                        or_(*participant_identity),
                    )
                )
            )
        )
        creator_controls_unassigned = deal.owner_id is None and is_creator
        can_view = is_owner or is_creator or is_participant
        return RecordAccessDecision(
            can_view=can_view,
            can_collaborate=is_owner or is_participant or creator_controls_unassigned,
            can_control=is_owner or creator_controls_unassigned,
            can_administer=False,
            is_owner=is_owner,
            is_creator=is_creator,
            is_participant=is_participant,
        )

    async def require_deal(
        self,
        deal_id: UUID,
        level: DealAccessLevel = "view",
    ) -> tuple[CRMDeal, RecordAccessDecision]:
        """Load one active org deal and enforce the requested access level."""
        # The org condition is the tenant trust boundary; a client-supplied deal
        # UUID is never sufficient to load a record by itself.
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.is_deleted).is_(False),
        )
        deal = (await self.session.execute(stmt)).scalar_one_or_none()
        if deal is None:
            raise CRMRecordAccessError("Deal not found", 404)

        decision = await self.decision_for_deal(deal)
        allowed = {
            "view": decision.can_view,
            "collaborate": decision.can_collaborate,
            "control": decision.can_control,
            "admin": decision.can_administer,
        }[level]
        if not decision.can_view:
            raise CRMRecordAccessError("Deal not found", 404)
        if not allowed:
            raise CRMRecordAccessError("Not permitted to perform this deal action", 403)
        return deal, decision


class LeadAccessPolicy:
    """Build lead visibility predicates and enforce claim-based member access."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        actor: CRMActor | None,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.actor = actor

    def visible_lead_predicate(self, lead_columns: Any | None = None) -> Any:
        """Return admin-wide or member relationship/unassigned lead visibility."""
        c = lead_columns if lead_columns is not None else cast(Any, CRMLead).__table__.c
        if self.actor is None:
            return false()
        if isinstance(self.actor, CRMSystemActor) or self.actor.is_admin:
            return c.organisation_id == self.organisation_id

        participant = cast(Any, CRMLeadParticipant).__table__.alias("access_lead_participant").c
        actor = self.actor
        participant_identity = [participant.related_local_id == actor.user_id]
        owner_identity = [c.owner_id == actor.user_id]
        if actor.normalized_email:
            participant_identity.append(
                func.lower(func.trim(participant.email)) == actor.normalized_email
            )
            owner_identity.append(func.lower(func.trim(c.owner_email)) == actor.normalized_email)
        active_participant = exists(
            select(1).where(
                participant.organisation_id == self.organisation_id,
                participant.lead_id == c.lead_id,
                participant.participant_type == CRMLeadParticipantType.USER.value,
                participant.is_deleted.is_(False),
                or_(*participant_identity),
            )
        )
        return or_(
            c.owner_id.is_(None),
            *owner_identity,
            c.created_by_user_id == actor.user_id,
            active_participant,
        )

    async def require_lead(
        self,
        lead_id: UUID,
        level: LeadAccessLevel = "view",
    ) -> CRMLead:
        """Load one visible lead and require ownership for member mutations."""
        c = cast(Any, CRMLead).__table__.c
        stmt = select(CRMLead).where(
            c.organisation_id == self.organisation_id,
            c.lead_id == lead_id,
            c.is_deleted.is_(False),
            self.visible_lead_predicate(c),
        )
        lead = (await self.session.execute(stmt)).scalar_one_or_none()
        if lead is None:
            raise CRMRecordAccessError("Lead not found", 404)
        if level == "view":
            return lead

        actor = self.actor
        if actor is None:
            raise CRMRecordAccessError("Lead not found", 404)
        if isinstance(actor, CRMSystemActor) or actor.is_admin:
            return lead
        actor_email = actor.normalized_email
        is_owner = lead.owner_id == actor.user_id or bool(
            actor_email and lead.owner_email and lead.owner_email.strip().lower() == actor_email
        )
        if not is_owner:
            raise CRMRecordAccessError("Claim or own the lead before changing it", 403)
        return lead
