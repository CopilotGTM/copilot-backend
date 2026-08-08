"""Organization-scoped CRM options used by trigger catalog and validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables import AdminUserStatus, SupabaseUserStatus, User
from src.domain.models.organisation_tables import (
    AgentStatus,
    AutomationTrigger,
    AutomationTriggerStatus,
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMDealStage,
    CRMPipeline,
    CRMUser,
    UserAgent,
)
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting, MeetingSource
from src.service.automation.triggers.crm_schema import active_custom_field_options
from src.service.automation.triggers.direct import (
    ManualInvocationAccess,
    ManualSubject,
    crm_user_ids_for_principal,
)

if TYPE_CHECKING:
    from src.api.schemas.automation_trigger_models import TriggerCatalogPipelineOption


_EMAIL_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE,
    IntegrationType.MICROSOFT,
    IntegrationType.GMAIL,
    IntegrationType.OUTLOOK_EMAIL,
)


_STATIC_OPTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "deal_statuses": tuple((value, value.title()) for value in ("open", "won", "lost", "closed")),
    "meeting_providers": tuple(
        (item.value, item.value.replace("_", " ").title()) for item in MeetingSource
    ),
    "risk_statuses": (
        ("OPEN", "Open"),
        ("IN_PROGRESS", "In progress"),
        ("RESOLVED", "Resolved"),
        ("DISMISSED", "Dismissed"),
        ("WONT_FIX", "Won't fix"),
    ),
    "risk_resolution_statuses": (
        ("RESOLVED", "Resolved"),
        ("DISMISSED", "Dismissed"),
        ("WONT_FIX", "Won't fix"),
    ),
    "risk_severities": tuple(
        (value, value.title()) for value in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    ),
    "agent_run_failure_statuses": (
        ("failed", "Execution failed"),
        ("validation_failed", "Output validation failed"),
    ),
}


@dataclass(frozen=True)
class TriggerOptionRecord:
    """Tenant-scoped option data before saved-binding disclosure rules are applied."""

    value: str
    label: str
    status: str
    selectable: bool
    # The UI can explain an unavailable or inactive value without exposing a
    # second identifier or having to infer the reason from status alone.
    reason: str | None = None


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
        # Pipeline and stage IDs are client-supplied option values; organization
        # predicates are the trust boundary for both queries.
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
        # Validate IDs against current tenant rows before persisting them into a
        # trigger snapshot; existence in another organization is not sufficient.
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
        static = self._static_options(source)
        if static is not None:
            if values - {value for value, _label in static}:
                raise ValueError(f"Unknown {source} option")
            return
        if source == "meeting_languages":
            await self._validate_meeting_languages(values)
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
        if source == "organisation_users":
            columns = cast(Any, User).__table__.c
            statement = select(columns.user_id).where(
                columns.organisation_id == self.organisation_id,
                columns.user_id.in_(identifiers),
                columns.status == SupabaseUserStatus.ACTIVE.value,
                columns.admin_status == AdminUserStatus.APPROVED.value,
            )
        elif source == "automation_triggers":
            columns = cast(Any, AutomationTrigger).__table__.c
            statement = select(columns.trigger_id).where(
                columns.organisation_id == self.organisation_id,
                columns.trigger_id.in_(identifiers),
                columns.status != AutomationTriggerStatus.ARCHIVED.value,
            )
        elif source in {"deals", "contacts", "accounts", "agents"}:
            model, identity = {
                "deals": (CRMDeal, cast(Any, CRMDeal).__table__.c.deal_id),
                "contacts": (CRMContact, cast(Any, CRMContact).__table__.c.contact_id),
                "accounts": (CRMCompany, cast(Any, CRMCompany).__table__.c.company_id),
                "agents": (UserAgent, cast(Any, UserAgent).__table__.c.agent_id),
            }[source]
            columns = cast(Any, model).__table__.c
            active = (
                columns.status == AgentStatus.ACTIVE.value
                if source == "agents"
                else columns.is_deleted.is_(False)
            )
            statement = select(identity).where(
                columns.organisation_id == self.organisation_id,
                identity.in_(identifiers),
                active,
            )
        else:
            raise ValueError(f"Unsupported trigger option source: {source}")
        found = set((await self.session.execute(statement)).scalars().all())
        if found != identifiers:
            raise ValueError(f"Unknown {source} option")
        return

    async def validate_source_selection(
        self,
        source: str,
        values: set[str],
        *,
        principal_id: UUID,
        organisation_scope: bool,
    ) -> None:
        """Require every selected source to exist inside its execution authority."""

        try:
            identifiers = {UUID(value) for value in values}
        except ValueError as exc:
            raise ValueError(f"{source} values must be UUIDs") from exc
        if not identifiers:
            return

        if source == "ai_deals":
            if not organisation_scope:
                raise ValueError("AI deal source selection is organisation only")
            source = "deals"

        if source in {"deals", "contacts", "accounts"}:
            model, identity_column = {
                "deals": (CRMDeal, cast(Any, CRMDeal).__table__.c.deal_id),
                "contacts": (CRMContact, cast(Any, CRMContact).__table__.c.contact_id),
                "accounts": (CRMCompany, cast(Any, CRMCompany).__table__.c.company_id),
            }[source]
            columns = cast(Any, model).__table__.c
            conditions = [
                columns.organisation_id == self.organisation_id,
                columns.is_deleted.is_(False),
                identity_column.in_(identifiers),
            ]
            if not organisation_scope:
                # Personal bindings may select only records owned by the resolved
                # principal (or created by it for deals), not arbitrary tenant rows.
                crm_user_ids = crm_user_ids_for_principal(
                    self.organisation_id,
                    principal_id,
                )
                ownership = columns.owner_id.in_(crm_user_ids)
                if source == "deals":
                    ownership = or_(
                        ownership,
                        columns.created_by_user_id.in_(crm_user_ids),
                    )
                conditions.append(ownership)
            statement = select(identity_column).where(*conditions)
        elif source == "email_mailboxes":
            columns = cast(Any, KnowledgeIntegration).__table__.c
            conditions = [
                columns.organisation_id == self.organisation_id,
                columns.integration_id.in_(identifiers),
                columns.integration_type.in_(_EMAIL_INTEGRATION_TYPES),
                columns.is_active.is_(True),
            ]
            if not organisation_scope:
                # A personal mailbox is valid only when this user created the
                # integration; tenant ownership alone would leak another user's mailbox.
                conditions.append(columns.created_by_user_id == principal_id)
            statement = select(columns.integration_id).where(*conditions)
        elif source == "agents":
            columns = cast(Any, UserAgent).__table__.c
            conditions = [
                columns.organisation_id == self.organisation_id,
                columns.agent_id.in_(identifiers),
                columns.status == AgentStatus.ACTIVE.value,
            ]
            if not organisation_scope:
                conditions.append(columns.owner_user_id == principal_id)
            statement = select(columns.agent_id).where(*conditions)
        else:
            raise ValueError(f"Unsupported trigger source selection: {source}")

        found = set((await self.session.execute(statement)).scalars().all())
        if found != identifiers:
            raise ValueError("Unknown or unavailable trigger source")

    async def search_options(
        self,
        source: str,
        *,
        query: str,
        limit: int,
        offset: int,
        principal_id: UUID,
        organisation_scope: bool,
        authority_scoped: bool,
    ) -> tuple[list[TriggerOptionRecord], int | None]:
        """Return one active, selectable option page without exposing internal IDs as labels."""

        # Search and resolve share the same authority flags so a friendly label
        # cannot be discovered through a broader query than the saved binding allows.
        static = self._static_options(source)
        if static is not None:
            rows = [
                TriggerOptionRecord(value, label, "active", True)
                for value, label in static
                if not query or query.lower() in label.lower()
            ]
            page = rows[offset : offset + limit + 1]
        elif source == "meeting_languages":
            page = await self._search_meeting_languages(query, limit, offset)
        else:
            statement, formatter = self._option_statement(
                source,
                values=None,
                query=query,
                active_only=True,
                principal_id=principal_id,
                organisation_scope=organisation_scope,
                authority_scoped=authority_scoped,
            )
            rows = list(
                (await self.session.execute(statement.limit(limit + 1).offset(offset)))
                .scalars()
                .all()
            )
            page = [formatter(row, True) for row in rows]
        has_more = len(page) > limit
        return page[:limit], offset + limit if has_more else None

    async def resolve_options(
        self,
        source: str,
        values: set[str],
        *,
        principal_id: UUID,
        organisation_scope: bool,
        authority_scoped: bool,
    ) -> dict[str, TriggerOptionRecord]:
        """Resolve tenant rows in one query; callers apply saved-binding disclosure gates."""

        # Batch the selected IDs into one tenant query; presentation decides later
        # whether a non-selectable saved value may still be disclosed.
        static = self._static_options(source)
        if static is not None:
            labels = dict(static)
            return {
                value: TriggerOptionRecord(value, labels[value], "active", True)
                for value in values & labels.keys()
            }
        if source == "meeting_languages":
            found = await self._meeting_languages(values)
            return {value: TriggerOptionRecord(value, value, "active", True) for value in found}
        try:
            identifiers = {UUID(value) for value in values}
        except ValueError as exc:
            raise ValueError(f"{source} values must be UUIDs") from exc
        statement, formatter = self._option_statement(
            source,
            values=identifiers,
            query="",
            active_only=False,
            principal_id=principal_id,
            organisation_scope=organisation_scope,
            authority_scoped=authority_scoped,
        )
        rows = list((await self.session.execute(statement)).scalars().all())
        resolved = []
        for row in rows:
            active = self._row_is_selectable(source, row)
            resolved.append(formatter(row, active))
        return {item.value: item for item in resolved}

    def _option_statement(
        self,
        source: str,
        *,
        values: set[UUID] | None,
        query: str,
        active_only: bool,
        principal_id: UUID,
        organisation_scope: bool,
        authority_scoped: bool,
    ) -> tuple[Any, Any]:
        """Build the one tenant query for a registry-owned option source."""

        # Every dynamic source starts with the organization predicate; authority
        # narrowing is added only after that immutable tenant boundary is present.
        if source in {"deals", "contacts", "accounts", "ai_deals"}:
            source = "deals" if source == "ai_deals" else source
            model, identity_name, text_names = {
                "deals": (CRMDeal, "deal_id", ("name", "company_name", "company_domain")),
                "contacts": (
                    CRMContact,
                    "contact_id",
                    ("full_name", "first_name", "last_name", "email", "company_name"),
                ),
                "accounts": (CRMCompany, "company_id", ("name", "domain")),
            }[source]
            columns = cast(Any, model).__table__.c
            identity = getattr(columns, identity_name)
            conditions = [columns.organisation_id == self.organisation_id]
            if active_only:
                conditions.append(columns.is_deleted.is_(False))
            if authority_scoped and not organisation_scope:
                crm_user_ids = crm_user_ids_for_principal(
                    self.organisation_id,
                    principal_id,
                )
                ownership = columns.owner_id.in_(crm_user_ids)
                if source == "deals":
                    ownership = or_(
                        ownership,
                        columns.created_by_user_id.in_(crm_user_ids),
                    )
                conditions.append(ownership)
            formatter = lambda row, selectable: self._format_crm_record(  # noqa: E731
                source, row, selectable
            )
        elif source == "email_mailboxes":
            model = KnowledgeIntegration
            columns = cast(Any, model).__table__.c
            identity = columns.integration_id
            text_names = ("integration_email", "name")
            conditions = [
                columns.organisation_id == self.organisation_id,
                columns.integration_type.in_(_EMAIL_INTEGRATION_TYPES),
            ]
            if active_only:
                conditions.append(columns.is_active.is_(True))
            if authority_scoped and not organisation_scope:
                conditions.append(columns.created_by_user_id == principal_id)
            formatter = self._format_mailbox
        elif source == "agents":
            model = UserAgent
            columns = cast(Any, model).__table__.c
            identity = columns.agent_id
            text_names = ("name", "description")
            conditions = [columns.organisation_id == self.organisation_id]
            if active_only:
                conditions.append(columns.status == AgentStatus.ACTIVE.value)
            if authority_scoped and not organisation_scope:
                conditions.append(columns.owner_user_id == principal_id)
            formatter = self._format_agent
        elif source == "crm_users":
            model = CRMUser
            columns = cast(Any, model).__table__.c
            identity = columns.user_id
            text_names = ("full_name", "first_name", "last_name", "email")
            conditions = [columns.organisation_id == self.organisation_id]
            if active_only:
                conditions.extend([columns.is_deleted.is_(False), columns.is_active.is_(True)])
            formatter = self._format_crm_user
        elif source == "organisation_users":
            model = User
            columns = cast(Any, model).__table__.c
            identity = columns.user_id
            text_names = ("first_name", "last_name", "email")
            conditions = [columns.organisation_id == self.organisation_id]
            if active_only:
                conditions.extend(
                    [
                        columns.status == SupabaseUserStatus.ACTIVE.value,
                        columns.admin_status == AdminUserStatus.APPROVED.value,
                    ]
                )
            formatter = self._format_organisation_user
        elif source == "automation_triggers":
            model = AutomationTrigger
            columns = cast(Any, model).__table__.c
            identity = columns.trigger_id
            text_names = ("name", "trigger_key")
            conditions = [columns.organisation_id == self.organisation_id]
            if active_only:
                conditions.append(columns.status != AutomationTriggerStatus.ARCHIVED.value)
            if not organisation_scope:
                conditions.append(columns.created_by_user_id == principal_id)
            formatter = self._format_automation_trigger
        elif source in {"pipelines", "pipeline_stages"}:
            model = CRMPipeline if source == "pipelines" else CRMDealStage
            columns = cast(Any, model).__table__.c
            identity = columns.pipeline_id if source == "pipelines" else columns.stage_id
            text_names = ("label", "name")
            conditions = [columns.organisation_id == self.organisation_id]
            if active_only:
                conditions.extend([columns.is_deleted.is_(False), columns.is_active.is_(True)])
            formatter = lambda row, selectable: self._format_crm_metadata(  # noqa: E731
                source, row, selectable
            )
        else:
            raise ValueError(f"Unsupported trigger option source: {source}")

        if values is not None:
            conditions.append(identity.in_(values))
        if query:
            pattern = f"%{query.strip().lower()}%"
            conditions.append(
                or_(
                    *(
                        func.lower(func.coalesce(getattr(columns, name), "")).like(pattern)
                        for name in text_names
                    )
                )
            )
        statement = (
            select(model)
            .where(*conditions)
            .order_by(*self._option_order(columns, text_names, identity))
        )
        return statement, formatter

    @staticmethod
    def _option_order(columns: Any, text_names: tuple[str, ...], identity: Any) -> tuple[Any, ...]:
        return (func.lower(func.coalesce(getattr(columns, text_names[0]), "")), identity)

    @staticmethod
    def _row_is_selectable(source: str, row: Any) -> bool:
        if source in {"deals", "contacts", "accounts"}:
            return not row.is_deleted
        if source == "email_mailboxes":
            return bool(row.is_active)
        if source == "agents":
            return getattr(row.status, "value", row.status) == AgentStatus.ACTIVE.value
        if source == "organisation_users":
            return (
                getattr(row.status, "value", row.status) == SupabaseUserStatus.ACTIVE.value
                and getattr(row.admin_status, "value", row.admin_status)
                == AdminUserStatus.APPROVED.value
            )
        if source == "automation_triggers":
            return (
                getattr(row.status, "value", row.status) != AutomationTriggerStatus.ARCHIVED.value
            )
        return not row.is_deleted and bool(row.is_active)

    @classmethod
    def _format_crm_record(cls, source: str, row: Any, selectable: bool) -> TriggerOptionRecord:
        if source == "deals":
            value = row.deal_id
            primary = row.name or "Unnamed deal"
            secondary = row.company_name or row.company_domain
        elif source == "contacts":
            value = row.contact_id
            primary = (
                row.full_name
                or " ".join(item for item in (row.first_name, row.last_name) if item)
                or row.email
                or "Unnamed contact"
            )
            secondary = row.email if row.email != primary else row.company_name
        else:
            value = row.company_id
            primary = row.name or row.domain or "Unnamed account"
            secondary = row.domain if row.domain != primary else None
        return TriggerOptionRecord(
            str(value),
            cls._label(primary, secondary),
            "active" if selectable else "deleted",
            selectable,
        )

    @classmethod
    def _format_mailbox(cls, row: Any, selectable: bool) -> TriggerOptionRecord:
        provider = getattr(row.integration_type, "value", row.integration_type)
        return TriggerOptionRecord(
            str(row.integration_id),
            cls._label(row.integration_email or row.name, str(provider).replace("_", " ").title()),
            "active" if selectable else "inactive",
            selectable,
        )

    @staticmethod
    def _format_agent(row: Any, selectable: bool) -> TriggerOptionRecord:
        status = getattr(row.status, "value", row.status)
        return TriggerOptionRecord(
            str(row.agent_id),
            row.name,
            "deleted"
            if status == AgentStatus.ARCHIVED.value
            else "active"
            if selectable
            else "inactive",
            selectable,
        )

    @classmethod
    def _format_crm_user(cls, row: Any, selectable: bool) -> TriggerOptionRecord:
        primary = (
            row.full_name
            or " ".join(item for item in (row.first_name, row.last_name) if item)
            or row.email
            or "Unnamed CRM user"
        )
        status = "deleted" if row.is_deleted else "active" if selectable else "inactive"
        return TriggerOptionRecord(
            str(row.user_id),
            cls._label(primary, row.email if row.email != primary else None),
            status,
            selectable,
        )

    @staticmethod
    def _format_crm_metadata(source: str, row: Any, selectable: bool) -> TriggerOptionRecord:
        value = row.pipeline_id if source == "pipelines" else row.stage_id
        status = "deleted" if row.is_deleted else "active" if selectable else "inactive"
        return TriggerOptionRecord(str(value), row.label or row.name, status, selectable)

    @classmethod
    def _format_organisation_user(cls, row: Any, selectable: bool) -> TriggerOptionRecord:
        name = " ".join(value for value in (row.first_name, row.last_name) if value).strip()
        return TriggerOptionRecord(
            str(row.user_id),
            cls._label(name or row.email, row.email if row.email != name else None),
            "active" if selectable else "inactive",
            selectable,
        )

    @staticmethod
    def _format_automation_trigger(row: Any, selectable: bool) -> TriggerOptionRecord:
        from src.service.automation.triggers.registry import AUTOMATION_REGISTRY

        definition = AUTOMATION_REGISTRY.trigger(row.trigger_key)
        breadcrumb = (
            f"{definition.category_label} / {definition.resource_label} / {definition.event_label}"
        )
        return TriggerOptionRecord(
            str(row.trigger_id),
            f"{breadcrumb} · {row.name}",
            "active" if selectable else "deleted",
            selectable,
        )

    @staticmethod
    def _static_options(source: str) -> tuple[tuple[str, str], ...] | None:
        if source == "trigger_event_keys":
            from src.service.automation.triggers.registry import AUTOMATION_REGISTRY

            labels: dict[str, str] = {}
            for definition in AUTOMATION_REGISTRY.catalog_triggers():
                for event_key in definition.match_event_keys:
                    labels.setdefault(
                        event_key,
                        f"{definition.category_label} / {definition.resource_label} / "
                        f"{definition.event_label}",
                    )
            return tuple(labels.items())
        return _STATIC_OPTIONS.get(source)

    async def _meeting_languages(self, values: set[str]) -> set[str]:
        columns = cast(Any, Meeting).__table__.c
        return set(
            (
                await self.session.execute(
                    select(columns.transcript_language).where(
                        columns.organisation_id == self.organisation_id,
                        columns.transcript_language.in_(values),
                    )
                )
            ).scalars()
        )

    async def _validate_meeting_languages(self, values: set[str]) -> None:
        if await self._meeting_languages(values) != values:
            raise ValueError("Unknown meeting language option")

    async def _search_meeting_languages(
        self,
        query: str,
        limit: int,
        offset: int,
    ) -> list[TriggerOptionRecord]:
        columns = cast(Any, Meeting).__table__.c
        conditions = [
            columns.organisation_id == self.organisation_id,
            columns.transcript_language.is_not(None),
            columns.transcript_language != "",
        ]
        if query:
            conditions.append(func.lower(columns.transcript_language).contains(query.lower()))
        statement = (
            select(columns.transcript_language)
            .where(*conditions)
            .distinct()
            .order_by(columns.transcript_language)
            .limit(limit + 1)
            .offset(offset)
        )
        values = list((await self.session.execute(statement)).scalars().all())
        return [TriggerOptionRecord(value, value, "active", True) for value in values]

    @staticmethod
    def _label(primary: str, secondary: str | None) -> str:
        return f"{primary} · {secondary}" if secondary else primary

    async def _validate_custom_field_options(self, source: str, values: set[str]) -> None:
        # The field definition and its options are tenant-owned; the UUID prefix
        # from the request is useful for lookup but is not an authorization proof.
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
