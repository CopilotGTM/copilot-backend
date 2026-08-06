"""Direct manual invocation and subject-isolated fanout contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import (
    AutomationEventDispatch,
    AutomationTrigger,
    AutomationTriggerStatus,
)
from src.service.automation.errors import AutomationServiceError
from src.service.automation.triggers.direct import (
    MANUAL_SUBJECT_LIMIT,
    ManualInvocationService,
    ManualSubject,
)


def trigger(organisation_id: UUID, principal_id: UUID) -> AutomationTrigger:
    """Build the trigger fixture used by the surrounding regression cases."""
    return AutomationTrigger(
        organisation_id=organisation_id,
        created_by_user_id=principal_id,
        name="Manual deal review",
        target_id=uuid4(),
        target_revision=2,
        target_snapshot={},
        trigger_key="system.manual.invoked",
        family="manual",
        match_event_keys=["system.manual.invoked"],
        execution_principal_type="user",
        execution_principal_id=principal_id,
        scope_type="owned_or_created_by_principal",
        audience_mode="principal",
        config_hash="a" * 64,
        status=AutomationTriggerStatus.ACTIVE,
    )


class FakeAccess:
    """Represent the Fake Access test double used by the surrounding regression cases."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, UUID], dict] = {}
        self.dispatches: dict[str, AutomationEventDispatch] = {}
        self.load_calls: list[tuple[tuple[ManualSubject, ...], UUID, bool]] = []

    async def load_subject_records(
        self,
        subjects: tuple[ManualSubject, ...],
        *,
        principal_id: UUID,
        organisation_scope: bool,
    ) -> dict[tuple[str, UUID], dict]:
        """Expose the load subject records operation expected by the Fake Access test double."""
        self.load_calls.append((subjects, principal_id, organisation_scope))
        return {
            subject.identity: self.records[subject.identity]
            for subject in subjects
            if subject.identity in self.records
        }

    async def get_dispatch(self, idempotency_key: str) -> AutomationEventDispatch | None:
        """Expose the get dispatch operation expected by the Fake Access test double."""
        return self.dispatches.get(idempotency_key)


class FakeEnqueuer:
    """Represent the Fake Enqueuer test double used by the surrounding regression cases."""

    def __init__(self, access: FakeAccess) -> None:
        self.access = access
        self.envelopes = []

    async def enqueue(self, envelope):  # noqa: ANN001
        """Expose the enqueue operation expected by the Fake Enqueuer test double."""
        self.envelopes.append(envelope)
        existing = self.access.dispatches.get(envelope.idempotency_key)
        if existing is not None:
            assert existing.envelope == envelope.model_dump(mode="json")
            return existing
        dispatch = AutomationEventDispatch(
            event_id=envelope.event_id,
            organisation_id=envelope.organisation_id,
            event_key=envelope.event_key,
            route_mode="direct",
            direct_trigger_id=envelope.route.trigger_id,
            idempotency_key=envelope.idempotency_key,
            envelope=envelope.model_dump(mode="json"),
        )
        self.access.dispatches[envelope.idempotency_key] = dispatch
        return dispatch


def service(organisation_id: UUID, principal_id: UUID):
    """Build the service fixture used by the surrounding regression cases."""
    access = FakeAccess()
    enqueuer = FakeEnqueuer(access)
    item = ManualInvocationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        principal_id,
        access=access,
        event_enqueuer=enqueuer,
        clock=lambda: datetime(2026, 8, 5, 11, tzinfo=UTC),
    )
    return item, access, enqueuer


@pytest.mark.asyncio
async def test_global_manual_invocation_is_replay_stable() -> None:
    """Verify the contract that global manual invocation is replay stable."""
    organisation_id, principal_id = uuid4(), uuid4()
    invocation, access, enqueuer = service(organisation_id, principal_id)
    binding = trigger(organisation_id, principal_id)

    first = await invocation.invoke(binding, invocation_id="ui-action-42", subjects=())
    replay = await invocation.invoke(binding, invocation_id="ui-action-42", subjects=())

    assert len(first) == len(replay) == 1
    assert first[0].envelope.event_id == replay[0].envelope.event_id
    assert first[0].envelope.occurred_at == replay[0].envelope.occurred_at
    assert first[0].envelope.subject.type is None
    assert first[0].envelope.record == {}
    assert first[0].envelope.route.trigger_id == binding.trigger_id
    assert first[0].envelope.route.config_hash == binding.config_hash
    assert first[0].envelope.correlation_id == (f"manual:{binding.trigger_id}:ui-action-42")
    assert access.load_calls == []
    assert len(enqueuer.envelopes) == 2


@pytest.mark.asyncio
async def test_multi_subject_manual_invocation_fans_out_with_safe_snapshots() -> None:
    """Verify the contract that multi subject manual invocation fans out with safe snapshots."""
    organisation_id, principal_id = uuid4(), uuid4()
    invocation, access, _enqueuer = service(organisation_id, principal_id)
    binding = trigger(organisation_id, principal_id)
    subjects = (
        ManualSubject("deal", uuid4()),
        ManualSubject("contact", uuid4()),
        ManualSubject("account", uuid4()),
    )
    access.records = {
        subjects[0].identity: {
            "id": str(subjects[0].subject_id),
            "owner_id": str(principal_id),
            "created_by_user_id": None,
        },
        subjects[1].identity: {
            "id": str(subjects[1].subject_id),
            "owner_id": str(principal_id),
        },
        subjects[2].identity: {
            "id": str(subjects[2].subject_id),
            "owner_id": str(principal_id),
        },
    }

    results = await invocation.invoke(
        binding,
        invocation_id="ui-action-43",
        subjects=subjects,
    )

    assert [item.envelope.subject.type for item in results] == [
        "deal",
        "contact",
        "account",
    ]
    assert [item.envelope.subject.id for item in results] == [
        str(subject.subject_id) for subject in subjects
    ]
    assert len({item.envelope.event_id for item in results}) == len(subjects)
    assert len({item.envelope.correlation_id for item in results}) == 1
    assert access.load_calls == [(subjects, principal_id, False)]


@pytest.mark.asyncio
async def test_manual_invocation_rejects_missing_duplicate_and_excess_subjects() -> None:
    """Verify the contract that manual invocation rejects missing duplicate and excess subjects."""
    organisation_id, principal_id = uuid4(), uuid4()
    invocation, access, _enqueuer = service(organisation_id, principal_id)
    binding = trigger(organisation_id, principal_id)
    subject = ManualSubject("deal", uuid4())

    with pytest.raises(AutomationServiceError, match="unavailable"):
        await invocation.invoke(binding, invocation_id="missing", subjects=(subject,))

    access.records[subject.identity] = {
        "id": str(subject.subject_id),
        "owner_id": str(principal_id),
    }
    with pytest.raises(AutomationServiceError, match="unique"):
        await invocation.invoke(
            binding,
            invocation_id="duplicate",
            subjects=(subject, subject),
        )

    too_many = tuple(ManualSubject("deal", uuid4()) for _ in range(MANUAL_SUBJECT_LIMIT + 1))
    with pytest.raises(AutomationServiceError, match="at most"):
        await invocation.invoke(binding, invocation_id="too-many", subjects=too_many)


@pytest.mark.asyncio
async def test_manual_invocation_rejects_wrong_binding_or_actor() -> None:
    """Verify the contract that manual invocation rejects wrong binding or actor."""
    organisation_id, principal_id = uuid4(), uuid4()
    invocation, _access, _enqueuer = service(organisation_id, principal_id)
    binding = trigger(organisation_id, principal_id)

    binding.family = "event"
    with pytest.raises(AutomationServiceError, match="manual"):
        await invocation.invoke(binding, invocation_id="wrong-family", subjects=())

    binding.family = "manual"
    binding.execution_principal_id = uuid4()
    with pytest.raises(AutomationServiceError, match="principal"):
        await invocation.invoke(binding, invocation_id="wrong-user", subjects=())


@pytest.mark.asyncio
async def test_organisation_admin_can_invoke_another_admins_shared_trigger() -> None:
    """Authorize shared manual bindings by current admin authority, not creator identity."""
    organisation_id, creator_id, admin_id = uuid4(), uuid4(), uuid4()
    binding = trigger(organisation_id, creator_id)
    binding.execution_principal_type = "organisation_service"
    binding.execution_principal_id = organisation_id
    binding.scope_type = "organisation"
    binding.audience_mode = "none"
    access = FakeAccess()
    enqueuer = FakeEnqueuer(access)

    invocation = ManualInvocationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        admin_id,
        is_admin=True,
        access=access,
        event_enqueuer=enqueuer,
    )
    result = await invocation.invoke(binding, invocation_id="shared-action", subjects=())

    assert len(result) == 1
    assert result[0].envelope.route.trigger_id == binding.trigger_id
