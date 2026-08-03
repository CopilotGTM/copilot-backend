from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.service.crm.leads.contracts import LeadServiceError
from src.service.crm.leads.participant_service import LeadParticipantService


class _Result:
    def scalars(self):
        return self

    def first(self):
        return None


class _Session:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result()

    def add(self, _value):
        raise AssertionError("invalid related records must fail before insert")


@pytest.mark.asyncio
@pytest.mark.parametrize("participant_type", ["contact", "company", "user"])
async def test_related_local_id_must_resolve_inside_organisation(participant_type) -> None:
    organisation_id = uuid4()
    session = _Session()
    service = LeadParticipantService(session, organisation_id)
    service._get_lead_or_404 = AsyncMock()

    with pytest.raises(LeadServiceError, match="Related participant not found"):
        await service.add_participant(
            uuid4(),
            {
                "participant_type": participant_type,
                "related_local_id": uuid4(),
            },
        )

    compiled = session.statements[0].compile()
    assert organisation_id in compiled.params.values()
