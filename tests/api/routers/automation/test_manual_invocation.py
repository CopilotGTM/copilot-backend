"""HTTP contracts for authenticated manual trigger invocation."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from pydantic import ValidationError
from src.api.routers.automation.triggers_handler import invoke_agent_trigger
from src.contracts.automation_trigger_models import (
    AutomationManualInvocationRequest,
    AutomationManualInvocationResponse,
    AutomationManualInvocationSubjectResult,
)


class Session:
    """Represent the Session test double used by the surrounding regression cases."""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        """Expose the commit operation expected by the Session test double."""
        self.committed = True

    async def rollback(self) -> None:
        """Expose the rollback operation expected by the Session test double."""
        self.rolled_back = True


class Service:
    """Represent the Service test double used by the surrounding regression cases."""

    def __init__(self) -> None:
        self.session = Session()
        self.body = None

    async def invoke_manual_trigger(self, agent_id, trigger_id, body):  # noqa: ANN001
        """Expose the invoke manual trigger operation expected by the Service test double."""
        self.body = body
        return AutomationManualInvocationResponse(
            invocation_id=body.invocation_id,
            correlation_id=f"manual:{trigger_id}:{body.invocation_id}",
            items=[
                AutomationManualInvocationSubjectResult(
                    event_id=uuid4(),
                    subject=body.subjects[0] if body.subjects else None,
                )
            ],
        )


def test_manual_request_rejects_server_owned_identity_fields() -> None:
    """Verify the contract that manual request rejects server owned identity fields."""
    payload = {
        "invocation_id": "ui-action-1",
        "subjects": [{"type": "deal", "id": str(uuid4())}],
    }

    body = AutomationManualInvocationRequest.model_validate(payload)
    assert body.subjects[0].type == "deal"

    for field_name in ("organisation_id", "event_id", "config_hash"):
        with pytest.raises(ValidationError):
            AutomationManualInvocationRequest.model_validate({**payload, field_name: str(uuid4())})


def test_manual_request_rejects_duplicate_subjects() -> None:
    """Verify the contract that manual request rejects duplicate subjects."""
    subject_id = uuid4()
    with pytest.raises(ValidationError, match="unique"):
        AutomationManualInvocationRequest.model_validate(
            {
                "invocation_id": "ui-action-2",
                "subjects": [
                    {"type": "deal", "id": str(subject_id)},
                    {"type": "deal", "id": str(subject_id)},
                ],
            }
        )


@pytest.mark.asyncio
async def test_manual_endpoint_commits_the_complete_fanout() -> None:
    """Verify the contract that manual endpoint commits the complete fanout."""
    service = Service()
    body = AutomationManualInvocationRequest.model_validate(
        {
            "invocation_id": "ui-action-3",
            "subjects": [{"type": "deal", "id": str(uuid4())}],
        }
    )

    result = await invoke_agent_trigger(
        agent_id=uuid4(),
        trigger_id=uuid4(),
        body=body,
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    assert result.data.invocation_id == "ui-action-3"
    assert result.data.items[0].subject == body.subjects[0]
    assert service.session.committed is True
    assert service.session.rolled_back is False
