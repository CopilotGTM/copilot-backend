"""Validated direct fanout for authenticated manual trigger invocations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.master_tables import AdminUserStatus, SupabaseUserStatus, User
from src.domain.models.organisation_tables import (
    AutomationEventDispatch,
    AutomationPrincipalType,
    AutomationTrigger,
    AutomationTriggerFamily,
    AutomationTriggerStatus,
)
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.service.automation.errors import AutomationServiceError
from src.service.automation.events.access import AutomationEventDispatchAccess
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.events.service import AutomationEventDispatchService
from src.service.crm.mutations.json_safe import normalize_json_value
from src.service.crm.mutations.policies import (
    AccountMutationPolicy,
    ContactMutationPolicy,
    DealMutationPolicy,
)

MANUAL_SUBJECT_LIMIT = 100
_MANUAL_EVENT_KEY = "system.manual.invoked"
_SubjectType = Literal["deal", "contact", "account"]


def crm_user_ids_for_principal(organisation_id: UUID, principal_id: UUID) -> Any:
    """Map one workspace principal to active tenant CRM owner identities."""

    user_columns = cast(Any, User).__table__.c
    crm_user_columns = cast(Any, CRMUser).__table__.c
    principal_email = select(func.lower(func.trim(user_columns.email))).where(
        user_columns.organisation_id == organisation_id,
        user_columns.user_id == principal_id,
        user_columns.status == SupabaseUserStatus.ACTIVE.value,
        user_columns.admin_status == AdminUserStatus.APPROVED.value,
    )
    return select(crm_user_columns.user_id).where(
        crm_user_columns.organisation_id == organisation_id,
        crm_user_columns.is_deleted.is_(False),
        or_(
            crm_user_columns.user_id == principal_id,
            func.lower(func.trim(crm_user_columns.email)).in_(principal_email),
        ),
    )


@dataclass(frozen=True)
class ManualSubject:
    """A supported CRM identity supplied through the authenticated route."""

    subject_type: _SubjectType
    subject_id: UUID

    @property
    def identity(self) -> tuple[str, UUID]:
        """Return the stable Manual Subject identity used for routing and idempotency."""
        return self.subject_type, self.subject_id


@dataclass(frozen=True)
class ManualInvocationItem:
    """Canonical envelope and its durable dispatch row."""

    envelope: AutomationEventEnvelope
    dispatch: AutomationEventDispatch


class ManualInvocationAccessProtocol(Protocol):
    """Persistence reads needed before direct event ingestion."""

    async def load_subject_records(
        self,
        subjects: tuple[ManualSubject, ...],
        *,
        principal_id: UUID,
        organisation_scope: bool,
    ) -> dict[tuple[str, UUID], dict[str, Any]]:
        """Load subject records for the current organisation-scoped operation."""
        ...

    async def get_dispatch(self, idempotency_key: str) -> AutomationEventDispatch | None:
        """Load dispatch within the organisation boundary."""
        ...


class EventEnqueuer(Protocol):
    """Transaction-local canonical event ingestion."""

    async def enqueue(self, envelope: AutomationEventEnvelope) -> AutomationEventDispatch:
        """Queue the current Event Enqueuer work while preserving its tenant and idempotency boundaries."""
        ...


class ManualInvocationAccess:
    """Load only safe, active CRM snapshots within the selected authority."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        event_access: AutomationEventDispatchAccess | None = None,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.event_access = event_access or AutomationEventDispatchAccess(
            session,
            organisation_id,
        )

    async def get_dispatch(self, idempotency_key: str) -> AutomationEventDispatch | None:
        """Resolve a prior invocation without rebuilding its time-dependent envelope."""

        return await self.event_access.get_by_idempotency_key(idempotency_key)

    async def load_subject_records(
        self,
        subjects: tuple[ManualSubject, ...],
        *,
        principal_id: UUID,
        organisation_scope: bool,
    ) -> dict[tuple[str, UUID], dict[str, Any]]:
        """Resolve the selection in at most one query per supported CRM resource."""

        records: dict[tuple[str, UUID], dict[str, Any]] = {}
        grouped = {
            subject_type: {
                item.subject_id for item in subjects if item.subject_type == subject_type
            }
            for subject_type in ("deal", "contact", "account")
        }
        for subject_type, ids in grouped.items():
            if not ids:
                continue
            model, identity_column, serializer = self._resource(subject_type)
            columns = cast(Any, model).__table__.c
            conditions = [
                columns.organisation_id == self.organisation_id,
                identity_column.in_(ids),
                columns.is_deleted.is_(False),
            ]
            if not organisation_scope:
                crm_user_ids = crm_user_ids_for_principal(
                    self.organisation_id,
                    principal_id,
                )
                ownership = columns.owner_id.in_(crm_user_ids)
                if subject_type == "deal":
                    ownership = or_(
                        ownership,
                        columns.created_by_user_id.in_(crm_user_ids),
                    )
                conditions.append(ownership)
            rows = (await self.session.execute(select(model).where(*conditions))).scalars().all()
            for row in rows:
                subject_id = cast(UUID, getattr(row, identity_column.name))
                record = normalize_json_value(serializer(row), field_name=subject_type)
                records[(subject_type, subject_id)] = cast(dict[str, Any], record)
        return records

    @staticmethod
    def _resource(subject_type: str) -> tuple[Any, Any, Callable[[Any], dict[str, Any]]]:
        if subject_type == "deal":
            return (
                CRMDeal,
                cast(Any, CRMDeal).__table__.c.deal_id,
                DealMutationPolicy.automation_record,
            )
        if subject_type == "contact":
            return (
                CRMContact,
                cast(Any, CRMContact).__table__.c.contact_id,
                ContactMutationPolicy.automation_record,
            )
        return (
            CRMCompany,
            cast(Any, CRMCompany).__table__.c.company_id,
            AccountMutationPolicy.automation_record,
        )


class ManualInvocationService:
    """Build one replay-stable direct event per selected subject."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        authenticated_user_id: UUID,
        *,
        is_admin: bool = False,
        access: ManualInvocationAccessProtocol | None = None,
        event_enqueuer: EventEnqueuer | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.organisation_id = organisation_id
        self.authenticated_user_id = authenticated_user_id
        self.is_admin = is_admin
        self.access = access or ManualInvocationAccess(session, organisation_id)
        self.event_enqueuer = event_enqueuer or AutomationEventDispatchService(
            session,
            organisation_id,
        )
        self.clock = clock

    async def invoke(
        self,
        trigger: AutomationTrigger,
        *,
        invocation_id: str,
        subjects: tuple[ManualSubject, ...],
    ) -> list[ManualInvocationItem]:
        """Validate authority, resolve new subjects, and enqueue the full fanout."""

        organisation_scope = self._validate_trigger(trigger)
        invocation_id = invocation_id.strip()
        if not invocation_id or len(invocation_id) > 200:
            raise AutomationServiceError("invocation_id must contain at most 200 characters")
        if len(subjects) > MANUAL_SUBJECT_LIMIT:
            raise AutomationServiceError(
                f"manual invocation accepts at most {MANUAL_SUBJECT_LIMIT} subjects"
            )
        identities = [subject.identity for subject in subjects]
        if len(identities) != len(set(identities)):
            raise AutomationServiceError("manual invocation subjects must be unique")

        selected: tuple[ManualSubject | None, ...] = subjects or (None,)
        correlation_id = f"manual:{trigger.trigger_id}:{invocation_id}"
        existing: dict[str, AutomationEventDispatch] = {}
        missing_subjects: list[ManualSubject] = []
        for subject in selected:
            key = self._idempotency_key(trigger.trigger_id, invocation_id, subject)
            dispatch = await self.access.get_dispatch(key)
            if dispatch is not None:
                existing[key] = dispatch
            elif subject is not None:
                missing_subjects.append(subject)

        records = (
            await self.access.load_subject_records(
                tuple(missing_subjects),
                principal_id=self.authenticated_user_id,
                organisation_scope=organisation_scope,
            )
            if missing_subjects
            else {}
        )
        unavailable = [subject for subject in missing_subjects if subject.identity not in records]
        if unavailable:
            raise AutomationServiceError(
                "One or more manual invocation subjects are unavailable",
                status_code=404,
            )

        occurred_at = self.clock()
        items: list[ManualInvocationItem] = []
        for subject in selected:
            key = self._idempotency_key(trigger.trigger_id, invocation_id, subject)
            prior = existing.get(key)
            envelope = (
                self._replayed_envelope(prior, trigger)
                if prior is not None
                else self._build_envelope(
                    trigger,
                    invocation_id=invocation_id,
                    subject=subject,
                    record=records[subject.identity] if subject is not None else {},
                    correlation_id=correlation_id,
                    idempotency_key=key,
                    occurred_at=occurred_at,
                )
            )
            dispatch = await self.event_enqueuer.enqueue(envelope)
            items.append(ManualInvocationItem(envelope=envelope, dispatch=dispatch))
        return items

    def _validate_trigger(self, trigger: AutomationTrigger) -> bool:
        if trigger.organisation_id != self.organisation_id:
            raise AutomationServiceError("Manual trigger organisation does not match session")
        if self._value(trigger.family) != AutomationTriggerFamily.MANUAL.value:
            raise AutomationServiceError("Only manual triggers can be invoked from this route")
        if trigger.trigger_key != _MANUAL_EVENT_KEY or trigger.match_event_keys != [
            _MANUAL_EVENT_KEY
        ]:
            raise AutomationServiceError(
                "Manual trigger event contract is invalid", status_code=409
            )
        if self._value(trigger.status) != AutomationTriggerStatus.ACTIVE.value:
            raise AutomationServiceError("Manual trigger must be active", status_code=409)

        principal_type = self._value(trigger.execution_principal_type)
        if principal_type == AutomationPrincipalType.USER.value:
            if trigger.created_by_user_id != self.authenticated_user_id:
                raise AutomationServiceError(
                    "Manual trigger does not belong to the authenticated principal"
                )
            if trigger.execution_principal_id != self.authenticated_user_id:
                raise AutomationServiceError("Manual trigger principal does not match caller")
            return False
        if (
            principal_type == AutomationPrincipalType.ORGANISATION_SERVICE.value
            and trigger.execution_principal_id == self.organisation_id
        ):
            if not self.is_admin:
                raise AutomationServiceError(
                    "Organisation manual triggers require an organisation admin",
                    status_code=403,
                )
            return True
        raise AutomationServiceError("Manual trigger principal is invalid")

    @staticmethod
    def _idempotency_key(
        trigger_id: UUID,
        invocation_id: str,
        subject: ManualSubject | None,
    ) -> str:
        subject_key = (
            "global" if subject is None else f"{subject.subject_type}:{subject.subject_id}"
        )
        return f"manual:{trigger_id}:{invocation_id}:{subject_key}"

    def _build_envelope(
        self,
        trigger: AutomationTrigger,
        *,
        invocation_id: str,
        subject: ManualSubject | None,
        record: dict[str, Any],
        correlation_id: str,
        idempotency_key: str,
        occurred_at: datetime,
    ) -> AutomationEventEnvelope:
        return AutomationEventEnvelope.model_validate(
            {
                "event_id": uuid5(
                    NAMESPACE_URL, f"automation:{self.organisation_id}:{idempotency_key}"
                ),
                "organisation_id": self.organisation_id,
                "event_key": _MANUAL_EVENT_KEY,
                "route": {
                    "mode": "direct",
                    "trigger_id": trigger.trigger_id,
                    "config_hash": trigger.config_hash,
                },
                "occurred_at": occurred_at,
                "source": {
                    "domain": "system",
                    "system": "agent_studio",
                    "operation": "invoke",
                    "mode": "interactive",
                    "source_event_id": invocation_id,
                },
                "actor": {
                    "type": "user",
                    "id": str(self.authenticated_user_id),
                },
                "subject": {
                    "type": subject.subject_type if subject else None,
                    "id": str(subject.subject_id) if subject else None,
                },
                "record": record,
                "correlation_id": correlation_id,
                "idempotency_key": idempotency_key,
            }
        )

    def _replayed_envelope(
        self,
        dispatch: AutomationEventDispatch,
        trigger: AutomationTrigger,
    ) -> AutomationEventEnvelope:
        try:
            envelope = AutomationEventEnvelope.model_validate(dispatch.envelope)
        except ValueError as exc:
            raise AutomationServiceError(
                "Stored manual invocation envelope is invalid",
                status_code=409,
            ) from exc
        if (
            envelope.organisation_id != self.organisation_id
            or envelope.event_key != _MANUAL_EVENT_KEY
            or envelope.route.trigger_id != trigger.trigger_id
        ):
            raise AutomationServiceError(
                "Manual invocation identity conflicts with an existing event",
                status_code=409,
            )
        return envelope

    @staticmethod
    def _value(value: Any) -> Any:
        return getattr(value, "value", value)


__all__ = [
    "MANUAL_SUBJECT_LIMIT",
    "ManualInvocationAccess",
    "ManualInvocationItem",
    "ManualInvocationService",
    "ManualSubject",
]
