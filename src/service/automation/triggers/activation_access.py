"""Organization-scoped SQL for CRM trigger activation and replay-safe run creation.

Unlike user-facing trigger CRUD, this layer is called by an internal event producer and therefore
does not accept an authenticated owner. The deal's canonical owner selects candidates, while every
query still repeats the immutable organization boundary.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import (
    AgentRun,
    AgentStatus,
    AutomationTargetType,
    AutomationTrigger,
    AutomationTriggerStatus,
    AutomationTriggerType,
    UserAgent,
)
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealMutationEvent


class TriggerActivationAccess:
    """Load one CRM source event and idempotently persist its matching agent runs."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def get_crm_event(self, event_id: UUID) -> CRMDealMutationEvent | None:
        """Load one immutable event without trusting its globally unique identifier alone."""

        columns = cast(Any, CRMDealMutationEvent).__table__.c
        statement = select(CRMDealMutationEvent).where(
            columns.organisation_id == self.organisation_id,
            columns.event_id == event_id,
        )
        return (await self.session.execute(statement)).scalars().first()

    async def get_deal(self, deal_id: UUID) -> CRMDeal | None:
        """Load the current non-deleted deal snapshot inside the source event's organization."""

        columns = cast(Any, CRMDeal).__table__.c
        statement = select(CRMDeal).where(
            columns.organisation_id == self.organisation_id,
            columns.deal_id == deal_id,
            columns.is_deleted.is_(False),
        )
        return (await self.session.execute(statement)).scalars().first()

    async def get_crm_entity(self, entity_type: str, entity_id: UUID) -> Any | None:
        """Load the owner-bearing source row, including soft-deleted lifecycle-event entities."""

        model_and_id = {
            "deal": (CRMDeal, "deal_id"),
            "contact": (CRMContact, "contact_id"),
            "account": (CRMCompany, "company_id"),
        }.get(entity_type)
        if model_and_id is None:
            return None
        model, id_name = model_and_id
        columns = cast(Any, model).__table__.c
        statement = select(model).where(
            columns.organisation_id == self.organisation_id,
            getattr(columns, id_name) == entity_id,
        )
        return (await self.session.execute(statement)).scalars().first()

    async def list_crm_event_candidates(
        self, *, owner_user_id: UUID, source_domain: str, event_type: str
    ) -> list[AutomationTrigger]:
        """Return active owner triggers whose mutable target agent is still active."""

        trigger_columns = cast(Any, AutomationTrigger).__table__.c
        agent_columns = cast(Any, UserAgent).__table__.c
        statement = (
            select(AutomationTrigger)
            .join(
                UserAgent,
                agent_columns.agent_id == trigger_columns.target_id,
            )
            .where(
                trigger_columns.organisation_id == self.organisation_id,
                trigger_columns.owner_user_id == owner_user_id,
                trigger_columns.target_type == AutomationTargetType.AGENT.value,
                trigger_columns.trigger_type == AutomationTriggerType.EVENT.value,
                trigger_columns.source_domain == source_domain,
                trigger_columns.event_type == event_type,
                trigger_columns.status == AutomationTriggerStatus.ACTIVE.value,
                agent_columns.organisation_id == self.organisation_id,
                agent_columns.owner_user_id == owner_user_id,
                agent_columns.status == AgentStatus.ACTIVE.value,
            )
            .order_by(trigger_columns.trigger_id)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def insert_run_if_absent(self, run: AgentRun) -> bool:
        """Insert one run or report a replay without aborting the surrounding CRM transaction."""

        run_table = cast(Any, AgentRun).__table__
        columns = run_table.c
        values = {column.name: getattr(run, column.name) for column in run_table.columns}
        statement = (
            insert(AgentRun)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=[columns.organisation_id, columns.idempotency_key],
                index_where=columns.idempotency_key.is_not(None),
            )
            .returning(columns.run_id)
        )
        inserted_run_id = (await self.session.execute(statement)).scalar_one_or_none()
        return inserted_run_id is not None

    async def get_run_by_idempotency_key(self, idempotency_key: str) -> AgentRun | None:
        """Return the run created by an earlier delivery of the same source occurrence."""

        columns = cast(Any, AgentRun).__table__.c
        statement = select(AgentRun).where(
            columns.organisation_id == self.organisation_id,
            columns.idempotency_key == idempotency_key,
        )
        return (await self.session.execute(statement)).scalars().first()
