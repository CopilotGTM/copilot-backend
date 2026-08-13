"""Tenant-scoped candidate, readiness, lineage, and run persistence queries."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import String, and_, func, literal, or_, select, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.master_tables import (
    AdminUserStatus,
    SupabaseUserStatus,
    User,
)
from src.domain.models.organisation_tables import (
    AgentRun,
    AgentStatus,
    AutomationTrigger,
    AutomationTriggerStatus,
    CRMCompany,
    CRMContact,
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
    CRMUser,
    UserAgent,
)
from src.domain.models.organisation_tables.sync.email_models import Email
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting
from src.service.automation.producers.meeting import load_meeting_event_context
from src.service.automation.triggers.options import AutomationTriggerOptionAccess


class TriggerActivationAccess:
    """Read current binding readiness and idempotently create tenant runs."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.options = AutomationTriggerOptionAccess(session, organisation_id)

    async def list_event_candidates(self, event_key: str) -> list[AutomationTrigger]:
        """Return active match bindings selected by the event-key GIN projection."""

        columns = cast(Any, AutomationTrigger).__table__.c
        statement = (
            select(AutomationTrigger)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.family == "event",
                columns.status == AutomationTriggerStatus.ACTIVE.value,
                columns.match_event_keys.contains([event_key]),
            )
            .order_by(columns.trigger_id)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def get_direct_candidate(self, trigger_id: UUID) -> AutomationTrigger | None:
        """Load one active non-match binding for a direct event route."""

        columns = cast(Any, AutomationTrigger).__table__.c
        statement = select(AutomationTrigger).where(
            columns.organisation_id == self.organisation_id,
            columns.trigger_id == trigger_id,
            columns.family != "event",
            columns.status == AutomationTriggerStatus.ACTIVE.value,
        )
        return (await self.session.execute(statement)).scalars().first()

    async def list_active_agent_delivery_configs(
        self,
        agent_ids: set[UUID],
    ) -> dict[UUID, dict[str, Any]]:
        """Return current delivery decisions for active executable agents."""

        if not agent_ids:
            return {}
        columns = cast(Any, UserAgent).__table__.c
        statement = select(columns.agent_id, columns.output_delivery_config).where(
            columns.organisation_id == self.organisation_id,
            columns.agent_id.in_(agent_ids),
            columns.status == AgentStatus.ACTIVE.value,
        )
        return {
            row.agent_id: dict(row.output_delivery_config)
            for row in (await self.session.execute(statement)).all()
        }

    async def list_active_user_ids(self, user_ids: set[UUID]) -> set[UUID]:
        """Return active approved principals that still belong to this tenant."""

        if not user_ids:
            return set()
        columns = cast(Any, User).__table__.c
        statement = select(columns.user_id).where(
            columns.organisation_id == self.organisation_id,
            columns.user_id.in_(user_ids),
            sa_cast(columns.status, String) == SupabaseUserStatus.ACTIVE.value,
            sa_cast(columns.admin_status, String) == AdminUserStatus.APPROVED.value,
        )
        return set((await self.session.execute(statement)).scalars().all())

    async def list_crm_user_ids_by_principal(
        self,
        principal_ids: set[UUID],
    ) -> dict[UUID, set[UUID]]:
        """Map active workspace users to provider CRM-owner rows by stable ID or email."""

        if not principal_ids:
            return {}
        user_columns = cast(Any, User).__table__.c
        crm_user_columns = cast(Any, CRMUser).__table__.c
        statement = (
            select(
                user_columns.user_id.label("principal_id"),
                crm_user_columns.user_id.label("crm_user_id"),
            )
            .select_from(User)
            .join(
                CRMUser,
                and_(
                    crm_user_columns.organisation_id == self.organisation_id,
                    crm_user_columns.is_deleted.is_(False),
                    or_(
                        crm_user_columns.user_id == user_columns.user_id,
                        and_(
                            user_columns.email.is_not(None),
                            crm_user_columns.email.is_not(None),
                            func.lower(func.trim(crm_user_columns.email))
                            == func.lower(func.trim(user_columns.email)),
                        ),
                    ),
                ),
            )
            .where(
                user_columns.organisation_id == self.organisation_id,
                user_columns.user_id.in_(principal_ids),
                sa_cast(user_columns.status, String) == SupabaseUserStatus.ACTIVE.value,
                sa_cast(user_columns.admin_status, String) == AdminUserStatus.APPROVED.value,
            )
        )
        mapped: dict[UUID, set[UUID]] = {}
        for row in (await self.session.execute(statement)).all():
            mapped.setdefault(row.principal_id, set()).add(row.crm_user_id)
        return mapped

    async def validate_option_selection(self, source: str, values: set[str]) -> None:
        """Revalidate saved stable IDs against current tenant metadata."""

        await self.options.validate_option_selection(source, values)

    async def list_subject_participant_user_ids(
        self,
        subject_type: str,
        subject_id: UUID,
    ) -> set[UUID]:
        """Resolve current app users related to a supported subject."""

        candidate_ids: set[UUID] = set()
        candidate_emails: set[str] = set()
        if subject_type == "deal":
            columns = cast(Any, CRMDeal).__table__.c
            deal = (
                (
                    await self.session.execute(
                        select(CRMDeal).where(
                            columns.organisation_id == self.organisation_id,
                            columns.deal_id == subject_id,
                            columns.is_deleted.is_(False),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if deal is None:
                return set()
            candidate_ids.update(
                value for value in (deal.owner_id, deal.created_by_user_id) if value
            )
            if deal.owner_email:
                candidate_emails.add(deal.owner_email.strip().casefold())
            participant_columns = cast(Any, CRMDealParticipant).__table__.c
            participants = (
                await self.session.execute(
                    select(
                        participant_columns.related_local_id,
                        participant_columns.related_email,
                    ).where(
                        participant_columns.organisation_id == self.organisation_id,
                        participant_columns.deal_id == subject_id,
                        participant_columns.participant_type == CRMParticipantType.USER.value,
                        participant_columns.is_deleted.is_(False),
                    )
                )
            ).all()
            candidate_ids.update(
                row.related_local_id for row in participants if row.related_local_id
            )
            candidate_emails.update(
                row.related_email.strip().casefold()
                for row in participants
                if row.related_email and row.related_email.strip()
            )
        elif subject_type in {"contact", "account"}:
            model = CRMContact if subject_type == "contact" else CRMCompany
            columns = cast(Any, model).__table__.c
            identity = columns.contact_id if subject_type == "contact" else columns.company_id
            subject_record: Any = (
                (
                    await self.session.execute(
                        select(model).where(
                            columns.organisation_id == self.organisation_id,
                            identity == subject_id,
                            columns.is_deleted.is_(False),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if subject_record is None:
                return set()
            if subject_record.owner_id:
                candidate_ids.add(subject_record.owner_id)
            if subject_record.owner_email:
                candidate_emails.add(subject_record.owner_email.strip().casefold())
        elif subject_type == "email":
            email_columns = cast(Any, Email).__table__.c
            email = (
                (
                    await self.session.execute(
                        select(Email).where(
                            email_columns.organisation_id == self.organisation_id,
                            email_columns.email_id == subject_id,
                            email_columns.is_deleted.is_(False),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if email is None:
                return set()
            candidate_emails.update(
                value.strip().casefold()
                for value in (
                    email.from_email,
                    *email.to_emails,
                    *email.cc_emails,
                    *email.bcc_emails,
                )
                if value and value.strip()
            )
            integration_columns = cast(Any, KnowledgeIntegration).__table__.c
            integration = (
                (
                    await self.session.execute(
                        select(KnowledgeIntegration).where(
                            integration_columns.organisation_id == self.organisation_id,
                            integration_columns.integration_id == email.integration_id,
                            integration_columns.is_active.is_(True),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if integration:
                candidate_ids.add(integration.created_by_user_id)
        elif subject_type == "meeting":
            meeting_columns = cast(Any, Meeting).__table__.c
            meeting = (
                (
                    await self.session.execute(
                        select(Meeting).where(
                            meeting_columns.organisation_id == self.organisation_id,
                            meeting_columns.meeting_id == subject_id,
                            meeting_columns.is_deleted.is_(False),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if meeting is None:
                return set()
            context = await load_meeting_event_context(
                self.session,
                self.organisation_id,
                meeting,
            )
            return set(context.attendee_ids) | ({context.owner_id} if context.owner_id else set())
        else:
            return set()

        if not candidate_ids and not candidate_emails:
            return set()
        user_columns = cast(Any, User).__table__.c
        identities = []
        if candidate_ids:
            identities.append(user_columns.user_id.in_(candidate_ids))
        if candidate_emails:
            identities.append(func.lower(func.trim(user_columns.email)).in_(candidate_emails))
        statement = select(user_columns.user_id).where(
            user_columns.organisation_id == self.organisation_id,
            sa_cast(user_columns.status, String) == SupabaseUserStatus.ACTIVE.value,
            sa_cast(user_columns.admin_status, String) == AdminUserStatus.APPROVED.value,
            or_(*identities),
        )
        return set((await self.session.execute(statement)).scalars().all())

    async def pause_invalid_trigger(
        self,
        trigger_id: UUID,
        *,
        error_code: str,
        message: str,
    ) -> bool:
        """Pause an active binding whose persisted contract no longer validates."""

        columns = cast(Any, AutomationTrigger).__table__.c
        statement = (
            update(AutomationTrigger)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.trigger_id == trigger_id,
                columns.status == AutomationTriggerStatus.ACTIVE.value,
            )
            .values(
                status=AutomationTriggerStatus.PAUSED.value,
                last_error_code=error_code,
                last_error=message,
                updated_at=func.now(),
            )
        )
        result = await self.session.execute(statement)
        return bool(getattr(result, "rowcount", 0))

    async def list_causation_trigger_ids(self, caused_by_run_id: UUID | None) -> set[UUID]:
        """Return trigger IDs in the bounded parent-run chain for loop prevention."""

        if caused_by_run_id is None:
            return set()
        table = cast(Any, AgentRun).__table__
        columns = table.c
        lineage = (
            select(
                columns.run_id,
                columns.trigger_id,
                columns.caused_by_run_id,
                literal(0).label("lineage_depth"),
            )
            .where(
                columns.organisation_id == self.organisation_id,
                columns.run_id == caused_by_run_id,
            )
            .cte("run_lineage", recursive=True)
        )
        parent = table.alias("parent_run")
        lineage = lineage.union_all(
            select(
                parent.c.run_id,
                parent.c.trigger_id,
                parent.c.caused_by_run_id,
                (lineage.c.lineage_depth + 1).label("lineage_depth"),
            ).where(
                parent.c.organisation_id == self.organisation_id,
                parent.c.run_id == lineage.c.caused_by_run_id,
                lineage.c.lineage_depth < 3,
            )
        )
        statement = select(lineage.c.trigger_id)
        return set((await self.session.execute(statement)).scalars().all())

    async def insert_run_if_absent(self, run: AgentRun) -> bool:
        """Insert one trigger/event decision or report an idempotent replay."""

        table = cast(Any, AgentRun).__table__
        columns = table.c
        values = {column.name: getattr(run, column.name) for column in table.columns}
        statement = (
            insert(AgentRun)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=[
                    columns.organisation_id,
                    columns.trigger_id,
                    columns.event_id,
                ]
            )
            .returning(columns.run_id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def get_run(self, *, trigger_id: UUID, event_id: UUID) -> AgentRun | None:
        """Resolve the run created by an earlier delivery of this decision."""

        columns = cast(Any, AgentRun).__table__.c
        statement = select(AgentRun).where(
            columns.organisation_id == self.organisation_id,
            columns.trigger_id == trigger_id,
            columns.event_id == event_id,
        )
        return (await self.session.execute(statement)).scalars().first()
