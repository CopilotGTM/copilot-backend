"""Organisation membership policy persists through the shared preference document."""

import pytest
from src.api.schemas.membership_models import UpdateMembershipPolicyRequest
from src.domain.models.master_tables import Organisation
from src.service.orgnisation.membership_policy_service import MembershipPolicyService


class _Session:
    def __init__(self, organisation):
        self.organisation = organisation
        self.commits = 0

    async def get(self, _model, _identifier):
        return self.organisation

    async def commit(self):
        self.commits += 1

    async def refresh(self, _value):
        return None


@pytest.mark.asyncio
async def test_missing_membership_policy_reads_as_open() -> None:
    organisation = Organisation(organisation_name="Acme", preferences={})

    result = await MembershipPolicyService(
        _Session(organisation), organisation.organisation_id
    ).get_policy()

    assert result.domain_join_policy == "open"


@pytest.mark.asyncio
async def test_update_membership_policy_preserves_other_preferences() -> None:
    organisation = Organisation(
        organisation_name="Acme",
        preferences={"integrations": {"google": {"gmail_read_enabled": True}}},
    )
    session = _Session(organisation)

    result = await MembershipPolicyService(session, organisation.organisation_id).update_policy(
        UpdateMembershipPolicyRequest(domain_join_policy="invite_only")
    )

    assert result.domain_join_policy == "invite_only"
    assert organisation.preferences["integrations"]["google"]["gmail_read_enabled"] is True
    assert session.commits == 1
