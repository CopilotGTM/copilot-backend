from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError
from src.api.schemas.crm_participant_models import (
    DealParticipantCreateItem,
    DealParticipantsCreateRequest,
    DealParticipantUpdateRequest,
)


def test_participant_create_item_normalizes_relationship_role() -> None:
    item = DealParticipantCreateItem(
        participant_type="user",
        related_id=UUID(int=1),
        relationship_role="  SE  ",
    )

    assert item.relationship_role == "SE"


@pytest.mark.parametrize("payload", [{}, {"participants": []}])
def test_participant_create_request_requires_at_least_one_participant(payload) -> None:
    with pytest.raises(ValidationError):
        DealParticipantsCreateRequest(**payload)


@pytest.mark.parametrize(
    "request_cls,payload",
    [
        (
            DealParticipantsCreateRequest,
            {
                "participants": [{"participant_type": "user", "related_id": str(UUID(int=1))}],
                "deal_id": str(UUID(int=2)),
            },
        ),
        (
            DealParticipantsCreateRequest,
            {
                "participants": [
                    {
                        "participant_type": "user",
                        "related_id": str(UUID(int=1)),
                        "role": "SE",
                    }
                ]
            },
        ),
        (DealParticipantUpdateRequest, {"relationship_role": "SE", "role": "Sales Engineer"}),
    ],
)
def test_participant_write_requests_reject_unknown_fields(request_cls, payload) -> None:
    with pytest.raises(ValidationError):
        request_cls(**payload)
