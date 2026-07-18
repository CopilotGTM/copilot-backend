"""Shared authenticated-actor setup for CRM service regression tests.

Relationship rules are tested directly in ``test_record_access_policy.py``.
The older service tests in this package exercise query construction, mutation
semantics, and projections; they run as an explicit admin so those assertions
do not depend on the former fail-open behavior for a missing actor.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from src.domain.models.master_tables.user_models import UserAppRole
from src.service.crm.access.record_access_policy import (
    CRMAccessActor,
    DealAccessPolicy,
    RecordAccessDecision,
)
from src.service.crm.accounts import write_service as account_write_module
from src.service.crm.contacts import write_service as contact_write_module
from src.service.crm.deals import write_service as deal_write_module
from src.service.crm.deals.last_activity_service import DealLastActivityService

_ACTOR_ARGUMENTS = {
    "DealReadService": "actor",
    "DealWriteService": "access_actor",
    "NotesReadService": "access_actor",
    "NotesWriteService": "access_actor",
    "StakeholderReadService": "access_actor",
    "TaskReadService": "access_actor",
    "TaskWriteService": "access_actor",
}


def _authenticated_service_class(service_class: type[Any], actor_argument: str) -> type[Any]:
    """Return a test-only subclass that supplies an admin when none is explicit."""

    class AuthenticatedService(service_class):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # Admin-wide access is a product requirement. Supplying it explicitly
            # keeps legacy unit tests from relying on a missing-principal bypass.
            kwargs.setdefault(
                actor_argument,
                CRMAccessActor(uuid4(), UserAppRole.ADMIN),
            )
            super().__init__(*args, **kwargs)

    AuthenticatedService.__name__ = service_class.__name__
    return AuthenticatedService


@pytest.fixture(autouse=True)
def explicit_admin_for_legacy_service_tests(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Give service-mechanics tests an explicit principal without changing production defaults."""

    if request.module.__name__.endswith("test_record_access_policy"):
        return

    async def authorize_test_deal(
        _policy: DealAccessPolicy,
        deal_id: Any,
        _level: Any = "view",
    ) -> tuple[Any, RecordAccessDecision]:
        # Authorization decisions have their own requirement-based suite. These
        # older tests isolate downstream service behavior at that boundary.
        deal = SimpleNamespace(
            deal_id=deal_id,
            owner_email=None,
            owner_name=None,
            company_domain=None,
        )
        return deal, RecordAccessDecision(True, True, True, True)

    if not request.module.__name__.endswith("test_stakeholders_service_participant_reads"):
        monkeypatch.setattr(DealAccessPolicy, "require_deal", authorize_test_deal)

    async def resolve_test_crm_identity(*_args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            user_id=kwargs["app_user_id"],
            full_name="Test User",
            email="test-user@example.com",
        )

    # Identity materialization is covered by test_crm_user_identity_utils.py.
    # Mutation tests need only the independently required creator attribution.
    for write_module in (account_write_module, contact_write_module, deal_write_module):
        monkeypatch.setattr(
            write_module,
            "ensure_app_user_crm_identity",
            resolve_test_crm_identity,
        )

    if not request.module.__name__.endswith("test_deal_last_activity_service"):
        async def skip_last_activity_projection(_service: Any, _deal_id: Any) -> None:
            return None

        # Last-activity calculation has a focused suite; unrelated mutation
        # tests stop at this projection boundary instead of extending DB queues.
        monkeypatch.setattr(
            DealLastActivityService,
            "refresh_deal",
            skip_last_activity_projection,
        )

    module = request.module
    for class_name, actor_argument in _ACTOR_ARGUMENTS.items():
        service_class = getattr(module, class_name, None)
        if isinstance(service_class, type):
            monkeypatch.setattr(
                module,
                class_name,
                _authenticated_service_class(service_class, actor_argument),
            )
